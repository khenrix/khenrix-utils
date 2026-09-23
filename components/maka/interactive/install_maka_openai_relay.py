#!/usr/bin/env python3
"""Install the inert-at-rest user service for the interactive Maka relay.

This script changes live state only when invoked with the explicit ``install``
subcommand.  The generated LaunchAgent contains paths and a port, never the
caller token or the real OpenAI credential.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import http.client
import hmac
import json
import os
import pathlib
import plistlib
import pwd
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass

INTERACTIVE_ROOT = pathlib.Path(__file__).resolve().parent
SCRIPTS_ROOT = INTERACTIVE_ROOT.parent / "scripts"
for local_root in (INTERACTIVE_ROOT, SCRIPTS_ROOT):
    if str(local_root) not in sys.path:
        sys.path.insert(0, str(local_root))

from harden_python_runtime import RuntimeHardeningError, assert_current_runtime
from maka_openai_relay import (
    DEFAULT_PORT,
    HEALTH_CHALLENGE_HEADER,
    KeychainOpenAIKey,
    LOOPBACK_HOST,
    MODEL_IDS,
    ProviderCredentialError,
    RelayConfigurationError,
    health_challenge_proof,
    read_keychain_account as read_relay_keychain_account,
    read_private_token,
)
from maka_auth_mode import (
    AuthModeError,
    invalidate_relay_ready,
    read_keychain_account,
    read_mode,
    write_keychain_account,
    write_relay_ready,
)
for local_root in (INTERACTIVE_ROOT, SCRIPTS_ROOT):
    with contextlib.suppress(ValueError):
        sys.path.remove(str(local_root))


LABEL = "dev.khenrix.maka-openai-relay"
SERVICE_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,126}[A-Za-z0-9])?$")


@dataclass(frozen=True)
class InstallPaths:
    lab_root: pathlib.Path
    script: pathlib.Path
    configure_script: pathlib.Path
    config_directory: pathlib.Path
    install_lock_file: pathlib.Path
    token_file: pathlib.Path
    attestation_file: pathlib.Path
    launch_agent: pathlib.Path
    auth_mode_file: pathlib.Path
    keychain_account_file: pathlib.Path
    relay_ready_file: pathlib.Path


def default_paths(home: pathlib.Path | None = None) -> InstallPaths:
    account_home = home or pathlib.Path.home()
    lab_root = account_home / ".local" / "share" / "khenrix-utils" / "maka"
    interactive = lab_root / "interactive"
    return InstallPaths(
        lab_root=lab_root,
        script=interactive / "maka_openai_relay.py",
        configure_script=interactive / "configure_maka_openai_relay.mjs",
        config_directory=account_home / ".config" / "khenrix-utils" / "maka" / "relay",
        install_lock_file=account_home
        / ".config"
        / "khenrix-utils"
        / "maka"
        / "relay"
        / "install.lock",
        token_file=account_home
        / ".config"
        / "khenrix-utils"
        / "maka"
        / "relay"
        / "caller-token",
        attestation_file=account_home
        / ".config"
        / "khenrix-utils"
        / "maka"
        / "relay"
        / "relay-attestation",
        launch_agent=account_home
        / "Library"
        / "LaunchAgents"
        / f"{LABEL}.plist",
        auth_mode_file=account_home
        / ".config"
        / "khenrix-utils"
        / "maka"
        / "maka-auth-mode",
        keychain_account_file=account_home
        / ".config"
        / "khenrix-utils"
        / "maka"
        / "maka-openai-keychain-account",
        relay_ready_file=account_home
        / ".config"
        / "khenrix-utils"
        / "maka"
        / "maka-api-key-relay-ready",
    )


def legacy_service_paths(paths: InstallPaths, label: str) -> tuple[pathlib.Path, str]:
    """Resolve an explicitly supplied legacy label without a company default."""

    if label == LABEL or SERVICE_LABEL_PATTERN.fullmatch(label) is None:
        raise RelayConfigurationError("legacy LaunchAgent label is invalid")
    launch_agent = paths.launch_agent.parent / f"{label}.plist"
    return launch_agent, f"gui/{os.getuid()}/{label}"


def assert_api_key_relay_mode(paths: InstallPaths) -> None:
    """Refuse destructive relay reconciliation on a subscription machine."""

    try:
        mode = read_mode(paths.auth_mode_file)
    except (AuthModeError, OSError) as error:
        raise RelayConfigurationError("Maka API-key relay mode is not selected") from error
    if mode != "api-key-relay":
        raise RelayConfigurationError("Maka API-key relay mode is not selected")


@contextlib.contextmanager
def exclusive_install_lock(paths: InstallPaths):
    """Serialize account selection, LaunchAgent replacement, and marker publication."""

    paths.config_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = paths.config_directory.lstat()
    if (
        not stat.S_ISDIR(directory.st_mode)
        or stat.S_ISLNK(directory.st_mode)
        or directory.st_uid != os.getuid()
        or directory.st_mode & 0o077
    ):
        raise RelayConfigurationError("relay config directory is not private")
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(paths.install_lock_file, flags, 0o600)
    except OSError as error:
        raise RelayConfigurationError("relay installer lock is unavailable") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077
            or metadata.st_size != 0
        ):
            raise RelayConfigurationError("relay installer lock is unsafe")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RelayConfigurationError("another relay installation is already running") from error
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def select_keychain_account(
    paths: InstallPaths,
    requested: str | None,
    *,
    replace: bool = False,
) -> str:
    """Validate one exact item, then persist its non-secret account ID."""

    selector_exists = paths.keychain_account_file.exists() or paths.keychain_account_file.is_symlink()
    current: str | None = None
    if selector_exists:
        try:
            current = read_keychain_account(paths.keychain_account_file)
        except (AuthModeError, OSError) as error:
            raise RelayConfigurationError("Maka Keychain account selector is invalid") from error
    if requested is None:
        if current is None:
            raise RelayConfigurationError("an explicit Keychain account is required")
        candidate = current
    else:
        candidate = requested
        if current is not None and candidate != current and not replace:
            raise RelayConfigurationError(
                "changing the Keychain account requires explicit replacement"
            )
    try:
        provider_key = KeychainOpenAIKey(candidate)()
    except (ProviderCredentialError, RelayConfigurationError, OSError) as error:
        raise RelayConfigurationError("the selected Keychain item is unavailable") from error
    del provider_key
    if current != candidate:
        try:
            write_keychain_account(
                candidate,
                paths.keychain_account_file,
                replace=current is not None and replace,
            )
        except (AuthModeError, OSError) as error:
            raise RelayConfigurationError("Maka Keychain account could not be selected") from error
    return candidate


def assert_private_install_artifacts(paths: InstallPaths) -> None:
    """Verify every local relay file before publishing the readiness marker."""

    directory = paths.config_directory.lstat()
    if (
        not stat.S_ISDIR(directory.st_mode)
        or stat.S_ISLNK(directory.st_mode)
        or directory.st_uid != os.getuid()
        or directory.st_mode & 0o077
    ):
        raise RelayConfigurationError("relay config directory is not private")
    for path in (
        paths.token_file,
        paths.attestation_file,
        paths.keychain_account_file,
        paths.launch_agent,
    ):
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077
        ):
            raise RelayConfigurationError("relay installation file is not private")
    read_private_token(paths.token_file)
    read_private_token(paths.attestation_file)
    read_relay_keychain_account(paths.keychain_account_file)


def build_launch_agent(
    paths: InstallPaths,
    python_executable: pathlib.Path,
    port: int = DEFAULT_PORT,
) -> dict[str, object]:
    if not python_executable.is_absolute():
        raise RelayConfigurationError("Python executable must be absolute")
    if not 1 <= port <= 65_535:
        raise RelayConfigurationError("relay port is invalid")
    return {
        "Label": LABEL,
        "ProgramArguments": [
            "/usr/bin/env",
            "-i",
            "PATH=/usr/bin:/bin",
            "LANG=C",
            str(python_executable),
            "-I",
            "-S",
            "-B",
            str(paths.script),
            "serve",
            "--token-file",
            str(paths.token_file),
            "--attestation-file",
            str(paths.attestation_file),
            "--keychain-account-file",
            str(paths.keychain_account_file),
            "--relay-ready-file",
            str(paths.relay_ready_file),
            "--port",
            str(port),
        ],
        "WorkingDirectory": str(paths.script.parent),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Interactive",
        "ThrottleInterval": 5,
        "Umask": 0o077,
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
    }


def ensure_private_token(paths: InstallPaths) -> str:
    paths.config_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory_metadata = paths.config_directory.lstat()
    if not stat.S_ISDIR(directory_metadata.st_mode):
        raise RelayConfigurationError("relay config path is not a directory")
    if directory_metadata.st_uid != os.getuid():
        raise RelayConfigurationError("relay config directory has the wrong owner")
    paths.config_directory.chmod(0o700)
    if not paths.token_file.exists():
        token = secrets.token_urlsafe(48)
        descriptor = os.open(
            paths.token_file,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            os.write(descriptor, f"{token}\n".encode("ascii"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return read_private_token(paths.token_file)


def atomic_write_plist(path: pathlib.Path, document: dict[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise RelayConfigurationError("LaunchAgents path is a symlink")
    payload = plistlib.dumps(document, fmt=plistlib.FMT_XML, sort_keys=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = pathlib.Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def run_launchctl(arguments: list[str], *, allow_failure: bool = False) -> bool:
    completed = subprocess.run(
        ["/bin/launchctl", *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        close_fds=True,
    )
    if completed.returncode != 0 and not allow_failure:
        raise RelayConfigurationError("LaunchAgent operation failed")
    return completed.returncode == 0


def wait_until_unloaded(service: str, timeout_seconds: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        completed = subprocess.run(
            ["/bin/launchctl", "print", service],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            close_fds=True,
        )
        if completed.returncode != 0:
            return
        time.sleep(0.05)
    raise RelayConfigurationError("previous LaunchAgent did not unload")


def _read_attestation(path: pathlib.Path) -> str | None:
    try:
        return read_private_token(path)
    except RelayConfigurationError:
        return None


def _get_local_json(
    port: int,
    path: str,
    headers: dict[str, str],
) -> tuple[int, object]:
    connection = http.client.HTTPConnection(LOOPBACK_HOST, port, timeout=1.0)
    try:
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        body = response.read(64 * 1024)
        return response.status, json.loads(body)
    finally:
        connection.close()


def wait_until_ready(
    token: str,
    attestation_file: pathlib.Path,
    port: int,
    timeout_seconds: float = 10.0,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            expected_attestation = _read_attestation(attestation_file)
            if expected_attestation is None:
                time.sleep(0.1)
                continue
            challenge = secrets.token_urlsafe(32)
            status, health = _get_local_json(
                port,
                "/healthz",
                {
                    "Host": f"{LOOPBACK_HOST}:{port}",
                    HEALTH_CHALLENGE_HEADER: challenge,
                    "Connection": "close",
                },
            )
            expected_proof = health_challenge_proof(expected_attestation, challenge)
            if (
                status != 200
                or not isinstance(health, dict)
                or health.get("status") != "ready"
                or not isinstance(health.get("proof"), str)
                or not hmac.compare_digest(health["proof"], expected_proof)
            ):
                time.sleep(0.1)
                continue
            status, document = _get_local_json(
                port,
                "/v1/models",
                {
                    "Host": f"{LOOPBACK_HOST}:{port}",
                    "Authorization": f"Bearer {token}",
                    "Connection": "close",
                },
            )
            if (
                status == 200
                and isinstance(document, dict)
                and [row.get("id") for row in document.get("data", [])] == list(MODEL_IDS)
            ):
                return
        except (OSError, http.client.HTTPException, json.JSONDecodeError, IndexError, TypeError):
            pass
        time.sleep(0.1)
    raise RelayConfigurationError("relay did not become ready")


def resolve_mise_binary(
    home: pathlib.Path,
    candidates: tuple[pathlib.Path, ...] | None = None,
) -> pathlib.Path:
    admitted = candidates or (
        home / ".local" / "bin" / "mise",
        pathlib.Path("/opt/homebrew/bin/mise"),
        pathlib.Path("/usr/local/bin/mise"),
    )
    located = shutil.which("mise")
    ordered = ([pathlib.Path(located)] if located and candidates is None else []) + list(admitted)
    for candidate in dict.fromkeys(ordered):
        if candidate not in admitted:
            continue
        try:
            entry = candidate.lstat()
            target = candidate.resolve(strict=True)
            metadata = target.stat()
        except OSError:
            continue
        if (
            not (stat.S_ISREG(entry.st_mode) or stat.S_ISLNK(entry.st_mode))
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid not in {0, os.getuid()}
            or metadata.st_mode & 0o022
            or not os.access(candidate, os.X_OK)
        ):
            continue
        return candidate
    raise RelayConfigurationError("mise is unavailable at an admitted standard path")


def configure_maka(paths: InstallPaths, port: int, *, purge_credentials: bool) -> None:
    account = pwd.getpwuid(os.getuid())
    account_home = pathlib.Path(account.pw_dir)
    mise = resolve_mise_binary(account_home)
    environment = {
        "HOME": str(account_home),
        "USER": account.pw_name,
        "LOGNAME": account.pw_name,
        "PATH": f"{mise.parent}:/usr/bin:/bin",
        "LANG": "C",
        "LC_ALL": "C",
    }
    package_root = discover_maka_package_root(paths, mise, environment)
    arguments = [
            str(mise),
            "-C",
            str(paths.lab_root),
            "exec",
            "--",
            "node",
            str(paths.configure_script),
            "--token-file",
            str(paths.token_file),
            "--attestation-file",
            str(paths.attestation_file),
            "--port",
            str(port),
            "--package-root",
            str(package_root),
        ]
    if purge_credentials:
        arguments.append("--purge-credentials")
    completed = subprocess.run(
        arguments,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        close_fds=True,
        env=environment,
        text=True,
        timeout=90,
    )
    if completed.returncode != 0:
        raise RelayConfigurationError("Maka relay policy configuration failed")


def discover_maka_package_root(
    paths: InstallPaths,
    mise: pathlib.Path,
    environment: dict[str, str],
) -> pathlib.Path:
    completed = subprocess.run(
        [str(mise), "-C", str(paths.lab_root), "which", "maka"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        close_fds=True,
        env=environment,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RelayConfigurationError("pinned Maka executable is unavailable")
    shim = pathlib.Path(completed.stdout.strip())
    if not shim.is_absolute() or not shim.is_file():
        raise RelayConfigurationError("pinned Maka shim is invalid")
    source = shim.read_text(encoding="utf-8")
    match = re.search(r"^# aube-bin-shim v2 target=([^\s]+)$", source, re.MULTILINE)
    if match is None:
        raise RelayConfigurationError("pinned Maka shim is unrecognized")
    target = (shim.parent / match.group(1)).resolve()
    package_root = target.parent.parent
    try:
        manifest = json.loads((package_root / "package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RelayConfigurationError("pinned Maka package is invalid") from error
    if manifest.get("name") != "maka-agent":
        raise RelayConfigurationError("pinned Maka package is invalid")
    return package_root


def install(
    port: int,
    python_executable: pathlib.Path,
    keychain_account: str | None = None,
    *,
    replace_keychain_account: bool = False,
    replace_legacy_service: bool = False,
    legacy_service_label: str | None = None,
    purge_profile_credentials: bool = True,
) -> None:
    if sys.platform != "darwin":
        raise RelayConfigurationError("the LaunchAgent installer requires macOS")
    paths = default_paths()
    assert_api_key_relay_mode(paths)
    trusted_python = assert_current_runtime()
    try:
        if python_executable.resolve(strict=True) != trusted_python.resolve(strict=True):
            raise RelayConfigurationError("LaunchAgent Python is not the hardened pinned runtime")
    except OSError as error:
        raise RelayConfigurationError("LaunchAgent Python is unavailable") from error
    with exclusive_install_lock(paths):
        _install_locked(
            paths,
            port,
            python_executable,
            keychain_account,
            replace_keychain_account=replace_keychain_account,
            replace_legacy_service=replace_legacy_service,
            legacy_service_label=legacy_service_label,
            purge_profile_credentials=purge_profile_credentials,
        )


def _install_locked(
    paths: InstallPaths,
    port: int,
    python_executable: pathlib.Path,
    keychain_account: str | None,
    *,
    replace_keychain_account: bool,
    replace_legacy_service: bool,
    legacy_service_label: str | None,
    purge_profile_credentials: bool,
) -> None:
    if replace_legacy_service != (legacy_service_label is not None):
        raise RelayConfigurationError(
            "legacy service replacement requires both explicit migration options"
        )
    select_keychain_account(
        paths,
        keychain_account,
        replace=replace_keychain_account,
    )
    try:
        invalidate_relay_ready(paths.relay_ready_file)
    except (AuthModeError, OSError) as error:
        raise RelayConfigurationError("relay readiness could not be invalidated") from error
    for required in (paths.script, paths.configure_script, paths.lab_root / "mise.toml"):
        if not required.is_file():
            raise RelayConfigurationError("the pinned Maka lab is incomplete")
    token = ensure_private_token(paths)
    domain = f"gui/{os.getuid()}"
    service = f"{domain}/{LABEL}"
    legacy_launch_agent: pathlib.Path | None = None
    legacy_service: str | None = None
    legacy_loaded = False
    if replace_legacy_service:
        assert legacy_service_label is not None
        legacy_launch_agent, legacy_service = legacy_service_paths(
            paths, legacy_service_label
        )
        try:
            metadata = legacy_launch_agent.lstat()
        except OSError as error:
            raise RelayConfigurationError("legacy LaunchAgent is unavailable") from error
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077
        ):
            raise RelayConfigurationError("legacy LaunchAgent is unsafe")
        legacy_loaded = run_launchctl(["print", legacy_service], allow_failure=True)
        run_launchctl(["bootout", legacy_service], allow_failure=True)
        wait_until_unloaded(legacy_service)
    try:
        document = build_launch_agent(paths, python_executable, port)
        atomic_write_plist(paths.launch_agent, document)
        run_launchctl(["bootout", service], allow_failure=True)
        wait_until_unloaded(service)
        run_launchctl(["bootstrap", domain, str(paths.launch_agent)])
        run_launchctl(["kickstart", service])
        wait_until_ready(token, paths.attestation_file, port)
        configure_maka(paths, port, purge_credentials=purge_profile_credentials)
        wait_until_ready(token, paths.attestation_file, port)
        assert_private_install_artifacts(paths)
        try:
            write_relay_ready(paths.relay_ready_file, paths.keychain_account_file)
        except (AuthModeError, OSError) as error:
            raise RelayConfigurationError("relay readiness could not be recorded") from error
    except Exception:
        run_launchctl(["bootout", service], allow_failure=True)
        if legacy_loaded:
            assert legacy_launch_agent is not None and legacy_service is not None
            run_launchctl(["bootstrap", domain, str(legacy_launch_agent)])
            run_launchctl(["kickstart", legacy_service])
        raise
    print("Installed the authenticated local Maka OpenAI relay.")


def render(port: int, python_executable: pathlib.Path) -> None:
    """Render the non-secret plist for review without changing live state."""

    document = build_launch_agent(default_paths(), python_executable, port)
    sys.stdout.buffer.write(plistlib.dumps(document, fmt=plistlib.FMT_XML, sort_keys=True))


def restore_legacy_service(legacy_service_label: str) -> None:
    """Stop the Khenrix relay and restart the untouched legacy LaunchAgent."""

    if sys.platform != "darwin":
        raise RelayConfigurationError("the LaunchAgent rollback requires macOS")
    paths = default_paths()
    legacy_launch_agent, legacy_service = legacy_service_paths(
        paths, legacy_service_label
    )
    metadata = legacy_launch_agent.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o077
    ):
        raise RelayConfigurationError("legacy LaunchAgent is unsafe")
    domain = f"gui/{os.getuid()}"
    service = f"{domain}/{LABEL}"
    run_launchctl(["bootout", service], allow_failure=True)
    wait_until_unloaded(service)
    run_launchctl(["bootout", legacy_service], allow_failure=True)
    wait_until_unloaded(legacy_service)
    run_launchctl(["bootstrap", domain, str(legacy_launch_agent)])
    run_launchctl(["kickstart", legacy_service])
    print("Restored the legacy Maka OpenAI relay service.")


def parse_args(arguments: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    commands: dict[str, argparse.ArgumentParser] = {}
    for name in ("render", "install"):
        command = subcommands.add_parser(name)
        commands[name] = command
        command.add_argument("--port", type=int, default=DEFAULT_PORT)
        command.add_argument(
            "--python",
            type=pathlib.Path,
            default=pathlib.Path(sys.executable).resolve(),
        )
    restore_legacy = subcommands.add_parser("restore-legacy")
    restore_legacy.add_argument(
        "--legacy-service-label",
        required=True,
        help="exact validated label of the local LaunchAgent to restore",
    )
    commands["install"].add_argument(
        "--keychain-account",
        help="exact non-secret Account field for the existing Codex Auth Keychain item",
    )
    commands["install"].add_argument(
        "--replace-legacy-service",
        action="store_true",
        help="atomically replace the former Agentic Setup LaunchAgent and restore it on failure",
    )
    commands["install"].add_argument(
        "--legacy-service-label",
        help="exact validated label of the local LaunchAgent being replaced",
    )
    commands["install"].add_argument(
        "--preserve-profile-credentials",
        action="store_true",
        help="skip the explicit broad credential purge during a reviewed legacy migration",
    )
    commands["install"].add_argument(
        "--replace-keychain-account",
        action="store_true",
        help="explicitly replace an existing account selector after validating the new item",
    )
    return parser.parse_args(arguments)


def main(arguments: list[str] | None = None) -> int:
    os.umask(0o077)
    options = parse_args(arguments)
    try:
        if options.command == "render":
            render(options.port, options.python)
        elif options.command == "install":
            install(
                options.port,
                options.python,
                options.keychain_account,
                replace_keychain_account=options.replace_keychain_account,
                replace_legacy_service=options.replace_legacy_service,
                legacy_service_label=options.legacy_service_label,
                purge_profile_credentials=not options.preserve_profile_credentials,
            )
        elif options.command == "restore-legacy":
            restore_legacy_service(options.legacy_service_label)
        else:
            return 64
    except (OSError, RelayConfigurationError, RuntimeHardeningError, subprocess.SubprocessError):
        print("Maka relay installation failed safely.", file=sys.stderr)
        return 78
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
