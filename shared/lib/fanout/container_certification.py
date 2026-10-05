"""Shared private evidence admission and separate owner certificate gates."""
from __future__ import annotations

import hashlib
import base64
import json
import hmac
import os
import stat
import tempfile
import time
from dataclasses import asdict
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

from .artifacts import ArtifactRef, ArtifactStore, canonical_json
from .container_routes import MachineRoutes, read_machine_routes
from .container_routes import _open_parent, _read_owned_json
from .container_source import ReleaseSourceBinding, release_source_binding
from .controller import LifecycleController, _mac, read_evidence
from .certification import CertificationDecision


TRIAL_CHECKS = ("two_repository", "peer_exchange", "candidate_verification", "partial_failure", "cold_recovery")
RELEASE_CHECKS = ("maka", "standard_canary", "deep_canary", "skill_evaluations", "strict_gate")
_TRIAL_SCHEMA = "fanout-container-trial-certificate-v1"
_RELEASE_SCHEMA = "fanout-container-release-certificate-v1"


class CertificationRejected(ValueError):
    """Required owner-held certification evidence could not be authenticated."""


@dataclass(frozen=True, slots=True)
class PrivateProbeDecision:
    passed: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TrialCertificate:
    payload: bytes = field(repr=False)
    mac: str = field(repr=False)

    def to_dict(self) -> dict[str, object]:
        return {"schema_version": _TRIAL_SCHEMA, "payload": json.loads(self.payload), "mac": self.mac}


@dataclass(frozen=True, slots=True)
class ReleaseCertificate:
    payload: bytes = field(repr=False)
    mac: str = field(repr=False)

    def to_dict(self) -> dict[str, object]:
        return {"schema_version": _RELEASE_SCHEMA, "payload": json.loads(self.payload), "mac": self.mac}


def read_certificate(path: Path) -> TrialCertificate | ReleaseCertificate:
    try:
        value, raw = _read_owned_json(Path(path))
        if canonical_json(value) != raw:
            raise ValueError("certificate canonical bytes")
        return _certificate(value)
    except Exception:
        raise CertificationRejected("owner-held certificate is unavailable") from None


def _certificate(value: object) -> TrialCertificate | ReleaseCertificate:
    if (not isinstance(value, dict) or set(value) != {"schema_version", "payload", "mac"}
            or not isinstance(value["payload"], dict) or not isinstance(value["mac"], str)):
        raise CertificationRejected("certificate is malformed")
    kind = {_TRIAL_SCHEMA: TrialCertificate, _RELEASE_SCHEMA: ReleaseCertificate}.get(value["schema_version"])
    if kind is None:
        raise CertificationRejected("certificate type is unsupported")
    return kind(canonical_json(value["payload"]), value["mac"])


@dataclass(frozen=True, slots=True, init=False)
class AuthenticatedPrivateEvidence:
    controller_id: str
    receipt_sha256: str
    route_digests: tuple[tuple[str, str], ...]
    source_binding: ReleaseSourceBinding
    attestations_complete: bool
    _receipt: bytes = field(repr=False)

    @property
    def receipt(self) -> dict[str, object]:
        import json
        return json.loads(self._receipt)

    def summary(self) -> dict[str, object]:
        return {"controller_id": self.controller_id,
                "receipt_sha256": self.receipt_sha256,
                "route_digests": dict(self.route_digests),
                "release_source": self.source_binding.to_dict(),
                "attestations_complete": self.attestations_complete}


def read_private_route_evidence(private_root: Path, manifest: MachineRoutes | None = None,
                                *, require_attestations: bool = False
                                ) -> AuthenticatedPrivateEvidence:
    """Extend the established authenticated route/ledger reader, never its codec."""
    from scripts import fanout_container_certify as probe
    if manifest is None:
        receipt = probe._read_private_receipt(Path(private_root))
        manifest = read_machine_routes(Path(receipt["manifest_path"]),
                                       Path(receipt["policy_path"]))
    receipt, controller = probe._open_private_route_authority(private_root, manifest)
    if require_attestations:
        _read_cell_attestations(controller, receipt)
    raw = canonical_json(receipt)
    result = object.__new__(AuthenticatedPrivateEvidence)
    for name, value in {
        "controller_id": controller.controller_id,
        "receipt_sha256": hashlib.sha256(raw).hexdigest(),
        "route_digests": tuple(sorted((provider, route["digest"])
                                     for provider, route in receipt["machine_routes"].items())),
        "source_binding": release_source_binding(),
        "attestations_complete": require_attestations,
        "_receipt": raw,
    }.items():
        object.__setattr__(result, name, value)
    return result


