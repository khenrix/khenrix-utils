# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

"""Fail-closed URL contamination filter for Eval subject egress."""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import time
from pathlib import Path
from typing import NamedTuple
from urllib.parse import unquote, urlsplit

PINNED_REVISION = "d49e28f1e4ddd13d289e85a5f312a66750951932"
MAX_DECODE_PASSES = 4
MAX_AUDIT_BYTES = 1024 * 1024
AUDIT_PATH = Path(
    os.environ.get("MAKA_EVAL_EGRESS_AUDIT", "/opt/maka-egress-state/hits.jsonl")
)
PERCENT_ESCAPE = re.compile(r"%(?![0-9a-fA-F]{2})")
TERMINAL_BENCH = re.compile(r"terminal[-_.%/+\s]*bench", re.IGNORECASE)
OPENAI_API_HOST = "api.openai.com"
OPENAI_API_ENDPOINT = None
MAX_OPENAI_REQUESTS = 32
MAX_OPENAI_BODY_BYTES = 16 * 1024 * 1024
OPENAI_RESPONSES_WEBSOCKET_PROTOCOL = "responses_websockets=2026-02-06"
_OPENAI_API_KEY: str | None = None
_OPENAI_REQUEST_COUNT = 0
_OPENAI_WEBSOCKET_PROBE_COUNT = 0


def load(loader: object) -> None:
    """Read the real key once from inherited fd 3; never from argv/env/files."""
    del loader
    global _OPENAI_API_KEY
    try:
        with os.fdopen(3, "rb", closefd=True) as stream:
            raw = stream.read(16 * 1024)
    except OSError as error:
        raise RuntimeError("OpenAI credential pipe is unavailable") from error
    raw = raw.rstrip(b"\r\n")
    if not raw or b"\x00" in raw or len(raw) >= 16 * 1024:
        raise RuntimeError("OpenAI credential pipe was invalid")
    try:
        _OPENAI_API_KEY = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RuntimeError("OpenAI credential was not UTF-8") from error
    raw = b""


def contamination_rule(raw_url: str) -> tuple[str, str, str] | None:
    normalized = normalize_url(raw_url)
    url = urlsplit(normalized)
    host = (url.hostname or "").lower().rstrip(".")
    path_query = f"{url.path}?{url.query}" if url.query else url.path
    lowered = path_query.lower()

    # Search the host and the path separately. A benchmark name in the hostname
    # is a contamination surface, and searching the two fields joined would let
    # a rule match across their boundary.
    def anywhere(needle: str) -> bool:
        return needle in host or needle in lowered

    if host == "r.jina.ai":
        inner = unquote(url.path.lstrip("/"))
        if inner.startswith(("http://", "https://")):
            nested = contamination_rule(inner)
            if nested:
                return (f"jina_recursive:{nested[0]}", host, path_query)

    if anywhere(PINNED_REVISION):
        return ("pinned_revision", host, path_query)
    if host == "tbench.ai" or host.endswith(".tbench.ai"):
        return ("tbench_domain", host, path_query)
    if host == "hub.harborframework.com" and "/tasks/terminal-bench" in lowered:
        return ("harbor_task_registry", host, path_query)
    if benchmark_repository(host, lowered):
        return ("benchmark_repository", host, path_query)
    if public_trajectory_repository(host, lowered):
        return ("public_trajectory", host, path_query)
    if anywhere("patches-terminalbench-"):
        return ("known_patch_artifact", host, path_query)
    if TERMINAL_BENCH.search(host) or TERMINAL_BENCH.search(lowered):
        return ("terminal_bench_url", host, path_query)
    return None


def normalize_url(raw_url: str) -> str:
    value = raw_url.strip()
    if not value:
        raise ValueError("empty URL")
    for _ in range(MAX_DECODE_PASSES):
        if PERCENT_ESCAPE.search(value):
            raise ValueError("malformed percent escape")
        decoded = unquote(value)
        if decoded == value:
            break
        value = decoded
    else:
        raise ValueError("URL exceeded decode limit")
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("unsupported URL")
    return value


