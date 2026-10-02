"""The diagnostic broker is a finite, credential-blind provider boundary."""
from __future__ import annotations

import json
import http.client
import socket
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.lib.fanout.broker import (  # noqa: E402
    Broker, BrokerListener, BrokerPermit, BrokerRejected, BrokerResponse,
)


def _permit(provider: str = "claude", *, budget: int = 1) -> BrokerPermit:
    return BrokerPermit(
        provider=provider, run_id="run-1", task_id="task-1", target_id="address",
        seat_id="seat-1", route="initial", model="test-model",
        placeholder_key="disposable-placeholder", max_requests=budget,
        max_request_bytes=1024, max_response_bytes=1024,
        max_output_tokens=32,
    )


def _routed_permit() -> BrokerPermit:
    return replace(_permit(), upstream_route_digest="a" * 64,
                   upstream_origin="https://eu-aiplatform.googleapis.com",
                   upstream_model="test-model", upstream_path="/v1/pinned-route")


def _request(provider: str = "claude") -> tuple[str, dict[str, str], bytes]:
    if provider == "claude":
        return (
            "/v1/messages", {"x-api-key": "disposable-placeholder",
                             "content-type": "application/json"},
            b'{"model":"test-model","max_tokens":16,"messages":[]}',
        )
    if provider == "codex":
        return (
            "/v1/responses", {"x-fanout-seat": "disposable-placeholder",
                              "content-type": "application/json"},
            b'{"model":"test-model","max_output_tokens":16,"input":[]}',
        )
    return (
        "/v1beta/models/test-model:streamGenerateContent?alt=sse",
        {"x-goog-api-key": "disposable-placeholder",
         "content-type": "application/json"},
        b'{"generationConfig":{"maxOutputTokens":16},"contents":[]}',
    )


@pytest.mark.parametrize("provider", ("claude", "codex", "agy"))
def test_broker_passes_only_assigned_route_with_controller_held_credential(provider):
    sent = []

    def upstream(method, path, headers, body):
        sent.append((method, path, headers, body))
        return BrokerResponse(200, {"content-type": "application/json"}, b'{"ok":true}')

    broker = Broker(_permit(provider), credential="controller-secret", send=upstream)
    path, headers, body = _request(provider)
    response = broker.dispatch("POST", path, headers, body)

    assert response.status == 200
    assert response.body == b'{"ok":true}'
    assert len(sent) == 1
    assert sent[0][0:2] == ("POST", path)
    assert sent[0][2][{"claude": "x-api-key", "codex": "authorization",
                        "agy": "x-goog-api-key"}[provider]] in (
        "controller-secret", "Bearer controller-secret",
    )
    assert b"controller-secret" not in response.body
    assert "controller-secret" not in json.dumps(response.headers)
    assert "disposable-placeholder" not in json.dumps(sent[0][2])


def test_route_bound_broker_refuses_sender_without_pure_preview():
    sent = []
    broker = Broker(_routed_permit(), credential="controller-secret",
                    send=lambda *args: sent.append(args))
    path, headers, body = _request()
    with pytest.raises(BrokerRejected, match="preview"):
        broker.preview_upstream("POST", path, headers, body)
    assert sent == []


def test_route_bound_direct_dispatch_requires_durable_reservation():
    sent = []
    broker = Broker(_routed_permit(), credential="controller-secret",
                    send=lambda *args: (sent.append(args), BrokerResponse(
                        200, {"content-type": "application/json"}, b'{}'))[1])
    path, headers, body = _request()
    with pytest.raises(BrokerRejected, match="reservation"):
        broker.dispatch("POST", path, headers, body)
    assert sent == []


