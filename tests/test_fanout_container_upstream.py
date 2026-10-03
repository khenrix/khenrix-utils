"""The host sender keeps provider origins and response lifetimes fixed."""
from __future__ import annotations

import importlib
import base64
import hashlib
import http.client
import io
import json
import socket
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.lib.fanout.broker import Broker, BrokerPermit, BrokerRejected  # noqa: E402
from shared.lib.fanout.container_broker import FrameStream, HostRelay, PermitLedger, RelayRequest, dispatch_once  # noqa: E402
from shared.lib.fanout.controller import create_lifecycle_controller, read_evidence, write_evidence  # noqa: E402
from shared.lib.fanout.container_routes import RouteAdmission, RouteProfile  # noqa: E402


def _permit(provider: str) -> BrokerPermit:
    return BrokerPermit(
        provider=provider, run_id="run-1", task_id="task-1", target_id="target-1",
        seat_id="seat-1", route="initial", model="test-model",
        placeholder_key="disposable-placeholder", max_requests=1,
        max_request_bytes=1024, max_response_bytes=1024, max_output_tokens=32,
    )


def _request(provider: str) -> tuple[str, dict[str, str], bytes]:
    if provider == "claude":
        return "/v1/messages", {"x-api-key": "disposable-placeholder", "content-type": "application/json"}, b'{"model":"test-model","max_tokens":16,"messages":[]}'
    if provider == "codex":
        return "/v1/responses", {"x-fanout-seat": "disposable-placeholder", "content-type": "application/json"}, b'{"model":"test-model","max_output_tokens":16,"input":[]}'
    return "/v1beta/models/test-model:streamGenerateContent?alt=sse", {"x-goog-api-key": "disposable-placeholder", "content-type": "application/json"}, b'{"generationConfig":{"maxOutputTokens":16},"contents":[]}'


def _route(provider: str, family: str | None = None) -> RouteAdmission:
    if provider == "claude":
        values = ("vertex-adc", "anthropic-vertex-messages",
                  "https://europe-west1-aiplatform.googleapis.com", "fictional-project",
                  "europe-west1", "claude-opus-5-5", "claude-opus-5-5", "gcloud-adc")
    elif provider == "agy":
        values = ("vertex-adc", "google-vertex-generate-content",
                  "https://europe-west1-aiplatform.googleapis.com", "fictional-project",
                  "europe-west1", "gemini-3.8-flash-high", "gemini-3.8-flash", "gcloud-adc")
    else:
        values = ("openai-keychain", "openai-responses", "https://api.openai.com",
                  "fictional-openai-project", None, "gpt-6.1-sol", "gpt-6.1-sol",
                  "keychain:fixture-account")
    auth, api, origin, project, location, requested, wire, ref = values
    profile = RouteProfile(
        route_id=f"{provider}-approved", provider=provider, auth_family=family or auth,
        api_family=api, origin=origin, project=project, location=location,
        requested_model=requested, wire_model=wire, effective_model=wire,
        credential_ref=ref, max_data_classification="protected",
        execution_classes=("read-only", "repo-write"), transform_version=1,
    )
    return RouteAdmission("fictional-machine", "a" * 64, "b" * 64, profile)


def _routed_permit(route: RouteAdmission, path: str) -> BrokerPermit:
    return replace(_permit(route.profile.provider), model=route.profile.requested_model,
                   upstream_route_digest=route.digest,
                   upstream_origin=route.profile.origin,
                   upstream_model=route.profile.effective_model,
                   upstream_path=path)


def _routed_sender(upstream_module, provider: str, *, path: str | None = None,
                   max_request_bytes: int = 1024):
    route = _route(provider)
    if path is None:
        if provider == "claude":
            path = ("/v1/projects/fictional-project/locations/europe-west1/"
                    "publishers/anthropic/models/claude-opus-5-5:streamRawPredict")
        elif provider == "agy":
            path = ("/v1/projects/fictional-project/locations/europe-west1/"
                    "publishers/google/models/gemini-3.8-flash:streamGenerateContent?alt=sse")
        else:
            path = "/v1/responses"
    permit = replace(_routed_permit(route, path), max_request_bytes=max_request_bytes)
    return upstream_module.HostUpstream(permit, route, max_response_bytes=128,
                                        deadline_seconds=2), permit, route


def _routed_request(provider: str):
    if provider == "claude":
        return "/v1/messages", {"x-api-key": "fake-adc-token", "content-type": "application/json"}, b'{"model":"claude-opus-5-5","max_tokens":16,"messages":[],"stream":true}'
    if provider == "agy":
        return "/v1beta/models/gemini-3.8-flash:streamGenerateContent?alt=sse", {"x-goog-api-key": "fake-adc-token", "content-type": "application/json"}, b'{"generationConfig":{"maxOutputTokens":16},"contents":[]}'
    return "/v1/responses", {"authorization": "Bearer fake-openai-key", "content-type": "application/json"}, b'{"model":"gpt-6.1-sol","max_output_tokens":16,"input":[],"stream":true}'


@pytest.mark.parametrize("mime", ["application/json", "text/event-stream"])
def test_success_capture_requires_complete_safe_body(upstream_module, connection_factory, mime):
    sender, permit, route = _routed_sender(upstream_module, "codex")
    captured = []
    sender = upstream_module.HostUpstream(
        permit, route, max_response_bytes=128, deadline_seconds=2,
        success_sink=captured.append)
    data = b'{"ok":true}' if mime == "application/json" else b'data: {"ok":true}\n\n'
    _, observed = connection_factory
    observed.headers["content-type"] = mime
    observed.chunks = [data]
    path, headers, body = _routed_request("codex")
    result = sender("POST", path, headers, body)
    if mime == "text/event-stream":
        assert captured == []
        assert b"".join(result.body) == data
    else:
        assert result.body == data
    assert len(captured) == 1
    assert captured[0]["normalized_body"] == body
    assert captured[0]["effective_body"] == body
    assert captured[0]["response_body"] == data
    assert b"fake-openai-key" not in repr(captured).encode()
    assert "headers" not in captured[0]


def test_partial_stream_or_length_framing_never_captures_success(upstream_module, connection_factory):
    _, permit, route = _routed_sender(upstream_module, "codex")
    captured = []
    sender = upstream_module.HostUpstream(
        permit, route, max_response_bytes=128, deadline_seconds=2,
        success_sink=captured.append)
    _, observed = connection_factory
    observed.headers["content-type"] = "text/event-stream"
    observed.chunks = [b'data: {"ok":true}\n\n']
    response = sender("POST", *_routed_request("codex"))
    next(iter(response.body))
    response.body.close()
    assert captured == []
    observed.headers["content-type"] = "application/json"
    observed.chunks = [b'{"ok":true}']
    observed.length = 9
    with pytest.raises(BrokerRejected):
        sender("POST", *_routed_request("codex"))
    assert captured == []


