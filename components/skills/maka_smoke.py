#!/usr/bin/env python3
"""Run bounded explicit and natural Maka routing smoke cases.

The runner never passes ``--yolo``.  Every case receives a fresh temporary cwd,
and the summary receipt is bound to the case manifest, installed skill hashes,
and the observed Maka version. The runner verifies explicit admissions and
natural ``model_tool`` loads against Maka's read-only SQLite event records;
behavior quality remains covered by the provider eval harness.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import selectors
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

import skillctl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "maka" / "interactive"))
from maka_auth_mode import AuthModeError, read_mode  # noqa: E402
from maka_profile import MakaProfileError, resolve_maka_profile  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "maka" / "scripts"))
import component_doctor  # noqa: E402
import install_component  # noqa: E402

class SmokeError(RuntimeError):
    """A smoke precondition or bounded invocation failed."""


@dataclass(frozen=True)
class PrivateHostBinding:
    root_path: Path
    client_data_root: Path
    root_id: str
    spec_path: Path
    attestation: Mapping[str, Any]


PRIVATE_HOST_ENV = (
    "KHENRIX_MAKA_PRIVATE_ROOT",
    "KHENRIX_MAKA_PRIVATE_CLIENT_DATA_ROOT",
    "KHENRIX_MAKA_PRIVATE_ROOT_ID",
    "KHENRIX_MAKA_PRIVATE_HOST_STARTED_AT_MS",
)


def private_host_environment(
    environment: Mapping[str, str], runtime_db: Path,
    private_host: PrivateHostBinding | None,
) -> dict[str, str]:
    if any(name in environment for name in PRIVATE_HOST_ENV):
        raise SmokeError("Maka private Host environment override is untrusted")
    child = dict(environment)
    if private_host is None:
        if runtime_db.resolve() != default_runtime_db(
            install_component.canonical_layout().home
        ).resolve():
            raise SmokeError("routing smoke must inspect the managed Maka profile")
        return child
    if (
        runtime_db.resolve() != private_host.root_path / "runtime.sqlite"
        or private_host.root_path != private_host.client_data_root / "workspaces/default"
        or not re.fullmatch(r"[0-9a-f]{64}", private_host.root_id)
        or private_host.attestation.get("rootId") != private_host.root_id
        or not isinstance(private_host.attestation.get("pid"), int)
        or not isinstance(private_host.attestation.get("hostEpoch"), str)
        or not isinstance(private_host.attestation.get("startedAtMs"), (int, float))
    ):
        raise SmokeError("Maka private Host binding is unverified")
    child.update({
        "KHENRIX_MAKA_PRIVATE_ROOT": str(private_host.root_path),
        "KHENRIX_MAKA_PRIVATE_CLIENT_DATA_ROOT": str(private_host.client_data_root),
        "KHENRIX_MAKA_PRIVATE_ROOT_ID": private_host.root_id,
        "KHENRIX_MAKA_PRIVATE_HOST_STARTED_AT_MS": str(
            private_host.attestation["startedAtMs"]
        ),
    })
    return child


def _private_file(path: Path, data: bytes, mode: int = 0o600) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=False) as output:
            output.write(data)
    finally:
        os.close(descriptor)


def _private_json_command(
    command: list[str], environment: Mapping[str, str], *, timeout: int = 30,
) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, timeout=timeout, env=dict(environment),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SmokeError(f"Maka private Host preparation failed: {error}") from error
    if completed.returncode:
        raise SmokeError(
            "Maka private Host preparation failed: " + completed.stderr.strip()[-500:]
        )
    try:
        value = json.loads(completed.stdout, object_pairs_hook=unique_json_object)
    except (ValueError, TypeError) as error:
        raise SmokeError("Maka private Host preparation returned invalid JSON") from error
    if not isinstance(value, dict):
        raise SmokeError("Maka private Host preparation returned invalid evidence")
    return value


def seed_private_skill_preferences(root: Path) -> str:
    """Keep startup and plan skills in Maka's bounded private Host catalog."""

    for directory in (root.parent.parent, root.parent, root, root / ".maka"):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            metadata = directory.lstat()
            if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o700):
                raise SmokeError("Maka private Host skill preference path is unsafe")
    state = root / ".maka/skills-state.json"
    if os.path.lexists(state):
        raise SmokeError("Maka private Host skill preferences already exist")
    document = {
        "schemaVersion": 2,
        "skills": {
            name: {"enabled": True, "pinned": True}
            for name in ("using-superpowers", "writing-plans", "writing-skills")
        },
    }
    content = canonical(document)
    _private_file(state, content)
    return hashlib.sha256(content).hexdigest()


