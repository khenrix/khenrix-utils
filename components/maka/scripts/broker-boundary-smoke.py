#!/usr/bin/env python3
"""Prove task/proxy key separation with a synthetic credential."""

from __future__ import annotations

import base64
import json
import os
import pathlib
import secrets
import subprocess
import sys
import urllib.parse

controller_id, evidence_text, lab_root = sys.argv[1:]
evidence = pathlib.Path(evidence_text)
suffix = secrets.token_hex(8)
volume = f"maka-secret-boundary-{suffix}"
trusted_container = f"maka-secret-boundary-proxy-{suffix}"
untrusted_container = f"maka-secret-boundary-task-{suffix}"
sentinel = bytearray(b"synthetic-real-" + secrets.token_urlsafe(32).encode())
created_containers: list[str] = []
volume_created = False


def docker(*args: str, input_bytes: bytes | None = None, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["docker", *args], input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check)


def variants(value: bytes) -> set[bytes]:
    standard_b64 = base64.b64encode(value)
    urlsafe_b64 = base64.urlsafe_b64encode(value)
    percent_upper = b"".join(f"%{byte:02X}".encode() for byte in value)
    percent_lower = percent_upper.lower()
    return {
        value,
        standard_b64,
        standard_b64.rstrip(b"="),
        urlsafe_b64,
        urlsafe_b64.rstrip(b"="),
        value.hex().encode(),
        value.hex().upper().encode(),
        urllib.parse.quote_from_bytes(value, safe="").encode(),
        percent_upper,
        percent_lower,
    }


def safe_write(name: str, data: bytes) -> None:
    if any(candidate in data for candidate in variants(bytes(sentinel))):
        raise RuntimeError(f"synthetic secret reached {name}")
    (evidence / name).write_bytes(data)


def scan_evidence() -> None:
    for path in evidence.rglob("*"):
        if path.is_file() and not path.is_symlink():
            data = path.read_bytes()
            if any(candidate in data for candidate in variants(bytes(sentinel))):
                raise RuntimeError(f"synthetic secret reached retained evidence file {path.name}")