@pytest.mark.parametrize("reflected", [False, True])
def test_success_capture_applies_existing_cross_event_disclosure_guard(
    upstream_module, connection_factory, tmp_path, reflected, capsys,
):
    from shared.lib.fanout.artifacts import ArtifactStore
    _, permit, route = _routed_sender(upstream_module, "codex")
    _, response = connection_factory
    response.headers["content-type"] = "text/event-stream"
    raw = (b'data: {"delta":"fake-openai-"}\n\ndata: {"delta":"key"}\n\n' if reflected else
           b'data: { "delta": "hello " }\r\n\r\ndata: {"delta":"world"}\r\n\r\n')
    response.chunks = [raw[:7], raw[7:31], raw[31:49], raw[49:]]
    root = tmp_path / "raw"
    with ArtifactStore(root) as store:
        existing = set(root.rglob("*"))
        captured = []
        def persist(payload):
            captured.append(payload)
            store.write_bytes("success.bin", payload["response_body"])
        sender = upstream_module.HostUpstream(permit, route, max_response_bytes=128,
            deadline_seconds=2, success_sink=persist)
        if reflected:
            with pytest.raises(BrokerRejected) as rejected:
                result = sender("POST", *_routed_request("codex"))
                b"".join(result.body)
            assert "fake-openai-" not in str(rejected.value)
            assert captured == []
            assert set(root.rglob("*")) == existing
        else:
            result = sender("POST", *_routed_request("codex"))
            assert b"".join(result.body) == raw
            assert len(captured) == 1
            assert captured[0]["delivered_body"] == b'data: {"delta":"hello "}\n\ndata: {"delta":"world"}\n\n'
    output = capsys.readouterr()
    assert "fake-openai-" not in output.out + output.err


def test_success_capture_refuses_normalized_stream_over_finite_budget(
    upstream_module, connection_factory,
):
    _, permit, route = _routed_sender(upstream_module, "codex")
    _, response = connection_factory
    response.headers["content-type"] = "text/event-stream"
    response.chunks = [b"data:{}\n\n"]
    captured = []
    sender = upstream_module.HostUpstream(permit, route, max_response_bytes=9,
        deadline_seconds=2, success_sink=captured.append)
    with pytest.raises(BrokerRejected, match="finite byte budget"):
        b"".join(sender("POST", *_routed_request("codex")).body)
    assert captured == []


@pytest.fixture
def upstream_module():
    try:
        return importlib.import_module("shared.lib.fanout.container_upstream")
    except ModuleNotFoundError:
        class MissingUpstream:
            def HostUpstream(self, *_args, **_kwargs):
                pytest.fail("host upstream module is missing")
        return MissingUpstream()


class FakeResponse:
    def __init__(self, *, status=200, mime="application/json", chunks=(b'{"ok":true}',),
                 extra_headers=None):
        self.status = status
        self.headers = {"content-type": mime, **(extra_headers or {})}
        self.chunks = list(chunks)
        self.length = None
        self.closed = False

    def getheader(self, name, default=None):
        return self.headers.get(name.lower(), default)

    def read(self, amount):
        if not self.chunks:
            return b""
        chunk = self.chunks.pop(0)
        assert amount > 0
        if len(chunk) > amount:
            self.chunks.insert(0, chunk[amount:])
        return chunk[:amount]

    def read1(self, amount):
        return self.read(amount)

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, host, port, *, timeout, context, response):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.context = context
        self.response = response
        self.closed = False
        self.closed_event = threading.Event()
        self.debuglevel = 99
        self.requests = []
        self.sock = None
        self.timeouts = []

    def connect(self):
        self.sock = self

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def set_debuglevel(self, level):
        self.debuglevel = level

    def request(self, method, path, body=None, headers=None):
        if self.sock is None and getattr(self, "auto_open", True):
            self.connect()
        assert self.sock is not None and not self.closed
        self.requests.append((method, path, body, headers))

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True
        self.sock = None
        self.closed_event.set()


@pytest.fixture
def connection_factory(monkeypatch):
    made = []
    response = FakeResponse()

    def make(host, port, *, timeout, context):
        connection = FakeConnection(host, port, timeout=timeout, context=context,
                                    response=response)
        made.append(connection)
        return connection

    monkeypatch.setattr("http.client.HTTPSConnection", make)
    return made, response
def test_vertex_claude_preview_pins_eu_endpoint_and_removes_guest_model(upstream_module):
    sender, _, route = _routed_sender(upstream_module, "claude")
    path, headers, body = _routed_request("claude")
    outbound = sender.preview("POST", path, headers, body)
    assert outbound.origin == route.profile.origin
    assert outbound.path.endswith(
        "/publishers/anthropic/models/claude-opus-5-5:streamRawPredict")
    assert json.loads(outbound.body) == {
        "anthropic_version": "vertex-2023-10-16", "max_tokens": 16,
        "messages": [], "stream": True,
    }
    assert outbound.route_digest == route.digest