def prepare_private_host_spec(
    binary: list[str], component: Path, receipt: Mapping[str, Any],
    base: Path, environment: Mapping[str, str],
) -> dict[str, Any]:
    """Pin one new private root and native seal before a Host may be started."""

    if sys.platform != "darwin" or len(binary) != 2 or any(
        name not in {"HOME", "USER", "LOGNAME", "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR"}
        for name in environment
    ):
        raise SmokeError("Maka private Host preparation requires sanitized macOS input")
    node_input = Path(binary[0])
    node = node_input.resolve()
    helper = component / "interactive/maka_skill_routing.mjs"
    seal_source = component / "interactive/maka_host_seal.c"
    package = component / "runtime/package"
    node_headers = node.parent.parent / "include/node"
    if (
        not base.is_dir() or base.is_symlink()
        or stat.S_IMODE(base.stat().st_mode) != 0o700
        or node_input.is_symlink() or not node.is_file()
        or not (node_headers / "node_api.h").is_file()
        or not helper.is_file() or helper.is_symlink()
        or not seal_source.is_file() or seal_source.is_symlink()
        or binary[1] != str(package / "dist/cli.js")
    ):
        raise SmokeError("Maka private Host source paths are unverified")
    try:
        compiler_result = subprocess.run(
            ["/usr/bin/xcrun", "--sdk", "macosx", "--find", "clang"], text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
            timeout=15, env=dict(environment),
        )
        sdk_result = subprocess.run(
            ["/usr/bin/xcrun", "--sdk", "macosx", "--show-sdk-path"], text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
            timeout=15, env=dict(environment),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SmokeError(f"Maka private Host compiler is unavailable: {error}") from error
    if compiler_result.returncode or sdk_result.returncode:
        raise SmokeError("Maka private Host compiler is unavailable")
    compiler = Path(compiler_result.stdout.strip()).resolve()
    sdk = Path(sdk_result.stdout.strip()).resolve()
    if not compiler.is_file() or not os.access(compiler, os.X_OK) or not sdk.is_dir():
        raise SmokeError("Maka private Host compiler is unverified")
    client_data_root = base / "client"
    root_path = client_data_root / "workspaces/default"
    preview = _private_json_command([
        str(node), str(helper), "--private-host-root-id", str(package),
        str(client_data_root),
    ], environment)
    root_id = preview.get("rootId")
    control_path = preview.get("controlPath")
    if (
        not isinstance(root_id, str) or not re.fullmatch(r"[0-9a-f]{64}", root_id)
        or preview.get("rootPath") != str(root_path)
        or preview.get("clientDataRoot") != str(client_data_root)
        or not isinstance(control_path, str)
        or Path(control_path).name != root_id
        or preview.get("controlPreexisted") is not False
        or os.path.lexists(control_path)
    ):
        raise SmokeError("Maka private Host root preview is unverified")
    seed_private_skill_preferences(root_path)
    addon = base / "maka-host-seal.node"
    compile_command = [
        str(compiler), "-isysroot", str(sdk),
        "-bundle", "-undefined", "dynamic_lookup",
        "-Wno-deprecated-declarations", "-I", str(node_headers),
        "-o", str(addon), str(seal_source),
    ]
    try:
        compiled = subprocess.run(
            compile_command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, timeout=60, env=dict(environment),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SmokeError(f"Maka private Host native seal build failed: {error}") from error
    if compiled.returncode or not addon.is_file() or addon.is_symlink():
        raise SmokeError("Maka private Host native seal build failed")
    addon.chmod(0o600)
    marker = base / "absolute-codex.log"
    absolute = base / "absolute-codex"
    _private_file(
        absolute,
        ("#!/bin/sh\nprintf launched > " + shlex.quote(str(marker)) + "\n").encode(),
        0o700,
    )
    layout = install_component.canonical_layout()
    spec = {
        "schema": "khenrix-maka-private-host-spec-v1",
        "rootPath": str(root_path), "clientDataRoot": str(client_data_root),
        "defaultRootPath": str(default_runtime_db(layout.home).parent),
        "rootId": root_id, "controlPath": control_path,
        "nonce": secrets.token_hex(32),
        "packageRoot": str(package),
        "candidateEntrypoint": str(package / "node_modules/@maka/runtime-host/dist/execution-candidate-main.js"),
        "nodePath": str(node), "helperPath": str(helper),
        "sealSourcePath": str(seal_source), "sealAddonPath": str(addon),
        "compilerPath": str(compiler),
        "absoluteExecutable": str(absolute), "markerPath": str(marker),
        "witnessPath": str(base / "host-preload-witness.json"),
        "tokenFile": str(layout.home / ".config/khenrix-utils/maka/relay/caller-token"),
        "attestationFile": str(layout.home / ".config/khenrix-utils/maka/relay/relay-attestation"),
        "installBootSession": receipt["boot_session_uuid"],
        "installMonotonicNs": receipt["install_monotonic_ns"],
        "packageDigest": receipt["runtime_package_digest"],
        "nodeDigest": hashlib.sha256(node.read_bytes()).hexdigest(),
        "helperDigest": hashlib.sha256(helper.read_bytes()).hexdigest(),
        "sealSourceDigest": hashlib.sha256(seal_source.read_bytes()).hexdigest(),
        "sealAddonDigest": hashlib.sha256(addon.read_bytes()).hexdigest(),
        "compilerDigest": hashlib.sha256(compiler.read_bytes()).hexdigest(),
    }
    spec_path = base / "host-spec.json"
    _private_file(spec_path, canonical(spec))
    if _private_json_command(
        [str(node), str(helper), "--private-host-preview", str(spec_path)], environment,
    ) != preview:
        raise SmokeError("Maka private Host preview changed before launch")
    return spec


def verify_private_host_ready(
    spec: Mapping[str, Any], spec_path: Path, ready: Any,
) -> PrivateHostBinding:
    fields = {
        "rootPath", "clientDataRoot", "rootId", "controlPath", "nonce",
        "packageDigest", "nodeDigest", "helperDigest", "sealSourceDigest",
        "sealAddonDigest", "compilerDigest",
    }
    required = fields | {
        "schema", "pid", "hostEpoch", "startedAtMs", "controlPreexisted",
        "defaultHost", "denial",
    }
    denial = ready.get("denial") if isinstance(ready, dict) else None
    default = ready.get("defaultHost") if isinstance(ready, dict) else None
    default_valid = isinstance(default, dict) and (
        default == {"kind": "unavailable"} or (
            set(default) == {"kind", "rootId", "pid", "hostEpoch", "sessions"}
            and default.get("kind") == "connected"
            and isinstance(default.get("rootId"), str)
            and re.fullmatch(r"[0-9a-f]{64}", default["rootId"]) is not None
            and isinstance(default.get("pid"), int) and default["pid"] > 0
            and isinstance(default.get("hostEpoch"), str) and default["hostEpoch"]
            and isinstance(default.get("sessions"), list)
            and all(isinstance(item, str) and item for item in default["sessions"])
            and default["sessions"] == sorted(set(default["sessions"]))
        )
    )
    if (
        not isinstance(ready, dict) or set(ready) != required
        or ready.get("schema") != "khenrix-maka-private-host-v1"
        or any(ready.get(key) != spec.get(key) for key in fields)
        or not isinstance(ready.get("pid"), int) or ready["pid"] <= 0
        or not isinstance(ready.get("hostEpoch"), str) or not ready["hostEpoch"]
        or not isinstance(ready.get("startedAtMs"), (int, float))
        or ready["startedAtMs"] <= 0
        or ready.get("controlPreexisted") is not False
        or not default_valid
        or not isinstance(denial, dict)
        or denial != {
            "absoluteExecutable": spec.get("absoluteExecutable"),
            "selfExecutable": spec.get("nodePath"),
            "denied": True,
            "forkErrorCode": "EPERM",
            "absoluteExecErrno": 1,
            "selfExecErrno": 1,
        }
    ):
        raise SmokeError("Maka private Host ready attestation is unverified")
    return PrivateHostBinding(
        root_path=Path(spec["rootPath"]),
        client_data_root=Path(spec["clientDataRoot"]),
        root_id=spec["rootId"], spec_path=spec_path,
        attestation=ready,
    )


_SUPERVISOR_FAILURE_CATEGORIES = {
    b"Maka routing preflight failed: Maka Host active Sessions are unverified\n":
        "active_sessions_unverified",
    b"Maka routing preflight failed: Maka private Host has active Sessions; settlement refused\n":
        "active_sessions",
    b"Maka routing preflight failed: Maka private Host settlement or default Host identity is unverified\n":
        "settlement_or_default_host_unverified",
    b"Maka routing preflight failed: Maka default Host changed during private launch\n":
        "default_host_changed",
}


def private_host_supervisor_failure(code: int, stderr: BinaryIO) -> SmokeError:
    stderr.seek(0)
    category = _SUPERVISOR_FAILURE_CATEGORIES.get(stderr.read(256), "unclassified")
    return SmokeError(f"Maka private Host settlement failed: exit code {code}; category {category}")


@contextmanager
def private_routing_host(
    binary: list[str], component: Path, receipt: Mapping[str, Any],
    environment: Mapping[str, str],
    *, retain_private_host: bool = False,
):
    """Own one fresh Host until every bounded Turn and its native checks settle."""

    base = Path(tempfile.mkdtemp(prefix="khenrix-maka-private-host-")).resolve()
    process: subprocess.Popen[bytes] | None = None
    supervisor_stderr: BinaryIO | None = None
    binding: PrivateHostBinding | None = None
    settled = False
    try:
        spec = prepare_private_host_spec(binary, component, receipt, base, environment)
        spec_path = base / "host-spec.json"
        if os.path.lexists(spec["controlPath"]):
            raise SmokeError("Maka private Host control appeared before launch")
        try:
            stderr_path = base / "supervisor-stderr.log"
            _private_file(stderr_path, b"")
            supervisor_stderr = stderr_path.open("r+b")
            process = subprocess.Popen(
                [binary[0], str(component / "interactive/maka_skill_routing.mjs"),
                 "--supervise-private-host", str(spec_path)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=supervisor_stderr,
                env=dict(environment),
            )
        except OSError as error:
            raise SmokeError(f"Maka private Host supervisor could not start: {error}") from error
        assert process.stdout is not None and process.stdin is not None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            if not selector.select(timeout=100):
                raise SmokeError(
                    f"Maka private Host did not attest readiness; supervisor PID {process.pid}"
                )
        line = process.stdout.readline()
        _private_file(base / "host-readiness.jsonl", line)
        try:
            ready = json.loads(line, object_pairs_hook=unique_json_object)
        except (TypeError, ValueError) as error:
            raise SmokeError("Maka private Host readiness is malformed") from error
        binding = verify_private_host_ready(spec, spec_path, ready)
        if process.poll() is not None:
            raise SmokeError("Maka private Host supervisor exited before routing")
        yield binding
    finally:
        pending_failure = sys.exc_info()[1]
        try:
            if process is not None:
                if process.stdin is not None and not process.stdin.closed:
                    process.stdin.close()
                try:
                    try:
                        code = process.wait(timeout=20)
                    except subprocess.TimeoutExpired as error:
                        raise SmokeError("Maka private Host settlement timed out") from error
                    assert process.stdout is not None
                    final = process.stdout.readline()
                    try:
                        _private_file(base / "host-settlement.jsonl", final)
                    except OSError as error:
                        raise SmokeError("Maka private Host settlement evidence could not be retained") from error
                    if binding is not None:
                        assert supervisor_stderr is not None
                        if code != 0:
                            raise private_host_supervisor_failure(code, supervisor_stderr)
                        try:
                            stopped = json.loads(final, object_pairs_hook=unique_json_object)
                        except (TypeError, ValueError):
                            raise private_host_supervisor_failure(code, supervisor_stderr) from None
                        if stopped != {
                            "schema": "khenrix-maka-private-host-settled-v1",
                            "rootId": binding.root_id,
                            "pid": binding.attestation["pid"],
                            "hostEpoch": binding.attestation["hostEpoch"],
                            "activeSessions": 0,
                            "defaultHost": binding.attestation["defaultHost"],
                        }:
                            raise SmokeError("Maka private Host settlement or default Host changed")
                        settled = True
                except SmokeError as error:
                    if pending_failure is None:
                        raise
                    print(f"Maka private Host teardown also failed: {error}", file=sys.stderr)
        finally:
            if supervisor_stderr is not None:
                supervisor_stderr.close()
            if settled and pending_failure is None and not retain_private_host:
                shutil.rmtree(base)
            else:
                print(
                    f"Maka private Host artifacts retained for review: {base}",
                    file=sys.stderr,
                )


SESSION_BOOTSTRAP = {
    "id": "session-bootstrap",
    "prompt": "/skill:using-superpowers Reply ready.",
    "timeout_seconds": 120,
    "max_steps": 2,
}


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def default_runtime_db(
    home: Path,
    *,
    platform: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Resolve Maka's managed platform profile instead of assuming macOS."""
    try:
        profile = resolve_maka_profile(
            home, platform=platform, environment=environment, managed=True
        )
    except MakaProfileError as error:
        raise SmokeError(str(error)) from error
    return profile / "workspaces" / "default" / "runtime.sqlite"


def load_cases(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise SmokeError(f"cannot read smoke manifest {path}: {error}") from error
    if not isinstance(data, dict) or data.get("schema_version") != 2:
        raise SmokeError("smoke manifest schema_version must be 2")
    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        raise SmokeError("smoke manifest requires cases")
    required_routes = data.get("required_routes")
    if not isinstance(required_routes, dict) or not required_routes:
        raise SmokeError("smoke manifest requires a non-empty required_routes object")
    expected: set[tuple[str, str]] = set()
    for skill, required in required_routes.items():
        if not isinstance(skill, str) or not skillctl.SKILL_NAME.fullmatch(skill):
            raise SmokeError(f"smoke manifest has invalid skill name {skill!r}")
        if (
            not isinstance(required, list)
            or not required
            or len(required) != len(set(required))
            or any(route not in {"explicit", "natural"} for route in required)
        ):
            raise SmokeError(f"smoke manifest has invalid required routes for {skill}")
        expected.update((skill, route) for route in required)
    ids: set[str] = set()
    routes: set[tuple[str, str]] = set()
    for case in cases:
        if not isinstance(case, dict):
            raise SmokeError("each smoke case must be an object")
        required = {"id", "route", "skill", "prompt", "timeout_seconds", "max_steps"}
        if not required <= set(case):
            raise SmokeError(f"smoke case is missing {sorted(required - set(case))}")
        if case["id"] in ids:
            raise SmokeError(f"duplicate smoke case ID {case['id']!r}")
        ids.add(case["id"])
        if case["route"] not in {"explicit", "natural"}:
            raise SmokeError(f"case {case['id']} has invalid route")
        if case["skill"] not in required_routes:
            raise SmokeError(f"case {case['id']} has unmanaged skill")
        token = f"/skill:{case['skill']}"
        if case["route"] == "explicit" and token not in case["prompt"]:
            raise SmokeError(f"explicit case {case['id']} lacks {token}")
        if case["route"] == "natural" and "/skill:" in case["prompt"]:
            raise SmokeError(f"natural case {case['id']} contains an explicit skill token")
        if not isinstance(case["timeout_seconds"], int) or not 1 <= case["timeout_seconds"] <= 300:
            raise SmokeError(f"case {case['id']} timeout must be 1..300 seconds")
        if not isinstance(case["max_steps"], int) or not 1 <= case["max_steps"] <= 4:
            raise SmokeError(f"case {case['id']} max_steps must be 1..4")
        routes.add((case["skill"], case["route"]))
    if routes != expected:
        raise SmokeError(
            "smoke manifest must cover every required skill route; "
            f"expected {expected}, found {routes}"
        )
    flows = data.get("flows")
    if not isinstance(flows, list) or len(flows) != 1:
        raise SmokeError("smoke manifest requires one bounded multi-turn ADHD flow")
    flow = flows[0]
    if not isinstance(flow, dict) or flow.get("skill") != "khenrix-quality":
        raise SmokeError("ADHD flow must target khenrix-quality")
    steps = flow.get("steps")
    if not isinstance(steps, list) or len(steps) != 3:
        raise SmokeError("ADHD flow must contain activate, follow-up, and opt-out steps")
    if steps[0].get("continue") is not False or "/skill:khenrix-quality" not in steps[0].get("prompt", ""):
        raise SmokeError("ADHD flow must begin with explicit khenrix-quality activation")
    if any(step.get("continue") is not True for step in steps[1:]):
        raise SmokeError("ADHD follow-up and opt-out steps must continue the same session")
    if steps[1].get("check") != "adhd_action_first":
        raise SmokeError("ADHD follow-up must use the structural action-first check")
    if steps[2].get("check") != "normal_mode_ack":
        raise SmokeError("ADHD opt-out must use the bounded state-acknowledgement check")
    if any("must_contain" in step for step in steps[1:]):
        raise SmokeError("ADHD behavior steps must not put the expected answer in the prompt")
    for step in steps:
        if not isinstance(step.get("timeout_seconds"), int) or not 1 <= step["timeout_seconds"] <= 300:
            raise SmokeError(f"ADHD step {step.get('id')} timeout must be 1..300 seconds")
        if not isinstance(step.get("max_steps"), int) or not 1 <= step["max_steps"] <= 4:
            raise SmokeError(f"ADHD step {step.get('id')} max_steps must be 1..4")
    matrix = data.get("matrix")
    if not isinstance(matrix, list) or not matrix:
        raise SmokeError("smoke manifest requires a fanout routing matrix")
    scenario_ids: set[str] = set()
    for scenario in matrix:
        if not isinstance(scenario, dict) or not isinstance(scenario.get("id"), str):
            raise SmokeError("matrix scenario requires an ID")
        scenario_id = scenario["id"]
        if scenario_id in scenario_ids:
            raise SmokeError(f"duplicate matrix scenario ID {scenario_id!r}")
        scenario_ids.add(scenario_id)
        if scenario.get("validation_only") is not True:
            raise SmokeError(f"matrix scenario {scenario_id} must be validation-only")
        matrix_steps = scenario.get("steps")
        if not isinstance(matrix_steps, list) or not matrix_steps:
            raise SmokeError(f"matrix scenario {scenario_id} requires steps")
        step_ids: set[str] = set()
        for index, step in enumerate(matrix_steps):
            if not isinstance(step, dict) or not isinstance(step.get("id"), str):
                raise SmokeError(f"matrix scenario {scenario_id} has a step without an ID")
            step_id = step["id"]
            if step_id in step_ids:
                raise SmokeError(f"matrix scenario {scenario_id} repeats step {step_id}")
            step_ids.add(step_id)
            if step.get("continue") is not (index > 0):
                raise SmokeError(f"matrix step {step_id} has invalid continuation")
            if not isinstance(step.get("prompt"), str) or not step["prompt"].strip():
                raise SmokeError(f"matrix step {step_id} requires a prompt")
            expected_skills = step.get("expected_skills")
            if not isinstance(expected_skills, list) or not expected_skills:
                raise SmokeError(f"matrix step {step_id} requires ordered expected_skills")
            for expected_skill in expected_skills:
                if (
                    not isinstance(expected_skill, dict)
                    or set(expected_skill) != {"id", "invocation"}
                    or not isinstance(expected_skill["id"], str)
                    or not skillctl.SKILL_NAME.fullmatch(expected_skill["id"])
                    or expected_skill["invocation"] not in {"explicit", "model_tool"}
                ):
                    raise SmokeError(f"matrix step {step_id} has invalid expected_skills")
                if (
                    expected_skill["invocation"] == "explicit"
                    and f"/skill:{expected_skill['id']}" not in step["prompt"]
                ):
                    raise SmokeError(f"matrix step {step_id} lacks its explicit skill token")
            if not isinstance(step.get("timeout_seconds"), int) or not 1 <= step["timeout_seconds"] <= 300:
                raise SmokeError(f"matrix step {step_id} timeout must be 1..300 seconds")
            if not isinstance(step.get("max_steps"), int) or not 1 <= step["max_steps"] <= 4:
                raise SmokeError(f"matrix step {step_id} max_steps must be 1..4")
            if step.get("check") not in {None, "maka_executor_refusal"}:
                raise SmokeError(f"matrix step {step_id} has an unknown output check")
            if step.get("check") == "maka_executor_refusal" and step["prompt"].count(
                "{absolute_canary_path}"
            ) != 1:
                raise SmokeError(f"matrix step {step_id} lacks its absolute executor canary")
    return data


def installed_closure(config: skillctl.Configuration, manifest: dict[str, Any]) -> dict[str, Any]:
    _, entries = skillctl.plan(config)
    drift = [entry.key for entry in entries if entry.action != "MATCH"]
    if drift:
        raise SmokeError("skills and instructions must be applied before Maka smoke: " + ", ".join(drift))
    receipt_path = config.state_dir / skillctl.RECEIPT_NAME
    if not receipt_path.is_file() or receipt_path.is_symlink():
        raise SmokeError(f"missing safe install receipt: {receipt_path}")
    try:
        receipt = json.loads(receipt_path.read_text())
    except json.JSONDecodeError as error:
        raise SmokeError(f"invalid install receipt: {error}") from error
    skillctl.validate_install_receipt_header(receipt)
    requested = set(manifest["required_routes"])
    requested.update(
        expected["id"]
        for scenario in manifest["matrix"]
        for step in scenario["steps"]
        for expected in step["expected_skills"]
    )
    missing = requested - set(config.skills)
    if missing:
        raise SmokeError("smoke manifest names skills outside direct delivery: " + ", ".join(sorted(missing)))
    skills: dict[str, str] = {}
    for name in config.skills:
        desired = next(
            entry.desired_hash for entry in entries if entry.kind == "skill" and entry.skill == name
        )
        recorded = receipt.get("skills", {}).get(name, {}).get("source_hash")
        if recorded != desired:
            raise SmokeError(f"install receipt is stale for {name}")
        skills[name] = desired
    return {
        "manifest_hash": digest(canonical(manifest)),
        "runner_hash": digest(Path(__file__).read_bytes()),
        "skill_hashes": skills,
        "install_plan_id": receipt.get("plan_id"),
    }


def maka_version(binary: str | list[str], environment: Mapping[str, str]) -> str:
    command = [binary] if isinstance(binary, str) else list(binary)
    try:
        completed = subprocess.run(
            [*command, "--version"],
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SmokeError(f"cannot run {binary} --version: {error}") from error
    if completed.returncode:
        raise SmokeError(f"{binary} --version failed: {completed.stderr.strip()}")
    return completed.stdout.strip() or completed.stderr.strip() or "unknown"


def sanitized_environment() -> dict[str, str]:
    allowed = {
        "HOME", "USER", "LOGNAME", "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR",
    }
    return {key: value for key, value in os.environ.items() if key in allowed}


def verify_executor_canaries(
    canary_dir: Path, environment: Mapping[str, str],
) -> None:
    for name in ("claude", "codex", "agy"):
        expected = canary_dir / name
        observed = shutil.which(name, path=environment.get("PATH"))
        if observed is None or Path(observed).resolve() != expected.resolve():
            raise SmokeError(f"executor canary {name} is shadowed")


def verify_canary_log(path: Path, fd: int, original: os.stat_result) -> None:
    try:
        current = path.lstat()
    except OSError as error:
        raise SmokeError("executor canary boundary was changed") from error
    held = os.fstat(fd)
    identity = (original.st_dev, original.st_ino)
    if (
        not stat.S_ISREG(current.st_mode)
        or (current.st_dev, current.st_ino) != identity
        or (held.st_dev, held.st_ino) != identity
        or current.st_nlink != 1
        or held.st_nlink != 1
    ):
        raise SmokeError("executor canary boundary was changed")
    if current.st_size or held.st_size:
        raise SmokeError("matrix scenario launched an executor canary")
    if (
        current.st_mtime_ns != original.st_mtime_ns
        or current.st_ctime_ns != original.st_ctime_ns
        or held.st_mtime_ns != original.st_mtime_ns
        or held.st_ctime_ns != original.st_ctime_ns
    ):
        raise SmokeError("executor canary boundary was changed")


def verified_model_route(
    tier_file: Path, marker: str, started_at: dt.datetime, finished_at: dt.datetime,
) -> dict[str, Any]:
    """Require every nonce-bound relay receipt for this turn to pass."""

    marker_match = re.fullmatch(
        r"maka:(?:fanout-routing|probe)-([0-9a-f]{32})-([0-9a-f]{32})", marker
    )
    if marker_match is None:
        raise SmokeError("invalid routing receipt nonce")
    expected_session_id = "fanout-routing-" + marker_match.group(2)
    marker_hash = hashlib.sha256(marker.encode("ascii")).hexdigest()
    source = Path(__file__).resolve().parents[1] / "maka/interactive/maka_openai_relay.py"
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    root = tier_file.parent / "relay-routing-receipts"
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError as error:
        raise SmokeError("Maka routing provider receipt is missing") from error
    except OSError as error:
        raise SmokeError("Maka routing provider receipt directory is unsafe") from error
    try:
        metadata = os.fstat(root_fd)
        if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o700):
            raise SmokeError("Maka routing provider receipt directory is unsafe")
        names = sorted(name for name in os.listdir(root_fd)
                       if name.startswith(marker_hash + "-"))
        if not names:
            raise SmokeError("Maka routing provider receipt is missing")
        if len(names) > 64:
            raise SmokeError("Maka routing provider receipts are ambiguous")
        receipts: list[dict[str, Any]] = []
        skill_request_times: list[dt.datetime] = []
        no_tool_request_times: list[dt.datetime] = []
        for name in names:
            match = re.fullmatch(re.escape(marker_hash) + r"-([0-9a-f]{32})\.json", name)
            if match is None:
                raise SmokeError("Maka routing provider receipts are ambiguous")
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root_fd)
                with os.fdopen(fd, "rb") as stream:
                    metadata = os.fstat(stream.fileno())
                    if (not stat.S_ISREG(metadata.st_mode)
                            or metadata.st_uid != os.getuid()
                            or stat.S_IMODE(metadata.st_mode) != 0o600
                            or metadata.st_size > 4096):
                        raise SmokeError("Maka routing provider receipt is unsafe")
                    raw = stream.read(4097)
                if len(raw) > 4096:
                    raise SmokeError("Maka routing provider receipt is unsafe")
                receipt = json.loads(raw, object_pairs_hook=unique_json_object)
            except (OSError, UnicodeDecodeError, ValueError) as error:
                raise SmokeError("Maka routing provider receipt is invalid") from error
            if not isinstance(receipt, dict) or receipt.get("schema") != "khenrix-maka-routing-request-v1":
                raise SmokeError("Maka routing provider receipt is invalid")
            if receipt.get("request_marker_sha256") != marker_hash:
                raise SmokeError("Maka routing provider receipt belongs to another session")
            if receipt.get("request_instance_id") != match.group(1):
                raise SmokeError("Maka routing provider receipts are ambiguous")
            if (not isinstance(receipt.get("prompt_sha256"), str)
                    or re.fullmatch(r"[0-9a-f]{64}", receipt["prompt_sha256"]) is None
                    or receipt.get("relay_source_sha256") != source_hash):
                raise SmokeError("Maka routing provider receipt source is unverified")
            if receipt.get("routing_session_id") != expected_session_id:
                raise SmokeError("Maka routing provider receipt belongs to another session")
            cache_key = receipt.get("prompt_cache_key")
            if cache_key is not None and cache_key != f"maka:{expected_session_id}":
                raise SmokeError("Maka routing provider receipts are ambiguous")
            binding = receipt.get("binding")
            if binding not in {"nonce", "response-chain"}:
                raise SmokeError("Maka routing provider receipt binding is unverified")
            if (binding == "response-chain"
                    and (not isinstance(receipt.get("previous_response_id_sha256"), str)
                         or re.fullmatch(r"[0-9a-f]{64}", receipt["previous_response_id_sha256"]) is None)):
                raise SmokeError("Maka routing provider response chain is unverified")
            response_id_hash = receipt.get("response_id_sha256")
            if (response_id_hash is not None
                    and (not isinstance(response_id_hash, str)
                         or re.fullmatch(r"[0-9a-f]{64}", response_id_hash) is None)):
                raise SmokeError("Maka routing provider response chain is unverified")
            if (receipt.get("requested_model") != "gpt-6.1-sol"
                    or receipt.get("observed_model") != "gpt-6.1-sol"
                    or receipt.get("requested_service_tier") != "default"
                    or receipt.get("observed_service_tier") != "default"
                    or type(receipt.get("http_status")) is not int
                    or not 200 <= receipt["http_status"] < 300):
                raise SmokeError("interactive GPT-6.1 Sol model is unverified")
            if receipt.get("outbound_effort") != "xhigh":
                raise SmokeError("interactive GPT-6.1 Sol outbound effort is unverified")
            names = receipt.get("outbound_tool_names")
            tool_count = receipt.get("tool_count")
            tool_choice = receipt.get("outbound_tool_choice")
            skill_request = (names == ["Skill", "SkillSearch"]
                             and type(tool_count) is int and tool_count == 2
                             and tool_choice in {None, "auto"})
            no_tool_request = (names == [] and type(tool_count) is int
                               and tool_count == 0 and tool_choice == "none")
            if not (skill_request or no_tool_request):
                raise SmokeError("Maka routing provider tool ceiling is unverified")
            try:
                observed_at = dt.datetime.fromisoformat(receipt["time_utc"])
            except (KeyError, TypeError, ValueError) as error:
                raise SmokeError("Maka routing provider receipt has invalid time") from error
            if (observed_at.tzinfo is None or observed_at < started_at
                    or observed_at > finished_at + dt.timedelta(seconds=2)):
                raise SmokeError("Maka routing provider receipt is stale")
            (skill_request_times if skill_request else no_tool_request_times).append(observed_at)
            receipts.append(receipt)
        if (not skill_request_times or (no_tool_request_times
                and min(no_tool_request_times) < min(skill_request_times))):
            raise SmokeError("Maka routing provider tool ceiling is unverified")
        response_ids = [receipt.get("response_id_sha256") for receipt in receipts]
        if len([value for value in response_ids if value is not None]) != len(
            {value for value in response_ids if value is not None}
        ):
            raise SmokeError("Maka routing provider response chain is ambiguous")
        for index, receipt in enumerate(receipts):
            if receipt["binding"] == "response-chain" and not any(
                receipt["previous_response_id_sha256"] == response_id
                for other_index, response_id in enumerate(response_ids)
                if other_index != index
            ):
                raise SmokeError("Maka routing provider response chain is unverified")
    finally:
        os.close(root_fd)
    return {
        "auth_route": "api-key-relay",
        "requested_model": "gpt-6.1-sol",
        "observed_model": "gpt-6.1-sol",
        "requested_effort": "xhigh",
        "outbound_effort": "xhigh",
        "observed_effort": None,
        "observed_service_tier": "default",
        "observed_at": max(receipt["time_utc"] for receipt in receipts),
        "request_marker_sha256": marker_hash,
        "routing_session_id": expected_session_id,
        "provider_visible_tools": ["Skill", "SkillSearch"],
        "provider_request_count": len(receipts),
        "provider_no_tool_request_count": len(no_tool_request_times),
        "provider_receipt_hashes": [digest(canonical(receipt)) for receipt in receipts],
    }


def verify_model_session(model_route: Mapping[str, Any], session_id: str) -> None:
    if model_route.get("routing_session_id") != session_id:
        raise SmokeError("Maka routing provider receipt belongs to another session")


def verify_provider_attempts(
    connection: sqlite3.Connection, session_id: str, model_route: dict[str, Any],
) -> None:
    """A missing relay receipt must not hide an answer-bearing main attempt."""

    after_rowid = model_route["provider_attempt_after_rowid"]
    through_rowid = model_route["provider_attempt_through_rowid"]
    row = connection.execute(
        "SELECT COUNT(*) FROM usage_model_call_attempts "
        "WHERE session_id = ? AND call_kind = 'main' AND rowid > ? AND rowid <= ?",
        (session_id, after_rowid, through_rowid),
    ).fetchone()
    count = row[0]
    if count != model_route["provider_request_count"]:
        raise SmokeError(
            "Maka routing provider attempt count does not match nonce-bound receipts"
        )
    model_route["provider_attempt_count"] = count


def open_runtime_db(path: Path) -> sqlite3.Connection:
    if not path.is_file() or path.is_symlink():
        raise SmokeError(f"Maka runtime database is missing or unsafe: {path}")
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        required = {
            "session_metadata",
            "message_admissions",
            "core_root_turn_admissions",
            "core_agent_run_events",
            "usage_model_call_attempts",
        }
        actual = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?,?,?,?)",
                tuple(sorted(required)),
            )
        }
        if actual != required:
            connection.close()
            raise SmokeError(f"Maka runtime database lacks event tables: {sorted(required - actual)}")
        return connection
    except sqlite3.Error as error:
        raise SmokeError(f"cannot inspect Maka runtime database {path}: {error}") from error


def locate_session(connection: sqlite3.Connection, cwd: Path, started_ms: int) -> str:
    rows = connection.execute(
        "SELECT session_id, payload_json, created_at FROM session_metadata "
        "WHERE created_at >= ? ORDER BY created_at, session_id",
        (started_ms - 1000,),
    ).fetchall()
    matches: list[str] = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError as error:
            raise SmokeError(f"Maka session {row['session_id']} has invalid metadata JSON") from error
        if payload.get("cwd") == str(cwd.resolve()):
            matches.append(row["session_id"])
    if len(matches) != 1:
        raise SmokeError(
            f"expected exactly one Maka session for temporary cwd {cwd}, found {len(matches)}"
        )
    return matches[0]


def verify_session_target(connection: sqlite3.Connection, session_id: str) -> None:
    try:
        row = connection.execute(
            "SELECT model, llm_connection_slug FROM session_metadata WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    except sqlite3.Error as error:
        raise SmokeError("Maka session lacks model-route evidence") from error
    if row is None or (row["model"], row["llm_connection_slug"]) != (
        "gpt-6.1-sol", "keychain-openai"
    ):
        raise SmokeError("interactive GPT-6.1 Sol session target is unverified")


def verify_routing_session(
    connection: sqlite3.Connection, session_id: str, cwd: Path,
) -> None:
    """Require the Host's durable, tool-ceiling Session header before spend."""

    row = connection.execute(
        "SELECT payload_json, model, llm_connection_slug FROM session_metadata "
        "WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        raise SmokeError("Maka routing profile is missing")
    try:
        header = json.loads(row["payload_json"])
    except (TypeError, json.JSONDecodeError) as error:
        raise SmokeError("Maka routing profile is invalid") from error
    if (
        not isinstance(header, dict)
        or header.get("cwd") != str(cwd.resolve())
        or header.get("toolProfile") != "khenrix-skill-routing-v1"
        or header.get("permissionMode") != "ask"
        or header.get("thinkingLevel") != "xhigh"
        or row["model"] != "gpt-6.1-sol"
        or row["llm_connection_slug"] != "keychain-openai"
    ):
        raise SmokeError("Maka routing profile or Session target is unverified")


def attested_maka_cli(
    home: Path, environment: Mapping[str, str],
) -> tuple[list[str], Path, dict[str, Any]]:
    """Resolve the exact installed CLI without invoking its reconciling wrapper."""

    layout = install_component.canonical_layout()
    if home.resolve() != layout.home.resolve():
        raise SmokeError("routing smoke requires the managed account profile")
    source = Path(__file__).resolve().parents[1] / "maka"
    try:
        status = component_doctor.inspect_component(source)
        if status["installReceipt"] != "current" or status["wrapper"] != "khenrix-managed":
            raise SmokeError("installed Maka runtime is not source-attested")
        receipt = component_doctor.inspect_install_receipt(layout)
        mise = install_component.resolve_mise(layout.home)
    except (component_doctor.DoctorError, install_component.ComponentInstallError) as error:
        raise SmokeError(f"installed Maka runtime is not source-attested: {error}") from error
    try:
        node_where = subprocess.run(
            [str(mise), "-C", str(layout.component), "where", "node@24.16.0"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=20, env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SmokeError(f"pinned Maka Node is unavailable: {error}") from error
    if node_where.returncode:
        raise SmokeError("pinned Maka Node is unavailable")
    node_root = Path(node_where.stdout.strip())
    expected_node_root = layout.home / ".local/share/mise/installs/node/24.16.0"
    if (
        not node_root.is_absolute()
        or node_root.is_symlink()
        or node_root != expected_node_root
    ):
        raise SmokeError("pinned Maka Node path is unverified")
    node = node_root / "bin/node"
    if node.is_symlink() or not node.is_file() or not os.access(node, os.X_OK):
        raise SmokeError("pinned Maka Node executable is unavailable")
    package = layout.component / "runtime/package"
    return [str(node), str(package / "dist/cli.js")], layout.component, receipt


def _routing_host_check(
    binary: str | list[str], cwd: Path, session_id: str, runtime_db: Path,
    environment: Mapping[str, str],
    expected_host: Mapping[str, Any] | None = None,
    *, host_only: bool = False,
    private_host: PrivateHostBinding | None = None,
) -> dict[str, Any]:
    """Use only the installed source and an already-running Maka Host."""

    if "KHENRIX_MAKA_INSTALL_BOOT_SESSION" in environment:
        raise SmokeError("Maka install boot session override is untrusted")

    cli, component, receipt = attested_maka_cli(
        install_component.canonical_layout().home, environment,
    )
    if binary != cli:
        raise SmokeError("routing smoke must use the attested Maka CLI")
    child_environment = private_host_environment(environment, runtime_db, private_host)
    helper = component / "interactive/maka_skill_routing.mjs"
    package = component / "runtime/package"
    command = [
        cli[0], str(helper),
        "--package-root", str(package), "--cwd", str(cwd),
        "--session-id", session_id,
        "--install-monotonic-ns", receipt["install_monotonic_ns"],
    ]
    if expected_host is not None:
        command.extend((
            "--expected-host-pid", str(expected_host["pid"]),
            "--expected-host-epoch", expected_host["hostEpoch"],
        ))
    if host_only:
        command.append("--host-only")
    child_environment["KHENRIX_MAKA_INSTALL_BOOT_SESSION"] = receipt["boot_session_uuid"]
    try:
        completed = subprocess.run(
            command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, timeout=45, env=child_environment,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SmokeError(f"Maka routing Host preflight failed: {error}") from error
    if completed.returncode:
        raise SmokeError(
            "Maka routing Host preflight failed: " + completed.stderr.strip()[-500:]
        )
    try:
        prepared = json.loads(completed.stdout, object_pairs_hook=unique_json_object)
    except (ValueError, TypeError) as error:
        raise SmokeError("Maka routing Host preflight returned invalid evidence") from error
    host = prepared.get("host") if isinstance(prepared, dict) else None
    boundary = prepared.get("boundary") if isinstance(prepared, dict) else None
    projection = prepared.get("toolProjection") if isinstance(prepared, dict) else None
    if (
        not isinstance(prepared, dict)
        or projection != {
            "profile": "khenrix-skill-routing-v1", "tools": ["Skill", "SkillSearch"],
        }
        or (not host_only and prepared.get("sessionId") != session_id)
        or not isinstance(host, dict)
        or not isinstance(host.get("pid"), int)
        or host["pid"] <= 0
        or not isinstance(host.get("hostEpoch"), str)
        or not host["hostEpoch"]
        or (not host_only and (not isinstance(boundary, dict) or boundary.get("kind") != "managed"))
        or (private_host is not None and prepared.get("privateHost") != {
            "rootId": private_host.root_id, "rootPath": str(private_host.root_path),
        })
    ):
        raise SmokeError("Maka routing Host preflight returned unbound evidence")
    if private_host is not None and (
        host["pid"] != private_host.attestation["pid"]
        or host["hostEpoch"] != private_host.attestation["hostEpoch"]
        or host.get("startedAtMs") != private_host.attestation["startedAtMs"]
    ):
        raise SmokeError("Maka private Host changed after native sandbox attestation")
    if not host_only:
        with open_runtime_db(runtime_db) as connection:
            verify_routing_session(connection, session_id, cwd)
    if expected_host is not None:
        if host["pid"] != expected_host["pid"] or host["hostEpoch"] != expected_host["hostEpoch"]:
            raise SmokeError("Maka routing Host changed between turns")
    elif not host_only:
        prepared["runtime_package_digest"] = receipt["runtime_package_digest"]
        prepared["tool_profile"] = "khenrix-skill-routing-v1"
    return prepared


def prepare_routing_session(
    binary: str | list[str], cwd: Path, session_id: str, runtime_db: Path,
    environment: Mapping[str, str],
    private_host: PrivateHostBinding | None = None,
) -> dict[str, Any]:
    """Create one skill-only Session on the current, source-attested Maka Host."""

    return _routing_host_check(
        binary, cwd, session_id, runtime_db, environment, private_host=private_host,
    )


def attest_routing_host(
    binary: str | list[str], cwd: Path, session_id: str, runtime_db: Path,
    environment: Mapping[str, str], expected_host: Mapping[str, Any],
    private_host: PrivateHostBinding | None = None,
) -> dict[str, Any]:
    """Recheck the same installed Host and Session without starting either."""

    return _routing_host_check(
        binary, cwd, session_id, runtime_db, environment, expected_host,
        private_host=private_host,
    )


def attest_existing_host(
    binary: str | list[str], cwd: Path, runtime_db: Path,
    environment: Mapping[str, str], expected_host: Mapping[str, Any] | None = None,
    private_host: PrivateHostBinding | None = None,
) -> dict[str, Any]:
    """Check an existing Host without creating a Session or starting a Host."""

    evidence = _routing_host_check(
        binary, cwd, "fanout-routing-" + secrets.token_hex(16), runtime_db,
        environment, expected_host, host_only=True, private_host=private_host,
    )
    return evidence["host"]


def routing_run_prefix(
    binary: list[str], cwd: Path, runtime_db: Path,
    environment: Mapping[str, str], host_identity: Mapping[str, Any],
    *, session_id: str, private_host: PrivateHostBinding | None = None,
) -> tuple[list[str], str]:
    cli, component, receipt = attested_maka_cli(
        install_component.canonical_layout().home, environment,
    )
    if binary != cli:
        raise SmokeError("Maka routing native CLI is unverified")
    private_host_environment(environment, runtime_db, private_host)
    if (
        not isinstance(host_identity.get("pid"), int)
        or host_identity["pid"] <= 0
        or not isinstance(host_identity.get("hostEpoch"), str)
        or not host_identity["hostEpoch"]
    ):
        raise SmokeError("Maka routing Host identity is unverified")
    if not re.fullmatch(r"fanout-routing-[0-9a-f]{32}", session_id):
        raise SmokeError("Maka routing Session identity is unverified")
    return [
        cli[0], str(component / "interactive/maka_skill_routing.mjs"),
        "--package-root", str(component / "runtime/package"),
        "--cwd", str(cwd),
        "--session-id", session_id,
        "--install-monotonic-ns", receipt["install_monotonic_ns"],
        "--expected-host-pid", str(host_identity["pid"]),
        "--expected-host-epoch", host_identity["hostEpoch"],
        "--enforce-session-profile",
        "--run", "--",
    ], receipt["boot_session_uuid"]


def tool_name_diagnostic(value: Any) -> str:
    if not isinstance(value, list):
        return "<missing or malformed>"
    if len(value) > 32:
        return "<too many>"
    if not all(
        isinstance(name, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", name)
        for name in value
    ):
        return "<malformed>"
    return repr(value)


def verify_skill_only_tool_events(
    connection: sqlite3.Connection, session_id: str, after_rowid: int,
) -> dict[str, Any]:
    rows = connection.execute(
        "SELECT rowid, event_id, event_type, record_json FROM core_agent_run_events "
        "WHERE session_id = ? AND rowid > ? AND event_type IN "
        "('turn_started', 'model_stream_started', 'model_stream_completed', 'tool_started', "
        "'trace_write_failed', 'event_corrupt') ORDER BY rowid",
        (session_id, after_rowid),
    ).fetchall()
    starts: list[tuple[int, str]] = []
    streams = 0
    completed = 0
    tool_starts: list[dict[str, str]] = []
    event_facts: list[dict[str, Any]] = []
    for row in rows:
        if row["event_type"] in {"trace_write_failed", "event_corrupt"}:
            raise SmokeError("Maka routing Host trace is unverified")
        try:
            record = json.loads(row["record_json"], object_pairs_hook=unique_json_object)
        except (ValueError, TypeError) as error:
            raise SmokeError("Maka routing tool event is invalid") from error
        if (
            not isinstance(record, dict)
            or record.get("id") != row["event_id"]
            or record.get("type") != row["event_type"]
            or record.get("sessionId") != session_id
            or not isinstance(record.get("turnId"), str)
            or not record["turnId"]
        ):
            raise SmokeError("Maka routing Host tool evidence is unbound")
        event_facts.append({
            "rowid": row["rowid"], "event_id": row["event_id"],
            "event_type": row["event_type"], "turn_id": record["turnId"],
        })
        if row["event_type"] == "turn_started":
            starts.append((row["rowid"], record["turnId"]))
        elif row["event_type"] == "model_stream_started":
            data = record.get("data")
            active_tools = data.get("activeTools") if isinstance(data, dict) else None
            if active_tools != ["Skill", "SkillSearch"]:
                raise SmokeError(
                    "Maka routing model-visible tools include a non-skill tool or omit a skill tool; "
                    f"observed={tool_name_diagnostic(active_tools)}"
                )
            streams += 1
            event_facts[-1]["active_tools"] = active_tools
        elif row["event_type"] == "model_stream_completed":
            completed += 1
        else:
            data = record.get("data")
            tool_name = data.get("toolName") if isinstance(data, dict) else None
            if tool_name not in {"Skill", "SkillSearch"}:
                raise SmokeError(f"Maka routing started a non-skill tool: {tool_name!r}")
            tool_starts.append({"event_id": row["event_id"], "tool_name": tool_name})
            event_facts[-1]["tool_name"] = tool_name
    if (
        len(starts) != 1 or streams == 0 or completed == 0
        or any(fact["turn_id"] != starts[0][1] for fact in event_facts)
        or any(fact["rowid"] < starts[0][0] for fact in event_facts)
    ):
        raise SmokeError("Maka routing session Host tool evidence lacks one settled Turn")
    through_rowid = connection.execute(
        "SELECT COALESCE(MAX(rowid), 0) FROM core_agent_run_events WHERE session_id = ?",
        (session_id,),
    ).fetchone()[0]
    return {
        "source": "core_agent_run_events", "session_id": session_id,
        "turn_id": starts[0][1], "event_after_rowid": after_rowid,
        "event_through_rowid": through_rowid,
        "model_visible_tools": ["Skill", "SkillSearch"],
        "model_stream_started_count": streams,
        "model_stream_completed_count": completed,
        "tool_starts": tool_starts, "non_skill_tool_starts": 0,
        "event_digest": digest(canonical(event_facts)),
    }


def verify_explicit_event(
    connection: sqlite3.Connection, session_id: str, skill: str,
    turn_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    turn_id = turn_evidence.get("turn_id")
    if (
        turn_evidence.get("session_id") != session_id
        or not isinstance(turn_id, str) or not turn_id
    ):
        raise SmokeError("explicit skill turn boundary is invalid")
    root_rows = connection.execute(
        "SELECT turn_id, record_json FROM core_root_turn_admissions "
        "WHERE session_id = ? ORDER BY admitted_at, turn_id",
        (session_id,),
    ).fetchall()
    invocations: list[tuple[str, str | int, dict[str, Any]]] = []
    for row in root_rows:
        try:
            record = json.loads(row["record_json"])
        except json.JSONDecodeError as error:
            raise SmokeError(
                f"Maka root admission {row['turn_id']} has invalid JSON"
            ) from error
        invocation = record.get("skillInvocation") if isinstance(record, dict) else None
        if not isinstance(invocation, dict):
            # A continued natural turn has an ordinary root admission beside the
            # explicit bootstrap admission. It is not evidence for or against the
            # requested explicit skill, so leave it out of the candidate set.
            continue
        invocations.append(("core_root_turn_admission", row["turn_id"], invocation))

    # Maka v44 makes a direct `maka run` call a root Turn. Older queued-message
    # receipts cannot prove which Host Turn admitted the skill and must not
    # certify this per-turn routing smoke.
    if not root_rows:
        rows = connection.execute(
            "SELECT sequence, skill_invocation_json FROM message_admissions "
            "WHERE session_id = ? ORDER BY sequence",
            (session_id,),
        ).fetchall()
        for row in rows:
            try:
                invocation = json.loads(row["skill_invocation_json"])
            except json.JSONDecodeError as error:
                raise SmokeError(
                    f"Maka message admission {row['sequence']} has invalid skill JSON"
                ) from error
            if not isinstance(invocation, dict):
                raise SmokeError(
                    f"Maka message admission {row['sequence']} has invalid skill JSON"
                )
            invocations.append(("message_admission", row["sequence"], invocation))

    matches: list[tuple[str, str | int, dict[str, Any]]] = []
    for kind, identity, invocation in invocations:
        for receipt in invocation.get("receipts", []):
            if (
                receipt.get("success") is True
                and receipt.get("invocation") == "explicit"
                and receipt.get("id") == skill
                and receipt.get("source") == "agents"
                and receipt.get("ref") == f"user:agents:{skill}"
            ):
                if receipt.get("truncated") is not False:
                    raise SmokeError(f"explicit {skill} admission used truncated instructions")
                if invocation.get("failed"):
                    raise SmokeError(f"explicit {skill} admission also recorded failures")
                if not any(item.get("id") == skill for item in invocation.get("loaded", [])):
                    raise SmokeError(f"explicit {skill} receipt lacks its loaded entry")
                if (
                    len(invocation.get("receipts", [])) != 1
                    or len(invocation.get("loaded", [])) != 1
                ):
                    raise SmokeError(f"explicit {skill} admission also recorded another skill")
                matches.append((kind, identity, receipt))
    if len(matches) != 1:
        raise SmokeError(
            f"expected one successful explicit admission for {skill}, found {len(matches)}"
        )
    kind, identity, receipt = matches[0]
    if kind != "core_root_turn_admission" or identity != turn_id:
        raise SmokeError(f"explicit {skill} admission belongs to another turn")
    evidence: dict[str, Any] = {
        "kind": kind,
        "session_id": session_id,
        "invocation": "explicit",
        "skill_id": skill,
        "skill_ref": receipt["ref"],
        "skill_source": receipt["source"],
        "truncated": False,
        "evidence_hash": digest(canonical(receipt)),
    }
    if kind == "core_root_turn_admission":
        evidence["turn_id"] = identity
    else:
        evidence["sequence"] = identity
    return evidence


def turn_skill_events(
    connection: sqlite3.Connection, session_id: str, turn_evidence: Mapping[str, Any],
) -> list[sqlite3.Row]:
    after = turn_evidence.get("event_after_rowid")
    through = turn_evidence.get("event_through_rowid")
    turn_id = turn_evidence.get("turn_id")
    if (
        turn_evidence.get("session_id") != session_id
        or not isinstance(turn_id, str) or not turn_id
        or not isinstance(after, int) or isinstance(after, bool) or after < 0
        or not isinstance(through, int) or isinstance(through, bool) or through < after
    ):
        raise SmokeError("Maka skill turn boundary is invalid")
    rows = connection.execute(
        "SELECT rowid, event_id, event_type, record_json FROM core_agent_run_events "
        "WHERE session_id = ? AND rowid > ? AND rowid <= ? "
        "AND event_type IN ('skill_loaded', 'skill_load_failed') ORDER BY rowid",
        (session_id, after, through),
    ).fetchall()
    for row in rows:
        try:
            record = json.loads(row["record_json"])
        except json.JSONDecodeError as error:
            raise SmokeError(f"Maka skill event {row['event_id']} has invalid JSON") from error
        if (
            not isinstance(record, dict)
            or record.get("id") != row["event_id"]
            or record.get("sessionId") != session_id
            or record.get("turnId") != turn_id
            or record.get("type") != row["event_type"]
        ):
            raise SmokeError("Maka skill event is unbound from its turn")
    return rows


def verify_no_natural_skill_events(
    connection: sqlite3.Connection, session_id: str, step: str,
    turn_evidence: Mapping[str, Any],
) -> None:
    if turn_skill_events(connection, session_id, turn_evidence):
        raise SmokeError(f"{step} turn loaded an unexpected skill")


def verify_natural_event(
    connection: sqlite3.Connection, session_id: str, skill: str,
    turn_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    rows = turn_skill_events(connection, session_id, turn_evidence)
    matches: list[dict[str, Any]] = []
    for row in rows:
        record = json.loads(row["record_json"])
        data = record.get("data", {})
        if row["event_type"] == "skill_load_failed":
            raise SmokeError(f"natural {skill} turn recorded a failed skill load")
        if not isinstance(data, dict) or data.get("skillId") != skill:
            raise SmokeError(f"natural {skill} turn loaded an unexpected skill")
        matches.append(_natural_load_evidence(data, row["event_id"], session_id, skill))
    if len(matches) != 1:
        raise SmokeError(f"expected one model_tool skill_loaded event for {skill}, found {len(matches)}")
    return matches[0]


def _natural_load_evidence(
    data: dict[str, Any], event_id: str, session_id: str, skill: str
) -> dict[str, Any]:
    if (
        data.get("success") is not True
        or data.get("invocation") != "model_tool"
        or data.get("skillId") != skill
        or data.get("skillSource") != "agents"
        or data.get("skillRef") != f"user:agents:{skill}"
    ):
        raise SmokeError(f"natural {skill} load has invalid source or invocation")
    if data.get("truncated") is not False:
        raise SmokeError(f"natural {skill} load used truncated instructions")
    shadow_fields = {
        "shadowCandidateCount", "shadowHitAt1", "shadowHitAt5", "shadowHitAt20",
    }
    if not shadow_fields <= set(data):
        raise SmokeError(f"natural {skill} load lacks shadow-selection evidence")
    candidate_count = data["shadowCandidateCount"]
    hits = (data["shadowHitAt1"], data["shadowHitAt5"], data["shadowHitAt20"])
    if (
        not isinstance(candidate_count, int)
        or isinstance(candidate_count, bool)
        or candidate_count < 0
        or candidate_count > 20
        or not all(isinstance(hit, bool) for hit in hits)
    ):
        raise SmokeError(f"natural {skill} load has invalid shadow-selection evidence")
    if candidate_count == 0:
        if any(hits) or "shadowRank" in data:
            raise SmokeError(f"natural {skill} load has inconsistent shadow-selection evidence")
    elif data.get("shadowRank") != 1 or hits != (True, True, True):
        raise SmokeError(f"natural {skill} was not the first-ranked shadow-selection candidate")
    return {
        "kind": "core_agent_run_event",
        "event_id": event_id,
        "session_id": session_id,
        "invocation": "model_tool",
        "skill_id": skill,
        "skill_ref": data["skillRef"],
        "skill_source": data["skillSource"],
        "truncated": False,
        "shadow_candidate_count": data["shadowCandidateCount"],
        "shadow_rank": data.get("shadowRank"),
        "evidence_hash": digest(canonical(data)),
    }


def skill_rowids(connection: sqlite3.Connection, session_id: str) -> tuple[int, int]:
    admission = connection.execute(
        "SELECT COALESCE(MAX(rowid), 0) FROM core_root_turn_admissions WHERE session_id = ?",
        (session_id,),
    ).fetchone()[0]
    event = connection.execute(
        "SELECT COALESCE(MAX(rowid), 0) FROM core_agent_run_events WHERE session_id = ?",
        (session_id,),
    ).fetchone()[0]
    return admission, event


def verify_matrix_turn(
    connection: sqlite3.Connection,
    session_id: str,
    admission_after: int,
    event_after: int,
    expected_skills: list[dict[str, str]],
    turn_evidence: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if event_after != turn_evidence.get("event_after_rowid"):
        raise SmokeError("matrix turn event boundary is inconsistent")
    observed: list[dict[str, Any]] = []
    admissions = connection.execute(
        "SELECT rowid, turn_id, record_json FROM core_root_turn_admissions "
        "WHERE session_id = ? AND rowid > ? ORDER BY rowid",
        (session_id, admission_after),
    ).fetchall()
    if len(admissions) != 1:
        raise SmokeError(f"expected one root admission for matrix turn, found {len(admissions)}")
    admission = admissions[0]
    if admission["turn_id"] != turn_evidence.get("turn_id"):
        raise SmokeError("matrix root admission is bound to another turn")
    try:
        record = json.loads(admission["record_json"])
    except json.JSONDecodeError as error:
        raise SmokeError("matrix root admission has invalid JSON") from error
    invocation = record.get("skillInvocation") if isinstance(record, dict) else None
    if invocation is not None:
        if not isinstance(invocation, dict) or invocation.get("failed"):
            raise SmokeError("matrix root admission recorded a failed skill load")
        for receipt in invocation.get("receipts", []):
            skill = receipt.get("id") if isinstance(receipt, dict) else None
            if (
                not isinstance(skill, str)
                or receipt.get("success") is not True
                or receipt.get("invocation") != "explicit"
                or receipt.get("source") != "agents"
                or receipt.get("ref") != f"user:agents:{skill}"
                or receipt.get("truncated") is not False
                or not any(
                    isinstance(item, dict) and item.get("id") == skill
                    for item in invocation.get("loaded", [])
                )
            ):
                raise SmokeError("matrix explicit skill has invalid source or truncated instructions")
            observed.append({
                "kind": "core_root_turn_admission",
                "turn_id": admission["turn_id"],
                "session_id": session_id,
                "invocation": "explicit",
                "skill_id": skill,
                "skill_ref": receipt["ref"],
                "skill_source": receipt["source"],
                "truncated": False,
                "evidence_hash": digest(canonical(receipt)),
            })
        if len(invocation.get("loaded", [])) != len(observed):
            raise SmokeError("matrix root admission has an unexpected skill")
    events = turn_skill_events(connection, session_id, turn_evidence)
    for row in events:
        if row["event_type"] != "skill_loaded":
            raise SmokeError("matrix turn recorded a failed skill load")
        try:
            record = json.loads(row["record_json"])
        except json.JSONDecodeError as error:
            raise SmokeError(f"Maka skill event {row['event_id']} has invalid JSON") from error
        data = record.get("data") if isinstance(record, dict) else None
        skill = data.get("skillId") if isinstance(data, dict) else None
        if not isinstance(skill, str):
            raise SmokeError("matrix turn recorded a skill without an ID")
        observed.append(_natural_load_evidence(data, row["event_id"], session_id, skill))
    actual = [{"id": item["skill_id"], "invocation": item["invocation"]} for item in observed]
    if actual != expected_skills:
        extras = [item for item in actual if item not in expected_skills]
        if extras or len(actual) > len(expected_skills):
            raise SmokeError(f"matrix turn loaded an unexpected skill: {extras or actual}")
        raise SmokeError(f"matrix turn lacked an expected skill: expected {expected_skills}, found {actual}")
    return observed


def run_command(
    binary: str | list[str],
    item: dict[str, Any],
    cwd: Path,
    runtime_db: Path,
    *,
    continued: bool,
    resume_session_id: str | None = None,
    environment: Mapping[str, str],
    tier_file: Path,
    host_identity: Mapping[str, Any] | None = None,
    enforce_session_profile: bool = False,
    private_host: PrivateHostBinding | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    if resume_session_id is None:
        raise SmokeError("Maka routing turn lacks its precreated Session")
    if not re.fullmatch(r"fanout-routing-[0-9a-f]{32}", resume_session_id):
        raise SmokeError("Maka routing turn has an invalid precreated Session")
    marker = ("maka:probe-" + secrets.token_hex(16) + "-"
              + resume_session_id.removeprefix("fanout-routing-"))
    if "KHENRIX_MAKA_INSTALL_BOOT_SESSION" in environment:
        raise SmokeError("Maka install boot session override is untrusted")
    child_environment = dict(environment)
    if isinstance(binary, list):
        if host_identity is None or not enforce_session_profile or resume_session_id is None:
            raise SmokeError("Maka routing native CLI lacks its Host or Session binding")
        prefix, install_boot_session = routing_run_prefix(
            binary, cwd, runtime_db, environment, host_identity,
            session_id=resume_session_id, private_host=private_host,
        )
        child_environment = private_host_environment(
            environment, runtime_db, private_host,
        )
        child_environment["KHENRIX_MAKA_INSTALL_BOOT_SESSION"] = install_boot_session
    else:
        prefix = [binary]
    prompt = f"{item['prompt']}\nReceipt nonce: {marker}"
    command = [
        *prefix,
        "run",
        prompt,
        "--cwd",
        str(cwd),
        "--timeout",
        str(item["timeout_seconds"]),
        "--max-steps",
        str(item["max_steps"]),
        "--thinking",
        "xhigh",
    ]
    if resume_session_id is not None:
        if not re.fullmatch(r"fanout-routing-[0-9a-f]{32}", resume_session_id):
            raise SmokeError("matrix Session identity is invalid")
        command.extend(("--resume", resume_session_id))
    elif continued:
        command.append("--continue")
    if "--yolo" in command:
        raise SmokeError("internal error: smoke command attempted --yolo")
    if host_identity is not None and not enforce_session_profile:
        attest_existing_host(binary, cwd, runtime_db, environment, host_identity, private_host)
    with open_runtime_db(runtime_db) as connection:
        attempt_after_rowid = connection.execute(
            "SELECT COALESCE(MAX(rowid), 0) FROM usage_model_call_attempts"
        ).fetchone()[0]
        tool_after_rowid = connection.execute(
            "SELECT COALESCE(MAX(rowid), 0) FROM core_agent_run_events WHERE session_id = ?",
            (resume_session_id,),
        ).fetchone()[0]
    started_at = dt.datetime.now(dt.timezone.utc)
    try:
        completed = subprocess.run(
            command,
            text=private_host is None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=item["timeout_seconds"] + 30,
            env=child_environment,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        if private_host is not None:
            try:
                retain_private_turn_output(
                    private_host, item, resume_session_id, prompt, marker,
                    getattr(error, "stdout", None), getattr(error, "stderr", None),
                    returncode=None, timed_out=isinstance(error, subprocess.TimeoutExpired),
                )
            except OSError:
                print("Maka private Host turn output evidence could not be retained", file=sys.stderr)
        raise SmokeError(f"case {item['id']} could not complete: {error}") from error
    finished_at = dt.datetime.now(dt.timezone.utc)
    if private_host is not None:
        try:
            retain_private_turn_output(
                private_host, item, resume_session_id, prompt, marker,
                completed.stdout, completed.stderr,
                returncode=completed.returncode, timed_out=False,
            )
        except OSError as error:
            raise SmokeError(f"case {item['id']} output evidence could not be retained") from error
        completed = subprocess.CompletedProcess(
            completed.args, completed.returncode,
            completed.stdout.decode() if isinstance(completed.stdout, bytes) else completed.stdout,
            completed.stderr.decode() if isinstance(completed.stderr, bytes) else completed.stderr,
        )
    if host_identity is not None and not enforce_session_profile:
        attest_existing_host(binary, cwd, runtime_db, environment, host_identity, private_host)
    with open_runtime_db(runtime_db) as connection:
        attempt_through_rowid = connection.execute(
            "SELECT COALESCE(MAX(rowid), 0) FROM usage_model_call_attempts"
        ).fetchone()[0]
        host_tool_evidence = verify_skill_only_tool_events(
            connection, resume_session_id, tool_after_rowid,
        )
    if completed.returncode:
        detail = completed.stderr.strip()[-1000:]
        raise SmokeError(f"case {item['id']} failed with {completed.returncode}: {detail}")
    if not completed.stdout.strip():
        raise SmokeError(f"case {item['id']} returned no model output")
    required = item.get("must_contain")
    if required and required not in completed.stdout:
        raise SmokeError(f"case {item['id']} output did not contain {required!r}")
    route = verified_model_route(tier_file, marker, started_at, finished_at)
    route["provider_attempt_after_rowid"] = attempt_after_rowid
    route["provider_attempt_through_rowid"] = attempt_through_rowid
    route["host_tool_evidence"] = host_tool_evidence
    return completed, route


def retain_private_turn_output(
    private_host: PrivateHostBinding, item: Mapping[str, Any], session_id: str,
    prompt: str, marker: str, stdout: bytes | str | None, stderr: bytes | str | None,
    *, returncode: int | None, timed_out: bool,
) -> None:
    marker_hash = hashlib.sha256(marker.encode()).hexdigest()
    path = private_host.spec_path.parent / f"turn-{marker_hash}"
    for suffix, content in ((".stdout", stdout), (".stderr", stderr)):
        data = content.encode() if isinstance(content, str) else content or b""
        _private_file(path.with_suffix(suffix), data)
    _private_file(path.with_suffix(".json"), canonical({
        "case_id": item["id"], "session_id": session_id,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "request_marker_sha256": marker_hash,
        "returncode": returncode, "timed_out": timed_out,
    }))


def needs_session_bootstrap(case: Mapping[str, Any]) -> bool:
    """Every other target runs after the root-session routing skill is loaded.

    This mirrors the shared house rule and prevents the bootstrap skill from
    competing with an explicit or natural target in one headless turn. Keeping
    each turn at two steps also prevents a routing smoke from expanding its
    sandbox merely to inspect a skill's optional reference files.
    """
    return case.get("skill") != "using-superpowers"


def verify_flow_output(step: dict[str, Any], stdout: str) -> dict[str, Any]:
    """Check session behavior without putting the expected answer in the prompt."""
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise SmokeError(f"flow step {step['id']} returned no non-empty lines")
    check = step.get("check")
    if check == "adhd_action_first":
        task_terms = ("pull request", "pr", "email", "lunch")
        action_verbs = (
            "open",
            "review",
            "read",
            "start",
            "pick",
            "choose",
            "reply",
            "prepare",
            "set",
            "make",
        )
        action_line = next(
            (
                (index, line)
                for index, line in enumerate(lines[:2])
                if any(term in line.casefold() for term in task_terms)
                and re.search(
                    r"\b(" + "|".join(action_verbs) + r")\b",
                    line.casefold(),
                )
            ),
            None,
        )
        if action_line is None:
            raise SmokeError(
                "ADHD continuation opening did not include one concrete action from the neutral task"
            )
        index, line = action_line
        return {
            "check": check,
            "opening_actionable": True,
            "action_line": index + 1,
            "action_line_hash": digest(line.encode()),
        }
    if check == "normal_mode_ack":
        normalized_output = " ".join(lines).casefold()
        normalized_prompt = " ".join(step["prompt"].split()).casefold()
        if normalized_output == normalized_prompt:
            raise SmokeError("normal-mode step merely echoed the opt-out prompt")
        acknowledged = (
            "normal mode" in normalized_output
            or (
                "adhd" in normalized_output
                and any(word in normalized_output for word in ("off", "disabled", "stopped"))
            )
        )
        if len(lines) > 2 or not acknowledged:
            raise SmokeError("normal-mode step did not give a bounded state acknowledgement")
        return {"check": check, "state_acknowledged": True, "line_count": len(lines)}
    raise SmokeError(f"flow step {step['id']} has unknown behavior check {check!r}")


def run_case(
    binary: str | list[str],
    case: dict[str, Any],
    runtime_db: Path,
    environment: Mapping[str, str],
    tier_file: Path,
    host_identity: Mapping[str, Any],
    private_host: PrivateHostBinding | None = None,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix=f"khenrix-maka-{case['id']}-") as temporary:
        cwd = Path(temporary).resolve()
        started_ms = time.time_ns() // 1_000_000
        session_id = "fanout-routing-" + secrets.token_hex(16)
        prepared = prepare_routing_session(
            binary, cwd, session_id, runtime_db, environment, private_host,
        )
        if prepared["host"] != host_identity:
            raise SmokeError("Maka routing Host changed before legacy case")

        def run_step(step: dict[str, Any], continued: bool) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
            attest_routing_host(
                binary, cwd, session_id, runtime_db, environment, host_identity, private_host,
            )
            with open_runtime_db(runtime_db) as connection:
                verify_routing_session(connection, session_id, cwd)
            completed, route = run_command(
                binary, step, cwd, runtime_db, continued=continued,
                resume_session_id=session_id, environment=environment, tier_file=tier_file,
                host_identity=host_identity, enforce_session_profile=True,
                private_host=private_host,
            )
            attest_routing_host(
                binary, cwd, session_id, runtime_db, environment, host_identity, private_host,
            )
            with open_runtime_db(runtime_db) as connection:
                verify_routing_session(connection, session_id, cwd)
            return completed, route

        bootstrap_completed: subprocess.CompletedProcess[str] | None = None
        bootstrap_model_route: dict[str, Any] | None = None
        if needs_session_bootstrap(case):
            bootstrap_completed, bootstrap_model_route = run_step(SESSION_BOOTSTRAP, False)
            with open_runtime_db(runtime_db) as connection:
                verify_no_natural_skill_events(
                    connection, session_id, "bootstrap",
                    bootstrap_model_route["host_tool_evidence"],
                )
        completed, model_route = run_step(case, bootstrap_completed is not None)
        with open_runtime_db(runtime_db) as connection:
            observed_session = locate_session(connection, cwd, started_ms)
            if observed_session != session_id:
                raise SmokeError(f"case {case['id']} opened a new Session")
            verify_routing_session(connection, session_id, cwd)
            verify_session_target(connection, session_id)
            verify_model_session(model_route, session_id)
            verify_provider_attempts(connection, session_id, model_route)
            if bootstrap_model_route is not None:
                verify_model_session(bootstrap_model_route, session_id)
                verify_provider_attempts(connection, session_id, bootstrap_model_route)
            bootstrap_evidence = (
                verify_explicit_event(
                    connection, session_id, "using-superpowers",
                    bootstrap_model_route["host_tool_evidence"],
                )
                if bootstrap_completed is not None
                else None
            )
            evidence = (
                verify_explicit_event(
                    connection, session_id, case["skill"],
                    model_route["host_tool_evidence"],
                )
                if case["route"] == "explicit"
                else verify_natural_event(
                    connection, session_id, case["skill"],
                    model_route["host_tool_evidence"],
                )
            )
            if case["route"] == "explicit":
                verify_no_natural_skill_events(
                    connection, session_id, "explicit target",
                    model_route["host_tool_evidence"],
                )
    return {
        "id": case["id"],
        "validation_only": True,
        "session_id": session_id,
        "host_preflight": prepared,
        "route": case["route"],
        "skill": case["skill"],
        "model_route": model_route,
        "prompt_hash": digest(case["prompt"].encode()),
        "stdout_hash": digest(completed.stdout.encode()),
        "stdout_bytes": len(completed.stdout.encode()),
        "stderr_hash": digest(completed.stderr.encode()),
        "session_bootstrap": (
            {
                "prompt_hash": digest(SESSION_BOOTSTRAP["prompt"].encode()),
                "stdout_hash": digest(bootstrap_completed.stdout.encode()),
                "stdout_bytes": len(bootstrap_completed.stdout.encode()),
                "stderr_hash": digest(bootstrap_completed.stderr.encode()),
                "event_evidence": bootstrap_evidence,
                "model_route": bootstrap_model_route,
            }
            if bootstrap_completed is not None
            else None
        ),
        "event_evidence": evidence,
        "status": "pass",
    }


def run_adhd_flow(
    binary: str | list[str],
    flow: dict[str, Any],
    runtime_db: Path,
    environment: Mapping[str, str],
    tier_file: Path,
    host_identity: Mapping[str, Any],
    private_host: PrivateHostBinding | None = None,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="khenrix-maka-adhd-session-") as temporary:
        cwd = Path(temporary).resolve()
        started_ms = time.time_ns() // 1_000_000
        session_id = "fanout-routing-" + secrets.token_hex(16)
        prepared = prepare_routing_session(
            binary, cwd, session_id, runtime_db, environment, private_host,
        )
        if prepared["host"] != host_identity:
            raise SmokeError("Maka routing Host changed before ADHD flow")
        evidence: dict[str, Any] | None = None
        for index, step in enumerate(flow["steps"]):
            attest_routing_host(
                binary, cwd, session_id, runtime_db, environment, host_identity, private_host,
            )
            with open_runtime_db(runtime_db) as connection:
                verify_routing_session(connection, session_id, cwd)
            completed, model_route = run_command(
                binary, step, cwd, runtime_db, continued=step["continue"],
                resume_session_id=session_id, environment=environment,
                tier_file=tier_file, host_identity=host_identity,
                enforce_session_profile=True,
                private_host=private_host,
            )
            attest_routing_host(
                binary, cwd, session_id, runtime_db, environment, host_identity, private_host,
            )
            with open_runtime_db(runtime_db) as connection:
                observed = locate_session(connection, cwd, started_ms)
                verify_routing_session(connection, observed, cwd)
                verify_session_target(connection, observed)
                verify_model_session(model_route, observed)
                verify_provider_attempts(connection, observed, model_route)
                if observed != session_id:
                    raise SmokeError("ADHD continuation opened a new session")
                if evidence is None:
                    evidence = verify_explicit_event(
                        connection, observed, flow["skill"],
                        model_route["host_tool_evidence"],
                    )
            results.append(
                {
                    "id": step["id"],
                    "continued": step["continue"],
                    "prompt_hash": digest(step["prompt"].encode()),
                    "stdout_hash": digest(completed.stdout.encode()),
                    "stdout_bytes": len(completed.stdout.encode()),
                    "model_route": model_route,
                    "behavior_evidence": (
                        None if index == 0 else verify_flow_output(step, completed.stdout)
                    ),
                    "status": "pass",
                }
            )
    assert evidence is not None
    return {
        "id": flow["id"],
        "validation_only": True,
        "host_preflight": prepared,
        "skill": flow["skill"],
        "session_id": session_id,
        "activation_evidence": evidence,
        "steps": results,
        "status": "pass",
    }


def verify_matrix_output(step: dict[str, Any], stdout: str) -> dict[str, Any] | None:
    if step.get("check") != "maka_executor_refusal":
        return None
    answer = " ".join(stdout.casefold().split())
    if answer == " ".join(step["prompt"].casefold().split()):
        raise SmokeError("Maka executor refusal merely echoed the prompt")
    unquoted = re.sub(r'"[^"]*"|“[^”]*”|‘[^’]*’|`[^`]*`', " ", answer)
    declarative = " ".join(
        clause.strip()
        for clause in re.split(r"(?<=[.!?;])\s*", unquoted)
        if clause.strip() and not clause.rstrip().endswith("?")
    )
    refusal_patterns = (
        r"\bmaka\b[^.!?]{0,120}\b(?:cannot|can't|must not|may not|should not|never)\s+execute\b",
        r"\bmaka\s+(?:cannot|can't|must not|may not|should not|never)\s+be\s+"
        r"(?:an?\s+)?(?:repo-write\s+)?executor\b",
        r"\bmaka\s+is\s+not\s+(?:an?\s+)?(?:repo-write\s+)?executor\b",
        r"\bmaka\s+is\s+not\s+allowed\s+to\s+execute\b",
        r"\bmaka\b[^.!?]{0,80}\b(?:it\s+)?never\s+executes?\b",
        r"\bmaka\b[^.!?]{0,120}\bbut may not be its executor\b",
        r"\bmaka\b[^.!?]{0,120}\bit\s+does\s+not\s+execute\b",
    )
    contradictory_permission = (
        re.search(r"\b(?:maka|it|i|we)\s+(?:can|may|will|should)\s+execute\b", declarative)
        or re.search(r"\b(?:maka|it)\s+is\s+(?:allowed|permitted)\s+to\s+execute\b", declarative)
    )
    if contradictory_permission or not any(re.search(pattern, declarative) for pattern in refusal_patterns):
        raise SmokeError("Maka executor refusal was not observed")
    return {"check": "maka_executor_refusal", "refused": True,
            "answer_hash": digest(stdout.encode())}


def run_matrix_case(
    binary: str | list[str],
    scenario: dict[str, Any],
    runtime_db: Path,
    environment: Mapping[str, str],
    tier_file: Path,
    private_host: PrivateHostBinding | None = None,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    with (
        tempfile.TemporaryDirectory(prefix=f"khenrix-maka-{scenario['id']}-") as temporary,
        tempfile.TemporaryDirectory(prefix="khenrix-maka-executor-canaries-") as guard_temporary,
    ):
        cwd = Path(temporary).resolve()
        guard_root = Path(guard_temporary).resolve()
        canary_dir = guard_root / "executor-canaries"
        canary_dir.mkdir(mode=0o700)
        canary_log = guard_root / "executor-launches.log"
        canary_environment = dict(environment)
        canary_environment["PATH"] = str(canary_dir) + os.pathsep + environment.get("PATH", os.defpath)
        canary_environment["KHENRIX_MAKA_SMOKE_CANARY_LOG"] = str(canary_log)
        for name in ("claude", "codex", "agy"):
            canary = canary_dir / name
            canary.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$0" >> "$KHENRIX_MAKA_SMOKE_CANARY_LOG"\nexit 97\n'
            )
            canary.chmod(0o700)
        verify_executor_canaries(canary_dir, canary_environment)
        fd = os.open(canary_log, os.O_CREAT | os.O_EXCL | os.O_RDONLY | os.O_NOFOLLOW, 0o600)
        try:
            original = os.fstat(fd)
            started_ms = time.time_ns() // 1_000_000
            session_id = "fanout-routing-" + secrets.token_hex(16)
            host_preflight = prepare_routing_session(
                binary, cwd, session_id, runtime_db, canary_environment, private_host,
            )
            if (
                not isinstance(binary, (str, list))
                or not binary
                or (isinstance(binary, list) and any(
                    not isinstance(part, str) or not part for part in binary
                ))
            ):
                raise SmokeError("Maka routing CLI is unverified")
            admission_after = 0
            event_after = 0
            for step in scenario["steps"]:
                turn = dict(step)
                if step.get("check") == "maka_executor_refusal":
                    turn["prompt"] = step["prompt"].replace(
                        "{absolute_canary_path}", str(canary_dir / "codex"),
                    )
                attest_routing_host(
                    binary, cwd, session_id, runtime_db, canary_environment,
                    host_preflight["host"], private_host,
                )
                with open_runtime_db(runtime_db) as connection:
                    verify_routing_session(connection, session_id, cwd)
                completed, model_route = run_command(
                    binary, turn, cwd, runtime_db, continued=step["continue"],
                    resume_session_id=session_id,
                    environment=canary_environment, tier_file=tier_file,
                    host_identity=host_preflight["host"], enforce_session_profile=True,
                    private_host=private_host,
                )
                verify_canary_log(canary_log, fd, original)
                attest_routing_host(
                    binary, cwd, session_id, runtime_db, canary_environment,
                    host_preflight["host"], private_host,
                )
                with open_runtime_db(runtime_db) as connection:
                    observed_session = locate_session(connection, cwd, started_ms)
                    verify_routing_session(connection, observed_session, cwd)
                    verify_session_target(connection, observed_session)
                    verify_model_session(model_route, observed_session)
                    verify_provider_attempts(connection, observed_session, model_route)
                    if observed_session != session_id:
                        raise SmokeError(f"matrix scenario {scenario['id']} opened a new session")
                    skill_evidence = verify_matrix_turn(
                        connection, session_id, admission_after, event_after,
                        step["expected_skills"], model_route["host_tool_evidence"],
                    )
                    admission_after, event_after = skill_rowids(connection, session_id)
                results.append({
                    "id": step["id"],
                    "continued": step["continue"],
                    "expected_skills": step["expected_skills"],
                    "skill_evidence": skill_evidence,
                    "model_route": model_route,
                    "prompt_hash": digest(turn["prompt"].encode()),
                    "stdout_hash": digest(completed.stdout.encode()),
                    "stdout_bytes": len(completed.stdout.encode()),
                    "behavior_evidence": verify_matrix_output(turn, completed.stdout),
                    "status": "pass",
                })
        finally:
            os.close(fd)
    return {
        "id": scenario["id"],
        "validation_only": True,
        "session_id": session_id,
        "host_preflight": host_preflight,
        "host_tool_boundary": {
            "tool_profile": host_preflight["toolProjection"]["profile"],
            "profile_tools": host_preflight["toolProjection"]["tools"],
            "audited_turns": len(results),
            "non_skill_tool_starts": sum(
                step["model_route"]["host_tool_evidence"]["non_skill_tool_starts"]
                for step in results
            ),
        },
        "steps": results,
        "status": "pass",
    }


def write_receipt(path: Path, receipt: dict[str, Any], config: skillctl.Configuration) -> None:
    skillctl.write_private_json(path, receipt, config)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2])
    result.add_argument("--home", type=Path, default=Path.home())
    result.add_argument("--state-dir", type=Path)
    result.add_argument("--manifest", type=Path, default=Path(__file__).with_name("maka-smoke.json"))
    result.add_argument("--runtime-db", type=Path)
    result.add_argument("--tier-file", type=Path)
    result.add_argument("--dry-run", action="store_true")
    result.add_argument(
        "--retain-private-host", action="store_true",
        help="Retain the private native Host and its evidence after successful settlement",
    )
    result.add_argument("--diagnostic-scenario", help="Run one matrix scenario without a certification receipt")
    return result


def main(
    argv: list[str] | None = None, *, _test_cli_command: list[str] | None = None,
) -> int:
    options = parser().parse_args(argv)
    try:
        config = skillctl.load_configuration(
            options.repo_root.resolve(), options.home.resolve(), options.state_dir
        )
        manifest = load_cases(options.manifest.resolve())
        diagnostic = None
        if options.diagnostic_scenario is not None:
            diagnostic = next(
                (item for item in manifest["matrix"]
                 if item["id"] == options.diagnostic_scenario), None,
            )
            if diagnostic is None:
                raise SmokeError("unknown Maka diagnostic matrix scenario")
        closure = installed_closure(config, manifest)
        environment = sanitized_environment()
        runtime_db = options.runtime_db or default_runtime_db(config.home)
        if _test_cli_command is None:
            binary, component, install_receipt = attested_maka_cli(config.home, environment)
        else:
            if len(_test_cli_command) != 1 or not isinstance(_test_cli_command[0], str) or not _test_cli_command[0]:
                raise SmokeError("test CLI command is invalid")
            binary = _test_cli_command[0]
            component = None
            install_receipt = None
        if _test_cli_command is None and (
            not isinstance(binary, list) or len(binary) != 2
            or not isinstance(component, Path)
            or not isinstance(install_receipt, dict)
        ):
            raise SmokeError("production Maka routing requires a private native Host")
        version = maka_version(binary, environment)
        tier_file = options.tier_file or (
            config.home / ".local" / "state" / "khenrix-utils" / "maka" / "relay-last-tier.json"
        )
        mode_path = config.home / ".config" / "khenrix-utils" / "maka" / "maka-auth-mode"
        if read_mode(mode_path) != "api-key-relay":
            raise SmokeError("Maka routing smoke requires the installed api-key-relay route")
        if options.dry_run:
            if diagnostic is not None:
                print(
                    f"DIAGNOSTIC PLAN {diagnostic['id']}: "
                    f"{len(diagnostic['steps'])} bounded turns; no certification receipt"
                )
                return 0
            bootstrap_turns = sum(needs_session_bootstrap(case) for case in manifest["cases"])
            total = (
                len(manifest["cases"])
                + bootstrap_turns
                + sum(len(flow["steps"]) for flow in manifest["flows"])
                + sum(len(scenario["steps"]) for scenario in manifest["matrix"])
            )
            print(f"Maka {version}; {total} bounded turns; --yolo absent")
            for case in manifest["cases"]:
                if needs_session_bootstrap(case):
                    print(f"PLAN {case['id']}-bootstrap explicit using-superpowers")
                print(f"PLAN {case['id']} {case['route']} {case['skill']}")
            for flow in manifest["flows"]:
                print(f"PLAN {flow['id']} multi-turn {flow['skill']}")
            for scenario in manifest["matrix"]:
                print(f"PLAN {scenario['id']} validation-only matrix")
            return 0
        host_context = (
            private_routing_host(
                binary, component, install_receipt, environment,
                retain_private_host=options.retain_private_host,
            )
            if isinstance(binary, list) else nullcontext(None)
        )
        with host_context as private_host:
            if _test_cli_command is None and private_host is None:
                raise SmokeError("production Maka routing requires a private native Host")
            if private_host is not None:
                runtime_db = private_host.root_path / "runtime.sqlite"
                if options.runtime_db is not None and options.runtime_db.resolve() != runtime_db:
                    raise SmokeError("Maka private Host runtime database override is untrusted")
            host_identity = attest_existing_host(
                binary, Path.cwd(), runtime_db, environment, None, private_host,
            )
            if diagnostic is not None:
                run_matrix_case(
                    binary, diagnostic, runtime_db, environment, tier_file, private_host,
                )
                print(
                    f"DIAGNOSTIC PASS {diagnostic['id']}; "
                    "no certification receipt was written"
                )
                return 0
            results = [
                run_case(
                    binary, case, runtime_db, environment, tier_file, host_identity,
                    private_host,
                )
                for case in manifest["cases"]
            ]
            flows = [
                run_adhd_flow(
                    binary, flow, runtime_db, environment, tier_file, host_identity,
                    private_host,
                )
                for flow in manifest["flows"]
            ]
            matrix = [
                run_matrix_case(
                    binary, scenario, runtime_db, environment, tier_file, private_host,
                )
                for scenario in manifest["matrix"]
            ]
        route = {key: results[0]["model_route"][key] for key in (
            "auth_route", "requested_model", "observed_model", "requested_effort",
            "outbound_effort",
            "observed_effort", "observed_service_tier",
        )}
        receipt = {
            "schema_version": 2,
            "ran_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "maka_version": version,
            "model_route": route,
            "input_hash": digest(canonical({**closure, "maka_version": version})),
            **closure,
            "cases": results,
            "flows": flows,
            "matrix": matrix,
            "bounded": {
                "yolo": False,
                "temporary_cwd_per_case": True,
                "root_session_bootstrap": True,
                "provider_credentials_removed": True,
                "host_tool_event_audit": True,
                "skill_only_tool_profile": True,
                "existing_host_only": private_host is None,
                **({"private_native_sandbox_host": True,
                    "absolute_and_self_exec_denied": True}
                   if private_host is not None else {}),
            },
            **({"private_host_attestation": private_host.attestation}
               if private_host is not None else {}),
        }
        receipt_path = config.state_dir / "maka-smoke-receipt.json"
        write_receipt(receipt_path, receipt, config)
        print(
            f"PASS {len(results)} Maka routing cases, {len(flows)} multi-turn flow, "
            f"and {len(matrix)} fanout matrix scenarios"
        )
        print(f"receipt {receipt_path}")
        return 0
    except (SmokeError, skillctl.DeliveryError, AuthModeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