def test_route_bound_legacy_listener_cannot_send_without_reservation():
    sent = []
    broker = Broker(_routed_permit(), credential="controller-secret",
                    send=lambda *args: (sent.append(args), BrokerResponse(
                        200, {"content-type": "application/json"}, b'{}'))[1])
    path, headers, body = _request()
    with BrokerListener(broker) as listener:
        connection = http.client.HTTPConnection(listener.host, listener.port, timeout=3)
        connection.request("POST", path, body=body, headers=headers)
        response = connection.getresponse()
        assert response.status == 403
        response.read()
        connection.close()
    assert sent == []


def test_claude_broker_allows_only_bounded_beta_query_on_messages():
    sent = []
    broker = Broker(_permit(), credential="controller-secret",
                    send=lambda *args: (sent.append(args), BrokerResponse(
                        200, {"content-type": "application/json"}, b'{"ok":true}'))[1])
    _, headers, body = _request()
    broker.dispatch("POST", "/v1/messages?beta=test-beta", headers, body)
    assert len(sent) == 1
    assert sent[0][1] == "/v1/messages"


def test_claude_broker_clamps_large_integer_generation_before_upstream():
    sent = []
    permit = replace(_permit(), clamp_output_tokens=True)
    broker = Broker(permit, credential="controller-secret",
                    send=lambda *args: (sent.append(args), BrokerResponse(
                        200, {"content-type": "application/json"}, b'{"ok":true}'))[1])
    path, headers, _ = _request()
    broker.dispatch("POST", path, headers,
                    b'{"model":"test-model","max_tokens":128000,"messages":[]}')
    assert json.loads(sent[0][3])["max_tokens"] == 32
    assert broker.effective_output_tokens == (32,)


def test_agy_broker_refuses_auxiliary_and_clamps_main_generation():
    sent = []
    permit = replace(_permit("agy", budget=1), model="gemini-3.8-flash",
                     clamp_output_tokens=True)
    broker = Broker(permit, credential="controller-secret",
                    send=lambda *args: (sent.append(args), BrokerResponse(
                        200, {"content-type": "application/json"}, b'{"ok":true}'))[1])
    _, headers, _ = _request("agy")
    path = "/v1beta/models/gemini-3.8-flash:streamGenerateContent?alt=sse"
    with pytest.raises(BrokerRejected, match="auxiliary model"):
        broker.dispatch("POST", "/v1beta/models/gemini-3.1-flash-lite-preview:streamGenerateContent?alt=sse",
                        headers, b'{"contents":[]}')
    assert broker.refused_agy_auxiliary_requests == 1
    broker.dispatch("POST", path, headers,
                    b'{"generationConfig":{"maxOutputTokens":65536},"contents":[]}')
    assert len(sent) == 1
    assert sent[0][1] == path
    assert json.loads(sent[0][3])["generationConfig"]["maxOutputTokens"] == 32
    assert broker.effective_output_tokens == (32,)


@pytest.mark.parametrize("body", (
    b'{"generationConfig":{"maxOutputTokens":65536},"contents":[]}',
    b'{"model":"gemini-3.8-flash","generationConfig":{"maxOutputTokens":65536},"contents":[]}',
))
def test_agy_cli_profile_forwards_only_measured_wire_model_with_clamped_output(body):
    sent = []
    permit = replace(_permit("agy"), model="gemini-3.8-flash-high",
                     clamp_output_tokens=True)
    broker = Broker(permit, credential="controller-secret",
                    send=lambda *args: (sent.append(args), BrokerResponse(
                        200, {"content-type": "application/json"}, b'{"ok":true}'))[1])
    _, headers, _ = _request("agy")
    path = "/v1beta/models/gemini-3.8-flash:streamGenerateContent?alt=sse"

    response = broker.dispatch("POST", path, headers, body)

    assert response.body == b'{"ok":true}'
    assert len(sent) == 1
    assert sent[0][1] == path
    assert sent[0][2]["x-goog-api-key"] == "controller-secret"
    assert "disposable-placeholder" not in json.dumps(sent[0][2])
    assert json.loads(sent[0][3])["generationConfig"]["maxOutputTokens"] == 32
    assert broker.effective_output_tokens == (32,)