def benchmark_repository(host: str, path_query: str) -> bool:
    repositories = (
        "harbor-framework/terminal-bench",
        "terminal-benchmarks/terminal-bench",
        "tbench-ai/terminal-bench",
    )
    return host in {
        "github.com",
        "api.github.com",
        "raw.githubusercontent.com",
        "codeload.github.com",
    } and any(repository in path_query for repository in repositories)


def public_trajectory_repository(host: str, path_query: str) -> bool:
    return (
        host in {"github.com", "api.github.com", "raw.githubusercontent.com", "huggingface.co"}
        and "hqeric/maka-eval-trajectories" in path_query
    )


try:
    from mitmproxy import http
    from mitmproxy.net.http import url as mitm_url
except ImportError:
    http = None
    mitm_url = None

try:
    from mitmproxy.proxy import commands as proxy_commands
    from mitmproxy.proxy.layer import Layer
    from mitmproxy.proxy.layers import ClientTLSLayer, ServerTLSLayer
    from mitmproxy.proxy.layers.tcp import TCPLayer
    from mitmproxy.proxy.layers.tls import HTTP_ALPNS
except ImportError:
    proxy_commands = None
    Layer = object
    ClientTLSLayer = None
    ServerTLSLayer = None
    TCPLayer = None
    HTTP_ALPNS = (b"h3", b"h2", b"http/1.1", b"http/1.0", b"http/0.9")

try:
    from mitmproxy.net.tls import starts_like_tls_record
except ImportError:
    def starts_like_tls_record(data: bytes) -> bool:
        return len(data) >= 3 and data[0] == 0x16 and data[1] == 0x03


def configure(updated: object) -> None:
    # HTTP 101 upgrades construct TCPLayer inside the HTTP layer without
    # another next_layer hook. rawtcp=false makes that path CloseConnection
    # itself; WebSocket upgrades stay on the websocket layer.
    try:
        from mitmproxy import ctx
    except ImportError:
        return
    if getattr(ctx.options, "rawtcp", False):
        ctx.options.rawtcp = False


class Endpoint(NamedTuple):
    host: str
    port: int


class IdentityError(ValueError):
    """A request did not have one unambiguous upstream identity."""


OPENAI_API_ENDPOINT = Endpoint(OPENAI_API_HOST, 443)


def host_token(raw: object) -> str:
    """Strictly validate a host/SNI token and return lower-case ASCII."""
    if (
        not isinstance(raw, str)
        or not raw
        or raw != raw.strip()
        or raw.endswith(".")
        or not raw.isascii()
    ):
        raise IdentityError
    probe = f"[{raw}]" if ":" in raw else raw
    try:
        parsed_host, parsed_port = mitm_url.parse_authority(probe, check=True)
    except (AttributeError, ValueError) as error:
        raise IdentityError from error
    if parsed_port is not None or (probe.startswith("[") and ":" not in parsed_host):
        raise IdentityError
    return parsed_host.lower()


def endpoint(host: object, port: object) -> Endpoint:
    if type(port) is not int or not 1 <= port <= 65535:
        raise IdentityError
    return Endpoint(host_token(host), port)


def authority(raw: object, default_port: int) -> Endpoint:
    if (
        not isinstance(raw, str)
        or not raw
        or raw != raw.strip()
        or "\\" in raw
        or "@" in raw
        or not raw.isascii()
    ):
        raise IdentityError
    try:
        parsed_host, parsed_port = mitm_url.parse_authority(raw, check=True)
    except (AttributeError, ValueError) as error:
        raise IdentityError from error
    if parsed_host.endswith(".") or (raw.startswith("[") and ":" not in parsed_host):
        raise IdentityError
    return endpoint(parsed_host, default_port if parsed_port is None else parsed_port)


def raw_host_values(request: object) -> list[str]:
    try:
        return request.headers.get_all("host")
    except (AttributeError, TypeError) as error:
        raise IdentityError from error


def validate_connect_identity(flow: object) -> Endpoint:
    request = flow.request
    if str(getattr(request, "method", "")).upper() != "CONNECT":
        raise IdentityError
    target = endpoint(getattr(request, "host", None), getattr(request, "port", None))
    if authority(getattr(request, "authority", None), target.port) != target:
        raise IdentityError
    hosts = raw_host_values(request)
    if len(hosts) > 1 or (hosts and authority(hosts[0], target.port) != target):
        raise IdentityError
    return target


