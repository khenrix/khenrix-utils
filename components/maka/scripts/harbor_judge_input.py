#!/usr/bin/env python3
"""Bounded in-memory ballot transport for the Harbor judge FIFO."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import pathlib
import re
import signal
import stat
import sys


MAX_REQUEST_BYTES = 131072
MAX_BASE64_BYTES = ((MAX_REQUEST_BYTES + 2) // 3) * 4
FIELDS = {"panel_id", "criterion", "answer_a", "answer_b", "expected_boundary_sha256"}
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
PROBE_BYTES = b"maka-fifo-probe-v1\n"


class BallotError(ValueError):
    pass


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise BallotError("Harbor judge request is invalid")
        result[key] = value
    return result


def validate(raw: bytes) -> bytes:
    if not raw or len(raw) > MAX_REQUEST_BYTES:
        raise BallotError("Harbor judge request is too large")
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"),
                           object_pairs_hook=unique_object,
                           parse_constant=lambda _name: (_ for _ in ()).throw(ValueError()))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise BallotError("Harbor judge request is invalid") from None
    if (not isinstance(value, dict) or set(value) != FIELDS or
            value.get("panel_id") != "harbor" or
            any(not isinstance(value.get(name), str) or not value[name].strip()
                for name in FIELDS) or
            DIGEST.fullmatch(value["expected_boundary_sha256"]) is None):
        raise BallotError("Harbor judge request is invalid")
    return raw


def decoded_input() -> bytes:
    encoded = sys.stdin.buffer.read(MAX_BASE64_BYTES + 1)
    if len(encoded) > MAX_BASE64_BYTES:
        raise BallotError("Harbor judge request is too large")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except binascii.Error:
        raise BallotError("Harbor judge request encoding is invalid") from None
    return validate(raw)


def deliver(path: str, raw: bytes, *, timeout_seconds: int = 180) -> None:
    metadata = os.lstat(path)
    if not stat.S_ISFIFO(metadata.st_mode) or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise BallotError("Harbor judge request FIFO is unsafe")
    signal.signal(signal.SIGALRM, lambda _signal, _frame: (_ for _ in ()).throw(TimeoutError()))
    signal.alarm(timeout_seconds)
    descriptor = os.open(path, os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISFIFO(opened.st_mode) or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise BallotError("Harbor judge request FIFO changed")
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(raw)
            stream.flush()
    finally:
        os.close(descriptor)
        signal.alarm(0)


def read_probe(path: str) -> bytes:
    metadata = os.lstat(path)
    if (not stat.S_ISFIFO(metadata.st_mode) or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600):
        raise BallotError("Harbor judge probe FIFO is unsafe")
    signal.signal(signal.SIGALRM, lambda _signal, _frame: (_ for _ in ()).throw(TimeoutError()))
    signal.alarm(10)
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if not stat.S_ISFIFO(opened.st_mode) or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise BallotError("Harbor judge probe FIFO changed")
        observed = os.read(descriptor, len(PROBE_BYTES) + 1)
        if observed != PROBE_BYTES:
            raise BallotError("Harbor judge probe transport failed")
        return observed
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        signal.alarm(0)


def main(arguments: list[str]) -> int:
    try:
        if arguments == ["capture"]:
            raw = validate(sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1))
            sys.stdout.buffer.write(base64.b64encode(raw))
        elif arguments == ["digest"]:
            sys.stdout.write(hashlib.sha256(decoded_input()).hexdigest() + "\n")
        elif arguments == ["boundary"]:
            sys.stdout.write(json.loads(decoded_input())["expected_boundary_sha256"] + "\n")
        elif len(arguments) == 2 and arguments[0] == "source-digest":
            from install_component import ComponentInstallError, source_digest
            try:
                sys.stdout.write(source_digest(pathlib.Path(arguments[1])) + "\n")
            except ComponentInstallError as error:
                raise BallotError("Harbor judge source is unavailable") from error
        elif len(arguments) == 2 and arguments[0] == "probe-write":
            deliver(arguments[1], PROBE_BYTES, timeout_seconds=10)
        elif len(arguments) == 2 and arguments[0] == "probe-read":
            sys.stdout.buffer.write(read_probe(arguments[1]))
        elif len(arguments) == 2 and arguments[0] == "deliver":
            deliver(arguments[1], decoded_input())
        elif arguments and arguments[0].startswith("capture-native-"):
            from harbor_judge_capture import (
                CaptureRejected, native_cleanup, native_context, native_emit,
                native_receive, publish_sealed,
            )
            try:
                if len(arguments) == 10 and arguments[0] == "capture-native-context":
                    sys.stdout.write(native_context(arguments[1:]) + "\n")
                elif len(arguments) == 8 and arguments[0] == "capture-native-receive":
                    native_receive(arguments[1:])
                elif len(arguments) == 9 and arguments[0] == "capture-native-emit":
                    native_emit(arguments[1:])
                elif len(arguments) == 2 and arguments[0] == "capture-native-cleanup":
                    native_cleanup(arguments[1])
                elif len(arguments) == 3 and arguments[0] == "capture-native-sealed":
                    publish_sealed(arguments[1], arguments[2])
                else:
                    raise CaptureRejected()
            except Exception:
                raise BallotError("Harbor native capture association is unproven") from None
        else:
            raise BallotError("Harbor judge input invocation is invalid")
    except (BallotError, OSError, TimeoutError) as error:
        print(str(error) if isinstance(error, BallotError) else "Harbor judge request transport failed",
              file=sys.stderr)
        return 78
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
