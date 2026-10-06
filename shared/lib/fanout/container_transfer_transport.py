"""Controller-owned, bounded Docker transfer for a single retained seat."""
from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import secrets
import selectors
import stat
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import BinaryIO, Iterable, Iterator

from .artifacts import ArtifactLimits, ArtifactRef, ArtifactStore, canonical_json
from .broker import BrokerRejected
from .container_runtime import ContainerHandle, ContainerSpec, DOCKER, decode_launch_record, private_volume_names
from .container_transfer import (
    MAX_ENTRIES, MAX_FRAME, MAX_MANIFEST, MAX_TOTAL, TransferEntry, TransferIdentity,
    TransferManifest, TransferRejected, _decode_manifest, _import_content,
    _read_exact, _read_length, _validate_identity, encode_manifest,
    seal_transfer, snapshot_manifest, stage_import,
)
from .container_vm import VMProbe
from .controller import (
    LifecycleController, assert_controller, controller_directory,
    read_evidence, write_evidence,
)
from .errors import ArtifactError, LifecycleError, ProviderRequestError
from .process import _cleanup_group, build_child_environment


_MATERIAL_CATEGORY = "container-input-material"
_GRANT_CATEGORY = "container-input-grant"
_RECEIPT_CATEGORY = "container-input-transfer"
_EXPORT_CATEGORY = "container-candidate-export"
_HANDOFF_CATEGORY = "quality-container-input-handoff"
_JOURNAL_SCHEMA = "fanout-input-claim-v1"
_MAGIC = b"FANOUT-XFER-1\n"
_MAX_MANIFEST_WIRE = MAX_MANIFEST + 4 * ((MAX_MANIFEST + MAX_FRAME - 1) // MAX_FRAME)
_MAX_EXPORT_STREAM = (MAX_TOTAL + _MAX_MANIFEST_WIRE
                      + 4 * (MAX_ENTRIES + (MAX_TOTAL + MAX_FRAME - 1) // MAX_FRAME)
                      + len(_MAGIC) + 4)
_MAX_STDERR = 64 * 1024
_MAX_ACK = 65
_MATERIAL_STORE = "input-material-store"
_MATERIAL_KEYS = frozenset({
    "schema_version", "identity", "source_device", "source_inode",
    "original_handle", "workspace_volume", "run_inputs_sha256",
    "task_packet_sha256", "target_baseline_sha256", "workspace_sha256",
    "skill_delivery_sha256", "dependency_context_sha256", "manifest_ref",
    "provider_context_sha256", "skill_bundle_sha256",
})
_DIGEST_KEYS = (
    "run_inputs_sha256", "task_packet_sha256", "target_baseline_sha256",
    "workspace_sha256", "skill_delivery_sha256", "dependency_context_sha256",
    "provider_context_sha256", "skill_bundle_sha256",
)
_STABLE_VM_FIELDS = (
    "profile", "docker_endpoint", "effective_mounts_sha256",
    "guest_mounts_sha256", "ssh_host_key_sha256",
)
_VM_HASH_FIELDS = (
    "effective_mounts_sha256", "guest_mounts_sha256",
    "stdio_roundtrip_sha256", "ssh_host_key_sha256",
)


@dataclass(frozen=True, slots=True)
class InputSourceGrant:
    source_root: Path
    identity: TransferIdentity
    source_device: int
    source_inode: int
    material_evidence_name: str
    material_evidence_digest: str
    evidence_name: str
    evidence_digest: str
    admission: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class QualityProvisioning:
    store_root: Path
    allocation: tuple[str, str]
    bindings: dict
    role: str
    phase: str
    condition: str
    catalog: dict | None = None
    backend_attachment: tuple[str, str] | None = None


def _quality_context(value):
    if (type(value) is not QualityProvisioning or type(value.store_root) is not type(Path())
            or type(value.allocation) is not tuple or len(value.allocation) != 2
            or not _sha(value.allocation[1]) or value.allocation[0] != value.allocation[1] + ".json"
            or type(value.bindings) is not dict
            or set(value.bindings) != {"campaign", "casebook", "source", "projection", "profile", "inputs"}
            or any(not _sha(item) for item in value.bindings.values())
            or any(type(item) is not str or not 1 <= len(item) <= 64
                   for item in (value.role, value.phase, value.condition))
            or value.catalog is not None and type(value.catalog) is not dict
            or value.backend_attachment is not None and (
                type(value.backend_attachment) is not tuple or len(value.backend_attachment) != 2
                or not _sha(value.backend_attachment[1])
                or value.backend_attachment[0] != value.backend_attachment[1] + ".json")):
        raise ProviderRequestError("quality provisioning context is invalid")
    result = {"store_root": str(value.store_root), "allocation": list(value.allocation),
            "bindings": value.bindings, "role": value.role, "phase": value.phase,
            "condition": value.condition, "catalog": value.catalog}
    if value.backend_attachment is not None:
        result["backend_attachment"] = list(value.backend_attachment)
    return result


def _registered_quality_row(controller, request, context, permit, permit_id, epoch):
    from . import diagnostic
    from .container_certification import _read_owned_json
    from .container_source import runtime_source_digest
    from .providers import ProviderRequest, ExecutorProfile
    try:
        _quality_context(context)
        if (type(epoch) is not int or epoch < 1 or not _sha(permit_id)
                or type(request) is not ProviderRequest or type(request.profile) is not ExecutorProfile
                or len(request.prompt_bytes) > 1024 * 1024
                or request.execution_class != "read-only" or request.resume is not False
                or request.retries != 0 or permit.route != "initial"
                or context.phase != "initial" or context.bindings["source"] != runtime_source_digest()
                or context.bindings["inputs"] != request.inputs_digest
                or any(getattr(request, key) is not None for key in
                       ("native_boundary", "native_controller", "native_verification",
                        "container_boundary", "canary_capture"))
                or request.certificate_roots != ()
                or any(getattr(permit, key) != getattr(request, key) for key in
                       ("run_id", "task_id", "target_id", "seat_id", "inputs_digest", "session_id"))
                or permit.provider != request.executor_id or permit.model != request.profile.requested_model
                or permit.upstream_route_digest is None):
            raise ValueError("initial request")
        with ArtifactStore.open_existing(context.store_root) as store:
            allocation = diagnostic.reopen_quality_allocation(controller, store,
                context.allocation, bindings=context.bindings)
            rows = [row for row in allocation["rows"] if
                    (row["role"], row["phase"], row["condition"], row["epoch"]) ==
                    (context.role, context.phase, context.condition, epoch)]
            if (len(rows) != 1 or rows[0]["blocker"] is not None or rows[0]["initial"] is not None
                    or rows[0]["profile"] != request.profile.digest
                    or rows[0]["process_seconds"] != request.timeout
                    or rows[0]["catalog"] != (None if context.catalog is None else context.catalog["projected"]["digest"])):
                raise ValueError("allocated row")
            if request.executor_id == "codex":
                diagnostic._reopen_quality_catalog(store, context.catalog)
            elif context.catalog is not None:
                raise ValueError("unexpected catalog")
            claim = {"allocation": context.allocation[1], "row": rows[0]}
            digest = hashlib.sha256(canonical_json(claim)).hexdigest()
            actual, _ = _read_owned_json(controller_directory(controller, "locks") / f"quality-row-{digest}")
            pointer, _ = _read_owned_json(controller_directory(controller, "locks") / f"quality-registration-{digest}")
            if set(pointer) != {"authority"}:
                raise ValueError("registration pointer")
            registration = read_evidence(controller, "quality-permit-registration", *pointer["authority"])
            expected = {**claim, "permit_id": permit_id, "epoch": epoch,
                        "permit": asdict(permit), "initial_receipt": None}
            if context.backend_attachment is not None:
                expected["backend_attachment"] = list(context.backend_attachment)
            if (canonical_json(actual) != canonical_json(claim)
                    or canonical_json(registration) != canonical_json(expected)
                    or asdict(diagnostic._quality_resolved_permit(controller, store,
                        context.allocation, allocation, rows[0], None,
                        backend_attachment=context.backend_attachment)) != asdict(permit)):
                raise ValueError("consumed registration")
        return list(pointer["authority"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError, ArtifactError, LifecycleError) as error:
        raise ProviderRequestError("quality provisioning allocation or request differs") from error


def _quality_zero(controller, request, admission):
    from .container_boundary import SessionStore
    from .container_broker import PermitLedger, _digest
    if (type(admission.ledger) is not PermitLedger or admission.ledger.controller is not controller
            or admission.ledger.root != controller_directory(controller, "locks") / "spend"
            or type(admission.session_store) is not SessionStore
            or admission.session_store.controller is not controller
            or admission.session_store.identity !=
               (request.run_id, request.task_id, request.target_id, request.seat_id)
            or admission.session_store.volume != private_volume_names(
                request.run_id, request.task_id, request.target_id, request.seat_id)[1]):
        raise ProviderRequestError("quality provisioning ledger or session association differs")
    with admission.ledger._locked() as state:
        entry = state["permits"].get(admission.permit_id)
        requests = [key for key, value in state["requests"].items()
                    if value["permit_id"] == admission.permit_id]
        if (not isinstance(entry, dict) or entry["permit_digest"] != _digest(admission.broker_permit)
                or entry["epoch"] != admission.epoch or entry["fenced"] is not False
                or any(entry[key] != 0 for key in
                       ("used", "attempts", "auxiliary_attempts", "auxiliary_refusals"))
                or requests):
            raise ProviderRequestError("quality provisioning requires zero permit activity")
        observed = {key: entry[key] for key in
                    ("used", "attempts", "auxiliary_attempts", "auxiliary_refusals")}
        observed["requests"] = requests
    def empty(state):
        if state["bindings"] or state["turns"]:
            raise ProviderRequestError("quality provisioning requires an empty session store")
        return {"bindings": dict(state["bindings"]), "turns": dict(state["turns"])}, False
    observed.update(admission.session_store._update(empty))
    return observed


def _quality_provisioning_association(controller, request, admission):
    from .container_boundary import ContainerAdmission
    if type(admission) is not ContainerAdmission:
        raise ProviderRequestError("quality provisioning admission type differs")
    registration = _registered_quality_row(controller, request, admission.quality_provisioning,
        admission.broker_permit, admission.permit_id, admission.epoch)
    _quality_zero(controller, request, admission)
    return registration


_ZERO_ACTIVITY = {"used": 0, "attempts": 0, "auxiliary_attempts": 0,
                  "auxiliary_refusals": 0, "requests": [], "bindings": {}, "turns": {}}


def _handoff_pointer(controller, identity):
    digest = hashlib.sha256(canonical_json(asdict(identity))).hexdigest()
    return controller_directory(controller, "locks") / f"quality-input-handoff-{digest}"


def _reopen_quality_handoff_preparation(controller, request, proof):
    """Authenticate immutable import/stop facts, not current turn authority."""
    from .container_certification import _read_owned_json
    from .container_runtime import ContainerLauncher, _json_one
    try:
        value = read_evidence(controller, _HANDOFF_CATEGORY, *proof)
        if (set(value) != {"schema", "request_digest", "registration", "input_receipt",
                          "provisioning_spec", "model_spec", "stop", "zero_activity"}
                or value["schema"] != "quality-container-handoff-preparation-v1"
                or canonical_json(value["zero_activity"]) != canonical_json(_ZERO_ACTIVITY)
                or value["request_digest"] != admitted_request_digest(request)):
            raise ValueError("handoff preparation")
        pointer, _ = _read_owned_json(_handoff_pointer(controller, _identity(request)))
        if pointer != {"preparation": list(proof)}:
            raise ValueError("handoff pointer")
        receipt = _completed_input_receipt(controller, request,
            private_volume_names(request.run_id, request.task_id, request.target_id, request.seat_id)[0])
        if value["input_receipt"] != [receipt.evidence_name, receipt.evidence_digest]:
            raise ValueError("completed import")
        grant = read_evidence(controller, _GRANT_CATEGORY,
                              receipt.grant_evidence_name, receipt.grant_evidence_digest)
        raw = grant["quality_provisioning"]
        original_fields = set(QualityProvisioning.__dataclass_fields__) - {"backend_attachment"}
        if set(raw) not in (original_fields, original_fields | {"backend_attachment"}):
            raise ValueError("quality source context")
        backend = None
        if "backend_attachment" in raw:
            if type(raw["backend_attachment"]) is not list or len(raw["backend_attachment"]) != 2:
                raise ValueError("quality source backend pointer")
            backend = tuple(raw["backend_attachment"])
        context = QualityProvisioning(Path(raw["store_root"]), tuple(raw["allocation"]),
            raw["bindings"], raw["role"], raw["phase"], raw["condition"], raw["catalog"], backend)
        permit = _decode_original_permit(controller, grant)
        if value["registration"] != _registered_quality_row(controller, request, context,
                permit, grant["permit_id"], grant["permit_epoch"]):
            raise ValueError("original registration")
        spec = ContainerSpec(**value["provisioning_spec"])
        model_spec = ContainerSpec(**value["model_spec"])
        ContainerLauncher._validate_spec(spec)
        ContainerLauncher._validate_spec(model_spec)
        if (spec.workspace_readonly is not False or model_spec.workspace_readonly is not True
                or model_spec != replace(spec, workspace_readonly=True)
                or canonical_json(value["provisioning_spec"]) != canonical_json(asdict(spec))
                or canonical_json(value["model_spec"]) != canonical_json(asdict(model_spec))):
            raise ValueError("handoff specs")
        origin = read_evidence(controller, "container-seat-launch",
                               receipt.handle_evidence_name, receipt.handle_evidence_digest)
        decode_launch_record(origin, spec, receipt.original_handle, recover=False)
        reopen_stopped_seat(controller, value["stop"], spec=spec, handle=receipt.original_handle)
        stopped = read_evidence(controller, "container-seat-stopped", *value["stop"])
        if (stopped["launch_evidence_name"], stopped["launch_evidence_digest"]) != (
                receipt.handle_evidence_name, receipt.handle_evidence_digest):
            raise ValueError("original stopped launch")
        with ArtifactStore.open_existing(controller_directory(controller, "quarantine") / "runtime-proofs") as store:
            item = _json_one(store.read_bytes(ArtifactRef(**stopped["raw_inspect"])), "handoff stopped inspect")
        observed = ContainerLauncher._stopped_observation(receipt.original_handle.container_id, spec, item)
        return value, receipt, grant, spec, model_spec, origin, (receipt.original_handle.container_id, observed)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, ArtifactError, LifecycleError) as error:
        raise ProviderRequestError("quality container handoff authentication failed") from error


def reopen_quality_handoff_history(controller, request, proof):
    """Read completed handoff provenance after later model activity; grants no launch."""
    try:
        completed = read_evidence(controller, _HANDOFF_CATEGORY, *proof)
        if set(completed) != {"schema", "preparation", "model_launch"} or completed["schema"] != "quality-container-handoff-v1":
            raise ValueError("completed handoff")
        value, receipt, grant, spec, model_spec, origin, stopped = _reopen_quality_handoff_preparation(
            controller, request, completed["preparation"])
        model = read_evidence(controller, "container-seat-launch", *completed["model_launch"])
        handle = ContainerHandle(**model["handle"])
        association = decode_launch_record(model, model_spec, handle, recover=False, handoff=True)
        if (model["original_handoff"] != completed["preparation"]
                or association.predecessors != _quality_handoff_stopped_scope(controller, model, spec, receipt.original_handle, stopped)
                or model["volumes"] != origin["volumes"]):
            raise ValueError("handoff model launch")
        return value
    except (OSError, ValueError, KeyError, TypeError, AttributeError, ArtifactError, LifecycleError) as error:
        raise ProviderRequestError("quality container completed handoff differs") from error


def reopen_quality_handoff(controller, request, proof, *, ledger, sessions):
    """Authenticate an unused handoff against current zero ledger/empty sessions."""
    from .container_boundary import ContainerAdmission
    value = reopen_quality_handoff_history(controller, request, proof)
    receipt = _completed_input_receipt(controller, request,
        private_volume_names(request.run_id, request.task_id, request.target_id, request.seat_id)[0])
    grant = read_evidence(controller, _GRANT_CATEGORY, receipt.grant_evidence_name, receipt.grant_evidence_digest)
    _quality_zero(controller, request, ContainerAdmission(_decode_original_permit(controller, grant),
        grant["permit_id"], grant["permit_epoch"], ledger, session_store=sessions))
    return value


def _prepare_quality_handoff(controller, request, transport, admission):
    if getattr(transport, "_quarantined", False):
        raise ProviderRequestError("quality provisioning transport is already stopped")
    _preflight(controller, request, transport, admission)
    registration = _quality_provisioning_association(controller, request, admission)
    validate_input_receipt(controller, request, admission.input_receipt, transport.spec.workspace_volume)
    transport._close(retain_control=True)
    _quality_provisioning_association(controller, request, admission)
    observed = _quality_zero(controller, request, admission)
    proof = write_evidence(controller, _HANDOFF_CATEGORY, {
        "schema": "quality-container-handoff-preparation-v1",
        "request_digest": admitted_request_digest(request), "registration": registration,
        "input_receipt": [admission.input_receipt.evidence_name, admission.input_receipt.evidence_digest],
        "provisioning_spec": asdict(transport.spec),
        "model_spec": asdict(replace(transport.spec, workspace_readonly=True)),
        "stop": list(transport.stop_evidence), "zero_activity": observed})
    try:
        fd = os.open(_handoff_pointer(controller, _identity(request)),
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError as error:
        raise ProviderRequestError("quality input handoff already claimed") from error
    with os.fdopen(fd, "wb") as handle:
        handle.write(canonical_json({"preparation": list(proof)}))
        handle.flush()
        os.fsync(handle.fileno())
    _reopen_quality_handoff_preparation(controller, request, proof)
    return proof


@dataclass(frozen=True, slots=True)
class InputTransferReceipt:
    identity: TransferIdentity
    manifest_sha256: str
    workspace_volume: str
    workdir: str
    original_handle: ContainerHandle
    handle_evidence_name: str
    handle_evidence_digest: str
    material_evidence_name: str
    material_evidence_digest: str
    grant_evidence_name: str
    grant_evidence_digest: str
    evidence_name: str
    evidence_digest: str


@dataclass(frozen=True, slots=True)
class CandidateExportReceipt:
    identity: TransferIdentity
    turn_digest: str
    manifest_sha256: str
    root: Path
    container_id: str
    container_inspect_sha256: str
    workspace_volume: str
    session_volume: str
    evidence_name: str
    evidence_digest: str


def _sha(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        char in "0123456789abcdef" for char in value)


def _identity(request) -> TransferIdentity:
    identity = TransferIdentity(request.run_id, request.task_id,
                                request.target_id, request.seat_id,
                                request.inputs_digest)
    try:
        _validate_identity(identity)
    except TransferRejected as error:
        raise ProviderRequestError("container transfer request identity is invalid") from error
    return identity


def _workdir(identity: TransferIdentity) -> str:
    digest = hashlib.sha256(canonical_json(asdict(identity))).hexdigest()
    return f"/workspace/inputs/{digest}/published/repo"


def admitted_request_digest(request) -> str:
    """Bind the prompt and all scalar turn controls before a model can run."""
    return hashlib.sha256(canonical_json({
        "executor_id": request.executor_id,
        "prompt_sha256": hashlib.sha256(request.prompt_bytes).hexdigest(),
        "cwd": str(request.cwd),
        "execution_class": request.execution_class,
        "session_id": request.session_id,
        "resume": request.resume,
        "timeout": request.timeout,
        "timeout_override_reason": request.timeout_override_reason,
        "timeout_override_review_sha256": request.timeout_override_review_sha256,
        "retries": request.retries,
        "profile_digest": request.profile.digest if request.profile is not None else None,
        "artifact_prefix": request.artifact_prefix,
        "slot_cap": request.slot_cap,
        "slot_timeout": request.slot_timeout,
        "slot_root": str(request.slot_root) if request.slot_root is not None else None,
        "context_sha256": request.context_sha256,
        "skill_bundle_sha256": request.skill_bundle_sha256,
        "skill_delivery_sha256": request.skill_delivery_sha256,
        "staged_skill_root": (str(request.staged_skill_root)
                              if request.staged_skill_root is not None else None),
        "run_id": request.run_id, "task_id": request.task_id,
        "target_id": request.target_id, "seat_id": request.seat_id,
        "inputs_digest": request.inputs_digest,
    })).hexdigest()


def _root_info(controller: LifecycleController, path: Path, *, empty: bool = False):
    quarantine = controller_directory(controller, "quarantine")
    candidate = Path(path)
    if candidate.parent != quarantine or candidate.name in {"", ".", ".."}:
        raise ProviderRequestError("container transfer root is outside private quarantine")
    parent_fd = os.open(quarantine, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(candidate.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                     dir_fd=parent_fd)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o700):
                raise ProviderRequestError("container transfer root is not owner-private")
            if empty:
                with os.scandir(fd) as entries:
                    if next(entries, None) is not None:
                        raise ProviderRequestError("container export quarantine is not empty")
            return info
        finally:
            os.close(fd)
    except OSError as error:
        raise ProviderRequestError("container transfer root is unavailable") from error
    finally:
        os.close(parent_fd)


def _material(controller: LifecycleController, name: str, digest: str,
              identity: TransferIdentity, handle: ContainerHandle,
              source_info, request=None) -> tuple[TransferManifest, ArtifactRef]:
    try:
        value = read_evidence(controller, _MATERIAL_CATEGORY, name, digest)
        if set(value) != _MATERIAL_KEYS or value["schema_version"] != "InputMaterialV1":
            raise ValueError("material shape")
        if (value["identity"] != asdict(identity)
                or value["source_device"] != source_info.st_dev
                or value["source_inode"] != source_info.st_ino
                or value["original_handle"] != asdict(handle)
                or value["workspace_volume"] != handle.workspace_volume
                or value["run_inputs_sha256"] != identity.inputs_digest
                or (request is not None and not request.resume and
                    value["provider_context_sha256"] != request.context_sha256)
                or (request is not None and value["skill_bundle_sha256"] != request.skill_bundle_sha256)
                or (request is not None and value["skill_delivery_sha256"] != request.skill_delivery_sha256)
                or any(not _sha(value[key]) for key in _DIGEST_KEYS)):
            raise ValueError("material provenance")
        ref_value = value["manifest_ref"]
        if not isinstance(ref_value, dict) or set(ref_value) != {"path", "digest", "size"}:
            raise ValueError("manifest reference")
        ref = ArtifactRef(**ref_value)
        if (not _sha(ref.digest) or type(ref.size) is not int
                or not 0 < ref.size <= MAX_MANIFEST):
            raise ValueError("manifest reference")
        store_root = controller_directory(controller, "quarantine") / _MATERIAL_STORE
        with ArtifactStore.open_existing(
            store_root, limits=ArtifactLimits(MAX_MANIFEST, MAX_MANIFEST * 2),
        ) as store:
            raw = store.read_bytes(ref)
        payload = json.loads(raw)
        if (not isinstance(payload, dict) or set(payload) !=
                {"schema_version", "identity", "entries"}
                or not isinstance(payload["identity"], dict)
                or not isinstance(payload["entries"], list)
                or canonical_json(payload) != raw):
            raise ValueError("manifest artifact")
        manifest = TransferManifest(
            payload["schema_version"], TransferIdentity(**payload["identity"]),
            tuple(TransferEntry(**entry) for entry in payload["entries"]),
        )
        encode_manifest(manifest)
        if manifest.identity != identity:
            raise ValueError("manifest identity")
        kinds = {entry.path: entry.kind for entry in manifest.entries}
        if (kinds.get("repo") != "directory" or kinds.get("context") != "directory"
                or {path.split("/", 1)[0] for path in kinds} != {"repo", "context"}
                or not any(path.startswith("context/") for path in kinds)):
            raise ValueError("material is incomplete")
        return manifest, ref
    except (OSError, ValueError, TypeError, KeyError, TransferRejected,
            ArtifactError, LifecycleError) as error:
        raise ProviderRequestError("container input material or manifest is invalid") from error


def _preflight(controller: LifecycleController, request, transport,
               admission, expected_vm: dict | None = None) -> dict:
    from .container_boundary import (
        ContainerAdmission, PinnedDockerTransport, PinnedSSHSeatControl,
        _authenticated_launch, _check_association,
    )
    assert_controller(controller)
    if (type(transport) is not PinnedDockerTransport
            or not isinstance(admission, ContainerAdmission)
            or admission.transport is not transport
            or transport.controller is not controller
            or (admission.handle_evidence_name, admission.handle_evidence_digest) !=
            (transport.handle_evidence_name, transport.handle_evidence_digest)):
        raise ProviderRequestError("container transfer authority is invalid")
    control = getattr(transport, "_control", None)
    if getattr(transport, "_production_edge", False) and control is None:
        raise ProviderRequestError("container production control is unavailable")
    if control is not None and (
            type(control) is not PinnedSSHSeatControl
            or control.controller is not controller
            or control.spec != transport.spec
            or control.permit_id != admission.permit_id
            or control.epoch != admission.epoch
            or control._lease is None or control._lease._fd is None):
        raise ProviderRequestError("container live control lease is unavailable")
    identity = _identity(request)
    spec = transport.spec
    handle = transport.handle
    workspace, session = private_volume_names(
        identity.run_id, identity.task_id, identity.target_id, identity.seat_id)
    if ((spec.run_id, spec.task_id, spec.target_id, spec.seat_id,
         spec.image_digest, spec.cli_digest, spec.workspace_volume,
         spec.session_volume, spec.socket_path) !=
        (identity.run_id, identity.task_id, identity.target_id, identity.seat_id,
         admission.image_digest, admission.cli_digest, workspace, session,
         f"/run/fanout/{identity.seat_id}.sock")
            or (handle.workspace_volume, handle.session_volume) !=
            (workspace, session)):
        raise ProviderRequestError("container transfer target or volume differs")
    if admission.quality_provisioning is None:
        _check_association(request, admission)
    else:
        if spec.workspace_readonly is not False:
            raise ProviderRequestError("quality provisioning workspace must be writable")
        _quality_provisioning_association(controller, request, admission)
        if getattr(transport, "_production_edge", False):
            import types
            from .container_boundary import quality_docker_runner, HostSessionLease
            runner = transport.launcher._docker
            try:
                if (type(runner) is not types.FunctionType
                        or runner.__code__ not in quality_docker_runner.__code__.co_consts
                        or runner.controller is not controller
                        or control.ledger is not admission.ledger
                        or canonical_json(asdict(control.broker.permit)) != canonical_json(asdict(admission.broker_permit))
                        or type(control._lease) is not HostSessionLease
                        or control._lease.path != controller_directory(controller, "locks") /
                           f"relay-{admission.permit_id}-{admission.epoch}.lock"):
                    raise ValueError("fixed helper or retained control")
                control._lease.require_held()
                proof = read_evidence(controller, "quality-docker-controls", *runner.controls)
                limits = proof["limits"]
                quality_docker_runner(controller,
                    allocation_digest=admission.quality_provisioning.allocation[1],
                    max_commands=limits["max_commands"], command_seconds=limits["command_seconds"],
                    outer_seconds=limits["outer_seconds"], original_controls=runner.controls)
            except (OSError, ValueError, TypeError, AttributeError, KeyError, LifecycleError) as error:
                raise ProviderRequestError("quality provisioning requires the original finite Docker helper") from error
    _authenticated_launch(controller, request, handle, admission)
    try:
        vm = transport.attest_vm()
        running = transport.attest_container()
        socket = transport.attest_socket()
        image = transport.attest_image()
        cli = transport.attest_cli(request.executor_id)
    except Exception as error:
        raise ProviderRequestError("container transfer live attestation failed") from error
    if (not isinstance(vm, VMProbe) or vm.profile != "fanout-isolated"
            or running != handle or socket != admission.socket_attestation
            or image != admission.image_digest or cli != admission.cli_digest):
        raise ProviderRequestError("container transfer live attestation differs")
    proof = asdict(vm)
    if expected_vm is not None:
        if (not isinstance(expected_vm, dict) or set(expected_vm) != set(proof)
                or expected_vm["profile"] != "fanout-isolated"
                or not isinstance(expected_vm["docker_endpoint"], str)
                or not expected_vm["docker_endpoint"]
                or any(not _sha(expected_vm[field]) for field in _VM_HASH_FIELDS)):
            raise ProviderRequestError("container source grant VM probe is invalid")
        if any(proof[field] != expected_vm[field] for field in _STABLE_VM_FIELDS):
            raise ProviderRequestError("container transfer VM attestation changed")
    return proof


def issue_input_source_grant(controller: LifecycleController, request,
                             transport, admission, source_root: Path,
                             material_evidence_name: str,
                             material_evidence_digest: str) -> InputSourceGrant:
    """Issue a source grant only from authenticated controller material."""
    if request.resume or admission.broker_permit.route != "initial":
        raise ProviderRequestError("container original input grant requires an initial permit")
    vm = _preflight(controller, request, transport, admission)
    identity = _identity(request)
    info = _root_info(controller, source_root)
    manifest, _ = _material(controller, material_evidence_name,
                            material_evidence_digest, identity,
                            transport.handle, info, request)
    if snapshot_manifest(Path(source_root), identity) != manifest:
        raise ProviderRequestError("container source differs from input material")
    payload = {
        "identity": asdict(identity), "source_root": str(source_root),
        "source_device": info.st_dev, "source_inode": info.st_ino,
        "material_evidence_name": material_evidence_name,
        "material_evidence_digest": material_evidence_digest,
        "handle_evidence_name": transport.handle_evidence_name,
        "handle_evidence_digest": transport.handle_evidence_digest,
        "original_handle": asdict(transport.handle),
        "vm_probe": vm, "socket": asdict(admission.socket_attestation),
        "image_digest": admission.image_digest,
        "cli_digest": admission.cli_digest,
        "profile_digest": request.profile.digest,
        "request_digest": admitted_request_digest(request),
        "permit_id": admission.permit_id,
        "permit_epoch": admission.epoch,
        "permit_digest": hashlib.sha256(canonical_json(asdict(admission.broker_permit))).hexdigest(),
        "original_permit": asdict(admission.broker_permit),
    }
    if admission.quality_provisioning is not None:
        payload["quality_provisioning"] = _quality_context(admission.quality_provisioning)
    name, digest = write_evidence(controller, _GRANT_CATEGORY, payload)
    return InputSourceGrant(Path(source_root), identity, info.st_dev,
                            info.st_ino, material_evidence_name,
                            material_evidence_digest, name, digest, admission)


def _open_grant(controller: LifecycleController, request, transport,
                grant: InputSourceGrant) -> tuple[TransferManifest, dict]:
    if not isinstance(grant, InputSourceGrant):
        raise ProviderRequestError("container source grant is invalid")
    admission = grant.admission
    try:
        payload = read_evidence(controller, _GRANT_CATEGORY,
                                grant.evidence_name, grant.evidence_digest)
    except Exception as error:
        raise ProviderRequestError("container source grant is not authenticated") from error
    if (grant.identity != _identity(request)
            or grant.source_root != Path(payload.get("source_root", ""))
            or (grant.source_device, grant.source_inode) !=
            (payload.get("source_device"), payload.get("source_inode"))
            or (grant.material_evidence_name, grant.material_evidence_digest) !=
            (payload.get("material_evidence_name"),
             payload.get("material_evidence_digest"))
            or payload.get("identity") != asdict(grant.identity)
            or payload.get("original_handle") != asdict(transport.handle)
            or payload.get("handle_evidence_name") != transport.handle_evidence_name
            or payload.get("handle_evidence_digest") != transport.handle_evidence_digest
            or payload.get("profile_digest") != request.profile.digest
            or payload.get("request_digest") != admitted_request_digest(request)
            or payload.get("permit_id") != admission.permit_id
            or payload.get("permit_epoch") != admission.epoch
            or payload.get("original_permit") != asdict(admission.broker_permit)
            or payload.get("permit_digest") != hashlib.sha256(
                canonical_json(asdict(admission.broker_permit))).hexdigest()
            or payload.get("socket") != asdict(admission.socket_attestation)
            or payload.get("image_digest") != admission.image_digest
            or payload.get("cli_digest") != admission.cli_digest):
        raise ProviderRequestError("container source grant association differs")
    if payload.get("quality_provisioning") != (None if admission.quality_provisioning is None
                                               else _quality_context(admission.quality_provisioning)):
        raise ProviderRequestError("quality source grant context differs")
    sealed_vm = payload.get("vm_probe")
    if not isinstance(sealed_vm, dict):
        raise ProviderRequestError("container source grant VM probe is invalid")
    _preflight(controller, request, transport, admission, sealed_vm)
    info = _root_info(controller, grant.source_root)
    if (info.st_dev, info.st_ino) != (grant.source_device, grant.source_inode):
        raise ProviderRequestError("container source root changed")
    manifest, _ = _material(controller, grant.material_evidence_name,
                            grant.material_evidence_digest, grant.identity,
                            transport.handle, info, request)
    if snapshot_manifest(grant.source_root, grant.identity) != manifest:
        raise ProviderRequestError("container source differs from input material")
    return manifest, payload


class _InputClaimJournal:
    """One durable claim per controller, seat, and workspace volume."""

    def __init__(self, controller: LifecycleController, identity: TransferIdentity,
                 volume: str):
        self.controller = controller
        self.identity = identity
        self.volume = volume
        stem = hashlib.sha256(canonical_json({
            "controller": controller.controller_id,
            "seat": identity.seat_id, "volume": volume,
        })).hexdigest()
        self.path = controller_directory(controller, "transactions") / f"input-{stem}.json"
        self.confirmation = controller_directory(controller, "transactions") / f"input-{stem}.confirmed"
        self.lock = controller_directory(controller, "locks") / f"input-{stem}.lock"

    def _mac(self, payload: dict) -> str:
        return hmac.new(self.controller._secret.encode("ascii"),
                        b"fanout-input-claim\0" + canonical_json(payload),
                        hashlib.sha256).hexdigest()

    def _confirmation_mac(self, payload: dict) -> str:
        return hmac.new(self.controller._secret.encode("ascii"),
                        b"fanout-input-confirmation\0" + canonical_json(payload),
                        hashlib.sha256).hexdigest()

    def _confirmation_payload(self, value: dict) -> dict:
        return {
            "schema_version": "fanout-input-confirmation-v1",
            "controller_id": self.controller.controller_id,
            "identity": asdict(self.identity),
            "workspace_volume": self.volume,
            "grant_evidence_digest": value["grant_evidence_digest"],
            "receipt_evidence_name": value["receipt_evidence_name"],
            "receipt_evidence_digest": value["receipt_evidence_digest"],
        }

    def _confirm(self, value: dict) -> None:
        payload = self._confirmation_payload(value)
        data = canonical_json({"payload": payload,
                               "mac": self._confirmation_mac(payload)})
        fd = os.open(self.confirmation, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     os.O_NOFOLLOW, 0o600)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        directory = os.open(self.confirmation.parent,
                            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _require_confirmation(self, value: dict) -> None:
        try:
            fd = os.open(self.confirmation, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError as error:
            raise ProviderRequestError("container input claim is not complete") from error
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 64 * 1024):
                raise ProviderRequestError("container input confirmation is unsafe")
            data = os.read(fd, 64 * 1024 + 1)
        finally:
            os.close(fd)
        try:
            record = json.loads(data)
            payload = self._confirmation_payload(value)
            if (set(record) != {"payload", "mac"}
                    or canonical_json(record) != data
                    or record["payload"] != payload
                    or not hmac.compare_digest(record["mac"],
                                               self._confirmation_mac(payload))):
                raise ValueError("confirmation authentication")
        except (TypeError, KeyError, ValueError, UnicodeError) as error:
            raise ProviderRequestError("container input confirmation is invalid") from error

    def _read(self) -> dict | None:
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 64 * 1024):
                raise ProviderRequestError("container input claim is unsafe")
            data = os.read(fd, 64 * 1024 + 1)
        finally:
            os.close(fd)
        try:
            record = json.loads(data)
            payload = record["payload"]
            if (set(record) != {"payload", "mac"}
                    or canonical_json(record) != data
                    or not hmac.compare_digest(record["mac"], self._mac(payload))
                    or payload["schema_version"] != _JOURNAL_SCHEMA
                    or payload["controller_id"] != self.controller.controller_id
                    or payload["identity"] != asdict(self.identity)
                    or payload["workspace_volume"] != self.volume
                    or payload["state"] not in {"pending", "complete", "abandoned"}):
                raise ValueError("claim authentication")
            return payload
        except (TypeError, KeyError, ValueError, UnicodeError) as error:
            raise ProviderRequestError("container input claim authentication failed") from error

    def _write(self, payload: dict) -> None:
        data = canonical_json({"payload": payload, "mac": self._mac(payload)})
        if len(data) > 64 * 1024:
            raise ProviderRequestError("container input claim exceeds limit")
        temporary = self.path.parent / f".input-{secrets.token_hex(16)}"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, self.path)
        directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _locked(self, operation):
        assert_controller(self.controller)
        fd = os.open(self.lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600):
                raise ProviderRequestError("container input claim lock is unsafe")
            fcntl.flock(fd, fcntl.LOCK_EX)
            return operation()
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def claim(self, grant: InputSourceGrant) -> None:
        def operation():
            if self._read() is not None:
                raise ProviderRequestError("container input claim forbids restage")
            self._write({
                "schema_version": _JOURNAL_SCHEMA,
                "controller_id": self.controller.controller_id,
                "identity": asdict(self.identity),
                "workspace_volume": self.volume,
                "state": "pending",
                "grant_evidence_name": grant.evidence_name,
                "grant_evidence_digest": grant.evidence_digest,
                "receipt_evidence_name": None,
                "receipt_evidence_digest": None,
            })
        self._locked(operation)

    def complete(self, grant: InputSourceGrant, name: str, digest: str) -> None:
        def operation():
            value = self._read()
            if (value is None or value["state"] != "pending"
                    or (value["grant_evidence_name"], value["grant_evidence_digest"]) !=
                    (grant.evidence_name, grant.evidence_digest)):
                raise ProviderRequestError("container input claim changed before completion")
            value["state"] = "complete"
            value["receipt_evidence_name"] = name
            value["receipt_evidence_digest"] = digest
            self._write(value)
            self._confirm(value)
        self._locked(operation)

    def abandon(self) -> None:
        def operation():
            value = self._read()
            if value is None or value["state"] == "abandoned":
                return
            value["state"] = "abandoned"
            self._write(value)
        self._locked(operation)

    def completed(self) -> tuple[str, str]:
        def operation():
            value = self._read()
            if value is None or value["state"] != "complete":
                raise ProviderRequestError("container input claim is not complete")
            self._require_confirmation(value)
            return value["receipt_evidence_name"], value["receipt_evidence_digest"]
        return self._locked(operation)


def _run_transfer(transport, command: str, chunks: Iterable[bytes],
                  output: BinaryIO, limit: int) -> tuple[bytes, int]:
    handle = transport.handle
    if (command not in {"import", "export"} or not isinstance(handle, ContainerHandle)
            or not _sha(handle.container_id)):
        raise ProviderRequestError("container transfer command identity is invalid")
    argv = (*DOCKER, "exec", "-i", "--user", "65532:65532", "--workdir",
            "/workspace", handle.container_id, "python",
            "/opt/fanout/container_transfer.py", command)
    if (getattr(transport, "_production_edge", False)
            and ("_transfer_popen" in vars(transport)
                 or "_transfer_timeout" in vars(transport))):
        raise ProviderRequestError("container live transfer edge was overridden")
    popen = getattr(transport, "_transfer_popen", subprocess.Popen)
    timeout = getattr(transport, "_transfer_timeout", 180.0)
    if not isinstance(timeout, (int, float)) or not 0 < timeout <= 600:
        raise ProviderRequestError("container transfer timeout is invalid")
    try:
        process = popen(argv, cwd=transport.controller.root,
                        env=build_child_environment(), stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        start_new_session=True)
    except OSError as error:
        raise ProviderRequestError("container transfer could not start") from error
    selector = selectors.DefaultSelector()
    stderr = bytearray()
    total = 0
    deadline = time.monotonic() + timeout
    try:
        assert process.stdin and process.stdout and process.stderr
        for pipe, event, label in (
            (process.stdin, selectors.EVENT_WRITE, "stdin"),
            (process.stdout, selectors.EVENT_READ, "stdout"),
            (process.stderr, selectors.EVENT_READ, "stderr"),
        ):
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, event, label)
        iterator = iter(chunks)
        pending = memoryview(b"")
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderRequestError("container transfer timed out")
            for key, _ in selector.select(min(remaining, 0.1)):
                label = key.data
                if label == "stdin":
                    if not pending:
                        try:
                            chunk = next(iterator)
                        except StopIteration:
                            selector.unregister(key.fileobj)
                            process.stdin.close()
                            continue
                        if not isinstance(chunk, bytes) or not chunk:
                            raise ProviderRequestError("container transfer input frame is invalid")
                        pending = memoryview(chunk)
                    try:
                        sent = os.write(key.fd, pending[:65536])
                    except BrokenPipeError as error:
                        raise ProviderRequestError("container transfer input closed early") from error
                    pending = pending[sent:]
                elif label == "stdout":
                    chunk = os.read(key.fd, min(65536, limit - total + 1))
                    if not chunk:
                        selector.unregister(key.fileobj)
                    elif total + len(chunk) > limit:
                        raise ProviderRequestError("container transfer output limit exceeded")
                    else:
                        output.write(chunk)
                        total += len(chunk)
                else:
                    chunk = os.read(key.fd, min(65536, _MAX_STDERR - len(stderr) + 1))
                    if not chunk:
                        selector.unregister(key.fileobj)
                    elif len(stderr) + len(chunk) > _MAX_STDERR:
                        raise ProviderRequestError("container transfer stderr limit exceeded")
                    else:
                        stderr.extend(chunk)
        try:
            status = process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as error:
            raise ProviderRequestError("container transfer timed out") from error
        return bytes(stderr), status
    except BaseException:
        if process.poll() is None:
            _cleanup_group(process, 0.5, 3.0)
        raise
    finally:
        selector.close()
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None and not pipe.closed:
                pipe.close()


def _input_payload(receipt: InputTransferReceipt) -> dict:
    return {
        "identity": asdict(receipt.identity),
        "manifest_sha256": receipt.manifest_sha256,
        "workspace_volume": receipt.workspace_volume,
        "workdir": receipt.workdir,
        "original_handle": asdict(receipt.original_handle),
        "handle_evidence_name": receipt.handle_evidence_name,
        "handle_evidence_digest": receipt.handle_evidence_digest,
        "material_evidence_name": receipt.material_evidence_name,
        "material_evidence_digest": receipt.material_evidence_digest,
        "grant_evidence_name": receipt.grant_evidence_name,
        "grant_evidence_digest": receipt.grant_evidence_digest,
    }


def stage_inputs(controller: LifecycleController, request, transport,
                 source_grant: InputSourceGrant) -> InputTransferReceipt:
    """Claim once, stream complete authenticated inputs, and persist the ACK."""
    manifest, _ = _open_grant(controller, request, transport, source_grant)
    wire = encode_manifest(manifest)
    expected_ack = hashlib.sha256(wire).hexdigest().encode("ascii") + b"\n"
    journal = _InputClaimJournal(controller, source_grant.identity,
                                 transport.handle.workspace_volume)
    journal.claim(source_grant)
    try:
        frames: Iterator[bytes] = seal_transfer(manifest, source_grant.source_root)
        chunks = iter((_MAGIC, len(wire).to_bytes(4, "big"), wire))
        import itertools
        with tempfile.TemporaryFile(dir=controller_directory(controller, "quarantine")) as output:
            stderr, status = _run_transfer(
                transport, "import", itertools.chain(chunks, frames),
                output, _MAX_ACK)
            output.seek(0)
            ack = output.read(_MAX_ACK + 1)
        if status != 0 or ack != expected_ack or stderr:
            raise ProviderRequestError("container import ACK or child status differs")
        bare = InputTransferReceipt(
            source_grant.identity, hashlib.sha256(wire).hexdigest(),
            transport.handle.workspace_volume, _workdir(source_grant.identity),
            transport.handle, transport.handle_evidence_name,
            transport.handle_evidence_digest,
            source_grant.material_evidence_name,
            source_grant.material_evidence_digest,
            source_grant.evidence_name, source_grant.evidence_digest, "", "",
        )
        name, digest = write_evidence(controller, _RECEIPT_CATEGORY,
                                      _input_payload(bare))
        journal.complete(source_grant, name, digest)
        return InputTransferReceipt(
            bare.identity, bare.manifest_sha256, bare.workspace_volume,
            bare.workdir, bare.original_handle, bare.handle_evidence_name,
            bare.handle_evidence_digest, bare.material_evidence_name,
            bare.material_evidence_digest, bare.grant_evidence_name,
            bare.grant_evidence_digest, name, digest,
        )
    except BaseException as primary:
        try:
            journal.abandon()
        except BaseException as abandonment:
            primary.add_note(
                f"Container input abandonment failed: {type(abandonment).__name__}")
        try:
            transport.close()
        except BaseException as cleanup:
            primary.add_note(f"Container input cleanup failed: {type(cleanup).__name__}")
        raise


def abandon_input_claim(controller: LifecycleController, request,
                        workspace_volume: str) -> None:
    """Revoke an imported workspace when initial boundary issuance fails."""
    _InputClaimJournal(controller, _identity(request), workspace_volume).abandon()


def reopen_input_receipt(controller: LifecycleController, request, transport,
                         admission) -> InputTransferReceipt:
    """Recover a completed import; attest the new handle without restaging."""
    identity = _identity(request)
    _preflight(controller, request, transport, admission)
    return _completed_input_receipt(controller, request, transport.handle.workspace_volume)


def _completed_input_receipt(controller, request, workspace_volume):
    identity = _identity(request)
    journal = _InputClaimJournal(controller, identity,
                                 workspace_volume)
    name, digest = journal.completed()
    try:
        payload = read_evidence(controller, _RECEIPT_CATEGORY, name, digest)
        original = ContainerHandle(**payload["original_handle"])
        receipt = InputTransferReceipt(
            identity, payload["manifest_sha256"], payload["workspace_volume"],
            payload["workdir"], original, payload["handle_evidence_name"],
            payload["handle_evidence_digest"],
            payload["material_evidence_name"],
            payload["material_evidence_digest"],
            payload["grant_evidence_name"], payload["grant_evidence_digest"],
            name, digest,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ProviderRequestError("container input receipt is invalid") from error
    validate_input_receipt(controller, request, receipt,
                           workspace_volume)
    return receipt


def original_input_launch(controller, request, spec, *, broker, current_permit,
                          ledger, permit_id, epoch):
    from .broker import Broker
    from .container_broker import PermitLedger
    if not request.resume:
        raise ProviderRequestError("original input launch requires exact resume")
    if (not isinstance(broker, Broker) or broker.permit is not current_permit
            or not isinstance(ledger, PermitLedger) or ledger.controller is not controller
            or ledger.root != controller_directory(controller, "locks") / "spend"):
        raise ProviderRequestError("container recovery current broker association is invalid")
    try:
        ledger.require_permit(permit_id, epoch, current_permit)
    except BrokerRejected as error:
        raise ProviderRequestError("container recovery current permit is invalid") from error
    receipt = _completed_input_receipt(controller, request, spec.workspace_volume)
    grant = read_evidence(controller, _GRANT_CATEGORY,
                          receipt.grant_evidence_name, receipt.grant_evidence_digest)
    original = _original_permit(controller, request, grant)
    if (current_permit.route != "resume"
            or current_permit.session_id != request.session_id
            or request.session_id is None
            or any(getattr(current_permit, field) != getattr(original, field) for field in
                   ("provider", "model", "run_id", "task_id", "target_id", "seat_id",
                    "inputs_digest", "upstream_route_digest", "upstream_origin",
                    "upstream_model", "upstream_path"))):
        raise ProviderRequestError("container recovery current permit differs from original authority")
    from .container_boundary import SessionStore
    sessions = SessionStore(controller, spec.run_id, spec.task_id,
                            spec.target_id, spec.seat_id, spec.session_volume)
    if sessions.resume(request.executor_id).session_id != current_permit.session_id:
        raise ProviderRequestError("container recovery current session differs")
    launch = read_evidence(controller, "container-seat-launch",
                           receipt.handle_evidence_name, receipt.handle_evidence_digest)
    decode_launch_record(launch, spec, receipt.original_handle, recover=False)
    return launch, [receipt.evidence_name, receipt.evidence_digest]


def _decode_original_permit(controller, grant):
    from .broker import BrokerPermit
    from .container_broker import PermitLedger, _digest, _seat_scope
    try:
        raw = grant["original_permit"]
        if not isinstance(raw, dict) or set(raw) != set(BrokerPermit.__dataclass_fields__):
            raise ValueError("complete original permit")
        original = BrokerPermit(**raw)
        if (canonical_json(asdict(original)) != canonical_json(raw)
                or _digest(original) != grant["permit_digest"]
                or original.route != "initial"
                or original.provider not in {"claude", "codex", "agy"}
                or (original.provider != "claude" and original.session_id is not None)
                or (original.provider == "claude" and original.session_id is None)
                or not _sha(grant["profile_digest"])
                or not _sha(grant["permit_id"])
                or type(grant["permit_epoch"]) is not int or grant["permit_epoch"] < 1
                or original.upstream_route_digest is None):
            raise ValueError("original initial association")
    except (KeyError, TypeError, ValueError) as error:
        raise ProviderRequestError("container original permit authentication failed") from error
    ledger = PermitLedger(controller_directory(controller, "locks") / "spend", controller)
    with ledger._locked() as state:
        try:
            entry = state["permits"][grant["permit_id"]]
            if (entry["permit_digest"] != _digest(original)
                    or entry["epoch"] != grant["permit_epoch"]
                    or entry["seat_scope"] != _seat_scope(original)
                    or any(entry[field] != getattr(original, field) for field in
                           ("provider", "model", "upstream_route_digest", "upstream_origin",
                            "upstream_model", "upstream_path", "max_requests",
                            "max_response_bytes", "max_output_tokens"))):
                raise ValueError("original ledger association")
        except (KeyError, TypeError, ValueError) as error:
            raise ProviderRequestError("container original permit authentication failed") from error
    return original


def _original_permit(controller, request, grant):
    original = _decode_original_permit(controller, grant)
    try:
        if (original.provider != request.executor_id
                or original.model != request.profile.requested_model
                or grant["profile_digest"] != request.profile.digest
                or any(getattr(original, field) != getattr(request, field)
                       for field in ("run_id", "task_id", "target_id", "seat_id", "inputs_digest"))
                or original.session_id != (request.session_id if request.executor_id == "claude" else None)):
            raise ValueError("original request association")
        if request.resume:
            from .container_boundary import SessionStore
            _, session = private_volume_names(request.run_id, request.task_id,
                                              request.target_id, request.seat_id)
            sessions = SessionStore(controller, request.run_id, request.task_id,
                                    request.target_id, request.seat_id, session)
            binding = sessions.read(request.executor_id)
            if (binding is None or binding.session_id != request.session_id
                    or binding.upstream_route_digest != original.upstream_route_digest):
                raise ValueError("authenticated learned session chain")
            settled = sessions._update(lambda state: (
                state["bindings"].get(request.executor_id) == asdict(binding)
                and state["turns"].get(binding.turn_digest) ==
                    {"provider": request.executor_id, "state": "settled"}, False))
            if not settled:
                raise ValueError("authenticated original successful turn")
        return original
    except (KeyError, TypeError, ValueError) as error:
        raise ProviderRequestError("container original permit authentication failed") from error


def _quality_handoff_stopped_scope(controller, launch, original_spec, original_handle, stopped):
    if "handoff_scope" not in launch:
        return (stopped,)
    from .container_coordinator import _read_lifecycle_snapshot
    snapshot = read_evidence(controller, "container-coordinator-snapshot", *launch["handoff_scope"])
    member = read_evidence(controller, "container-coordinator-member", *snapshot["subject"])
    context = read_evidence(controller, "container-coordinator-context", *member["context"])
    if context["schema_version"] != "fanout-quality-container-context-v1":
        raise ProviderRequestError("quality handoff requires its original quality scope")
    scope, subject = _read_lifecycle_snapshot(controller, launch["handoff_scope"],
        spec=original_spec, handle=original_handle, context_ref=member["context"])
    if scope.stopped.count(stopped) or (original_spec, original_handle) not in scope.active:
        raise ProviderRequestError("quality handoff original importer scope differs")
    return (*scope.stopped, stopped)


def authenticate_launch_record(controller, launch, spec, handle, *, inputs_digest=None):
    handoff = launch.get("route") == "quality-handoff"
    recovery = decode_launch_record(launch, spec, handle, recover=launch.get("route") == "recover", handoff=handoff)
    if recovery is None:
        return None
    try:
        preparation = None
        if handoff:
            preparation = read_evidence(controller, _HANDOFF_CATEGORY, *launch["original_handoff"])
            if (preparation.get("schema") != "quality-container-handoff-preparation-v1"
                    or canonical_json(preparation.get("zero_activity")) != canonical_json(_ZERO_ACTIVITY)
                    or preparation.get("model_spec") != asdict(spec)
                    or preparation["model_spec"]["workspace_readonly"] is not True):
                raise ValueError("handoff preparation")
        name, digest = preparation["input_receipt"] if handoff else launch["original_input"]
        payload = read_evidence(controller, _RECEIPT_CATEGORY, name, digest)
        identity = TransferIdentity(**payload["identity"])
        receipt = InputTransferReceipt(
            identity, payload["manifest_sha256"], payload["workspace_volume"],
            payload["workdir"], ContainerHandle(**payload["original_handle"]),
            payload["handle_evidence_name"], payload["handle_evidence_digest"],
            payload["material_evidence_name"], payload["material_evidence_digest"],
            payload["grant_evidence_name"], payload["grant_evidence_digest"], name, digest)
        if (payload != _input_payload(receipt)
                or (identity.run_id, identity.task_id, identity.target_id, identity.seat_id) !=
                   (spec.run_id, spec.task_id, spec.target_id, spec.seat_id)
                or inputs_digest is not None and identity.inputs_digest != inputs_digest
                or receipt.workspace_volume != spec.workspace_volume
                or receipt.workdir != _workdir(identity)
                or _InputClaimJournal(controller, identity, spec.workspace_volume).completed() != (name, digest)):
            raise ValueError("exact completed original import")
        grant = read_evidence(controller, _GRANT_CATEGORY,
                              receipt.grant_evidence_name, receipt.grant_evidence_digest)
        original = _decode_original_permit(controller, grant)
        if (grant["identity"] != asdict(identity)
                or any(getattr(original, field) != getattr(identity, field) for field in
                       ("run_id", "task_id", "target_id", "seat_id", "inputs_digest"))
                or grant["original_handle"] != asdict(receipt.original_handle)
                or (grant["handle_evidence_name"], grant["handle_evidence_digest"]) !=
                   (receipt.handle_evidence_name, receipt.handle_evidence_digest)
                or (grant["material_evidence_name"], grant["material_evidence_digest"]) !=
                   (receipt.material_evidence_name, receipt.material_evidence_digest)
                or (grant["image_digest"], grant["cli_digest"]) != (spec.image_digest, spec.cli_digest)):
            raise ValueError("original grant association")
        origin = read_evidence(controller, "container-seat-launch",
                               receipt.handle_evidence_name, receipt.handle_evidence_digest)
        quality_recovery = not handoff and spec.workspace_readonly is True and "quality_provisioning" in grant
        if quality_recovery:
            context = grant["quality_provisioning"]
            fields = set(QualityProvisioning.__dataclass_fields__)
            if (set(context) != fields or context["backend_attachment"] is None
                    or context["phase"] != "initial" or context["condition"] != "candidate"):
                raise ValueError("quality recovery original context")
            from .container_certification import _read_owned_json
            pointer, _ = _read_owned_json(_handoff_pointer(controller, identity))
            prior = read_evidence(controller, _HANDOFF_CATEGORY, *pointer["preparation"])
            if (prior["schema"] != "quality-container-handoff-preparation-v1"
                    or prior["model_spec"] != asdict(spec) or prior["model_spec"]["workspace_readonly"] is not True
                    or prior["input_receipt"] != [name, digest]
                    or canonical_json(prior["zero_activity"]) != canonical_json(_ZERO_ACTIVITY)):
                raise ValueError("quality recovery original handoff")
        original_spec = replace(spec, workspace_readonly=False) if handoff or quality_recovery else spec
        decode_launch_record(origin, original_spec, receipt.original_handle, recover=False)
        if handoff:
            from .container_runtime import ContainerLauncher, _json_one
            from .container_certification import _read_owned_json
            pointer, _ = _read_owned_json(_handoff_pointer(controller, identity))
            if (pointer != {"preparation": launch["original_handoff"]}
                    or preparation["provisioning_spec"] != asdict(original_spec)
                    or preparation["provisioning_spec"]["workspace_readonly"] is not False
                    or "quality_provisioning" not in grant):
                raise ValueError("handoff original policy")
            reopen_stopped_seat(controller, preparation["stop"], spec=original_spec, handle=receipt.original_handle)
            stopped = read_evidence(controller, "container-seat-stopped", *preparation["stop"])
            if (stopped["launch_evidence_name"], stopped["launch_evidence_digest"]) != (
                    receipt.handle_evidence_name, receipt.handle_evidence_digest):
                raise ValueError("handoff stopped launch")
            with ArtifactStore.open_existing(controller_directory(controller, "quarantine") / "runtime-proofs") as store:
                item = _json_one(store.read_bytes(ArtifactRef(**stopped["raw_inspect"])), "handoff stopped inspect")
            observation = ContainerLauncher._stopped_observation(receipt.original_handle.container_id, original_spec, item)
            if recovery.predecessors != _quality_handoff_stopped_scope(controller, launch, original_spec,
                    receipt.original_handle, (receipt.original_handle.container_id, observation)):
                raise ValueError("handoff stopped observation")
        if launch["volumes"] != origin["volumes"]:
            raise ValueError("original creation metadata")
        source = Path(grant["source_root"])
        info = _root_info(controller, source)
        if (info.st_dev, info.st_ino) != (grant["source_device"], grant["source_inode"]):
            raise ValueError("original source root")
        manifest, _ = _material(controller, receipt.material_evidence_name,
                                receipt.material_evidence_digest, identity, receipt.original_handle, info)
        if (snapshot_manifest(source, identity) != manifest
                or hashlib.sha256(encode_manifest(manifest)).hexdigest() != receipt.manifest_sha256):
            raise ValueError("original complete material")
        return recovery
    except BrokerRejected:
        raise
    except (OSError, KeyError, TypeError, ValueError, TransferRejected) as error:
        raise ProviderRequestError("container original launch authentication failed") from error


def validate_input_receipt(controller: LifecycleController, request,
                           receipt: InputTransferReceipt,
                           workspace_volume: str) -> None:
    if not isinstance(receipt, InputTransferReceipt):
        raise ProviderRequestError("container input receipt is absent")
    identity = _identity(request)
    if (receipt.identity != identity or receipt.workspace_volume != workspace_volume
            or receipt.workdir != _workdir(identity)
            or not _sha(receipt.manifest_sha256)):
        raise ProviderRequestError("container input receipt association differs")
    journal = _InputClaimJournal(controller, identity, workspace_volume)
    if journal.completed() != (receipt.evidence_name, receipt.evidence_digest):
        raise ProviderRequestError("container input claim receipt changed")
    try:
        payload = read_evidence(controller, _RECEIPT_CATEGORY,
                                receipt.evidence_name, receipt.evidence_digest)
        if payload != _input_payload(receipt):
            raise ValueError("receipt payload")
        origin = read_evidence(controller, "container-seat-launch",
                               receipt.handle_evidence_name,
                               receipt.handle_evidence_digest)
        if (origin.get("handle") != asdict(receipt.original_handle)
                or origin.get("route") not in {"start", "recover"}
                or origin.get("spec", {}).get("workspace_volume") != workspace_volume):
            raise ValueError("original handle provenance")
        grant = read_evidence(controller, _GRANT_CATEGORY,
                              receipt.grant_evidence_name,
                              receipt.grant_evidence_digest)
        _original_permit(controller, request, grant)
        if (grant.get("identity") != asdict(identity)
                or grant.get("original_handle") != asdict(receipt.original_handle)
                or grant.get("material_evidence_name") != receipt.material_evidence_name
                or grant.get("material_evidence_digest") != receipt.material_evidence_digest
                or (not request.resume and
                    grant.get("request_digest") != admitted_request_digest(request))):
            raise ValueError("grant association")
        original_spec = ContainerSpec(
            identity.run_id, identity.task_id, identity.target_id, identity.seat_id,
            grant["image_digest"], receipt.original_handle.workspace_volume,
            receipt.original_handle.session_volume, f"/run/fanout/{identity.seat_id}.sock",
            grant["cli_digest"])
        decode_launch_record(origin, original_spec, receipt.original_handle, recover=False)
        source = Path(grant["source_root"])
        info = _root_info(controller, source)
        if (info.st_dev, info.st_ino) != (grant["source_device"], grant["source_inode"]):
            raise ValueError("source root changed")
        manifest, _ = _material(controller, receipt.material_evidence_name,
                                receipt.material_evidence_digest, identity,
                                receipt.original_handle, info, request)
        if snapshot_manifest(source, identity) != manifest:
            raise ValueError("source tree changed")
        if hashlib.sha256(encode_manifest(manifest)).hexdigest() != receipt.manifest_sha256:
            raise ValueError("manifest digest")
    except BrokerRejected:
        raise
    except (OSError, KeyError, TypeError, ValueError, TransferRejected) as error:
        raise ProviderRequestError("container input receipt authentication failed") from error


def export_candidate(request, boundary, private_root: Path) -> CandidateExportReceipt:
    """Export a parsed turn into a fresh host-only quarantine before stop."""
    from .container_boundary import (
        ContainerSeatBoundary, PinnedDockerTransport, PinnedSSHSeatControl,
        _check_live,
        _turn_cleanup_authorized, _turn_digest,
    )
    if not isinstance(boundary, ContainerSeatBoundary):
        raise ProviderRequestError("container export boundary is invalid")
    if not _turn_cleanup_authorized(request, boundary):
        raise ProviderRequestError("container export boundary seal differs")
    transport = boundary.admission.transport
    if type(transport) is not PinnedDockerTransport:
        raise ProviderRequestError("container export needs retained production transport")
    control = getattr(transport, "_control", None)
    if getattr(transport, "_production_edge", False) and control is None:
        raise ProviderRequestError("container export production control is unavailable")
    if control is not None and (
            type(control) is not PinnedSSHSeatControl
            or control.controller is not boundary.controller
            or control.permit_id != boundary.permit_id
            or control.epoch != boundary.permit_epoch
            or control._lease is None or control._lease._fd is None):
        raise ProviderRequestError("container export live control lease is unavailable")
    validate_input_receipt(boundary.controller, request,
                           boundary.admission.input_receipt,
                           boundary.workspace_volume)
    boundary.session_store.require_uncertain(
        request.executor_id, _turn_digest(request, boundary))
    try:
        boundary.admission.ledger.require_permit(
            boundary.permit_id, boundary.permit_epoch,
            boundary.admission.broker_permit)
    except Exception as error:
        raise ProviderRequestError("container export permit changed") from error
    _check_live(boundary)
    _root_info(boundary.controller, private_root, empty=True)
    identity = _identity(request)
    raw_identity = canonical_json(asdict(identity))
    if len(raw_identity) > 8192:
        raise ProviderRequestError("container export identity exceeds limit")
    request_wire = _MAGIC + len(raw_identity).to_bytes(4, "big") + raw_identity
    with tempfile.TemporaryFile(dir=private_root) as output:
        stderr, status = _run_transfer(transport, "export", (request_wire,),
                                       output, _MAX_EXPORT_STREAM)
        if status != 0 or stderr:
            raise ProviderRequestError("container export child status differs")
        output.seek(0)
        try:
            if _read_exact(output, len(_MAGIC)) != _MAGIC:
                raise TransferRejected("export magic")
            wire = _read_exact(output, _read_length(output, _MAX_MANIFEST_WIRE))
            manifest = _decode_manifest(wire, identity)
            published = stage_import(Path(private_root), identity, wire,
                                     _import_content(output, manifest))
            if snapshot_manifest(published, identity) != manifest:
                raise TransferRejected("exported tree changed")
        except TransferRejected as error:
            raise ProviderRequestError("container export stream is invalid") from error
    digest = hashlib.sha256(canonical_json(asdict(manifest))).hexdigest()
    payload = {
        "identity": asdict(identity), "turn_digest": _turn_digest(request, boundary),
        "manifest_sha256": digest, "root": str(published),
        "container_id": boundary.container_id,
        "container_inspect_sha256": boundary.container_inspect_sha256,
        "workspace_volume": boundary.workspace_volume,
        "session_volume": boundary.session_volume,
    }
    name, evidence_digest = write_evidence(boundary.controller, _EXPORT_CATEGORY,
                                           payload)
    return CandidateExportReceipt(
        identity, payload["turn_digest"], digest, published,
        boundary.container_id, boundary.container_inspect_sha256,
        boundary.workspace_volume, boundary.session_volume,
        name, evidence_digest,
    )


def reopen_candidate_export(controller: LifecycleController,
                            receipt: CandidateExportReceipt, *,
                            identity: TransferIdentity, turn_digest: str,
                            stop_evidence: tuple[str, str]) -> CandidateExportReceipt:
    """Reopen host-only export and stopped-seat proof without a live transport."""
    from .container_runtime import ContainerLauncher, ContainerSpec
    try:
        assert_controller(controller)
        if type(receipt) is not CandidateExportReceipt or not _sha(turn_digest):
            raise ValueError("export type")
        value = read_evidence(controller, _EXPORT_CATEGORY,
                              receipt.evidence_name, receipt.evidence_digest)
        expected = asdict(receipt)
        expected.pop("evidence_name")
        expected.pop("evidence_digest")
        expected["root"] = str(receipt.root)
        if (value != expected or receipt.identity != identity
                or receipt.turn_digest != turn_digest):
            raise ValueError("export association")
        root = Path(receipt.root)
        if root.name != "published":
            raise ValueError("export root")
        _root_info(controller, root.parent)
        root_info = os.lstat(root)
        if (not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode)
                or root_info.st_uid != os.getuid()
                or stat.S_IMODE(root_info.st_mode) != 0o700):
            raise ValueError("export root ownership")
        manifest = snapshot_manifest(root, identity)
        if hashlib.sha256(canonical_json(asdict(manifest))).hexdigest() != receipt.manifest_sha256:
            raise ValueError("export manifest")
        stopped = read_evidence(controller, "container-seat-stopped", *stop_evidence)
        if set(stopped) != {"spec", "handle", "launch_evidence_name",
                            "launch_evidence_digest", "raw_inspect"}:
            raise ValueError("stopped evidence")
        spec, handle = ContainerSpec(**stopped["spec"]), ContainerHandle(**stopped["handle"])
        launch = read_evidence(controller, "container-seat-launch",
            stopped["launch_evidence_name"], stopped["launch_evidence_digest"])
        authenticate_launch_record(controller, launch, spec, handle, inputs_digest=identity.inputs_digest)
        if ((spec.run_id, spec.task_id, spec.target_id, spec.seat_id) !=
                   (identity.run_id, identity.task_id, identity.target_id, identity.seat_id)
                or (handle.container_id, handle.inspect_sha256, handle.workspace_volume,
                    handle.session_volume) != (receipt.container_id,
                    receipt.container_inspect_sha256, receipt.workspace_volume, receipt.session_volume)
                or (spec.workspace_volume, spec.session_volume) !=
                   private_volume_names(identity.run_id, identity.task_id, identity.target_id, identity.seat_id)):
            raise ValueError("stopped association")
        reopen_stopped_seat(controller, stop_evidence, spec=spec, handle=handle)
        return receipt
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ArtifactError,
            LifecycleError, TransferRejected) as error:
        raise ProviderRequestError("container candidate export authentication failed") from error


