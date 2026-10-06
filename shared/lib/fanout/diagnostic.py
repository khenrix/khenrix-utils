"""Controller-owned, disposable provider turns through the native broker boundary."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import sys
import time
from dataclasses import asdict, dataclass, replace
from functools import partial
from pathlib import Path
from typing import Sequence

from .broker import BrokerListener
from .artifacts import ArtifactRef, ArtifactStore, canonical_json
from .container_broker import PermitLedger
from .controller import LifecycleController, read_evidence, write_evidence
from .errors import ProviderProtocolError, ProviderRequestError
from .native_boundary import (
    NativeSeatBoundary, _broker_binding, issue_broker_admission,
    issue_native_boundary, wrap_native_command,
)
from .process import ProcessCommand, ProcessResult, ProcessStatus, run_command
from .providers import (
    AgyAdapter, ClaudeAdapter, CodexAdapter, ProviderRegistry, ProviderRequest,
    ProviderResult, _make_result,
)
from .repo import SeatWorkspaceVerification


_CATEGORY = "brokered-provider-diagnostic"
_QUALITY_ALLOCATION = "quality-case-allocation"
_QUALITY_REQUEST_CONTROLS = frozenset({
    "executor_id", "execution_class", "session_id", "resume", "timeout",
    "timeout_override_reason", "timeout_override_review_sha256", "retries",
    "artifact_prefix", "slot_cap", "slot_timeout", "context_sha256",
    "skill_bundle_sha256", "skill_delivery_sha256", "run_id", "task_id",
    "target_id", "seat_id", "inputs_digest", "cwd", "slot_root", "staged_skill_root",
})


def _quality_request_material(request, prefix):
    controls = {name: getattr(request, name) for name in _QUALITY_REQUEST_CONTROLS}
    for name in ("cwd", "slot_root", "staged_skill_root"):
        controls[name] = None if controls[name] is None else str(controls[name])
    prompt_bytes, profile_bytes = request.prompt_bytes, canonical_json(request.profile.to_dict())
    prompt = ArtifactRef(prefix + "/prompt.bin", hashlib.sha256(prompt_bytes).hexdigest(), len(prompt_bytes))
    profile = ArtifactRef(prefix + "/profile.json", hashlib.sha256(profile_bytes).hexdigest(), len(profile_bytes))
    value = {
        "schema": "fanout-quality-request-v1", "controls": controls,
        "prompt": asdict(prompt), "profile": asdict(profile)}
    if (prompt.size > 1024 * 1024 or profile.size > 64 * 1024
            or len(canonical_json(value)) > 64 * 1024):
        raise ProviderRequestError("quality retained request, prompt or profile exceeds its bound")
    return value, prompt_bytes, profile_bytes


def _retain_quality_request(store, request, prefix):
    value, prompt_bytes, profile_bytes = _quality_request_material(request, prefix)
    prompt, profile = (ArtifactRef(**value[key]) for key in ("prompt", "profile"))
    store.write_bytes(prompt.path, prompt_bytes)
    store.write_bytes(profile.path, profile_bytes)
    return store.write_json(prefix + "/request.json", value)


def _read_quality_request_artifact(store, ref, limit):
    if (type(store) is not ArtifactStore or type(ref) is not ArtifactRef
            or type(ref.size) is not int or not 0 <= ref.size <= limit
            or type(ref.digest) is not str or re.fullmatch(r"[0-9a-f]{64}", ref.digest) is None):
        raise ProviderRequestError("quality retained artifact reference differs or exceeds its bound")
    parts = store._parts(ref.path)
    store._reject_reserved_path(parts)
    root_fd = store._root_fd_copy()
    try:
        parent_fd = store._open_parent(root_fd, parts, create=False)
    finally:
        os.close(root_fd)
    try:
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                    or before.st_size != ref.size or before.st_size > limit):
                raise ProviderRequestError("quality retained artifact actual size or ownership differs")
            remaining, chunks = before.st_size, []
            while remaining:
                chunk = os.read(fd, min(remaining, 64 * 1024))
                if not chunk:
                    raise ProviderRequestError("quality retained artifact changed during bounded read")
                chunks.append(chunk)
                remaining -= len(chunk)
            after = os.fstat(fd)
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ProviderRequestError("quality retained artifact changed during bounded read")
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)
    raw = b"".join(chunks)
    if hashlib.sha256(raw).hexdigest() != ref.digest:
        raise ProviderRequestError("quality retained artifact bytes differ")
    return raw


def _read_quality_request_json(store, ref):
    from .artifacts import _unique_json_object, _finite_json_number
    return json.loads(_read_quality_request_artifact(store, ref, 64 * 1024),
        object_pairs_hook=_unique_json_object, parse_constant=_finite_json_number,
        parse_float=_finite_json_number)


def _reopen_quality_request(store, ref, request):
    from .providers import ExecutorProfile
    from .skills import SkillAdmission
    if type(request) is not ProviderRequest or type(request.profile) is not ExecutorProfile:
        raise ProviderRequestError("quality predecessor requires the exact admitted request")
    if request.staged_skill_admission is not None and type(request.staged_skill_admission) is not SkillAdmission:
        raise ProviderRequestError("quality predecessor skill admission differs")
    if ref.size > 64 * 1024:
        raise ProviderRequestError("quality retained request exceeds its bound")
    value = _read_quality_request_json(store, ref)
    if (set(value) != {"schema", "controls", "prompt", "profile"}
            or value["schema"] != "fanout-quality-request-v1"
            or set(value["controls"]) != _QUALITY_REQUEST_CONTROLS):
        raise ProviderRequestError("quality retained request fields differ")
    prompt, profile_ref = (ArtifactRef(**value[key]) for key in ("prompt", "profile"))
    if prompt.size > 1024 * 1024 or profile_ref.size > 64 * 1024:
        raise ProviderRequestError("quality retained prompt or profile exceeds its bound")
    adapter = ProviderRegistry.default().require(request.executor_id)
    profile = ExecutorProfile.from_dict(_read_quality_request_json(store, profile_ref), adapter=adapter)
    if profile.digest != request.profile.digest:
        raise ProviderRequestError("quality retained profile differs from resume")
    original = ProviderRequest(**value["controls"], prompt=_read_quality_request_artifact(store, prompt, 1024 * 1024), profile=profile,
        artifact_store=store, staged_skill_admission=request.staged_skill_admission,
        skill_delivery_ref=request.skill_delivery_ref, agy_guard=request.agy_guard)
    if (original.resume or original.execution_class != "read-only" or original.retries != 0
            or any(getattr(original, name) != getattr(request, name) for name in (
                "executor_id", "execution_class", "run_id", "task_id", "target_id", "seat_id",
                "inputs_digest", "context_sha256", "skill_bundle_sha256", "skill_delivery_sha256"))
            or str(original.cwd) != str(request.cwd)
            or original.staged_skill_root != request.staged_skill_root):
        raise ProviderRequestError("quality retained original association differs from resume")
    return original


def _read_quality_container_request(store, ref):
    """Recover only retained bytes and controls; command readers prove their authority."""
    from .providers import ExecutorProfile
    try:
        value = _read_quality_request_json(store, ref)
        if (type(value) is not dict or set(value) != {"schema", "controls", "prompt", "profile"}
                or value["schema"] != "fanout-quality-request-v1"
                or type(value["controls"]) is not dict
                or set(value["controls"]) != _QUALITY_REQUEST_CONTROLS):
            raise ValueError("retained request fields")
        provider = value["controls"]["executor_id"]
        profile = ExecutorProfile.from_dict(_read_quality_request_json(store, ArtifactRef(**value["profile"])),
            adapter=ProviderRegistry.default().require(provider))
        request = ProviderRequest(**value["controls"], profile=profile, artifact_store=store,
            prompt=_read_quality_request_artifact(store, ArtifactRef(**value["prompt"]), 1024 * 1024))
        pinned = ProviderRegistry.default().select(provider, "read-only", profile.quality_tier)
        if (request.execution_class != "read-only" or request.retries != 0 or profile.digest != pinned.digest
                or not request.resume and provider != "claude" and request.session_id is not None):
            raise ValueError("retained quality controls")
        return request
    except Exception as error:
        raise ProviderRequestError("quality retained container request differs") from error


def _read_quality_container_turn(controller, store, proof, *, authority, bindings,
                                 backend_attachment, request=None):
    from .quality_case import _read_quality_backend_attachment
    try:
        value = read_evidence(controller, "quality-readonly-turn", *proof)
        if (value.get("schema") != "fanout-quality-container-readonly-turn-v1"
                or value.get("backend_attachment") != list(backend_attachment)):
            raise ValueError("explicit original container backend")
        command = read_evidence(controller, "quality-container-command", *value["command"])
        registration = command["registration"]
        row = registration["row"]
        if (command["allocation"] != list(authority) or command["store"] != str(store.root)
                or command["bindings"] != bindings
                or command["backend_attachment"] != list(backend_attachment)
                or registration["backend_attachment"] != list(backend_attachment)):
            raise ValueError("original allocated command")
        _read_quality_backend_attachment(controller, store, backend_attachment,
            authority=authority, bindings=bindings, row=row)
        if request is None:
            request = _read_quality_container_request(store, ArtifactRef(**value["request"]))
        from .container_boundary import validate_quality_container_readonly_turn
        actual = validate_quality_container_readonly_turn(controller, store, request, tuple(proof),
            backend_attachment=backend_attachment)
        if actual != value:
            raise ValueError("original container turn")
        terminal = read_evidence(controller, "quality-container-terminal", *actual["terminal"])
        return request, actual, command, terminal
    except Exception as error:
        raise ProviderRequestError("quality original container completion differs") from error


@dataclass(frozen=True, slots=True)
class QualityPredecessor:
    """Pointers to existing authority, reauthenticated on every quality-only use."""

    quality_proof: tuple[str, str]
    receipt_name: str
    receipt_digest: str
    row_digest: str
    session_id: str


@dataclass(frozen=True, slots=True)
class QualityContainerPredecessor:
    """Original container pointers, never a native boundary or in-memory result."""

    quality_proof: tuple[str, str]
    backend_attachment: tuple[str, str]
    row_digest: str
    session_id: str


def reopen_quality_predecessor(controller, store, request, proof, *, verification,
                               authority, bindings, initial_digest,
                               backend_attachment=None) -> QualityPredecessor | QualityContainerPredecessor:
    """Reopen an allocated original without recreating a result, boundary or listener."""
    if backend_attachment is not None:
        return _reopen_quality_container_predecessor(controller, store, request, proof,
            verification=verification, authority=authority, bindings=bindings,
            initial_digest=initial_digest, backend_attachment=backend_attachment)
    from .native_boundary import _require_broker_initial
    from .repo import validate_seat_workspace
    # Cold reopening must load the same fixed quality command builder as issuance.
    from . import eval_provider  # noqa: F401
    try:
        if (type(store) is not ArtifactStore or type(request) is not ProviderRequest
                or not request.resume or request.execution_class != "read-only" or request.retries != 0
                or request.native_boundary is not None):
            raise ValueError("exact quality resume")
        if type(verification) is not SeatWorkspaceVerification:
            raise ValueError("issued workspace verification")
        validate_seat_workspace(controller, verification)
        allocation = reopen_quality_allocation(controller, store, authority, bindings=bindings)
        value = read_evidence(controller, "quality-readonly-turn", *proof)
        original = _reopen_quality_request(store, ArtifactRef(**value["request"]), request)
        validate_quality_readonly_turn(controller, store, original, proof)
        command = read_evidence(controller, "quality-native-command", *value["command"])
        registration = command["registration"]
        row = registration["row"]
        if (command["allocation"] != list(authority) or command["bindings"] != bindings
                or row not in allocation["rows"] or row["initial"] is not None
                or quality_row_identity(row) != initial_digest
                or registration["initial_receipt"] is not None):
            raise ValueError("allocated original")
        claim = {"allocation": authority[1], "row": row}
        digest = hashlib.sha256(canonical_json(claim)).hexdigest()
        from .container_certification import _read_owned_json
        actual, _ = _read_owned_json(controller.root / "locks" / f"quality-row-{digest}")
        pointer, _ = _read_owned_json(controller.root / "locks" / f"quality-registration-{digest}")
        if (actual != claim or read_evidence(controller, "quality-permit-registration",
                *pointer["authority"]) != registration):
            raise ValueError("original row consumption")
        parsed = ProviderRegistry((original.profile,)).require(original.executor_id).parse(
            store.read_bytes(ArtifactRef(**value["stdout"])))
        terminal_ref = tuple(value["terminal"])
        terminal = read_evidence(controller, _CATEGORY, *terminal_ref)
        if (not parsed.final or parsed.error is not None or not parsed.session_id
                or parsed.session_id != request.session_id or terminal["session_id"] != parsed.session_id
                or parsed.observed_model != terminal["observed_model"]):
            raise ValueError("original raw session")
        _require_broker_initial(controller, verification, request, terminal_ref)
        boundary_digest = terminal["boundary_receipt_digest"]
        boundary = read_evidence(controller, "native-seat-boundary",
                                 f"{boundary_digest}.json", boundary_digest)
        admission = read_evidence(controller, "native-broker-diagnostic-admission",
            boundary["broker_admission_name"], boundary["broker_admission_digest"])
        if admission.get("quality_admission") != value["command"]:
            raise ValueError("original quality native admission")
        return QualityPredecessor(tuple(proof), *terminal_ref, initial_digest, parsed.session_id)
    except Exception as error:
        raise ProviderRequestError("quality predecessor authority or original completion differs") from error


def _reopen_quality_container_predecessor(controller, store, request, proof, *, verification,
                                          authority, bindings, initial_digest, backend_attachment):
    from .repo import validate_seat_workspace
    try:
        if (type(store) is not ArtifactStore or type(request) is not ProviderRequest
                or not request.resume or request.execution_class != "read-only" or request.retries != 0
                or any(getattr(request, key) is not None for key in
                    ("native_boundary", "native_controller", "native_verification", "container_boundary", "canary_capture"))
                or request.certificate_roots or type(verification) is not SeatWorkspaceVerification):
            raise ValueError("exact admitted container resume")
        validate_seat_workspace(controller, verification)
        if (Path(request.cwd) != verification.workspace.root
                or request.seat_id != verification.workspace.seat_id):
            raise ValueError("same original admitted container workspace")
        value = read_evidence(controller, "quality-readonly-turn", *proof)
        original = _reopen_quality_request(store, ArtifactRef(**value["request"]), request)
        _, value, command, terminal = _read_quality_container_turn(controller, store, proof,
            authority=authority, bindings=bindings, backend_attachment=backend_attachment, request=original)
        registration = command["registration"]
        row = registration["row"]
        parsed = ProviderRegistry((original.profile,)).require(original.executor_id).parse(
            _read_quality_request_artifact(store, ArtifactRef(**value["stdout"]), 8 * 1024 * 1024))
        if (row["role"] != "candidate-seat" or row["phase"] != "initial"
                or row["initial"] is not None or registration["initial_receipt"] is not None
                or quality_row_identity(row) != initial_digest or not parsed.final or parsed.error is not None
                or not parsed.session_id or parsed.session_id != request.session_id
                or terminal["session_id"] != parsed.session_id):
            raise ValueError("actual original row and raw own session")
        return QualityContainerPredecessor(tuple(proof), backend_attachment, initial_digest, parsed.session_id)
    except Exception as error:
        raise ProviderRequestError("quality container predecessor authority or original completion differs") from error


def reopen_quality_container_delivery_turn(controller, store, request, proof, *, authority,
                                            bindings, row, backend_attachment):
    """Join a reconstructed service request to its genuine stopped container chain."""
    _, value, command, terminal = _read_quality_container_turn(controller, store, proof,
        authority=authority, bindings=bindings, backend_attachment=backend_attachment, request=request)
    if (command["registration"]["row"] != row or request.resume != (row["initial"] is not None)
            or row["role"] not in {"candidate-seat", "owner-synthesis"}):
        raise ProviderRequestError("quality delivered container row differs from the admitted request")
    return value, command, terminal


def quality_row_identity(row):
    return hashlib.sha256(canonical_json(row)).hexdigest()


def _quality_row_permit(row, session=None):
    from .broker import BrokerPermit
    pending = row["initial"] is not None and row["permit"]["session_id"] is None
    value = {**row["permit"]}
    if pending:
        if session is None:
            value["route"] = "initial"
        else:
            value["session_id"] = session
    return BrokerPermit(**value)


def _quality_empty_tools(body):
    from .artifacts import _unique_json_object
    from .broker import BrokerRejected
    try:
        value = json.loads(body, object_pairs_hook=_unique_json_object)
        if not isinstance(value, dict):
            raise ValueError("request object")
        pending = [value]
        while pending:
            item = pending.pop()
            if isinstance(item, dict):
                for key, child in item.items():
                    if key in {"tools", "functions", "functionDeclarations", "tool_calls",
                               "functionCall", "tool_use", "tool_result"} and child:
                        raise ValueError("nonempty tool roster or call")
                    if key == "type" and child in {"tool_use", "tool_result", "function_call",
                                                   "function_call_output"}:
                        raise ValueError("tool content")
                    pending.append(child)
            elif isinstance(item, list):
                pending.extend(item)
    except (ValueError, TypeError, RecursionError) as error:
        raise BrokerRejected("quality request tools must remain empty") from error


def _quality_preview(upstream, store, prefix, captures, method, path, headers, body):
    from .broker import BrokerRejected
    _quality_empty_tools(body)
    prepared = upstream.preview(method, path, headers, body)
    _quality_empty_tools(prepared.body)
    digest = hashlib.sha256(body).hexdigest()
    if digest in captures:
        retained = store.read_json(captures[digest])
        if (retained["method"] != method or retained["provider_path"] != path
                or retained["route"] != prepared.route_digest
                or retained["origin"] != prepared.origin or retained["path"] != prepared.path
                or retained["model"] != prepared.effective_model
                or store.read_bytes(ArtifactRef(**retained["normalized_body"])) != body
                or store.read_bytes(ArtifactRef(**retained["effective_body"])) != prepared.body):
            raise BrokerRejected("quality retained prepared request differs")
    else:
        base = f"quality/{prefix}/prepared/{digest}"
        normalized = store.write_bytes(base + "/normalized.json", body)
        effective = store.write_bytes(base + "/effective.json", prepared.body)
        captures[digest] = store.write_json(base + "/identity.json", {
            "method": method, "provider_path": path, "route": prepared.route_digest,
            "origin": prepared.origin, "path": prepared.path, "model": prepared.effective_model,
            "normalized_body": asdict(normalized), "effective_body": asdict(effective)})
    return prepared


def _quality_send(upstream, store, prefix, captures, method, path, headers, body):
    _quality_preview(upstream, store, prefix, captures, method, path, headers, body)
    return upstream(method, path, headers, body)


def quality_upstream_sender(upstream, store, prefix):
    from .container_upstream import HostUpstream
    if (type(upstream) is not HostUpstream or type(store) is not ArtifactStore
            or not isinstance(prefix, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", prefix)):
        raise ProviderRequestError("quality sender requires the genuine sealed host upstream")
    captures = {}
    sender = partial(_quality_send, upstream, store, prefix, captures)
    sender.preview = partial(_quality_preview, upstream, store, prefix, captures)
    sender.captures = captures
    return sender


def _quality_allocation(controller, store, ref, bindings):
    """Reopen the complete allocation, not an unauthenticated digest flag."""
    try:
        value = store.read_json(ref)
        if (set(value) != {"schema", "case_id", "controller_id", "bindings",
                           "maka_output_tokens", "rows"}
                or value["schema"] != "fanout-quality-case-allocation-v1"
                or not re.fullmatch(r"(?:normal|deep|forge)-[0-9]{2}", value["case_id"])
                or value["controller_id"] != controller.controller_id
                or set(bindings) != {"campaign", "casebook", "source", "projection",
                                     "profile", "inputs"}
                or value["bindings"] != bindings
                or any(not re.fullmatch(r"[0-9a-f]{64}", digest)
                       for digest in bindings.values())
                or type(value["maka_output_tokens"]) is not int
                or value["maka_output_tokens"] != 8192
                or type(value["rows"]) is not list
                or not 1 <= len(value["rows"]) <= (20 if value["case_id"].startswith("forge-") else 35)):
            raise ValueError("identity")
        keys = set()
        for row in value["rows"]:
            if (set(row) != {"role", "phase", "condition", "epoch", "permit",
                             "blocker", "initial", "profile", "catalog", "process_seconds", "outer_seconds"}
                    or any(not isinstance(row[key], str)
                           or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", row[key])
                           for key in ("role", "phase", "condition"))
                    or row["condition"] not in {"baseline", "candidate", "disabled", "transfer"}
                    or type(row["epoch"]) is not int or not 1 <= row["epoch"] <= 35
                    or any(type(row[key]) not in (int, float)
                           or not math.isfinite(row[key]) or row[key] <= 0
                           for key in ("process_seconds", "outer_seconds"))
                    or row["process_seconds"] > row["outer_seconds"]):
                raise ValueError("row")
            if (not isinstance(row["profile"], str) or not re.fullmatch(r"[0-9a-f]{64}", row["profile"])
                    or row["catalog"] is not None and (not isinstance(row["catalog"], str)
                        or not re.fullmatch(r"[0-9a-f]{64}", row["catalog"]))):
                raise ValueError("profile/catalog")
            permit = _quality_row_permit(row)
            if (permit.inputs_digest != bindings["inputs"]
                    or (row["permit"]["route"] == "resume") != (row["initial"] is not None)
                    or any(row[key] is not None and
                           (not isinstance(row[key], str)
                            or not re.fullmatch(r"[0-9a-f]{64}", row[key]))
                           for key in ("initial", "blocker"))):
                raise ValueError("row binding")
            key = (row["role"], row["phase"], row["condition"], row["epoch"])
            if key in keys:
                raise ValueError("duplicate row")
            keys.add(key)
        for row in value["rows"]:
            if row["initial"] is None:
                continue
            originals = [prior for prior in value["rows"]
                         if quality_row_identity(prior) == row["initial"]]
            if (len(originals) != 1 or originals[0]["initial"] is not None
                    or originals[0]["epoch"] >= row["epoch"]
                    or originals[0]["condition"] != row["condition"]
                    or originals[0]["profile"] != row["profile"]
                    or originals[0]["catalog"] != row["catalog"]
                    or any(originals[0]["permit"][key] != row["permit"][key] for key in
                        ("provider", "run_id", "task_id", "target_id", "seat_id", "model", "inputs_digest",
                         "upstream_route_digest", "upstream_origin", "upstream_model", "upstream_path"))):
                raise ValueError("prospective original row")
        return value
    except Exception as error:
        raise ProviderRequestError("quality allocation identity or rows differ") from error


def seal_quality_allocation(controller: LifecycleController, store: ArtifactStore,
                            ref: ArtifactRef, *, bindings: dict) -> tuple[str, str]:
    _quality_allocation(controller, store, ref, bindings)
    from .controller import controller_directory
    authority = write_evidence(controller, _QUALITY_ALLOCATION, {"allocation": asdict(ref)})
    path = controller_directory(controller, "locks") / "quality-allocation.json"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError as error:
        raise ProviderRequestError("quality allocation already frozen") from error
    with os.fdopen(fd, "wb") as handle:
        handle.write(canonical_json({"authority": authority}))
        handle.flush()
        os.fsync(handle.fileno())
    return authority


def reopen_quality_allocation(controller: LifecycleController, store: ArtifactStore,
                              authority: tuple[str, str], *, bindings: dict) -> dict:
    try:
        from .controller import controller_directory
        from .container_certification import _read_owned_json
        frozen, _ = _read_owned_json(controller_directory(controller, "locks") / "quality-allocation.json")
        if frozen != {"authority": list(authority)}:
            raise ValueError("frozen allocation")
        evidence = read_evidence(controller, _QUALITY_ALLOCATION, *authority)
        if set(evidence) != {"allocation"}:
            raise ValueError("allocation evidence")
        ref = ArtifactRef(**evidence["allocation"])
    except Exception as error:
        raise ProviderRequestError("quality allocation authority differs") from error
    return _quality_allocation(controller, store, ref, bindings)


def register_quality_permit(controller, store, authority, ledger, *, role, phase,
                            condition, permit, epoch, bindings, blocker=None,
                            initial=None, backend_attachment=None) -> str:
    """Consume a frozen row before registering, including known-unstarted failures."""
    value = reopen_quality_allocation(controller, store, authority, bindings=bindings)
    rows = [row for row in value["rows"] if (row["role"], row["phase"],
            row["condition"], row["epoch"]) == (role, phase, condition, epoch)]
    if (type(ledger) is not PermitLedger or ledger.controller is not controller
            or ledger.root != controller.root / "locks" / "spend"
            or len(rows) != 1 or rows[0]["blocker"] != blocker):
        raise ProviderRequestError("quality allocation dispatch differs")
    row = rows[0]
    resolved = _quality_resolved_permit(controller, store, authority, value, row, initial,
                                      backend_attachment=backend_attachment)
    if asdict(resolved) != asdict(permit):
        raise ProviderRequestError("quality allocation resolved permit differs")
    if backend_attachment is not None:
        from .quality_case import _read_quality_backend_attachment
        from .container_boundary import (quality_docker_runner, _quality_docker_lock,
            _quality_docker_consumed, _quality_docker_remaining)
        attachment = _read_quality_backend_attachment(controller, store, backend_attachment,
            authority=authority, bindings=bindings, row=row)
        controls = tuple(attachment["container"]["docker_controls"])
        original = read_evidence(controller, "quality-docker-controls", *controls)
        limits = original["limits"]
        quality_docker_runner(controller, allocation_digest=authority[1],
            max_commands=limits["max_commands"], command_seconds=limits["command_seconds"],
            outer_seconds=limits["outer_seconds"], original_controls=controls)
        with _quality_docker_lock(controller, authority[1], identity=original["marker_identity"], proof=original) as (_, descriptor):
            if _quality_docker_consumed(controller, descriptor, original, controls) >= limits["max_commands"]:
                raise ProviderRequestError("quality Docker original command allocation is exhausted")
            _quality_docker_remaining(original)
    # Only an original controller-authenticated blocker can unlock a reserved fix.
    if blocker is not None:
        proof = read_evidence(controller, "quality-original-blocker", f"{blocker}.json", blocker)
        if proof != {"case_id": value["case_id"], "role": role, "required": True}:
            raise ProviderRequestError("quality allocation original blocker differs")
    claim = {"allocation": authority[1], "row": row}
    digest = hashlib.sha256(canonical_json(claim)).hexdigest()
    from .controller import controller_directory
    path = controller_directory(controller, "locks") / f"quality-row-{digest}"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError as error:
        raise ProviderRequestError("quality allocation row already consumed; no replenishment") from error
    with os.fdopen(fd, "wb") as handle:
        handle.write(canonical_json(claim))
        handle.flush()
        os.fsync(handle.fileno())
    parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    permit_id = ledger.register(permit, epoch)
    try:
        registration = {
            **claim, "permit_id": permit_id, "epoch": epoch, "permit": asdict(permit),
            "initial_receipt": None if initial is None else list(initial)}
        if backend_attachment is not None:
            registration["backend_attachment"] = list(backend_attachment)
        registered = write_evidence(controller, "quality-permit-registration", registration)
        fd = os.open(path.with_name(f"quality-registration-{digest}"),
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(canonical_json({"authority": list(registered)}))
            handle.flush(); os.fsync(handle.fileno())
    except BaseException as error:
        if backend_attachment is not None:
            try:
                ledger.fence_epoch(permit_id, epoch)
            except BaseException:
                error.add_note("quality original permit fencing also failed")
        raise
    return permit_id


def _quality_resolved_permit(controller, store, authority, allocation, row, initial,
                             *, backend_attachment=None):
    if backend_attachment is not None:
        if (row["condition"] != "candidate" or row["blocker"] is not None
                or row["role"] not in {"candidate-seat", "owner-synthesis"}
                or row["phase"] not in ({"initial", "resume"} if row["role"] == "candidate-seat" else {"initial"})):
            raise ProviderRequestError("quality container backend requires an original admitted work row")
        from .quality_case import _read_quality_backend_attachment
        _read_quality_backend_attachment(controller, store, backend_attachment,
            authority=authority, bindings=allocation["bindings"], row=row)
    if row["initial"] is None:
        if initial is not None:
            raise ProviderRequestError("quality initial row cannot consume a predecessor")
        return _quality_row_permit(row)
    try:
        if not isinstance(initial, (tuple, list)) or len(initial) != 2:
            raise ValueError("completed predecessor")
        if backend_attachment is not None:
            request, _, command, terminal = _read_quality_container_turn(controller, store, initial,
                authority=authority, bindings=allocation["bindings"], backend_attachment=backend_attachment)
            registration = command["registration"]
            original = registration["row"]
            session = terminal["session_id"]
            if (request.resume or original["initial"] is not None
                    or original["phase"] != "initial" or original["role"] != row["role"]
                    or registration["initial_receipt"] is not None
                    or quality_row_identity(original) != row["initial"]
                    or row["permit"]["session_id"] is not None and row["permit"]["session_id"] != session):
                raise ValueError("completed original container row")
            return _quality_row_permit(row, session)
        prior = read_evidence(controller, "quality-readonly-turn", *initial)
        command = read_evidence(controller, "quality-native-command", *prior["command"])
        registration = command["registration"]
        original = registration["row"]
        if (command["allocation"] != list(authority) or command["store"] != str(store.root)
                or command["bindings"] != allocation["bindings"]
                or original not in allocation["rows"]
                or quality_row_identity(original) != row["initial"]
                or original["initial"] is not None):
            raise ValueError("original row association")
        terminal = read_evidence(controller, _CATEGORY, *prior["terminal"])
        assigned_session = original["permit"]["session_id"]
        initial_route = ("initial:unassigned" if assigned_session is None
                         else f"initial:assigned:{assigned_session}")
        if (terminal["valid"] is not True or terminal["route"] != initial_route
                or terminal["process_status"] != "exit" or terminal["exit_code"] != 0
                or not terminal["session_id"] or not prior["records"]
                or any(record["state"] != "settled" for record in prior["records"])):
            raise ValueError("unsettled predecessor")
        ledger = PermitLedger(controller.root / "locks" / "spend", controller)
        with ledger._locked() as state:
            actual = state["permits"].get(registration["permit_id"])
            if (actual is None or actual["epoch"] != original["epoch"] or not actual["fenced"]
                    or actual["permit_digest"] != hashlib.sha256(canonical_json(registration["permit"])).hexdigest()):
                raise ValueError("original concrete permit")
        if (list(ledger.request_records(registration["permit_id"], original["epoch"])) != prior["records"]
                or hashlib.sha256(store.read_bytes(ArtifactRef(**prior["stdout"]))).hexdigest() != terminal["stdout_sha256"]
                or hashlib.sha256(store.read_bytes(ArtifactRef(**prior["stderr"]))).hexdigest() != terminal["stderr_sha256"]
                or not store.read_bytes(ArtifactRef(**prior["final"]))):
            raise ValueError("original raw completion")
        session = terminal["session_id"]
        if row["permit"]["session_id"] is not None and row["permit"]["session_id"] != session:
            raise ValueError("frozen assigned session")
        return _quality_row_permit(row, session)
    except Exception as error:
        raise ProviderRequestError("quality resume requires its exact completed original row") from error


def _quality_loaded_sources():
    root = Path(__file__).resolve().parents[3]
    loaded = {}
    for module in tuple(sys.modules.values()):
        raw = getattr(module, "__file__", None)
        if not raw:
            continue
        path = Path(raw)
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            continue
        if relative.startswith(("shared/lib/fanout/", "scripts/fanout_")) and path.suffix == ".py":
            if path.is_symlink() or path.resolve(strict=True) != path:
                raise ProviderRequestError("quality loaded source origin differs")
            loaded[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    helper_key = "shared/skills/llm-fanout-execute/scripts/execute.py"
    helpers = [root / helper_key]
    if root.name == ".runtime":
        helpers.append(root.parent / "scripts/execute.py")
    for helper in helpers:
        for module in tuple(sys.modules.values()):
            raw = getattr(module, "__file__", None)
            origin = getattr(getattr(module, "__spec__", None), "origin", None)
            if raw != str(helper) and origin != str(helper):
                continue
            if (raw != str(helper) or origin not in (None, str(helper))
                    or helper.is_symlink() or helper.resolve(strict=True) != helper):
                raise ProviderRequestError("quality loaded execute helper origin differs")
            loaded[helper_key] = hashlib.sha256(helper.read_bytes()).hexdigest()
    return loaded


def _require_quality_loaded_sources(expected):
    from .container_source import _source_paths, _read_source
    root = Path(__file__).resolve().parents[3]
    allowed = set(_source_paths(root))
    required = {"shared/lib/fanout/diagnostic.py", "shared/lib/fanout/native_boundary.py",
                "shared/lib/fanout/providers.py"}
    if type(expected) is not dict or not required <= expected.keys() or len(expected) > len(allowed):
        raise ProviderRequestError("quality loaded source evidence fields differ")
    from . import eval_provider
    for module, name in ((sys.modules[__name__], "diagnostic.py"),
            (sys.modules[NativeSeatBoundary.__module__], "native_boundary.py"),
            (sys.modules[ProviderRequest.__module__], "providers.py"), (eval_provider, "eval_provider.py")):
        if Path(module.__file__) != root / "shared/lib/fanout" / name:
            raise ProviderRequestError("quality current protected assembly loaded a foreign origin")
    for name in allowed:
        if name.startswith("shared/lib/fanout/") and name.endswith(".py"):
            identity = __package__ + "." + name.removeprefix("shared/lib/fanout/")[:-3].replace("/", ".")
            identity = identity.removesuffix(".__init__")
        elif name.startswith("scripts/fanout_") and name.endswith(".py"):
            identity = name[:-3].replace("/", ".")
        else:
            continue
        module = sys.modules.get(identity)
        if module is not None and (getattr(module, "__file__", None) != str(root / name)
                or getattr(getattr(module, "__spec__", None), "origin", None) != str(root / name)):
            raise ProviderRequestError("quality current canonical module loaded a foreign source origin")
    for module in tuple(sys.modules.values()):
        origin = getattr(getattr(module, "__spec__", None), "origin", None)
        if type(origin) is not str:
            continue
        try:
            name = Path(origin).relative_to(root).as_posix()
        except ValueError:
            continue
        if name in allowed and getattr(module, "__file__", None) != origin:
            raise ProviderRequestError("quality current loaded module origin differs")
    loaded = _quality_loaded_sources()
    for name, digest in expected.items():
        if (type(name) is not str or name not in allowed or type(digest) is not str
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                or hashlib.sha256(_read_source(root, name, 1024 * 1024)).hexdigest() != digest
                or name in loaded and loaded[name] != digest):
            raise ProviderRequestError("quality loaded source origin or closed source bytes differ")


def project_quality_catalog(store, original_ref, path):
    original = store.read_bytes(original_ref)
    if hashlib.sha256(original).hexdigest() != "fd219bd9f061278275f528939f82f54d2eb97df4b25c23b022adbe48813d920b":
        raise ProviderRequestError("quality catalog authentic source differs")
    value = store.read_json(original_ref)
    models = value.get("models")
    selected = [model for model in models or () if model.get("slug") == "gpt-6.1-sol"]
    if len(models or ()) != 11 or len(selected) != 1 or hashlib.sha256(json_catalog_bytes(selected[0])).hexdigest() != "56373206bcf4c478be9aa324af04e2324219a836005e1044260910307f2cceeb":
        raise ProviderRequestError("quality catalog original descriptor differs")
    selected[0].update(shell_type="disabled", apply_patch_tool_type=None,
        experimental_supported_tools=[], supports_search_tool=False, tool_mode="direct",
        use_responses_lite=False)
    return store.write_json(path, value)


def json_catalog_bytes(value):
    import json
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _reopen_quality_catalog(store, catalog):
    if set(catalog) != {"original", "projected"}:
        raise ProviderRequestError("quality catalog association differs")
    original_ref, projected_ref = (ArtifactRef(**catalog[key]) for key in ("original", "projected"))
    original = _read_quality_request_artifact(store, original_ref, 2 * 1024 * 1024)
    if hashlib.sha256(original).hexdigest() != "fd219bd9f061278275f528939f82f54d2eb97df4b25c23b022adbe48813d920b":
        raise ProviderRequestError("quality catalog authentic source differs")
    expected = json.loads(original)
    selected = [model for model in expected.get("models", ()) if model.get("slug") == "gpt-6.1-sol"]
    if len(selected) != 1 or hashlib.sha256(json_catalog_bytes(selected[0])).hexdigest() != "56373206bcf4c478be9aa324af04e2324219a836005e1044260910307f2cceeb":
        raise ProviderRequestError("quality catalog descriptor differs")
    selected[0].update(shell_type="disabled", apply_patch_tool_type=None,
        experimental_supported_tools=[], supports_search_tool=False, tool_mode="direct",
        use_responses_lite=False)
    if _read_quality_request_artifact(store, projected_ref, 2 * 1024 * 1024) != canonical_json(expected):
        raise ProviderRequestError("quality catalog tool projection differs")
    return canonical_json(expected)


def issue_quality_native_command(controller, store, authority, *, request, ledger,
                                 permit, epoch, bindings, role, phase, condition,
                                 catalog=None):
    allocation = reopen_quality_allocation(controller, store, authority, bindings=bindings)
    rows = [row for row in allocation["rows"] if (row["role"], row["phase"],
        row["condition"], row["epoch"]) == (role, phase, condition, epoch)]
    if (len(rows) != 1 or rows[0]["profile"] != request.profile.digest
            or rows[0]["catalog"] != (None if catalog is None else catalog["projected"]["digest"])):
        raise ProviderRequestError("quality native allocated row differs")
    claim = {"allocation": authority[1], "row": rows[0]}
    from .container_certification import _read_owned_json
    digest = hashlib.sha256(canonical_json(claim)).hexdigest()
    try:
        actual, _ = _read_owned_json(controller.root / "locks" / f"quality-row-{digest}")
        pointer, _ = _read_owned_json(controller.root / "locks" / f"quality-registration-{digest}")
        if set(pointer) != {"authority"}:
            raise ValueError("registration authority")
        registration = read_evidence(controller, "quality-permit-registration", *pointer["authority"])
        permit_id = registration["permit_id"]
        ledger.require_permit(permit_id, epoch, permit)
        if (actual != claim or registration != {**claim, "permit_id": permit_id, "epoch": epoch,
                "permit": asdict(permit), "initial_receipt": registration.get("initial_receipt")}
                or asdict(_quality_resolved_permit(controller, store, authority, allocation,
                    rows[0], registration["initial_receipt"])) != asdict(permit)):
            raise ValueError("claim")
    except Exception as error:
        raise ProviderRequestError("quality native row must already be consumed and registered") from error
    if request.executor_id == "codex":
        from .native_boundary import _private_state, _write_immutable
        state = _private_state(controller.root, request.run_id, request.task_id,
                               request.target_id, request.seat_id)
        home = state / "home" / ".codex"
        home.mkdir(mode=0o700, exist_ok=True)
        if home.is_symlink() or home.stat().st_mode & 0o077:
            raise ProviderRequestError("quality Codex private catalog directory differs")
        _write_immutable(home / "quality-catalog.json", _reopen_quality_catalog(store, catalog))
    elif request.executor_id == "agy":
        from .native_boundary import _private_state, _agy_private_settings, _quality_agy_agent
        state = _private_state(controller.root, request.run_id, request.task_id,
                               request.target_id, request.seat_id)
        _agy_private_settings(state / "home")
        _quality_agy_agent(state / "home")
    proof = write_evidence(controller, "quality-native-command", {
        "allocation": list(authority), "store": str(store.root), "bindings": bindings,
        "registration": registration,
        "catalog": catalog, "loaded": _quality_loaded_sources(),
        "request": _quality_request_identity(request)})
    reopen_quality_native_command(controller, request, proof)
    return proof


def _quality_request_identity(request):
    from .container_transfer_transport import admitted_request_digest
    return admitted_request_digest(request)


def reopen_quality_native_command(controller, request, proof):
    """Reconstruct the same bounded command at issuance and immediately before spawn."""
    from .container_source import runtime_source_digest
    from .container_certification import _read_owned_json
    from .eval_provider import quality_session_command
    try:
        value = read_evidence(controller, "quality-native-command", *proof)
        if (set(value) != {"allocation", "store", "bindings", "registration", "catalog", "loaded", "request"}
                or value["request"] != _quality_request_identity(request)
                or runtime_source_digest() != value["bindings"]["source"]):
            raise ValueError("request/source")
        _require_quality_loaded_sources(value["loaded"])
        with ArtifactStore.open_existing(value["store"]) as store:
            allocation = reopen_quality_allocation(controller, store, tuple(value["allocation"]),
                bindings=value["bindings"])
            registration = value["registration"]
            row = registration["row"]
            if (set(registration) != {"allocation", "row", "permit_id", "epoch", "permit", "initial_receipt"}
                    or row not in allocation["rows"] or registration["allocation"] != value["allocation"][1]
                    or registration["epoch"] != row["epoch"] or request.timeout != row["process_seconds"]
                    or request.retries != 0 or request.execution_class != "read-only"
                    or row["profile"] != request.profile.digest
                    or row["catalog"] != (None if value["catalog"] is None else value["catalog"]["projected"]["digest"])):
                raise ValueError("row")
            claim = {"allocation": registration["allocation"], "row": row}
            digest = hashlib.sha256(canonical_json(claim)).hexdigest()
            actual, _ = _read_owned_json(controller.root / "locks" / f"quality-row-{digest}")
            pointer, _ = _read_owned_json(controller.root / "locks" / f"quality-registration-{digest}")
            if (actual != claim or read_evidence(controller, "quality-permit-registration",
                    *pointer["authority"]) != registration):
                raise ValueError("consumed row")
            from .broker import BrokerPermit
            permit = BrokerPermit(**registration["permit"])
            if asdict(_quality_resolved_permit(controller, store, tuple(value["allocation"]),
                    allocation, row, registration["initial_receipt"])) != asdict(permit):
                raise ValueError("resolved original session")
            ledger = PermitLedger(controller.root / "locks" / "spend", controller)
            ledger.require_permit(registration["permit_id"], row["epoch"], permit)
            if (permit.provider, permit.run_id, permit.task_id, permit.target_id, permit.seat_id,
                    permit.inputs_digest, permit.session_id, permit.route) != (
                    request.executor_id, request.run_id, request.task_id, request.target_id, request.seat_id,
                    request.inputs_digest, request.session_id, "resume" if request.resume else "initial"):
                raise ValueError("permit")
            catalog_path = None
            if request.executor_id == "codex":
                from .native_boundary import _hash_file
                identity = hashlib.sha256(canonical_json((request.run_id, request.task_id,
                    request.target_id, request.seat_id))).hexdigest()
                path = controller.root / "native-seat-state" / identity / "home" / ".codex" / "quality-catalog.json"
                if _hash_file(path, mode=0o400) != hashlib.sha256(_reopen_quality_catalog(store, value["catalog"])).hexdigest():
                    raise ValueError("private catalog")
                catalog_path = str(path)
            elif value["catalog"] is not None:
                raise ValueError("unexpected catalog")
            if registration["initial_receipt"] is not None:
                predecessor = read_evidence(controller, "quality-readonly-turn", *registration["initial_receipt"])
                previous = read_evidence(controller, "quality-native-command", *predecessor["command"])
                if previous["catalog"] != value["catalog"]:
                    raise ValueError("exact initial/resume catalog")
            command = quality_session_command(request, catalog_path=catalog_path)
            if request.executor_id == "agy":
                from .native_boundary import _AGY_DIRECT_BINARY, _hash_file
                from .eval_provider import AGY_AGENT_TEXT
                identity = hashlib.sha256(canonical_json((request.run_id, request.task_id,
                    request.target_id, request.seat_id))).hexdigest()
                path = controller.root / "native-seat-state" / identity / "home" / ".gemini/config/agents/eval-no-tools.md"
                if _hash_file(path, mode=0o600) != hashlib.sha256(AGY_AGENT_TEXT.encode()).hexdigest():
                    raise ValueError("private empty-tools agent")
                command = replace(command, argv=(str(_AGY_DIRECT_BINARY), *command.argv[1:]))
            return replace(command, max_output_bytes=8 * 1024 * 1024), registration
    except Exception as error:
        raise ProviderRequestError("quality native command association differs") from error


def validate_quality_prior_command(controller, request, proof):
    from .container_source import runtime_source_digest
    value = read_evidence(controller, "quality-native-command", *proof)
    with ArtifactStore.open_existing(value["store"]) as store:
        allocation = reopen_quality_allocation(controller, store, tuple(value["allocation"]),
            bindings=value["bindings"])
        registration = value["registration"]
        row = registration["row"]
        permit = registration["permit"]
        _require_quality_loaded_sources(value["loaded"])
        if (set(registration) != {"allocation", "row", "permit_id", "epoch", "permit", "initial_receipt"}
                or row not in allocation["rows"] or permit["route"] != "initial"
                or runtime_source_digest() != value["bindings"]["source"]
                or any(permit[name] != getattr(request, name) for name in
                       ("run_id", "task_id", "target_id", "seat_id", "inputs_digest"))
                or permit["provider"] != request.executor_id
                or (request.executor_id == "claude" and permit["session_id"] != request.session_id)):
            raise ProviderRequestError("quality initial source or own-session association differs")
        ledger = PermitLedger(controller.root / "locks" / "spend", controller)
        with ledger._locked() as state:
            prior = state["permits"].get(registration["permit_id"])
            if (prior is None or prior["epoch"] != row["epoch"] or not prior["fenced"]
                    or prior["permit_digest"] != hashlib.sha256(canonical_json(permit)).hexdigest()):
                raise ProviderRequestError("quality initial permit is not exactly settled/fenced")
        if request.executor_id == "codex":
            _reopen_quality_catalog(store, value["catalog"])


def run_quality_readonly_turn(request, *, controller, store, authority, ledger,
                              permit, epoch, bindings, role, phase, condition,
                              route, credential, verification, other_target_roots,
                              denied_owner_roots, catalog=None, initial=None,
                              blocker=None, initial_digest=None, initial_proof=None):
    """Consume one allocated row and reuse the protected brokered process assembly."""
    from .broker import Broker
    from .container_upstream import HostUpstream
    from scripts.fanout_container_certify import _capture_private_wire
    if (type(request) is not ProviderRequest or request.execution_class != "read-only"
            or request.retries != 0 or request.native_boundary is not None
            or initial is not None and not isinstance(initial, (BrokeredDiagnosticResult, QualityPredecessor))):
        raise ProviderRequestError("quality readonly turn requires the exact zero-retry request")
    allocation = reopen_quality_allocation(controller, store, authority, bindings=bindings)
    row = next((row for row in allocation["rows"] if (row["role"], row["phase"],
        row["condition"], row["epoch"]) == (role, phase, condition, epoch)), None)
    if (row is None or request.timeout != row["process_seconds"]
            or row["initial"] != initial_digest
            or (initial is None) != (initial_digest is None)
            or (initial is None) != (initial_proof is None)):
        raise ProviderRequestError("quality readonly phase or original session differs")
    if initial_proof is not None:
        previous = read_evidence(controller, "quality-readonly-turn", *initial_proof)
        if previous["terminal"] != [initial.receipt_name, initial.receipt_digest]:
            raise ProviderRequestError("quality readonly original terminal differs")
        if type(initial) is QualityPredecessor and initial != reopen_quality_predecessor(
                controller, store, request, initial_proof, verification=verification,
                authority=authority, bindings=bindings, initial_digest=initial_digest):
            raise ProviderRequestError("quality readonly predecessor pointer differs")
    prefix = f"quality/{role}/{phase}/{epoch}"
    _quality_request_material(request, prefix)
    started = time.monotonic()
    permit_id = register_quality_permit(controller, store, authority, ledger,
        role=role, phase=phase, condition=condition, permit=permit, epoch=epoch,
        bindings=bindings, blocker=blocker, initial=initial_proof)
    wires = {}
    terminal = None
    def success(payload):
        captured = _capture_private_wire(ledger, permit_id, epoch, store.root,
                                        f"quality-{role}-{phase}-{epoch}", payload)
        wires[captured["request_id"]] = captured
    try:
        sender = quality_upstream_sender(HostUpstream(permit, route,
            max_response_bytes=permit.max_response_bytes, deadline_seconds=permit.response_deadline,
            success_sink=success), store, f"{role}-{phase}-{epoch}")
        command_proof = issue_quality_native_command(controller, store, authority,
            request=request, ledger=ledger, permit=permit, epoch=epoch, bindings=bindings,
            role=role, phase=phase, condition=condition, catalog=catalog)
        request_ref = _retain_quality_request(store, request, prefix)
        with BrokerListener(Broker(permit, credential=credential, send=sender),
                            ledger_binding=(ledger, permit_id, epoch)) as listener:
            terminal = run_brokered_provider_diagnostic(request, controller=controller,
                verification=verification, broker_listener=listener,
                other_target_roots=other_target_roots, denied_owner_roots=denied_owner_roots,
                initial=initial, quality_admission=command_proof)
    finally:
        ledger.fence_epoch(permit_id, epoch)
    elapsed = time.monotonic() - started
    process = terminal.process
    stdout = store.write_bytes(prefix + "/stdout.bin", process.stdout)
    stderr = store.write_bytes(prefix + "/stderr.bin", process.stderr)
    final = store.write_bytes(prefix + "/final.txt", terminal.result.answer.encode("utf-8"))
    records = ledger.request_records(permit_id, epoch)
    main, auxiliary = ledger.attempt_counts(permit_id, epoch)
    proof = write_evidence(controller, "quality-readonly-turn", {
        "schema": "fanout-quality-readonly-turn-v1", "command": list(command_proof),
        "terminal": [terminal.receipt_name, terminal.receipt_digest],
        "request": asdict(request_ref),
        "stdout": asdict(stdout), "stderr": asdict(stderr), "final": asdict(final),
        "records": list(records), "prepared": [asdict(ref) for ref in sender.captures.values()],
        "wires": wires, "route": asdict(route), "main_attempts": main, "auxiliary_attempts": auxiliary,
        "elapsed": elapsed, "outer_seconds": row["outer_seconds"]})
    validate_quality_readonly_turn(controller, store, request, proof)
    return terminal, proof


def validate_quality_readonly_turn(controller, store, request, proof):
    from .broker import BrokerPermit
    from .container_broker import attempt_limit
    value = read_evidence(controller, "quality-readonly-turn", *proof)
    expected_fields = {"schema", "command", "terminal", "stdout", "stderr", "final",
        "records", "prepared", "wires", "route", "main_attempts", "auxiliary_attempts", "elapsed", "outer_seconds"}
    if set(value) not in (expected_fields, expected_fields | {"request"}) or value["schema"] != "fanout-quality-readonly-turn-v1":
        raise ProviderRequestError("quality readonly receipt differs")
    command = read_evidence(controller, "quality-native-command", *value["command"])
    if command["request"] != _quality_request_identity(request) or command["store"] != str(store.root):
        raise ProviderRequestError("quality readonly request or store differs")
    allocation = reopen_quality_allocation(controller, store, tuple(command["allocation"]),
        bindings=command["bindings"])
    registration = command["registration"]
    row = registration["row"]
    permit = BrokerPermit(**registration["permit"])
    ledger = PermitLedger(controller.root / "locks" / "spend", controller)
    records = ledger.request_records(registration["permit_id"], row["epoch"])
    attempts = ledger.attempt_counts(registration["permit_id"], row["epoch"])
    with ledger._locked() as state:
        frozen = state["permits"].get(registration["permit_id"])
        if frozen is None or not frozen["fenced"]:
            raise ProviderRequestError("quality readonly permit is not fenced")
    terminal = read_evidence(controller, _CATEGORY, *value["terminal"])
    if (row not in allocation["rows"] or value["records"] != list(records)
            or attempts != (value["main_attempts"], value["auxiliary_attempts"])
            or attempts[0] > attempt_limit(permit) or len(records) > permit.max_requests
            or terminal["valid"] is not True or terminal["process_status"] != "exit"
            or terminal["exit_code"] != 0 or value["outer_seconds"] != row["outer_seconds"]
            or type(value["elapsed"]) not in (int, float) or not math.isfinite(value["elapsed"])
            or not 0 < value["elapsed"] <= row["outer_seconds"]
            or not records or {record["request_id"] for record in records} != set(value["wires"])
            or any(record["state"] != "settled" for record in records)):
        raise ProviderRequestError("quality readonly delivery or finite accounting differs")
    for name in ("stdout", "stderr"):
        if hashlib.sha256(store.read_bytes(ArtifactRef(**value[name]))).hexdigest() != terminal[name + "_sha256"]:
            raise ProviderRequestError("quality readonly process bytes differ")
    final = store.read_bytes(ArtifactRef(**value["final"]))
    registry = ProviderRegistry((request.profile,))
    parsed = registry.require(request.executor_id).parse(store.read_bytes(ArtifactRef(**value["stdout"])))
    if not final or final != parsed.answer.encode("utf-8"):
        raise ProviderRequestError("quality readonly final bytes differ")
    for retained in value["prepared"]:
        prepared = store.read_json(ArtifactRef(**retained))
        for key in ("normalized_body", "effective_body"):
            _quality_empty_tools(store.read_bytes(ArtifactRef(**prepared[key])))
    from .container_certification import reopen_quality_wire
    from .container_routes import RouteAdmission, RouteProfile
    profile = {**value["route"]["profile"]}
    profile["execution_classes"] = tuple(profile["execution_classes"])
    route = RouteAdmission(**{**value["route"], "profile": RouteProfile(**profile)})
    for record in records:
        reopen_quality_wire(store, value["wires"][record["request_id"]], permit,
                            record, route)
    return value


@dataclass(frozen=True, slots=True)
class BrokeredDiagnosticResult:
    result: ProviderResult
    boundary: NativeSeatBoundary
    process: ProcessResult
    receipt_name: str
    receipt_digest: str
    effective_output_tokens: tuple[int, ...]
    broker_model: str
    refused_agy_auxiliary_requests: int


def run_brokered_provider_diagnostic(
    request: ProviderRequest, *, controller: LifecycleController,
    verification: SeatWorkspaceVerification, broker_listener: BrokerListener,
    other_target_roots: Sequence[Path], denied_owner_roots: Sequence[Path],
    initial: BrokeredDiagnosticResult | QualityPredecessor | None = None,
    quality_admission: tuple[str, str] | None = None,
) -> BrokeredDiagnosticResult:
    """Run one fake or approved probe turn; never authorize ordinary dispatch."""
    if (not isinstance(request, ProviderRequest) or request.profile is None
            or request.native_boundary is not None or request.retries != 0
            or any(getattr(request, name) is None for name in (
                "run_id", "task_id", "target_id", "inputs_digest", "seat_id"))):
        raise ProviderRequestError("brokered diagnostic requires one exact controller seat turn")
    if request.execution_class != "read-only":
        raise ProviderRequestError("brokered diagnostic supports read-only diagnostics only")
    initial_receipt = None
    quality_command = None
    if request.resume and type(initial) is QualityPredecessor:
        if quality_admission is None:
            raise ProviderRequestError("cold quality predecessor requires its allocated quality admission")
        quality_command = reopen_quality_native_command(controller, request, quality_admission)
        value = read_evidence(controller, "quality-native-command", *quality_admission)
        registration = value["registration"]
        with ArtifactStore.open_existing(value["store"]) as store:
            reopened = reopen_quality_predecessor(controller, store, request, initial.quality_proof,
                verification=verification, authority=tuple(value["allocation"]),
                bindings=value["bindings"], initial_digest=registration["row"]["initial"])
        if initial != reopened or registration["initial_receipt"] != list(initial.quality_proof):
            raise ProviderRequestError("cold quality predecessor differs from its allocated resume")
        initial_receipt = (initial.receipt_name, initial.receipt_digest)
    elif request.resume:
        _authenticate_resume(controller, request, initial)
        initial_receipt = (initial.receipt_name, initial.receipt_digest)
    elif initial is not None:
        raise ProviderRequestError("initial diagnostic cannot consume a prior turn")
    selected_registry = (ProviderRegistry((request.profile,)) if request.resume
                         else ProviderRegistry.for_installed((request.executor_id,)))
    profile = selected_registry.admit(
        request.executor_id, request.execution_class, request.profile.quality_tier,
    )
    if profile.digest != request.profile.digest:
        raise ProviderRequestError("diagnostic profile differs from the installed pin")
    selected_registry.assert_installed_version(profile)
    adapter = selected_registry.require(request.executor_id)
    if quality_admission is not None:
        command, registration = (quality_command if quality_command is not None else
                                 reopen_quality_native_command(controller, request, quality_admission))
        claim = {"allocation": registration["allocation"], "row": registration["row"]}
        digest = hashlib.sha256(canonical_json(claim)).hexdigest()
        try:
            descriptor = os.open(controller.root / "locks" / f"quality-cli-{digest}",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError as error:
            raise ProviderRequestError("quality CLI row already attempted; no replay") from error
        with os.fdopen(descriptor, "wb") as output:
            output.write(canonical_json({"command": list(quality_admission), "request": _quality_request_identity(request)}))
            output.flush(); os.fsync(output.fileno())
        if request.executor_id == "codex":
            command = _codex_broker_command(request, command, broker_listener)
    elif type(adapter) is ClaudeAdapter:
        command = adapter.build_command(request, brokered_diagnostic=True)
    elif type(adapter) is CodexAdapter:
        command = _codex_broker_command(
            request, adapter.build_command(request), broker_listener,
        )
    elif type(adapter) is AgyAdapter and request.execution_class == "read-only":
        command = adapter.build_command(request, brokered_diagnostic=True)
    else:
        raise ProviderRequestError("provider lacks a brokered diagnostic command")
    if command.environment:
        raise ProviderRequestError("brokered diagnostic command carries environment overrides")
    admission = issue_broker_admission(
        controller, verification, request, command, broker_listener,
        initial_receipt=initial_receipt,
        quality_admission=quality_admission,
    )
    boundary = issue_native_boundary(
        controller, verification, run_id=request.run_id, task_id=request.task_id,
        target_id=request.target_id, inputs_digest=request.inputs_digest,
        request=request, command=command, other_target_roots=other_target_roots,
        denied_owner_roots=denied_owner_roots, broker_listener=broker_listener,
        broker_admission=admission,
    )
    started = time.monotonic()
    process = run_command(wrap_native_command(
        command, boundary, request=request, controller=controller,
        verification=verification,
    ))
    parsed = None
    if process.status is ProcessStatus.EXIT and process.stdout:
        try:
            parsed = adapter.parse(process.stdout)
        except ProviderProtocolError:
            pass
    result = _make_result(request, adapter, process, parsed, 1,
                          time.monotonic() - started)
    effective_output_tokens = broker_listener.effective_output_tokens
    broker_model = broker_listener.active_binding()[3].model
    refused_agy_auxiliary_requests = broker_listener.refused_agy_auxiliary_requests
    receipt_name, receipt_digest = write_evidence(controller, _CATEGORY, {
        "schema_version": "fanout-brokered-diagnostic-v1",
        "run_id": request.run_id, "task_id": request.task_id,
        "target_id": request.target_id, "seat_id": request.seat_id,
        "inputs_digest": request.inputs_digest, "profile_digest": profile.digest,
        "boundary_receipt_digest": boundary.receipt_digest,
        "route": boundary.route, "session_id": result.session_id,
        "valid": result.valid, "reason": result.reason,
        "process_status": process.status.value, "exit_code": process.returncode,
        "stdout_sha256": hashlib.sha256(process.stdout).hexdigest(),
        "stderr_sha256": hashlib.sha256(process.stderr).hexdigest(),
        "observed_model": result.observed_model,
        "broker_model": broker_model,
        "effective_output_tokens": list(effective_output_tokens),
        "refused_agy_auxiliary_requests": refused_agy_auxiliary_requests,
    })
    return BrokeredDiagnosticResult(result, boundary, process,
                                    receipt_name, receipt_digest,
                                    effective_output_tokens, broker_model,
                                    refused_agy_auxiliary_requests)


def _codex_broker_command(request: ProviderRequest, command: ProcessCommand,
                          listener: BrokerListener) -> ProcessCommand:
    if request.executor_id != "codex":
        raise ProviderRequestError("Codex broker command has a different provider")
    try:
        port, _, _, placeholder = _broker_binding(listener, request)
    except ValueError as error:
        raise ProviderRequestError("Codex broker binding is unavailable") from error
    return codex_broker_command(request, command, port=port, placeholder=placeholder)


def codex_broker_command(request: ProviderRequest, command: ProcessCommand, *,
                        port: int, placeholder: str) -> ProcessCommand:
    """Apply only the stable tokenless Responses overrides to a canonical command."""
    import re
    if (request.executor_id != "codex" or type(port) is not int or not 1 <= port <= 65535
            or not isinstance(placeholder, str)
            or re.fullmatch(r"[A-Za-z0-9._-]{16,128}", placeholder) is None):
        raise ProviderRequestError("Codex broker binding is unavailable")
    prefix = 3 if request.resume else 2
    if tuple(command.argv[:prefix]) != (("codex", "exec", "resume") if request.resume
                                         else ("codex", "exec")):
        raise ProviderRequestError("Codex broker command differs from pinned grammar")
    options = (
        "model_provider=fanout_broker",
        "model_providers.fanout_broker.name=FanoutBroker",
        f'model_providers.fanout_broker.base_url="http://127.0.0.1:{port}/v1"',
        "model_providers.fanout_broker.wire_api=responses",
        "model_providers.fanout_broker.requires_openai_auth=false",
        "model_providers.fanout_broker.request_max_retries=0",
        "model_providers.fanout_broker.stream_max_retries=0",
        f'model_providers.fanout_broker.http_headers={{"X-Fanout-Seat"="{placeholder}"}}',
    )
    argv = (tuple(command.argv[:prefix])
            + tuple(part for option in options for part in ("-c", option))
            + tuple(command.argv[prefix:]))
    return replace(command, argv=argv)


def _authenticate_resume(controller: LifecycleController, request: ProviderRequest,
                         initial: BrokeredDiagnosticResult | None) -> None:
    if not isinstance(initial, BrokeredDiagnosticResult):
        raise ProviderRequestError("exact diagnostic resume needs an authenticated initial turn")
    try:
        receipt = read_evidence(controller, _CATEGORY,
                                initial.receipt_name, initial.receipt_digest)
    except Exception as error:
        raise ProviderRequestError("diagnostic initial turn evidence is unavailable") from error
    expected = {
        "schema_version": "fanout-brokered-diagnostic-v1",
        "run_id": request.run_id, "task_id": request.task_id,
        "target_id": request.target_id, "seat_id": request.seat_id,
        "inputs_digest": request.inputs_digest,
        "profile_digest": request.profile.digest,
        "boundary_receipt_digest": initial.boundary.receipt_digest,
        "route": f"initial:assigned:{request.session_id}" if request.executor_id == "claude"
                 else "initial:unassigned",
        "session_id": request.session_id,
        "valid": True, "reason": "ok",
        "process_status": ProcessStatus.EXIT.value, "exit_code": 0,
        "stdout_sha256": initial.result.stdout_digest,
        "stderr_sha256": initial.result.stderr_digest,
        "observed_model": initial.result.observed_model,
        "broker_model": initial.broker_model,
        "effective_output_tokens": list(initial.effective_output_tokens),
        "refused_agy_auxiliary_requests": initial.refused_agy_auxiliary_requests,
    }
    if receipt != expected or initial.result.session_id != request.session_id:
        raise ProviderRequestError("exact diagnostic resume differs from initial evidence")