@pytest.mark.parametrize("path,body,reason", (
    ("/v1beta/models/gemini-3.8-flash-high:streamGenerateContent?alt=sse",
     b'{"generationConfig":{"maxOutputTokens":16},"contents":[]}', "path"),
    ("/v1beta/models/gemini-3.8-pro:streamGenerateContent?alt=sse",
     b'{"generationConfig":{"maxOutputTokens":16},"contents":[]}', "path"),
    ("/v1beta/models/gemini-3.1-flash-lite-preview:streamGenerateContent?alt=sse",
     b'{"generationConfig":{"maxOutputTokens":16},"contents":[]}', "auxiliary"),
    ("/v1beta/models/gemini-3.8-flash:streamGenerateContent?alt=sse",
     b'{"model":"gemini-3.8-flash-high","generationConfig":{"maxOutputTokens":16},"contents":[]}',
     "model"),
))
def test_agy_cli_profile_rejects_alias_and_other_wire_models_without_spend(path, body, reason):
    sent = []
    permit = replace(_permit("agy"), model="gemini-3.8-flash-high",
                     clamp_output_tokens=True)
    broker = Broker(permit, credential="controller-secret", send=lambda *args: sent.append(args))
    _, headers, _ = _request("agy")

    with pytest.raises(BrokerRejected, match=reason):
        broker.dispatch("POST", path, headers, body)

    assert sent == []
    assert broker.effective_output_tokens == ()
    if reason == "auxiliary":
        assert broker.refused_agy_auxiliary_requests == 1


@pytest.mark.parametrize("path", (
    "/v1beta/models/test-model:generateContent",
    "/v1beta/models/test-model:streamGenerateContent",
    "/v1beta/models/test-model:streamGenerateContent?alt=json",
    "/v1beta/models/test-model:streamGenerateContent?alt=sse&key=reflected",
))
def test_agy_broker_rejects_non_sse_or_expanded_route(path):
    sent = []
    broker = Broker(_permit("agy"), credential="controller-secret",
                    send=lambda *args: sent.append(args))
    _, headers, body = _request("agy")
    with pytest.raises(BrokerRejected):
        broker.dispatch("POST", path, headers, body)
    assert sent == []


@pytest.mark.parametrize("invalid", ('"128000"', "-1", "0", "null", "true"))
def test_claude_broker_clamp_rejects_nonpositive_or_noninteger_generation(invalid):
    sent = []
    broker = Broker(replace(_permit(), clamp_output_tokens=True),
                    credential="controller-secret", send=lambda *args: sent.append(args))
    path, headers, _ = _request()
    with pytest.raises(BrokerRejected):
        broker.dispatch("POST", path, headers,
                        ('{"model":"test-model","max_tokens":' + invalid + '}').encode())
    assert sent == []


@pytest.mark.parametrize("invalid", ('"65536"', "-1", "0", "null", "true"))
def test_agy_broker_clamp_rejects_nonpositive_or_noninteger_generation(invalid):
    sent = []
    broker = Broker(replace(_permit("agy"), clamp_output_tokens=True),
                    credential="controller-secret", send=lambda *args: sent.append(args))
    path, headers, _ = _request("agy")
    with pytest.raises(BrokerRejected):
        broker.dispatch("POST", path, headers,
                        ('{"generationConfig":{"maxOutputTokens":' + invalid + '}}').encode())
    assert sent == []


@pytest.mark.parametrize("path", (
    "/v1/messages?beta=one&beta=two",
    "/v1/messages?beta=one&next=x",
    "/v1/messages?beta=one%26next%3Dhttps",
    "/v1/messages?beta=one%2Ftwo",
    "/v1/messages?beta=disposable-placeholder",
    "/v1/messages?beta=controller-secret",
    "/v1/messages?beta=" + "x" * 129,
    "/v1/messages?next=https://example.invalid",
))
def test_claude_broker_rejects_unbounded_or_encoded_query(path):
    sent = []
    broker = Broker(_permit(), credential="controller-secret",
                    send=lambda *args: sent.append(args))
    _, headers, body = _request()
    with pytest.raises(BrokerRejected):
        broker.dispatch("POST", path, headers, body)
    assert sent == []


