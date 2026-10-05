"""Owner-approved, host-only credential sources for machine routes."""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import time
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlencode

from .container_routes import RouteProfile, RouteRejected, _ACCOUNT, _open_parent, _validate_profile
from .container_transfer_support import canonical_json
from .container_upstream import _TransportLease, _connect, _remaining, _system_tls_context


_OWNER_HOME = Path.home()
_GCLOUD_SYSTEM_PATHS = tuple(Path(directory) / "gcloud" for directory in (
    "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
))
_TOKENINFO_JSON = re.compile(
    r'application/json(?: *; *charset *= *(?:utf-8|"utf-8"))?\Z',
    re.ASCII | re.IGNORECASE,
)
_MAX_SOURCE_BYTES = 64 * 1024
_MAX_TOKENINFO_BYTES = 8 * 1024
_TOKENINFO_DEADLINE_SECONDS = 5.0
_KEYCHAIN_READY_PREFIX = b"maka-api-key-relay-ready-v2\nkeychain-account-sha256="


class RouteCredentialError(ValueError):
    """The selected owner-approved host credential source is unavailable."""


def _owned_bytes(path: Path, limit: int) -> bytes:
    try:
        parent = _open_parent(path)
        try:
            before = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or stat.S_IMODE(before.st_mode) != 0o600
                    or not 0 < before.st_size <= limit):
                raise RouteCredentialError("approved credential source is unsafe")
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=parent)
            try:
                opened = os.fstat(fd)
                if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid()
                        or stat.S_IMODE(opened.st_mode) != 0o600
                        or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns,
                            opened.st_ctime_ns) != (before.st_dev, before.st_ino, before.st_size,
                                                    before.st_mtime_ns, before.st_ctime_ns)):
                    raise RouteCredentialError("approved credential source changed")
                data = os.read(fd, limit + 1)
                after = os.fstat(fd)
                if (len(data) != opened.st_size or len(data) > limit
                        or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                            after.st_ctime_ns) != (opened.st_dev, opened.st_ino,
                                                   opened.st_size, opened.st_mtime_ns,
                                                   opened.st_ctime_ns)):
                    raise RouteCredentialError("approved credential source changed")
                return data
            finally:
                os.close(fd)
        finally:
            os.close(parent)
    except (OSError, ValueError):
        raise RouteCredentialError("approved credential source is unavailable") from None


def _json_object(raw: bytes) -> dict[str, object]:
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate field")
            result[key] = value
        return result

    def invalid_constant(_value: str) -> None:
        raise ValueError("non-finite number")

    try:
        document = json.loads(raw, object_pairs_hook=unique,
                              parse_constant=invalid_constant)
    except (UnicodeError, ValueError, RecursionError):
        raise RouteCredentialError("approved credential document is invalid") from None
    if type(document) is not dict:
        raise RouteCredentialError("approved credential document is invalid")
    return document


