#!/usr/bin/env python3
"""Run Eval with a decoy credential while brokering the real key in memory."""

from __future__ import annotations

import base64
import os
import pathlib
import resource
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.parse

SOCKET_PATH = pathlib.Path("/run/maka-secret/key.sock")
MAX_KEY_BYTES = 16 * 1024
SCAN_CHUNK_BYTES = 1024 * 1024
MAX_SCAN_FILE_BYTES = 64 * 1024 * 1024
MAX_SCAN_TOTAL_BYTES = 512 * 1024 * 1024
MAX_SCAN_ENTRIES = 100_000
MAX_SCAN_SECONDS = 60
PROVIDER_KEYS = {
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "GEMINI_API_KEY",
    "GOOGLE_API_KEY", "GOOGLE_GENERATIVE_AI_API_KEY", "XAI_API_KEY", "GROQ_API_KEY",
    "MISTRAL_API_KEY", "COHERE_API_KEY", "TOGETHER_API_KEY", "OPENROUTER_API_KEY",
}


def read_key() -> bytearray:
    raw = sys.stdin.buffer.readline(MAX_KEY_BYTES)
    overflow = sys.stdin.buffer.read(1)
    value = bytearray(raw.rstrip(b"\r\n"))
    raw = b""
    if overflow or not value or len(value) >= MAX_KEY_BYTES or 0 in value:
        wipe(value)
        raise SystemExit("Codex Auth did not provide one valid bounded API key")
    return value


def wipe(value: bytearray) -> None:
    for index in range(len(value)):
        value[index] = 0


def broker(
    key: bytearray,
    ready: threading.Event,
    served: threading.Event,
    stop: threading.Event,
    failures: list[str],
) -> None:
    SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    listener: socket.socket | None = None
    try:
        SOCKET_PATH.unlink(missing_ok=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(SOCKET_PATH))
        os.chmod(SOCKET_PATH, 0o600)
        listener.listen(1)
        listener.settimeout(0.25)
        ready.set()
        deadline = time.monotonic() + 180
        while not stop.is_set() and time.monotonic() < deadline:
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            with connection:
                connection.sendall(key)
            served.set()
            wipe(key)
            return
    except Exception as error:
        failures.append(type(error).__name__)
    finally:
        ready.set()
        if listener is not None:
            listener.close()
        SOCKET_PATH.unlink(missing_ok=True)


def secret_variants(key: bytes) -> set[bytes]:
    standard_b64 = base64.b64encode(key)
    urlsafe_b64 = base64.urlsafe_b64encode(key)
    percent_upper = b"".join(f"%{byte:02X}".encode() for byte in key)
    percent_lower = percent_upper.lower()
    return {
        key,
        standard_b64,
        standard_b64.rstrip(b"="),
        urlsafe_b64,
        urlsafe_b64.rstrip(b"="),
        key.hex().encode(),
        key.hex().upper().encode(),
        urllib.parse.quote_from_bytes(key, safe="").encode(),
        percent_upper,
        percent_lower,
    }


def scan_retained(root: pathlib.Path, key: bytes) -> list[str]:
    variants = sorted(secret_variants(key), key=len, reverse=True)
    overlap = max(map(len, variants)) - 1
    leaked: list[str] = []
    scanned_bytes = 0
    scanned_entries = 0
    deadline = time.monotonic() + MAX_SCAN_SECONDS
    if not root.is_dir():
        return leaked
    for path in root.rglob("*"):
        scanned_entries += 1
        if scanned_entries > MAX_SCAN_ENTRIES or time.monotonic() > deadline:
            raise OSError("retained evidence exceeded the credential scan work bound")
        metadata = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode):
            continue
        if metadata.st_size > MAX_SCAN_FILE_BYTES:
            raise OSError("retained evidence exceeded the credential scan bound")
        matched = False
        tail = b""
        file_bytes = 0
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
            ):
                raise OSError("retained evidence changed before credential scanning")
            while True:
                remaining_file = MAX_SCAN_FILE_BYTES - file_bytes
                remaining_total = MAX_SCAN_TOTAL_BYTES - scanned_bytes - file_bytes
                chunk = stream.read(min(SCAN_CHUNK_BYTES, remaining_file + 1, remaining_total + 1))
                if not chunk:
                    break
                file_bytes += len(chunk)
                if file_bytes > MAX_SCAN_FILE_BYTES or scanned_bytes + file_bytes > MAX_SCAN_TOTAL_BYTES:
                    raise OSError("retained evidence grew beyond the credential scan bound")
                window = tail + chunk
                if any(variant in window for variant in variants):
                    matched = True
                tail = window[-overlap:]
                if time.monotonic() > deadline:
                    raise OSError("retained evidence exceeded the credential scan deadline")
            finished = os.fstat(stream.fileno())
        if (
            finished.st_size != file_bytes
            or finished.st_mtime_ns != opened.st_mtime_ns
            or finished.st_ctime_ns != opened.st_ctime_ns
        ):
            raise OSError("retained evidence changed during credential scanning")
        scanned_bytes += file_bytes
        if not matched:
            continue
        # The second pass is capped and must observe the same file revision.
        with path.open("rb") as stream:
            reopened = os.fstat(stream.fileno())
            if (
                (reopened.st_dev, reopened.st_ino) != (finished.st_dev, finished.st_ino)
                or reopened.st_size != file_bytes
                or reopened.st_mtime_ns != finished.st_mtime_ns
                or reopened.st_ctime_ns != finished.st_ctime_ns
            ):
                raise OSError("retained evidence changed before credential redaction")
            data = stream.read(MAX_SCAN_FILE_BYTES + 1)
            reread = os.fstat(stream.fileno())
        if (
            len(data) != file_bytes
            or len(data) > MAX_SCAN_FILE_BYTES
            or reread.st_size != file_bytes
            or reread.st_mtime_ns != reopened.st_mtime_ns
            or reread.st_ctime_ns != reopened.st_ctime_ns
        ):
            raise OSError("retained evidence changed during credential redaction")
        for variant in variants:
            data = data.replace(variant, b"[REDACTED-EPHEMERAL-KEY]")
        path.write_bytes(data)
        leaked.append(str(path.relative_to(root)))
    return leaked