@pytest.mark.parametrize("method,path", (
    ("CONNECT", "api.example.invalid:443"),
    ("GET", "/v1/messages"),
    ("POST", "/v1/models"),
    ("POST", "/v1/messages?target=https://example.invalid"),
    ("POST", "https://example.invalid/v1/messages"),
    ("POST", "/v1/../admin"),
))
def test_broker_rejects_other_methods_and_paths_before_upstream(method, path):
    sent = []
    broker = Broker(_permit(), credential="controller-secret",
                    send=lambda *args: sent.append(args))
    _, headers, body = _request()
    with pytest.raises(BrokerRejected):
        broker.dispatch(method, path, headers, body)
    assert sent == []


@pytest.mark.parametrize("headers,body", (
    ({"x-api-key": "wrong", "content-type": "application/json"},
     b'{"model":"test-model","max_tokens":16}'),
    ({"x-api-key": "disposable-placeholder", "content-type": "application/json"},
     b'{"model":"wrong-model","max_tokens":16}'),
    ({"x-api-key": "disposable-placeholder", "content-type": "application/json"},
     b'{"model":"test-model","max_tokens":33}'),
    ({"x-api-key": "disposable-placeholder", "content-type": "application/json"},
     b'{"model":"test-model"}'),
))
def test_broker_rejects_wrong_auth_model_or_unbounded_generation(headers, body):
    sent = []
    broker = Broker(_permit(), credential="controller-secret",
                    send=lambda *args: sent.append(args))
    with pytest.raises(BrokerRejected):
        broker.dispatch("POST", "/v1/messages", headers, body)
    assert sent == []


def test_codex_broker_rejects_seat_supplied_authorization_header():
    sent = []
    broker = Broker(_permit("codex"), credential="controller-secret",
                    send=lambda *args: sent.append(args))
    path, headers, body = _request("codex")
    headers["authorization"] = "Bearer seat-secret"
    with pytest.raises(BrokerRejected):
        broker.dispatch("POST", path, headers, body)
    assert sent == []


def test_broker_budget_is_reserved_before_upstream_and_never_retried():
    sent = []

    def upstream(*args):
        sent.append(args)
        raise OSError("uncertain upstream response")

    broker = Broker(_permit(), credential="controller-secret", send=upstream)
    path, headers, body = _request()
    with pytest.raises(BrokerRejected, match="upstream"):
        broker.dispatch("POST", path, headers, body)
    with pytest.raises(BrokerRejected, match="budget"):
        broker.dispatch("POST", path, headers, body)
    assert len(sent) == 1


@pytest.mark.parametrize("response", (
    BrokerResponse(302, {"location": "https://example.invalid"}, b""),
    BrokerResponse(200, {"content-type": "application/json"}, b"controller-secret"),
    BrokerResponse(200, {"x-api-key": "controller-secret"}, b"{}"),
    BrokerResponse(200, {"content-type": "application/json"},
                   b'{"message":"controller\\u002dsecret"}'),
    BrokerResponse(200, {"content-type": "text/event-stream"},
                   b'event: message\ndata: {"message":"controller\\u002dsecret"}\n\n'),
    BrokerResponse(200, {"content-type": "application/json"},
                   b'{"message":"controller\\u002dsecret","message":"safe"}'),
    BrokerResponse(200, {"content-type": "text/event-stream"},
                   b'data: {"message":"controller\\u002dsecret","message":"safe"}\n\n'),
    BrokerResponse(200, {"content-type": "application/json"},
                   json.dumps({"inner": '{"token":"controller\\u002dsecret"}'}).encode()),
    BrokerResponse(200, {"content-type": "application/json"},
                   json.dumps({"inner": '"controller\\u002dsecret"'}).encode()),
    BrokerResponse(200, {"content-encoding": "gzip"}, b"opaque-compressed-body"),
    BrokerResponse(200, {"content-type": "application/json"}, b"x" * 1025),
))
def test_broker_does_not_reflect_credential_redirect_or_unbounded_response(response):
    broker = Broker(_permit(), credential="controller-secret",
                    send=lambda *_args: response)
    path, headers, body = _request()
    with pytest.raises(BrokerRejected) as error:
        broker.dispatch("POST", path, headers, body)
    assert "controller-secret" not in str(error.value)


