#!/usr/bin/env python3
"""Install and operate the pinned, local, cross-CLI claude-mem runtime."""

from __future__ import annotations

import argparse
import base64
import copy
import datetime as dt
import hashlib
import io
import json
import os
import pathlib
import re
import secrets
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Mapping
from typing import Any

import tomllib

PACKAGE = "claude-mem"
PACKAGE_VERSION = "13.25.1"
SOURCE_COMMIT = "dcfc44221deabc228b0698a0012f87ed2fe6bbe7"
ARTIFACT_URL = "https://registry.npmjs.org/claude-mem/-/claude-mem-13.25.1.tgz"
ARTIFACT_INTEGRITY = "sha512-zhqFluHfYWo5Xi8CoP6aNTZVCCdXwwsklUrWQyOMDG6MsO+4DPekdGB2COpQpt3i6oE++O0FUFjQxr2P3utX0w=="
BUN_VERSION = "1.4.2"
OPENAI_MODEL = "gpt-5.6-sol"
RELAY_PORT = 48174
GATEWAY_PORT = 48175
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
MAX_UNPACKED_BYTES = 96 * 1024 * 1024
ACCOUNT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@._+|:-]{0,127}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
PROJECT_RE = re.compile(r"^[^\x00-\x1f,]{1,512}$")
ROUTES = {"claude-subscription", "codex-subscription", "openai-keychain", "local-claude"}
CONTROLLER_FILES = (
    "memoryctl.py",
    "memory_search.py",
    "memory_gateway.py",
    "provider_relay.py",
    "provenance.json",
    "README.md",
    "THIRD_PARTY_NOTICES.md",
    "LICENSE.claude-mem.txt",
)


class MemoryConfigurationError(RuntimeError):
    """The local memory configuration is incomplete or unsafe."""


def account_home() -> pathlib.Path:
    override = os.environ.get("AGENTIC_MEMORY_HOME")
    return pathlib.Path(override).expanduser().resolve() if override else pathlib.Path.home()


def config_dir() -> pathlib.Path:
    return account_home() / ".config" / "agentic-memory"


def install_root() -> pathlib.Path:
    return account_home() / ".local" / "share" / "agentic-memory"


def data_dir() -> pathlib.Path:
    return install_root() / "data"


def runtime_root() -> pathlib.Path:
    return install_root() / "runtime" / PACKAGE / PACKAGE_VERSION


def installed_controller_root() -> pathlib.Path:
    return install_root() / "controller"


def controller_root() -> pathlib.Path:
    installed = installed_controller_root()
    return installed if (installed / "memoryctl.py").is_file() else pathlib.Path(__file__).resolve().parent


def worker_port() -> int:
    return 37700 + (os.getuid() % 100)


def route_path() -> pathlib.Path:
    return config_dir() / "route.json"


def relay_token_path() -> pathlib.Path:
    return config_dir() / "relay-token"


def relay_pid_path() -> pathlib.Path:
    return config_dir() / "relay.pid"


def exclusions_path() -> pathlib.Path:
    return config_dir() / "excluded-projects.json"


def gateway_token_path() -> pathlib.Path:
    return config_dir() / "gateway-token"


def gateway_pid_path() -> pathlib.Path:
    return config_dir() / "gateway.pid"


def settings_path() -> pathlib.Path:
    return data_dir() / "settings.json"


def _assert_private_directory(path: pathlib.Path, *, create: bool) -> None:
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        metadata = path.lstat()
    except OSError as error:
        raise MemoryConfigurationError(f"private directory is unavailable: {path}") from error
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise MemoryConfigurationError(f"unsafe private directory: {path}")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        if not create:
            raise MemoryConfigurationError(f"private directory permissions must be 0700: {path}")
        os.chmod(path, 0o700)


