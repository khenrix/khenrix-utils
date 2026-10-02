"""Durable spend and bounded relay behavior for a container seat."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
from dataclasses import asdict
from dataclasses import replace
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shared.lib.fanout.broker import (  # noqa: E402
    Broker, BrokerPermit, BrokerRejected, BrokerResponse,
)
from shared.lib.fanout.container_routes import (  # noqa: E402
    PreparedOutbound, RouteAdmission, RouteProfile,
)
from shared.lib.fanout.controller import (  # noqa: E402
    create_lifecycle_controller, resume_lifecycle_controller,
)
from shared.lib.fanout.container_broker import (  # noqa: E402
    FrameStream, HostRelay, PermitLedger, RelayRequest, dispatch_once as _dispatch_once,
    parse_request_frame, stream_and_settle,
)
from shared.lib.fanout import container_broker  # noqa: E402


def _permit(provider="claude", *, budget=1, route="initial", session_id=None):
    return BrokerPermit(
        provider=provider, run_id="run-1", task_id="task-1", target_id="target-1",
        seat_id="seat-1", route=route, model="test-model",
        placeholder_key="disposable-placeholder", max_requests=budget,
        max_request_bytes=1024, max_response_bytes=1024, max_output_tokens=32,
        session_id=session_id,
    )


def _routed_permit():
    return replace(_permit(), upstream_route_digest="a" * 64,
                   upstream_origin="https://eu-aiplatform.googleapis.com",
                   upstream_model="test-model", upstream_path="/v1/route-a")


def _body(provider="claude", limit=16):
    if provider == "agy":
        return json.dumps({"generationConfig": {"maxOutputTokens": limit},
                           "contents": []}).encode()
    key = "max_tokens" if provider == "claude" else "max_output_tokens"
    return json.dumps({"model": "test-model", key: limit, "messages": []}).encode()


def _path(provider="claude"):
    return {"claude": "/v1/messages", "codex": "/v1/responses",
            "agy": "/v1beta/models/test-model:streamGenerateContent?alt=sse"}[provider]


def _headers(provider="claude"):
    name = {"claude": "x-api-key", "codex": "x-fanout-seat",
            "agy": "x-goog-api-key"}[provider]
    return {name: "disposable-placeholder", "content-type": "application/json"}


def _request(permit_id, provider="claude", *, request_id="request-a", epoch=1,
             path=None, limit=16):
    return RelayRequest(permit_id, epoch, request_id, "POST", path or _path(provider),
                        _headers(provider), _body(provider, limit))


def _ledger(tmp_path, permit=None, *, epoch=1):
    controller = create_lifecycle_controller(tmp_path / "controller")
    ledger = PermitLedger(tmp_path / "spend", controller)
    permit = permit or _permit()
    return controller, ledger, ledger.register(permit, epoch)


def _broker(permit, *, response=None, sends=None):
    sends = sends if sends is not None else []
    response = response or BrokerResponse(200, {"content-type": "application/json"}, b'{"ok":true}')

    def send(*args):
        sends.append(args)
        return response

    return Broker(permit, credential="controller-secret", send=send)


def _identity(provider="claude", *, path=None, body=None, limit=16, model="test-model"):
    identity_type = getattr(container_broker, "OutboundRequestIdentity", None)
    assert identity_type is not None, "outbound identity API is missing"
    return identity_type("POST", path or _path(provider),
                         hashlib.sha256(body if body is not None else _body(provider, limit)).hexdigest(),
                         limit, model)


def _routed_identity():
    return replace(_identity(), upstream_route_digest="a" * 64,
                   upstream_origin="https://eu-aiplatform.googleapis.com",
                   upstream_model="test-model", upstream_path="/v1/route-a",
                   upstream_body_sha256="b" * 64)


def dispatch_once(request, ledger, broker):
    ledger.record_attempt(request.permit_id, request.epoch)
    return _dispatch_once(request, ledger, broker)


def test_after_send_crash_is_uncertain_and_never_replayed(tmp_path):
    controller, ledger, permit_id = _ledger(tmp_path)
    ledger.record_attempt(permit_id, 1)
    ledger.reserve(permit_id, 1, "request-a", _identity())
    sends = ["request-a"]
    reopened = PermitLedger(tmp_path / "spend", resume_lifecycle_controller(
        controller.root, controller.capability))
    reopened.fence_epoch(permit_id, 1)
    with pytest.raises(BrokerRejected, match="uncertain|fenced"):
        reopened.reserve(permit_id, 1, "request-a", _identity())
    with pytest.raises(BrokerRejected, match="fenced"):
        reopened.reserve(permit_id, 1, "request-b", _identity())
    assert sends == ["request-a"]


def test_pre_send_crash_conservatively_consumes_budget(tmp_path):
    controller, ledger, permit_id = _ledger(tmp_path)
    ledger.record_attempt(permit_id, 1)
    ledger.reserve(permit_id, 1, "request-a", _identity())
    reopened = PermitLedger(tmp_path / "spend", controller)
    with pytest.raises(BrokerRejected, match="uncertain"):
        reopened.reserve(permit_id, 1, "request-a", _identity())
    restarted_sends = []
    with pytest.raises(BrokerRejected, match="budget"):
        dispatch_once(_request(permit_id, request_id="request-b"), reopened,
                      _broker(_permit(), sends=restarted_sends))
    assert restarted_sends == []


def test_fenced_permit_cannot_be_registered_again_at_same_epoch(tmp_path):
    permit = _permit()
    controller, ledger, permit_id = _ledger(tmp_path, permit)
    ledger.record_attempt(permit_id, 1)
    ledger.reserve(permit_id, 1, "request-a", _identity())
    ledger.fence_epoch(permit_id, 1)
    reopened = PermitLedger(tmp_path / "spend", controller)
    with pytest.raises(BrokerRejected, match="registered|epoch|fenced"):
        reopened.register(permit, 1)
    successor = reopened.register(permit, 2)
    assert successor != permit_id
    reopened.record_attempt(successor, 2)
    reopened.reserve(successor, 2, "request-b", _identity())


def test_same_seat_cannot_reset_budget_by_changing_permit_within_epoch(tmp_path):
    permit = _permit()
    _, ledger, permit_id = _ledger(tmp_path, permit)
    with pytest.raises(BrokerRejected, match="registered|epoch|seat"):
        ledger.register(replace(permit, max_requests=2), 1)
    with pytest.raises(BrokerRejected, match="fence|active"):
        ledger.register(permit, 2)
    ledger.fence_epoch(permit_id, 1)
    assert ledger.register(replace(permit, route="resume", session_id="session-1"), 2)


def test_settlement_and_epoch_fence_survive_broker_restart(tmp_path):
    permit = _permit(budget=2)
    controller, ledger, permit_id = _ledger(tmp_path, permit)
    sends = []
    response = dispatch_once(_request(permit_id), ledger, _broker(permit, sends=sends))
    assert b"".join(response.body) == b'{"ok":true}'
    assert ledger.disposition("request-a")[0] == "uncertain"
    response.body.acknowledge(hashlib.sha256(b'{"ok":true}').hexdigest())
    reopened = PermitLedger(tmp_path / "spend", controller)
    assert reopened.disposition("request-a") == ("settled", hashlib.sha256(b'{"ok":true}').hexdigest())
    with pytest.raises(BrokerRejected, match="replay|settled"):
        reopened.reserve(permit_id, 1, "request-a", _identity())
    reopened.fence_epoch(permit_id, 1)
    with pytest.raises(BrokerRejected, match="fenced"):
        reopened.reserve(permit_id, 1, "request-b", _identity())
    assert len(sends) == 1


def test_exact_epoch_and_session_permit_are_enforced_before_upstream(tmp_path):
    permit = _permit(route="resume", session_id="session-1")
    _, ledger, permit_id = _ledger(tmp_path, permit, epoch=4)
    sends = []
    with pytest.raises(BrokerRejected, match="epoch"):
        dispatch_once(_request(permit_id, epoch=3), ledger, _broker(permit, sends=sends))
    wrong_session = replace(permit, session_id="session-2")
    with pytest.raises(BrokerRejected, match="permit"):
        dispatch_once(_request(permit_id, epoch=4), ledger,
                      _broker(wrong_session, sends=sends))
    assert sends == []


def test_ledger_rejects_tamper_and_other_controller(tmp_path):
    controller, ledger, permit_id = _ledger(tmp_path)
    other = create_lifecycle_controller(tmp_path / "other-controller")
    with pytest.raises(BrokerRejected, match="controller"):
        PermitLedger(tmp_path / "spend", other).reserve(permit_id, 1, "request-a", _identity())
    record = tmp_path / "spend" / "ledger.json"
    data = record.read_bytes().replace(b'"epoch":1', b'"epoch":9')
    record.write_bytes(data)
    with pytest.raises(BrokerRejected, match="integrity|invalid"):
        PermitLedger(tmp_path / "spend", controller).reserve(permit_id, 1, "request-a", _identity())


def test_interrupted_sse_stream_stays_uncertain_and_cannot_replay(tmp_path):
    permit = _permit()
    controller, ledger, permit_id = _ledger(tmp_path, permit)

    def partial():
        yield b'data: {"message":"one"}\n\n'
        raise OSError("upstream broke")

    broker = _broker(permit, response=BrokerResponse(
        200, {"content-type": "text/event-stream"}, partial()))
    response = dispatch_once(_request(permit_id), ledger, broker)
    chunks = iter(response.body)
    assert next(chunks) == b'data: {"message":"one"}\n\n'
    with pytest.raises(BrokerRejected, match="stream"):
        list(chunks)
    assert PermitLedger(tmp_path / "spend", controller).disposition("request-a")[0] == "uncertain"
    with pytest.raises(BrokerRejected, match="uncertain"):
        ledger.reserve(permit_id, 1, "request-a", _identity())


def test_complete_sse_stream_settles_only_after_consumption(tmp_path):
    _, ledger, permit_id = _ledger(tmp_path)
    ledger.record_attempt(permit_id, 1)
    ledger.reserve(permit_id, 1, "request-a", _identity())
    body = [b'data: {"message":"one"}\n\n', b'data: [DONE]\n\n']
    response = stream_and_settle(BrokerResponse(200, {"content-type": "text/event-stream"},
                                                iter(body)), ledger, "request-a")
    iterator = iter(response.body)
    assert next(iterator) == body[0]
    assert ledger.disposition("request-a")[0] == "uncertain"
    assert list(iterator) == body[1:]
    assert ledger.disposition("request-a")[0] == "uncertain"
    response.body.acknowledge(hashlib.sha256(b"".join(body)).hexdigest())
    assert ledger.disposition("request-a") == (
        "settled", hashlib.sha256(b"".join(body)).hexdigest())


def test_relay_stream_byte_budget_leaves_overflow_uncertain(tmp_path):
    _, ledger, permit_id = _ledger(tmp_path, replace(_permit(), max_response_bytes=8))
    ledger.record_attempt(permit_id, 1)
    ledger.reserve(permit_id, 1, "request-a", _identity())
    response = stream_and_settle(BrokerResponse(200, {"content-type": "text/event-stream"},
                                                iter([b"12345678", b"9"])), ledger,
                                 "request-a")
    with pytest.raises(BrokerRejected, match="byte budget"):
        list(response.body)
    assert ledger.disposition("request-a")[0] == "uncertain"


@pytest.mark.parametrize("extra", [{"host_path": "/Users/me"}, {"command": "id"},
                                     {"socket": "/run/fanout/other.sock"},
                                     {"upstream_url": "https://example.invalid"}])
def test_relay_refuses_host_path_command_second_socket_and_proxy_fields(extra):
    frame = {"type": "request", "permit_id": "a" * 64, "epoch": 1,
             "request_id": "request-a", "method": "POST",
             "provider_path": "/v1/messages", "headers": _headers(),
             "body_b64": base64.b64encode(_body()).decode(), **extra}
    with pytest.raises(BrokerRejected, match="frame|field"):
        parse_request_frame(frame)


@pytest.mark.parametrize("path", ["https://example.invalid/v1/messages", "//host/path",
                                       "/v1/messages\nHost: hostile"])
def test_relay_refuses_arbitrary_url_or_control_path(path):
    frame = {"type": "request", "permit_id": "a" * 64, "epoch": 1,
             "request_id": "request-a", "method": "POST",
             "provider_path": path, "headers": _headers(),
             "body_b64": base64.b64encode(_body()).decode()}
    with pytest.raises(BrokerRejected, match="path"):
        parse_request_frame(frame)


def test_frame_size_and_duplicate_json_fields_are_refused():
    wire = io.BytesIO((2_000_001).to_bytes(4, "big"))
    with pytest.raises(BrokerRejected, match="frame"):
        FrameStream(wire, io.BytesIO()).recv()
    raw = b'{"type":"close","type":"request"}'
    with pytest.raises(BrokerRejected, match="duplicate"):
        FrameStream(io.BytesIO(len(raw).to_bytes(4, "big") + raw), io.BytesIO()).recv()


def test_frame_write_completes_short_writes():
    class ShortWriter:
        def __init__(self):
            self.data = bytearray()

        def write(self, data):
            self.data.extend(data[:1])
            return 1

        def flush(self):
            pass

    writer = ShortWriter()
    FrameStream(io.BytesIO(), writer).send({"type": "close"})
    assert FrameStream(io.BytesIO(writer.data), io.BytesIO()).recv() == {"type": "close"}


@pytest.mark.parametrize("side", ["host", "guest"])
def test_frame_write_times_out_when_peer_stops_reading(side):
    guest = _guest_module() if side == "guest" else None
    frame_type = FrameStream if side == "host" else guest.FrameStream
    read_fd, write_fd = os.pipe()
    os.set_blocking(write_fd, False)
    try:
        while True:
            os.write(write_fd, b"x" * 4096)
    except BlockingIOError:
        pass
    os.set_blocking(write_fd, True)
    writer = os.fdopen(write_fd, "wb", buffering=0)
    frames = frame_type(io.BytesIO(), writer)
    frames.write_timeout = 0.05
    failures = []

    def send():
        try:
            frames.send({"type": "close"})
        except Exception as error:
            failures.append(error)

    worker = threading.Thread(target=send, daemon=True)
    worker.start()
    worker.join(timeout=0.3)
    stalled = worker.is_alive()
    os.close(read_fd)
    worker.join(timeout=1)
    writer.close()
    assert not stalled
    assert len(failures) == 1
    assert isinstance(failures[0], BrokerRejected if side == "host" else guest.RelayRejected)
    assert "deadline" in str(failures[0])


def test_invalid_broker_request_consumes_attempt_but_not_spend(tmp_path):
    permit = _permit(budget=1)
    _, ledger, permit_id = _ledger(tmp_path, permit)
    sends = []
    broker = _broker(permit, sends=sends)
    with pytest.raises(BrokerRejected, match="generation"):
        dispatch_once(_request(permit_id, request_id="too-large", limit=33), ledger, broker)
    with pytest.raises(BrokerRejected, match="reservation"):
        ledger.disposition("too-large")
    assert ledger.request_records(permit_id, 1) == ()
    assert ledger.attempt_counts(permit_id, 1) == (1, 0)
    with pytest.raises(BrokerRejected, match="attempt budget"):
        dispatch_once(_request(permit_id, request_id="one"), ledger, broker)
    assert sends == []


@pytest.mark.parametrize("change", ("path", "auth", "model", "body"))
def test_invalid_agy_main_does_not_reserve_or_block_measured_main(tmp_path, change):
    permit = replace(_permit("agy", budget=1), model="gemini-3.8-flash-high")
    _, ledger, permit_id = _ledger(tmp_path, permit)
    sends = []
    broker = _broker(permit, sends=sends)
    main_path = "/v1beta/models/gemini-3.8-flash:streamGenerateContent?alt=sse"
    bad = _request(permit_id, "agy", request_id="invalid", path=main_path)
    if change == "path":
        bad = replace(bad, provider_path="/v1beta/models/gemini-3.8-flash-high:streamGenerateContent?alt=sse")
    elif change == "auth":
        bad = replace(bad, headers={"content-type": "application/json",
                                    "x-goog-api-key": "wrong"})
    elif change == "model":
        bad = replace(bad, body=b'{"model":"gemini-3.8-flash-high","generationConfig":{"maxOutputTokens":16},"contents":[]}')
    else:
        bad = replace(bad, body=b'{"generationConfig":')

    with pytest.raises(BrokerRejected):
        dispatch_once(bad, ledger, broker)
    with pytest.raises(BrokerRejected, match="reservation"):
        ledger.disposition("invalid")

    good = _request(permit_id, "agy", request_id="main", path=main_path)
    response = dispatch_once(good, ledger, broker)
    assert b"".join(response.body) == b'{"ok":true}'
    assert len(sends) == 1
    assert sends[0][1] == main_path


def test_broker_reflection_and_agy_auxiliary_are_denied_over_relay(tmp_path):
    permit = _permit("agy", budget=2)
    _, ledger, permit_id = _ledger(tmp_path, permit)
    sends = []
    broker = _broker(permit, response=BrokerResponse(
        200, {"content-type": "application/json"}, b'{"leak":"controller-secret"}'),
        sends=sends)
    with pytest.raises(BrokerRejected, match="disclosure"):
        dispatch_once(_request(permit_id, "agy", request_id="reflect"), ledger, broker)
    assert ledger.disposition("reflect")[0] == "uncertain"
    auxiliary = "/v1beta/models/gemini-3.1-flash-lite-preview:streamGenerateContent?alt=sse"
    with pytest.raises(BrokerRejected, match="path"):
        dispatch_once(_request(permit_id, "agy", request_id="aux", path=auxiliary),
                      ledger, broker)
    assert len(sends) == 1


class _Frames:
    def __init__(self, requests):
        self.requests = iter(requests)
        self.sent = []

    def recv(self):
        return next(self.requests)

    def send(self, frame):
        self.sent.append(frame)


class _FailingFrames(_Frames):
    def __init__(self, requests, fail_type):
        super().__init__(requests)
        self.fail_type = fail_type

    def send(self, frame):
        if frame["type"] == self.fail_type:
            raise BrokerRejected("relay frame write failed")
        super().send(frame)


def _request_frame(permit_id):
    return {"type": "request", "permit_id": permit_id, "epoch": 1,
            "request_id": "request-a", "method": "POST",
            "provider_path": _path(), "headers": _headers(),
            "body_b64": base64.b64encode(_body()).decode()}


def test_host_relay_requires_ready_and_close_handshake(tmp_path):
    permit = _permit()
    _, ledger, permit_id = _ledger(tmp_path, permit)
    relay = HostRelay(ledger, _broker(permit))
    digest = hashlib.sha256(b'{"ok":true}').hexdigest()
    frames = _Frames([{"type": "ready", "seat_id": "seat-1", "permit_id": permit_id,
                       "epoch": 1}, _request_frame(permit_id),
                      {"type": "response_ack", "request_id": "request-a",
                       "response_digest": digest}, {"type": "close"}])
    relay.serve(frames)
    assert [frame["type"] for frame in frames.sent] == [
        "ready_ack", "response_start", "response_chunk", "response_end", "closed"]
    assert base64.b64decode(frames.sent[2]["body_b64"]) == b'{"ok":true}'
    assert frames.sent[3]["response_digest"] == digest
    assert ledger.disposition("request-a")[0] == "settled"


def test_agy_auxiliary_refusal_precedes_one_durable_main_reservation(tmp_path):
    permit = replace(_permit("agy", budget=1), model="gemini-3.8-flash-high",
                     clamp_output_tokens=True)
    controller, ledger, permit_id = _ledger(tmp_path, permit)
    sent = []
    auxiliary_path = "/v1beta/models/gemini-3.1-flash-lite-preview:streamGenerateContent?alt=sse"
    main_path = "/v1beta/models/gemini-3.8-flash:streamGenerateContent?alt=sse"
    digest = hashlib.sha256(b'{"ok":true}').hexdigest()

    def frame(request_id, path):
        return {"type": "request", "permit_id": permit_id, "epoch": 1,
                "request_id": request_id, "method": "POST", "provider_path": path,
                "headers": _headers("agy"),
                "body_b64": base64.b64encode(_body("agy", 64)).decode()}

    frames = _Frames([{"type": "ready", "seat_id": "seat-1", "permit_id": permit_id,
                       "epoch": 1}, frame("aux-1", auxiliary_path),
                      frame("aux-2", auxiliary_path), frame("main", main_path),
                      {"type": "response_ack", "request_id": "main",
                       "response_digest": digest}, {"type": "close"}])

    HostRelay(ledger, _broker(permit, sends=sent)).serve(frames)

    assert [item["type"] for item in frames.sent] == [
        "ready_ack", "reject", "reject", "response_start", "response_chunk",
        "response_end", "closed"]
    assert len(sent) == 1
    assert sent[0][1] == main_path
    assert json.loads(sent[0][3])["generationConfig"]["maxOutputTokens"] == 32
    assert ledger.disposition("main") == ("settled", digest)
    for request_id in ("aux-1", "aux-2"):
        with pytest.raises(BrokerRejected, match="reservation"):
            ledger.disposition(request_id)
    reopened = PermitLedger(tmp_path / "spend", resume_lifecycle_controller(
        controller.root, controller.capability))
    assert reopened.auxiliary_refusal_count(permit_id, 1) == 1
    assert reopened.attempt_counts(permit_id, 1) == (3, 2)
    assert reopened.request_records(permit_id, 1) == ({
        "request_id": "main", "permit_id": permit_id, "epoch": 1,
        "identity": asdict(_identity(
            "agy", path=main_path,
            body=b'{"contents":[],"generationConfig":{"maxOutputTokens":32}}',
            limit=32, model="gemini-3.8-flash")),
        "state": "settled", "response_digest": digest,
    },)


def test_agy_auxiliary_refusal_requires_seat_authentication(tmp_path):
    permit = replace(_permit("agy"), model="gemini-3.8-flash-high")
    _, ledger, permit_id = _ledger(tmp_path, permit)
    auxiliary = "/v1beta/models/gemini-3.1-flash-lite-preview:streamGenerateContent?alt=sse"
    bad = {"type": "request", "permit_id": permit_id, "epoch": 1,
           "request_id": "bad-auth", "method": "POST", "provider_path": auxiliary,
           "headers": {"content-type": "application/json", "x-goog-api-key": "wrong"},
           "body_b64": base64.b64encode(_body("agy")).decode()}
    frames = _Frames([{"type": "ready", "seat_id": "seat-1", "permit_id": permit_id,
                       "epoch": 1}, bad, {"type": "close"}])

    HostRelay(ledger, _broker(permit)).serve(frames)

    assert [item["type"] for item in frames.sent] == ["ready_ack", "reject", "closed"]
    assert ledger.auxiliary_refusal_count(permit_id, 1) == 0


def test_host_relay_bounds_repeated_rejected_frames(tmp_path):
    permit = replace(_permit("agy"), model="gemini-3.8-flash-high")
    _, ledger, permit_id = _ledger(tmp_path, permit)
    auxiliary = "/v1beta/models/gemini-3.1-flash-lite-preview:streamGenerateContent?alt=sse"
    request = {"type": "request", "permit_id": permit_id, "epoch": 1,
               "request_id": "aux", "method": "POST", "provider_path": auxiliary,
               "headers": _headers("agy"),
               "body_b64": base64.b64encode(_body("agy")).decode()}

    class CountingFrames:
        def __init__(self):
            self.received = 0
            self.sent = []

        def recv(self):
            self.received += 1
            if self.received == 1:
                return {"type": "ready", "seat_id": "seat-1",
                        "permit_id": permit_id, "epoch": 1}
            return request

        def send(self, frame):
            self.sent.append(frame)

    frames = CountingFrames()
    with pytest.raises(BrokerRejected, match="budget"):
        HostRelay(ledger, _broker(permit)).serve(frames)
    assert frames.received <= 5
    assert ledger.auxiliary_refusal_count(permit_id, 1) == 1


@pytest.mark.parametrize("provider", ("claude", "codex"))
def test_non_agy_relay_keeps_original_request_attempt_limit(tmp_path, provider):
    permit = _permit(provider, budget=1)
    _, ledger, permit_id = _ledger(tmp_path, permit)
    request = {"type": "request", "permit_id": permit_id, "epoch": 1,
               "request_id": "invalid", "method": "POST", "provider_path": "/v1/other",
               "headers": _headers(provider),
               "body_b64": base64.b64encode(_body(provider)).decode()}

    class CountingFrames:
        def __init__(self):
            self.received = 0
            self.sent = []

        def recv(self):
            self.received += 1
            if self.received == 1:
                return {"type": "ready", "seat_id": "seat-1",
                        "permit_id": permit_id, "epoch": 1}
            return request

        def send(self, frame):
            self.sent.append(frame)

    frames = CountingFrames()
    with pytest.raises(BrokerRejected, match="budget"):
        HostRelay(ledger, _broker(permit)).serve(frames)
    assert frames.received == 3
    assert [frame["type"] for frame in frames.sent] == ["ready_ack", "reject"]
    assert ledger.attempt_counts(permit_id, 1) == (1, 0)
    with pytest.raises(BrokerRejected, match="reservation"):
        ledger.disposition("invalid")


@pytest.mark.parametrize("fail_type", ["response_start", "response_chunk", "response_end"])
def test_failed_byte_response_frame_never_settles(tmp_path, fail_type):
    permit = _permit()
    _, ledger, permit_id = _ledger(tmp_path, permit)
    frames = _FailingFrames([{"type": "ready", "seat_id": "seat-1",
                              "permit_id": permit_id, "epoch": 1},
                             _request_frame(permit_id), {"type": "close"}], fail_type)
    with pytest.raises(BrokerRejected):
        HostRelay(ledger, _broker(permit)).serve(frames)
    assert ledger.disposition("request-a")[0] == "uncertain"


def test_missing_guest_completion_ack_leaves_stream_uncertain(tmp_path):
    permit = _permit()
    _, ledger, permit_id = _ledger(tmp_path, permit)
    frames = _Frames([{"type": "ready", "seat_id": "seat-1",
                       "permit_id": permit_id, "epoch": 1},
                      _request_frame(permit_id), {"type": "close"}])
    response = BrokerResponse(200, {"content-type": "text/event-stream"},
                              iter([b'data: {"ok":true}\n\n']))
    with pytest.raises(BrokerRejected, match="ack"):
        HostRelay(ledger, _broker(permit, response=response)).serve(frames)
    assert ledger.disposition("request-a")[0] == "uncertain"


def test_host_relay_rejects_wrong_seat_before_spend(tmp_path):
    permit = _permit()
    _, ledger, permit_id = _ledger(tmp_path, permit)
    frames = _Frames([{"type": "ready", "seat_id": "seat-2", "permit_id": permit_id,
                       "epoch": 1}])
    with pytest.raises(BrokerRejected, match="seat"):
        HostRelay(ledger, _broker(permit)).serve(frames)
    assert frames.sent == []


def test_host_relay_rejects_unhashable_ready_permit_as_protocol_error(tmp_path):
    permit = _permit()
    _, ledger, _ = _ledger(tmp_path, permit)
    frames = _Frames([{"type": "ready", "seat_id": "seat-1", "permit_id": {},
                       "epoch": 1}])
    with pytest.raises(BrokerRejected, match="permit"):
        HostRelay(ledger, _broker(permit)).serve(frames)


def test_rejected_request_does_not_reflect_a_credential_in_request_id(tmp_path):
    permit = _permit()
    _, ledger, permit_id = _ledger(tmp_path, permit)
    bad = _request_frame(permit_id)
    bad["request_id"] = "controller-secret"
    bad["provider_path"] = "/v1/other"
    frames = _Frames([{"type": "ready", "seat_id": "seat-1", "permit_id": permit_id,
                       "epoch": 1}, bad, {"type": "close"}])
    HostRelay(ledger, _broker(permit)).serve(frames)
    assert b"controller-secret" not in json.dumps(frames.sent).encode()
    assert [frame["type"] for frame in frames.sent] == ["ready_ack", "reject", "closed"]


@pytest.mark.parametrize("provider,path,body,expected_path,expected_body,model", [
    ("claude", "/v1/messages?beta=tools-1", None, "/v1/messages",
     b'{"max_tokens":32,"messages":[],"model":"test-model"}', "test-model"),
    ("codex", "/v1/responses", b'{"model":"test-model","messages":[]}',
     "/v1/responses", b'{"max_output_tokens":32,"messages":[],"model":"test-model"}',
     "test-model"),
    ("agy", "/v1beta/models/gemini-3.8-flash:streamGenerateContent?alt=sse",
     b'{"model":"gemini-3.8-flash","generationConfig":{"maxOutputTokens":64},"contents":[]}',
     "/v1beta/models/gemini-3.8-flash:streamGenerateContent?alt=sse",
     b'{"contents":[],"generationConfig":{"maxOutputTokens":32},"model":"gemini-3.8-flash"}',
     "gemini-3.8-flash"),
])
def test_outbound_identity_uses_validated_wire_request(
    tmp_path, provider, path, body, expected_path, expected_body, model,
):
    permit = replace(_permit(provider), model=("gemini-3.8-flash-high" if provider == "agy"
                                                else "test-model"), clamp_output_tokens=provider != "codex")
    controller, ledger, permit_id = _ledger(tmp_path, permit)
    request = _request(permit_id, provider, path=path, limit=64 if provider != "codex" else 16)
    if body is not None:
        request = replace(request, body=body)
    broker = _broker(permit)
    response = dispatch_once(request, ledger, broker)
    assert b"".join(response.body) == b'{"ok":true}'
    records = PermitLedger(tmp_path / "spend", resume_lifecycle_controller(
        controller.root, controller.capability)).request_records(permit_id, 1)
    assert records == ({"request_id": "request-a", "permit_id": permit_id, "epoch": 1,
                        "identity": {"method": "POST", "path": expected_path,
                                     "body_sha256": hashlib.sha256(expected_body).hexdigest(),
                                     "effective_output_tokens": 32,
                                     "wire_model": model,
                                     "upstream_route_digest": None,
                                     "upstream_origin": None,
                                     "upstream_path": None,
                                     "upstream_body_sha256": None,
                                     "upstream_model": None},
                        "state": "uncertain", "response_digest": None},)


def test_same_model_different_route_cannot_reuse_permit(tmp_path):
    permit = _routed_permit()
    _, ledger, permit_id = _ledger(tmp_path, permit)
    with pytest.raises(BrokerRejected, match="identity"):
        ledger.require_permit(permit_id, 1, replace(permit, upstream_route_digest="b" * 64))


@pytest.mark.parametrize("change", (
    {"project": "another-fake-project"},
    {"credential_ref": "keychain:another-inert-ref"},
    {"credential_ref": "op://FakeVault/FakeItem/key", "auth_family": "openai-1password"},
    {"origin": "https://api.changed.invalid"},
    {"transform_version": 2},
))
def test_same_model_changed_machine_route_cannot_reuse_permit(tmp_path, change):
    profile = RouteProfile(
        route_id="org-codex", provider="codex", auth_family="openai-keychain",
        api_family="openai-responses", origin="https://api.openai.com",
        project="invented-project", location=None, requested_model="gpt-6.1-sol",
        wire_model="gpt-6.1-sol", effective_model="gpt-6.1-sol",
        credential_ref="keychain:inert-ref", max_data_classification="protected",
        execution_classes=("repo-write",), transform_version=1,
    )
    admission = RouteAdmission("machine-a", "1" * 64, "2" * 64, profile)
    changed = replace(admission, profile=replace(profile, **change),
                      manifest_digest=("3" * 64 if "credential_ref" in change
                                       else admission.manifest_digest))
    permit = replace(_routed_permit(), upstream_route_digest=admission.digest)
    _, ledger, permit_id = _ledger(tmp_path, permit)
    with pytest.raises(BrokerRejected, match="identity"):
        ledger.require_permit(permit_id, 1,
                              replace(permit, upstream_route_digest=changed.digest))


@pytest.mark.parametrize("field", ("policy_digest", "manifest_digest", "machine_id"))
def test_same_model_changed_machine_authority_cannot_reuse_permit(tmp_path, field):
    profile = RouteProfile(
        "org-codex", "codex", "openai-keychain", "openai-responses",
        "https://api.openai.com", "invented-project", None, "gpt-6.1-sol",
        "gpt-6.1-sol", "gpt-6.1-sol", "keychain:inert-ref", "protected",
        ("repo-write",), 1,
    )
    admission = RouteAdmission("machine-a", "1" * 64, "2" * 64, profile)
    value = "machine-b" if field == "machine_id" else "3" * 64
    permit = replace(_routed_permit(), upstream_route_digest=admission.digest)
    _, ledger, permit_id = _ledger(tmp_path, permit)
    with pytest.raises(BrokerRejected, match="identity"):
        ledger.require_permit(permit_id, 1, replace(
            permit, upstream_route_digest=replace(admission, **{field: value}).digest))


def test_ledger_reopen_refuses_tampered_effective_body(tmp_path):
    controller, ledger, permit_id = _ledger(tmp_path, _routed_permit())
    ledger.record_attempt(permit_id, 1)
    ledger.reserve(permit_id, 1, "request-a", _routed_identity())
    record_path = tmp_path / "spend" / "ledger.json"
    record = json.loads(record_path.read_text())
    record["payload"]["requests"]["request-a"]["identity"]["upstream_body_sha256"] = "0" * 64
    record_path.write_text(json.dumps(record))
    with pytest.raises(BrokerRejected, match="integrity"):
        PermitLedger(tmp_path / "spend", controller).request_records(permit_id, 1)


@pytest.mark.parametrize("field,value", (
    ("upstream_model", "different-model"),
    ("upstream_path", "/v1/different-route"),
    ("upstream_origin", "https://different.invalid"),
    ("upstream_route_digest", "c" * 64),
))
def test_authenticated_effective_request_cannot_differ_from_permit(tmp_path, field, value):
    controller, ledger, permit_id = _ledger(tmp_path, _routed_permit())
    ledger.record_attempt(permit_id, 1)
    ledger.reserve(permit_id, 1, "request-a", _routed_identity())
    record_path = tmp_path / "spend" / "ledger.json"
    record = json.loads(record_path.read_text())
    record["payload"]["requests"]["request-a"]["identity"][field] = value
    record["mac"] = ledger._mac(record["payload"])
    record_path.write_text(json.dumps(record))
    with pytest.raises(BrokerRejected, match="identity"):
        PermitLedger(tmp_path / "spend", controller).request_records(permit_id, 1)


def test_route_preview_is_reserved_before_sender_model_call(tmp_path):
    permit = _routed_permit()
    _, ledger, permit_id = _ledger(tmp_path, permit)

    class Sender:
        def _prepare(self, method, path, headers, body):
            assert method == "POST" and path == "/v1/messages"
            assert headers == {"content-type": "application/json"}
            return PreparedOutbound(permit.upstream_origin, permit.upstream_path,
                                    body.replace(b'"messages":', b'"upstream_messages":'),
                                    permit.upstream_model, permit.upstream_route_digest)

        def preview(self, method, path, headers, body):
            return self._prepare(method, path, headers, body)

        def __call__(self, method, path, headers, body):
            prepared = self._prepare(method, path, {
                name: value for name, value in headers.items() if name != "x-api-key"}, body)
            records = ledger.request_records(permit_id, 1)
            assert len(records) == 1 and records[0]["state"] == "uncertain"
            assert records[0]["identity"]["upstream_body_sha256"] == hashlib.sha256(
                prepared.body).hexdigest()
            return BrokerResponse(200, {"content-type": "application/json"}, b'{"ok":true}')

    response = dispatch_once(_request(permit_id), ledger,
                             Broker(permit, credential="controller-secret", send=Sender()))
    assert b"".join(response.body) == b'{"ok":true}'


def test_changed_effective_preview_refuses_before_reservation_or_send(tmp_path):
    permit = _routed_permit()
    _, ledger, permit_id = _ledger(tmp_path, permit)
    sent = []

    class Sender:
        def preview(self, _method, _path, _headers, body):
            return PreparedOutbound(permit.upstream_origin, "/v1/changed-route",
                                    body, permit.upstream_model,
                                    permit.upstream_route_digest)

        def __call__(self, *args):
            sent.append(args)
            return BrokerResponse(200, {"content-type": "application/json"}, b'{}')

    with pytest.raises(BrokerRejected, match="preview"):
        dispatch_once(_request(permit_id), ledger,
                      Broker(permit, credential="controller-secret", send=Sender()))
    assert ledger.request_records(permit_id, 1) == ()
    assert sent == []


def test_reserved_send_cannot_replay_after_broker_restart(tmp_path):
    permit = _routed_permit()
    controller, ledger, permit_id = _ledger(tmp_path, permit)
    sent = []

    class Sender:
        def preview(self, _method, _path, _headers, body):
            return PreparedOutbound(permit.upstream_origin, permit.upstream_path,
                                    body, permit.upstream_model,
                                    permit.upstream_route_digest)

        def __call__(self, *args):
            sent.append(args)
            return BrokerResponse(200, {"content-type": "application/json"}, b'{}')

    sender = Sender()
    dispatch_once(_request(permit_id), ledger,
                  Broker(permit, credential="controller-secret", send=sender))
    identity = container_broker.OutboundRequestIdentity(
        **ledger.request_records(permit_id, 1)[0]["identity"])
    request = _request(permit_id)
    reopened = PermitLedger(tmp_path / "spend", resume_lifecycle_controller(
        controller.root, controller.capability))
    with pytest.raises(BrokerRejected, match="claimed|replay"):
        Broker(permit, credential="controller-secret", send=sender).dispatch(
            request.method, request.provider_path, request.headers, request.body,
            reservation=(reopened, permit_id, 1, request.request_id, identity))
    assert len(sent) == 1


def test_reserved_send_must_match_exact_effective_request(tmp_path):
    permit = _routed_permit()
    _, ledger, permit_id = _ledger(tmp_path, permit)
    ledger.record_attempt(permit_id, 1)
    ledger.reserve(permit_id, 1, "request-a", _routed_identity())
    sent = []

    class Sender:
        def preview(self, _method, _path, _headers, body):
            return PreparedOutbound(permit.upstream_origin, permit.upstream_path,
                                    body, permit.upstream_model,
                                    permit.upstream_route_digest)

        def __call__(self, *args):
            sent.append(args)
            return BrokerResponse(200, {"content-type": "application/json"}, b'{}')

    request = _request(permit_id)
    with pytest.raises(BrokerRejected, match="reservation request"):
        Broker(permit, credential="controller-secret", send=Sender()).dispatch(
            request.method, request.provider_path, request.headers, request.body,
            reservation=(ledger, permit_id, 1, request.request_id, _routed_identity()))
    assert sent == []


def test_ledger_reopen_rejects_old_schema_and_malformed_authenticated_entries(tmp_path):
    controller, ledger, permit_id = _ledger(tmp_path)
    ledger.record_attempt(permit_id, 1)
    ledger.reserve(permit_id, 1, "request-a", _identity())
    record_path = tmp_path / "spend" / "ledger.json"
    original = json.loads(record_path.read_text())
    mutations = [
        lambda payload: payload.update(schema_version="fanout-container-spend-v2"),
        lambda payload: payload.update(schema_version="fanout-container-spend-v3"),
        lambda payload: payload["requests"]["request-a"]["identity"].update(wire_model=5),
        lambda payload: payload["requests"]["request-a"]["identity"].update(prompt="secret"),
        lambda payload: payload["requests"]["request-a"].update(epoch=2),
        lambda payload: payload["permits"][permit_id].update(attempts=0),
        lambda payload: payload["permits"][permit_id].update(auxiliary_attempts=2),
        lambda payload: payload["permits"][permit_id].update(unexpected=True),
        lambda payload: payload["permits"][permit_id].update(provider=[]),
        lambda payload: payload["requests"]["request-a"].update(state=[]),
        lambda payload: payload["requests"]["request-a"].update(permit_id="f" * 64),
        lambda payload: payload["requests"].update(extra=dict(payload["requests"]["request-a"])),
    ]
    for mutate in mutations:
        changed = json.loads(json.dumps(original))
        mutate(changed["payload"])
        changed["mac"] = ledger._mac(changed["payload"])
        record_path.write_text(json.dumps(changed))
        with pytest.raises(BrokerRejected, match="invalid|schema|entry|epoch"):
            PermitLedger(tmp_path / "spend", controller).request_records(permit_id, 1)
    changed = json.loads(json.dumps(original))
    changed["payload"]["requests"]["request-a"]["identity"]["wire_model"] = "changed"
    record_path.write_text(json.dumps(changed))
    with pytest.raises(BrokerRejected, match="integrity"):
        PermitLedger(tmp_path / "spend", controller).request_records(permit_id, 1)


def test_reserve_rejects_mismatched_identity_before_persisting(tmp_path):
    controller, ledger, permit_id = _ledger(tmp_path)
    ledger.record_attempt(permit_id, 1)
    with pytest.raises(BrokerRejected, match="identity"):
        ledger.reserve(permit_id, 1, "wrong", _identity(model="other-model"))
    reopened = PermitLedger(tmp_path / "spend", resume_lifecycle_controller(
        controller.root, controller.capability))
    assert reopened.request_records(permit_id, 1) == ()
    reopened.reserve(permit_id, 1, "right", _identity())
    assert reopened.disposition("right") == ("uncertain", None)


def test_cold_reopen_rejects_more_classified_events_than_attempts(tmp_path):
    permit = replace(_permit("agy"), model="gemini-3.8-flash-high")
    controller, ledger, permit_id = _ledger(tmp_path, permit)
    ledger.record_attempt(permit_id, 1)
    main = "/v1beta/models/gemini-3.8-flash:streamGenerateContent?alt=sse"
    ledger.reserve(permit_id, 1, "main", _identity("agy", path=main,
                                                     model="gemini-3.8-flash"))
    record_path = tmp_path / "spend" / "ledger.json"
    record = json.loads(record_path.read_text())
    record["payload"]["permits"][permit_id]["auxiliary_attempts"] = 1
    record["payload"]["permits"][permit_id]["auxiliary_refusals"] = 1
    record["mac"] = ledger._mac(record["payload"])
    record_path.write_text(json.dumps(record))
    with pytest.raises(BrokerRejected, match="attempt"):
        PermitLedger(tmp_path / "spend", controller).request_records(permit_id, 1)


@pytest.mark.parametrize("provider,model,expected", [
    ("agy", "gemini-3.8-flash-high", 34),
    ("agy", "test-model", 32),
    ("claude", "test-model", 32),
    ("codex", "test-model", 32),
])
def test_attempt_ceiling_is_profile_specific_and_finite(tmp_path, provider, model, expected):
    permit = replace(_permit(provider, budget=32), model=model)
    _, ledger, permit_id = _ledger(tmp_path, permit)
    assert container_broker.attempt_limit(permit) == expected
    for count in range(1, expected + 1):
        ledger.record_attempt(permit_id, 1)
        assert ledger.attempt_counts(permit_id, 1) == (count, 0)
    with pytest.raises(BrokerRejected, match="attempt|budget"):
        ledger.record_attempt(permit_id, 1)


def test_attempt_reopen_rejects_stale_epoch_and_excess_auxiliary(tmp_path):
    controller, ledger, permit_id = _ledger(tmp_path, replace(
        _permit("agy"), model="gemini-3.8-flash-high"))
    ledger.record_attempt(permit_id, 1)
    ledger.record_auxiliary_refusal(permit_id, 1)
    reopened = PermitLedger(tmp_path / "spend", resume_lifecycle_controller(
        controller.root, controller.capability))
    assert reopened.attempt_counts(permit_id, 1) == (1, 1)
    assert reopened.auxiliary_refusal_count(permit_id, 1) == 1
    with pytest.raises(BrokerRejected, match="epoch"):
        reopened.record_attempt(permit_id, 2)


def test_host_relay_records_rejected_shape_and_aborts_invalid_json(tmp_path):
    permit = _permit(budget=2)
    _, ledger, permit_id = _ledger(tmp_path, permit)
    frames = _Frames([{"type": "ready", "seat_id": "seat-1", "permit_id": permit_id,
                       "epoch": 1}, {"type": "request", "unexpected": True}, {"type": "close"}])
    HostRelay(ledger, _broker(permit)).serve(frames)
    assert [frame["type"] for frame in frames.sent] == ["ready_ack", "reject", "closed"]
    assert ledger.attempt_counts(permit_id, 1) == (1, 0)
    malformed = b'{"type":"request",'
    wire = io.BytesIO()
    writer = FrameStream(io.BytesIO(), wire)
    writer.send({"type": "ready", "seat_id": "seat-1", "permit_id": permit_id, "epoch": 1})
    wire.write(len(malformed).to_bytes(4, "big") + malformed)
    with pytest.raises(BrokerRejected, match="JSON"):
        HostRelay(ledger, _broker(permit)).serve(FrameStream(io.BytesIO(wire.getvalue()), io.BytesIO()))
    assert ledger.attempt_counts(permit_id, 1) == (1, 0)


def test_host_relay_fails_on_extra_malformed_attempt_after_ceiling(tmp_path):
    permit = _permit(budget=1)
    _, ledger, permit_id = _ledger(tmp_path, permit)
    frames = _Frames([{"type": "ready", "seat_id": "seat-1", "permit_id": permit_id,
                       "epoch": 1}, {"type": "request", "unexpected": True},
                      {"type": "request", "another": "malformed"}])
    with pytest.raises(BrokerRejected, match="attempt budget"):
        HostRelay(ledger, _broker(permit)).serve(frames)
    assert [item["type"] for item in frames.sent] == ["ready_ack", "reject"]
    assert ledger.attempt_counts(permit_id, 1) == (1, 0)


def test_guest_idle_session_sends_finite_closure(monkeypatch):
    guest = _guest_module()
    with tempfile.TemporaryDirectory(prefix="fanout-", dir="/private/tmp") as socket_root:
        monkeypatch.setattr(guest, "GUEST_SOCKET_ROOT", socket_root)
        frames = _Frames([{"type": "ready_ack"}, {"type": "closed"}])
        relay = guest.GuestRelay("seat-1", "a" * 64, 1, frames,
                                 max_requests=2, max_request_bytes=1024,
                                 max_response_bytes=1024)
        relay.timeout = 0.01
        relay.serve()
        assert [frame["type"] for frame in frames.sent] == ["ready", "close"]
        assert not os.path.lexists(guest.seat_socket_path("seat-1"))


def test_guest_accepts_only_finite_34_attempt_ceiling(monkeypatch):
    guest = _guest_module()
    with tempfile.TemporaryDirectory(prefix="fanout-", dir="/private/tmp") as socket_root:
        monkeypatch.setattr(guest, "GUEST_SOCKET_ROOT", socket_root)
        frames = _Frames([])
        relay = guest.GuestRelay("seat-1", "a" * 64, 1, frames,
                                 max_requests=34, max_request_bytes=1024,
                                 max_response_bytes=1024)
        try:
            assert relay.max_requests == 34
        finally:
            relay.server_close()
            os.unlink(guest.seat_socket_path("seat-1"))
        with pytest.raises(guest.RelayRejected, match="budget"):
            guest.GuestRelay("seat-1", "a" * 64, 1, frames,
                             max_requests=35, max_request_bytes=1024,
                             max_response_bytes=1024)


def test_guest_unix_http_round_trip_uses_typed_host_frames(monkeypatch, tmp_path):
    guest = _guest_module()
    permit = _permit()
    _, ledger, permit_id = _ledger(tmp_path, permit)
    sent = []
    host_socket, guest_socket = socket.socketpair()
    host_frames = FrameStream(host_socket.makefile("rb", buffering=0),
                              host_socket.makefile("wb", buffering=0), timeout=2)
    guest_frames = guest.FrameStream(guest_socket.makefile("rb", buffering=0),
                                    guest_socket.makefile("wb", buffering=0), timeout=2)
    failures = []

    def run(operation):
        try:
            operation()
        except Exception as error:
            failures.append(error)

    with tempfile.TemporaryDirectory(prefix="fanout-", dir="/private/tmp") as socket_root:
        monkeypatch.setattr(guest, "GUEST_SOCKET_ROOT", socket_root)
        relay = guest.GuestRelay("seat-1", permit_id, 1, guest_frames,
                                 max_requests=1, max_request_bytes=1024,
                                 max_response_bytes=1024)
        host = threading.Thread(target=run, args=(
            lambda: HostRelay(ledger, _broker(permit, sends=sent)).serve(host_frames),), daemon=True)
        remote = threading.Thread(target=run, args=(relay.serve,), daemon=True)
        host.start()
        remote.start()
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(2)
        client.connect(guest.seat_socket_path("seat-1"))
        body = _body()
        client.sendall(("POST /v1/messages HTTP/1.1\r\nHost: 127.0.0.1:8765\r\n"
                        "x-api-key: disposable-placeholder\r\nContent-Type: application/json\r\n"
                        f"Content-Length: {len(body)}\r\n\r\n").encode() + body)
        parts = []
        while chunk := client.recv(4096):
            parts.append(chunk)
        client.close()
        remote.join(timeout=3)
        host.join(timeout=3)
    host_socket.close()
    guest_socket.close()
    assert not failures
    assert not host.is_alive() and not remote.is_alive()
    assert b"HTTP/1.1 200" in b"".join(parts)
    assert b'{"ok":true}' in b"".join(parts)
    assert b"controller-secret" not in b"".join(parts)
    assert len(sent) == 1


def test_guest_host_two_agy_auxiliary_denials_then_one_main_send(monkeypatch, tmp_path):
    guest = _guest_module()
    permit = replace(_permit("agy", budget=1), model="gemini-3.8-flash-high")
    controller, ledger, permit_id = _ledger(tmp_path, permit)
    sent = []
    host_socket, guest_socket = socket.socketpair()
    host_frames = FrameStream(host_socket.makefile("rb", buffering=0),
                              host_socket.makefile("wb", buffering=0), timeout=2)
    guest_frames = guest.FrameStream(guest_socket.makefile("rb", buffering=0),
                                    guest_socket.makefile("wb", buffering=0), timeout=2)
    failures = []

    def run(operation):
        try:
            operation()
        except Exception as error:
            failures.append(error)

    with tempfile.TemporaryDirectory(prefix="fanout-", dir="/private/tmp") as socket_root:
        monkeypatch.setattr(guest, "GUEST_SOCKET_ROOT", socket_root)
        relay = guest.GuestRelay("seat-1", permit_id, 1, guest_frames,
                                 max_requests=3, max_request_bytes=1024,
                                 max_response_bytes=1024)
        host = threading.Thread(target=run, args=(
            lambda: HostRelay(ledger, _broker(permit, sends=sent)).serve(host_frames),),
            daemon=True)
        remote = threading.Thread(target=run, args=(relay.serve,), daemon=True)
        host.start()
        remote.start()

        def post(path):
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(2)
            client.connect(guest.seat_socket_path("seat-1"))
            body = _body("agy")
            client.sendall((f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1:8765\r\n"
                            "x-goog-api-key: disposable-placeholder\r\n"
                            "Content-Type: application/json\r\n"
                            f"Content-Length: {len(body)}\r\n\r\n").encode() + body)
            chunks = []
            while chunk := client.recv(4096):
                chunks.append(chunk)
            client.close()
            return b"".join(chunks)

        auxiliary = "/v1beta/models/gemini-3.1-flash-lite-preview:streamGenerateContent?alt=sse"
        main = "/v1beta/models/gemini-3.8-flash:streamGenerateContent?alt=sse"
        statuses = [post(auxiliary), post(auxiliary), post(main)]
        remote.join(timeout=3)
        host.join(timeout=3)
    host_socket.close()
    guest_socket.close()
    assert not failures
    assert not host.is_alive() and not remote.is_alive()
    assert [b"HTTP/1.1 403" in item for item in statuses] == [True, True, False]
    assert b"HTTP/1.1 200" in statuses[2]
    assert len(sent) == 1
    reopened = PermitLedger(tmp_path / "spend", resume_lifecycle_controller(
        controller.root, controller.capability))
    assert reopened.attempt_counts(permit_id, 1) == (3, 2)
    assert reopened.auxiliary_refusal_count(permit_id, 1) == 1
    records = reopened.request_records(permit_id, 1)
    assert len(records) == 1
    assert records[0]["state"] == "settled"
    assert records[0]["identity"]["wire_model"] == "gemini-3.8-flash"


def _guest_module():
    path = ROOT / "components/fanout-container/relay.py"
    spec = importlib.util.spec_from_file_location("fanout_guest_relay_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_guest_socket_is_exact_and_bridge_is_loopback_only(tmp_path, monkeypatch):
    guest = _guest_module()
    monkeypatch.setattr(guest, "GUEST_SOCKET_ROOT", str(tmp_path))
    assert guest.seat_socket_path("seat-1") == str(tmp_path / "seat-1.sock")
    with pytest.raises(ValueError):
        guest.seat_socket_path("../other")
    bridge = guest.LoopbackBridge(port=0, connector=lambda path: None)
    try:
        assert bridge.server_address[0] == "127.0.0.1"
        assert bridge.broker_socket_path == "/run/fanout/broker.sock"
    finally:
        bridge.server_close()


def test_bridge_forwards_bounded_http_to_only_mounted_unix_socket(tmp_path):
    guest = _guest_module()
    observed = []
    left, right = socket.socketpair()

    def connector(path):
        observed.append(path)
        return left

    bridge = guest.LoopbackBridge(port=0, connector=connector, max_request_bytes=1024,
                                  max_response_bytes=1024)
    thread = threading.Thread(target=bridge.serve_forever, daemon=True)
    thread.start()
    try:
        client = socket.create_connection(bridge.server_address)
        client.sendall((f"POST /v1/messages HTTP/1.1\r\nHost: 127.0.0.1:{bridge.server_address[1]}\r\n"
                        "Content-Type: application/json\r\nContent-Length: 2\r\n\r\n{}").encode())
        forwarded = right.recv(4096)
        assert b"POST /v1/messages HTTP/1.1" in forwarded
        right.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}")
        assert b"HTTP/1.1 200" in client.recv(4096)
        client.close()
        assert observed == ["/run/fanout/broker.sock"]
    finally:
        bridge.shutdown()
        bridge.server_close()
        right.close()
        thread.join(timeout=2)


def test_bridge_slow_request_body_hits_total_deadline(monkeypatch):
    guest = _guest_module()
    monkeypatch.setattr(guest, "REQUEST_DEADLINE", 0.05, raising=False)
    connected = []
    bridge = guest.LoopbackBridge(port=0, connector=lambda path: connected.append(path))
    thread = threading.Thread(target=bridge.serve_forever, daemon=True)
    thread.start()
    client = socket.create_connection(bridge.server_address)
    client.settimeout(0.5)
    try:
        client.sendall((f"POST /v1/messages HTTP/1.1\r\n"
                        f"Host: 127.0.0.1:{bridge.server_address[1]}\r\n"
                        "Content-Type: application/json\r\nContent-Length: 30\r\n\r\nx").encode())
        time.sleep(0.08)
        assert b"403" in client.recv(4096)
        assert connected == []
    finally:
        client.close()
        bridge.shutdown()
        bridge.server_close()
        thread.join(timeout=2)


def test_bridge_incomplete_headers_cannot_hold_listener_forever(monkeypatch):
    guest = _guest_module()
    monkeypatch.setattr(guest, "REQUEST_DEADLINE", 0.05, raising=False)
    connected = []
    bridge = guest.LoopbackBridge(port=0, connector=lambda path: connected.append(path))
    thread = threading.Thread(target=bridge.serve_forever, daemon=True)
    thread.start()
    client = socket.create_connection(bridge.server_address)
    client.settimeout(0.5)
    try:
        client.sendall(b"POST /v1/messages HTTP/1.1\r\nHost: 127.0.0.1:")
        response = client.recv(4096)
        while response:
            response = client.recv(4096)
        assert connected == []
    finally:
        client.close()
        bridge.shutdown()
        bridge.server_close()
        thread.join(timeout=2)


def test_bridge_slow_response_cannot_complete_after_total_deadline(monkeypatch):
    guest = _guest_module()
    monkeypatch.setattr(guest, "RESPONSE_DEADLINE", 0.05, raising=False)
    left, right = socket.socketpair()
    bridge = guest.LoopbackBridge(port=0, connector=lambda path: left)
    thread = threading.Thread(target=bridge.serve_forever, daemon=True)
    thread.start()

    def upstream():
        right.recv(4096)
        try:
            right.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                          b"Transfer-Encoding: chunked\r\n\r\n")
            for _ in range(8):
                right.sendall(b"1\r\nx\r\n")
                time.sleep(0.02)
            right.sendall(b"0\r\n\r\n")
        except OSError:
            pass

    provider = threading.Thread(target=upstream, daemon=True)
    provider.start()
    client = socket.create_connection(bridge.server_address)
    client.settimeout(1)
    try:
        client.sendall((f"POST /v1/messages HTTP/1.1\r\n"
                        f"Host: 127.0.0.1:{bridge.server_address[1]}\r\n"
                        "Content-Type: application/json\r\nContent-Length: 2\r\n\r\n{}").encode())
        parts = []
        while chunk := client.recv(4096):
            parts.append(chunk)
        assert b"HTTP/1.1 200" in b"".join(parts)
        assert b"0\r\n\r\n" not in b"".join(parts)
    finally:
        client.close()
        bridge.shutdown()
        bridge.server_close()
        right.close()
        thread.join(timeout=2)
        provider.join(timeout=2)


@pytest.mark.parametrize("gap", ("headers", "body"))
def test_bridge_response_gap_over_ten_seconds_completes_chunk_framing(gap):
    guest = _guest_module()
    left, right = socket.socketpair()
    right.settimeout(2)
    bridge = guest.LoopbackBridge(port=0, connector=lambda path: left)
    thread = threading.Thread(target=bridge.serve_forever, daemon=True)
    thread.start()
    stop = threading.Event()
    failures = []
    initial = b'data: {"ok":true}\n\n'
    final = b'data: [DONE]\n\n'
    initial_chunk = f"{len(initial):X}\r\n".encode() + initial + b"\r\n"
    final_chunk = f"{len(final):X}\r\n".encode() + final + b"\r\n"

    def upstream():
        try:
            request = bytearray()
            while not request.endswith(b"\r\n\r\n{}"):
                part = right.recv(4096)
                if not part:
                    raise AssertionError("bridge request was incomplete")
                request.extend(part)
            if gap == "headers" and stop.wait(10.2):
                return
            right.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                          b"Transfer-Encoding: chunked\r\n\r\n" + initial_chunk)
            if gap == "body" and stop.wait(10.2):
                return
            right.sendall(final_chunk + b"0\r\n\r\n")
        except OSError:
            pass
        except Exception as error:
            failures.append(error)

    provider = threading.Thread(target=upstream, daemon=True)
    provider.start()
    client = socket.create_connection(bridge.server_address)
    client.settimeout(12)
    try:
        client.sendall((f"POST /v1/messages HTTP/1.1\r\n"
                        f"Host: 127.0.0.1:{bridge.server_address[1]}\r\n"
                        "Content-Type: application/json\r\nContent-Length: 2\r\n\r\n{}").encode())
        parts = []
        while chunk := client.recv(4096):
            parts.append(chunk)
        wire = b"".join(parts)
        assert wire.endswith(b"0\r\n\r\n"), f"bridge truncated response after {gap} gap"
        assert b"HTTP/1.1 200" in wire
        assert wire.split(b"\r\n\r\n", 1)[1] == initial_chunk + final_chunk + b"0\r\n\r\n"
        assert failures == []
    finally:
        stop.set()
        client.close()
        bridge.shutdown()
        bridge.server_close()
        right.close()
        thread.join(timeout=2)
        provider.join(timeout=2)
    assert not thread.is_alive() and not provider.is_alive()


@pytest.mark.parametrize("gap", ("headers", "body"))
def test_bridge_stalled_response_stops_at_absolute_deadline(monkeypatch, gap):
    guest = _guest_module()
    monkeypatch.setattr(guest, "RESPONSE_DEADLINE", 0.1)
    left, right = socket.socketpair()
    right.settimeout(1)
    bridge = guest.LoopbackBridge(port=0, connector=lambda path: left)
    thread = threading.Thread(target=bridge.serve_forever, daemon=True)
    thread.start()
    client = socket.create_connection(bridge.server_address)
    client.settimeout(1)
    try:
        client.sendall((f"POST /v1/messages HTTP/1.1\r\n"
                        f"Host: 127.0.0.1:{bridge.server_address[1]}\r\n"
                        "Content-Type: application/json\r\nContent-Length: 2\r\n\r\n{}").encode())
        request = bytearray()
        while not request.endswith(b"\r\n\r\n{}"):
            request.extend(right.recv(4096))
        if gap == "body":
            right.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                          b"Transfer-Encoding: chunked\r\n\r\n1\r\nx\r\n")
        started = time.monotonic()
        parts = []
        while chunk := client.recv(4096):
            parts.append(chunk)
        assert time.monotonic() - started < 1
        wire = b"".join(parts)
        assert not wire.endswith(b"0\r\n\r\n")
        if gap == "body":
            assert b"HTTP/1.1 200" in wire and b"1\r\nx\r\n" in wire
    finally:
        client.close()
        bridge.shutdown()
        bridge.server_close()
        right.close()
        thread.join(timeout=2)