def reopen_stopped_seat(controller, stop_evidence, *, spec, handle):
    """Reopen the existing exact stopped observation; never execute Docker."""
    from .container_runtime import ContainerLauncher
    ContainerLauncher._validate_spec(spec)
    try:
        stopped = read_evidence(controller, "container-seat-stopped", *stop_evidence)
        if (set(stopped) != {"spec", "handle", "launch_evidence_name", "launch_evidence_digest", "raw_inspect"}
                or not isinstance(stopped["spec"], dict)
                or type(stopped["spec"].get("workspace_readonly")) is not bool
                or stopped["spec"] != asdict(spec) or stopped["handle"] != asdict(handle)
                or (spec.workspace_volume, spec.session_volume) !=
                   private_volume_names(spec.run_id, spec.task_id, spec.target_id, spec.seat_id)):
            raise ValueError("exact stopped seat")
        launch = read_evidence(controller, "container-seat-launch", stopped["launch_evidence_name"], stopped["launch_evidence_digest"])
        authenticate_launch_record(controller, launch, spec, handle)
        ref = ArtifactRef(**stopped["raw_inspect"])
        if ref.path != f"stopped/{handle.container_id}.json":
            raise ValueError("stopped artifact path")
        with ArtifactStore.open_existing(controller_directory(controller, "quarantine") /
                                         "runtime-proofs") as store:
            raw = store.read_bytes(ref)
        items = json.loads(raw)
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            raise ValueError("stopped inspect")
        item = items[0]
        mounts = {entry["Destination"]: entry for entry in item["Mounts"]}
        host_mounts = item["HostConfig"]["Mounts"]
        by_target = {entry["Target"]: entry for entry in host_mounts}
        wanted = {"/workspace": ("volume", spec.workspace_volume, not spec.workspace_readonly),
                  "/session": ("volume", spec.session_volume, True),
                  "/run/fanout/broker.sock": ("bind", spec.socket_path, False)}
        if (item["Id"] != handle.container_id or item["State"]["Running"] is not False
                or item["HostConfig"]["RestartPolicy"]["Name"] != "no"
                or item["Config"]["Image"] != spec.image_digest
                or item["Config"]["Labels"] != ContainerLauncher._labels(spec)
                or len(item["Mounts"]) != len(wanted) or set(mounts) != set(wanted)
                or len(host_mounts) != len(wanted) or set(by_target) != set(wanted)
                or any(mounts[path]["Type"] != kind or
                       mounts[path]["Name" if kind == "volume" else "Source"] != source
                       or mounts[path].get("RW") is not writable
                       or by_target[path].get("Type") != kind
                       or by_target[path].get("Source") != source
                       or by_target[path].get("ReadOnly", False) is not (not writable)
                       for path, (kind, source, writable) in wanted.items())):
            raise ValueError("stopped state or ownership")
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ArtifactError,
            LifecycleError, TransferRejected) as error:
        raise ProviderRequestError("container stopped-seat authentication failed") from error
