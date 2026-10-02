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
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import BinaryIO, Iterable, Iterator

from .artifacts import ArtifactLimits, ArtifactRef, ArtifactStore, canonical_json
from .container_runtime import ContainerHandle, DOCKER, private_volume_names
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
              source_info, request) -> tuple[TransferManifest, ArtifactRef]:
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
                or (not request.resume and
                    value["provider_context_sha256"] != request.context_sha256)
                or value["skill_bundle_sha256"] != request.skill_bundle_sha256
                or value["skill_delivery_sha256"] != request.skill_delivery_sha256
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
    _check_association(request, admission)
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
    }
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
            or payload.get("permit_digest") != hashlib.sha256(
                canonical_json(asdict(admission.broker_permit))).hexdigest()
            or payload.get("socket") != asdict(admission.socket_attestation)
            or payload.get("image_digest") != admission.image_digest
            or payload.get("cli_digest") != admission.cli_digest):
        raise ProviderRequestError("container source grant association differs")
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
    journal = _InputClaimJournal(controller, identity,
                                 transport.handle.workspace_volume)
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
                           transport.handle.workspace_volume)
    return receipt


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
        if (grant.get("identity") != asdict(identity)
                or grant.get("original_handle") != asdict(receipt.original_handle)
                or grant.get("material_evidence_name") != receipt.material_evidence_name
                or grant.get("material_evidence_digest") != receipt.material_evidence_digest
                or (not request.resume and
                    grant.get("request_digest") != admitted_request_digest(request))):
            raise ValueError("grant association")
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
        if (launch != {"spec": asdict(spec), "handle": asdict(handle), "route": launch.get("route")}
                or launch["route"] not in {"start", "recover"}
                or (spec.run_id, spec.task_id, spec.target_id, spec.seat_id) !=
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
    try:
        stopped = read_evidence(controller, "container-seat-stopped", *stop_evidence)
        if (set(stopped) != {"spec", "handle", "launch_evidence_name", "launch_evidence_digest", "raw_inspect"}
                or stopped["spec"] != asdict(spec) or stopped["handle"] != asdict(handle)
                or (spec.workspace_volume, spec.session_volume) !=
                   private_volume_names(spec.run_id, spec.task_id, spec.target_id, spec.seat_id)):
            raise ValueError("exact stopped seat")
        launch = read_evidence(controller, "container-seat-launch", stopped["launch_evidence_name"], stopped["launch_evidence_digest"])
        if launch not in ({"spec": asdict(spec), "handle": asdict(handle), "route": "start"},
                          {"spec": asdict(spec), "handle": asdict(handle), "route": "recover"}):
            raise ValueError("exact stopped launch")
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
        wanted = {"/workspace": ("volume", spec.workspace_volume),
                  "/session": ("volume", spec.session_volume),
                  "/run/fanout/broker.sock": ("bind", spec.socket_path)}
        if (item["Id"] != handle.container_id or item["State"]["Running"] is not False
                or item["HostConfig"]["RestartPolicy"]["Name"] != "no"
                or item["Config"]["Image"] != spec.image_digest
                or item["Config"]["Labels"] != ContainerLauncher._labels(spec)
                or len(item["Mounts"]) != len(wanted) or set(mounts) != set(wanted)
                or any(mounts[path]["Type"] != kind or
                       mounts[path]["Name" if kind == "volume" else "Source"] != source
                       for path, (kind, source) in wanted.items())):
            raise ValueError("stopped state or ownership")
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ArtifactError,
            LifecycleError, TransferRejected) as error:
        raise ProviderRequestError("container stopped-seat authentication failed") from error