def _bounded_secret(raw: bytes) -> str:
    if not 0 < len(raw) <= _MAX_SOURCE_BYTES:
        raise RouteCredentialError("approved credential is unavailable")
    try:
        value = raw.decode("utf-8")
    except UnicodeError:
        raise RouteCredentialError("approved credential is invalid") from None
    if (not value or len(value) > _MAX_SOURCE_BYTES
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise RouteCredentialError("approved credential is invalid")
    return value


def _run(argv: list[str], *, timeout: float, env: dict[str, str]) -> bytes:
    process = None
    completed = False
    try:
        deadline = time.monotonic() + timeout
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   close_fds=True, env=env, start_new_session=True)
        chunks = []
        size = 0
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise RouteCredentialError("approved key source is unavailable")
                chunk = os.read(process.stdout.fileno(),
                                min(4096, _MAX_SOURCE_BYTES + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > _MAX_SOURCE_BYTES:
                    raise RouteCredentialError("approved key source is unavailable")
                chunks.append(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0 or process.wait(timeout=remaining) != 0:
            raise RouteCredentialError("approved key source is unavailable")
        completed = True
        return b"".join(chunks)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        raise RouteCredentialError("approved key source is unavailable") from None
    finally:
        if process is not None:
            if not completed:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
            process.stdout.close()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass


def _account(raw: bytes) -> str:
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        raise RouteCredentialError("approved account selector is invalid") from None
    if not text.endswith("\n") or _ACCOUNT.fullmatch(text[:-1]) is None:
        raise RouteCredentialError("approved account selector is invalid")
    return text[:-1]


def _fingerprint(route: RouteProfile, principal: str) -> str:
    return hashlib.sha256(canonical_json({
        "auth_family": route.auth_family,
        "source_ref": route.credential_ref,
        "principal": principal,
        "project": route.project,
    })).hexdigest()


def _resolve_gcloud() -> Path:
    candidates = (_OWNER_HOME / "google-cloud-sdk/bin/gcloud", *_GCLOUD_SYSTEM_PATHS)
    for candidate in candidates:
        try:
            if not candidate.is_absolute() or ".." in candidate.parts:
                continue
            metadata = candidate.lstat()
            if (not stat.S_ISREG(metadata.st_mode) or not os.access(candidate, os.X_OK)
                    or metadata.st_uid not in {0, os.getuid()} or metadata.st_mode & 0o022):
                continue
            for parent in candidate.parents:
                metadata = parent.lstat()
                if (not stat.S_ISDIR(metadata.st_mode)
                        or metadata.st_uid not in {0, os.getuid()} or metadata.st_mode & 0o022):
                    break
            else:
                return candidate
        except OSError:
            continue
    raise RouteCredentialError("gcloud is unavailable at an approved safe installation")


class HostCredentialSource:
    """Capture approval before lookup and recheck it before each host-side load."""

    def __init__(self, route: RouteProfile):
        if not isinstance(route, RouteProfile):
            raise RouteCredentialError("approved route is invalid")
        try:
            descriptor = asdict(route)
            descriptor["execution_classes"] = list(route.execution_classes)
            _validate_profile(descriptor)
        except (RouteRejected, TypeError, ValueError):
            raise RouteCredentialError("approved route is invalid") from None
        self.route = route
        self.fingerprint: str | None = None
        root = _OWNER_HOME / ".config/khenrix-utils"
        if route.auth_family == "openai-keychain":
            self._selector = root / "maka/maka-openai-keychain-account"
            self._ready = root / "maka/maka-api-key-relay-ready"
            self._approval = self._keychain_approval()
        elif route.auth_family == "openai-1password":
            self._selector = root / "fanout/op-account"
            self._approval = _owned_bytes(self._selector, 256)
            _account(self._approval)
        elif route.auth_family == "vertex-adc":
            self._selector = root / "fanout/vertex-adc-source.json"
            self._adc = _OWNER_HOME / ".config/gcloud/application_default_credentials.json"
            self._approval = _owned_bytes(self._selector, 1024)
            self._vertex_approval()
        else:
            raise RouteCredentialError("route has no approved direct credential source")

    def _keychain_approval(self) -> bytes:
        raw = _owned_bytes(self._selector, 256)
        account = _account(raw)
        if self.route.credential_ref != f"keychain:{account}":
            raise RouteCredentialError("Keychain selector differs from approved route")
        marker = _owned_bytes(self._ready, 256)
        expected = (_KEYCHAIN_READY_PREFIX + hashlib.sha256(raw).hexdigest().encode()
                    + b"\n")
        if marker != expected:
            raise RouteCredentialError("Keychain selector is not ready")
        return raw + marker

    def _vertex_approval(self) -> str:
        document = _json_object(self._approval)
        if (document.keys() != {"account", "quota_project", "source_ref"}
                or document["source_ref"] != self.route.credential_ref
                or document["quota_project"] != self.route.project
                or type(document["account"]) is not str
                or _ACCOUNT.fullmatch(document["account"]) is None):
            raise RouteCredentialError("Vertex source approval differs from route")
        return document["account"]

    def _require_adc(self) -> None:
        adc = _json_object(_owned_bytes(self._adc, _MAX_SOURCE_BYTES))
        if (adc.get("type") != "authorized_user"
                or adc.get("quota_project_id") != self.route.project):
            raise RouteCredentialError("ADC quota project differs from route")

    def load(self) -> str:
        """Return a credential only to the host controller; never use a fallback."""
        self.fingerprint = None
        if self.route.auth_family == "openai-keychain":
            if self._keychain_approval() != self._approval:
                raise RouteCredentialError("Keychain approval changed")
            account = self.route.credential_ref.removeprefix("keychain:")
            raw = _run(["/usr/bin/security", "find-generic-password", "-s",
                        "Codex Auth", "-a", account, "-w"], timeout=10,
                       env={"PATH": "/usr/bin:/bin", "LANG": "C"})
            document = _json_object(raw)
            value = document.get("OPENAI_API_KEY")
            if type(value) is not str:
                raise RouteCredentialError("OpenAI key is unavailable")
            try:
                credential = _bounded_secret(value.encode("utf-8"))
            except UnicodeError:
                raise RouteCredentialError("OpenAI key is invalid") from None
            if self._keychain_approval() != self._approval:
                raise RouteCredentialError("Keychain approval changed")
            principal = account
        elif self.route.auth_family == "openai-1password":
            if _owned_bytes(self._selector, 256) != self._approval:
                raise RouteCredentialError("1Password account approval changed")
            account = _account(self._approval)
            raw = _run(["op", "read", "-n", self.route.credential_ref,
                        "--account", account], timeout=10,
                       env={"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
                            "LANG": "C", "HOME": str(_OWNER_HOME)})
            credential = _bounded_secret(raw.removesuffix(b"\n"))
            if _owned_bytes(self._selector, 256) != self._approval:
                raise RouteCredentialError("1Password account approval changed")
            principal = account
        elif self.route.auth_family == "vertex-adc":
            if _owned_bytes(self._selector, 1024) != self._approval:
                raise RouteCredentialError("Vertex source approval changed")
            principal = self._vertex_approval()
            self._require_adc()
            raw = _run([str(_resolve_gcloud()), "auth", "application-default", "print-access-token"],
                       timeout=10, env={
                           "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
                           "LANG": "C", "HOME": str(_OWNER_HOME),
                           "CLOUDSDK_CORE_DISABLE_PROMPTS": "1",
                       })
            credential = _bounded_secret(raw.removesuffix(b"\n"))
            if _owned_bytes(self._selector, 1024) != self._approval:
                raise RouteCredentialError("Vertex source approval changed")
            self._require_adc()
            self._verify_vertex_token(credential, principal)
            if _owned_bytes(self._selector, 1024) != self._approval:
                raise RouteCredentialError("Vertex source approval changed")
            self._require_adc()
        else:
            raise RouteCredentialError("route has no approved direct credential source")
        self.fingerprint = _fingerprint(self.route, principal)
        return credential

    @staticmethod
    def _verify_vertex_token(token: str, principal: str) -> None:
        connection = None
        lease = None
        try:
            deadline = time.monotonic() + _TOKENINFO_DEADLINE_SECONDS
            connection = http.client.HTTPSConnection(
                "www.googleapis.com", 443, timeout=_TOKENINFO_DEADLINE_SECONDS,
                context=_system_tls_context())
            connection.set_debuglevel(0)
            connection.auto_open = False
            lease = _TransportLease(connection, deadline)
            lease.start()
            _connect(connection, lease, deadline)
            _remaining(connection, deadline)
            connection.request("POST", "/oauth2/v1/tokeninfo",
                               body=urlencode({"access_token": token}).encode(),
                               headers={"content-type": "application/x-www-form-urlencoded",
                                        "accept": "application/json"})
            _remaining(connection, deadline)
            response = connection.getresponse()
            lease.attach(response)
            _remaining(connection, deadline)
            mime = response.getheader("content-type")
            if (response.status != 200 or type(mime) is not str or len(mime) > 128
                    or _TOKENINFO_JSON.fullmatch(mime) is None
                    or response.getheader("content-encoding") not in (None, "identity")):
                raise RouteCredentialError("ADC token account is unavailable")
            raw = response.read(_MAX_TOKENINFO_BYTES + 1)
            _remaining(connection, deadline)
            if not 0 < len(raw) <= _MAX_TOKENINFO_BYTES or response.read(1):
                raise RouteCredentialError("ADC token account is unavailable")
            _remaining(connection, deadline)
            document = _json_object(raw)
            expires = document.get("expires_in")
            if type(expires) is str and re.fullmatch(r"[0-9]{1,5}", expires):
                expires = int(expires)
            if (document.get("email") != principal
                    or type(expires) is not int or not 60 < expires <= 43200):
                raise RouteCredentialError("ADC token account or expiration differs")
        except RouteCredentialError:
            raise
        except Exception:
            raise RouteCredentialError("ADC token verification failed") from None
        finally:
            if lease is not None:
                lease.close()
            elif connection is not None:
                connection.close()