@pytest.mark.parametrize("protocol_names", [
    ("anthropic-beta", "anthropic-version"),
    ("Anthropic-Beta", "AnThRoPiC-VeRsIoN"),
    None,
], ids=["lowercase", "mixed-case", "absent"])
def test_vertex_claude_protocol_headers_survive_real_relay_delivery(
    upstream_module, connection_factory, tmp_path, protocol_names,
):
    made, _ = connection_factory
    regional = _route("claude")
    route = replace(regional, profile=replace(
        regional.profile, location="eu", origin="https://aiplatform.eu.rep.googleapis.com"))
    upstream_path = ("/v1/projects/fictional-project/locations/eu/publishers/anthropic/"
                     "models/claude-opus-5-5:streamRawPredict")
    permit = replace(_routed_permit(route, upstream_path), max_request_bytes=4096,
                     max_output_tokens=1024, clamp_output_tokens=True)
    sender = upstream_module.HostUpstream(permit, route, max_response_bytes=128,
                                         deadline_seconds=2)
    broker = Broker(permit, credential="fake-adc-token", send=sender)
    controller = create_lifecycle_controller(tmp_path / "controller")
    ledger = PermitLedger(controller.root / "locks" / "spend", controller)
    permit_id = ledger.register(permit, 1)
    beta = ("claude-code-20250219,interleaved-thinking-2025-05-14,"
            "thinking-token-count-2026-05-13,context-management-2025-06-27,"
            "prompt-caching-scope-2026-01-05,mid-conversation-system-2026-04-07,"
            "per-turn-control-2026-07-01,mid-conversation-tool-changes-2026-07-01,"
            "effort-2025-11-24,synthetic-unknown-2099-01-01")
    headers = {"x-api-key": "disposable-placeholder", "content-type": "application/json"}
    if protocol_names is not None:
        headers.update({protocol_names[0]: beta, protocol_names[1]: "2023-06-01"})
    body = json.dumps({
        "model": "claude-opus-5-5", "max_tokens": 128000, "stream": True,
        "thinking": {"type": "adaptive", "display": "omitted"},
        "output_config": {"effort": "xhigh"},
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "Count pebbles."},
                                           {"type": "text", "text": "Use the tool."}]},
            {"role": "system", "output_config": {"effort": "xhigh"},
             "content": [{"type": "text", "text": "Check the count.",
                          "cache_control": {"type": "ephemeral"}}]},
        ],
        "tools": [{"name": "count", "description": "Count pebbles",
                   "input_schema": {"type": "object", "properties": {}}}],
    }).encode()
    request = {"type": "request", "permit_id": permit_id, "epoch": 1,
               "request_id": "capabilities", "method": "POST",
               "provider_path": "/v1/messages?beta=true", "headers": headers,
               "body_b64": base64.b64encode(body).decode()}
    response_digest = hashlib.sha256(b'{"ok":true}').hexdigest()
    incoming = io.BytesIO()
    guest = FrameStream(io.BytesIO(), incoming)
    for frame in [
        {"type": "ready", "seat_id": "seat-1", "permit_id": permit_id, "epoch": 1},
        request,
        {"type": "response_ack", "request_id": "capabilities",
         "response_digest": response_digest},
        {"type": "close"},
    ]:
        guest.send(frame)
    outgoing = io.BytesIO()
    relay = HostRelay(ledger, broker)
    relay.serve(FrameStream(io.BytesIO(incoming.getvalue()), outgoing))
    received = FrameStream(io.BytesIO(outgoing.getvalue()), io.BytesIO())
    frames = [received.recv() for _ in range(5)]
    assert frames == [
        {"type": "ready_ack"},
        {"type": "response_start", "request_id": "capabilities", "status": 200,
         "headers": {"content-type": "application/json"}},
        {"type": "response_chunk", "request_id": "capabilities",
         "body_b64": "eyJvayI6dHJ1ZX0="},
        {"type": "response_end", "request_id": "capabilities",
         "response_digest": response_digest},
        {"type": "closed"},
    ]
    assert len(made) == 1 and len(made[0].requests) == 1 and made[0].closed
    assert (made[0].host, made[0].port) == ("aiplatform.eu.rep.googleapis.com", 443)
    method, sent_path, sent_body, sent_headers = made[0].requests[0]
    assert (method, sent_path) == ("POST", upstream_path)
    assert sent_headers == {
        "content-type": "application/json", "authorization": "Bearer fake-adc-token",
        "x-goog-user-project": "fictional-project",
        **({"anthropic-beta": beta, "anthropic-version": "2023-06-01"}
           if protocol_names is not None else {}),
    }
    expected = {
        "anthropic_version": "vertex-2023-10-16", "max_tokens": 1024, "stream": True,
        "thinking": {"type": "adaptive", "display": "omitted"},
        "output_config": {"effort": "xhigh"},
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "Count pebbles."},
                                           {"type": "text", "text": "Use the tool."}]},
            {"role": "system", "output_config": {"effort": "xhigh"},
             "content": [{"type": "text", "text": "Check the count.",
                          "cache_control": {"type": "ephemeral"}}]},
        ],
        "tools": [{"name": "count", "description": "Count pebbles",
                   "input_schema": {"type": "object", "properties": {}}}],
    }
    assert json.loads(sent_body) == expected
    expected_wire = (json.dumps(expected, sort_keys=True, separators=(",", ":")) + "\n").encode()
    assert sent_body == expected_wire
    normalized = {key: value for key, value in expected.items() if key != "anthropic_version"}
    normalized["model"] = "claude-opus-5-5"
    normalized_body = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
    records = PermitLedger(ledger.root, controller).request_records(permit_id, 1)
    assert records == ({
        "request_id": "capabilities", "permit_id": permit_id, "epoch": 1,
        "state": "settled", "response_digest": response_digest,
        "identity": {
            "method": "POST", "path": "/v1/messages",
            "body_sha256": hashlib.sha256(normalized_body).hexdigest(),
            "effective_output_tokens": 1024, "wire_model": "claude-opus-5-5",
            "upstream_route_digest": route.digest,
            "upstream_origin": "https://aiplatform.eu.rep.googleapis.com",
            "upstream_path": upstream_path,
            "upstream_body_sha256": hashlib.sha256(expected_wire).hexdigest(),
            "upstream_model": "claude-opus-5-5",
        },
    },)
    assert broker.effective_output_tokens == (1024,)
    assert ledger.attempt_counts(permit_id, 1) == (1, 0)
    with ledger._locked() as state:
        assert state["permits"][permit_id]["used"] == 1
        assert state["send_claims"] == {"capabilities": True}
    duplicate = io.BytesIO()
    duplicate_writer = FrameStream(io.BytesIO(), duplicate)
    duplicate_writer.send({"type": "ready", "seat_id": "seat-1", "permit_id": permit_id, "epoch": 1})
    duplicate_writer.send(request)
    with pytest.raises(BrokerRejected, match="attempt budget is exhausted"):
        relay.serve(FrameStream(io.BytesIO(duplicate.getvalue()), io.BytesIO()))
    with pytest.raises(BrokerRejected, match="replay refused"):
        dispatch_once(RelayRequest(permit_id, 1, "capabilities", "POST",
                                   "/v1/messages?beta=true", headers, body), ledger, broker)
    assert len(made) == 1 and len(made[0].requests) == 1
    assert ledger.attempt_counts(permit_id, 1) == (1, 0)
    assert ledger.request_records(permit_id, 1) == records


@pytest.mark.parametrize("protocol_headers", [
    {"anthropic-beta": "effort-2025-11-24\r\nInjected: true"},
    {"anthropic-beta": "effort-2025-11-24", "Anthropic-Beta": "other"},
])
def test_vertex_claude_protocol_headers_reject_injection_and_duplicates(
    upstream_module, connection_factory, protocol_headers,
):
    made, _ = connection_factory
    sender, _, _ = _routed_sender(upstream_module, "claude")
    path, headers, body = _routed_request("claude")
    with pytest.raises(BrokerRejected, match="headers are invalid"):
        sender("POST", path, {**headers, **protocol_headers}, body)
    assert made == []


def test_vertex_gemini_keeps_exact_wire_model(upstream_module):
    sender, _, _ = _routed_sender(upstream_module, "agy")
    path, headers, body = _routed_request("agy")
    outbound = sender.preview("POST", path, headers, body)
    assert outbound.path.endswith(
        "/publishers/google/models/gemini-3.8-flash:streamGenerateContent?alt=sse")
    assert outbound.body == body