try:
    untrusted_command = [
        "run", "--name", untrusted_container,
        "--ulimit", "core=0",
        "--mount", f"type=bind,source={lab_root},target={lab_root}",
        "--workdir", lab_root,
        "--env", f"MAKA_LAB_ROOT={lab_root}",
        "--env", f"MAKA_EVIDENCE_DIR={evidence}",
        "--env", f"OPENAI_API_KEY=maka-decoy-boundary-{suffix}",
        controller_id,
        "python", "controller/broker-boundary-untrusted-child.py",
    ]
    untrusted = docker(*untrusted_command, check=False)
    created_containers.append(untrusted_container)
    safe_write("untrusted-controller.stdout", untrusted.stdout)
    safe_write("untrusted-controller.stderr", untrusted.stderr)
    untrusted_inspect = docker("container", "inspect", untrusted_container).stdout
    untrusted_logs_result = docker("container", "logs", untrusted_container)
    untrusted_logs = untrusted_logs_result.stdout + untrusted_logs_result.stderr
    untrusted_diff = docker("container", "diff", untrusted_container).stdout
    safe_write("untrusted-container-inspect.json", untrusted_inspect)
    safe_write("untrusted-container-logs.txt", untrusted_logs)
    safe_write("untrusted-container-diff.txt", untrusted_diff)

    docker("volume", "create", "--label", "io.maka.lab.boundary-test=true", volume)
    volume_created = True
    command = [
        "run", "-i", "--name", trusted_container,
        "--ulimit", "core=0",
        "--mount", f"type=bind,source={lab_root},target={lab_root}",
        "--mount", f"type=volume,source={volume},target=/run/maka-secret",
        "--workdir", lab_root,
        "--env", f"MAKA_LAB_ROOT={lab_root}",
        "--env", f"MAKA_EVIDENCE_DIR={evidence}",
        "--env", f"MAKA_EVAL_SECRET_VOLUME={volume}",
        controller_id,
        "python", "controller/secret-broker-exec.py", "python", "controller/broker-boundary-child.py",
    ]
    completed = docker(*command, input_bytes=bytes(sentinel) + b"\n", check=False)
    created_containers.append(trusted_container)
    safe_write("controller.stdout", completed.stdout)
    safe_write("controller.stderr", completed.stderr)
    inspected = docker("container", "inspect", trusted_container).stdout
    logs_result = docker("container", "logs", trusted_container)
    logs = logs_result.stdout + logs_result.stderr
    image = docker("image", "inspect", controller_id).stdout
    history = docker("image", "history", "--no-trunc", controller_id).stdout
    diff = docker("container", "diff", trusted_container).stdout
    volume_metadata = docker("volume", "inspect", volume).stdout
    for name, data in (
        ("trusted-container-inspect.json", inspected),
        ("trusted-container-logs.txt", logs),
        ("controller-image.json", image),
        ("controller-history.txt", history),
        ("trusted-container-diff.txt", diff),
        ("secret-volume.json", volume_metadata),
    ):
        safe_write(name, data)
    volume_listing = docker(
        "run", "--rm", "--entrypoint", "sh",
        "--mount", f"type=volume,source={volume},target=/run/maka-secret",
        controller_id, "-c", "find /run/maka-secret -mindepth 1 -maxdepth 1 -print",
    ).stdout
    safe_write("secret-volume-files.txt", volume_listing)
    if volume_listing.strip():
        raise RuntimeError("secret broker volume was not empty after execution")
    trusted_report = json.loads((evidence / "broker-child.json").read_text())
    if trusted_report != {
        "openaiCredentialClass": "decoy",
        "providerVariables": ["OPENAI_API_KEY"],
        "realCredentialInEnvironment": False,
        "processRole": "trusted-broker-consumer",
        "coreDumpLimit": "zero-soft-and-hard",
    }:
        raise RuntimeError("trusted proxy-consumer boundary report was invalid")
    untrusted_report = json.loads((evidence / "broker-untrusted-child.json").read_text())
    if untrusted_report != {
        "openaiCredentialClass": "decoy",
        "providerVariables": ["OPENAI_API_KEY"],
        "realCredentialInEnvironment": False,
        "secretBrokerMountPresent": False,
        "processRole": "untrusted-task-simulation",
        "coreDumpLimit": "zero-soft-and-hard",
    }:
        raise RuntimeError("untrusted task boundary report was invalid")
    (evidence / "boundary-result.json").write_text(json.dumps({
        "ok": completed.returncode == 0 and untrusted.returncode == 0,
        "trustedProxyConsumerExitCode": completed.returncode,
        "untrustedTaskSimulationExitCode": untrusted.returncode,
        "realKeyTransport": "stdin-to-memory-to-unix-socket",
        "untrustedTaskEnvironmentCredential": "decoy-only",
        "untrustedTaskSecretMountPresent": False,
        "trustedProxyConsumedBroker": True,
        "dockerMetadataScanned": True,
        "retainedEvidenceScanned": True,
    }, indent=2) + "\n")
    scan_evidence()
    if untrusted.returncode != 0:
        raise SystemExit(untrusted.returncode)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)
finally:
    active_error = sys.exc_info()[0] is not None
    cleanup_errors: list[str] = []
    for container in created_containers:
        removed = docker("container", "rm", "-f", container, check=False)
        if removed.returncode != 0:
            cleanup_errors.append("named_container_remove_failed")
    remaining = docker("container", "ls", "-aq", "--filter", f"volume={volume}", check=False).stdout
    for retained in remaining.decode().splitlines():
        if retained:
            removed = docker("container", "rm", "-f", retained, check=False)
            if removed.returncode != 0:
                cleanup_errors.append("retained_container_remove_failed")
    remaining_after = docker("container", "ls", "-aq", "--filter", f"volume={volume}", check=False).stdout
    if remaining_after.strip():
        cleanup_errors.append("retained_container_survived")
    if volume_created:
        removed = docker("volume", "rm", volume, check=False)
        if removed.returncode != 0:
            cleanup_errors.append("secret_volume_remove_failed")
    if docker("volume", "inspect", volume, check=False).returncode == 0:
        cleanup_errors.append("secret_volume_survived")
    cleanup_result = json.dumps({
        "ok": not cleanup_errors,
        "errors": cleanup_errors,
        "remainingContainers": len(remaining_after.decode().splitlines()),
        "volumeRemoved": "secret_volume_survived" not in cleanup_errors,
    }, indent=2).encode() + b"\n"
    safe_write("cleanup-result.json", cleanup_result)
    for index in range(len(sentinel)):
        sentinel[index] = 0
    if cleanup_errors and not active_error:
        raise RuntimeError("boundary cleanup did not remove all secret-bearing resources")