def validate_http_identity(flow: object) -> Endpoint:
    request = flow.request
    if str(getattr(request, "method", "")).upper() == "CONNECT":
        raise IdentityError
    scheme = getattr(request, "scheme", None)
    if scheme not in {"http", "https"}:
        raise IdentityError
    default_port = 443 if scheme == "https" else 80
    target = endpoint(getattr(request, "host", None), getattr(request, "port", None))
    hosts = raw_host_values(request)
    is_h2_or_h3 = bool(getattr(request, "is_http2", False) or getattr(request, "is_http3", False))
    retained_authority = getattr(request, "authority", "")
    if is_h2_or_h3:
        if authority(retained_authority, default_port) != target:
            raise IdentityError
        if len(hosts) > 1 or (hosts and authority(hosts[0], default_port) != target):
            raise IdentityError
    else:
        if len(hosts) != 1 or authority(hosts[0], default_port) != target:
            raise IdentityError
        if retained_authority and authority(retained_authority, default_port) != target:
            raise IdentityError
    return target


def require_tls_alignment(flow: object, target: Endpoint) -> None:
    client = getattr(flow, "client_conn", None)
    server = getattr(flow, "server_conn", None)
    address = getattr(server, "address", None)
    if (
        not isinstance(address, (tuple, list))
        or len(address) != 2
        or endpoint(address[0], address[1]) != target
    ):
        raise IdentityError
    if not (
        getattr(client, "tls", False)
        and getattr(client, "tls_established", False)
        and getattr(server, "tls", False)
        and getattr(server, "tls_established", False)
    ):
        raise IdentityError
    if host_token(getattr(client, "sni", None)) != target.host:
        raise IdentityError
    if host_token(getattr(server, "sni", None)) != target.host:
        raise IdentityError


def target_url(request: object, target: Endpoint) -> str:
    scheme = getattr(request, "scheme", "")
    path = getattr(request, "path", "")
    if not isinstance(path, str):
        raise IdentityError
    try:
        return mitm_url.unparse(scheme, target.host, target.port, "" if path == "*" else path)
    except (AttributeError, ValueError) as error:
        raise IdentityError from error


def reject_identity(flow: object) -> None:
    flow.response = http.Response.make(
        421,
        b"Request authority or TLS identity was ambiguous.\n",
        {
            "Content-Type": "text/plain; charset=utf-8",
            "X-Maka-Eval-Egress-Rule": "authority_mismatch",
        },
    )
    append_audit("authority_mismatch", "", "rejected")


def request(flow: object) -> None:
    if str(getattr(flow.request, "method", "")).upper() == "CONNECT":
        http_connect(flow)
        return
    try:
        target = validate_http_identity(flow)
        if getattr(flow.request, "scheme", None) == "https":
            require_tls_alignment(flow, target)
        raw_url = target_url(flow.request, target)
    except IdentityError:
        reject_identity(flow)
        return
    if handle_openai_websocket_probe(flow, target):
        return
    apply_http_policy(flow, raw_url)
    if getattr(flow, "response", None) is None:
        inject_openai_authorization(flow, target)


def strict_json_object(raw: bytes) -> dict[str, object]:
    if not raw or len(raw) > MAX_OPENAI_BODY_BYTES:
        raise ValueError

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for name, value in pairs:
            if name in result:
                raise ValueError
            result[name] = value
        return result

    try:
        parsed = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ValueError from None
    if not isinstance(parsed, dict):
        raise ValueError
    return parsed


def one_header(request: object, name: str) -> str | None:
    try:
        values = request.headers.get_all(name)
    except (AttributeError, TypeError):
        return None
    return values[0] if len(values) == 1 and isinstance(values[0], str) else None