def _assert_private_file(path: pathlib.Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise MemoryConfigurationError(f"private file is unavailable: {path}") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise MemoryConfigurationError(f"private file must be owner-only: {path}")


def _atomic_private_write(path: pathlib.Path, payload: bytes, *, executable: bool = False) -> None:
    _assert_private_directory(path.parent, create=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = pathlib.Path(temporary)
    mode = 0o700 if executable else 0o600
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, mode)
    finally:
        temporary_path.unlink(missing_ok=True)


def _atomic_private_json(path: pathlib.Path, document: Mapping[str, Any]) -> None:
    _atomic_private_write(path, (json.dumps(document, indent=2, sort_keys=True) + "\n").encode())


def _load_private_json(path: pathlib.Path) -> dict[str, Any]:
    _assert_private_file(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MemoryConfigurationError(f"invalid private JSON: {path}") from error
    if not isinstance(value, dict):
        raise MemoryConfigurationError(f"private JSON must be an object: {path}")
    return value


def _relay_token(*, create: bool = False) -> str:
    path = relay_token_path()
    if path.exists() or path.is_symlink():
        _assert_private_file(path)
        token = path.read_text(encoding="ascii").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            raise MemoryConfigurationError("local relay token is malformed")
        return token
    if not create:
        raise MemoryConfigurationError("local relay token is unavailable")
    token = secrets.token_urlsafe(32)
    _atomic_private_write(path, f"{token}\n".encode("ascii"))
    return token


def _gateway_token() -> str:
    path = gateway_token_path()
    if path.exists() or path.is_symlink():
        _assert_private_file(path)
        token = path.read_text(encoding="ascii").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            raise MemoryConfigurationError("local gateway token is malformed")
        return token
    token = secrets.token_urlsafe(32)
    _atomic_private_write(path, f"{token}\n".encode("ascii"))
    return token


def read_route() -> dict[str, Any]:
    route = _load_private_json(route_path())
    if route.get("schema_version") != 2 or route.get("route") not in ROUTES:
        raise MemoryConfigurationError("memory route selector is invalid")
    name = route["route"]
    if name == "openai-keychain":
        account = route.get("keychain_account")
        if not isinstance(account, str) or not ACCOUNT_RE.fullmatch(account):
            raise MemoryConfigurationError("memory Keychain account selector is invalid")
    elif "keychain_account" in route:
        raise MemoryConfigurationError("route must not retain a Keychain account")
    if name == "local-claude":
        provider_file = route.get("provider_file")
        if not isinstance(provider_file, str) or not pathlib.Path(provider_file).is_absolute():
            raise MemoryConfigurationError("local provider descriptor path is invalid")
        load_local_provider(pathlib.Path(provider_file))
    elif "provider_file" in route:
        raise MemoryConfigurationError("route must not retain a provider descriptor")
    return route


def load_local_provider(path: pathlib.Path) -> dict[str, Any]:
    document = _load_private_json(path)
    model = document.get("model")
    auth = document.get("auth_method")
    environment = document.get("environment", {})
    if document.get("schema_version") != 1 or not isinstance(model, str) or not MODEL_RE.fullmatch(model):
        raise MemoryConfigurationError("local provider descriptor has an invalid model")
    if auth not in {"api-key", "gateway", "cli"}:
        raise MemoryConfigurationError("local provider descriptor has an invalid auth method")
    if not isinstance(environment, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in environment.items()):
        raise MemoryConfigurationError("local provider environment must contain strings")
    allowed = {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "CLAUDE_CODE_USE_VERTEX",
        "CLOUD_ML_REGION",
        "GOOGLE_APPLICATION_CREDENTIALS",
    }
    unknown = sorted(set(environment) - allowed)
    if unknown:
        raise MemoryConfigurationError(f"local provider descriptor contains unsupported keys: {', '.join(unknown)}")
    return document


def excluded_projects() -> list[str]:
    path = exclusions_path()
    if not path.exists():
        return []
    document = _load_private_json(path)
    projects = document.get("projects")
    if document.get("schema_version") != 1 or not isinstance(projects, list):
        raise MemoryConfigurationError("excluded projects file is invalid")
    if not all(isinstance(project, str) and PROJECT_RE.fullmatch(project) for project in projects):
        raise MemoryConfigurationError("excluded projects file contains an invalid project")
    return sorted(set(projects))


def update_excluded_project(project: str, *, remove: bool) -> list[str]:
    if not PROJECT_RE.fullmatch(project):
        raise MemoryConfigurationError("project exclusion must be 1-512 printable characters without commas")
    projects = set(excluded_projects())
    if remove:
        projects.discard(project)
    else:
        projects.add(project)
    result = sorted(projects)
    _atomic_private_json(exclusions_path(), {"schema_version": 1, "projects": result})
    if route_path().exists():
        route = read_route()
        _atomic_private_json(settings_path(), settings_for_route(route))
        if worker_health() and (stop_worker() != 0 or run_worker(["start"]) != 0):
            raise MemoryConfigurationError("memory worker failed to restart after exclusions changed")
    return result


def _common_settings() -> dict[str, str]:
    return {
        "CLAUDE_MEM_DATA_DIR": str(data_dir()),
        "CLAUDE_MEM_WORKER_HOST": "127.0.0.1",
        "CLAUDE_MEM_WORKER_PORT": str(worker_port()),
        "CLAUDE_MEM_LOG_LEVEL": "INFO",
        "CLAUDE_MEM_MODE": "code",
        "CLAUDE_MEM_RUNTIME": "worker",
        "CLAUDE_MEM_SEMANTIC_INJECT": "false",
        "CLAUDE_MEM_CHROMA_ENABLED": "false",
        "CLAUDE_MEM_CHROMA_API_KEY": "",
        "CLAUDE_MEM_TIER_ROUTING_ENABLED": "false",
        "CLAUDE_MEM_TIER_SIMPLE_MODEL": "",
        "CLAUDE_MEM_TIER_SUMMARY_MODEL": "",
        "CLAUDE_MEM_TIER_FAST_MODEL": "",
        "CLAUDE_MEM_TIER_SMART_MODEL": "",
        "CLAUDE_MEM_CLOUD_SYNC_TOKEN": "",
        "CLAUDE_MEM_CLOUD_SYNC_USER_ID": "",
        "CLAUDE_MEM_CLOUD_SYNC_HUB_URL": "",
        "CLAUDE_MEM_CLOUD_SYNC_DEVICE_ID": "",
        "CLAUDE_MEM_CLOUD_SYNC_DEVICE_NAME": "",
        "CLAUDE_MEM_CLOUD_SYNC_WS": "false",
        "CLAUDE_MEM_PRO_MEMORY_KEY": "",
        "CLAUDE_MEM_PRO_MEMORY_BASE_URL": "",
        "CLAUDE_MEM_PRO_MEMORY_MODEL": "",
        "CLAUDE_MEM_TELEGRAM_ENABLED": "false",
        "CLAUDE_MEM_TELEGRAM_WRAPUPS_ENABLED": "false",
        "CLAUDE_MEM_GROK_BOT_AWARENESS_ENABLED": "false",
        "CLAUDE_MEM_GROK_BOT_INJECT_ENABLED": "false",
        "CLAUDE_MEM_CCS_ALIGN_ENABLED": "false",
        "CLAUDE_MEM_TRANSCRIPTS_ENABLED": "false",
        "CLAUDE_MEM_CODEX_TRANSCRIPT_INGESTION": "false",
        "CLAUDE_MEM_TV_TOKEN": "",
        "CLAUDE_MEM_SERVER_URL": "",
        "CLAUDE_MEM_SERVER_API_KEY": "",
        "CLAUDE_MEM_SERVER_PROJECT_ID": "",
        "CLAUDE_MEM_SERVER_BETA_URL": "",
        "CLAUDE_MEM_SERVER_BETA_API_KEY": "",
        "CLAUDE_MEM_SERVER_BETA_PROJECT_ID": "",
        "CLAUDE_MEM_QUEUE_ENGINE": "sqlite",
        "CLAUDE_MEM_REDIS_URL": "",
        "CLAUDE_MEM_WELCOME_HINT_ENABLED": "false",
        "CLAUDE_MEM_FOLDER_CLAUDEMD_ENABLED": "false",
        "CLAUDE_MEM_GEMINI_API_KEY": "",
        "CLAUDE_MEM_GEMINI_MODEL": "",
        "CLAUDE_MEM_GEMINI_RATE_LIMITING_ENABLED": "false",
        "CLAUDE_MEM_EXCLUDED_PROJECTS": ",".join(excluded_projects()),
    }


def settings_for_route(route: Mapping[str, Any]) -> dict[str, str]:
    settings = _common_settings()
    name = str(route["route"])
    if name in {"codex-subscription", "openai-keychain"}:
        settings.update(
            {
                "CLAUDE_MEM_PROVIDER": "openrouter",
                "CLAUDE_MEM_MODEL": "",
                "CLAUDE_MEM_OPENROUTER_API_KEY": _relay_token(),
                "CLAUDE_MEM_OPENROUTER_MODEL": OPENAI_MODEL,
                "CLAUDE_MEM_OPENROUTER_BASE_URL": f"http://127.0.0.1:{RELAY_PORT}/v1",
                "CLAUDE_MEM_OPENROUTER_SITE_URL": "",
                "CLAUDE_MEM_OPENROUTER_APP_NAME": "khenrix-memory",
            }
        )
    elif name == "claude-subscription":
        settings.update(
            {
                "CLAUDE_MEM_PROVIDER": "claude",
                "CLAUDE_MEM_MODEL": "sonnet",
                "CLAUDE_MEM_CLAUDE_AUTH_METHOD": "subscription",
                "CLAUDE_MEM_OPENROUTER_API_KEY": "",
                "CLAUDE_MEM_OPENROUTER_MODEL": "",
                "CLAUDE_MEM_OPENROUTER_BASE_URL": "",
            }
        )
    elif name == "local-claude":
        provider = load_local_provider(pathlib.Path(str(route["provider_file"])))
        settings.update(
            {
                "CLAUDE_MEM_PROVIDER": "claude",
                "CLAUDE_MEM_MODEL": str(provider["model"]),
                "CLAUDE_MEM_CLAUDE_AUTH_METHOD": str(provider["auth_method"]),
                "CLAUDE_MEM_OPENROUTER_API_KEY": "",
                "CLAUDE_MEM_OPENROUTER_MODEL": "",
                "CLAUDE_MEM_OPENROUTER_BASE_URL": "",
            }
        )
    else:
        raise MemoryConfigurationError(f"unsupported memory route: {name}")
    return settings


def select_route(
    route: str,
    *,
    keychain_account: str | None = None,
    provider_file: pathlib.Path | None = None,
    restart: bool = True,
) -> dict[str, Any]:
    if route not in ROUTES:
        raise MemoryConfigurationError(f"unsupported memory route: {route}")
    selector: dict[str, Any] = {"schema_version": 2, "route": route}
    if route == "openai-keychain":
        if os.uname().sysname != "Darwin":
            raise MemoryConfigurationError("openai-keychain is supported only on macOS")
        if not keychain_account or not ACCOUNT_RE.fullmatch(keychain_account):
            raise MemoryConfigurationError("openai-keychain requires one exact --keychain-account")
        selector["keychain_account"] = keychain_account
    elif keychain_account is not None:
        raise MemoryConfigurationError("--keychain-account applies only to openai-keychain")
    if route == "local-claude":
        if provider_file is None:
            raise MemoryConfigurationError("local-claude requires --provider-file")
        resolved = provider_file.expanduser().resolve()
        load_local_provider(resolved)
        selector["provider_file"] = str(resolved)
    elif provider_file is not None:
        raise MemoryConfigurationError("--provider-file applies only to local-claude")

    was_running = worker_health()
    if restart and was_running:
        stop_worker(ignore_missing_route=True)
    stop_relay()
    _assert_private_directory(config_dir(), create=True)
    _assert_private_directory(data_dir(), create=True)
    if route in {"codex-subscription", "openai-keychain"}:
        _relay_token(create=True)
    _atomic_private_json(route_path(), selector)
    _atomic_private_json(settings_path(), settings_for_route(selector))
    _atomic_private_json(
        data_dir() / "telemetry.json",
        {"enabled": False, "installId": "disabled-by-khenrix-utils", "decidedAt": "managed-by-khenrix-utils"},
    )
    if restart and was_running and run_worker(["start"]) != 0:
        raise MemoryConfigurationError("memory worker failed to restart after route change")
    return selector


def runtime_environment(route: Mapping[str, Any] | None = None) -> dict[str, str]:
    selected = dict(route or read_route())
    env = dict(os.environ)
    for key in tuple(env):
        if key.startswith("CLAUDE_MEM_"):
            env.pop(key, None)
    for key in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "CLAUDE_CODE_USE_VERTEX",
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "CLOUD_ML_REGION",
        "GOOGLE_APPLICATION_CREDENTIALS",
    ):
        env.pop(key, None)
    env.update(settings_for_route(selected))
    env.update(
        {
            "CLAUDE_MEM_TELEMETRY": "0",
            "CLAUDE_MEM_TELEMETRY_ERRORS": "0",
            "DO_NOT_TRACK": "1",
            "DISABLE_ERROR_REPORTING": "1",
            "DISABLE_TELEMETRY": "1",
        }
    )
    if selected["route"] == "local-claude":
        provider = load_local_provider(pathlib.Path(str(selected["provider_file"])))
        env.update(provider["environment"])
    return env


def _artifact_digest(payload: bytes) -> str:
    return "sha512-" + base64.b64encode(hashlib.sha512(payload).digest()).decode("ascii")


def _verify_artifact(payload: bytes) -> None:
    if len(payload) > MAX_ARTIFACT_BYTES:
        raise MemoryConfigurationError("claude-mem artifact is unexpectedly large")
    if not secrets.compare_digest(_artifact_digest(payload), ARTIFACT_INTEGRITY):
        raise MemoryConfigurationError("claude-mem artifact integrity mismatch")


def _safe_extract(payload: bytes, destination: pathlib.Path) -> None:
    total = 0
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        for member in archive.getmembers():
            relative = pathlib.PurePosixPath(member.name)
            if relative.is_absolute() or ".." in relative.parts or member.issym() or member.islnk() or member.isdev():
                raise MemoryConfigurationError("claude-mem artifact contains an unsafe entry")
            total += max(member.size, 0)
            if total > MAX_UNPACKED_BYTES:
                raise MemoryConfigurationError("claude-mem artifact expands beyond its limit")
            target = destination.joinpath(*relative.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise MemoryConfigurationError("claude-mem artifact entry could not be read")
                with source, target.open("wb") as handle:
                    shutil.copyfileobj(source, handle)


def _runtime_is_valid(root: pathlib.Path) -> bool:
    try:
        package = json.loads((root / "package" / "package.json").read_text())
        marker = json.loads((root / ".artifact.json").read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return (
        package.get("name") == PACKAGE
        and package.get("version") == PACKAGE_VERSION
        and marker.get("integrity") == ARTIFACT_INTEGRITY
        and marker.get("source_commit") == SOURCE_COMMIT
        and (root / "package" / "plugin" / "scripts" / "worker-service.cjs").is_file()
        and (root / "package" / "plugin" / "ui" / "viewer.html").is_file()
    )


def stage_runtime(*, artifact: bytes | None = None) -> pathlib.Path:
    target = runtime_root()
    if target.exists():
        if _runtime_is_valid(target):
            return target
        raise MemoryConfigurationError(f"staged runtime is invalid: {target}")
    _assert_private_directory(target.parent, create=True)
    if artifact is None:
        request = urllib.request.Request(ARTIFACT_URL, headers={"User-Agent": "khenrix-memory-stage/1"})
        with urllib.request.urlopen(request, timeout=30) as response:
            artifact = response.read(MAX_ARTIFACT_BYTES + 1)
    _verify_artifact(artifact)
    temporary = pathlib.Path(tempfile.mkdtemp(prefix=".stage-", dir=target.parent))
    try:
        _safe_extract(artifact, temporary)
        package = json.loads((temporary / "package" / "package.json").read_text())
        if package.get("name") != PACKAGE or package.get("version") != PACKAGE_VERSION:
            raise MemoryConfigurationError("claude-mem artifact package identity mismatch")
        (temporary / ".artifact.json").write_text(
            json.dumps(
                {"package": PACKAGE, "version": PACKAGE_VERSION, "integrity": ARTIFACT_INTEGRITY, "source_commit": SOURCE_COMMIT},
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        for current, _directories, files in os.walk(temporary):
            os.chmod(current, 0o700)
            for name in files:
                os.chmod(pathlib.Path(current) / name, 0o600)
        os.replace(temporary, target)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    return target


def install_controller() -> pathlib.Path:
    source = pathlib.Path(__file__).resolve().parent
    target = installed_controller_root()
    _assert_private_directory(target, create=True)
    for name in CONTROLLER_FILES:
        candidate = source / name
        if not candidate.is_file():
            raise MemoryConfigurationError(f"controller source is missing: {name}")
        _atomic_private_write(target / name, candidate.read_bytes(), executable=name.endswith(".py"))
    return target


def _bun_path() -> str:
    candidate = os.environ.get("AGENTIC_MEMORY_BUN") or shutil.which("bun")
    if not candidate:
        raise MemoryConfigurationError("mise-pinned Bun is unavailable; run mise install")
    result = subprocess.run([candidate, "--version"], check=False, capture_output=True, text=True, timeout=5)
    if result.returncode != 0 or result.stdout.strip() != BUN_VERSION:
        raise MemoryConfigurationError(f"memory runtime requires Bun {BUN_VERSION}")
    return candidate


def _worker_script() -> pathlib.Path:
    script = runtime_root() / "package" / "plugin" / "scripts" / "worker-service.cjs"
    if not script.is_file() or not _runtime_is_valid(runtime_root()):
        raise MemoryConfigurationError("pinned claude-mem runtime is not staged")
    return script


def _relay_health(timeout: float = 0.35) -> bool:
    request = b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
    try:
        with socket.create_connection(("127.0.0.1", RELAY_PORT), timeout=timeout) as client:
            client.sendall(request)
            response = client.recv(2048)
        return b"200 OK" in response and b"khenrix-memory-relay" in response
    except OSError:
        return False


def _ensure_relay(route: Mapping[str, Any]) -> None:
    if route["route"] not in {"openai-keychain", "codex-subscription"} or _relay_health():
        return
    _assert_private_directory(config_dir(), create=True)
    descriptor = os.open(config_dir() / "relay.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                str(controller_root() / "provider_relay.py"),
                "serve",
                "--route-config",
                str(route_path()),
                "--token-file",
                str(relay_token_path()),
                "--port",
                str(RELAY_PORT),
            ],
            stdin=subprocess.DEVNULL,
            stdout=descriptor,
            stderr=descriptor,
            env=runtime_environment(route),
            start_new_session=True,
            close_fds=True,
        )
    finally:
        os.close(descriptor)
    _atomic_private_write(relay_pid_path(), f"{process.pid}\n".encode("ascii"))
    for _ in range(50):
        if _relay_health():
            return
        if process.poll() is not None:
            break
        time.sleep(0.1)
    raise MemoryConfigurationError("provider relay failed to start")


def _gateway_health(timeout: float = 0.35) -> bool:
    request = b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
    try:
        with socket.create_connection(("127.0.0.1", GATEWAY_PORT), timeout=timeout) as client:
            client.sendall(request)
            response = client.recv(2048)
        return b"200 OK" in response and b"khenrix-memory-gateway" in response
    except OSError:
        return False


def _ensure_gateway() -> None:
    if _gateway_health():
        return
    token = _gateway_token()
    if not token:
        raise MemoryConfigurationError("memory gateway token is unavailable")
    descriptor = os.open(config_dir() / "gateway.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                str(controller_root() / "memory_gateway.py"),
                "--token-file",
                str(gateway_token_path()),
                "--worker-port",
                str(worker_port()),
                "--port",
                str(GATEWAY_PORT),
            ],
            stdin=subprocess.DEVNULL,
            stdout=descriptor,
            stderr=descriptor,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        os.close(descriptor)
    _atomic_private_write(gateway_pid_path(), f"{process.pid}\n".encode("ascii"))
    for _ in range(50):
        if _gateway_health():
            return
        if process.poll() is not None:
            break
        time.sleep(0.1)
    raise MemoryConfigurationError("memory gateway failed to start")


def _relay_pid() -> int | None:
    path = relay_pid_path()
    if not path.exists():
        return None
    _assert_private_file(path)
    text = path.read_text(encoding="ascii").strip()
    return int(text) if text.isdigit() and int(text) > 1 else None


def _managed_pid(path: pathlib.Path) -> int | None:
    if not path.exists():
        return None
    _assert_private_file(path)
    text = path.read_text(encoding="ascii").strip()
    return int(text) if text.isdigit() and int(text) > 1 else None


def _pid_is_relay(pid: int) -> bool:
    result = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, check=False)
    return result.returncode == 0 and "provider_relay.py" in result.stdout and "agentic-memory" in result.stdout


def stop_relay() -> None:
    pid = _relay_pid()
    if pid and _pid_is_relay(pid):
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            pass
        for _ in range(20):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
    relay_pid_path().unlink(missing_ok=True)


def stop_gateway() -> None:
    pid = _managed_pid(gateway_pid_path())
    if pid:
        result = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, check=False)
        if result.returncode == 0 and "memory_gateway.py" in result.stdout and "agentic-memory" in result.stdout:
            try:
                os.kill(pid, 15)
            except ProcessLookupError:
                pass
    gateway_pid_path().unlink(missing_ok=True)


def run_worker(arguments: list[str]) -> int:
    if arguments and arguments[0] == "hook" and (
        os.environ.get("KHENRIX_NESTED_AGENT") == "1" or os.environ.get("KHENRIX_MEMORY_ADAPTER") == "1"
    ):
        return 0
    route = read_route()
    _ensure_relay(route)
    os.umask(0o077)
    prefix = [_bun_path(), str(_worker_script())]
    env = runtime_environment(route)
    if len(arguments) >= 2 and arguments[:2] == ["hook", "codex"]:
        env["CLAUDE_MEM_CODEX_HOOK"] = "1"
    if arguments and arguments[0] == "hook":
        started = subprocess.run([*prefix, "start"], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, check=False)
        if started.returncode != 0:
            return started.returncode
        _ensure_gateway()
    result = subprocess.run([*prefix, *arguments], env=env, check=False)
    if result.returncode == 0 and arguments and arguments[0] == "start":
        _ensure_gateway()
    return result.returncode


def stop_worker(*, ignore_missing_route: bool = False) -> int:
    try:
        result = run_worker(["stop"])
    except MemoryConfigurationError:
        if ignore_missing_route:
            return 0
        raise
    stop_gateway()
    stop_relay()
    return result


def worker_health(timeout: float = 0.35) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{worker_port()}/api/health", timeout=timeout) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def _memory_command(platform: str, event: str) -> str:
    return f"python3 {installed_controller_root() / 'memoryctl.py'} hook {platform} {event}"


def _handler(platform: str, event: str, timeout: int, *, async_: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"type": "command", "command": _memory_command(platform, event), "timeout": timeout}
    if async_:
        result["async"] = True
    return result


def canonical_hooks(cli: str) -> dict[str, Any]:
    start = {"type": "command", "command": f"python3 {installed_controller_root() / 'memoryctl.py'} start", "timeout": 60}
    if cli in {"claude", "codex"}:
        platform = "claude-code" if cli == "claude" else "codex"
        async_ = cli == "claude"
        hooks: dict[str, Any] = {
            "SessionStart": [{"matcher": "startup|resume|clear|compact", "hooks": [start, _handler(platform, "context", 60 if cli == "claude" else 30)]}],
            "UserPromptSubmit": [{"hooks": [_handler(platform, "session-init", 60 if cli == "claude" else 30)]}],
            "PreToolUse": [{"matcher": "Read" if cli == "claude" else "^Bash$|^mcp__.+__(read|view|cat)(_file|_files)?$", "hooks": [_handler(platform, "file-context", 60 if cli == "claude" else 30, async_=async_)]}],
            "PostToolUse": [{"matcher": "*" if cli == "claude" else ".*", "hooks": [_handler(platform, "observation", 120, async_=async_)]}],
            "Stop": [{"hooks": [_handler(platform, "summarize", 120, async_=async_)]}],
        }
        if cli == "claude":
            hooks["SessionEnd"] = [{"hooks": [_handler(platform, "session-end", 120, async_=True)]}]
        return {"hooks": hooks}
    if cli == "agy":
        return {
            "agentic-memory": {
                "enabled": True,
                "PreInvocation": [_handler("antigravity-cli", "context", 10)],
                "PreToolUse": [{"matcher": "*", "hooks": [_handler("antigravity-cli", "observation", 10)]}],
                "PostToolUse": [{"matcher": "*", "hooks": [_handler("antigravity-cli", "observation", 10)]}],
                "PostInvocation": [_handler("antigravity-cli", "observation", 10)],
                "Stop": [_handler("antigravity-cli", "summarize", 10)],
            }
        }
    raise MemoryConfigurationError(f"unsupported hook target: {cli}")


def hook_path(cli: str) -> pathlib.Path:
    if cli == "claude":
        return account_home() / ".claude" / "settings.json"
    if cli == "codex":
        return account_home() / ".codex" / "hooks.json"
    if cli == "agy":
        return account_home() / ".gemini" / "config" / "hooks.json"
    raise MemoryConfigurationError(f"unsupported hook target: {cli}")


def _contains_owned_hook(value: Any) -> bool:
    if isinstance(value, dict):
        command = value.get("command")
        if isinstance(command, str) and "agentic-memory/controller/memoryctl.py" in command:
            return True
        return any(_contains_owned_hook(candidate) for candidate in value.values())
    if isinstance(value, list):
        return any(_contains_owned_hook(candidate) for candidate in value)
    return False


def _owned_commands(value: Any) -> set[str]:
    commands: set[str] = set()
    if isinstance(value, dict):
        command = value.get("command")
        if isinstance(command, str) and "agentic-memory/controller/memoryctl.py" in command:
            commands.add(command)
        for candidate in value.values():
            commands.update(_owned_commands(candidate))
    elif isinstance(value, list):
        for candidate in value:
            commands.update(_owned_commands(candidate))
    return commands


def _without_owned_hooks(group: Mapping[str, Any]) -> dict[str, Any] | None:
    result = copy.deepcopy(dict(group))
    handlers = result.get("hooks")
    if isinstance(handlers, list):
        result["hooks"] = [handler for handler in handlers if not _contains_owned_hook(handler)]
        if not result["hooks"]:
            return None
    return result


def _read_json_document(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise MemoryConfigurationError(f"cannot merge invalid JSON: {path}") from error
    if not isinstance(document, dict):
        raise MemoryConfigurationError(f"configuration must be a JSON object: {path}")
    return document


def install_hooks(clis: list[str]) -> dict[str, str]:
    results: dict[str, str] = {}
    for cli in clis:
        path = hook_path(cli)
        document = _read_json_document(path)
        canonical = canonical_hooks(cli)
        if cli == "agy":
            document["agentic-memory"] = canonical["agentic-memory"]
        else:
            hooks = document.setdefault("hooks", {})
            if not isinstance(hooks, dict):
                raise MemoryConfigurationError(f"hooks must be an object: {path}")
            for event, groups in canonical["hooks"].items():
                existing = hooks.get(event, [])
                if not isinstance(existing, list):
                    raise MemoryConfigurationError(f"hook event must be an array: {path}: {event}")
                preserved = [clean for group in existing if isinstance(group, dict) if (clean := _without_owned_hooks(group)) is not None]
                hooks[event] = [*preserved, *groups]
        if path.exists():
            backup = path.with_name(path.name + ".khenrix-backup")
            if not backup.exists():
                _atomic_private_write(backup, path.read_bytes())
        _atomic_private_json(path, document)
        results[cli] = str(path)
    return results


def hook_status() -> dict[str, Any]:
    result: dict[str, Any] = {}
    for cli in ("claude", "codex", "agy"):
        path = hook_path(cli)
        try:
            document = _read_json_document(path)
            expected = _owned_commands(canonical_hooks(cli))
            installed = expected <= _owned_commands(document)
        except MemoryConfigurationError:
            installed = False
        result[cli] = {"installed": installed, "path": str(path)}
    codex = result["codex"]
    config = account_home() / ".codex" / "config.toml"
    trust = "untrusted"
    if codex["installed"] and config.is_file():
        try:
            state = tomllib.loads(config.read_text()).get("hooks", {}).get("state", {})
            document = _read_json_document(hook_path("codex"))
            expected_keys: set[str] = set()
            for event, groups in document.get("hooks", {}).items():
                snake = re.sub(r"(?<!^)(?=[A-Z])", "_", event).lower()
                for group_index, group in enumerate(groups):
                    for handler_index, handler in enumerate(group.get("hooks", [])):
                        if _contains_owned_hook(handler):
                            expected_keys.add(
                                f"{hook_path('codex')}:{snake}:{group_index}:{handler_index}"
                            )
            if isinstance(state, dict) and expected_keys and expected_keys <= set(state):
                trust = "recorded; Codex validates each hash at launch"
        except (OSError, tomllib.TOMLDecodeError):
            pass
    codex["trust"] = trust
    return result


def backup_database() -> pathlib.Path | None:
    source = data_dir() / "claude-mem.db"
    if not source.exists():
        return None
    _assert_private_file(source)
    backups = data_dir() / "backups"
    _assert_private_directory(backups, create=True)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S%fZ")
    destination = backups / f"claude-mem-{stamp}.db"
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as origin, sqlite3.connect(destination) as target:
        origin.backup(target)
    os.chmod(destination, 0o600)
    return destination


def restore_database(backup: pathlib.Path) -> pathlib.Path:
    source = backup.expanduser().resolve()
    _assert_private_file(source)
    try:
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=2) as connection:
            row = connection.execute("PRAGMA quick_check").fetchone()
        if not row or row[0] != "ok":
            raise MemoryConfigurationError("rollback database failed its integrity check")
    except sqlite3.Error as error:
        raise MemoryConfigurationError("rollback database is invalid") from error
    was_running = worker_health()
    if was_running and stop_worker() != 0:
        raise MemoryConfigurationError("memory worker did not stop for rollback")
    if (data_dir() / "claude-mem.db").exists():
        backup_database()
    _assert_private_directory(data_dir(), create=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".restore-", suffix=".db", dir=data_dir())
    os.close(descriptor)
    temporary = pathlib.Path(temporary_name)
    try:
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as origin, sqlite3.connect(temporary) as target:
            origin.backup(target)
        os.chmod(temporary, 0o600)
        os.replace(temporary, data_dir() / "claude-mem.db")
    finally:
        temporary.unlink(missing_ok=True)
    if was_running and run_worker(["start"]) != 0:
        raise MemoryConfigurationError("database restored but memory worker did not restart")
    return data_dir() / "claude-mem.db"


def upgrade_runtime() -> dict[str, Any]:
    was_running = worker_health()
    if was_running and stop_worker() != 0:
        raise MemoryConfigurationError("memory worker did not stop for upgrade")
    backup = backup_database()
    controller = install_controller()
    runtime = stage_runtime()
    hooks = install_hooks(["claude", "codex", "agy"])
    if route_path().exists():
        route = read_route()
        _atomic_private_json(settings_path(), settings_for_route(route))
    if was_running and run_worker(["start"]) != 0:
        raise MemoryConfigurationError("memory upgrade completed but worker did not restart")
    return {
        "controller": str(controller),
        "runtime": str(runtime),
        "backup": str(backup) if backup else None,
        "hooks": hooks,
        "worker_restarted": was_running,
    }


def setup_plan(
    route: str | None,
    *,
    keychain_account: str | None,
    provider_file: pathlib.Path | None,
    start: bool,
) -> dict[str, Any]:
    selected = route
    if selected is None and route_path().is_file():
        try:
            selected = str(read_route()["route"])
        except MemoryConfigurationError:
            selected = None
    return {
        "mode": "dry-run",
        "route": selected,
        "actions": [
            {"action": "install-controller", "path": str(installed_controller_root())},
            {"action": "stage-runtime", "path": str(runtime_root()), "integrity": ARTIFACT_INTEGRITY},
            {"action": "select-route", "route": selected or "required-on-first-install"},
            {"action": "merge-hooks", "targets": [str(hook_path(cli)) for cli in ("claude", "codex", "agy")]},
            {"action": "start-services", "enabled": start},
        ],
        "keychain_account_selected": bool(keychain_account),
        "local_provider_descriptor": str(provider_file.expanduser()) if provider_file else None,
        "preserves": [str(data_dir() / "claude-mem.db"), "unrelated hook entries"],
    }


def apply_setup(
    route: str | None,
    *,
    keychain_account: str | None,
    provider_file: pathlib.Path | None,
    start: bool,
) -> dict[str, Any]:
    existing: dict[str, Any] | None = None
    if route_path().is_file():
        try:
            existing = read_route()
        except MemoryConfigurationError:
            # An explicit route is enough to replace a selector written by the
            # earlier Agentic Setup controller.  Refuse to guess only when the
            # caller has not supplied the replacement route.
            if route is None:
                raise
    if route is None and existing is None:
        raise MemoryConfigurationError("first setup requires an explicit --route")
    selected = route or str(existing["route"])
    if route is None and (keychain_account is not None or provider_file is not None):
        raise MemoryConfigurationError("route options require an explicit --route")
    install_controller()
    stage_runtime()
    if route is not None:
        select_route(
            selected,
            keychain_account=keychain_account,
            provider_file=provider_file,
            restart=False,
        )
    else:
        _atomic_private_json(settings_path(), settings_for_route(existing or {}))
    hooks = install_hooks(["claude", "codex", "agy"])
    if start and run_worker(["start"]) != 0:
        raise MemoryConfigurationError("memory worker failed to start during setup")
    return {
        "mode": "apply",
        "route": selected,
        "controller": str(installed_controller_root()),
        "runtime": str(runtime_root()),
        "hooks": hooks,
        "started": start,
    }


def storage_document() -> dict[str, Any]:
    database = data_dir() / "claude-mem.db"
    backups = list((data_dir() / "backups").glob("*.db")) if (data_dir() / "backups").is_dir() else []
    return {
        "database_bytes": database.stat().st_size if database.is_file() else 0,
        "backup_count": len(backups),
        "backup_bytes": sum(path.stat().st_size for path in backups),
        "automatic_deletion": False,
    }


def _database_problem() -> str | None:
    database = data_dir() / "claude-mem.db"
    if not database.exists():
        return None
    try:
        _assert_private_file(database)
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=2) as connection:
            row = connection.execute("PRAGMA quick_check").fetchone()
        return None if row and row[0] == "ok" else "memory database integrity check failed"
    except (MemoryConfigurationError, sqlite3.Error) as error:
        return str(error)


def health_document(*, require_running: bool) -> dict[str, Any]:
    problems: list[str] = []
    warnings: list[str] = []
    route: dict[str, Any] | None = None
    try:
        _assert_private_directory(config_dir(), create=False)
        _assert_private_directory(data_dir(), create=False)
        route = read_route()
        settings = _load_private_json(settings_path())
        for key, value in settings_for_route(route).items():
            if settings.get(key) != value:
                problems.append(f"settings mismatch: {key}")
    except MemoryConfigurationError as error:
        problems.append(str(error))
    if not _runtime_is_valid(runtime_root()):
        problems.append("pinned claude-mem runtime is not staged")
    try:
        _bun_path()
    except MemoryConfigurationError as error:
        problems.append(str(error))
    worker_up = worker_health()
    relay_required = bool(route and route.get("route") in {"openai-keychain", "codex-subscription"})
    relay_up = relay_required and _relay_health()
    gateway_up = _gateway_health()
    if require_running and not worker_up:
        problems.append("memory worker is stopped")
    if require_running and relay_required and not relay_up:
        problems.append("provider relay is stopped")
    if require_running and not gateway_up:
        problems.append("authenticated memory gateway is stopped")
    database_problem = _database_problem()
    if database_problem:
        problems.append(database_problem)
    hooks = hook_status()
    for cli, state in hooks.items():
        if not state["installed"]:
            warnings.append(f"{cli} memory hooks are not installed")
    if hooks["codex"].get("trust") == "untrusted":
        warnings.append("Codex memory hooks require one-time interactive trust")
    return {
        "ok": not problems,
        "route": route.get("route") if route else None,
        "worker": "running" if worker_up else "stopped",
        "relay": "running" if relay_up else ("stopped" if relay_required else "not-required"),
        "gateway": "running" if gateway_up else "stopped",
        "hooks": hooks,
        "storage": storage_document(),
        "problems": problems,
        "warnings": warnings,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    route = commands.add_parser("route")
    route.add_argument("route", choices=sorted(ROUTES))
    route.add_argument("--keychain-account")
    route.add_argument("--provider-file", type=pathlib.Path)
    setup = commands.add_parser("setup")
    setup.add_argument("--apply", action="store_true")
    setup.add_argument("--route", choices=sorted(ROUTES))
    setup.add_argument("--keychain-account")
    setup.add_argument("--provider-file", type=pathlib.Path)
    setup.add_argument("--start", action="store_true")
    commands.add_parser("install")
    commands.add_parser("stage")
    hooks = commands.add_parser("hooks")
    hooks.add_argument("action", choices=("install", "status"))
    hooks.add_argument("--cli", action="append", choices=("claude", "codex", "agy"))
    commands.add_parser("doctor")
    commands.add_parser("status")
    commands.add_parser("viewer")
    commands.add_parser("start")
    commands.add_parser("stop")
    commands.add_parser("restart")
    commands.add_parser("backup")
    commands.add_parser("storage")
    commands.add_parser("upgrade")
    rollback = commands.add_parser("rollback")
    rollback.add_argument("--backup", type=pathlib.Path, required=True)
    exclude = commands.add_parser("exclude")
    exclude.add_argument("action", choices=("list", "add", "remove"))
    exclude.add_argument("project", nargs="?")
    hook = commands.add_parser("hook")
    hook.add_argument("platform", choices=("claude-code", "codex", "antigravity-cli"))
    hook.add_argument("event", choices=("context", "session-init", "file-context", "observation", "summarize", "session-end"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "route":
            result = select_route(
                args.route,
                keychain_account=args.keychain_account,
                provider_file=args.provider_file,
            )
            print(json.dumps(result, sort_keys=True))
            return 0
        if args.command == "setup":
            function = apply_setup if args.apply else setup_plan
            result = function(
                args.route,
                keychain_account=args.keychain_account,
                provider_file=args.provider_file,
                start=args.start,
            )
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        if args.command == "install":
            print(install_controller())
            return 0
        if args.command == "stage":
            print(stage_runtime())
            return 0
        if args.command == "hooks":
            if args.action == "install":
                print(json.dumps(install_hooks(args.cli or ["claude", "codex", "agy"]), indent=2, sort_keys=True))
            else:
                print(json.dumps(hook_status(), indent=2, sort_keys=True))
            return 0
        if args.command == "doctor":
            document = health_document(require_running=True)
            print(json.dumps(document, indent=2, sort_keys=True))
            return 0 if document["ok"] else 1
        if args.command == "status":
            print(json.dumps(health_document(require_running=False), indent=2, sort_keys=True))
            return 0
        if args.command == "viewer":
            if not worker_health() and (run_worker(["start"]) != 0 or not worker_health()):
                raise MemoryConfigurationError("memory worker failed to start")
            _ensure_gateway()
            webbrowser.open(f"http://127.0.0.1:{GATEWAY_PORT}/?token={_gateway_token()}", new=2)
            return 0
        if args.command == "start":
            return run_worker(["start"])
        if args.command == "stop":
            return stop_worker()
        if args.command == "restart":
            stopped = stop_worker()
            return stopped if stopped else run_worker(["start"])
        if args.command == "backup":
            print(backup_database() or "no database")
            return 0
        if args.command == "storage":
            print(json.dumps(storage_document(), indent=2, sort_keys=True))
            return 0
        if args.command == "upgrade":
            print(json.dumps(upgrade_runtime(), indent=2, sort_keys=True))
            return 0
        if args.command == "rollback":
            print(restore_database(args.backup))
            return 0
        if args.command == "exclude":
            if args.action == "list":
                if args.project is not None:
                    raise MemoryConfigurationError("exclude list takes no project")
                projects = excluded_projects()
            else:
                if args.project is None:
                    raise MemoryConfigurationError(f"exclude {args.action} requires a project")
                projects = update_excluded_project(args.project, remove=args.action == "remove")
            print(json.dumps({"projects": projects}, indent=2, sort_keys=True))
            return 0
        if args.command == "hook":
            return run_worker(["hook", args.platform, args.event])
    except MemoryConfigurationError as error:
        print(f"memory: {error}", file=sys.stderr)
        return 2
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
