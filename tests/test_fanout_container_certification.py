"""Certificate authority must reopen authenticated private evidence itself."""
from __future__ import annotations

import importlib
import base64
import copy
import hashlib
import json
import stat
import subprocess
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_fanout_container_certify import (
    certifier, company_manifest, complete_receipt, _completed_quality_root,
    _reseal_completed_receipt,
)
from test_fanout_container_boundary import _terminal, SESSION_IDS
from test_fanout_container_upstream import (
    upstream_module, connection_factory, _routed_sender, _routed_request,
)
import test_fanout_container_runtime as runtime_tests
from shared.lib.fanout.artifacts import ArtifactRef, ArtifactStore, canonical_json
from shared.lib.fanout.broker import Broker, BrokerPermit, BrokerRejected
from shared.lib.fanout.container_broker import OutboundRequestIdentity, PermitLedger, RelayRequest, dispatch_once
from shared.lib.fanout.container_boundary import PinnedDockerTransport, ContainerAdmission, SessionStore, _turn_digest
from shared.lib.fanout.container_runtime import ContainerLauncher, ContainerSpec, private_volume_names
from shared.lib.fanout.container_source import release_source_binding
from shared.lib.fanout.container_transfer import TransferIdentity, snapshot_manifest, encode_manifest
from shared.lib.fanout.container_transfer_transport import (
    _InputClaimJournal, admitted_request_digest,
)
from shared.lib.fanout.container_upstream import HostUpstream
from shared.lib.fanout.container_vm import (
    VMProfileSpec, parse_profile_config, require_vm_probe,
)
from shared.lib.fanout.controller import read_evidence, write_evidence
from shared.lib.fanout.controller import LifecycleCapability, create_lifecycle_controller
from shared.lib.fanout.providers import ProviderRequest, ProviderRegistry
from shared.lib.fanout.repo import capture_repository_baseline
from shared.lib.fanout.verification import create_candidate_from_export


def certification():
    try:
        return importlib.import_module("shared.lib.fanout.container_certification")
    except ModuleNotFoundError:
        pytest.fail("the shared authenticated evidence and certificate gates are missing")