def test_openai_preview_pins_responses_path_and_excludes_credential(upstream_module):
    sender, _, route = _routed_sender(upstream_module, "codex")
    path, headers, body = _routed_request("codex")
    outbound = sender.preview("POST", path, headers, body)
    assert (outbound.origin, outbound.path, outbound.body, outbound.effective_model) == (
        "https://api.openai.com", "/v1/responses", body, "gpt-6.1-sol")
    assert outbound.route_digest == route.digest
    assert b"fake-openai-key" not in outbound.body
    assert "fake-openai-key" not in repr(outbound)


@pytest.mark.parametrize("provider,expected_path,expected_model,expected_body", [
    ("claude", "/v1/projects/fictional-project/locations/eu/publishers/anthropic/models/"
     "claude-opus-5-5:streamRawPredict", "claude-opus-5-5",
     b'{"anthropic_version":"vertex-2023-10-16","max_tokens":16,"messages":[],"stream":true}\n'),
    ("agy", "/v1/projects/fictional-project/locations/eu/publishers/google/models/"
     "gemini-3.8-flash:streamGenerateContent?alt=sse", "gemini-3.8-flash",
     b'{"generationConfig":{"maxOutputTokens":16},"contents":[]}'),
])
def test_eu_preview_and_authenticated_request_identity_keep_exact_route(
    upstream_module, connection_factory, tmp_path, provider, expected_path, expected_model, expected_body,
):
    made, _ = connection_factory
    regional = _route(provider)
    route = replace(regional, profile=replace(regional.profile, location="eu",
                                             origin="https://aiplatform.eu.rep.googleapis.com"))
    permit = _routed_permit(route, expected_path)
    sender = upstream_module.HostUpstream(permit, route, max_response_bytes=128, deadline_seconds=2)
    path, headers, body = _routed_request(provider)
    preview = sender.preview("POST", path, headers, body)
    assert (preview.origin, preview.path, preview.effective_model, preview.body) == (
        "https://aiplatform.eu.rep.googleapis.com", expected_path, expected_model, expected_body)
    assert preview.route_digest == route.digest and route.digest != regional.digest
    assert made == []

    controller = create_lifecycle_controller(tmp_path / "controller")
    ledger = PermitLedger(tmp_path / "spend", controller)
    permit_id = ledger.register(permit, 1)
    ledger.record_attempt(permit_id, 1)
    auth_name = "x-api-key" if provider == "claude" else "x-goog-api-key"
    guest_headers = {**headers, auth_name: "disposable-placeholder"}
    response = dispatch_once(RelayRequest(permit_id, 1, "request-eu", "POST", path,
                                          guest_headers, body), ledger,
                             Broker(permit, credential="fake-adc-token", send=sender))
    assert b"".join(response.body) == b'{"ok":true}'
    assert made[0].host == "aiplatform.eu.rep.googleapis.com"
    assert made[0].requests[0][1:3] == (expected_path, expected_body)
    assert made[0].requests[0][3]["x-goog-user-project"] == "fictional-project"
    records = ledger.request_records(permit_id, 1)
    assert len(records) == 1
    identity = records[0]["identity"]
    assert (identity["upstream_origin"], identity["upstream_path"], identity["upstream_model"],
            identity["upstream_body_sha256"], identity["upstream_route_digest"]) == (
        "https://aiplatform.eu.rep.googleapis.com", expected_path, expected_model,
        hashlib.sha256(expected_body).hexdigest(), route.digest)
    record_path = tmp_path / "spend/ledger.json"
    document = json.loads(record_path.read_bytes())
    document["payload"]["requests"]["request-eu"]["identity"]["upstream_origin"] = regional.profile.origin
    record_path.write_text(json.dumps(document))
    with pytest.raises(BrokerRejected, match="integrity"):
        PermitLedger(tmp_path / "spend", controller).request_records(permit_id, 1)


def test_openai_mixed_case_project_is_bound_exactly_in_host_header(
    upstream_module, connection_factory,
):
    made, _ = connection_factory
    original = _route("codex")
    route = replace(original, profile=replace(original.profile, project="proj_FictionalA9z",
                                             credential_ref="keychain:cli|fixture"))
    permit = _routed_permit(route, "/v1/responses")
    sender = upstream_module.HostUpstream(permit, route, max_response_bytes=128, deadline_seconds=2)
    path, headers, body = _routed_request("codex")
    preview = sender.preview("POST", path, headers, body)
    assert preview.effective_model == "gpt-6.1-sol"
    assert preview.body == b'{"model":"gpt-6.1-sol","max_output_tokens":16,"input":[],"stream":true}'
    sender("POST", path, headers, body)
    assert made[0].requests[0][3]["openai-project"] == "proj_FictionalA9z"
    assert route.digest != original.digest
    with pytest.raises(BrokerRejected, match="headers"):
        sender.preview("POST", path, {**headers, "openai-project": "proj_fictionala9z"}, body)


@pytest.mark.parametrize("provider", ("claude", "agy", "codex"))
def test_preview_and_send_share_transform_and_host_auth(
    upstream_module, connection_factory, provider,
):
    made, _ = connection_factory
    sender, _, _ = _routed_sender(upstream_module, provider)
    path, headers, body = _routed_request(provider)
    preview = sender.preview("POST", path, headers, body)
    assert made == []
    sender("POST", path, headers, body)
    assert len(made) == 1
    method, sent_path, sent_body, sent_headers = made[0].requests[0]
    assert (method, sent_path, sent_body) == ("POST", preview.path, preview.body)
    assert made[0].host == preview.origin.removeprefix("https://")
    assert sent_headers["authorization"] == (
        "Bearer fake-openai-key" if provider == "codex" else "Bearer fake-adc-token")
    assert sent_headers.get("openai-project") == (
        "fictional-openai-project" if provider == "codex" else None)
    assert "x-api-key" not in sent_headers and "x-goog-api-key" not in sent_headers


def test_mismatched_permit_route_refuses_before_connect(upstream_module, connection_factory):
    made, _ = connection_factory
    route = _route("codex")
    permit = replace(_routed_permit(route, "/v1/responses"),
                     upstream_route_digest="c" * 64)
    with pytest.raises(BrokerRejected, match="route|permit"):
        upstream_module.HostUpstream(permit, route, max_response_bytes=128,
                                     deadline_seconds=2)
    assert made == []


def test_guest_selected_origin_or_project_header_refuses(upstream_module, connection_factory):
    made, _ = connection_factory
    sender, _, _ = _routed_sender(upstream_module, "codex")
    path, headers, body = _routed_request("codex")
    for bad_path, bad_headers in (
        ("https://evil.invalid/v1/responses", headers),
        (path, {**headers, "openai-project": "evil-project"}),
        (path, {**headers, "host": "evil.invalid"}),
    ):
        with pytest.raises(BrokerRejected):
            sender.preview("POST", bad_path, bad_headers, body)
    assert made == []