def reopen_quality_wire(store, captured, permit, record, route):
    """Reopen complete quality request/response bytes without synthetic marker policy."""
    from .container_broker import OutboundRequestIdentity
    from .container_upstream import HostUpstream
    from .canary_proof import served_response_identity
    from .diagnostic import _quality_empty_tools
    try:
        if (set(captured) != {"request_id", "http_status", "content_type", "normalized_body",
                             "effective_body", "response_body", "delivered_body"}
                or captured["request_id"] != record["request_id"]
                or record["state"] != "settled"
                or type(captured["http_status"]) is not int
                or not 200 <= captured["http_status"] < 300
                or captured["content_type"] not in {"application/json", "text/event-stream"}):
            raise ValueError("identity/status")
        normal = _artifact(store, captured["normalized_body"])
        effective = _artifact(store, captured["effective_body"])
        response = _artifact(store, captured["response_body"])
        delivered = _artifact(store, captured["delivered_body"])
        if any(len(body) > permit.max_response_bytes for body in (response, delivered)):
            raise ValueError("response budget")
        sender = HostUpstream(permit, route, max_response_bytes=permit.max_response_bytes,
                              deadline_seconds=permit.response_deadline)
        outbound = record["identity"]
        prepared = sender.preview("POST", outbound["path"], {"content-type": "application/json"}, normal)
        _quality_empty_tools(normal); _quality_empty_tools(effective)
        document = json.loads(normal)
        limit = (document["max_tokens"] if permit.provider == "claude" else
                 document["max_output_tokens"] if permit.provider == "codex" else
                 document["generationConfig"]["maxOutputTokens"])
        wire_model = document["model"] if permit.provider != "agy" else route.profile.wire_model
        identity = OutboundRequestIdentity("POST", outbound["path"], hashlib.sha256(normal).hexdigest(),
            limit, wire_model, prepared.route_digest, prepared.origin, prepared.path,
            hashlib.sha256(prepared.body).hexdigest(), prepared.effective_model)
        if (asdict(identity) != outbound or effective != prepared.body
                or hashlib.sha256(delivered).hexdigest() != record["response_digest"]):
            raise ValueError("actual wire bytes")
        original = served_response_identity(permit.provider, permit.model, permit.upstream_model,
                                             response, captured["content_type"])
        if original != served_response_identity(permit.provider, permit.model, permit.upstream_model,
                                                 delivered, captured["content_type"]):
            raise ValueError("served identity")
        return original
    except Exception as error:
        raise CertificationRejected("complete quality wire proof differs") from error


