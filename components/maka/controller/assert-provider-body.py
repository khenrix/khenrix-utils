#!/usr/bin/env python3
"""Pass the actual pinned AI SDK request body through the proxy gate."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys


class BodyHeaders:
    def get_all(self, name: str) -> list[str]:
        return ["application/json"] if name.lower() == "content-type" else []


class CapturedHeaders:
    def __init__(self, values: dict[str, object]) -> None:
        self.values = {name.lower(): value for name, value in values.items()}

    def get_all(self, name: str) -> list[str]:
        value = self.values.get(name.lower())
        if isinstance(value, str):
            return [value]
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return value
        return []


policy_path = pathlib.Path(__file__).parent / "egress-overlay" / "egress_filter.py"
spec = importlib.util.spec_from_file_location("maka_egress_contract", policy_path)
policy = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = policy
spec.loader.exec_module(policy)

capture = json.loads(pathlib.Path(sys.argv[1]).read_text())
for fallback_body in capture["fallbackBodies"]:
    body = json.dumps(fallback_body, separators=(",", ":")).encode()
    body_request = type("Request", (), {"headers": BodyHeaders(), "raw_content": body})()
    if not policy.valid_openai_body(body_request):
        raise SystemExit("actual pinned Maka request body failed the egress proxy contract")
    marked = json.loads(json.dumps(fallback_body))
    users = [item for item in marked["input"] if item.get("role") == "user"]
    if (len(users) != 1 or not isinstance(users[0].get("content"), list)
            or len(users[0]["content"]) != 1
            or users[0]["content"][0].get("type") != "input_text"):
        raise SystemExit("actual pinned Maka user prompt shape changed")
    users[0]["content"][0]["text"] = (
        "Judge receipt nonce: maka:fanout-judge-0123456789abcdef0123456789abcdef\n"
        "Compare two final answers."
    )
    if policy.prepare_judge_request(json.dumps(marked).encode()) is not None:
        raise SystemExit("historical coding request entered the current Harbor judge route")
websocket = capture["websocket"]
websocket_request = type(
    "WebsocketRequest",
    (),
    {
        "headers": CapturedHeaders(websocket["headers"]),
        "raw_content": b"",
        "scheme": "https",
        "method": websocket["method"],
        "path": websocket["path"],
        "query": (),
    },
)()
if not policy.valid_openai_websocket_probe(websocket_request, policy.OPENAI_API_ENDPOINT):
    raise SystemExit("actual pinned Maka WebSocket probe failed the egress proxy contract")
print("Actual pinned Maka ModelAdapter fallback passes the egress proxy contract")