@pytest.mark.parametrize("content_type,raw,canonical", (
    ("application/json", b'{"ok" : true }', b'{"ok":true}'),
    ("text/event-stream", b'event: message\ndata: {"ok" : true }\n\n',
     b'event: message\ndata: {"ok":true}\n\n'),
))
def test_broker_returns_only_canonicalized_structured_response(content_type, raw, canonical):
    broker = Broker(_permit(), credential="controller-secret",
                    send=lambda *_args: BrokerResponse(200, {"content-type": content_type}, raw))
    path, headers, body = _request()
    assert broker.dispatch("POST", path, headers, body).body == canonical


@pytest.mark.parametrize("status,headers", (
    (302, {"content-type": "text/event-stream", "location": "https://example.invalid"}),
    (200, {"content-type": "text/event-stream", "content-encoding": "gzip"}),
    (200, {"content-type": "text/event-stream", "x-api-key": "controller-secret"}),
    (200, {"content-type": "text/event-stream", "x-trace": "controller-secret"}),
))
def test_broker_refuses_unsafe_stream_metadata_before_delivery(status, headers):
    broker = Broker(_permit(), credential="controller-secret",
                    send=lambda *_args: BrokerResponse(
                        status, headers, (b'data: {"ok":true}\n\n',),
                    ))
    path, request_headers, body = _request()
    with pytest.raises(BrokerRejected):
        broker.dispatch("POST", path, request_headers, body)


def test_broker_rejects_unknown_provider_and_invalid_permit():
    with pytest.raises(BrokerRejected):
        _permit("maka")
    with pytest.raises(BrokerRejected, match="max_requests"):
        BrokerPermit(
            provider="claude", run_id="run-1", task_id="task-1", target_id="address",
            seat_id="seat-1", route="resume", session_id="exact-session",
            model="test-model",
            placeholder_key="disposable-placeholder", max_requests=0,
            max_request_bytes=1024, max_response_bytes=1024,
            max_output_tokens=32,
        )


def test_resume_broker_permit_requires_exact_session_identity():
    with pytest.raises(BrokerRejected, match="resume needs an exact session"):
        BrokerPermit(
            provider="claude", run_id="run-1", task_id="task-1", target_id="address",
            seat_id="seat-1", route="resume", model="test-model",
            placeholder_key="disposable-placeholder", max_requests=1,
            max_request_bytes=1024, max_response_bytes=1024,
            max_output_tokens=32,
        )

@pytest.mark.parametrize("deadline", (0, 601, float("inf"), float("nan"), True))
def test_broker_permit_requires_finite_response_deadline(deadline):
    with pytest.raises(BrokerRejected):
        replace(_permit(), response_deadline=deadline)


def test_broker_rejects_placeholder_equal_to_real_credential():
    with pytest.raises(BrokerRejected):
        Broker(_permit(), credential="disposable-placeholder",
               send=lambda *_args: BrokerResponse(200, {}, b""))