def quarantine_run_evidence(root: pathlib.Path) -> bool:
    lab_text = os.environ.get("MAKA_LAB_ROOT")
    if not lab_text:
        return False
    base = (pathlib.Path(lab_text) / "evidence" / "runs").resolve()
    candidate = pathlib.Path(os.path.abspath(root))
    if candidate.parent != base or candidate == base:
        return False
    try:
        if candidate.is_symlink():
            candidate.unlink()
        elif candidate.exists():
            shutil.rmtree(candidate)
    except OSError:
        return False
    return not candidate.exists() and not candidate.is_symlink()


def main() -> int:
    if len(sys.argv) < 2:
        raise SystemExit("usage: secret-broker-exec.py <command> [args...]")
    if resource.getrlimit(resource.RLIMIT_CORE) != (0, 0):
        raise SystemExit("OpenAI credential broker requires a zero core-dump limit")
    key = read_key()
    scan_key = bytes(key)
    ready = threading.Event()
    served = threading.Event()
    stop = threading.Event()
    failures: list[str] = []
    worker: threading.Thread | None = None
    child: subprocess.Popen[bytes] | None = None
    previous: dict[int, object] = {}
    status = 78
    launch_failed = False
    leaked: list[str] = []
    scan_failed = False

    def forward(host_signal: int, _frame: object) -> None:
        if child is not None and child.poll() is None:
            child.send_signal(host_signal)

    try:
        worker = threading.Thread(
            target=broker,
            args=(key, ready, served, stop, failures),
            daemon=True,
        )
        worker.start()
        if not ready.wait(5) or failures:
            raise RuntimeError("credential broker startup failed")
        environment = {
            name: value
            for name, value in os.environ.items()
            if name not in PROVIDER_KEYS and not name.endswith("_API_KEY")
        }
        environment["OPENAI_API_KEY"] = f"maka-decoy-{secrets.token_urlsafe(32)}"
        child = subprocess.Popen(sys.argv[1:], env=environment)
        previous = {
            host_signal: signal.signal(host_signal, forward)
            for host_signal in (signal.SIGINT, signal.SIGTERM)
        }
        status = child.wait()
    except (OSError, RuntimeError, subprocess.SubprocessError):
        launch_failed = True
    finally:
        for host_signal, handler in previous.items():
            signal.signal(host_signal, handler)
        try:
            if child is not None and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    failures.append("EvalChildDidNotTerminate")
                    try:
                        child.kill()
                        child.wait(timeout=5)
                    except (OSError, subprocess.TimeoutExpired):
                        failures.append("EvalChildDidNotDieAfterKill")
        finally:
            # Broker shutdown, retained-output scanning, and key wiping must run
            # even when the evaluated child cannot be reaped cleanly.
            stop.set()
            if worker is not None:
                worker.join(timeout=2)
                if worker.is_alive():
                    failures.append("BrokerThreadDidNotStop")
            evidence = pathlib.Path(os.environ.get("MAKA_EVIDENCE_DIR", "/nonexistent"))
            try:
                leaked = scan_retained(evidence, scan_key)
            except Exception:
                scan_failed = True
            finally:
                wipe(key)
                scan_key = b""

    if scan_failed:
        if quarantine_run_evidence(evidence):
            print("credential evidence scan failed; the run directory was quarantined", file=sys.stderr)
            return 79
        print("credential evidence scan failed and broker quarantine failed", file=sys.stderr)
        return 80
    if leaked:
        print("credential material reached retained evidence and was redacted", file=sys.stderr)
        return 78
    if launch_failed:
        print("credential broker could not launch the eval child", file=sys.stderr)
        return 78
    if failures:
        print("credential broker failed after startup", file=sys.stderr)
        return 78
    if status == 0 and not served.is_set():
        print("credential broker was never consumed", file=sys.stderr)
        return 78
    return status


if __name__ == "__main__":
    raise SystemExit(main())
