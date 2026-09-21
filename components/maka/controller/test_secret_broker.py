#!/usr/bin/env python3
"""Credential-free regression tests for broker cleanup and quarantine."""

from __future__ import annotations

import importlib.util
import contextlib
import io
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile


module_path = Path(__file__).with_name("secret-broker-exec.py")
spec = importlib.util.spec_from_file_location("maka_secret_broker", module_path)
broker = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = broker
spec.loader.exec_module(broker)


class StubbornChild:
    """Fail every wait so the outer cleanup path must still scan and wipe."""

    def __init__(self) -> None:
        self.terminated = False
        self.killed = False

    def poll(self) -> None:
        return None

    def wait(self, timeout: int | None = None) -> int:
        if timeout is None:
            raise subprocess.SubprocessError("synthetic child wait failure")
        raise subprocess.TimeoutExpired("synthetic-child", timeout)

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def send_signal(self, _host_signal: int) -> None:
        pass


with tempfile.TemporaryDirectory() as temp_text:
    temp = Path(temp_text)
    evidence = temp / "evidence" / "runs" / "stubborn-child"
    evidence.mkdir(parents=True)
    os.environ["MAKA_LAB_ROOT"] = str(temp)
    os.environ["MAKA_EVIDENCE_DIR"] = str(evidence)
    synthetic_key = bytearray(b"synthetic-broker-cleanup-key")
    child = StubbornChild()
    scan_calls: list[bytes] = []
    real_scan_retained = broker.scan_retained

    def fake_broker(
        _key: bytearray,
        ready: object,
        _served: object,
        stop: object,
        _failures: list[str],
    ) -> None:
        ready.set()
        stop.wait(10)

    def fake_scan(_root: Path, key: bytes) -> list[str]:
        scan_calls.append(key)
        return []

    broker.read_key = lambda: synthetic_key
    broker.broker = fake_broker
    broker.scan_retained = fake_scan
    broker.resource.getrlimit = lambda _resource: (0, 0)
    broker.subprocess.Popen = lambda *_args, **_kwargs: child
    broker.signal.signal = lambda _host_signal, _handler: signal.SIG_DFL
    broker.sys.argv = [str(module_path), "synthetic-child"]

    error_output = io.StringIO()
    with contextlib.redirect_stderr(error_output):
        status = broker.main()
    assert status == 78
    assert "could not launch" in error_output.getvalue()
    assert child.terminated and child.killed
    assert scan_calls == [b"synthetic-broker-cleanup-key"]
    assert synthetic_key == bytearray(len(synthetic_key))
    broker.scan_retained = real_scan_retained

    quarantined = temp / "evidence" / "runs" / "quarantine-me"
    quarantined.mkdir()
    (quarantined / "output").write_text("synthetic")
    assert broker.quarantine_run_evidence(quarantined.resolve())
    assert not quarantined.exists()
    outside = temp / "outside"
    outside.mkdir()
    assert not broker.quarantine_run_evidence(outside.resolve())
    assert outside.is_dir()

    oversized = temp / "evidence" / "runs" / "oversized"
    oversized.mkdir()
    with (oversized / "sparse").open("wb") as stream:
        stream.truncate(broker.MAX_SCAN_FILE_BYTES + 1)
    try:
        broker.scan_retained(oversized, b"synthetic")
    except OSError:
        pass
    else:
        raise AssertionError("oversized retained evidence did not fail closed")

    growing = temp / "evidence" / "runs" / "grow-after-stat"
    growing.mkdir()
    growing_file = growing / "output"
    growing_file.write_bytes(b"initial")
    original_fstat = broker.os.fstat
    original_file_limit = broker.MAX_SCAN_FILE_BYTES
    original_total_limit = broker.MAX_SCAN_TOTAL_BYTES
    grew = [False]

    def grow_after_open(file_descriptor: int):
        result = original_fstat(file_descriptor)
        if not grew[0]:
            grew[0] = True
            growing_file.write_bytes(b"x" * 2_048)
        return result

    broker.os.fstat = grow_after_open
    broker.MAX_SCAN_FILE_BYTES = 1_024
    broker.MAX_SCAN_TOTAL_BYTES = 4_096
    try:
        broker.scan_retained(growing, b"synthetic")
    except OSError:
        pass
    else:
        raise AssertionError("grow-after-stat evidence did not fail closed")
    finally:
        broker.os.fstat = original_fstat
        broker.MAX_SCAN_FILE_BYTES = original_file_limit
        broker.MAX_SCAN_TOTAL_BYTES = original_total_limit

    too_many = temp / "evidence" / "runs" / "too-many-entries"
    too_many.mkdir()
    (too_many / "one").touch()
    (too_many / "two").touch()
    original_entry_limit = broker.MAX_SCAN_ENTRIES
    broker.MAX_SCAN_ENTRIES = 1
    try:
        broker.scan_retained(too_many, b"synthetic")
    except OSError:
        pass
    else:
        raise AssertionError("retained evidence entry limit did not fail closed")
    finally:
        broker.MAX_SCAN_ENTRIES = original_entry_limit

print("Secret broker cleanup and quarantine tests passed")