def test_vertex_transform_over_request_budget_refuses_before_connect(
    upstream_module, connection_factory,
):
    made, _ = connection_factory
    path, headers, body = _routed_request("claude")
    sender, _, _ = _routed_sender(upstream_module, "claude",
                                  max_request_bytes=len(body) + 1)
    with pytest.raises(BrokerRejected, match="budget"):
        sender("POST", path, headers, body)
    assert made == []


@pytest.mark.parametrize("status", (401, 403, 429, 302))
def test_auth_quota_and_redirect_responses_are_not_provider_answers(
    upstream_module, connection_factory, status,
):
    made, response = connection_factory
    response.status = status
    sender, _, _ = _routed_sender(upstream_module, "codex")
    path, headers, body = _routed_request("codex")
    with pytest.raises(BrokerRejected):
        sender("POST", path, headers, body)
    assert len(made) == 1 and made[0].closed


def test_subscription_route_never_uses_direct_key_sender(upstream_module, connection_factory):
    made, _ = connection_factory
    route = _route("codex")
    subscription = replace(route, profile=replace(
        route.profile, auth_family="codex-subscription",
        api_family="codex-subscription", project=None,
        credential_ref="adapter:codex", execution_classes=("read-only",)))
    permit = _routed_permit(subscription, "/v1/responses")
    with pytest.raises(BrokerRejected):
        upstream_module.HostUpstream(permit, subscription, max_response_bytes=128,
                                     deadline_seconds=2)
    assert made == []


def test_route_send_uses_verified_system_tls_without_ambient_ca_or_keylog(
    upstream_module, connection_factory, monkeypatch, tmp_path,
):
    made, _ = connection_factory
    keylog = tmp_path / "tls-keys"
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "attacker-ca"))
    monkeypatch.setenv("SSLKEYLOGFILE", str(keylog))
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8888")
    sender, _, _ = _routed_sender(upstream_module, "codex")
    sender("POST", *_routed_request("codex"))
    assert made[0].context.verify_mode == upstream_module.ssl.CERT_REQUIRED
    assert made[0].context.check_hostname is True
    assert made[0].context.keylog_filename is None
    assert made[0].debuglevel == 0
    assert made[0].auto_open is False
    assert not keylog.exists()


def test_json_response_byte_cap_and_encoding_refusal_close_connection(
    upstream_module, connection_factory,
):
    made, response = connection_factory
    sender, _, _ = _routed_sender(upstream_module, "codex")
    path, headers, body = _routed_request("codex")
    response.chunks = [b"x" * 129]
    with pytest.raises(BrokerRejected, match="budget"):
        sender("POST", path, headers, body)
    assert made[0].closed
    response.chunks = [b'{}']
    response.headers["content-encoding"] = "gzip"
    with pytest.raises(BrokerRejected, match="unsafe"):
        sender("POST", path, headers, body)
    assert made[1].closed


def test_sse_stream_is_lazy_bounded_and_closes_on_completion(
    upstream_module, connection_factory,
):
    made, response = connection_factory
    response.headers["content-type"] = "text/event-stream"
    response.chunks = [b'data: {"ok":true}\n\n', b'data: [DONE]\n\n']
    sender, _, _ = _routed_sender(upstream_module, "claude")
    path, headers, body = _routed_request("claude")
    stream = sender("POST", path, headers, body).body
    assert not made[0].closed
    assert list(stream) == [b'data: {"ok":true}\n\n', b'data: [DONE]\n\n']
    assert made[0].closed and response.closed
    response.closed = False
    response.chunks = [b"x" * 129]
    with pytest.raises(BrokerRejected, match="budget"):
        list(sender("POST", path, headers, body).body)
    assert made[1].closed


@pytest.mark.parametrize("framing", ("content-length", "chunked"))
def test_sse_first_event_arrives_before_gated_later_event(upstream_module, framing):
    first = b'data: {"ok":true}\n\n'
    later = b'data: [DONE]\n\n'
    client, provider = socket.socketpair()
    connection = http.client.HTTPConnection("localhost")
    connection.sock = client
    if framing == "chunked":
        headers = b"Transfer-Encoding: chunked\r\n"
        first_wire = f"{len(first):X}\r\n".encode() + first + b"\r\n"
        later_wire = f"{len(later):X}\r\n".encode() + later + b"\r\n0\r\n\r\n"
    else:
        headers = f"Content-Length: {len(first) + len(later)}\r\n".encode()
        first_wire, later_wire = first, later
    provider.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n" +
                     headers + b"\r\n" + first_wire)
    response = http.client.HTTPResponse(client)
    response.begin()
    deadline = time.monotonic() + 2
    lease = upstream_module._TransportLease(connection, deadline)
    lease.attach(response)
    lease.start()
    stream = upstream_module._SSEBody(lease, response, 128, deadline)
    delivered = threading.Event()
    received = []
    failures = []

    def receive_first():
        try:
            received.append(next(stream))
        except Exception as error:
            failures.append(error)
        finally:
            delivered.set()

    reader = threading.Thread(target=receive_first, daemon=True)
    reader.start()
    try:
        arrived_before_later = delivered.wait(0.5)
        provider.sendall(later_wire)
        reader.join(timeout=1)
        assert arrived_before_later, "complete first SSE event buffered behind later event"
        assert failures == []
        assert received == [first]
        assert b"".join(stream) == later
    finally:
        stream.close()
        provider.close()
        reader.join(timeout=1)


def test_sse_stalled_socket_stops_at_absolute_deadline(upstream_module):
    client, provider = socket.socketpair()
    connection = http.client.HTTPConnection("localhost")
    connection.sock = client
    provider.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                     b"Transfer-Encoding: chunked\r\n\r\n")
    response = http.client.HTTPResponse(client)
    response.begin()
    deadline = time.monotonic() + 0.1
    lease = upstream_module._TransportLease(connection, deadline)
    lease.attach(response)
    lease.start()
    stream = upstream_module._SSEBody(lease, response, 128, deadline)
    started = time.monotonic()
    try:
        with pytest.raises(BrokerRejected, match="stream failed|deadline expired"):
            next(stream)
        assert time.monotonic() - started < 1
        assert connection.sock is None
    finally:
        stream.close()
        provider.close()