def valid_openai_websocket_probe(request: object, target: Endpoint) -> bool:
    """Recognize Maka's one expected WS probe without attaching a credential."""
    if (
        target != OPENAI_API_ENDPOINT
        or getattr(request, "scheme", None) != "https"
        or str(getattr(request, "method", "")).upper() != "GET"
        or getattr(request, "path", "") != "/v1/responses"
        or getattr(request, "query", None)
        or getattr(request, "raw_content", None) not in (b"", None)
    ):
        return False
    connection = one_header(request, "connection")
    upgrade = one_header(request, "upgrade")
    version = one_header(request, "sec-websocket-version")
    key = one_header(request, "sec-websocket-key")
    beta = one_header(request, "openai-beta")
    authorization = one_header(request, "authorization")
    if not all((connection, upgrade, version, key, beta, authorization)):
        return False
    connection_tokens = {
        token.strip().lower() for token in connection.split(",") if token.strip()
    }
    if (
        connection_tokens != {"upgrade"}
        or upgrade.lower() != "websocket"
        or version != "13"
        or beta != OPENAI_RESPONSES_WEBSOCKET_PROTOCOL
        or not re.fullmatch(r"Bearer maka-decoy-[A-Za-z0-9_-]{8,128}", authorization)
        or request.headers.get_all("api-key")
        or request.headers.get_all("proxy-authorization")
        or request.headers.get_all("openai-project")
        or request.headers.get_all("openai-organization")
    ):
        return False
    try:
        decoded_key = base64.b64decode(key, validate=True)
    except (binascii.Error, ValueError):
        return False
    return len(decoded_key) == 16


def handle_openai_websocket_probe(flow: object, target: Endpoint) -> bool:
    global _OPENAI_WEBSOCKET_PROBE_COUNT
    if not valid_openai_websocket_probe(flow.request, target):
        return False
    if _OPENAI_WEBSOCKET_PROBE_COUNT != 0:
        return False
    try:
        recorded = append_audit("openai_websocket_disabled", "", "sequence=1")
    except OSError:
        recorded = False
    if not recorded:
        flow.response = http.Response.make(
            503,
            b"OpenAI WebSocket fallback audit capacity is unavailable.\n",
            {"X-Maka-Eval-Egress-Rule": "audit_capacity"},
        )
        return True
    _OPENAI_WEBSOCKET_PROBE_COUNT = 1
    flow.response = http.Response.make(
        403,
        b"OpenAI WebSocket transport is disabled for this eval.\n",
        {"X-Maka-Eval-Egress-Rule": "openai_websocket_disabled"},
    )
    return True


def valid_openai_body(request: object) -> bool:
    content_types = request.headers.get_all("content-type")
    if len(content_types) != 1 or content_types[0].split(";", 1)[0].strip().lower() != "application/json":
        return False
    if request.headers.get_all("content-encoding"):
        return False
    raw = getattr(request, "raw_content", None)
    if not isinstance(raw, bytes):
        return False
    try:
        body = strict_json_object(raw)
    except ValueError:
        return False
    common_keys = {
        "model",
        "input",
        "parallel_tool_calls",
        "store",
        "include",
        "reasoning",
        "prompt_cache_key",
        "stream",
    }
    body_keys = set(body)
    has_tools = body_keys == common_keys | {"tools", "tool_choice"}
    if body_keys != common_keys and not has_tools:
        return False
    if contains_hosted_input(body["input"]):
        return False
    include = body.get("include")
    if has_tools:
        tools = body["tools"]
        if not isinstance(tools, list) or not tools or not all(valid_request_tool(tool) for tool in tools):
            return False
        if not valid_tool_choice(body["tool_choice"]):
            return False
    prompt_cache_key = body.get("prompt_cache_key")
    if (
        not isinstance(prompt_cache_key, str)
        or not prompt_cache_key.startswith("maka:")
        or len(prompt_cache_key) > 512
    ):
        return False
    return (
        body.get("model") == "gpt-5.6-sol"
        and body.get("reasoning") == {"effort": "xhigh", "summary": "auto"}
        and body.get("stream") is True
        and body.get("store") is False
        and body.get("parallel_tool_calls") is True
        and include == ["reasoning.encrypted_content"]
    )