def _read_cell_attestations(controller: LifecycleController, receipt: dict) -> None:
    from scripts import fanout_container_certify as probe
    from .broker import BrokerPermit, _safe_response_body, _safe_stream_body
    from .container_boundary import PinnedDockerTransport, _turn_digest
    from .container_broker import OutboundRequestIdentity, PermitLedger, _digest
    from .container_runtime import (ContainerHandle, ContainerLauncher, ContainerSpec,
                                    GuestSocketAttestation, RuntimeTranscript, decode_launch_record)
    from .container_transfer import TransferIdentity
    from .container_transfer_transport import (
        CandidateExportReceipt, InputTransferReceipt, admitted_request_digest,
        validate_input_receipt, authenticate_launch_record,
    )
    from .container_upstream import HostUpstream
    from .process import ProcessResult, ProcessStatus
    from .providers import ProviderRequest, ProviderRegistry, _make_result
    from .repo import capture_repository_baseline
    from .canary_proof import served_response_identity
    try:
        binding = release_source_binding()
        if ReleaseSourceBinding.from_dict(receipt.get("release_source_binding")) != binding:
            raise ValueError("release binding")
        manifest = read_machine_routes(Path(receipt["manifest_path"]), Path(receipt["policy_path"]))
        _, selected = probe._admit_route_set(Path(receipt["manifest_path"]), Path(receipt["policy_path"]),
            {provider: route["route_id"] for provider, route in receipt["machine_routes"].items()})
        registry = ProviderRegistry.default(version_probe=lambda _: "unused")
        raw_root = Path(receipt["private_run_root"]) / "raw"
        with ArtifactStore.open_existing(raw_root) as store:
            for provider in probe.PROVIDERS:
                credential_source = probe.HostCredentialSource(selected[provider].profile)
                credential = credential_source.load()
                if credential_source.fingerprint != receipt["providers"][provider]["initial"]["credential_source_fingerprint"]:
                    raise ValueError("cell host credential changed")
                initial_vm = None
                for route in probe.ROUTES:
                    cell = receipt["providers"][provider][route]
                    value = read_evidence(controller, "container-private-cell-attestation",
                        cell.get("attestation_evidence_name"), cell.get("attestation_evidence_digest"))
                    if (value["schema_version"] != "fanout-private-cell-attestation-v1"
                            or value["release_source_binding"] != binding.to_dict()
                            or value["host_runtime"] != receipt["host_runtime"]):
                        raise ValueError("cell source/runtime")
                    profile = registry.select(provider, "repo-write", "standard")
                    request = ProviderRequest(**value["request"], profile=profile)
                    permit = BrokerPermit(**value["permit"])
                    boundary = read_evidence(controller, "container-seat-admission",
                        value["boundary_evidence_name"], value["boundary_evidence_digest"])
                    for name, expected in {
                        "controller_id": controller.controller_id, "run_id": request.run_id,
                        "task_id": request.task_id, "target_id": request.target_id, "seat_id": request.seat_id,
                        "provider": provider, "inputs_digest": request.inputs_digest,
                        "profile_digest": profile.digest, "request_digest": admitted_request_digest(request),
                        "permit_id": cell["permit_id"], "permit_epoch": cell["permit_epoch"],
                        "permit_digest": _digest(permit), "route": route,
                        "upstream_route_digest": selected[provider].digest,
                        "image_digest": cell["image_digest"], "cli_digest": cell["cli_digest"],
                        "container_id": cell["container_id"], "container_inspect_sha256": cell["container_inspect_sha256"],
                        "session_volume": cell["session_volume"], "input_receipt_digest": cell["input_receipt_digest"],
                        "input_manifest_sha256": cell["input_manifest_sha256"],
                    }.items():
                        if boundary[name] != expected:
                            raise ValueError("cell admission")
                    with PermitLedger(controller.root / "locks" / "spend", controller)._locked() as ledger:
                        if ledger["permits"][cell["permit_id"]]["permit_digest"] != _digest(permit):
                            raise ValueError("cell complete permit")
                    if (request.executor_id != provider or request.resume != (route == "resume")
                            or permit.provider != provider or permit.route != route
                            or permit.model != profile.requested_model or permit.inputs_digest != request.inputs_digest
                            or any(getattr(permit, name) != getattr(request, name)
                                   for name in ("run_id", "task_id", "target_id", "seat_id", "session_id"))):
                        raise ValueError("cell request/permit")
                    launch = read_evidence(controller, "container-seat-launch",
                        boundary["handle_evidence_name"], boundary["handle_evidence_digest"])
                    spec, handle = ContainerSpec(**launch["spec"]), ContainerHandle(**launch["handle"])
                    recovery = decode_launch_record(launch, spec, handle, recover=request.resume)
                    authenticate_launch_record(controller, launch, spec, handle, inputs_digest=request.inputs_digest)
                    if (handle.container_id != cell["container_id"] or handle.inspect_sha256 != cell["container_inspect_sha256"]
                            or any(getattr(spec, name) != getattr(request, name) for name in ("run_id", "task_id", "target_id", "seat_id"))
                            or spec.image_digest != cell["image_digest"] or spec.cli_digest != cell["cli_digest"]
                            or spec.session_volume != cell["session_volume"] or spec.workspace_volume != boundary["workspace_volume"]
                            or launch["route"] != ("recover" if request.resume else "start")):
                        raise ValueError("cell launch")
                    socket = GuestSocketAttestation(**{name: boundary["socket_" + name]
                        for name in ("path", "uid", "mode", "device", "inode", "ctime_ns")})
                    if hashlib.sha256(canonical_json(asdict(socket))).hexdigest() != cell["socket_attestation_sha256"]:
                        raise ValueError("cell socket")
                    vm_observation = _artifact_json(store, value["vm"])
                    vm = _reopen_vm(vm_observation)
                    stable_vm = (vm.profile, vm.docker_endpoint, vm.ssh_host_key_sha256,
                                 vm.effective_mounts_sha256, vm.guest_mounts_sha256,
                                 vm_observation["profile_identity"])
                    if initial_vm is not None and initial_vm != stable_vm:
                        raise ValueError("initial/resume stable VM identity differs")
                    initial_vm = stable_vm
                    if (vm.digest != cell["vm_probe_sha256"] or boundary["vm_probe_digest"] != vm.digest
                            or any(boundary[name] != getattr(vm, field) for name, field in (
                                ("vm_docker_endpoint", "docker_endpoint"), ("vm_ssh_host_key_sha256", "ssh_host_key_sha256"),
                                ("vm_effective_mounts_sha256", "effective_mounts_sha256"), ("vm_guest_mounts_sha256", "guest_mounts_sha256")))):
                        raise ValueError("cell VM")
                    observations = RuntimeTranscript(_artifact_json(store, value["runtime"]))
                    launcher = ContainerLauncher(guest_socket_attestor=lambda _: socket)
                    launcher.restore_launch(launch, spec, handle, recover=request.resume)
                    launcher._run = observations.replay
                    if launcher.attest_running(spec, handle) != handle:
                        raise ValueError("cell running container")
                    observations.require_complete()
                    input_payload = read_evidence(controller, "container-input-transfer",
                        boundary["input_receipt_name"], boundary["input_receipt_digest"])
                    imported = InputTransferReceipt(
                        TransferIdentity(**input_payload["identity"]), input_payload["manifest_sha256"],
                        input_payload["workspace_volume"], input_payload["workdir"],
                        ContainerHandle(**input_payload["original_handle"]), input_payload["handle_evidence_name"],
                        input_payload["handle_evidence_digest"], input_payload["material_evidence_name"],
                        input_payload["material_evidence_digest"], input_payload["grant_evidence_name"],
                        input_payload["grant_evidence_digest"], boundary["input_receipt_name"], boundary["input_receipt_digest"])
                    validate_input_receipt(controller, request, imported, spec.workspace_volume)
                    process = value["process"]
                    stdout, stderr = _artifact(store, process["stdout"]), _artifact(store, process["stderr"])
                    if process["status"] != "exit" or process["returncode"] != 0 or process["capture_complete"] is not True:
                        raise ValueError("cell process")
                    adapter = registry.require(provider)
                    result = _make_result(request, adapter, ProcessResult(ProcessStatus.EXIT, 0, stdout, stderr),
                                          adapter.parse(stdout), 1, cell["elapsed_seconds"])
                    if (not result.valid or result.session_id != cell["session_id"]
                            or result.observed_model != cell["observed_model"]
                            or result.answer.encode() != _artifact(store, value["answer"])
                            or result.stdout_digest != cell["stdout_sha256"] or result.stderr_digest != cell["stderr_sha256"]
                            or result.answer_digest != cell["answer_sha256"]):
                        raise ValueError("cell terminal")
                    if json.loads(_artifact(store, value["isolation"])) != {
                            "isolation": cell["isolation"], "own_workspace_write": True}:
                        raise ValueError("cell isolation")
                    stopped = read_evidence(controller, "container-seat-stopped",
                        value["stop_evidence_name"], value["stop_evidence_digest"])
                    if (stopped["spec"] != asdict(spec) or stopped["handle"] != asdict(handle)
                            or stopped["launch_evidence_digest"] != boundary["handle_evidence_digest"]
                            or stopped["launch_evidence_name"] != boundary["handle_evidence_name"]):
                        raise ValueError("cell stop")
                    with ArtifactStore.open_existing(controller.root / "quarantine" / "runtime-proofs") as stops:
                        stopped_raw = _artifact(stops, stopped["raw_inspect"])
                    launcher._run = lambda *argv: stopped_raw if argv[:1] == ("inspect",) else b""
                    PinnedDockerTransport._quarantine_handle(spec, handle, launcher)
                    export_digest = cell["candidate_export_evidence_digest"]
                    export = read_evidence(controller, "container-candidate-export", export_digest + ".json", export_digest)
                    identity = TransferIdentity(**export["identity"])
                    if (identity != TransferIdentity(request.run_id, request.task_id, request.target_id, request.seat_id, request.inputs_digest)
                            or export["container_id"] != handle.container_id or export["container_inspect_sha256"] != handle.inspect_sha256
                            or export["workspace_volume"] != spec.workspace_volume or export["session_volume"] != spec.session_volume
                            or export["manifest_sha256"] != cell["candidate_export_manifest_sha256"]
                            or export["turn_digest"] != _turn_digest(request, SimpleNamespace(**boundary))
                            or export["root"] != cell["candidate_export_root"]):
                        raise ValueError("cell export")
                    export_root = Path(export["root"])
                    quarantine = controller.root / "quarantine"
                    if quarantine not in export_root.parents:
                        raise ValueError("cell export containment")
                    baseline_root = Path(value["baseline_root"])
                    if Path(receipt["private_run_root"]) not in baseline_root.parents:
                        raise ValueError("cell baseline containment")
                    baseline = capture_repository_baseline(baseline_root)
                    if baseline.digest != value["baseline_digest"]:
                        raise ValueError("cell baseline")
                    export_receipt = CandidateExportReceipt(identity, export["turn_digest"], export["manifest_sha256"],
                        export_root, handle.container_id, handle.inspect_sha256, spec.workspace_volume, spec.session_volume,
                        export_digest + ".json", export_digest)
                    marker = ((f"certified synthetic {provider} initial\n".encode() if route == "resume" else b"")
                              + f"certified synthetic {provider} {route}\n".encode())
                    if probe._checked_candidate(controller, baseline, export_receipt, marker).digest != cell["candidate_digest"]:
                        raise ValueError("cell candidate")
                    wire = value["wire"]
                    records = cell["ledger_requests"]
                    if not isinstance(wire, list) or len(wire) != len(records):
                        raise ValueError("cell wire set")
                    sender = HostUpstream(permit, selected[provider], max_response_bytes=permit.max_response_bytes, deadline_seconds=2)
                    for captured, record in zip(wire, records):
                        if captured["request_id"] != record["request_id"]:
                            raise ValueError("cell wire request identity")
                        normal, effective = _artifact(store, captured["normalized_body"]), _artifact(store, captured["effective_body"])
                        outbound = record["identity"]
                        prepared = sender.preview("POST", outbound["path"], {"content-type": "application/json"}, normal)
                        document = json.loads(normal)
                        limit = (document["max_tokens"] if provider == "claude" else document["max_output_tokens"]
                                 if provider == "codex" else document["generationConfig"]["maxOutputTokens"])
                        identity = OutboundRequestIdentity("POST", outbound["path"], hashlib.sha256(normal).hexdigest(), limit,
                            "gemini-3.8-flash" if provider == "agy" else document["model"],
                            prepared.route_digest, prepared.origin, prepared.path, hashlib.sha256(prepared.body).hexdigest(), prepared.effective_model)
                        response = _artifact(store, captured["response_body"])
                        delivered = (b"".join(_safe_stream_body((response,), credential, permit.max_response_bytes,
                                        time.monotonic() + permit.response_deadline))
                                     if captured["content_type"] == "text/event-stream" else
                                     _safe_response_body(response, captured["content_type"], credential))
                        if (len(response) > permit.max_response_bytes or len(delivered) > permit.max_response_bytes
                                or delivered != _artifact(store, captured["delivered_body"])
                                or captured["request_id"] != record["request_id"] or asdict(identity) != outbound
                                or prepared.body != effective or type(captured["http_status"]) is not int
                                or not 200 <= captured["http_status"] < 300
                                or captured["content_type"] not in {"application/json", "text/event-stream"}
                                or hashlib.sha256(delivered).hexdigest() != record["response_digest"]):
                            raise ValueError("cell wire bytes")
                        if served_response_identity(provider, permit.model, permit.upstream_model,
                                response, captured["content_type"]) != served_response_identity(
                                provider, permit.model, permit.upstream_model, delivered, captured["content_type"]):
                            raise ValueError("cell served response identity differs")
        if release_source_binding() != binding:
            raise ValueError("source changed during admission")
    except Exception:
        raise CertificationRejected("complete private cell attestations are unavailable") from None