def test_late_connect_and_tls_failure_never_retry_or_reflect_credential(
    upstream_module, connection_factory, monkeypatch,
):
    made, _ = connection_factory
    tick = [0.0]
    monkeypatch.setattr(upstream_module.time, "monotonic", lambda: tick[0])

    def late_connect(connection):
        tick[0] = 3.0
        connection.sock = connection

    monkeypatch.setattr(FakeConnection, "connect", late_connect)
    sender, _, _ = _routed_sender(upstream_module, "codex")
    path, headers, body = _routed_request("codex")
    with pytest.raises(BrokerRejected, match="deadline"):
        sender("POST", path, headers, body)
    assert made[0].requests == [] and made[0].closed
    tick[0] = 0.0

    def fail_connect(_connection):
        raise OSError("fake-openai-key from private TLS failure")

    monkeypatch.setattr(FakeConnection, "connect", fail_connect)
    with pytest.raises(BrokerRejected) as caught:
        sender("POST", path, headers, body)
    assert len(made) == 2 and made[1].requests == [] and made[1].closed
    assert "fake-openai-key" not in str(caught.value)


def test_inflight_request_is_closed_at_finite_deadline(
    upstream_module, connection_factory, monkeypatch,
):
    made, _ = connection_factory
    _, permit, route = _routed_sender(upstream_module, "codex")
    observed = []

    def stalled_request(connection, *_args, **_kwargs):
        observed.append(connection.closed_event.wait(timeout=0.5))
        raise TimeoutError("fake-openai-key from cancelled request")

    monkeypatch.setattr(FakeConnection, "request", stalled_request)
    sender = upstream_module.HostUpstream(permit, route, max_response_bytes=128,
                                          deadline_seconds=0.1)
    with pytest.raises(BrokerRejected) as caught:
        sender("POST", *_routed_request("codex"))
    assert observed == [True] and made[0].closed
    assert "fake-openai-key" not in str(caught.value)


def test_stream_read_failure_closes_and_hides_private_error(
    upstream_module, connection_factory,
):
    made, response = connection_factory
    response.headers["content-type"] = "text/event-stream"

    def fail(_amount):
        raise OSError("fake-adc-token from private upstream")

    response.read = fail
    sender, _, _ = _routed_sender(upstream_module, "claude")
    with pytest.raises(BrokerRejected) as caught:
        list(sender("POST", *_routed_request("claude")).body)
    assert made[0].closed
    assert "fake-adc-token" not in str(caught.value)


def test_bad_route_model_and_missing_token_limit_refuse_before_connect(
    upstream_module, connection_factory,
):
    made, _ = connection_factory
    sender, _, _ = _routed_sender(upstream_module, "codex")
    path, headers, body = _routed_request("codex")
    for bad_body in (body.replace(b"gpt-6.1-sol", b"other-model"),
                     b'{"model":"gpt-6.1-sol","input":[],"stream":true}'):
        with pytest.raises(BrokerRejected):
            sender("POST", path, headers, bad_body)
    assert made == []


def test_invalid_json_media_or_nonfinite_body_refuses_before_connect(
    upstream_module, connection_factory,
):
    made, _ = connection_factory
    sender, _, _ = _routed_sender(upstream_module, "codex")
    path, headers, body = _routed_request("codex")
    for bad_headers, bad_body in (
        ({**headers, "content-type": "text/plain"}, body),
        (headers, body.replace(b'"input":[]', b'"input":[],"temperature":NaN')),
    ):
        with pytest.raises(BrokerRejected):
            sender.preview("POST", path, bad_headers, bad_body)
    assert made == []


def test_guest_placeholder_never_becomes_host_authorization(
    upstream_module, connection_factory,
):
    made, _ = connection_factory
    sender, _, _ = _routed_sender(upstream_module, "claude")
    path, headers, body = _routed_request("claude")
    with pytest.raises(BrokerRejected):
        sender("POST", path, {**headers, "x-api-key": "disposable-placeholder"}, body)
    assert made == []


def _diagnostic_sender(module, provider="codex", sink=None, limit=128):
    _, permit, route = _routed_sender(module, provider)
    try:
        return module.HostUpstream(permit, route, max_response_bytes=limit,
                                   deadline_seconds=2, failure_sink=sink), permit, route
    except TypeError:
        pytest.fail("existing host sender cannot retain private failure diagnostics")


@pytest.mark.parametrize("status", (401, 403, 429, 500))
def test_private_upstream_failure_survives_worker_erasure_and_fixed_relay_reject(
    upstream_module, connection_factory, tmp_path, status,
):
    made, response = connection_factory
    response.status = status
    response.chunks = [b'{"error":{"code":"denied","message":"bounded denial","details":"omit"}}']
    controller = create_lifecycle_controller(tmp_path / "controller")
    captured = []
    def sink(payload):
        captured.append(write_evidence(controller, "container-upstream-failure", payload))
    sender, permit, route = _diagnostic_sender(upstream_module, sink=sink)
    ledger = PermitLedger(controller.root / "locks" / "spend", controller)
    permit_id = ledger.register(permit, 1)
    path, _, body = _routed_request("codex")
    headers = {"x-fanout-seat": "disposable-placeholder", "content-type": "application/json"}
    class Frames:
        def __init__(self):
            self.sent = []
            self.incoming = iter([
                {"type": "ready", "seat_id": "seat-1", "permit_id": permit_id, "epoch": 1},
                {"type": "request", "permit_id": permit_id, "epoch": 1, "request_id": "private-error",
                 "method": "POST", "provider_path": path, "headers": headers,
                 "body_b64": __import__("base64").b64encode(body).decode()},
                {"type": "close"},
            ])
        def recv(self): return next(self.incoming)
        def send(self, frame): self.sent.append(frame)
    frames = Frames()
    HostRelay(ledger, Broker(permit, credential="fake-openai-key", send=sender)).serve(frames)
    assert frames.sent == [{"type": "ready_ack"}, {"type": "reject", "status": 403}, {"type": "closed"}]
    assert len(captured) == len(made) == 1 and made[0].closed
    diagnostic = read_evidence(controller, "container-upstream-failure", *captured[0])
    assert diagnostic["http_status"] == status
    assert diagnostic["phase"] == "response_headers"
    assert diagnostic["category"] == "non2xx"
    assert diagnostic["structured_error"] == {"code": "denied", "message": "bounded denial"}
    assert diagnostic["upstream_route_digest"] == route.digest
    assert diagnostic["normalized_body_sha256"] == diagnostic["upstream_body_sha256"] == hashlib.sha256(body).hexdigest()
    record = ledger.request_records(permit_id, 1)[0]
    assert record["state"] == "uncertain" and record["response_digest"] is None
    with ledger._locked() as state:
        assert state["send_claims"] == {"private-error": True}
    assert b"fake-openai-key" not in json.dumps(diagnostic).encode()