def valid_request_tool(tool: object) -> bool:
    if tool == {"type": "apply_patch"}:
        return True
    if not isinstance(tool, dict) or tool.get("type") != "function":
        return False
    if not set(tool).issubset({"type", "name", "description", "parameters", "strict"}):
        return False
    if not isinstance(tool.get("name"), str) or not tool["name"] or len(tool["name"]) > 128:
        return False
    if "description" in tool and not isinstance(tool["description"], str):
        return False
    if not isinstance(tool.get("parameters"), dict):
        return False
    if "strict" in tool and type(tool["strict"]) is not bool:
        return False
    return True


def contains_hosted_input(value: object) -> bool:
    hosted_types = {
        "code_interpreter_call",
        "computer_call",
        "computer_call_output",
        "file_search_call",
        "image_generation_call",
        "input_file",
        "input_image",
        "item_reference",
        "mcp_call",
        "web_search_call",
    }
    if isinstance(value, list):
        return any(contains_hosted_input(item) for item in value)
    if not isinstance(value, dict):
        return False
    input_type = value.get("type")
    if input_type in hosted_types or (
        isinstance(input_type, str) and input_type.startswith("mcp_")
    ):
        return True
    return any(contains_hosted_input(item) for item in value.values())


def valid_tool_choice(choice: object) -> bool:
    if choice is None or (isinstance(choice, str) and choice in {"auto", "none", "required"}):
        return True
    return (
        isinstance(choice, dict)
        and set(choice) == {"type", "name"}
        and choice.get("type") == "function"
        and isinstance(choice.get("name"), str)
        and 0 < len(choice["name"]) <= 128
    )


def inject_openai_authorization(flow: object, target: Endpoint) -> None:
    global _OPENAI_REQUEST_COUNT
    if target.host != OPENAI_API_HOST:
        return
    request = flow.request
    valid = (
        target == OPENAI_API_ENDPOINT
        and getattr(request, "scheme", None) == "https"
        and str(getattr(request, "method", "")).upper() == "POST"
        and getattr(request, "path", "") == "/v1/responses"
        and not getattr(request, "query", None)
        and valid_openai_body(request)
        and _OPENAI_WEBSOCKET_PROBE_COUNT == 1
        and _OPENAI_REQUEST_COUNT < MAX_OPENAI_REQUESTS
    )
    if not valid:
        flow.response = http.Response.make(
            403,
            b"OpenAI credential boundary rejected the request.\n",
            {
                "Content-Type": "text/plain; charset=utf-8",
                "X-Maka-Eval-Egress-Rule": "credential_boundary",
            },
        )
        append_audit("credential_boundary", "", "rejected")
        return
    if not _OPENAI_API_KEY:
        raise RuntimeError("OpenAI credential is unavailable")
    next_sequence = _OPENAI_REQUEST_COUNT + 1
    try:
        audit_recorded = append_audit("openai_authorized", "", f"sequence={next_sequence}")
    except OSError:
        audit_recorded = False
    if not audit_recorded:
        flow.response = http.Response.make(
            503,
            b"OpenAI authorization audit capacity is unavailable.\n",
            {
                "Content-Type": "text/plain; charset=utf-8",
                "X-Maka-Eval-Egress-Rule": "audit_capacity",
            },
        )
        return
    _OPENAI_REQUEST_COUNT = next_sequence
    request.headers.set_all("Authorization", [f"Bearer {_OPENAI_API_KEY}"])
    request.headers.set_all("api-key", [])
    request.headers.set_all("proxy-authorization", [])
    request.headers.set_all("openai-project", [])
    request.headers.set_all("openai-organization", [])


def response(flow: object) -> None:
    response = getattr(flow, "response", None)
    if getattr(response, "status_code", None) != 101:
        return
    # mitmproxy 12.2.3 sets flow.websocket before HttpResponseHook only when
    # the 101 is a real WebSocket upgrade (Upgrade + version 13 + option on).
    # A websocket Upgrade header alone still falls through to CloseConnection
    # under rawtcp=false and must be audited.
    if getattr(flow, "websocket", None) is not None:
        return
    record_raw_tunnel(flow)