def test_listener_binds_only_loopback_and_does_not_expose_controller_secret():
    sent = []

    def upstream(method, path, headers, body):
        sent.append((method, path, headers, body))
        return BrokerResponse(200, {"content-type": "application/json"}, b'{"ok":true}')

    broker = Broker(_permit(), credential="controller-secret", send=upstream)
    with BrokerListener(broker) as listener:
        assert listener.host == "127.0.0.1"
        assert 0 < listener.port < 65536
        path, headers, body = _request()
        connection = http.client.HTTPConnection(listener.host, listener.port, timeout=3)
        connection.request("POST", path, body=body, headers=headers)
        response = connection.getresponse()
        assert (response.status, response.read()) == (200, b'{"ok":true}')
        connection.close()

        second = http.client.HTTPConnection(listener.host, listener.port, timeout=3)
        second.request("POST", path, body=body, headers=headers)
        response = second.getresponse()
        assert response.status == 403
        assert b"controller-secret" not in response.read()
        second.close()

        invalid = http.client.HTTPConnection(listener.host, listener.port, timeout=3)
        invalid.request("CONNECT", "example.invalid:443")
        response = invalid.getresponse()
        assert response.status == 403
        invalid.close()
    assert len(sent) == 1


def test_listener_delivers_valid_sse_frame_before_upstream_completes():
    release_last_frame = threading.Event()
    upstream_completed = threading.Event()

    def frames():
        yield b'event: message\ndata: {"ok"'
        yield b' : true}\n\n'
        release_last_frame.wait(timeout=3)
        upstream_completed.set()
        yield b'data: [DONE]\n\n'

    broker = Broker(_permit(), credential="controller-secret",
                    send=lambda *_args: BrokerResponse(
                        200, {"content-type": "text/event-stream"}, frames(),
                    ))
    with BrokerListener(broker) as listener:
        connection = http.client.HTTPConnection(listener.host, listener.port, timeout=2)
        try:
            path, headers, body = _request()
            connection.request("POST", path, body=body, headers=headers)
            response = connection.getresponse()
            assert response.status == 200
            assert response.read(len(b'event: message\ndata: {"ok":true}\n\n')) == (
                b'event: message\ndata: {"ok":true}\n\n'
            )
            assert not upstream_completed.is_set()
            release_last_frame.set()
            assert response.read() == b'data: [DONE]\n\n'
        finally:
            release_last_frame.set()
            connection.close()


def test_listener_forwards_safe_heartbeat_while_upstream_waits_for_data():
    release_data = threading.Event()

    def frames():
        yield b': keep-alive\n\n'
        release_data.wait(timeout=3)
        yield b'data: {"ok":true}\n\n'

    broker = Broker(_permit(), credential="controller-secret",
                    send=lambda *_args: BrokerResponse(
                        200, {"content-type": "text/event-stream"}, frames(),
                    ))
    with BrokerListener(broker) as listener:
        connection = http.client.HTTPConnection(listener.host, listener.port, timeout=2)
        try:
            path, headers, body = _request()
            connection.request("POST", path, body=body, headers=headers)
            response = connection.getresponse()
            assert response.status == 200
            assert response.read(3) == b':\n\n'
            release_data.set()
            assert response.read() == b'data: {"ok":true}\n\n'
        finally:
            release_data.set()
            connection.close()


def test_listener_never_delivers_a_later_secret_bearing_sse_frame():
    first = b'data: {"ok":true}\n\n'
    release_bad_frame = threading.Event()

    def frames():
        yield first
        release_bad_frame.wait(timeout=3)
        yield b'data: {"message":"controller\\u'
        yield b'002dsecret"}\n\n'

    broker = Broker(_permit(), credential="controller-secret",
                    send=lambda *_args: BrokerResponse(
                        200, {"content-type": "text/event-stream"}, frames(),
                    ))
    with BrokerListener(broker) as listener:
        connection = http.client.HTTPConnection(listener.host, listener.port, timeout=2)
        try:
            path, headers, body = _request()
            connection.request("POST", path, body=body, headers=headers)
            response = connection.getresponse()
            assert response.status == 200
            assert response.read(len(first)) == first
            release_bad_frame.set()
            with pytest.raises(http.client.IncompleteRead) as error:
                response.read()
            assert b"controller-secret" not in error.value.partial
            assert b"controller\\u" not in error.value.partial
        finally:
            release_bad_frame.set()
            connection.close()