@pytest.mark.parametrize("body,mime,encoding,status,location,omission", [
    (b'{"error":{"code":7,"status":"PERMISSION_DENIED","message":"safe","details":"omit"}}', "application/json", None, 403, None, None),
    (b'{"error":{"message":"fake-openai-key"}}', "application/json", None, 403, None, "unsafe_body"),
    (b'{"error":{"message":"fake-openai\\u002dkey"}}', "application/json", None, 403, None, "unsafe_body"),
    (b'{"error":{"message":"{\\"key\\":\\"fake-openai\\\\u002dkey\\"}"}}', "application/json", None, 403, None, "unsafe_body"),
    (b'{"error":{"code":1,"code":2}}', "application/json", None, 403, None, "unsafe_body"),
    (b'{"error":{"code":NaN}}', "application/json", None, 403, None, "unsafe_body"),
    (b'{"error":[]}', "application/json", None, 403, None, "wrong_shape"),
    (b'{"error":', "application/json", None, 403, None, "unsafe_body"),
    (b'\xff', "application/json", None, 403, None, "unsafe_body"),
    (b'x' * 129, "application/json", None, 403, None, "body_budget"),
    (b'private html', "text/html", None, 403, None, "unsupported_media"),
    (b'private gzip', "application/json", "gzip", 403, None, "unsafe_encoding"),
    (b'private redirect', "application/json", None, 302, None, "redirect_or_location"),
    (b'private location', "application/json", None, 200, "https://secret.invalid", "redirect_or_location"),
    (b'private invalid status', "application/json", None, "invalid", None, "invalid_status"),
])
def test_private_upstream_failure_omits_unsafe_complete_body(
    upstream_module, connection_factory, body, mime, encoding, status, location, omission,
):
    made, response = connection_factory
    response.status = status; response.headers = {"content-type": mime}
    if encoding is not None: response.headers["content-encoding"] = encoding
    if location is not None: response.headers["location"] = location
    response.chunks = [body]
    diagnostics = []
    sender, _, _ = _diagnostic_sender(upstream_module, sink=diagnostics.append)
    with pytest.raises(BrokerRejected): sender("POST", *_routed_request("codex"))
    assert len(diagnostics) == len(made) == 1 and made[0].closed
    diagnostic = diagnostics[0]
    assert diagnostic["http_status"] == (status if type(status) is int else None) and diagnostic["omission"] == omission
    if omission is None:
        assert diagnostic["structured_error"] == {"code": 7, "status": "PERMISSION_DENIED", "message": "safe"}
    else: assert diagnostic["structured_error"] is None
    assert all(marker not in json.dumps(diagnostic) for marker in ("fake-openai-key", "private", "secret.invalid", "details"))


@pytest.mark.parametrize("operation,phase", [("connect", "connect"), ("request", "request_write"), ("getresponse", "response_headers"), ("read", "json_body"), ("stream", "sse_body")])
def test_private_upstream_failure_captures_actual_transport_phase_without_exception(
    upstream_module, connection_factory, monkeypatch, operation, phase,
):
    made, response = connection_factory
    class HostileError(OSError):
        def __repr__(self): raise AssertionError("exception repr was read")
        def __str__(self): raise AssertionError("exception text was read")
    def fail(*_args, **_kwargs): raise HostileError("fake-openai-key")
    if operation in {"read", "stream"}:
        response.read = fail
        if operation == "stream": response.headers["content-type"] = "text/event-stream"
    else: monkeypatch.setattr(FakeConnection, operation, fail)
    diagnostics = []
    sender, _, _ = _diagnostic_sender(upstream_module, sink=diagnostics.append)
    with pytest.raises(BrokerRejected):
        result = sender("POST", *_routed_request("codex"))
        if operation == "stream": list(result.body)
    assert len(diagnostics) == len(made) == 1 and made[0].closed
    assert diagnostics[0]["phase"] == phase and diagnostics[0]["category"] == "transport_failure"
    assert diagnostics[0]["http_status"] == (200 if operation in {"read", "stream"} else None)
    assert "fake-openai-key" not in json.dumps(diagnostics)


def test_private_upstream_failure_sink_failure_is_closed_and_attempted_once(upstream_module, connection_factory):
    made, response = connection_factory
    response.status = 403
    calls = []
    def broken_sink(payload):
        calls.append(payload)
        raise OSError("secret capture writer value")
    sender, _, _ = _diagnostic_sender(upstream_module, sink=broken_sink)
    with pytest.raises(BrokerRejected) as caught: sender("POST", *_routed_request("codex"))
    assert len(calls) == len(made) == 1 and made[0].closed
    assert "secret" not in str(caught.value)


def test_private_upstream_success_preview_and_validation_do_not_capture(upstream_module, connection_factory):
    _, response = connection_factory
    diagnostics = []
    sender, _, _ = _diagnostic_sender(upstream_module, sink=diagnostics.append)
    sender.preview("POST", *_routed_request("codex"))
    assert sender("POST", *_routed_request("codex")).body == b'{"ok":true}'
    response.headers["content-type"] = "text/event-stream"
    response.chunks = [b'data: {"ok":true}\n\n']
    assert list(sender("POST", *_routed_request("codex")).body) == [b'data: {"ok":true}\n\n']
    with pytest.raises(BrokerRejected): sender("POST", "/wrong", *_routed_request("codex")[1:])
    assert diagnostics == []


def test_private_upstream_without_sink_preserves_immediate_non2xx_rejection(upstream_module, connection_factory):
    _, response = connection_factory
    response.status = 403; reads = []
    def read(_amount): reads.append(True); return b""
    response.read = read
    sender, _, _ = _routed_sender(upstream_module, "codex")
    with pytest.raises(BrokerRejected): sender("POST", *_routed_request("codex"))
    assert reads == []


@pytest.mark.parametrize("error_type", (OSError, SystemExit))
def test_private_upstream_writer_failure_never_escapes_safe_rejection(upstream_module, connection_factory, error_type):
    made, response = connection_factory; response.status = 403
    captures = []
    def sink(value):
        captures.append(value)
        raise error_type("PRIVATE_WRITER_CANARY")
    sender, _, _ = _diagnostic_sender(upstream_module, sink=sink)
    with pytest.raises(BrokerRejected) as caught: sender("POST", *_routed_request("codex"))
    assert len(captures) == len(made) == 1 and made[0].closed
    assert "PRIVATE_WRITER_CANARY" not in str(caught.value)


def test_private_upstream_error_body_partial_read_retains_actual_phase(upstream_module, connection_factory):
    _, response = connection_factory; response.status = 403
    chunks = iter([b'{"error":'])
    def read(_amount):
        try: return next(chunks)
        except StopIteration: raise OSError("PRIVATE_ERROR_CANARY")
    response.read = read; diagnostics = []
    sender, _, _ = _diagnostic_sender(upstream_module, sink=diagnostics.append)
    with pytest.raises(BrokerRejected): sender("POST", *_routed_request("codex"))
    assert len(diagnostics) == 1 and diagnostics[0]["phase"] == "error_body"
    assert diagnostics[0]["http_status"] == 403 and diagnostics[0]["omission"] == "body_read_failure"
    assert diagnostics[0]["structured_error"] is None