def _artifact(store: ArtifactStore, value: dict) -> bytes:
    if not isinstance(value, dict) or set(value) != {"path", "digest", "size"}:
        raise ValueError("artifact reference")
    return store.read_bytes(ArtifactRef(**value))


def _artifact_json(store: ArtifactStore, value: dict):
    raw = _artifact(store, value)
    result = json.loads(raw)
    if canonical_json(result) != raw:
        raise ValueError("artifact canonical bytes")
    return result


def _reopen_vm(value: dict):
    from scripts.fanout_container_vm_probe import _require_profile_binding
    from .container_vm import VMProfileSpec, parse_profile_config, require_vm_probe
    observed = {name: base64.b64decode(value[name], validate=True)
                for name in ("status", "context", "info", "colima", "lima", "mounts", "nonce", "reply")}
    if any(len(raw) > 1024 * 1024 for raw in observed.values()) or not observed["info"].strip():
        raise ValueError("VM observations")
    json.loads(observed["status"])
    context = json.loads(observed["context"])
    probe = require_vm_probe(VMProfileSpec("fanout-isolated", "colima-fanout-isolated", "x86_64"),
        parse_profile_config(observed["colima"].decode(), observed["lima"].decode(), context["Name"]),
        observed["mounts"].decode().splitlines(), context["Endpoints"]["docker"]["Host"],
        observed["nonce"], observed["reply"], ssh_host_key=value["host_key"], pinned_host_key=value["host_key"])
    if asdict(probe) != value["probe"]:
        raise ValueError("VM proof differs")
    identity = value["profile_identity"]
    _require_profile_binding(value["profile_binding"], identity)
    if (len(canonical_json(identity)) > 32768 or identity.get("schema") != 2
            or identity["profile"] != probe.profile or identity["context"] != context["Name"]
            or identity["endpoint"] != probe.docker_endpoint or identity["ssh_host_key_sha256"] != probe.ssh_host_key_sha256
            or identity["profile_config"]["sha256"] != hashlib.sha256(observed["colima"]).hexdigest()
            or identity["lima_config"]["sha256"] != hashlib.sha256(observed["lima"]).hexdigest()):
        raise ValueError("VM owner profile identity")
    return probe


