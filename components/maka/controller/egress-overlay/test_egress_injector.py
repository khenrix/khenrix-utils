#!/usr/bin/env python3
"""Fail-closed checks for request identity and OpenAI authorization injection."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from mitmproxy import http

module_path = Path("/opt/maka-eval/egress_filter.py")
spec = importlib.util.spec_from_file_location("egress_filter", module_path)
policy = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = policy
spec.loader.exec_module(policy)
unit_key = "unit-dummy-secret"
policy._OPENAI_API_KEY = unit_key
policy._OPENAI_REQUEST_COUNT = 0
policy._OPENAI_WEBSOCKET_PROBE_COUNT = 0
audit: list[tuple[str, str, str]] = []
def append_audit(*args: str) -> bool:
    audit.append(args)
    return True


policy.append_audit = append_audit
valid_body = json.dumps({
    "model": "gpt-5.6-sol",
    "reasoning": {"effort": "xhigh", "summary": "auto"},
    "stream": True,
    "store": False,
    "parallel_tool_calls": True,
    "include": ["reasoning.encrypted_content"],
    "input": "synthetic unit prompt",
    "tools": [{
        "type": "function",
        "name": "Read",
        "description": "Read one synthetic path",
        "parameters": {"type": "object", "properties": {}},
    }, {"type": "apply_patch"}],
    "tool_choice": "auto",
    "prompt_cache_key": "maka:synthetic-session",
}).encode()
tool_free_body = json.dumps({
    key: value
    for key, value in json.loads(valid_body).items()
    if key not in {"tools", "tool_choice"}
}).encode()


def body_with(**updates: object) -> bytes:
    body = json.loads(valid_body)
    body.update(updates)
    return json.dumps(body).encode()


def make_request(
    *,
    host: str = "api.openai.com",
    port: int = 443,
    scheme: str = "https",
    method: str = "POST",
    authority: str = "",
    path: str = "/v1/responses",
    version: str = "HTTP/1.1",
    hosts: tuple[str, ...] | None = ("api.openai.com",),
    body: bytes = valid_body,
    content_types: tuple[str, ...] = ("application/json",),
    content_encoding: str | None = None,
) -> http.Request:
    headers: list[tuple[bytes, bytes]] = [
        (b"authorization", b"Bearer maka-decoy"),
        (b"api-key", b"maka-decoy-api-key"),
        (b"proxy-authorization", b"maka-decoy-proxy"),
        (b"openai-project", b"proj_untrusted"),
        (b"openai-organization", b"org_untrusted"),
    ]
    if hosts is not None:
        headers.extend((b"host", value.encode()) for value in hosts)
    headers.extend((b"content-type", value.encode()) for value in content_types)
    if content_encoding is not None:
        headers.append((b"content-encoding", content_encoding.encode()))
    return http.Request(
        host,
        port,
        method.encode(),
        scheme.encode(),
        authority.encode(),
        path.encode(),
        version.encode(),
        http.Headers(headers),
        body,
        None,
        0,
        None,
    )


def flow(**overrides: object) -> SimpleNamespace:
    request_args = dict(overrides)
    server_host = str(request_args.pop("server_host", request_args.get("host", "api.openai.com")))
    server_port = int(request_args.pop("server_port", request_args.get("port", 443)))
    client_sni = str(request_args.pop("client_sni", request_args.get("host", "api.openai.com")))
    server_sni = str(request_args.pop("server_sni", request_args.get("host", "api.openai.com")))
    client_tls = bool(request_args.pop("client_tls", True))
    server_tls = bool(request_args.pop("server_tls", True))
    request = make_request(**request_args)
    client = SimpleNamespace(tls=client_tls, tls_established=client_tls, sni=client_sni)
    server = SimpleNamespace(
        address=(server_host, server_port),
        tls=server_tls,
        tls_established=server_tls,
        sni=server_sni,
    )
    return SimpleNamespace(request=request, client_conn=client, server_conn=server, response=None)


def run_allowed(value: SimpleNamespace) -> None:
    audit.clear()
    policy.request(value)
    assert value.response is None
    assert value.request.headers.get_all("Authorization") == [f"Bearer {unit_key}"]
    assert value.request.headers.get_all("api-key") == []
    assert value.request.headers.get_all("proxy-authorization") == []
    assert value.request.headers.get_all("openai-project") == []
    assert value.request.headers.get_all("openai-organization") == []
    assert audit == [("openai_authorized", "", f"sequence={policy._OPENAI_REQUEST_COUNT}")]


def websocket_probe() -> SimpleNamespace:
    candidate = flow(method="GET", body=b"", content_types=())
    candidate.request.headers.set_all("Authorization", ["Bearer maka-decoy-synthetic123"])
    candidate.request.headers.set_all("api-key", [])
    candidate.request.headers.set_all("proxy-authorization", [])
    candidate.request.headers.set_all("openai-project", [])
    candidate.request.headers.set_all("openai-organization", [])
    candidate.request.headers.set_all("Connection", ["Upgrade"])
    candidate.request.headers.set_all("Upgrade", ["websocket"])
    candidate.request.headers.set_all("Sec-WebSocket-Version", ["13"])
    candidate.request.headers.set_all("Sec-WebSocket-Key", ["MDEyMzQ1Njc4OWFiY2RlZg=="])
    candidate.request.headers.set_all("OpenAI-Beta", [policy.OPENAI_RESPONSES_WEBSOCKET_PROTOCOL])
    return candidate


before_probe = flow()
audit.clear()
policy.request(before_probe)
assert before_probe.response is not None and before_probe.response.status_code == 403
assert before_probe.request.headers.get_all("Authorization") == ["Bearer maka-decoy"]
assert f"Bearer {unit_key}" not in before_probe.request.headers.get_all("Authorization")
assert audit == [("credential_boundary", "", "rejected")]

probe = websocket_probe()
audit.clear()
policy.request(probe)
assert probe.response is not None and probe.response.status_code == 403
assert probe.response.headers.get("X-Maka-Eval-Egress-Rule") == "openai_websocket_disabled"
assert probe.request.headers.get_all("Authorization") == ["Bearer maka-decoy-synthetic123"]
assert f"Bearer {unit_key}" not in probe.request.headers.get_all("Authorization")
assert audit == [("openai_websocket_disabled", "", "sequence=1")]

duplicate_probe = websocket_probe()
audit.clear()
policy.request(duplicate_probe)
assert duplicate_probe.response is not None and duplicate_probe.response.status_code == 403
assert duplicate_probe.response.headers.get("X-Maka-Eval-Egress-Rule") == "credential_boundary"
assert audit == [("credential_boundary", "", "rejected")]

malformed_probe = websocket_probe()
malformed_probe.request.headers.set_all("Sec-WebSocket-Key", ["not-base64"])
audit.clear()
policy.request(malformed_probe)
assert malformed_probe.response is not None and malformed_probe.response.status_code == 403
assert audit == [("credential_boundary", "", "rejected")]

run_allowed(flow())
run_allowed(flow(version="HTTP/2.0", authority="api.openai.com", hosts=None))
run_allowed(flow(body=body_with(
    tool_choice={"type": "function", "name": "Read"},
)))
run_allowed(flow(body=tool_free_body))

identity_cases = (
    {"host": "attacker.example", "hosts": ("api.openai.com",), "server_host": "attacker.example", "client_sni": "attacker.example", "server_sni": "attacker.example"},
    {"hosts": ("attacker.example",)},
    {"authority": "attacker.example", "hosts": ("api.openai.com",)},
    {"server_host": "attacker.example"},
    {"client_sni": "attacker.example"},
    {"server_sni": "attacker.example"},
    {"client_tls": False},
    {"server_tls": False},
    {"hosts": ("api.openai.com", "api.openai.com")},
    {"hosts": ("api.openai.com:444",)},
    {"hosts": ("api.openai.com.",)},
    {"hosts": (" api.openai.com",)},
    {"hosts": ("api.openai.com\\evil",)},
    {"version": "HTTP/2.0", "authority": "api.openai.com", "hosts": ("attacker.example",)},
    {"version": "HTTP/2.0", "authority": "api.openai.com.", "hosts": None},
)
for case in identity_cases:
    rejected = flow(**case)
    audit.clear()
    policy.request(rejected)
    assert rejected.response is not None and rejected.response.status_code == 421, case
    assert f"Bearer {unit_key}" not in rejected.request.headers.get_all("Authorization"), case
    assert audit == [("authority_mismatch", "", "rejected")], case

credential_cases = (
    {"path": "/v1/models"},
    {"path": "https://api.openai.com/v1/responses"},
    {"method": "GET"},
    {"scheme": "http", "port": 80, "hosts": ("api.openai.com",), "server_port": 80},
    {"port": 444, "hosts": ("api.openai.com:444",), "server_port": 444},
    {"path": "/v1/responses?redirect=attacker.example"},
    {"body": b'{}'},
    {"body": valid_body.replace(b'gpt-5.6-sol', b'gpt-5.6-other')},
    {"body": valid_body.replace(b'"xhigh"', b'"high"')},
    {"body": valid_body[:-1] + b',"service_tier":"priority"}'},
    {"body": valid_body[:-1] + b',"max_output_tokens":100000}'},
    {"body": b'{"model":"gpt-5.6-sol","model":"gpt-5.6-sol","reasoning":{"effort":"xhigh","summary":"auto"},"stream":true,"store":false,"parallel_tool_calls":true,"include":["reasoning.encrypted_content"]}'},
    {"content_types": ()},
    {"content_types": ("application/json", "application/json")},
    {"content_encoding": "gzip"},
    {"body": body_with(background=True)},
    {"body": body_with(include=["reasoning.encrypted_content", "web_search_call.action.sources"])},
    {"body": body_with(input=[{"role": "user", "content": [{"type": "input_image", "image_url": "https://attacker.test/a"}]}])},
    {"body": body_with(input=[{"type": "item_reference", "id": "resp_known_item"}])},
    {"body": body_with(input=[{"type": "mcp_approval_response", "approval_request_id": "known"}])},
    {"body": body_with(tool_choice={"type": "hosted_tool", "name": "web_search"})},
    {"body": body_with(prompt_cache_key="attacker-controlled")},
    {"body": json.dumps({key: value for key, value in json.loads(valid_body).items() if key != "prompt_cache_key"}).encode()},
    {"body": body_with(previous_response_id="resp_known")},
    {"body": body_with(tools=[{"type": "apply_patch", "name": "attacker"}])},
    {"body": json.dumps({key: value for key, value in json.loads(valid_body).items() if key != "tool_choice"}).encode()},
)
for case in credential_cases:
    rejected = flow(**case)
    audit.clear()
    policy.request(rejected)
    assert rejected.response is not None and rejected.response.status_code == 403, case
    assert f"Bearer {unit_key}" not in rejected.request.headers.get_all("Authorization"), case
    assert audit == [("credential_boundary", "", "rejected")], case

for hosted_tool_type in (
    "web_search_preview",
    "web_search",
    "file_search",
    "mcp",
    "computer_use_preview",
    "computer",
    "code_interpreter",
    "image_generation",
):
    rejected = flow(body=body_with(tools=[{"type": hosted_tool_type}]))
    audit.clear()
    policy.request(rejected)
    assert rejected.response is not None and rejected.response.status_code == 403
    assert f"Bearer {unit_key}" not in rejected.request.headers.get_all("Authorization")
    assert audit == [("credential_boundary", "", "rejected")]


def connect_flow(*, host: str, authority: str, hosts: tuple[str, ...] | None) -> SimpleNamespace:
    request = make_request(
        host=host,
        port=443,
        scheme="",
        method="CONNECT",
        authority=authority,
        path=authority,
        hosts=hosts,
    )
    return SimpleNamespace(request=request, response=None)


for connect in (
    connect_flow(host="attacker.example", authority="attacker.example:443", hosts=("api.openai.com:443",)),
    connect_flow(host="api.openai.com", authority="api.openai.com:443", hosts=("attacker.example:443",)),
    connect_flow(host="api.openai.com", authority="api.openai.com.:443", hosts=None),
):
    audit.clear()
    policy.http_connect(connect)
    assert connect.response is not None and connect.response.status_code == 421
    assert f"Bearer {unit_key}" not in connect.request.headers.get_all("Authorization")
    assert audit == [("authority_mismatch", "", "rejected")]

unrelated = flow(
    host="example.com",
    hosts=("example.com",),
    server_host="example.com",
    client_sni="example.com",
    server_sni="example.com",
)
audit.clear()
policy.request(unrelated)
assert unrelated.response is None
assert unrelated.request.headers.get_all("Authorization") == ["Bearer maka-decoy"]
assert audit == []

policy._OPENAI_REQUEST_COUNT = policy.MAX_OPENAI_REQUESTS
over_limit = flow()
audit.clear()
policy.request(over_limit)
assert over_limit.response is not None and over_limit.response.status_code == 403
assert over_limit.request.headers.get_all("Authorization") == ["Bearer maka-decoy"]
assert audit == [("credential_boundary", "", "rejected")]

policy._OPENAI_REQUEST_COUNT = 0
policy.append_audit = lambda *_args: False
audit_unavailable = flow()
policy.request(audit_unavailable)
assert audit_unavailable.response is not None and audit_unavailable.response.status_code == 503
assert audit_unavailable.request.headers.get_all("Authorization") == ["Bearer maka-decoy"]
assert policy._OPENAI_REQUEST_COUNT == 0


def layer_probe(
    *, alpn: bytes | None, client_data: bytes = b"", server_data: bytes = b""
) -> SimpleNamespace:
    context = SimpleNamespace(
        client=SimpleNamespace(alpn=alpn),
        server=SimpleNamespace(address=("api.openai.com", 443)),
        layers=[],
        options=SimpleNamespace(proxy_debug=False),
    )
    return SimpleNamespace(
        layer=None,
        context=context,
        data_client=lambda: client_data,
        data_server=lambda: server_data,
    )


policy.append_audit = append_audit
for http_alpn in policy.HTTP_ALPNS:
    negotiated_http = layer_probe(alpn=http_alpn, server_data=b"server-first")
    audit.clear()
    policy.next_layer(negotiated_http)
    assert negotiated_http.layer is None, http_alpn
    assert audit == [], http_alpn

for unknown_alpn in (None, b"unknown-protocol"):
    unknown_server_first = layer_probe(
        alpn=unknown_alpn, server_data=b"server-first"
    )
    audit.clear()
    policy.next_layer(unknown_server_first)
    assert isinstance(unknown_server_first.layer, policy.CloseRawLayer), unknown_alpn
    assert audit == [("raw_tunnel", "api.openai.com", ":443")], unknown_alpn

original_server_tls = policy.ServerTLSLayer
original_client_tls = policy.ClientTLSLayer


class DummyTlsLayer:
    def __init__(self, context: object) -> None:
        self.context = context
        self.child_layer = None


try:
    policy.ServerTLSLayer = DummyTlsLayer
    policy.ClientTLSLayer = DummyTlsLayer
    fragmented_tls = layer_probe(alpn=None, client_data=b"\x16")
    audit.clear()
    policy.next_layer(fragmented_tls)
    assert isinstance(fragmented_tls.layer, DummyTlsLayer)
    assert isinstance(fragmented_tls.layer.child_layer, DummyTlsLayer)
    assert audit == []
finally:
    policy.ServerTLSLayer = original_server_tls
    policy.ClientTLSLayer = original_client_tls

print("OpenAI request identity and injector boundary tests passed")