def test_listener_rejects_secret_assembled_from_successive_text_deltas():
    first = (b'event: content_block_delta\ndata: '
             b'{"type":"content_block_delta","index":0,'
             b'"delta":{"type":"text_delta","text":"controller"}}\n\n')
    second = (b'event: content_block_delta\ndata: '
              b'{"type":"content_block_delta","index":0,'
              b'"delta":{"type":"text_delta","text":"-secret"}}\n\n')
    broker = Broker(_permit(), credential="controller-secret",
                    send=lambda *_args: BrokerResponse(
                        200, {"content-type": "text/event-stream"}, (first, second),
                    ))
    with BrokerListener(broker) as listener:
        connection = http.client.HTTPConnection(listener.host, listener.port, timeout=2)
        try:
            path, headers, body = _request()
            connection.request("POST", path, body=body, headers=headers)
            response = connection.getresponse()
            assert response.status == 200
            assert response.read(len(first)) == first
            with pytest.raises(http.client.IncompleteRead) as error:
                response.read()
            assert b"-secret" not in error.value.partial
        finally:
            connection.close()


def test_listener_aborts_stream_when_later_frame_exceeds_response_budget():
    first = b'data: {"ok":true}\n\n'

    def frames():
        yield first
        yield b'data: {"message":"' + b"x" * 1000 + b'"}\n\n'

    broker = Broker(_permit(), credential="controller-secret",
                    send=lambda *_args: BrokerResponse(
                        200, {"content-type": "text/event-stream"}, frames(),
                    ))
    with BrokerListener(broker) as listener:
        connection = http.client.HTTPConnection(listener.host, listener.port, timeout=2)
        try:
            path, headers, body = _request()
            connection.request("POST", path, body=body, headers=headers)
            response = connection.getresponse()
            assert response.status == 200
            assert response.read(len(first)) == first
            with pytest.raises(http.client.IncompleteRead) as error:
                response.read()
            assert b"x" * 1000 not in error.value.partial
        finally:
            connection.close()


def test_listener_aborts_stalled_sse_response_at_finite_deadline():
    first = b'data: {"ok":true}\n\n'
    release_stall = threading.Event()

    def frames():
        yield first
        release_stall.wait(timeout=3)
        yield b'data: [DONE]\n\n'

    permit = replace(_permit(), response_deadline=0.25)
    broker = Broker(permit, credential="controller-secret",
                    send=lambda *_args: BrokerResponse(
                        200, {"content-type": "text/event-stream"}, frames(),
                    ))
    with BrokerListener(broker) as listener:
        connection = http.client.HTTPConnection(listener.host, listener.port, timeout=2)
        try:
            path, headers, body = _request()
            connection.request("POST", path, body=body, headers=headers)
            response = connection.getresponse()
            assert response.status == 200
            assert response.read(len(first)) == first
            started = time.monotonic()
            with pytest.raises(http.client.IncompleteRead):
                response.read()
            assert time.monotonic() - started < 1.0
        finally:
            release_stall.set()
            connection.close()


def test_listener_bounds_wait_for_upstream_response_headers():
    release_upstream = threading.Event()

    def upstream(*_args):
        release_upstream.wait(timeout=3)
        return BrokerResponse(200, {"content-type": "application/json"}, b'{"ok":true}')

    broker = Broker(replace(_permit(), response_deadline=0.25),
                    credential="controller-secret", send=upstream)
    with BrokerListener(broker) as listener:
        connection = http.client.HTTPConnection(listener.host, listener.port, timeout=2)
        try:
            path, headers, body = _request()
            started = time.monotonic()
            connection.request("POST", path, body=body, headers=headers)
            response = connection.getresponse()
            assert response.status == 403
            assert time.monotonic() - started < 1.0
            assert b"controller-secret" not in response.read()
        finally:
            release_upstream.set()
            connection.close()