def http_connect(flow: object) -> None:
    try:
        target = validate_connect_identity(flow)
        request = flow.request
        scheme = "https" if target.port != 80 else "http"
        raw_url = mitm_url.unparse(scheme, target.host, target.port, "/")
    except (IdentityError, AttributeError, ValueError):
        reject_identity(flow)
        return
    apply_http_policy(flow, raw_url)


def tcp_start(flow: object) -> None:
    # Last resort if a TCPLayer is still admitted (tcp_hosts / ignore).
    # CONNECT raw is closed by next_layer → CloseRawLayer; HTTP 101 raw is
    # closed by rawtcp=false. Neither of those paths starts a TCPLayer.
    record_raw_tunnel(flow)
    kill_flow(flow)


def tcp_message(flow: object) -> None:
    messages = getattr(flow, "messages", None)
    if messages:
        messages[-1].content = b""
    kill_flow(flow)


def next_layer(nextlayer: object) -> None:
    # Script addons run before the built-in classifier assigns layer. If we
    # set CloseRawLayer here, NextLayer leaves it in place. Waiting for
    # isinstance(..., TCPLayer) never fires on the production CONNECT path.
    current = getattr(nextlayer, "layer", None)
    context = getattr(nextlayer, "context", None)
    if current is None:
        # ALPN is authoritative after TLS. HTTP/2 origins can send SETTINGS
        # before the client sends its request; leave that server-first traffic
        # to mitmproxy's built-in HttpLayer classifier.
        client = getattr(context, "client", None)
        if getattr(client, "alpn", None) in HTTP_ALPNS:
            return
        data_client = _next_layer_bytes(nextlayer, "data_client")
        if _is_fragmented_tls_record_prefix(data_client):
            if ClientTLSLayer is None or ServerTLSLayer is None:
                return
            server_tls = ServerTLSLayer(context)
            server_tls.child_layer = ClientTLSLayer(context)
            nextlayer.layer = server_tls
            return
        if not looks_like_raw_tcp(nextlayer):
            return
        record_raw_tunnel(context)
        nextlayer.layer = CloseRawLayer(context)
        return
    if TCPLayer is None or not isinstance(current, TCPLayer):
        return
    record_raw_tunnel(context)
    closer = CloseRawLayer(context)
    replace_layer(context, current, closer)
    nextlayer.layer = closer


def apply_http_policy(flow: object, raw_url: str) -> None:
    if http is None:
        raise RuntimeError("mitmproxy is required to run the Eval egress filter")
    try:
        matched = contamination_rule(raw_url)
        if not matched:
            return
        rule_id, host, normalized_path = matched
        append_audit(rule_id, host, normalized_path)
        flow.response = blocked_response(rule_id)
    except Exception as error:
        flow.response = http.Response.make(
            503,
            b"Eval egress policy could not classify this request.\n",
            {
                "Content-Type": "text/plain; charset=utf-8",
                "X-Maka-Eval-Egress-Rule": "policy_error",
            },
        )
        try:
            append_audit("policy_error", "", type(error).__name__)
        except Exception:
            pass


def looks_like_raw_tcp(nextlayer: object) -> bool:
    """Close only when the bytes cannot still become HTTP or TLS.

    Script next_layer runs before mitmproxy 12.2.3's classifier. Copying its
    `probably_no_http` test here would treat `GET` / `GET ` as raw and assign
    CloseRawLayer while the request line is still arriving. With rawtcp=false
    the built-in path would have kept waiting for HttpLayer.
    """
    data_client = _next_layer_bytes(nextlayer, "data_client")
    data_server = _next_layer_bytes(nextlayer, "data_server")
    if _could_start_tls_record(data_client):
        return False
    if not data_client and not data_server:
        return False
    if data_server or data_client.startswith(b"SSH"):
        return True
    return not _still_could_be_http(data_client)


def _could_start_tls_record(data: bytes) -> bool:
    """Keep a fragmented ClientHello undecided until its 3-byte prefix exists."""
    if starts_like_tls_record(data):
        return True
    return _is_fragmented_tls_record_prefix(data)


def _is_fragmented_tls_record_prefix(data: bytes) -> bool:
    return 0 < len(data) < 3 and b"\x16\x03".startswith(data)