@pytest.mark.parametrize("framing,omission", [
    ("complete_length", None),
    ("truncated_length", "body_read_failure"),
    ("close_delimited", None),
    ("complete_chunked", None),
    ("missing_last_chunk", "body_read_failure"),
    ("truncated_chunk", "body_read_failure"),
])
def test_private_upstream_http_framing_completion_before_error_projection(
    upstream_module, connection_factory, monkeypatch, tmp_path, framing, omission,
):
    error_body = b'{"error":{"code":7,"message":"bounded denial"}}'
    if framing in {"complete_length", "truncated_length"}:
        length = len(error_body) + (10 if framing == "truncated_length" else 0)
        framing_headers = f"Content-Length: {length}\r\n".encode()
        wire_body = error_body
    elif framing == "close_delimited":
        framing_headers = b"Connection: close\r\n"
        wire_body = error_body
    else:
        framing_headers = b"Transfer-Encoding: chunked\r\n"
        length = len(error_body) + (10 if framing == "truncated_chunk" else 0)
        wire_body = f"{length:x}\r\n".encode() + error_body
        if framing != "truncated_chunk": wire_body += b"\r\n"
        if framing == "complete_chunked": wire_body += b"0\r\n\r\n"
    wire = b"HTTP/1.1 403 Forbidden\r\nContent-Type: application/json\r\n" + framing_headers + b"\r\n" + wire_body
    class MemorySocket:
        def makefile(self, mode):
            assert mode == "rb"
            return io.BytesIO(wire)
    response = http.client.HTTPResponse(MemorySocket())
    response.begin()
    monkeypatch.setattr(FakeConnection, "getresponse", lambda self: response)
    monkeypatch.setattr(upstream_module, "_system_tls_context", lambda: object())
    made, _ = connection_factory
    controller = create_lifecycle_controller(tmp_path / "controller")
    captured = []
    def sink(payload):
        captured.append(write_evidence(controller, "container-upstream-failure", payload))
    sender, permit, route = _diagnostic_sender(upstream_module, sink=sink)
    ledger = PermitLedger(controller.root / "locks" / "spend", controller)
    permit_id = ledger.register(permit, 1)
    path, _, body = _routed_request("codex")
    class Frames:
        def __init__(self):
            self.sent = []
            self.incoming = iter([
                {"type": "ready", "seat_id": "seat-1", "permit_id": permit_id, "epoch": 1},
                {"type": "request", "permit_id": permit_id, "epoch": 1, "request_id": "framing-error",
                 "method": "POST", "provider_path": path,
                 "headers": {"x-fanout-seat": "disposable-placeholder", "content-type": "application/json"},
                 "body_b64": __import__("base64").b64encode(body).decode()},
                {"type": "close"},
            ])
        def recv(self): return next(self.incoming)
        def send(self, frame): self.sent.append(frame)
    frames = Frames()
    HostRelay(ledger, Broker(permit, credential="fake-openai-key", send=sender)).serve(frames)
    assert frames.sent == [{"type": "ready_ack"}, {"type": "reject", "status": 403}, {"type": "closed"}]
    assert len(captured) == len(made) == 1 and made[0].closed and response.isclosed()
    diagnostic = read_evidence(controller, "container-upstream-failure", *captured[0])
    assert diagnostic["http_status"] == 403 and diagnostic["category"] == "non2xx"
    assert diagnostic["structured_error"] == (None if omission else {"code": 7, "message": "bounded denial"})
    assert diagnostic["omission"] == omission
    assert diagnostic["phase"] == ("error_body" if omission else "response_headers")
    assert diagnostic["upstream_route_digest"] == route.digest
    assert diagnostic["normalized_body_sha256"] == diagnostic["upstream_body_sha256"] == hashlib.sha256(body).hexdigest()
    record = ledger.request_records(permit_id, 1)[0]
    assert record["state"] == "uncertain" and record["response_digest"] is None
    with ledger._locked() as state:
        assert state["send_claims"] == {"framing-error": True}
    assert "fake-openai-key" not in json.dumps(diagnostic)
    if omission: assert "bounded denial" not in json.dumps(diagnostic)


@pytest.mark.parametrize("extra,omission", [(0, None), (1, "body_budget")])
def test_private_upstream_error_diagnostic_cap_is_not_response_ceiling(upstream_module, connection_factory, extra, omission):
    _, response = connection_factory; response.status = 403
    body = b'{"error":{"code":7},"ignored":"' + b"x" * 16351 + b'"}'
    assert len(body) == 16384
    response.chunks = [body + b" " * extra]
    sender0, permit, route = _routed_sender(upstream_module, "codex")
    permit = replace(permit, max_response_bytes=32768)
    diagnostics = []
    sender = upstream_module.HostUpstream(permit, route, max_response_bytes=32768, deadline_seconds=2, failure_sink=diagnostics.append)
    with pytest.raises(BrokerRejected): sender("POST", *_routed_request("codex"))
    assert len(diagnostics) == 1 and diagnostics[0]["omission"] == omission
    assert diagnostics[0]["structured_error"] == ({"code": 7} if extra == 0 else None)


def test_private_upstream_error_projection_omits_overlong_fields_not_prefixes(upstream_module, connection_factory):
    _, response = connection_factory; response.status = 403
    response.chunks = [json.dumps({"error": {"code": 7, "type": "x" * 129, "message": "x" * 4097}, "ignored": "PRIVATE_CANARY"}).encode()]
    _, permit, route = _routed_sender(upstream_module, "codex")
    permit = replace(permit, max_response_bytes=8192)
    diagnostics = []
    sender = upstream_module.HostUpstream(permit, route, max_response_bytes=8192, deadline_seconds=2, failure_sink=diagnostics.append)
    with pytest.raises(BrokerRejected): sender("POST", *_routed_request("codex"))
    assert diagnostics[0]["structured_error"] == {"code": 7}
    assert "PRIVATE_CANARY" not in json.dumps(diagnostics)


def test_private_upstream_deadline_capture_keeps_transport_lease(upstream_module, connection_factory, monkeypatch):
    made, _ = connection_factory
    def stalled(connection, *_args, **_kwargs):
        assert connection.closed_event.wait(0.5)
        raise TimeoutError("PRIVATE_TRANSPORT_CANARY")
    monkeypatch.setattr(FakeConnection, "request", stalled)
    _, permit, route = _routed_sender(upstream_module, "codex")
    diagnostics = []
    sender = upstream_module.HostUpstream(permit, route, max_response_bytes=128, deadline_seconds=0.1, failure_sink=diagnostics.append)
    with pytest.raises(BrokerRejected): sender("POST", *_routed_request("codex"))
    assert len(diagnostics) == len(made) == 1 and made[0].closed
    assert diagnostics[0]["phase"] == "request_write" and diagnostics[0]["category"] == "deadline_expired"