def test_quality_wire_reader_reopens_actual_prepared_and_served_bytes(tmp_path):
    from test_fanout_container_upstream import _route, _routed_permit
    from shared.lib.fanout.container_certification import reopen_quality_wire, CertificationRejected
    route = _route("claude")
    permit = _routed_permit(route, "/v1/projects/fictional-project/locations/europe-west1/publishers/anthropic/models/claude-opus-5-5:rawPredict")
    normal = canonical_json({"model": permit.model, "max_tokens": 16,
        "messages": [{"role": "user", "content": "inert wire reader"}], "tools": []})
    prepared = HostUpstream(permit, route, max_response_bytes=permit.max_response_bytes,
                            deadline_seconds=permit.response_deadline).preview(
        "POST", "/v1/messages", {"content-type": "application/json"}, normal)
    response = canonical_json({"type": "message", "id": "inert-quality-response",
        "model": permit.upstream_model, "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "inert complete assistant answer"}]})
    identity = OutboundRequestIdentity("POST", "/v1/messages", hashlib.sha256(normal).hexdigest(),
        16, permit.model, prepared.route_digest, prepared.origin, prepared.path,
        hashlib.sha256(prepared.body).hexdigest(), prepared.effective_model)
    with ArtifactStore(tmp_path / "raw") as store:
        refs = {name: asdict(store.write_bytes(name + ".bin", body)) for name, body in
                (("normalized_body", normal), ("effective_body", prepared.body), ("response_body", response))}
        captured = {**refs, "delivered_body": refs["response_body"], "request_id": "wire-1",
                    "http_status": 200, "content_type": "application/json"}
        record = {"request_id": "wire-1", "identity": asdict(identity), "state": "settled",
                  "response_digest": hashlib.sha256(response).hexdigest()}
        assert reopen_quality_wire(store, captured, permit, record, route) == ("inert-quality-response", permit.upstream_model)
        foreign = store.write_bytes("foreign.bin", response.replace(b"inert-quality-response", b"foreign-response"))
        with pytest.raises(CertificationRejected, match="wire"):
            reopen_quality_wire(store, dict(captured, delivered_body=asdict(foreign)), permit, record, route)
        with pytest.raises(CertificationRejected, match="wire"):
            reopen_quality_wire(store, captured, permit, dict(record, state="uncertain"), route)


def _served_response(provider, identity="synthetic-response"):
    if provider == "claude":
        return {"type": "message", "id": identity, "model": "claude-opus-5-5",
                "stop_reason": "end_turn", "content": []}
    if provider == "codex":
        return {"object": "response", "id": identity, "model": "gpt-6.1-sol",
                "status": "completed", "error": None, "incomplete_details": None}
    return {"responseId": identity, "modelVersion": "gemini-3.8-flash",
            "candidates": [{"finishReason": "STOP", "content": {"parts": []}}]}


@pytest.fixture
def persisted_probe(certifier, complete_receipt, company_manifest, tmp_path, monkeypatch):
    root, controller, ledger = _completed_quality_root(
        certifier, complete_receipt, tmp_path, monkeypatch)
    return root, controller, ledger, company_manifest[2]


@pytest.fixture
def complete_private_probe(persisted_probe, certifier, tmp_path, monkeypatch, request):
    """Real HMAC records and full bytes; only Docker/provider I/O is synthetic."""
    root, controller, ledger, manifest = persisted_probe
    foreign_count = getattr(request, "param", 20)
    retained = {}
    for index in range(foreign_count):
        container_id = f"{index + 1000:064x}"
        retained[container_id] = {"Id": container_id, "Config": {"Labels": {}},
                                  "HostConfig": {"Mounts": []}, "Mounts": []}
    receipt = copy.deepcopy(certifier._read_private_receipt(root))
    receipt["release_source_binding"] = release_source_binding().to_dict()
    raw_root = root / "raw"
    raw_root.mkdir(mode=0o700)
    targets = root / "targets"
    targets.mkdir(mode=0o700)
    for name in ("repo-alpha", "repo-beta"):
        target = targets / name
        target.mkdir(mode=0o700)
        (target / "README.md").write_bytes(f"# {name}\nDisposable synthetic target.\n".encode())
        certifier._git_synthetic(target)
    baseline = capture_repository_baseline(targets / "repo-alpha")
    with ledger._locked() as state:
        state.update(permits={}, requests={}, send_claims={})
        ledger._save(state)
    _, selected = certifier._admit_route_set(
        Path(receipt["manifest_path"]), Path(receipt["policy_path"]),
        {provider: item["route_id"] for provider, item in receipt["machine_routes"].items()})
    colima = b"arch: x86_64\nvmType: qemu\nmounts: null\n"
    lima = b"mounts: []\n"
    context = {"Name": "colima-fanout-isolated", "Endpoints": {"docker": {
        "Host": f"unix://{Path.home()}/.colima/fanout-isolated/docker.sock"}}}
    observation = {name: base64.b64encode(data).decode() for name, data in {
        "status": b'{"status":"Running"}', "context": canonical_json(context),
        "info": b"27.0\n", "colima": colima, "lima": lima,
        "mounts": b"proc /proc proc rw 0 0\n", "nonce": b"n" * 32, "reply": b"n" * 32,
    }.items()}
    observation["host_key"] = "ssh-ed25519 " + "A" * 44
    vm = require_vm_probe(
        VMProfileSpec("fanout-isolated", "colima-fanout-isolated", "x86_64"),
        parse_profile_config(colima.decode(), lima.decode(), context["Name"]),
        ["proc /proc proc rw 0 0"], context["Endpoints"]["docker"]["Host"], b"n" * 32, b"n" * 32,
        ssh_host_key=observation["host_key"], pinned_host_key=observation["host_key"])
    observation["probe"] = asdict(vm)
    observation["profile_identity"] = {"schema": 2, "profile": vm.profile, "context": context["Name"],
        "endpoint": vm.docker_endpoint, "ssh_host_key_sha256": vm.ssh_host_key_sha256,
        "profile_dir": {"device": 1, "inode": 2}, "lima_dir": {"device": 1, "inode": 3},
        "profile_config": {"device": 1, "inode": 4, "sha256": hashlib.sha256(colima).hexdigest()},
        "lima_config": {"device": 1, "inode": 5, "sha256": hashlib.sha256(lima).hexdigest()},
        "lima_disk": {"device": 1, "inode": 6},
        "context_metadata_dir": {"path": "/synthetic/context", "device": 1, "inode": 7},
        "context_metadata": {"device": 1, "inode": 8, "sha256": "8" * 64}}
    observation["profile_binding"] = observation["profile_identity"]
    with ArtifactStore(raw_root) as raw, ArtifactStore(controller.root / "quarantine" / "input-material-store") as materials:
        for provider in ("claude", "codex", "agy"):
            input_receipt_ref = None
            for route, epoch in (("initial", 1), ("resume", 2)):
                cell = receipt["providers"][provider][route]
                profile = ProviderRegistry.default(version_probe=lambda _: "unused").select(provider, "repo-write", "standard")
                request = ProviderRequest(provider, "complete synthetic plan", cwd=baseline.repository,
                    execution_class="repo-write", profile=profile, run_id="private-quality",
                    task_id="synthetic-task", target_id="repo-alpha", seat_id=f"seat-{provider}",
                    inputs_digest=hashlib.sha256(baseline.entries[0].data).hexdigest(),
                    session_id=SESSION_IDS[provider] if route == "resume" or provider == "claude" else None, resume=route == "resume",
                    context_sha256=hashlib.sha256(b"complete synthetic plan").hexdigest(),
                    skill_bundle_sha256=hashlib.sha256(b"skills").hexdigest(),
                    skill_delivery_sha256=hashlib.sha256(b"skills").hexdigest())
                identity = TransferIdentity(request.run_id, request.task_id, request.target_id, request.seat_id, request.inputs_digest)
                permit = BrokerPermit(provider, request.run_id, request.task_id, request.target_id,
                    request.seat_id, route, profile.requested_model, "disposable-placeholder",
                    4, 1048576, 8388608, 8192, inputs_digest=request.inputs_digest,
                    session_id=request.session_id, upstream_route_digest=selected[provider].digest,
                    upstream_origin=selected[provider].profile.origin,
                    upstream_model=selected[provider].profile.effective_model,
                    upstream_path=receipt["machine_routes"][provider]["path"])
                permit_id = ledger.register(permit, epoch)
                cell["permit_id"] = permit_id
                workspace, session = private_volume_names(request.run_id, request.task_id, request.target_id, request.seat_id)
                spec = ContainerSpec(request.run_id, request.task_id, request.target_id, request.seat_id,
                    cell["image_digest"], workspace, session, f"/run/fanout/{request.seat_id}.sock", cell["cli_digest"])
                for name, value in {"WORKSPACE": workspace, "SESSION": session,
                    "SOCKET": spec.socket_path, "CONTAINER_ID": cell["container_id"],
                    "IMAGE": spec.image_digest, "IDENTITY_HASH": workspace.removeprefix("fanout-workspace-")}.items():
                    monkeypatch.setattr(runtime_tests, name, value)
                docker = runtime_tests.FakeDocker(spec)
                docker.old = copy.deepcopy(retained)
                socket = runtime_tests.FakeAttestor().proof
                launcher = ContainerLauncher(docker, guest_socket_attestor=lambda _: socket)
                sessions = SessionStore(controller, request.run_id, request.task_id,
                    request.target_id, request.seat_id, session)
                transport = PinnedDockerTransport.start(controller, spec, launcher=launcher,
                    vm_attestor=lambda: vm, socket_attestor=lambda _: socket,
                    image_attestor=lambda: spec.image_digest, cli_attestor=lambda _: spec.cli_digest,
                    process_runner=lambda _: None, recover=route == "resume", request=request,
                    session_control=SimpleNamespace(ledger=ledger, permit_id=permit_id, epoch=epoch,
                        broker=Broker(permit, credential="synthetic-controller-only", send=lambda *_: None)))
                handle = transport.handle
                launch_ref = (transport.handle_evidence_name, transport.handle_evidence_digest)
                transport.attest_container()
                observed = transport.runtime_observation
                if route == "initial":
                    source = controller.root / "quarantine" / f"source-{provider}"
                    source.mkdir(mode=0o700)
                    (source / "repo").mkdir(mode=0o700)
                    (source / "context").mkdir(mode=0o700)
                    (source / "repo/README.md").write_bytes(baseline.entries[0].data)
                    (source / "context/full-plan.md").write_bytes(b"complete synthetic plan")
                    (source / "context/skills.json").write_bytes(b"skills")
                    material_manifest = snapshot_manifest(source, identity)
                    manifest_ref = materials.write_json(f"expected/{provider}.json", asdict(material_manifest))
                    info = source.stat()
                    material_ref = write_evidence(controller, "container-input-material", {
                        "schema_version": "InputMaterialV1", "identity": asdict(identity),
                        "source_device": info.st_dev, "source_inode": info.st_ino,
                        "original_handle": asdict(handle), "workspace_volume": workspace,
                        "run_inputs_sha256": request.inputs_digest,
                        "task_packet_sha256": request.context_sha256,
                        "target_baseline_sha256": request.inputs_digest,
                        "workspace_sha256": hashlib.sha256(canonical_json(asdict(material_manifest))).hexdigest(),
                        "skill_delivery_sha256": request.skill_delivery_sha256,
                        "dependency_context_sha256": hashlib.sha256(b"dependencies").hexdigest(),
                        "provider_context_sha256": request.context_sha256,
                        "skill_bundle_sha256": request.skill_bundle_sha256,
                        "manifest_ref": asdict(manifest_ref)})
                    from shared.lib.fanout.container_transfer_transport import issue_input_source_grant
                    from test_fanout_container_transfer_transport import _guest_popen
                    admitted = ContainerAdmission(permit, permit_id, epoch, ledger, spec.image_digest,
                        spec.cli_digest, socket, transport, sessions, *launch_ref)
                    grant = issue_input_source_grant(controller, request, transport, admitted, source, *material_ref)
                    guest = tmp_path / f"certification-guest-{provider}"
                    guest.mkdir(mode=0o700)
                    transport._transfer_popen, _ = _guest_popen(guest, [], container_id=handle.container_id)
                    imported = transport.stage_inputs(request, grant)
                    input_receipt_ref = imported.evidence_name, imported.evidence_digest
                input_payload = certifier.read_evidence(controller, "container-input-transfer", *input_receipt_ref)
                boundary = {"controller_id": controller.controller_id, "run_id": request.run_id,
                    "task_id": request.task_id, "target_id": request.target_id, "seat_id": request.seat_id,
                    "provider": provider, "inputs_digest": request.inputs_digest, "profile_digest": profile.digest,
                    "request_digest": admitted_request_digest(request), "vm_probe_digest": vm.digest,
                    "vm_docker_endpoint": vm.docker_endpoint, "vm_ssh_host_key_sha256": vm.ssh_host_key_sha256,
                    "vm_effective_mounts_sha256": vm.effective_mounts_sha256, "vm_guest_mounts_sha256": vm.guest_mounts_sha256,
                    "image_digest": spec.image_digest, "cli_digest": spec.cli_digest,
                    "container_id": handle.container_id, "container_inspect_sha256": handle.inspect_sha256,
                    "workspace_volume": workspace, "session_volume": session, "permit_id": permit_id,
                    "permit_epoch": epoch, "upstream_route_digest": selected[provider].digest,
                    "permit_digest": hashlib.sha256(canonical_json(asdict(permit))).hexdigest(), "route": route,
                    "handle_evidence_name": launch_ref[0], "handle_evidence_digest": launch_ref[1],
                    "input_receipt_name": input_receipt_ref[0], "input_receipt_digest": input_receipt_ref[1],
                    "input_manifest_sha256": input_payload["manifest_sha256"], "input_workdir": input_payload["workdir"],
                    **{"socket_" + name: value for name, value in asdict(socket).items()}}
                boundary_ref = write_evidence(controller, "container-seat-admission", boundary)
                marker = ((f"certified synthetic {provider} initial\n".encode() if route == "resume" else b"") + f"certified synthetic {provider} {route}\n".encode())
                exported = controller.root / "quarantine" / f"candidate-{provider}-{route}"
                exported.mkdir(mode=0o700)
                (exported / "README.md").write_bytes(baseline.entries[0].data + marker)
                export_manifest = snapshot_manifest(exported, identity)
                export_digest = hashlib.sha256(canonical_json(asdict(export_manifest))).hexdigest()
                candidate = create_candidate_from_export(baseline, exported, identity, export_digest)
                export_ref = write_evidence(controller, "container-candidate-export", {
                    "identity": asdict(identity), "turn_digest": _turn_digest(request, SimpleNamespace(**boundary)),
                    "manifest_sha256": export_digest, "root": str(exported), "container_id": handle.container_id,
                    "container_inspect_sha256": handle.inspect_sha256, "workspace_volume": workspace, "session_volume": session})
                terminal = _terminal(provider, session_id=SESSION_IDS[provider])
                adapter = ProviderRegistry.default(version_probe=lambda _: "unused").require(provider)
                parsed = adapter.parse(terminal.stdout)
                from shared.lib.fanout.providers import _make_result
                result = _make_result(request, adapter, terminal, parsed, 1, 0.1)
                assert result.valid and result.session_id == SESSION_IDS[provider]
                turn_digest = _turn_digest(request, SimpleNamespace(**boundary))
                sessions.reserve(provider, turn_digest, initial=route == "initial",
                    upstream_route_digest=permit.upstream_route_digest,
                    initial_session_id=request.session_id if provider == "claude" and route == "initial" else None)
                sessions.settle(provider, result.session_id, turn_digest, initial=route == "initial",
                                upstream_route_digest=permit.upstream_route_digest)
                answer = parsed.answer.encode()
                prefix = f"{provider}-{route}"
                def artifact(name, data):
                    return asdict(raw.write_bytes(f"{prefix}-{name}", data))
                normal = ({"model": profile.requested_model, "messages": [], "max_tokens": 32, "stream": True} if provider == "claude" else
                          {"model": profile.requested_model, "input": [], "max_output_tokens": 32, "stream": True} if provider == "codex" else
                          {"contents": [], "generationConfig": {"maxOutputTokens": 32}})
                body = canonical_json(normal)
                path = "/v1/messages" if provider == "claude" else "/v1/responses" if provider == "codex" else "/v1beta/models/gemini-3.8-flash:streamGenerateContent?alt=sse"
                sender = HostUpstream(permit, selected[provider], max_response_bytes=permit.max_response_bytes, deadline_seconds=2)
                prepared = sender.preview("POST", path, {"content-type": "application/json"}, body)
                wire_identity = OutboundRequestIdentity("POST", path, hashlib.sha256(body).hexdigest(), 32,
                    "gemini-3.8-flash" if provider == "agy" else profile.requested_model,
                    prepared.route_digest, prepared.origin, prepared.path,
                    hashlib.sha256(prepared.body).hexdigest(), prepared.effective_model)
                request_id = f"{provider}-{route}"
                response = canonical_json(_served_response(provider, request_id)).rstrip(b"\n")
                ledger.record_attempt(permit_id, epoch)
                ledger.reserve(permit_id, epoch, request_id, wire_identity)
                ledger.claim_send(permit_id, epoch, request_id, permit, wire_identity)
                ledger.settle(request_id, hashlib.sha256(response).hexdigest())
                ledger.fence_epoch(permit_id, epoch)
                records = list(ledger.request_records(permit_id, epoch))
                transport.close()
                stopped_ref = transport.stop_evidence
                retained = copy.deepcopy(docker.old)
                retained[handle.container_id] = copy.deepcopy(docker.inspect)
                payload = {"schema_version": "fanout-private-cell-attestation-v1",
                    "release_source_binding": receipt["release_source_binding"], "host_runtime": receipt["host_runtime"],
                    "request": {"executor_id": provider, "prompt": request.prompt, "cwd": str(request.cwd),
                        "execution_class": "repo-write", "run_id": request.run_id, "task_id": request.task_id,
                        "target_id": request.target_id, "seat_id": request.seat_id, "inputs_digest": request.inputs_digest,
                        "session_id": request.session_id, "resume": request.resume,
                        "context_sha256": request.context_sha256, "skill_bundle_sha256": request.skill_bundle_sha256,
                        "skill_delivery_sha256": request.skill_delivery_sha256},
                    "permit": asdict(permit), "boundary_evidence_name": boundary_ref[0], "boundary_evidence_digest": boundary_ref[1],
                    "vm": artifact("vm.json", canonical_json(observation)),
                    "runtime": artifact("runtime.json", canonical_json(observed)),
                    "process": {"status": "exit", "returncode": 0, "capture_complete": True,
                        "stdout": artifact("stdout.bin", terminal.stdout), "stderr": artifact("stderr.bin", terminal.stderr)},
                    "answer": artifact("answer.bin", answer),
                    "isolation": artifact("isolation.json", canonical_json({"isolation": cell["isolation"], "own_workspace_write": True})),
                    "stop_evidence_name": stopped_ref[0], "stop_evidence_digest": stopped_ref[1],
                    "baseline_root": str(baseline.repository), "baseline_digest": baseline.digest,
                    "wire": [{"request_id": request_id, "content_type": "application/json", "http_status": 200,
                        "normalized_body": artifact("normal.bin", body), "effective_body": artifact("effective.bin", prepared.body),
                        "response_body": artifact("response.bin", response),
                        "delivered_body": asdict(ArtifactRef(f"{prefix}-response.bin", hashlib.sha256(response).hexdigest(), len(response)))}]}
                attestation_ref = write_evidence(controller, "container-private-cell-attestation", payload)
                cell.update(vm_probe_sha256=vm.digest, socket_attestation_sha256=hashlib.sha256(canonical_json(asdict(socket))).hexdigest(),
                    observed_model=adapter.parse(terminal.stdout).observed_model,
                    container_inspect_sha256=handle.inspect_sha256, session_volume=session, session_id=SESSION_IDS[provider],
                    ledger_requests=records, broker_dispositions=[{"request_id": request_id, "state": "settled", "response_sha256": records[0]["response_digest"]}],
                    input_receipt_digest=input_receipt_ref[1], input_manifest_sha256=input_payload["manifest_sha256"],
                    candidate_digest=candidate.digest, candidate_export_manifest_sha256=export_digest,
                    candidate_export_evidence_digest=export_ref[1], candidate_export_root=str(exported),
                    answer_sha256=hashlib.sha256(answer).hexdigest(), stdout_sha256=hashlib.sha256(terminal.stdout).hexdigest(), stderr_sha256=hashlib.sha256(terminal.stderr).hexdigest(),
                    attestation_evidence_name=attestation_ref[0], attestation_evidence_digest=attestation_ref[1])
    _reseal_completed_receipt(certifier, root, controller, receipt)
    return root, controller, ledger, manifest


@pytest.mark.parametrize("complete_private_probe", [0, 20], indirect=True)
def test_complete_persisted_attestations_are_reopened(complete_private_probe, monkeypatch):
    api = certification()
    def refuse_docker(*_):
        pytest.fail("saved runtime replay attempted Docker")
    monkeypatch.setattr("shared.lib.fanout.container_runtime._default_docker", refuse_docker)
    root, _, _, manifest = complete_private_probe
    proof = api.read_private_route_evidence(root, manifest, require_attestations=True)
    assert proof.attestations_complete
    receipt = proof.receipt
    assert receipt["providers"]["codex"]["initial"]["observed_model"] is None
    assert receipt["providers"]["codex"]["resume"]["observed_model"] is None
    for provider, count in (("claude", 1), ("codex", 3), ("agy", 5)):
        cell = receipt["providers"][provider]["resume"]
        value = read_evidence(complete_private_probe[1], "container-private-cell-attestation",
                              cell["attestation_evidence_name"], cell["attestation_evidence_digest"])
        boundary = read_evidence(complete_private_probe[1], "container-seat-admission",
                                 value["boundary_evidence_name"], value["boundary_evidence_digest"])
        launch = read_evidence(complete_private_probe[1], "container-seat-launch",
                               boundary["handle_evidence_name"], boundary["handle_evidence_digest"])
        assert len(launch["recovery"]["predecessors"]) == count


def _edit_complete_cell(complete_private_probe, certifier, provider, route, *, runtime=None, launch=None):
    root, controller, _, _ = complete_private_probe
    receipt = copy.deepcopy(certifier._read_private_receipt(root))
    cell = receipt["providers"][provider][route]
    value = read_evidence(controller, "container-private-cell-attestation",
                          cell["attestation_evidence_name"], cell["attestation_evidence_digest"])
    if runtime is not None:
        with ArtifactStore.open_existing(root / "raw") as store:
            observations = json.loads(store.read_bytes(ArtifactRef(**value["runtime"])))
            runtime(observations)
            value["runtime"] = asdict(store.write_json(f"{provider}-{route}-edited-runtime.json", observations))
    if launch is not None:
        boundary = read_evidence(controller, "container-seat-admission",
                                 value["boundary_evidence_name"], value["boundary_evidence_digest"])
        origin = read_evidence(controller, "container-seat-launch",
                               boundary["handle_evidence_name"], boundary["handle_evidence_digest"])
        launch(origin)
        name, digest = write_evidence(controller, "container-seat-launch", origin)
        boundary.update(handle_evidence_name=name, handle_evidence_digest=digest)
        value["boundary_evidence_name"], value["boundary_evidence_digest"] = write_evidence(
            controller, "container-seat-admission", boundary)
        stopped = read_evidence(controller, "container-seat-stopped",
                                value["stop_evidence_name"], value["stop_evidence_digest"])
        stopped.update(launch_evidence_name=name, launch_evidence_digest=digest)
        value["stop_evidence_name"], value["stop_evidence_digest"] = write_evidence(
            controller, "container-seat-stopped", stopped)
    cell["attestation_evidence_name"], cell["attestation_evidence_digest"] = write_evidence(
        controller, "container-private-cell-attestation", value)
    _reseal_completed_receipt(certifier, root, controller, receipt)


@pytest.mark.parametrize("provider,fault", [(provider, fault)
    for provider in ("claude", "codex", "agy") for fault in ("missing", "wrong-model", "incomplete")])
def test_full_authority_requires_actual_completed_served_identity(
    complete_private_probe, certifier, certificate_authority, trial_roots, provider, fault,
):
    root, controller, ledger, manifest = complete_private_probe
    api = certification()
    assert api.read_private_route_evidence(root, manifest, require_attestations=True).attestations_complete
    receipt = copy.deepcopy(certifier._read_private_receipt(root))
    cell = receipt["providers"][provider]["resume"]
    value = read_evidence(controller, "container-private-cell-attestation",
                          cell["attestation_evidence_name"], cell["attestation_evidence_digest"])
    response = _served_response(provider, f"{provider}-resume")
    if fault == "missing":
        response = {"requested_model": value["permit"]["model"], "ok": True}
    elif fault == "wrong-model":
        response["modelVersion" if provider == "agy" else "model"] = "synthetic-unserved-model"
    elif provider == "claude":
        response["stop_reason"] = None
    elif provider == "codex":
        response["status"] = "in_progress"
    else:
        response["candidates"][0]["finishReason"] = "MAX_TOKENS"
    body = canonical_json(response).rstrip(b"\n")
    digest = hashlib.sha256(body).hexdigest()
    with ArtifactStore.open_existing(root / "raw") as raw:
        ref = asdict(raw.write_bytes(f"{provider}-otherwise-valid-{fault}.json", body))
    value["wire"][0].update(response_body=ref, delivered_body=ref)
    request_id = value["wire"][0]["request_id"]
    with ledger._locked() as state:
        state["requests"][request_id]["response_digest"] = digest
        ledger._save(state)
    cell["ledger_requests"] = list(ledger.request_records(cell["permit_id"], cell["permit_epoch"]))
    cell["broker_dispositions"][0]["response_sha256"] = digest
    cell["attestation_evidence_name"], cell["attestation_evidence_digest"] = write_evidence(
        controller, "container-private-cell-attestation", value)
    _reseal_completed_receipt(certifier, root, controller, receipt)
    with pytest.raises(api.CertificationRejected):
        api.read_private_route_evidence(root, manifest, require_attestations=True)
    with pytest.raises(api.CertificationRejected):
        certificate_authority.issue_trial(root, trial_roots)


@pytest.mark.parametrize("fault", ["original", "delivered", "request"])
def test_full_reader_rejects_otherwise_valid_cross_request_wire_swaps(
    complete_private_probe, certifier, fault,
):
    root, controller, _, manifest = complete_private_probe
    api = certification()
    assert api.read_private_route_evidence(root, manifest, require_attestations=True).attestations_complete
    receipt = copy.deepcopy(certifier._read_private_receipt(root))
    initial, resumed = receipt["providers"]["claude"]["initial"], receipt["providers"]["claude"]["resume"]
    first = read_evidence(controller, "container-private-cell-attestation", initial["attestation_evidence_name"], initial["attestation_evidence_digest"])
    value = read_evidence(controller, "container-private-cell-attestation", resumed["attestation_evidence_name"], resumed["attestation_evidence_digest"])
    if fault == "request":
        value["wire"][0]["request_id"] = first["wire"][0]["request_id"]
    else:
        field = "response_body" if fault == "original" else "delivered_body"
        value["wire"][0][field] = first["wire"][0][field]
    resumed["attestation_evidence_name"], resumed["attestation_evidence_digest"] = write_evidence(controller, "container-private-cell-attestation", value)
    _reseal_completed_receipt(certifier, root, controller, receipt)
    with pytest.raises(api.CertificationRejected):
        api.read_private_route_evidence(root, manifest, require_attestations=True)


@pytest.mark.parametrize("changed", ["missing", "duplicate", "extra", "reordered", "shape",
    "argument", "base64", "noncanonical-base64", "oversized", "changed", "foreign-overlap"])
def test_signed_runtime_transcript_refuses_incomplete_or_unsafe_observations(
        complete_private_probe, certifier, changed):
    def edit(items):
        if changed == "missing":
            items.pop()
        elif changed == "duplicate":
            items.append(copy.deepcopy(items[0]))
        elif changed == "extra":
            items.append({"argv": ["info", "unused"], "stdout": ""})
        elif changed == "reordered":
            items[0], items[1] = items[1], items[0]
        elif changed == "shape":
            items[0]["extra"] = True
        elif changed == "argument":
            items[0]["argv"][0] = 1
        elif changed == "base64":
            items[0]["stdout"] += "!"
        elif changed == "noncanonical-base64":
            item = next(item for item in items if item["stdout"].endswith("="))
            encoded = item["stdout"]
            index = len(encoded.rstrip("=")) - 1
            alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
            item["stdout"] = encoded[:index] + alphabet[alphabet.index(encoded[index]) + 1] + encoded[index + 1:]
            assert base64.b64decode(item["stdout"]) == base64.b64decode(encoded)
        elif changed == "oversized":
            items[0]["stdout"] = base64.b64encode(b"x" * (1024 * 1024 + 1)).decode()
        elif changed == "changed":
            running = next(item for item in items if item["argv"][:3] == ["inspect", "--type", "container"])
            body = json.loads(base64.b64decode(running["stdout"]))
            body[0]["HostConfig"]["Privileged"] = True
            running["stdout"] = base64.b64encode(canonical_json(body)).decode()
        elif changed == "foreign-overlap":
            foreign = items[-1]
            body = json.loads(base64.b64decode(foreign["stdout"]))
            body[0]["Mounts"] = [{"Type": "bind", "Source": "/run/fanout"}]
            foreign["stdout"] = base64.b64encode(canonical_json(body)).decode()
    _edit_complete_cell(complete_private_probe, certifier, "claude", "initial", runtime=edit)
    with pytest.raises(certification().CertificationRejected):
        certification().read_private_route_evidence(complete_private_probe[0], complete_private_probe[3],
                                                   require_attestations=True)


@pytest.mark.parametrize("changed", ["missing", "empty", "duplicate", "incomplete", "incomplete-unrelated", "substituted",
    "digest", "shape", "spec", "handle", "initial", "id", "self", "entry", "type"])
def test_signed_recovery_association_is_exact_and_complete(complete_private_probe, certifier, changed):
    def edit(origin):
        if changed == "missing":
            origin.pop("recovery")
        elif changed == "empty":
            origin["recovery"] = None
        elif changed == "duplicate":
            origin["recovery"]["predecessors"].append(origin["recovery"]["predecessors"][0])
        elif changed == "incomplete":
            origin["recovery"]["predecessors"].pop()
        elif changed == "incomplete-unrelated":
            origin["recovery"]["predecessors"].pop(0)
        elif changed == "substituted":
            origin["recovery"]["predecessors"][0][0] = "f" * 64
        elif changed == "digest":
            origin["recovery"]["predecessors"][0][1] = "0" * 64
        elif changed == "shape":
            origin["recovery"]["extra"] = True
        elif changed == "spec":
            origin["recovery"]["spec"]["cli_digest"] = "f" * 64
        elif changed == "handle":
            origin["recovery"]["handle"]["inspect_sha256"] = "f" * 64
        elif changed == "initial":
            origin["route"] = "start"
        elif changed == "id":
            origin["recovery"]["predecessors"][0][0] = "not-a-container-id"
        elif changed == "self":
            origin["recovery"]["predecessors"][0][0] = origin["handle"]["container_id"]
        elif changed == "entry":
            origin["recovery"]["predecessors"][0].pop()
        elif changed == "type":
            origin["recovery"]["predecessors"] = {}
    _edit_complete_cell(complete_private_probe, certifier, "agy", "resume", launch=edit)
    with pytest.raises(certification().CertificationRejected):
        certification().read_private_route_evidence(complete_private_probe[0], complete_private_probe[3],
                                                   require_attestations=True)


@pytest.mark.parametrize("changed", ["missing", "digest", "running", "new-overlap"])
def test_cold_replay_revalidates_every_stopped_predecessor(complete_private_probe, certifier, changed):
    def edit(items):
        inspect = items[-1]
        if changed == "missing":
            inventory = next(item for item in items if item["argv"][:2] == ["container", "ls"])
            ids = base64.b64decode(inventory["stdout"]).decode().splitlines()
            inventory["stdout"] = base64.b64encode("".join(value + "\n" for value in ids if value != inspect["argv"][-1]).encode()).decode()
            items.pop()
        else:
            body = json.loads(base64.b64decode(inspect["stdout"]))
            if changed == "digest":
                body[0]["HostConfig"]["Privileged"] = True
            elif changed == "running":
                body[0]["State"]["Running"] = True
            elif changed == "new-overlap":
                new_id = "f" * 64
                body[0]["Id"] = new_id
                inventory = next(item for item in items if item["argv"][:2] == ["container", "ls"])
                inventory["stdout"] = base64.b64encode(base64.b64decode(inventory["stdout"]) + (new_id + "\n").encode()).decode()
                inspect = {"argv": ["inspect", "--type", "container", new_id]}
                items.append(inspect)
            inspect["stdout"] = base64.b64encode(canonical_json(body)).decode()
    _edit_complete_cell(complete_private_probe, certifier, "agy", "resume", runtime=edit)
    with pytest.raises(certification().CertificationRejected):
        certification().read_private_route_evidence(complete_private_probe[0], complete_private_probe[3],
                                                   require_attestations=True)


@pytest.mark.parametrize("artifact", ["vm.json", "runtime.json", "stdout.bin", "response.bin"])
def test_changed_complete_raw_artifact_prevents_issuance(complete_private_probe, artifact):
    api = certification()
    root, _, _, manifest = complete_private_probe
    path = root / "raw" / f"claude-initial-{artifact}"
    path.write_bytes(path.read_bytes() + b"altered")
    with pytest.raises(api.CertificationRejected):
        api.read_private_route_evidence(root, manifest, require_attestations=True)


@pytest.fixture
def certificate_authority(tmp_path):
    api = certification()
    trial_key, release_key = tmp_path / "owner-trial.key", tmp_path / "owner-release.key"
    trial_key.write_bytes(b"a" * 64)
    release_key.write_bytes(b"b" * 64)
    for path in (trial_key, release_key):
        path.chmod(0o600)
    return api.CertificateAuthority(trial_key, release_key)


@pytest.fixture
def trial_roots(tmp_path, certifier):
    from shared.lib.fanout.container_coordinator import prepare_container_trial_targets
    run = tmp_path / "synthetic-trial"
    run.mkdir(mode=0o700)
    return prepare_container_trial_targets(run)


def test_trial_issuance_refuses_caller_like_roots_without_creation_provenance(
    complete_private_probe, certificate_authority, tmp_path, certifier,
):
    run = tmp_path / "caller-like"
    (run / "targets").mkdir(mode=0o700, parents=True)
    run.chmod(0o700)
    roots = tuple(run / "targets" / name for name in ("repo-alpha", "repo-beta"))
    for target in roots:
        target.mkdir(mode=0o700)
        (target / "README.md").write_text("Disposable synthetic target.\n")
        certifier._git_synthetic(target)
    with pytest.raises(certification().CertificationRejected):
        certificate_authority.issue_trial(complete_private_probe[0], roots)


@pytest.mark.parametrize("provider", ["claude", "codex", "agy"])
@pytest.mark.parametrize("fault", ["missing", "disagreement"])
def test_success_producer_refuses_requested_only_bytes_but_retains_actual_raw(
    complete_private_probe, certifier, provider, fault,
):
    root, controller, ledger, _ = complete_private_probe
    cell = certifier._read_private_receipt(root)["providers"][provider]["initial"]
    value = read_evidence(controller, "container-private-cell-attestation",
                         cell["attestation_evidence_name"], cell["attestation_evidence_digest"])
    record = cell["ledger_requests"][0]
    raw = (b'{"ok":true}' if fault == "missing" else
           canonical_json(_served_response(provider, "synthetic-original")).rstrip(b"\n"))
    delivered = (raw if fault == "missing" else
                 canonical_json(_served_response(provider, "synthetic-swapped-delivery")).rstrip(b"\n"))
    with ArtifactStore.open_existing(root / "raw") as store:
        observed = {name: store.read_bytes(ArtifactRef(**value["wire"][0][name]))
                    for name in ("normalized_body", "effective_body")}
    identity = record["identity"]
    observed.update(method="POST", provider_path=identity["path"],
        **{name: identity[name] for name in ("upstream_origin", "upstream_path", "upstream_model", "upstream_route_digest")},
        content_type="application/json", http_status=200, response_body=raw, delivered_body=delivered)
    with ledger._locked() as state:
        state["requests"][record["request_id"]].update(state="uncertain", response_digest=None)
        ledger._save(state)
    prefix = f"{provider}-refused"
    with pytest.raises(BrokerRejected):
        certifier._capture_private_wire(ledger, cell["permit_id"], cell["permit_epoch"], root / "raw", prefix, observed)
    with ArtifactStore.open_existing(root / "raw") as store:
        ref = ArtifactRef(f"{prefix}-wire/{record['request_id']}/response_body.bin", hashlib.sha256(raw).hexdigest(), len(raw))
        assert store.read_bytes(ref) == raw
        if fault == "disagreement":
            assert store.read_bytes(ArtifactRef(f"{prefix}-wire/{record['request_id']}/delivered_body.bin",
                hashlib.sha256(delivered).hexdigest(), len(delivered))) == delivered
    assert ledger.request_records(cell["permit_id"], cell["permit_epoch"])[0]["state"] == "uncertain"


@pytest.mark.parametrize("fault", ["missing", "tampered", "noncanonical", "source", "substituted", "baseline"])
def test_trial_creation_provenance_refuses_otherwise_valid_preparation_drift(
    complete_private_probe, certificate_authority, trial_roots, certifier, fault,
):
    import shutil
    from shared.lib.fanout.container_coordinator import _read_trial_preparation
    root = trial_roots[0].parent.parent
    controller, _ = _read_trial_preparation(root)
    certificate = certificate_authority.issue_trial(complete_private_probe[0], trial_roots)
    pointer = root / "trial-preparation.json"
    if fault == "missing":
        pointer.rename(root / "preserved-preparation.json")
    elif fault == "tampered":
        saved = json.loads(pointer.read_bytes())["evidence"]
        path = controller.root / "receipts" / saved[0]
        path.write_bytes(path.read_bytes() + b"changed")
    elif fault == "noncanonical":
        pointer.write_bytes(pointer.read_bytes() + b"\n")
    elif fault == "source":
        saved = json.loads(pointer.read_bytes())["evidence"]
        value = read_evidence(controller, "container-trial-preparation", *saved)
        value["release_source_binding"]["runtime_sha256"] = "f" * 64
        changed = write_evidence(controller, "container-trial-preparation", value)
        pointer.write_bytes(canonical_json({"evidence": list(changed)}))
    elif fault == "substituted":
        retained = trial_roots[0].with_name("retained-created-root")
        trial_roots[0].rename(retained)
        shutil.copytree(retained, trial_roots[0])
        assert capture_repository_baseline(trial_roots[0]).digest == capture_repository_baseline(retained).digest
    else:
        (trial_roots[0] / "README.md").write_text("changed original baseline\n")
    assert not certificate_authority.verify_trial(certificate, trial_roots, release_source_binding().digest).passed
    with pytest.raises(certification().CertificationRejected):
        certificate_authority.issue_trial(complete_private_probe[0], trial_roots)


def test_trial_certificate_binds_exact_roots_and_cannot_satisfy_release(
    complete_private_probe, certificate_authority, trial_roots, tmp_path,
):
    api = certification()
    root, _, _, _ = complete_private_probe
    certificate = certificate_authority.issue_trial(root, trial_roots)
    assert type(certificate) is api.TrialCertificate
    assert certificate_authority.verify_trial(certificate, trial_roots, release_source_binding().digest).passed
    caller = tmp_path / "caller"
    caller.mkdir()
    assert not certificate_authority.verify_trial(certificate, (caller,), release_source_binding().digest).passed
    assert not certificate_authority.verify_release(certificate, trial_roots, release_source_binding().digest).passed
    assert not certificate_authority.verify_trial(replace(certificate, mac="0" * 64), trial_roots, release_source_binding().digest).passed


@pytest.mark.parametrize("change", ["challenge", "profile", "key", "mounts", "endpoint"])
def test_certificate_requires_same_stable_initial_resume_vm_not_challenge(
    complete_private_probe, certificate_authority, trial_roots, certifier, change,
):
    from shared.lib.fanout.container_certification import _reopen_vm
    root, controller, _, manifest = complete_private_probe
    receipt = copy.deepcopy(certifier._read_private_receipt(root))
    cell = receipt["providers"]["claude"]["resume"]
    value = read_evidence(controller, "container-private-cell-attestation",
                         cell["attestation_evidence_name"], cell["attestation_evidence_digest"])
    with ArtifactStore.open_existing(root / "raw") as raw:
        observed = json.loads(raw.read_bytes(ArtifactRef(**value["vm"])))
        if change == "profile":
            observed["profile_identity"]["lima_disk"]["inode"] += 1
        elif change == "key":
            observed["host_key"] = "ssh-ed25519 " + "B" * 44
        elif change == "mounts":
            observed["mounts"] = base64.b64encode(b"proc /proc proc rw 0 0\nsysfs /sys sysfs ro 0 0\n").decode()
        else:
            observed["nonce"] = observed["reply"] = base64.b64encode(b"c" * 32).decode()
        decoded = {name: base64.b64decode(observed[name]) for name in ("colima", "lima", "mounts", "nonce", "reply")}
        context = json.loads(base64.b64decode(observed["context"]))
        vm = require_vm_probe(VMProfileSpec("fanout-isolated", "colima-fanout-isolated", "x86_64"),
            parse_profile_config(decoded["colima"].decode(), decoded["lima"].decode(), context["Name"]),
            decoded["mounts"].decode().splitlines(), context["Endpoints"]["docker"]["Host"], decoded["nonce"], decoded["reply"],
            ssh_host_key=observed["host_key"], pinned_host_key=observed["host_key"])
        observed["probe"] = asdict(vm)
        if change == "endpoint":
            from shared.lib.fanout.providers import ProviderRequestError
            vm = replace(vm, docker_endpoint="unix:///synthetic/foreign.sock")
            context["Endpoints"]["docker"]["Host"] = vm.docker_endpoint
            observed["context"] = base64.b64encode(canonical_json(context)).decode()
            observed["probe"] = asdict(vm)
            observed["profile_identity"]["endpoint"] = vm.docker_endpoint
        observed["profile_identity"]["ssh_host_key_sha256"] = vm.ssh_host_key_sha256
        observed["profile_binding"] = copy.deepcopy(observed["profile_identity"])
        if change == "endpoint":
            with pytest.raises(ProviderRequestError, match="Docker endpoint"):
                _reopen_vm(observed)
        else:
            assert _reopen_vm(observed) == vm
        value["vm"] = asdict(raw.write_json(f"resume-valid-vm-{change}.json", observed))
    boundary = read_evidence(controller, "container-seat-admission", value["boundary_evidence_name"], value["boundary_evidence_digest"])
    boundary.update(vm_probe_digest=vm.digest, vm_docker_endpoint=vm.docker_endpoint,
        vm_ssh_host_key_sha256=vm.ssh_host_key_sha256, vm_effective_mounts_sha256=vm.effective_mounts_sha256,
        vm_guest_mounts_sha256=vm.guest_mounts_sha256)
    value["boundary_evidence_name"], value["boundary_evidence_digest"] = write_evidence(controller, "container-seat-admission", boundary)
    exported = read_evidence(controller, "container-candidate-export", cell["candidate_export_evidence_digest"] + ".json", cell["candidate_export_evidence_digest"])
    request = ProviderRequest(**value["request"], profile=ProviderRegistry.default().select("claude", "repo-write", "standard"))
    exported["turn_digest"] = _turn_digest(request, SimpleNamespace(**boundary))
    cell["candidate_export_evidence_digest"] = write_evidence(controller, "container-candidate-export", exported)[1]
    cell["vm_probe_sha256"] = vm.digest
    cell["attestation_evidence_name"], cell["attestation_evidence_digest"] = write_evidence(controller, "container-private-cell-attestation", value)
    _reseal_completed_receipt(certifier, root, controller, receipt)
    if change == "challenge":
        certificate = certificate_authority.issue_trial(root, trial_roots)
        assert certificate_authority.verify_trial(certificate, trial_roots, release_source_binding().digest).passed
    else:
        with pytest.raises(certification().CertificationRejected):
            certificate_authority.issue_trial(root, trial_roots)
        with pytest.raises(certification().CertificationRejected):
            certification().read_private_route_evidence(root, manifest, require_attestations=True)


def test_memory_only_current_source_drift_refuses_existing_full_proof_issuance(
    complete_private_probe, certificate_authority, trial_roots, tmp_path, monkeypatch,
):
    import shutil
    from shared.lib.fanout import container_source as source
    root = complete_private_probe[0]
    before = source.release_source_binding()
    native = source._source_root(None)
    copied = tmp_path / "same-release"
    for name in sorted(set(source._source_paths(native)) | set(source._memory_paths(native))):
        destination = copied / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(native / name, destination)
    assert source.release_source_binding(copied) == before
    monkeypatch.setattr(source, "_SOURCE_ROOT", copied)
    certificate = certificate_authority.issue_trial(root, trial_roots)
    assert certificate_authority.verify_trial(certificate, trial_roots, before.digest).passed
    memory = copied / "components/memory/memory_ingress.py"
    memory.write_bytes(memory.read_bytes() + b"\n# actual memory-only source drift\n")
    after = source.release_source_binding()
    assert after.runtime_sha256 == before.runtime_sha256
    assert after.memory_sha256 != before.memory_sha256 and after.modes_sha256 == before.modes_sha256
    with pytest.raises(certification().CertificationRejected):
        certificate_authority.issue_trial(root, trial_roots)


def test_certificate_issuance_reopens_instead_of_trusting_a_typed_result(
    complete_private_probe, certificate_authority, trial_roots,
):
    api = certification()
    root, _, _, manifest = complete_private_probe
    proof = api.read_private_route_evidence(root, manifest, require_attestations=True)
    artifact = root / "raw/claude-initial-stdout.bin"
    artifact.write_bytes(artifact.read_bytes() + b"swapped")
    with pytest.raises(api.CertificationRejected):
        certificate_authority.issue_trial(root, trial_roots)
    with pytest.raises((api.CertificationRejected, TypeError)):
        certificate_authority.issue_trial(proof, trial_roots)


def test_trial_certificate_rechecks_runtime_source_route_and_root_identity(
    complete_private_probe, certificate_authority, trial_roots, certifier, monkeypatch,
):
    root, _, _, _ = complete_private_probe
    certificate = certificate_authority.issue_trial(root, trial_roots)
    assert not certificate_authority.verify_trial(certificate, trial_roots, "f" * 64).passed
    trial_roots[0].rename(trial_roots[0].with_name("original-root"))
    trial_roots[0].mkdir(mode=0o700)
    assert not certificate_authority.verify_trial(certificate, trial_roots, release_source_binding().digest).passed


def _accepted_trial(api, authority, probe_root, roots, certifier):
    trial = authority.issue_trial(probe_root, roots)
    run = roots[0].parent.parent
    from shared.lib.fanout.container_coordinator import _read_trial_preparation
    controller, _ = _read_trial_preparation(run)
    with ArtifactStore(run / "raw") as store:
        checks = {name: {"artifact": asdict(store.write_json(name + ".json", {
            "check": name, "status": "pass", "release_source_binding": release_source_binding().to_dict()}))}
            for name in api.TRIAL_CHECKS}
    payload = {"schema_version": "fanout-synthetic-trial-result-v1", "private_run_root": str(run),
        "trial_certificate": trial.to_dict(), "release_source_binding": release_source_binding().to_dict(),
        "checks": checks}
    name, digest = write_evidence(controller, "container-synthetic-trial-complete", {
        "trial_result_sha256": hashlib.sha256(canonical_json(payload)).hexdigest()})
    payload.update(completion_evidence_name=name, completion_evidence_digest=digest)
    certifier._write_private(run / "trial-result.json", canonical_json(payload))
    return run


def test_release_issuance_requires_authenticated_trial_and_distinct_authority(
    complete_private_probe, certificate_authority, trial_roots, certifier, tmp_path,
):
    api = certification()
    root, _, _, _ = complete_private_probe
    caller = tmp_path / "caller"
    caller.mkdir()
    (caller / "README.md").write_text("caller baseline\n")
    certifier._git_synthetic(caller)
    with pytest.raises(api.CertificationRejected):
        certificate_authority.issue_release(root, (caller,), trial_root=tmp_path / "absent", evaluation_root=tmp_path / "evaluations")
    accepted = _accepted_trial(api, certificate_authority, root, trial_roots, certifier)
    with pytest.raises(api.CertificationRejected):
        certificate_authority.issue_release(root, (caller,), trial_root=accepted, evaluation_root=tmp_path / "evaluations")
    proof = api.read_private_route_evidence(root, require_attestations=True)
    with pytest.raises(api.CertificationRejected, match="accepted trial"):
        certificate_authority._accepted_trial(accepted, proof)
    forged = api.ReleaseCertificate(certificate_authority.issue_trial(root, trial_roots).payload, "0" * 64)
    assert not certificate_authority.verify_release(forged, (caller,), release_source_binding().digest).passed
    assert not certificate_authority.verify_trial(forged, trial_roots, release_source_binding().digest).passed


def test_trial_consumption_does_not_read_or_require_release_signing_key(
    complete_private_probe, certificate_authority, trial_roots,
):
    root, _, _, _ = complete_private_probe
    trial = certificate_authority.issue_trial(root, trial_roots)
    certificate_authority._release_key.unlink()
    assert certificate_authority.verify_trial(trial, trial_roots, release_source_binding().digest).passed
    profile = certificate_authority.admit_trial_profile(trial, trial_roots, "claude",
        source_hash=release_source_binding().digest)
    assert profile.digest == ProviderRegistry.default().select("claude", "repo-write", "standard").digest


def test_certified_linux_admission_keeps_exact_profile_digest_and_global_gate(
    complete_private_probe, certificate_authority, trial_roots,
):
    root, _, _, _ = complete_private_probe
    trial = certificate_authority.issue_trial(root, trial_roots)
    profile = certificate_authority.admit_trial_profile(trial, trial_roots, "codex",
        source_hash=release_source_binding().digest)
    default = ProviderRegistry.default(version_probe=lambda _: "unused").select("codex", "repo-write", "standard")
    assert profile.digest == default.digest
    assert not default.characterized
    with pytest.raises(Exception):
        ProviderRegistry.default(version_probe=lambda _: "unused").admit("codex", "repo-write", "standard")


@pytest.mark.parametrize("change", ["equal_keys", "public_key", "inside_run", "symlink"])
def test_certificate_keys_are_separate_private_owner_files(
    complete_private_probe, certificate_authority, trial_roots, change,
):
    api = certification()
    root, _, _, _ = complete_private_probe
    if change == "equal_keys":
        certificate_authority._release_key.write_bytes(certificate_authority._trial_key.read_bytes())
    elif change == "public_key":
        certificate_authority._trial_key.chmod(0o644)
    elif change == "inside_run":
        key = root / "trial.key"
        key.write_bytes(b"a" * 64)
        key.chmod(0o600)
        certificate_authority._trial_key = key
    else:
        key = certificate_authority._trial_key
        original = key.with_name("original-trial.key")
        key.rename(original)
        key.symlink_to(original)
    with pytest.raises(api.CertificationRejected):
        certificate_authority.issue_trial(root, trial_roots)


def test_public_gate_opens_only_fixed_owner_certificate_paths(tmp_path, monkeypatch):
    api = certification()
    from shared.lib.fanout.providers import ProviderRequestError
    monkeypatch.setattr(Path, "home", classmethod(lambda _: tmp_path))
    request = ProviderRequest("claude", "synthetic", cwd=tmp_path,
        execution_class="repo-write", container_boundary=SimpleNamespace(), certificate_roots=(tmp_path,))
    with pytest.raises(ProviderRequestError, match="release certificate"):
        api.require_owner_held_release_certificate(request)


def test_trial_entry_rejects_caller_root_and_injected_transport_before_runner(
    complete_private_probe, certificate_authority, trial_roots, tmp_path, monkeypatch,
):
    api = certification()
    from shared.lib.fanout import container_boundary
    root, _, _, _ = complete_private_probe
    trial = certificate_authority.issue_trial(root, trial_roots)
    called = []
    monkeypatch.setattr(container_boundary, "run_container_turn", lambda *args: called.append(args))
    monkeypatch.setattr(api, "_owner_authority", lambda: certificate_authority)
    request = ProviderRequest("claude", "synthetic", cwd=tmp_path,
        execution_class="repo-write", container_boundary=SimpleNamespace(), certificate_roots=(tmp_path,))
    with pytest.raises(api.CertificationRejected):
        api.run_trial_turn(request, trial, trial_roots)
    request = replace(request, cwd=trial_roots[0], certificate_roots=trial_roots)
    with pytest.raises(api.CertificationRejected):
        api.run_trial_turn(request, trial, trial_roots)
    assert called == []


def test_issuance_cli_refuses_missing_proof_without_content(tmp_path, capsys):
    module = importlib.import_module("scripts.fanout_release_certificate")
    root = tmp_path / "PRIVATE_CONTENT_MARKER"
    status = module.main(["--issue-trial", "--probe-root", str(root),
        "--synthetic-run-root", str(tmp_path / "trial")])
    captured = capsys.readouterr()
    assert status == 1
    assert "PRIVATE_CONTENT_MARKER" not in captured.out + captured.err
    assert captured.out == ""
    assert "prerequisites" in captured.err


def test_issuance_cli_reopens_complete_proof_and_never_overwrites_certificate(
    complete_private_probe, certificate_authority, trial_roots, capsys, monkeypatch,
):
    api = certification()
    module = importlib.import_module("scripts.fanout_release_certificate")
    root, _, _, _ = complete_private_probe
    run = trial_roots[0].parent.parent
    monkeypatch.setattr(module, "_owner_paths", lambda: (
        certificate_authority._trial_key, certificate_authority._release_key, run / "release.json"))
    args = ["--issue-trial", "--probe-root", str(root), "--synthetic-run-root", str(run)]
    assert module.main(args) == 0
    output = run / "trial-certificate.json"
    original = output.read_bytes()
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert certificate_authority.verify_trial(api.read_certificate(output), trial_roots, release_source_binding().digest).passed
    assert module.main(args) == 1
    assert output.read_bytes() == original
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "prerequisites" in captured.err


@pytest.mark.parametrize("change", ["missing_ledger", "uncertain_ledger", "source", "runtime", "route", "swapped_receipt", "transfer", "export"])
def test_both_issuers_reopen_persisted_drift_before_signing(
    complete_private_probe, certificate_authority, trial_roots, certifier, change,
):
    api = certification()
    root, controller, ledger, _ = complete_private_probe
    receipt = copy.deepcopy(certifier._read_private_receipt(root))
    cell = receipt["providers"]["claude"]["initial"]
    certificate = certificate_authority.issue_trial(root, trial_roots)
    request_id = cell["ledger_requests"][0]["request_id"]
    if change in {"missing_ledger", "uncertain_ledger"}:
        with ledger._locked() as state:
            if change == "missing_ledger":
                state["requests"].pop(request_id)
                state["send_claims"].pop(request_id)
                state["permits"][cell["permit_id"]]["used"] -= 1
            else:
                state["requests"][request_id].update(state="uncertain", response_digest=None)
            ledger._save(state)
    elif change == "source":
        receipt["source_sha256"] = "f" * 64
    elif change == "runtime":
        receipt["host_runtime"]["executable_sha256"] = "f" * 64
    elif change == "route":
        receipt["machine_routes"]["claude"]["digest"] = "f" * 64
    elif change == "swapped_receipt":
        receipt["private_run_root"] = str(root.with_name("another-run"))
    elif change == "transfer":
        path = controller.root / "receipts" / (cell["input_receipt_digest"] + ".json")
        path.write_bytes(path.read_bytes() + b"altered")
    else:
        (Path(cell["candidate_export_root"]) / "README.md").write_bytes(b"changed exported candidate")
    if change in {"source", "runtime", "route", "swapped_receipt"}:
        _reseal_completed_receipt(certifier, root, controller, receipt)
    assert not certificate_authority.verify_trial(certificate, trial_roots, release_source_binding().digest).passed
    with pytest.raises(api.CertificationRejected):
        certificate_authority.issue_trial(root, trial_roots)
    with pytest.raises(api.CertificationRejected):
        certificate_authority.issue_release(root, trial_roots, trial_root=trial_roots[0].parent.parent,
            evaluation_root=root.with_name("absent-evaluations"))


@pytest.mark.parametrize("change", ["permit", "export_turn"])
def test_cell_attestation_recomputes_full_permit_and_export_turn_binding(
    complete_private_probe, certifier, change,
):
    api = certification()
    root, controller, _, manifest = complete_private_probe
    receipt = copy.deepcopy(certifier._read_private_receipt(root))
    cell = receipt["providers"]["claude"]["initial"]
    if change == "permit":
        proof = read_evidence(controller, "container-private-cell-attestation",
            cell["attestation_evidence_name"], cell["attestation_evidence_digest"])
        proof["permit"]["max_requests"] = 3
        name, digest = write_evidence(controller, "container-private-cell-attestation", proof)
        cell.update(attestation_evidence_name=name, attestation_evidence_digest=digest)
    else:
        digest = cell["candidate_export_evidence_digest"]
        exported = read_evidence(controller, "container-candidate-export", digest + ".json", digest)
        exported["turn_digest"] = "f" * 64
        _, digest = write_evidence(controller, "container-candidate-export", exported)
        cell["candidate_export_evidence_digest"] = digest
    _reseal_completed_receipt(certifier, root, controller, receipt)
    with pytest.raises(api.CertificationRejected):
        api.read_private_route_evidence(root, manifest, require_attestations=True)


def test_existing_producer_retains_wire_only_for_exact_authenticated_send(
    complete_private_probe, certifier,
):
    root, controller, ledger, _ = complete_private_probe
    receipt = certifier._read_private_receipt(root)
    cell = receipt["providers"]["claude"]["initial"]
    payload = read_evidence(controller, "container-private-cell-attestation",
        cell["attestation_evidence_name"], cell["attestation_evidence_digest"])
    wire = payload["wire"][0]
    with ArtifactStore.open_existing(root / "raw") as store:
        normal = store.read_bytes(ArtifactRef(**wire["normalized_body"]))
        effective = store.read_bytes(ArtifactRef(**wire["effective_body"]))
        response = store.read_bytes(ArtifactRef(**wire["response_body"]))
    identity = cell["ledger_requests"][0]["identity"]
    observed = {"method": "POST", "provider_path": identity["path"], "normalized_body": normal,
        "effective_body": effective, "upstream_origin": identity["upstream_origin"],
        "upstream_path": identity["upstream_path"], "upstream_model": identity["upstream_model"],
        "upstream_route_digest": identity["upstream_route_digest"], "http_status": 200,
        "content_type": "application/json", "response_body": response, "delivered_body": response}
    with pytest.raises(BrokerRejected):
        certifier._capture_private_wire(ledger, cell["permit_id"], cell["permit_epoch"], root / "raw", "claude-initial", observed)
    request_id = cell["ledger_requests"][0]["request_id"]
    with ledger._locked() as state:
        state["requests"][request_id].update(state="uncertain", response_digest=None)
        ledger._save(state)
    captured = certifier._capture_private_wire(ledger, cell["permit_id"], cell["permit_epoch"], root / "raw", "claude-initial", observed)
    assert captured["request_id"] == request_id
    with ArtifactStore.open_existing(root / "raw") as store:
        assert store.read_bytes(ArtifactRef(**captured["response_body"])) == response
    with pytest.raises(BrokerRejected):
        certifier._capture_private_wire(ledger, cell["permit_id"], cell["permit_epoch"], root / "raw", "claude-resume", {
            **observed, "Authorization": "NEVER_CAPTURE_HEADER"})


@pytest.mark.parametrize("mime,raw", [
    ("application/json", json.dumps(_served_response("codex"), separators=(",", ":")).encode()),
    ("text/event-stream", b'data: ' + json.dumps({"type": "response.completed", "response": _served_response("codex")}, separators=(",", ":")).encode() + b'\n\n'),
])
def test_real_success_sender_records_authenticated_reserved_wire(
    certifier, upstream_module, connection_factory, tmp_path, mime, raw,
):
    _, permit, route = _routed_sender(upstream_module, "codex")
    _, response = connection_factory
    response.headers["content-type"] = mime
    response.chunks = [raw]
    controller = create_lifecycle_controller(tmp_path / "controller")
    ledger = PermitLedger(controller.root / "locks/spend", controller)
    permit_id = ledger.register(permit, 1)
    raw_root = tmp_path / "raw"
    with ArtifactStore(raw_root):
        pass
    captured = []
    sender = HostUpstream(permit, route, max_response_bytes=permit.max_response_bytes, deadline_seconds=2,
        success_sink=lambda payload: captured.append(certifier._capture_private_wire(
            ledger, permit_id, 1, raw_root, "codex-initial", payload)))
    path, _, body = _routed_request("codex")
    ledger.record_attempt(permit_id, 1)
    result = dispatch_once(RelayRequest(permit_id, 1, "actual-send", "POST", path,
        {"x-fanout-seat": permit.placeholder_key, "content-type": "application/json"}, body),
        ledger, Broker(permit, credential="fake-openai-key", send=sender))
    delivered = b"".join(result.body)
    assert delivered == raw
    result.body.acknowledge(hashlib.sha256(delivered).hexdigest())
    record, = ledger.request_records(permit_id, 1)
    assert record["state"] == "settled"
    assert record["response_digest"] == hashlib.sha256(raw).hexdigest()
    assert len(captured) == 1 and captured[0]["request_id"] == "actual-send"
    with ArtifactStore.open_existing(raw_root) as store:
        assert store.read_bytes(ArtifactRef(**captured[0]["response_body"])) == raw


@pytest.mark.parametrize("mime,chunks,expected", [
    ("application/json", [json.dumps(_served_response("claude"), indent=1).encode() + b'\n'],
     json.dumps(_served_response("claude"), separators=(",", ":")).encode()),
    ("text/event-stream", [b'data: ' + json.dumps({"type": "message_start", "message": {**_served_response("claude"), "stop_reason": None}}).encode() + b'\r',
      b'\n\r\ndata: { "type": "message_delta", "delta": { "stop_reason": "tool_use" } }\r\n\r\ndata: { "type": "message_stop" }\r\n\r\n'],
     b'data: ' + json.dumps({"type": "message_start", "message": {**_served_response("claude"), "stop_reason": None}}, separators=(",", ":")).encode() +
     b'\n\ndata: {"type":"message_delta","delta":{"stop_reason":"tool_use"}}\n\ndata: {"type":"message_stop"}\n\n'),
])
def test_real_normalized_delivery_reopens_complete_raw_and_delivered_proofs(
    complete_private_probe, certifier, upstream_module, connection_factory, mime, chunks, expected,
):
    api = certification()
    root, controller, ledger, manifest = complete_private_probe
    receipt = copy.deepcopy(certifier._read_private_receipt(root))
    cell = receipt["providers"]["claude"]["initial"]
    value = read_evidence(controller, "container-private-cell-attestation",
        cell["attestation_evidence_name"], cell["attestation_evidence_digest"])
    permit = BrokerPermit(**value["permit"])
    _, selected = certifier._admit_route_set(Path(receipt["manifest_path"]), Path(receipt["policy_path"]),
        {provider: route["route_id"] for provider, route in receipt["machine_routes"].items()})
    with ArtifactStore.open_existing(root / "raw") as store:
        body = store.read_bytes(ArtifactRef(**value["wire"][0]["normalized_body"]))
    request_id = cell["ledger_requests"][0]["request_id"]
    with ledger._locked() as state:
        state["requests"].pop(request_id)
        state["send_claims"].pop(request_id)
        state["permits"][cell["permit_id"]].update(fenced=False, used=0, attempts=0)
        ledger._save(state)
    _, response = connection_factory
    response.headers["content-type"] = mime
    response.chunks = list(chunks)
    captured = []
    sender = HostUpstream(permit, selected["claude"], max_response_bytes=permit.max_response_bytes, deadline_seconds=2,
        success_sink=lambda payload: captured.append(certifier._capture_private_wire(
            ledger, cell["permit_id"], 1, root / "raw", "normalized-proof", payload)))
    ledger.record_attempt(cell["permit_id"], 1)
    result = dispatch_once(RelayRequest(cell["permit_id"], 1, request_id, "POST", "/v1/messages",
        {"x-api-key": permit.placeholder_key, "content-type": "application/json"}, body),
        ledger, Broker(permit, credential="synthetic-private-value", send=sender))
    delivered = b"".join(result.body)
    assert delivered == expected and delivered != b"".join(chunks)
    result.body.acknowledge(hashlib.sha256(expected).hexdigest())
    ledger.fence_epoch(cell["permit_id"], 1)
    assert len(captured) == 1
    with ArtifactStore.open_existing(root / "raw") as store:
        assert store.read_bytes(ArtifactRef(**captured[0]["response_body"])) == b"".join(chunks)
        assert store.read_bytes(ArtifactRef(**captured[0]["delivered_body"])) == expected
    value["wire"] = captured
    name, digest = write_evidence(controller, "container-private-cell-attestation", value)
    cell.update(attestation_evidence_name=name, attestation_evidence_digest=digest)
    cell["ledger_requests"] = list(ledger.request_records(cell["permit_id"], 1))
    cell["broker_dispositions"][0]["response_sha256"] = hashlib.sha256(expected).hexdigest()
    _reseal_completed_receipt(certifier, root, controller, receipt)
    assert api.read_private_route_evidence(root, manifest, require_attestations=True).attestations_complete
    for kind in ("response_body", "delivered_body"):
        path = root / "raw" / captured[0][kind]["path"]
        original = path.read_bytes()
        path.write_bytes(original + b"changed")
        with pytest.raises(api.CertificationRejected):
            api.read_private_route_evidence(root, manifest, require_attestations=True)
        path.write_bytes(original)


@pytest.mark.parametrize("injected", [False, True])
def test_owner_trial_uses_fixed_docker_execution_and_refuses_replaced_callback(
    complete_private_probe, certificate_authority, trial_roots, monkeypatch, injected, certifier, tmp_path,
):
    import test_fanout_container_boundary as boundary_tests
    from shared.lib.fanout import container_boundary
    from shared.lib.fanout.container_broker import _digest
    from shared.lib.fanout.container_vm import VMProbe
    api = certification()
    proof_root, proof_controller, _, _ = complete_private_probe
    receipt = certifier._read_private_receipt(proof_root)
    workspace, volume = private_volume_names("run-synthetic", "task-plan", "target-one", "seat-claude")
    for name, value in {"WORKSPACE": workspace, "SESSION": volume,
        "SOCKET": "/run/fanout/seat-claude.sock", "CONTAINER_ID": "c" * 64,
        "IMAGE": receipt["image_digest"], "IDENTITY_HASH": workspace.removeprefix("fanout-workspace-")}.items():
        monkeypatch.setattr(runtime_tests, name, value)
    monkeypatch.setattr(boundary_tests, "IMAGE", receipt["image_digest"])
    monkeypatch.setattr(boundary_tests, "CLI", receipt["cli_digests"]["claude"])
    fixture_root = tmp_path / "certified-boundary-fixture"
    fixture_root.mkdir(mode=0o700)
    with monkeypatch.context() as construction:
        construction.setattr(boundary_tests, "ProviderRequest",
            lambda **fields: ProviderRequest(**{**fields, "cwd": trial_roots[0]}))
        request, boundary, transport, _, _, original_callback_calls = boundary_tests._launched_seat(container_boundary, fixture_root)
    request = replace(request, certificate_roots=trial_roots)
    initial = receipt["providers"]["claude"]["initial"]
    attestation = read_evidence(proof_controller, "container-private-cell-attestation",
        initial["attestation_evidence_name"], initial["attestation_evidence_digest"])
    with ArtifactStore.open_existing(proof_root / "raw") as raw:
        vm_observation = json.loads(raw.read_bytes(ArtifactRef(**attestation["vm"])))
    vm = VMProbe(**vm_observation["probe"])
    transport._vm_attestor = lambda: vm
    route = receipt["machine_routes"]["claude"]
    permit = replace(boundary.admission.broker_permit, upstream_route_digest=route["digest"],
        upstream_origin=route["origin"], upstream_model=route["model"], upstream_path=route["path"])
    boundary.admission.ledger.fence_epoch(boundary.permit_id, 1)
    permit_id = boundary.admission.ledger.register(permit, 2)
    admission = replace(boundary.admission, broker_permit=permit, permit_id=permit_id, epoch=2)
    boundary = replace(boundary, admission=admission, admission_identity=id(admission), permit_id=permit_id,
        permit_epoch=2, permit_digest=_digest(permit), upstream_route_digest=route["digest"],
        vm_probe_digest=vm.digest, vm_docker_endpoint=vm.docker_endpoint, vm_ssh_host_key_sha256=vm.ssh_host_key_sha256,
        vm_effective_mounts_sha256=vm.effective_mounts_sha256, vm_guest_mounts_sha256=vm.guest_mounts_sha256)
    name, digest = write_evidence(boundary.controller, "container-seat-admission", container_boundary._payload(boundary))
    boundary = replace(boundary, evidence_name=name, evidence_digest=digest)
    request = replace(request, container_boundary=boundary)
    control = object.__new__(container_boundary.PinnedSSHSeatControl)
    control.vm_observation = vm_observation
    control.controller, control.permit_id, control.epoch, control._lease = boundary.controller, permit_id, 2, None
    transport._control, transport._production_edge = control, True
    certificate = certificate_authority.issue_trial(proof_root, trial_roots)
    assert certificate_authority.verify_trial(certificate, trial_roots, release_source_binding().digest).passed
    api._require_certified_boundary(request, certificate)
    callback_calls, docker_commands, final_admissions = [], [], []
    def callback(command):
        callback_calls.append(command)
        raise RuntimeError("injected callback was reached")
    def docker_io(command, *, before_spawn):
        before_spawn()
        final_admissions.append(command)
        docker_commands.append(command)
        raise RuntimeError("fixed Docker execution was reached")
    monkeypatch.setattr(container_boundary, "_run_bounded_command", docker_io)
    monkeypatch.setattr(api, "_owner_authority", lambda: certificate_authority)
    if injected:
        transport._process_runner = callback
        with pytest.raises(api.CertificationRejected):
            api.run_trial_turn(request, certificate, trial_roots)
        assert docker_commands == []
        assert final_admissions == []
    else:
        with pytest.raises(RuntimeError, match="fixed Docker execution"):
            api.run_trial_turn(request, certificate, trial_roots)
        assert len(docker_commands) == 1
        assert final_admissions == docker_commands
        assert docker_commands[0].argv[:4] == ("docker", "--context", "colima-fanout-isolated", "exec")
        assert boundary.container_id in docker_commands[0].argv
    assert callback_calls == [] and original_callback_calls == []


def test_shared_reader_returns_authenticated_values_without_capability(persisted_probe):
    api = certification()
    root, controller, _, manifest = persisted_probe
    proof = api.read_private_route_evidence(root, manifest)
    assert proof.controller_id == controller.controller_id
    assert proof.receipt_sha256
    assert dict(proof.route_digests).keys() == {"claude", "codex", "agy"}
    assert controller.capability.export_token() not in repr(proof)
    assert controller.capability.export_token() not in str(proof.summary())
    assert controller.capability.export_token() not in repr(asdict(proof))


def test_incomplete_historical_attestations_refuse_issuance(persisted_probe, tmp_path):
    api = certification()
    root, _, _, manifest = persisted_probe
    with pytest.raises(api.CertificationRejected, match="attestation"):
        api.read_private_route_evidence(root, manifest, require_attestations=True)
    trial_key = tmp_path / "trial.key"
    release_key = tmp_path / "release.key"
    trial_key.write_bytes(b"a" * 64)
    release_key.write_bytes(b"b" * 64)
    trial_key.chmod(0o600)
    release_key.chmod(0o600)
    authority = api.CertificateAuthority(trial_key, release_key)
    roots = tuple(root / "targets" / name for name in ("repo-alpha", "repo-beta"))
    for path in roots:
        path.mkdir(parents=True, mode=0o700)
    with pytest.raises(api.CertificationRejected):
        authority.issue_trial(root, roots)
    with pytest.raises(api.CertificationRejected):
        authority.issue_release(root, roots, trial_root=tmp_path / "absent-trial", evaluation_root=tmp_path / "absent-evaluations")


@pytest.mark.parametrize("change", ["receipt", "ledger"])
def test_shared_reader_rejects_persisted_tampering(
    persisted_probe, certifier, change,
):
    api = certification()
    root, _, ledger, manifest = persisted_probe
    if change == "receipt":
        data = (root / "receipt.json").read_bytes()
        (root / "receipt.json").write_bytes(data.replace(b'"passed":true', b'"passed":false', 1))
    else:
        with ledger._locked() as state:
            record = next(iter(state["requests"].values()))
            record["state"] = "uncertain"
            record["response_digest"] = None
            ledger._save(state)
    with pytest.raises(api.CertificationRejected):
        api.read_private_route_evidence(root, manifest)


def test_public_container_dispatch_has_no_caller_authority(tmp_path):
    from shared.lib.fanout.providers import ProviderRequest, ProviderRequestError, run_provider
    request = ProviderRequest("codex", "synthetic", cwd=tmp_path,
                              execution_class="repo-write", container_boundary=object())
    with pytest.raises(ProviderRequestError, match="release certificate"):
        run_provider(request)
    from shared.lib.fanout.providers import _run_provider_with_runner
    with pytest.raises(ProviderRequestError, match="certificate"):
        _run_provider_with_runner(request, runner=lambda _: pytest.fail("injected runner ran"))
