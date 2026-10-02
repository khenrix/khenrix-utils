"""Bounded Codex Responses requests when the pinned CLI omits a token cap."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.lib.fanout.broker import (  # noqa: E402
    Broker, BrokerPermit, BrokerRejected, BrokerResponse,
)


def _permit(*, max_request_bytes: int = 1024) -> BrokerPermit:
    return BrokerPermit(
        provider="codex", run_id="run-1", task_id="task-1",
        target_id="target-1", seat_id="seat-1", route="initial",
        model="test-model", placeholder_key="disposable-placeholder",
        max_requests=1, max_request_bytes=max_request_bytes,
        max_response_bytes=1024, max_output_tokens=32,
    )


def _dispatch(broker: Broker, body: bytes) -> BrokerResponse:
    return broker.dispatch(
        "POST", "/v1/responses",
        {"x-fanout-seat": "disposable-placeholder", "content-type": "application/json"},
        body,
    )


def test_codex_missing_output_cap_is_inserted_by_controller_broker():
    sent: list[bytes] = []

    def upstream(_method, _path, _headers, body):
        sent.append(body)
        return BrokerResponse(200, {"content-type": "application/json"}, b'{}')

    broker = Broker(_permit(), credential="controller-secret", send=upstream)
    response = _dispatch(broker, b'{"model":"test-model","input":[]}')
    assert response.status == 200
    assert len(sent) == 1
    assert json.loads(sent[0])["max_output_tokens"] == 32


@pytest.mark.parametrize("value", (None, 0, 33, "32", True))
def test_codex_explicit_unsafe_output_cap_is_rejected(value):
    sent: list[bytes] = []
    broker = Broker(
        _permit(), credential="controller-secret",
        send=lambda _method, _path, _headers, body: (
            sent.append(body) or BrokerResponse(200, {"content-type": "application/json"}, b'{}')
        ),
    )
    body = json.dumps({"model": "test-model", "input": [],
                       "max_output_tokens": value}).encode()
    with pytest.raises(BrokerRejected, match="generation"):
        _dispatch(broker, body)
    assert sent == []


def test_codex_injected_cap_must_fit_request_byte_budget():
    body = b'{"model":"test-model","input":[]}'
    sent: list[bytes] = []
    broker = Broker(
        _permit(max_request_bytes=len(body) + 1), credential="controller-secret",
        send=lambda _method, _path, _headers, request_body: (
            sent.append(request_body)
            or BrokerResponse(200, {"content-type": "application/json"}, b'{}')
        ),
    )
    with pytest.raises(BrokerRejected, match="byte budget"):
        _dispatch(broker, body)
    assert sent == []