def _still_could_be_http(data: bytes) -> bool:
    first_line, newline, _rest = data.partition(b"\n")
    line = first_line.rstrip(b"\r")
    method, space, _remainder = line.partition(b" ")
    if not method.isascii() or not method.isalpha():
        return False
    if newline and not space:
        return False
    return True


def _next_layer_bytes(nextlayer: object, name: str) -> bytes:
    getter = getattr(nextlayer, name, None)
    if not callable(getter):
        return b""
    try:
        data = getter()
    except Exception:
        return b""
    return bytes(data) if isinstance(data, (bytes, bytearray)) else b""


def tcp_peer(flow: object) -> tuple[str, str]:
    server = getattr(flow, "server_conn", None) or getattr(flow, "server", None)
    address = getattr(server, "address", None) if server is not None else None
    if isinstance(address, (tuple, list)) and address:
        host = str(address[0])[:255]
        port = address[1] if len(address) > 1 else ""
        return host, f":{port}" if port != "" else ""
    return "", ""


def record_raw_tunnel(flow: object) -> None:
    host, path = tcp_peer(flow)
    try:
        append_audit("raw_tunnel", host, path)
    except Exception:
        pass


def kill_flow(flow: object) -> None:
    kill = getattr(flow, "kill", None)
    if callable(kill) and getattr(flow, "killable", True):
        try:
            kill()
        except Exception:
            pass


def replace_layer(context: object, current: object, closer: object) -> None:
    layers = getattr(context, "layers", None)
    if not isinstance(layers, list):
        return
    try:
        index = layers.index(current)
    except ValueError:
        index = len(layers)
    if closer in layers:
        layers.remove(closer)
    if current in layers:
        layers.remove(current)
    layers.insert(min(index, len(layers)), closer)


class CloseRawLayer(Layer):
    def __init__(self, context: object) -> None:
        if Layer is object:
            self.context = context
            return
        if getattr(context, "layers", None) is None:
            context.layers = []
        if getattr(context, "options", None) is None:
            context.options = type("Options", (), {"proxy_debug": False})()
        super().__init__(context)

    def handle_event(self, event: object):
        if proxy_commands is None:
            return
            yield
        for name in ("client", "server"):
            connection = getattr(self.context, name, None)
            if connection is not None:
                yield proxy_commands.CloseConnection(connection)


def blocked_response(rule_id: str):
    return http.Response.make(
        451,
        b"Benchmark source or public solution access is blocked during evaluation.\n",
        {
            "Content-Type": "text/plain; charset=utf-8",
            "X-Maka-Eval-Egress-Rule": rule_id,
        },
    )


def append_audit(rule_id: str, host: str, normalized_path: str) -> bool:
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    if AUDIT_PATH.exists() and AUDIT_PATH.stat().st_size >= MAX_AUDIT_BYTES:
        write_truncation_marker()
        return False
    record = {
        "ts": int(time.time() * 1000),
        "ruleId": rule_id,
        "host": host[:255],
        "normalizedPath": normalized_path[:4096],
    }
    with AUDIT_PATH.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n")
    return True


def write_truncation_marker() -> None:
    if audit_already_truncated():
        return
    record = {
        "ts": int(time.time() * 1000),
        "ruleId": "audit_truncated",
        "host": "",
        "normalizedPath": "",
    }
    prefix = ""
    if AUDIT_PATH.exists() and AUDIT_PATH.stat().st_size > 0:
        with AUDIT_PATH.open("rb") as stream:
            stream.seek(-1, os.SEEK_END)
            if stream.read(1) != b"\n":
                prefix = "\n"
    with AUDIT_PATH.open("a", encoding="utf-8") as stream:
        stream.write(prefix + json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n")


def audit_already_truncated() -> bool:
    if not AUDIT_PATH.exists():
        return False
    with AUDIT_PATH.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(max(0, size - 4096))
        tail = stream.read().decode("utf-8", errors="ignore")
    lines = [line for line in tail.splitlines() if line.strip()]
    if not lines:
        return False
    try:
        parsed = json.loads(lines[-1])
    except json.JSONDecodeError:
        return False
    return isinstance(parsed, dict) and parsed.get("ruleId") == "audit_truncated"
