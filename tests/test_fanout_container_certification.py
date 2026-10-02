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
from shared.lib.fanout.container_boundary import _turn_digest
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


@pytest.fixture
def persisted_probe(certifier, complete_receipt, company_manifest, tmp_path, monkeypatch):
    root, controller, ledger = _completed_quality_root(
        certifier, complete_receipt, tmp_path, monkeypatch)
    return root, controller, ledger, company_manifest[2]


@pytest.fixture
def complete_private_probe(persisted_probe, certifier, tmp_path, monkeypatch):
    """Real HMAC records and full bytes; only Docker/provider I/O is synthetic."""
    root, controller, ledger, manifest = persisted_probe
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
                    session_id=SESSION_IDS[provider], resume=route == "resume",
                    context_sha256=hashlib.sha256(b"complete synthetic plan").hexdigest(),
                    skill_bundle_sha256=hashlib.sha256(b"skills").hexdigest(),
                    skill_delivery_sha256=hashlib.sha256(b"skills").hexdigest())
                identity = TransferIdentity(request.run_id, request.task_id, request.target_id, request.seat_id, request.inputs_digest)
                permit = BrokerPermit(provider, request.run_id, request.task_id, request.target_id,
                    request.seat_id, route, profile.requested_model, "disposable-placeholder",
                    4, 1048576, 8388608, 1024, inputs_digest=request.inputs_digest,
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
                socket = runtime_tests.FakeAttestor().proof
                launcher = ContainerLauncher(docker, guest_socket_attestor=lambda _: socket)
                handle = launcher.start(spec)
                launch_ref = write_evidence(controller, "container-seat-launch", {
                    "spec": asdict(spec), "handle": asdict(handle), "route": "recover" if route == "resume" else "start"})
                observed = []
                original = launcher._run
                def record(*argv):
                    data = original(*argv)
                    observed.append({"argv": list(argv), "stdout": base64.b64encode(data).decode()})
                    return data
                launcher._run = record
                launcher.attest_running(spec, handle)
                launcher._run = original
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
                    grant_ref = write_evidence(controller, "container-input-grant", {
                        "identity": asdict(identity), "source_root": str(source),
                        "source_device": info.st_dev, "source_inode": info.st_ino,
                        "material_evidence_name": material_ref[0], "material_evidence_digest": material_ref[1],
                        "handle_evidence_name": launch_ref[0], "handle_evidence_digest": launch_ref[1],
                        "original_handle": asdict(handle), "vm_probe": asdict(vm), "socket": asdict(socket),
                        "image_digest": spec.image_digest, "cli_digest": spec.cli_digest,
                        "profile_digest": profile.digest, "request_digest": admitted_request_digest(request),
                        "permit_id": permit_id, "permit_epoch": epoch,
                        "permit_digest": hashlib.sha256(canonical_json(asdict(permit))).hexdigest()})
                    input_receipt_ref = write_evidence(controller, "container-input-transfer", {
                        "identity": asdict(identity), "manifest_sha256": hashlib.sha256(encode_manifest(material_manifest)).hexdigest(),
                        "workspace_volume": workspace, "workdir": "/workspace/inputs/" + hashlib.sha256(canonical_json(asdict(identity))).hexdigest() + "/published/repo",
                        "original_handle": asdict(handle), "handle_evidence_name": launch_ref[0],
                        "handle_evidence_digest": launch_ref[1], "material_evidence_name": material_ref[0],
                        "material_evidence_digest": material_ref[1], "grant_evidence_name": grant_ref[0],
                        "grant_evidence_digest": grant_ref[1]})
                    journal = _InputClaimJournal(controller, identity, workspace)
                    grant = SimpleNamespace(evidence_name=grant_ref[0], evidence_digest=grant_ref[1])
                    journal.claim(grant)
                    journal.complete(grant, *input_receipt_ref)
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
                answer = adapter.parse(terminal.stdout).answer.encode()
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
                response = b'{"ok":true}'
                ledger.record_attempt(permit_id, epoch)
                ledger.reserve(permit_id, epoch, request_id, wire_identity)
                ledger.claim_send(permit_id, epoch, request_id, permit, wire_identity)
                ledger.settle(request_id, hashlib.sha256(response).hexdigest())
                ledger.fence_epoch(permit_id, epoch)
                records = list(ledger.request_records(permit_id, epoch))
                stopped = copy.deepcopy(docker.inspect)
                stopped["State"]["Running"] = False
                stopped["State"]["Status"] = "exited"
                with ArtifactStore(controller.root / "quarantine" / "runtime-proofs") as stops:
                    stop_raw = stops.write_json(f"stopped/{handle.container_id}.json", [stopped])
                stopped_ref = write_evidence(controller, "container-seat-stopped", {
                    "spec": asdict(spec), "handle": asdict(handle),
                    "launch_evidence_name": launch_ref[0], "launch_evidence_digest": launch_ref[1], "raw_inspect": asdict(stop_raw)})
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


def test_complete_persisted_attestations_are_reopened(complete_private_probe):
    api = certification()
    root, _, _, manifest = complete_private_probe
    proof = api.read_private_route_evidence(root, manifest, require_attestations=True)
    assert proof.attestations_complete


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
    run = tmp_path / "synthetic-trial"
    run.mkdir(mode=0o700)
    (run / "targets").mkdir(mode=0o700)
    roots = tuple(run / "targets" / name for name in ("repo-alpha", "repo-beta"))
    for root in roots:
        root.mkdir(mode=0o700)
        (root / "README.md").write_text("Disposable synthetic target.\n")
        certifier._git_synthetic(root)
    return roots


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
    capability = LifecycleCapability("synthetic-trial-capability-" + "x" * 40)
    certifier._write_private(run.with_name(run.name + ".controller-capability"), capability.export_token().encode())
    controller = create_lifecycle_controller(run / "controller", capability=capability)
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
    ("application/json", b'{"ok":true}'),
    ("text/event-stream", b'data: {"ok":true}\n\n'),
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
    sender = HostUpstream(permit, route, max_response_bytes=128, deadline_seconds=2,
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
    ("application/json", [b'{ "ok": true }\n'], b'{"ok":true}'),
    ("text/event-stream", [b'data: { "ok": true }\r', b'\n\r\ndata: [DONE]\r\n\r\n'],
     b'data: {"ok":true}\n\ndata: [DONE]\n\n'),
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
    complete_private_probe, certificate_authority, trial_roots, monkeypatch, injected, certifier,
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
    request, boundary, transport, _, _, original_callback_calls = boundary_tests._launched_seat(container_boundary, trial_roots[0])
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
    callback_calls, docker_commands = [], []
    def callback(command):
        callback_calls.append(command)
        raise RuntimeError("injected callback was reached")
    def docker_io(command):
        docker_commands.append(command)
        raise RuntimeError("fixed Docker execution was reached")
    monkeypatch.setattr(container_boundary, "_run_bounded_command", docker_io)
    monkeypatch.setattr(api, "_owner_authority", lambda: certificate_authority)
    if injected:
        transport._process_runner = callback
        with pytest.raises(api.CertificationRejected):
            api.run_trial_turn(request, certificate, trial_roots)
        assert docker_commands == []
    else:
        with pytest.raises(RuntimeError, match="fixed Docker execution"):
            api.run_trial_turn(request, certificate, trial_roots)
        assert len(docker_commands) == 1
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
