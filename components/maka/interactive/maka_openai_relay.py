#!/usr/bin/env python3
"""A loopback-only, fail-closed OpenAI relay for interactive Maka.

The real provider credential is read from macOS Keychain only after a request
has passed the local caller and request-shape checks.  The process accepts no
credential through argv, environment variables, or files.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import datetime as dt
import hashlib
import hmac
import http.client
import http.server
import json
import math
import os
import pathlib
import re
import resource
import secrets
import signal
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from typing import Callable, Mapping, Protocol
from urllib.parse import urlsplit

SCRIPTS_ROOT = pathlib.Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from harden_python_runtime import RuntimeHardeningError, assert_current_runtime
with contextlib.suppress(ValueError):
    sys.path.remove(str(SCRIPTS_ROOT))


LOOPBACK_HOST = "127.0.0.1"
DEFAULT_PORT = 48173
MODEL_ID = "gpt-6.1-sol"
LEGACY_MODEL_ID = "gpt-5.6-sol"
PREVIOUS_MODEL_ID = "gpt-6-sol"
MODEL_IDS = (MODEL_ID, PREVIOUS_MODEL_ID, LEGACY_MODEL_ID)
PROXY_USERNAME = "maka-local"
OPENAI_HOST = "api.openai.com"
OPENAI_PORT = 443
OPENAI_PATH = "/v1/responses"
HEALTH_PATH = "/healthz"
HEALTH_CHALLENGE_HEADER = "X-Maka-Relay-Challenge"
SYSTEM_CA_BUNDLE = pathlib.Path("/etc/ssl/cert.pem")
MAX_REQUEST_BYTES = 16 * 1024 * 1024
MAX_UPSTREAM_HEADER_BYTES = 16 * 1024
KEYCHAIN_SERVICE = "Codex Auth"
RELAY_READY_PREFIX = b"maka-api-key-relay-ready-v2\nkeychain-account-sha256="
RELAY_SOURCE_SHA256 = hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest()

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43,256}$")
_KEYCHAIN_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@._+|:-]{0,127}$")
_HEALTH_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_PROMPT_CACHE_KEY_RE = re.compile(r"^maka:[A-Za-z0-9._:-]{1,240}$")
_JUDGE_MARKER_RE = re.compile(
    r"^Judge receipt nonce: (maka:fanout-judge-[0-9a-f]{32})$"
)
_ROUTING_MARKER_RE = re.compile(
    r"^(?:Fanout routing receipt nonce: "
    r"(maka:fanout-routing-[0-9a-f]{32}(?:-[0-9a-f]{32})?)"
    r"|Receipt nonce: (maka:probe-[0-9a-f]{32}-[0-9a-f]{32}))$"
)
_ROUTING_CACHE_KEY_RE = re.compile(r"^maka:fanout-routing-[0-9a-f]{32}$")
_PINNED_ROUTING_MARKER_RE = re.compile(
    r"^maka:(?:fanout-routing|probe)-[0-9a-f]{32}-([0-9a-f]{32})$"
)
_PINNED_ROUTING_LINE_PREFIX = "Fanout routing receipt nonce: maka:fanout-routing-"
_NEUTRAL_ROUTING_LINE_PREFIX = "Receipt nonce: maka:probe-"
_RESPONSE_ID_RE = re.compile(r"^resp_[A-Za-z0-9_-]{1,240}$")
_HEALTH_PROOF_CONTEXT = b"maka-relay-health-v1\x00"
_REQUIRED_BODY_KEYS = frozenset(
    {
        "model",
        "input",
        "parallel_tool_calls",
        "store",
        "include",
        "reasoning",
        "stream",
    }
)
_OPTIONAL_BODY_KEYS = frozenset({
    "tools", "tool_choice", "prompt_cache_key", "previous_response_id",
})
_ALLOWED_TOOL_TYPES = frozenset({"function", "apply_patch"})
_UPSTREAM_RESPONSE_HEADERS = frozenset(
    {
        "content-type",
        "openai-processing-ms",
        "openai-version",
        "x-request-id",
    }
)


class RelayConfigurationError(RuntimeError):
    """The local relay configuration is unsafe or incomplete."""


class ProviderCredentialError(RuntimeError):
    """The Keychain credential could not be loaded or decoded."""


class _DuplicateJsonKey(ValueError):
    pass


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> object:
    raise ValueError("non-finite JSON number")


def _strict_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("non-finite JSON number")
    return parsed


def health_challenge_proof(attestation: str, challenge: str) -> str:
    """Prove possession of the startup secret without disclosing it."""

    if not _TOKEN_RE.fullmatch(attestation) or not _HEALTH_CHALLENGE_RE.fullmatch(
        challenge
    ):
        raise RelayConfigurationError("relay health challenge is malformed")
    return hmac.digest(
        attestation.encode("ascii"),
        _HEALTH_PROOF_CONTEXT + challenge.encode("ascii"),
        "sha256",
    ).hex()


def read_private_token(path: pathlib.Path) -> str:
    """Read an owner-only caller token without following a symlink."""

    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise RelayConfigurationError("caller token is unavailable") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RelayConfigurationError("caller token is not a regular file")
        if metadata.st_uid != os.getuid():
            raise RelayConfigurationError("caller token has the wrong owner")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise RelayConfigurationError("caller token permissions are too broad")
        raw = os.read(descriptor, 1025)
        if len(raw) > 1024 or os.read(descriptor, 1):
            raise RelayConfigurationError("caller token is too large")
    finally:
        os.close(descriptor)
    try:
        token = raw.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise RelayConfigurationError("caller token is malformed") from error
    if not _TOKEN_RE.fullmatch(token):
        raise RelayConfigurationError("caller token is malformed")
    return token


def default_keychain_account_path(home: pathlib.Path | None = None) -> pathlib.Path:
    return (
        (home or pathlib.Path.home())
        / ".config"
        / "khenrix-utils"
        / "maka"
        / "maka-openai-keychain-account"
    )


def default_relay_ready_path(home: pathlib.Path | None = None) -> pathlib.Path:
    return (
        (home or pathlib.Path.home())
        / ".config"
        / "khenrix-utils"
        / "maka"
        / "maka-api-key-relay-ready"
    )


def _read_owner_only_payload(path: pathlib.Path, limit: int, label: str) -> bytes:
    try:
        parent = path.parent.lstat()
    except OSError as error:
        raise RelayConfigurationError(f"{label} is unavailable") from error
    if (
        not stat.S_ISDIR(parent.st_mode)
        or stat.S_ISLNK(parent.st_mode)
        or parent.st_uid != os.getuid()
        or stat.S_IMODE(parent.st_mode) & 0o077
    ):
        raise RelayConfigurationError(f"{label} directory is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise RelayConfigurationError(f"{label} is unavailable") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RelayConfigurationError(f"{label} is not a regular file")
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise RelayConfigurationError(f"{label} must be owner-only")
        raw = os.read(descriptor, limit)
        if os.read(descriptor, 1):
            raise RelayConfigurationError(f"{label} is too large")
    finally:
        os.close(descriptor)
    return raw


def read_keychain_account(path: pathlib.Path) -> str:
    """Read one explicit owner-only account ID; never enumerate Keychain."""

    raw = _read_owner_only_payload(path, 256, "Keychain account selector")
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError as error:
        raise RelayConfigurationError("Keychain account selector is invalid") from error
    if not text.endswith("\n"):
        raise RelayConfigurationError("Keychain account selector is invalid")
    account = text.removesuffix("\n")
    if not _KEYCHAIN_ACCOUNT_RE.fullmatch(account) or raw != f"{account}\n".encode("ascii"):
        raise RelayConfigurationError("Keychain account selector is invalid")
    return account


def relay_ready_payload(account: str) -> bytes:
    account_payload = f"{account}\n".encode("ascii")
    return RELAY_READY_PREFIX + hashlib.sha256(account_payload).hexdigest().encode("ascii") + b"\n"


def require_account_readiness(
    account: str,
    account_file: pathlib.Path,
    ready_file: pathlib.Path,
) -> None:
    """Prevent a restarted or long-lived relay from following an unverified selector change."""

    if read_keychain_account(account_file) != account:
        raise ProviderCredentialError("Keychain account selection changed after relay start")
    marker = _read_owner_only_payload(ready_file, 256, "Maka relay readiness marker")
    if not hmac.compare_digest(marker, relay_ready_payload(account)):
        raise ProviderCredentialError("Maka relay readiness does not match the selected account")


def parse_codex_auth_document(raw: bytes) -> str:
    """Extract the provider key from the existing Keychain JSON payload."""

    if len(raw) > 1024 * 1024:
        raise ProviderCredentialError("Keychain credential payload is too large")
    try:
        document = json.loads(
            raw,
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_json_constant,
            parse_float=_strict_json_float,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as error:
        raise ProviderCredentialError("Keychain credential payload is invalid") from error
    if not isinstance(document, dict):
        raise ProviderCredentialError("Keychain credential payload is invalid")
    key = document.get("OPENAI_API_KEY")
    if not isinstance(key, str) or not key or len(key) > 64 * 1024:
        raise ProviderCredentialError("OpenAI credential is unavailable in Keychain")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in key):
        raise ProviderCredentialError("OpenAI credential is malformed")
    return key


class KeychainOpenAIKey:
    """Load the real provider key through a private pipe for one request."""

    def __init__(
        self,
        account: str,
        account_file: pathlib.Path | None = None,
        ready_file: pathlib.Path | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        if not _KEYCHAIN_ACCOUNT_RE.fullmatch(account):
            raise RelayConfigurationError("Keychain account selector is invalid")
        if (account_file is None) != (ready_file is None):
            raise RelayConfigurationError("Keychain readiness paths are incomplete")
        self._account = account
        self._account_file = account_file
        self._ready_file = ready_file
        self._timeout_seconds = timeout_seconds

    def __call__(self) -> str:
        if self._account_file is not None and self._ready_file is not None:
            require_account_readiness(self._account, self._account_file, self._ready_file)
        try:
            completed = subprocess.run(
                [
                    "/usr/bin/security",
                    "find-generic-password",
                    "-s",
                    KEYCHAIN_SERVICE,
                    "-a",
                    self._account,
                    "-w",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=self._timeout_seconds,
                close_fds=True,
                env={"PATH": "/usr/bin:/bin", "LANG": "C"},
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ProviderCredentialError("Keychain credential lookup failed") from error
        if completed.returncode != 0:
            raise ProviderCredentialError("Keychain credential lookup failed")
        return parse_codex_auth_document(completed.stdout)


class UpstreamResponse(Protocol):
    status: int

    def getheader(self, name: str, default: str | None = None) -> str | None: ...

    def read(self, amount: int | None = None) -> bytes: ...

    def read1(self, amount: int = -1) -> bytes: ...


class UpstreamExchange(Protocol):
    response: UpstreamResponse

    def close(self) -> None: ...


class Forwarder(Protocol):
    def forward(self, body: bytes, provider_key: str) -> UpstreamExchange: ...


@dataclass
class _HttpClientExchange:
    response: http.client.HTTPResponse
    connection: http.client.HTTPSConnection

    def close(self) -> None:
        self.connection.close()


class OpenAIForwarder:
    """Forward an admitted request to one hard-coded TLS origin and path."""

    def __init__(self, timeout_seconds: float = 300.0) -> None:
        self._timeout_seconds = timeout_seconds
        metadata = SYSTEM_CA_BUNDLE.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise RelayConfigurationError("system CA bundle is unsafe")
        # Pin the root-owned macOS CA bundle explicitly. This avoids both
        # ambient SSL_CERT_* overrides and python.org builds whose compiled
        # default CA path is absent on this machine.
        self._tls_context = ssl.create_default_context(cafile=str(SYSTEM_CA_BUNDLE))
        # Never honor Python's TLS key logging hook, even if this class is
        # instantiated outside the isolated LaunchAgent environment.
        with contextlib.suppress(AttributeError):
            self._tls_context.keylog_filename = None

    def forward(self, body: bytes, provider_key: str) -> _HttpClientExchange:
        connection = http.client.HTTPSConnection(
            OPENAI_HOST,
            OPENAI_PORT,
            timeout=self._timeout_seconds,
            context=self._tls_context,
        )
        try:
            connection.request(
                "POST",
                OPENAI_PATH,
                body=body,
                headers={
                    "Authorization": f"Bearer {provider_key}",
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream, application/json",
                    "Connection": "close",
                },
            )
            return _HttpClientExchange(connection.getresponse(), connection)
        except BaseException:
            connection.close()
            raise


@dataclass(frozen=True)
class RelayDependencies:
    caller_token: str
    attestation: str
    key_loader: Callable[[], str]
    forwarder: Forwarder
    telemetry_path: pathlib.Path | None = None


class TierObserver:
    """Keep bounded SSE metadata for the initial policy gate and receipt."""

    MAX_LINE = 16 * 1024
    MAX_PREFIX = 64 * 1024
    TIERS = frozenset({"default", "flex", "priority", "scale", "auto"})

    def __init__(self) -> None:
        self.line = bytearray()
        self.dropping = False
        self.tier: str | None = None
        self.model: str | None = None
        self.first_data_seen = False
        self.created_model: str | None = None
        self.created_tier: str | None = None
        self.created_response_id: str | None = None
        self.reported_model: str | None = None
        self.reported_tier: str | None = None

    def feed(self, chunk: bytes) -> None:
        for byte in chunk:
            if byte == 10:
                if not self.dropping:
                    self._line(bytes(self.line).rstrip(b"\r"))
                self.line.clear()
                self.dropping = False
            elif not self.dropping:
                if len(self.line) < self.MAX_LINE:
                    self.line.append(byte)
                else:
                    self.line.clear()
                    self.dropping = True

    def _line(self, line: bytes) -> None:
        if not line.startswith(b"data: "):
            return
        first_data = not self.first_data_seen
        self.first_data_seen = True
        try:
            event = json.loads(line[6:])
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return
        if not isinstance(event, dict) or event.get("type") not in {
            "response.created", "response.completed"
        }:
            return
        response = event.get("response")
        if not isinstance(response, dict):
            return
        tier = response.get("service_tier")
        model = response.get("model")
        if first_data and event["type"] == "response.created":
            self.created_tier = tier if isinstance(tier, str) else None
            self.created_model = model if isinstance(model, str) else None
            response_id = response.get("id")
            self.created_response_id = (
                response_id if isinstance(response_id, str)
                and _RESPONSE_ID_RE.fullmatch(response_id) else None
            )
        self.reported_tier = tier if isinstance(tier, str) else None
        self.reported_model = model if isinstance(model, str) else None
        self.tier = tier if isinstance(tier, str) and tier in self.TIERS else None
        self.model = model if isinstance(model, str) and model in MODEL_IDS else None


class LoopbackThreadingHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    # SO_REUSEADDR permits a freshly restarted listener to reclaim the fixed
    # port from its own TIME_WAIT sockets. Python does not enable SO_REUSEPORT,
    # so a second live listener still cannot share the port.
    allow_reuse_address = True
    request_queue_size = 32

    def __init__(self, server_address: tuple[str, int],
                 handler_class: type[http.server.BaseHTTPRequestHandler]) -> None:
        super().__init__(server_address, handler_class)
        self.routing_chains: dict[str, tuple[str, str, str | None]] = {}
        self.routing_chain_lock = threading.Lock()

    def handle_error(self, _request: object, _client_address: object) -> None:
        # Socketserver's default prints tracebacks. Keep all request handling
        # failures out of LaunchAgent logs and fail the individual socket closed.
        return


class RelayHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "maka-local-relay"
    sys_version = ""

    @property
    def dependencies(self) -> RelayDependencies:
        return self.server.relay_dependencies  # type: ignore[attr-defined,no-any-return]

    def log_message(self, _format: str, *_arguments: object) -> None:
        # Request paths, headers, bodies, and provider errors must not reach logs.
        return

    def version_string(self) -> str:
        return self.server_version

    def do_CONNECT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self.close_connection = True
        if not self._proxy_authorized():
            self._reject(407, "proxy_auth_required", proxy_auth=True)
            return
        self._reject(403, "proxy_destination_denied")

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self._is_absolute_form():
            self._deny_forward_proxy()
            return
        parsed = urlsplit(self.path)
        if parsed.path == HEALTH_PATH and not parsed.query and not parsed.fragment:
            hosts = self.headers.get_all("Host", failobj=[])
            expected_host = f"{LOOPBACK_HOST}:{self.server.server_address[1]}"
            if len(hosts) != 1 or hosts[0] != expected_host:
                self._reject(403, "host_denied")
                return
            challenges = self.headers.get_all(HEALTH_CHALLENGE_HEADER, failobj=[])
            if len(challenges) != 1 or not _HEALTH_CHALLENGE_RE.fullmatch(challenges[0]):
                self._reject(400, "health_challenge_required")
                return
            self._send_json(
                200,
                {
                    "status": "ready",
                    "proof": health_challenge_proof(
                        self.dependencies.attestation, challenges[0]
                    ),
                },
            )
            return
        if not self._origin_request_admitted():
            return
        if parsed.query or parsed.fragment:
            self._reject(403, "query_denied")
            return
        if parsed.path == "/v1/models":
            self._send_json(
                200,
                {
                    "object": "list",
                    "data": [
                        {"id": model, "object": "model", "created": 0,
                         "owned_by": "maka-local-relay"}
                        for model in MODEL_IDS
                    ],
                },
            )
            return
        if parsed.path == OPENAI_PATH:
            # The pinned Maka adapter treats this response as the signal to use
            # its audited HTTP Responses fallback.
            self._reject(403, "websocket_disabled")
            return
        self._reject(404, "endpoint_denied")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self._is_absolute_form():
            self._deny_forward_proxy()
            return
        if not self._origin_request_admitted():
            return
        parsed = urlsplit(self.path)
        if parsed.path != OPENAI_PATH or parsed.query or parsed.fragment:
            self._reject(404, "endpoint_denied")
            return
        body = self._read_json_body()
        if body is None:
            return
        rejection = responses_body_rejection(body)
        if rejection is not None:
            # The suffix is a fixed schema category and never contains request
            # content. It keeps fail-closed compatibility failures diagnosable.
            self._reject(403, f"request_shape_denied_{rejection}")
            return
        body = enforce_outbound_policy(body)
        request_metadata = json.loads(body)
        if judge_request_marker(request_metadata) is not None:
            # The pinned Host still leaks Read into this profile. The local
            # relay is the last authority before the provider tool boundary.
            request_metadata.pop("tools", None)
            request_metadata["tool_choice"] = "none"
            body = json.dumps(
                request_metadata, separators=(",", ":"), ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        cache_key = request_metadata.get("prompt_cache_key")
        evidence = routing_request_evidence(request_metadata)
        pinned_cache_key = (isinstance(cache_key, str)
                            and _ROUTING_CACHE_KEY_RE.fullmatch(cache_key) is not None)
        pinned_marker = (evidence is not None
                         and _PINNED_ROUTING_MARKER_RE.fullmatch(evidence[0]) is not None)
        previous_id = request_metadata.get("previous_response_id")
        prior = None
        if isinstance(previous_id, str):
            with self.server.routing_chain_lock:  # type: ignore[attr-defined]
                prior = self.server.routing_chains.get(previous_id)  # type: ignore[attr-defined]
        chain_attributed = (prior is not None and prior[2] == cache_key
                            and (pinned_cache_key
                                 or _PINNED_ROUTING_MARKER_RE.fullmatch(prior[0]) is not None))
        session_bound = (pinned_marker or (chain_attributed and prior is not None
                         and _PINNED_ROUTING_MARKER_RE.fullmatch(prior[0]) is not None))
        skill_tools = routing_skill_tools(request_metadata)
        if (pinned_cache_key or pinned_marker or chain_attributed
                or routing_pinned_marker_prefix_seen(request_metadata)
                or (cache_key is None and skill_tools is not None)):
            attributed = (pinned_marker or (pinned_cache_key and evidence is not None)
                          or chain_attributed)
            zero_tool_finalization = (session_bound and "tools" not in request_metadata
                                      and "tool_choice" not in request_metadata)
            if self.dependencies.telemetry_path is not None:
                diagnostic = {
                    "schema": "khenrix-maka-routing-shape-v1",
                    "user_content_shape": routing_user_content_shape(request_metadata),
                    "nonce_seen": evidence is not None,
                    "pinned_cache_key": pinned_cache_key,
                }
                if attributed and skill_tools is None and not zero_tool_finalization:
                    diagnostic["inbound_tools"] = routing_inbound_tool_shape(request_metadata)
                with contextlib.suppress(OSError, RelayConfigurationError):
                    _write_private_value(
                        self.dependencies.telemetry_path.parent / "relay-routing-last-shape.json",
                        json.dumps(diagnostic, sort_keys=True),
                    )
            if not attributed:
                self._reject(403, "routing_attribution_denied")
                return
            if skill_tools is None:
                if not zero_tool_finalization:
                    self._reject(403, "routing_tools_denied")
                    return
                # The compatible SDK omits both fields on Maka's final step.
                # Make the outbound tool-free boundary explicit to the provider.
                request_metadata["tool_choice"] = "none"
            else:
                request_metadata["tools"] = [skill_tools["Skill"], skill_tools["SkillSearch"]]
            body = json.dumps(
                request_metadata, separators=(",", ":"), ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        requested_model = request_metadata["model"]
        try:
            provider_key = self.dependencies.key_loader()
        except Exception:
            self._reject(503, "provider_credential_unavailable")
            return
        try:
            exchange = self.dependencies.forwarder.forward(body, provider_key)
        except Exception:
            self._reject(502, "provider_unavailable")
            return
        finally:
            provider_key = ""
        try:
            self._relay_upstream(exchange.response, request_metadata)
        finally:
            exchange.close()

    def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._method_denied()

    def do_PUT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._method_denied()

    def do_PATCH(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._method_denied()

    def do_DELETE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._method_denied()

    def do_OPTIONS(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._method_denied()

    def _method_denied(self) -> None:
        self.close_connection = True
        self._reject(405, "method_denied", extra_headers={"Allow": "GET, POST"})

    def _is_absolute_form(self) -> bool:
        parsed = urlsplit(self.path)
        return bool(parsed.scheme or parsed.netloc)

    def _deny_forward_proxy(self) -> None:
        self.close_connection = True
        if not self._proxy_authorized():
            self._reject(407, "proxy_auth_required", proxy_auth=True)
            return
        self._reject(403, "proxy_destination_denied")

    def _origin_request_admitted(self) -> bool:
        self.close_connection = True
        hosts = self.headers.get_all("Host", failobj=[])
        expected_host = f"{LOOPBACK_HOST}:{self.server.server_address[1]}"
        if len(hosts) != 1 or hosts[0] != expected_host:
            self._reject(403, "host_denied")
            return False
        values = self.headers.get_all("Authorization", failobj=[])
        expected = f"Bearer {self.dependencies.caller_token}"
        if len(values) != 1 or not hmac.compare_digest(values[0], expected):
            self._reject(401, "caller_auth_required", bearer_auth=True)
            return False
        return True

    def _proxy_authorized(self) -> bool:
        values = self.headers.get_all("Proxy-Authorization", failobj=[])
        expected_value = base64.b64encode(
            f"{PROXY_USERNAME}:{self.dependencies.caller_token}".encode("ascii")
        ).decode("ascii")
        expected = f"Basic {expected_value}"
        return len(values) == 1 and hmac.compare_digest(values[0], expected)

    def _read_json_body(self) -> bytes | None:
        if self.headers.get_all("Transfer-Encoding", failobj=[]):
            self._reject(400, "transfer_encoding_denied")
            return None
        lengths = self.headers.get_all("Content-Length", failobj=[])
        if len(lengths) != 1 or not lengths[0].isdigit():
            self._reject(411, "content_length_required")
            return None
        length = int(lengths[0])
        if length < 2 or length > MAX_REQUEST_BYTES:
            self._reject(413, "request_too_large")
            return None
        content_types = self.headers.get_all("Content-Type", failobj=[])
        if len(content_types) != 1:
            self._reject(415, "content_type_denied")
            return None
        media_type, separator, charset = content_types[0].lower().partition(";")
        if media_type.strip() != "application/json" or (
            separator and charset.strip() not in {"charset=utf-8", "charset=\"utf-8\""}
        ):
            self._reject(415, "content_type_denied")
            return None
        body = self.rfile.read(length)
        if len(body) != length:
            self._reject(400, "incomplete_body")
            return None
        return body

    def _relay_upstream(self, response: UpstreamResponse,
                        request_metadata: dict[str, object]) -> None:
        requested_model = request_metadata["model"]
        status = response.status
        if not isinstance(status, int) or status < 200 or status > 599:
            self._reject(502, "provider_response_invalid")
            return
        forwarded_headers: list[tuple[str, str]] = []
        total_header_bytes = 0
        for name in sorted(_UPSTREAM_RESPONSE_HEADERS):
            value = response.getheader(name)
            if value is None:
                continue
            if any(ord(character) < 0x20 or ord(character) > 0x7E for character in value):
                self._reject(502, "provider_response_invalid")
                return
            total_header_bytes += len(name.encode("ascii")) + len(value.encode("utf-8"))
            if total_header_bytes > MAX_UPSTREAM_HEADER_BYTES:
                self._reject(502, "provider_response_invalid")
                return
            forwarded_headers.append((name, value))
        observer = TierObserver()
        prefix = bytearray()
        if status < 300:
            while not observer.first_data_seen:
                try:
                    chunk = response.read1(64 * 1024)
                except (OSError, http.client.HTTPException):
                    self._reject(502, "provider_response_invalid")
                    return
                if not chunk or len(prefix) + len(chunk) > observer.MAX_PREFIX:
                    self._reject(502, "provider_policy_mismatch")
                    self._record_tier_status(request_metadata, observer, 502)
                    return
                prefix.extend(chunk)
                observer.feed(chunk)
            if (observer.created_model != requested_model or
                    observer.created_tier != "default" or
                    observer.reported_model != requested_model or
                    observer.reported_tier != "default"):
                self._reject(502, "provider_policy_mismatch")
                self._record_tier_status(request_metadata, observer, 502)
                return
        self.send_response(status)
        for name, value in forwarded_headers:
            self.send_header(name, value)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        if prefix:
            try:
                self.wfile.write(prefix)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                self._record_tier_status(request_metadata, observer, status)
                return
        while True:
            # HTTPResponse.read(amount) may buffer a chunked SSE response until
            # `amount` bytes or EOF. read1 returns currently available bytes so
            # model output and tool calls reach Maka incrementally.
            chunk = response.read1(64 * 1024)
            if not chunk:
                break
            observer.feed(chunk)
            if (observer.reported_model != requested_model or
                    observer.reported_tier != "default"):
                break
            try:
                self.wfile.write(chunk)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                break
        self._record_tier_status(request_metadata, observer, status)

    def _record_tier_status(self, request_metadata: dict[str, object],
                            observer: TierObserver, status: int) -> None:
        path = self.dependencies.telemetry_path
        if path is not None:
            receipt = {
                "schema": "khenrix-maka-relay-tier-v1",
                "time_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                "requested_model": request_metadata["model"],
                "observed_model": observer.model,
                "requested_service_tier": "default",
                "observed_service_tier": observer.tier,
                "http_status": status,
            }
            with contextlib.suppress(OSError, RelayConfigurationError):
                _write_private_value(path, json.dumps(receipt, sort_keys=True))
            evidence = judge_request_evidence(request_metadata)
            if evidence is not None:
                marker, prompt_sha256 = evidence
                digest = hashlib.sha256(marker.encode("ascii")).hexdigest()
                receipt_root = path.parent / "relay-judge-receipts"
                judge_receipt = {
                    **receipt,
                    "schema": "khenrix-maka-judge-request-v1",
                    "request_marker_sha256": digest,
                    "prompt_sha256": prompt_sha256,
                    "relay_source_sha256": RELAY_SOURCE_SHA256,
                    "outbound_effort": request_metadata["reasoning"]["effort"],
                    "tool_count": len(request_metadata.get("tools", [])),
                }
                with contextlib.suppress(OSError, RelayConfigurationError):
                    receipt_root.mkdir(mode=0o700, exist_ok=True)
                    _private_directory(receipt_root)
                    receipt_path = receipt_root / f"{digest}-{secrets.token_hex(16)}.json"
                    _write_private_value(receipt_path, json.dumps(judge_receipt, sort_keys=True))
            routing_evidence = routing_request_evidence(request_metadata)
            binding = "nonce"
            cache_key = request_metadata.get("prompt_cache_key")
            previous_id = request_metadata.get("previous_response_id")
            if routing_evidence is None and isinstance(previous_id, str):
                with self.server.routing_chain_lock:  # type: ignore[attr-defined]
                    prior = self.server.routing_chains.get(previous_id)  # type: ignore[attr-defined]
                if prior is not None and prior[2] == cache_key:
                    routing_evidence = prior[:2]
                    binding = "response-chain"
            pinned_marker_match = (
                _PINNED_ROUTING_MARKER_RE.fullmatch(routing_evidence[0])
                if routing_evidence is not None else None
            )
            if routing_evidence is not None and (
                isinstance(cache_key, str) or pinned_marker_match is not None
            ):
                marker, prompt_sha256 = routing_evidence
                digest = hashlib.sha256(marker.encode("ascii")).hexdigest()
                instance = secrets.token_hex(16)
                receipt_root = path.parent / "relay-routing-receipts"
                routing_receipt = {
                    **receipt,
                    "schema": "khenrix-maka-routing-request-v1",
                    "request_marker_sha256": digest,
                    "request_instance_id": instance,
                    "prompt_sha256": prompt_sha256,
                    "binding": binding,
                    "relay_source_sha256": RELAY_SOURCE_SHA256,
                    "outbound_effort": request_metadata["reasoning"]["effort"],
                }
                if isinstance(cache_key, str):
                    routing_receipt["prompt_cache_key"] = cache_key
                if pinned_marker_match is not None:
                    routing_receipt["routing_session_id"] = (
                        f"fanout-routing-{pinned_marker_match.group(1)}"
                    )
                if (pinned_marker_match is not None or
                        (isinstance(cache_key, str)
                         and _ROUTING_CACHE_KEY_RE.fullmatch(cache_key))):
                    outbound_tools = request_metadata.get("tools", [])
                    routing_receipt["outbound_tool_names"] = [
                        tool["name"] for tool in outbound_tools
                    ]
                    routing_receipt["tool_count"] = len(outbound_tools)
                    routing_receipt["outbound_tool_choice"] = request_metadata["tool_choice"]
                if isinstance(previous_id, str):
                    routing_receipt["previous_response_id_sha256"] = hashlib.sha256(
                        previous_id.encode("ascii")
                    ).hexdigest()
                if observer.created_response_id is not None:
                    routing_receipt["response_id_sha256"] = hashlib.sha256(
                        observer.created_response_id.encode("ascii")
                    ).hexdigest()
                with contextlib.suppress(OSError, RelayConfigurationError):
                    receipt_root.mkdir(mode=0o700, exist_ok=True)
                    _private_directory(receipt_root)
                    receipt_path = receipt_root / f"{digest}-{instance}.json"
                    _write_private_value(receipt_path, json.dumps(routing_receipt, sort_keys=True))
                    if (observer.created_response_id is not None and status == 200
                            and observer.model == request_metadata["model"]
                            and observer.tier == "default"):
                        with self.server.routing_chain_lock:  # type: ignore[attr-defined]
                            chains = self.server.routing_chains  # type: ignore[attr-defined]
                            chains[observer.created_response_id] = (
                                marker, prompt_sha256, cache_key
                            )
                            if len(chains) > 256:
                                del chains[next(iter(chains))]

    def _send_json(self, status: int, document: Mapping[str, object]) -> None:
        encoded = json.dumps(document, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(encoded)

    def _reject(
        self,
        status: int,
        code: str,
        *,
        bearer_auth: bool = False,
        proxy_auth: bool = False,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        encoded = json.dumps(
            {"error": {"type": "local_relay_rejected", "code": code}},
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        if bearer_auth:
            self.send_header("WWW-Authenticate", 'Bearer realm="maka-local-relay"')
        if proxy_auth:
            self.send_header("Proxy-Authenticate", 'Basic realm="maka-local-relay"')
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(encoded)


def judge_request_evidence(request: Mapping[str, object]) -> tuple[str, str] | None:
    """Bind one judge receipt to a fresh nonce and all provider-visible user text."""

    items = request.get("input")
    if not isinstance(items, list):
        return None
    users = [item for item in items if isinstance(item, dict)
             and item.get("role") == "user"]
    if len(users) != 1:
        return None
    content = users[0].get("content")
    if not isinstance(content, list) or len(content) != 1:
        return None
    part = content[0]
    if not isinstance(part, dict) or part.get("type") != "input_text":
        return None
    body = part.get("text")
    if not isinstance(body, str) or len(body) > MAX_REQUEST_BYTES:
        return None
    first_line, separator, remaining = body.partition("\n")
    match = _JUDGE_MARKER_RE.fullmatch(first_line)
    if match is None or not separator or not remaining:
        return None
    return match.group(1), hashlib.sha256(body.encode("utf-8")).hexdigest()


def judge_request_marker(request: Mapping[str, object]) -> str | None:
    evidence = judge_request_evidence(request)
    return evidence[0] if evidence is not None else None


def routing_request_evidence(request: Mapping[str, object]) -> tuple[str, str] | None:
    """Bind a nonce only when the newest provider-visible user turn carries it."""

    items = request.get("input")
    if not isinstance(items, list):
        return None
    users = [item for item in items if isinstance(item, dict)
             and item.get("role") == "user"]
    if not users:
        return None
    for user in users[-1:]:
        content = user.get("content")
        if not isinstance(content, list) or not content:
            continue
        bodies: list[str] = []
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "input_text":
                bodies = []
                break
            body = part.get("text")
            if not isinstance(body, str):
                bodies = []
                break
            bodies.append(body)
        if not bodies:
            continue
        visible_text = "\n".join(bodies)
        if len(visible_text.encode("utf-8")) > MAX_REQUEST_BYTES:
            return None
        lines = visible_text.splitlines()
        if any(_pinned_routing_line_candidate(line)
               and _ROUTING_MARKER_RE.fullmatch(line) is None for line in lines):
            return None
        matches = [match.group(1) or match.group(2) for line in lines
                   if (match := _ROUTING_MARKER_RE.fullmatch(line)) is not None]
        if len(matches) > 1:
            return None
        if matches:
            return matches[0], hashlib.sha256(visible_text.encode("utf-8")).hexdigest()
    return None


def _pinned_routing_line_candidate(line: str) -> bool:
    return (line.startswith(_NEUTRAL_ROUTING_LINE_PREFIX)
            or (line.startswith(_PINNED_ROUTING_LINE_PREFIX)
                and "-" in line[len(_PINNED_ROUTING_LINE_PREFIX):]))


def routing_pinned_marker_prefix_seen(request: Mapping[str, object]) -> bool:
    items = request.get("input")
    if not isinstance(items, list):
        return False
    users = [item for item in items if isinstance(item, dict)
             and item.get("role") == "user"]
    if not users:
        return False
    content = users[-1].get("content")
    if not isinstance(content, list):
        return False
    return any(
        _pinned_routing_line_candidate(line)
        for part in content if isinstance(part, dict)
        and part.get("type") == "input_text"
        and isinstance(part.get("text"), str)
        for line in part["text"].splitlines()
    )


def routing_user_content_shape(request: Mapping[str, object]) -> str:
    items = request.get("input")
    if not isinstance(items, list):
        return "input-other"
    users = [item for item in items if isinstance(item, dict)
             and item.get("role") == "user"]
    if not users:
        return "no-user"
    content = users[-1].get("content")
    if isinstance(content, str):
        return "string"
    if not isinstance(content, list):
        return "other"
    if len(content) > 8:
        return "list:many"
    kinds = []
    for part in content:
        kind = part.get("type") if isinstance(part, dict) else None
        kinds.append(kind if kind in {"input_text", "input_image", "input_file"} else "other")
    return "list:" + ",".join(kinds)


def routing_skill_tools(request: Mapping[str, object]) -> dict[str, dict[str, object]] | None:
    tools = request.get("tools")
    if not isinstance(tools, list) or len(tools) not in {2, 3}:
        return None
    by_name: dict[str, dict[str, object]] = {}
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            return None
        name = tool.get("name")
        if (not isinstance(name, str) or name not in {"Read", "Skill", "SkillSearch"}
                or name in by_name):
            return None
        by_name[name] = tool
    if set(by_name) not in ({"Skill", "SkillSearch"},
                            {"Read", "Skill", "SkillSearch"}):
        return None
    return by_name


def routing_inbound_tool_shape(request: Mapping[str, object]) -> dict[str, object]:
    tools = request.get("tools")
    shape = "absent" if "tools" not in request else "other"
    if not isinstance(tools, list):
        return {"shape": shape, "count": 0, "names": [], "types": [],
                "truncated": False}
    names = []
    types = []
    for tool in tools[:8]:
        name = tool.get("name") if isinstance(tool, dict) else None
        tool_type = tool.get("type") if isinstance(tool, dict) else None
        names.append(name if isinstance(name, str)
                     and name in {"Read", "Skill", "SkillSearch"} else "other")
        types.append(tool_type if isinstance(tool_type, str)
                     and tool_type in {"function", "apply_patch"} else "other")
    return {"shape": "list", "count": len(tools), "names": names, "types": types,
            "truncated": len(tools) > 8}


def responses_body_rejection(raw: bytes) -> str | None:
    try:
        body = json.loads(
            raw,
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_json_constant,
            parse_float=_strict_json_float,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        return "json"
    if not isinstance(body, dict):
        return "top_level"
    keys = frozenset(body)
    if not _REQUIRED_BODY_KEYS.issubset(keys):
        # Key names are a closed local schema vocabulary, never request data.
        # Reporting the first missing field makes adapter drift diagnosable
        # without logging prompts, headers, or credential material.
        for required in sorted(_REQUIRED_BODY_KEYS):
            if required not in keys:
                return f"missing_{required}"
    if not keys.issubset(_REQUIRED_BODY_KEYS | _OPTIONAL_BODY_KEYS):
        return "unexpected_keys"
    if body.get("model") not in MODEL_IDS:
        return "model"
    if body.get("stream") is not True or body.get("store") is not False:
        return "stream_store"
    if body.get("parallel_tool_calls") is not True:
        return "parallel_tools"
    if body.get("include") != ["reasoning.encrypted_content"]:
        return "include"
    reasoning = body.get("reasoning")
    if not isinstance(reasoning, dict):
        return "reasoning_type"
    if "effort" not in reasoning:
        return "reasoning_missing_effort"
    if "summary" not in reasoning:
        return "reasoning_missing_summary"
    if frozenset(reasoning) != {"effort", "summary"}:
        return "reasoning_unexpected_key"
    if not isinstance(reasoning["effort"], str):
        return "reasoning_effort_unknown"
    if reasoning["effort"] not in {"medium", "xhigh", "max"}:
        known_efforts = {"none", "minimal", "low", "medium", "high"}
        return (
            f"reasoning_effort_{reasoning['effort']}"
            if reasoning["effort"] in known_efforts
            else "reasoning_effort_unknown"
        )
    if reasoning["summary"] not in {"auto", "detailed"}:
        return "reasoning_summary"
    if "prompt_cache_key" in body:
        prompt_cache_key = body["prompt_cache_key"]
        if not isinstance(prompt_cache_key, str) or not _PROMPT_CACHE_KEY_RE.fullmatch(
            prompt_cache_key
        ):
            return "cache_key"
    if "previous_response_id" in body:
        previous_id = body["previous_response_id"]
        if not isinstance(previous_id, str) or not _RESPONSE_ID_RE.fullmatch(previous_id):
            return "previous_response_id"
    model_input = body.get("input")
    if not isinstance(model_input, list) or not model_input:
        return "input"
    if "tools" not in body:
        return None if "tool_choice" not in body else "tool_choice"
    tools = body["tools"]
    if not isinstance(tools, list) or not tools or len(tools) > 256:
        return "tools"
    if body.get("tool_choice") != "auto":
        return "tool_choice"
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") not in _ALLOWED_TOOL_TYPES:
            return "tool_type"
    return None


def enforce_outbound_policy(raw: bytes) -> bytes:
    """Narrow Maka's SDK defaults and force Standard API processing.

    The pinned headless and TUI clients do not apply Runtime Policy's
    chatDefaults.thinkingLevel when a Session omits an explicit level. The
    relay accepts only that known SDK fallback, xhigh, or max. It sends xhigh
    for the fallback and preserves an explicit xhigh or max selection.
    """

    body = json.loads(
        raw,
        object_pairs_hook=_strict_json_object,
        parse_constant=_reject_json_constant,
        parse_float=_strict_json_float,
    )
    if body["reasoning"]["effort"] == "medium":
        body["reasoning"]["effort"] = "xhigh"
    # The pinned AI SDK defaults an omitted reasoningSummary to 'detailed'.
    # Keep the reviewed wire contract at 'auto' even on that client path.
    body["reasoning"]["summary"] = "auto"
    # service_tier is deliberately absent from the admitted inbound schema:
    # only this relay can choose a paid API processing tier.
    body["service_tier"] = "default"
    return json.dumps(
        body,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def valid_responses_body(raw: bytes) -> bool:
    return responses_body_rejection(raw) is None


def create_server(
    host: str,
    port: int,
    dependencies: RelayDependencies,
) -> LoopbackThreadingHTTPServer:
    if host != LOOPBACK_HOST:
        raise RelayConfigurationError("relay must bind to the IPv4 loopback address")
    if not 0 <= port <= 65535:
        raise RelayConfigurationError("relay port is invalid")
    if not _TOKEN_RE.fullmatch(dependencies.caller_token):
        raise RelayConfigurationError("caller token is malformed")
    if not _TOKEN_RE.fullmatch(dependencies.attestation):
        raise RelayConfigurationError("relay attestation is malformed")
    server = LoopbackThreadingHTTPServer((host, port), RelayHandler)
    server.relay_dependencies = dependencies  # type: ignore[attr-defined]
    return server


def _disable_core_dumps() -> None:
    with contextlib.suppress(ValueError, OSError):
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _private_directory(path: pathlib.Path) -> None:
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise RelayConfigurationError("relay config directory is unsafe")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise RelayConfigurationError("relay config directory permissions are too broad")


def _write_private_value(path: pathlib.Path, value: str) -> None:
    _private_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = pathlib.Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            output.write(f"{value}\n".encode("ascii"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _remove_attestation(path: pathlib.Path) -> None:
    _private_directory(path.parent)
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


def _sanitize_relay_environment() -> None:
    for name in (
        "SSLKEYLOGFILE",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "PYTHONHOME",
        "PYTHONPATH",
    ):
        os.environ.pop(name, None)


def serve(
    token_file: pathlib.Path,
    attestation_file: pathlib.Path,
    keychain_account_file: pathlib.Path,
    relay_ready_file: pathlib.Path,
    port: int,
) -> None:
    assert_current_runtime()
    _disable_core_dumps()
    _sanitize_relay_environment()
    _remove_attestation(attestation_file)
    token = read_private_token(token_file)
    keychain_account = read_keychain_account(keychain_account_file)
    attestation = secrets.token_urlsafe(48)
    dependencies = RelayDependencies(
        caller_token=token,
        attestation=attestation,
        key_loader=KeychainOpenAIKey(
            keychain_account,
            keychain_account_file,
            relay_ready_file,
        ),
        forwarder=OpenAIForwarder(),
        telemetry_path=pathlib.Path.home() / ".local/state/khenrix-utils/maka/relay-last-tier.json",
    )
    server = create_server(LOOPBACK_HOST, port, dependencies)
    _write_private_value(attestation_file, attestation)

    def request_shutdown(_signum: int, _frame: object) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
        _remove_attestation(attestation_file)


def parse_args(arguments: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    serve_parser = subcommands.add_parser("serve", help="run the local relay")
    serve_parser.add_argument("--token-file", type=pathlib.Path, required=True)
    serve_parser.add_argument("--attestation-file", type=pathlib.Path, required=True)
    serve_parser.add_argument("--keychain-account-file", type=pathlib.Path, required=True)
    serve_parser.add_argument("--relay-ready-file", type=pathlib.Path, required=True)
    serve_parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    return parser.parse_args(arguments)


def main(arguments: list[str] | None = None) -> int:
    options = parse_args(arguments)
    if options.command == "serve":
        try:
            serve(
                options.token_file,
                options.attestation_file,
                options.keychain_account_file,
                options.relay_ready_file,
                options.port,
            )
        except RelayConfigurationError:
            return 78
        except ProviderCredentialError:
            return 69
        except RuntimeHardeningError:
            return 78
        except OSError:
            return 71
        return 0
    return 64


if __name__ == "__main__":
    raise SystemExit(main())