def test_listener_rejects_oversized_and_chunked_request_before_upstream():
    sent = []
    broker = Broker(_permit(), credential="controller-secret",
                    send=lambda *args: sent.append(args))
    with BrokerListener(broker) as listener:
        oversized = http.client.HTTPConnection(listener.host, listener.port, timeout=3)
        oversized.request("POST", "/v1/messages", body=b"x" * 1025,
                          headers={"content-type": "application/json",
                                   "x-api-key": "disposable-placeholder"})
        response = oversized.getresponse()
        assert response.status == 403
        response.read()
        oversized.close()

        chunked = http.client.HTTPConnection(listener.host, listener.port, timeout=3)
        chunked.request("POST", "/v1/messages", body=[b"{}"], encode_chunked=True,
                        headers={"content-type": "application/json",
                                 "x-api-key": "disposable-placeholder"})
        response = chunked.getresponse()
        assert response.status == 403
        response.read()
        chunked.close()
    assert sent == []


def test_partial_body_cannot_stall_the_only_listener_worker():
    broker = Broker(_permit(), credential="controller-secret",
                    send=lambda *_args: BrokerResponse(
                        200, {"content-type": "application/json"}, b'{"ok":true}',
                    ))
    with BrokerListener(broker) as listener:
        partial = socket.create_connection((listener.host, listener.port), timeout=3)
        partial.sendall((
            f"POST /v1/messages HTTP/1.1\r\nHost: {listener.host}:{listener.port}\r\n"
            "Content-Type: application/json\r\n"
            "X-Api-Key: disposable-placeholder\r\n"
            "Content-Length: 50\r\n\r\n{"
        ).encode())
        received = []

        def second_request() -> None:
            connection = http.client.HTTPConnection(listener.host, listener.port, timeout=3)
            try:
                path, headers, body = _request()
                connection.request("POST", path, body=body, headers=headers)
                response = connection.getresponse()
                received.append((response.status, response.read()))
            finally:
                connection.close()

        thread = threading.Thread(target=second_request, daemon=True)
        thread.start()
        thread.join(timeout=2)
        completed_without_closing_partial = not thread.is_alive()
        partial.close()
        thread.join(timeout=5)
        assert completed_without_closing_partial
        assert received == [(200, b'{"ok":true}')]


def test_trickled_bodies_expire_before_exhausting_all_listener_workers():
    broker = Broker(_permit(), credential="controller-secret",
                    send=lambda *_args: BrokerResponse(
                        200, {"content-type": "application/json"}, b'{"ok":true}',
                    ))
    with BrokerListener(broker, request_deadline=0.35) as listener:
        stalled = []
        for _ in range(4):
            connection = socket.create_connection((listener.host, listener.port), timeout=2)
            connection.sendall((
                f"POST /v1/messages HTTP/1.1\r\nHost: {listener.host}:{listener.port}\r\n"
                "Content-Type: application/json\r\n"
                "X-Api-Key: disposable-placeholder\r\n"
                "Content-Length: 1000\r\n\r\n{"
            ).encode())
            stalled.append(connection)

        def trickle() -> None:
            until = time.monotonic() + 1.2
            while time.monotonic() < until:
                for connection in stalled:
                    try:
                        connection.sendall(b" ")
                    except OSError:
                        pass
                time.sleep(0.05)

        drip = threading.Thread(target=trickle, daemon=True)
        drip.start()
        time.sleep(0.6)
        fresh = http.client.HTTPConnection(listener.host, listener.port, timeout=2)
        try:
            path, headers, body = _request()
            fresh.request("POST", path, body=body, headers=headers)
            response = fresh.getresponse()
            assert (response.status, response.read()) == (200, b'{"ok":true}')
        finally:
            fresh.close()
            for connection in stalled:
                connection.close()
            drip.join(timeout=2)