class CertificateAuthority:
    def __init__(self, trial_key: Path, release_key: Path) -> None:
        self._trial_key = Path(trial_key)
        self._release_key = Path(release_key)

    def _key(self, path: Path, forbidden: tuple[Path, ...]) -> tuple[str, tuple[int, int]]:
        from .container_source import _SOURCE_ROOT
        excluded = (Path(_SOURCE_ROOT), *(Path.home() / name for name in (".colima", ".lima", ".docker")), *forbidden)
        try:
            if (not path.is_absolute() or any(path == root or root in path.parents for root in excluded)):
                raise ValueError("host key containment")
            parent = _open_parent(path)
            try:
                fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                try:
                    info = os.fstat(fd)
                    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size != 64):
                        raise ValueError("host key ownership")
                    key = os.read(fd, 65).decode("ascii")
                    after = os.fstat(fd)
                    if (len(key) != 64 or any(char not in "0123456789abcdef" for char in key)
                            or (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                        raise ValueError("host key bytes")
                finally:
                    os.close(fd)
            finally:
                os.close(parent)
        except Exception:
            raise CertificationRejected("owner-held certificate key is unavailable") from None
        return key, (info.st_dev, info.st_ino)

    def _keys(self, forbidden: tuple[Path, ...]) -> tuple[str, str]:
        trial, trial_identity = self._key(self._trial_key, forbidden)
        release, release_identity = self._key(self._release_key, forbidden)
        if trial_identity == release_identity or hmac.compare_digest(trial, release):
            raise CertificationRejected("separate owner-held certificate keys are unavailable")
        return trial, release

    def issue_trial(self, probe_root: Path, exact_synthetic_roots: tuple[Path, ...]) -> TrialCertificate:
        proof = read_private_route_evidence(probe_root, require_attestations=True)
        roots = _prepared_root_bindings(exact_synthetic_roots)
        key, _ = self._keys((Path(probe_root), *(Path(item["path"]) for item in roots)))
        payload = _certificate_binding(proof, roots)
        raw = canonical_json(payload)
        return TrialCertificate(raw, _mac(key, _TRIAL_SCHEMA.encode() + b"\0", payload))

    def issue_release(self, probe_root: Path, caller_roots: tuple[Path, ...], *, trial_root: Path,
                      evaluation_root: Path) -> ReleaseCertificate:
        proof = read_private_route_evidence(probe_root, require_attestations=True)
        roots = _root_bindings(caller_roots, trial=False)
        _, key = self._keys((Path(probe_root), Path(trial_root), Path(evaluation_root), *(Path(item["path"]) for item in roots)))
        payload = _certificate_binding(proof, roots)
        payload["accepted_trial"] = self._accepted_trial(Path(trial_root), proof)
        payload["accepted_evaluations"] = self._accepted_evaluations(Path(evaluation_root), Path(trial_root), proof)
        return ReleaseCertificate(canonical_json(payload), _mac(key, _RELEASE_SCHEMA.encode() + b"\0", payload))

    def _accepted_trial(self, trial_root: Path, proof: AuthenticatedPrivateEvidence) -> dict[str, object]:
        from scripts import fanout_container_certify as probe
        try:
            value, raw = _read_owned_json(trial_root / "trial-result.json")
            if (canonical_json(value) != raw or value["schema_version"] != "fanout-synthetic-trial-result-v1"
                    or value["private_run_root"] != str(trial_root)
                    or value["release_source_binding"] != proof.source_binding.to_dict()):
                raise ValueError("trial source/root")
            controller = probe._quality_controller(trial_root)
            unsigned = dict(value)
            name = unsigned.pop("completion_evidence_name")
            digest = unsigned.pop("completion_evidence_digest")
            completed = read_evidence(controller, "container-synthetic-trial-complete", name, digest)
            if completed != {"trial_result_sha256": hashlib.sha256(canonical_json(unsigned)).hexdigest()}:
                raise ValueError("trial completion")
            trial = _certificate(value["trial_certificate"])
            bound = json.loads(trial.payload)
            roots = tuple(Path(root["path"]) for root in bound["roots"])
            if (any(trial_root not in root.parents for root in roots)
                    or bound["probe_sha256"] != proof.receipt_sha256):
                raise ValueError("trial certificate")
            checks = value["checks"]
            if not isinstance(checks, dict) or set(checks) != set(TRIAL_CHECKS):
                raise ValueError("trial checks")
            observed = {}
            with ArtifactStore.open_existing(trial_root / "raw") as store:
                for name in TRIAL_CHECKS:
                    item = checks[name]
                    if not isinstance(item, dict) or set(item) != {"artifact"}:
                        raise ValueError("trial proof reference")
                    observed[name] = _artifact_json(store, item["artifact"])
            from .collaboration import validate_container_trial_result
            decision = validate_container_trial_result(controller, trial_root, observed, proof.source_binding)
            if type(decision) is not CertificationDecision or not decision.passed:
                raise ValueError("trial semantic evidence")
            if not self._verify_completed_trial(trial, trial_root).passed:
                raise ValueError("completed trial certificate")
            return {"root": str(trial_root), "sha256": hashlib.sha256(raw).hexdigest(),
                    "controller_id": controller.controller_id, "completion_digest": digest}
        except Exception:
            raise CertificationRejected("accepted trial and release evidence are unavailable") from None

    def _accepted_evaluations(self, evaluation_root: Path, trial_root: Path,
                              proof: AuthenticatedPrivateEvidence) -> dict[str, object]:
        from scripts import fanout_container_certify as probe
        try:
            value, raw = _read_owned_json(evaluation_root / "release-evaluations.json")
            if (canonical_json(value) != raw or value["schema_version"] != "fanout-release-evaluations-v1"
                    or value["private_run_root"] != str(evaluation_root) or value["trial_root"] != str(trial_root)
                    or value["release_source_binding"] != proof.source_binding.to_dict()):
                raise ValueError("release evaluation source/root")
            controller = probe._quality_controller(evaluation_root)
            unsigned = dict(value)
            name, digest = unsigned.pop("completion_evidence_name"), unsigned.pop("completion_evidence_digest")
            if read_evidence(controller, "container-release-evaluations-complete", name, digest) != {
                    "release_evaluations_sha256": hashlib.sha256(canonical_json(unsigned)).hexdigest()}:
                raise ValueError("evaluation completion")
            checks = value["checks"]
            if not isinstance(checks, dict) or set(checks) != set(RELEASE_CHECKS):
                raise ValueError("release evaluation checks")
            observed = {}
            with ArtifactStore.open_existing(evaluation_root / "raw") as store:
                for name in RELEASE_CHECKS:
                    item = checks[name]
                    if not isinstance(item, dict) or set(item) != {"artifact"}:
                        raise ValueError("evaluation proof reference")
                    observed[name] = _artifact_json(store, item["artifact"])
            from scripts.fanout_release_evaluations import validate_release_evaluations
            decision = validate_release_evaluations(controller, evaluation_root, observed, proof.source_binding, trial_root)
            if type(decision) is not CertificationDecision or not decision.passed:
                raise ValueError("release semantic evidence")
            return {"root": str(evaluation_root), "sha256": hashlib.sha256(raw).hexdigest(),
                    "controller_id": controller.controller_id, "completion_digest": digest}
        except Exception:
            raise CertificationRejected("accepted release evaluation evidence is unavailable") from None

    def verify_trial(self, certificate: TrialCertificate, exact_synthetic_roots: tuple[Path, ...],
                     source_hash: str) -> CertificationDecision:
        return self._verify(certificate, exact_synthetic_roots, source_hash, trial=True)

    def verify_release(self, certificate: ReleaseCertificate, caller_roots: tuple[Path, ...],
                       source_hash: str) -> CertificationDecision:
        return self._verify(certificate, caller_roots, source_hash, trial=False)

    def _authenticated_certificate(self, certificate, source_hash: str, *, trial: bool):
        expected_type = TrialCertificate if trial else ReleaseCertificate
        domain = _TRIAL_SCHEMA if trial else _RELEASE_SCHEMA
        if type(certificate) is not expected_type or not isinstance(certificate.payload, bytes) or len(certificate.payload) > 128 * 1024:
            raise ValueError("certificate type")
        payload = json.loads(certificate.payload)
        if canonical_json(payload) != certificate.payload:
            raise ValueError("certificate canonical bytes")
        probe_root = Path(payload["probe_root"])
        forbidden = (probe_root, *(Path(root["path"]) for root in payload["roots"]))
        if not trial:
            forbidden += (Path(payload["accepted_trial"]["root"]), Path(payload["accepted_evaluations"]["root"]))
        key, _ = self._key(self._trial_key if trial else self._release_key, forbidden)
        if not isinstance(certificate.mac, str) or not hmac.compare_digest(certificate.mac, _mac(key, domain.encode() + b"\0", payload)):
            raise ValueError("certificate HMAC")
        proof = read_private_route_evidence(probe_root, require_attestations=True)
        if source_hash != proof.source_binding.digest:
            raise ValueError("current source")
        return payload, proof

    def _verify_completed_trial(self, certificate: TrialCertificate, trial_root: Path) -> CertificationDecision:
        """Accept completed CAS evidence without changing fresh launch admission."""
        from .container_source import release_source_binding
        from .container_coordinator import completed_trial_root_bindings
        try:
            payload, proof = self._authenticated_certificate(certificate, release_source_binding().digest, trial=True)
            expected = _certificate_binding(proof, completed_trial_root_bindings(trial_root))
            if payload != expected:
                raise ValueError("completed certificate bindings")
            return CertificationDecision(True, ())
        except Exception:
            return CertificationDecision(False, ("completed trial certificate is invalid",))

    def _verify(self, certificate, roots: tuple[Path, ...], source_hash: str, *, trial: bool) -> CertificationDecision:
        try:
            payload, proof = self._authenticated_certificate(certificate, source_hash, trial=trial)
            expected = _certificate_binding(proof, _prepared_root_bindings(roots) if trial else _root_bindings(roots, trial=False))
            if not trial:
                expected["accepted_trial"] = self._accepted_trial(Path(payload["accepted_trial"]["root"]), proof)
                expected["accepted_evaluations"] = self._accepted_evaluations(
                    Path(payload["accepted_evaluations"]["root"]), Path(payload["accepted_trial"]["root"]), proof)
            if payload != expected:
                raise ValueError("certificate bindings")
            return CertificationDecision(True, ())
        except Exception:
            return CertificationDecision(False, ("owner-held trial certificate is invalid" if trial else
                                                  "owner-held release certificate is invalid",))

    def admit_trial_profile(self, certificate: TrialCertificate, exact_synthetic_roots: tuple[Path, ...],
                            executor_id: str, *, source_hash: str, quality_tier: str = "standard"):
        if not self.verify_trial(certificate, exact_synthetic_roots, source_hash).passed:
            raise CertificationRejected("trial certificate does not admit these synthetic roots")
        return _admitted_profile(certificate, executor_id, quality_tier)

    def admit_release_profile(self, certificate: ReleaseCertificate, caller_roots: tuple[Path, ...],
                              executor_id: str, *, source_hash: str, quality_tier: str = "standard"):
        if not self.verify_release(certificate, caller_roots, source_hash).passed:
            raise CertificationRejected("release certificate does not admit these caller roots")
        return _admitted_profile(certificate, executor_id, quality_tier)


def _root_bindings(roots: tuple[Path, ...], *, trial: bool) -> list[dict[str, object]]:
    from .repo import capture_repository_baseline
    if not isinstance(roots, tuple) or not 1 <= len(roots) <= 32:
        raise CertificationRejected("certificate target roots are unavailable")
    result = []
    try:
        for root in roots:
            path = Path(root)
            parent = _open_parent(path / ".certificate-root-check")
            try:
                info = os.fstat(parent)
            finally:
                os.close(parent)
            if not path.is_absolute() or path.is_symlink() or info.st_uid != os.getuid():
                raise ValueError("root ownership")
            if trial:
                allowed = (Path("/private/tmp"), Path(tempfile.gettempdir()).resolve())
                if (not any(base in path.parents for base in allowed) or path.parent.name != "targets"
                        or path.name not in {"repo-alpha", "repo-beta"} or stat.S_IMODE(info.st_mode) != 0o700):
                    raise ValueError("synthetic root")
            baseline = capture_repository_baseline(path)
            result.append({"path": str(path), "device": info.st_dev, "inode": info.st_ino,
                           "baseline_sha256": baseline.digest, "head": baseline.head})
        if len({item["path"] for item in result}) != len(result):
            raise ValueError("duplicate roots")
        if trial and (len(result) != 2 or {Path(item["path"]).name for item in result} != {"repo-alpha", "repo-beta"}
                      or len({Path(item["path"]).parent for item in result}) != 1):
            raise ValueError("exact synthetic roots")
    except Exception:
        raise CertificationRejected("exact synthetic roots are unavailable" if trial else
                                    "caller target roots are unavailable") from None
    return sorted(result, key=lambda item: item["path"])


def _prepared_root_bindings(roots: tuple[Path, ...]) -> list[dict[str, object]]:
    from .container_coordinator import _read_trial_preparation
    try:
        current = _root_bindings(roots, trial=True)
        _, created = _read_trial_preparation(Path(current[0]["path"]).parent.parent)
        if current != created:
            raise ValueError("exact prepared roots differ")
        return created
    except Exception:
        raise CertificationRejected("authenticated fresh synthetic preparation is unavailable") from None


def _certificate_binding(proof: AuthenticatedPrivateEvidence, roots: list[dict]) -> dict[str, object]:
    from scripts import fanout_container_certify as probe
    receipt = proof.receipt
    controller = probe._quality_controller(Path(receipt["private_run_root"]))
    vm = {}
    for provider, cells in receipt["providers"].items():
        attested = read_evidence(controller, "container-private-cell-attestation",
            cells["initial"]["attestation_evidence_name"], cells["initial"]["attestation_evidence_digest"])
        boundary = read_evidence(controller, "container-seat-admission",
            attested["boundary_evidence_name"], attested["boundary_evidence_digest"])
        vm[provider] = {name: boundary[name] for name in (
            "vm_docker_endpoint", "vm_ssh_host_key_sha256", "vm_effective_mounts_sha256", "vm_guest_mounts_sha256")}
        with ArtifactStore.open_existing(Path(receipt["private_run_root"]) / "raw") as raw:
            vm[provider]["profile_identity"] = _artifact_json(raw, attested["vm"])["profile_identity"]
    return {"release_source_binding": proof.source_binding.to_dict(), "host_runtime": receipt["host_runtime"],
            "probe_root": receipt["private_run_root"], "probe_sha256": proof.receipt_sha256,
            "controller_id": proof.controller_id, "manifest_digest": receipt["manifest_digest"],
            "policy_digest": receipt["policy_digest"], "route_digests": dict(proof.route_digests),
            "roots": roots, "image_digest": receipt["image_digest"], "cli_digests": receipt["cli_digests"],
            "profiles": {provider: cells["initial"]["profile_digest"] for provider, cells in receipt["providers"].items()},
            "vm": vm}


def _admitted_profile(certificate, executor_id: str, quality_tier: str):
    from .providers import ProviderRegistry
    profile = ProviderRegistry.default(version_probe=lambda _: "unused").select(executor_id, "repo-write", quality_tier)
    payload = json.loads(certificate.payload)
    if (quality_tier != "standard" or payload["profiles"].get(executor_id) != profile.digest
            or executor_id not in payload["cli_digests"]):
        raise CertificationRejected("certified Linux profile differs")
    return profile


def _owner_paths() -> tuple[Path, Path, Path]:
    root = Path.home() / ".config" / "khenrix-utils" / "fanout" / "certificates"
    return root / "trial.key", root / "release.key", root / "release-certificate.json"


def _owner_authority() -> CertificateAuthority:
    trial, release, _ = _owner_paths()
    return CertificateAuthority(trial, release)


def _require_certificate_turn_binding(request, certificate, boundary, vm_profile_identity) -> None:
    from .container_boundary import _payload, _SCHEMA
    try:
        payload = json.loads(certificate.payload)
        if (not isinstance(boundary, dict) or _payload(SimpleNamespace(**boundary)) != boundary
                or boundary["schema_version"] != _SCHEMA or request.execution_class != "repo-write"
                or request.profile is None or request.profile.digest != payload["profiles"].get(request.executor_id)
                or any(boundary[name] != expected for name, expected in {
                    "run_id": request.run_id, "task_id": request.task_id, "target_id": request.target_id,
                    "seat_id": request.seat_id, "provider": request.executor_id, "inputs_digest": request.inputs_digest,
                    "profile_digest": request.profile.digest, "session_id": request.session_id,
                    "route": "resume" if request.resume else "initial",
                    "image_digest": payload["image_digest"], "cli_digest": payload["cli_digests"].get(request.executor_id),
                    "upstream_route_digest": payload["route_digests"].get(request.executor_id),
                }.items())
                or any(boundary[name] != expected for name, expected in payload["vm"][request.executor_id].items()
                       if name != "profile_identity")
                or vm_profile_identity != payload["vm"][request.executor_id]["profile_identity"]):
            raise ValueError("certificate turn binding")
    except (ValueError, TypeError, KeyError, AttributeError):
        raise CertificationRejected("certificate does not bind the complete container turn") from None


def _require_certified_boundary(request, certificate) -> None:
    from .container_boundary import (
        ContainerSeatBoundary, PinnedDockerTransport, PinnedSSHSeatControl, validate_container_boundary, _payload,
    )
    boundary = request.container_boundary
    payload = json.loads(certificate.payload)
    if (type(boundary) is not ContainerSeatBoundary or request.execution_class != "repo-write"
            or Path(request.cwd) not in request.certificate_roots
            or request.profile is None or request.profile.digest != payload["profiles"].get(request.executor_id)):
        raise CertificationRejected("certificate does not bind this container request")
    transport = boundary.admission.transport
    if (type(transport) is not PinnedDockerTransport or transport._production_edge is not True
            or type(transport._control) is not PinnedSSHSeatControl):
        raise CertificationRejected("certificate does not bind the pinned Linux container")
    _require_certificate_turn_binding(request, certificate, _payload(boundary),
                                      transport._control.vm_observation["profile_identity"])
    transport._require_fixed_runner()
    validate_container_boundary(boundary, request, boundary.controller)
    if transport._control.vm_observation["profile_identity"] != payload["vm"][request.executor_id]["profile_identity"]:
        raise CertificationRejected("certificate VM owner profile identity changed")


def require_owner_held_release_certificate(request) -> ReleaseCertificate:
    from .providers import ProviderRequestError
    try:
        _, _, path = _owner_paths()
        certificate = read_certificate(path)
        authority = _owner_authority()
        profile = authority.admit_release_profile(certificate, request.certificate_roots, request.executor_id,
            source_hash=release_source_binding().digest, quality_tier=request.profile.quality_tier if request.profile else "standard")
        if request.profile is None or request.profile.digest != profile.digest:
            raise CertificationRejected("certificate profile differs")
        _require_certified_boundary(request, certificate)
        return certificate
    except Exception:
        raise ProviderRequestError("release-certified container boundary requires a valid owner-held release certificate") from None


def run_trial_turn(request, certificate: TrialCertificate, exact_synthetic_roots: tuple[Path, ...]):
    from .container_boundary import run_container_turn
    try:
        authority = _owner_authority()
        profile = authority.admit_trial_profile(certificate, exact_synthetic_roots, request.executor_id,
            source_hash=release_source_binding().digest, quality_tier=request.profile.quality_tier if request.profile else "standard")
        if (request.certificate_roots != exact_synthetic_roots or Path(request.cwd) not in exact_synthetic_roots
                or request.profile is None or request.profile.digest != profile.digest):
            raise CertificationRejected("trial certificate binds only exact synthetic roots")
        _require_certified_boundary(request, certificate)
    except Exception:
        raise CertificationRejected("trial certificate cannot admit this synthetic container request") from None
    return run_container_turn(request, request.container_boundary)
