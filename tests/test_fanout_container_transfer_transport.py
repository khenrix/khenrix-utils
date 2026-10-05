"""Host transfer tests use a local guest process behind the exact Docker edge."""
from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.lib.fanout.artifacts import ArtifactStore, canonical_json
from shared.lib.fanout.broker import BrokerPermit
from shared.lib.fanout.container_broker import PermitLedger
from shared.lib.fanout.container_runtime import (
    ContainerHandle, ContainerSpec, GuestSocketAttestation, private_volume_names,
)
from shared.lib.fanout.container_transfer import (
    TransferIdentity, snapshot_manifest,
)
from shared.lib.fanout.container_vm import VMProbe
from shared.lib.fanout.controller import (
    create_lifecycle_controller, read_evidence, write_evidence,
)
from shared.lib.fanout.errors import ProviderRequestError
from shared.lib.fanout.process import ProcessResult, ProcessStatus
from shared.lib.fanout.providers import ProviderRegistry, ProviderRequest


IMAGE = "fanout-seat@sha256:" + "a" * 64
CLI = "b" * 64
CONTAINER_ID = "c" * 64


class RunningLauncher:
    def __init__(self, handle):
        self.handle = handle
        self.attestations = 0

    def attest_running(self, spec, handle):
        assert handle == self.handle
        self.attestations += 1
        return self.handle


def _complete_original_inputs(controller, request, spec, handle, permit, ledger,
                              permit_id, epoch, origin, vm, socket, sessions):
    from shared.lib.fanout.container_boundary import ContainerAdmission, PinnedDockerTransport
    from shared.lib.fanout.container_transfer_transport import issue_input_source_grant, _InputClaimJournal
    identity = TransferIdentity(spec.run_id, spec.task_id, spec.target_id, spec.seat_id, request.inputs_digest)
    claim = _InputClaimJournal(controller, identity, spec.workspace_volume)
    if claim._read() is not None:
        return claim.completed()
    transport = PinnedDockerTransport(controller, spec, handle, RunningLauncher(handle),
        lambda: vm, lambda _: socket, lambda: spec.image_digest, lambda _: spec.cli_digest,
        lambda _: None, origin)
    admission = ContainerAdmission(permit, permit_id, epoch, ledger,
        spec.image_digest, spec.cli_digest, socket, transport, sessions, *origin)
    source = controller.root / "quarantine" / f"original-{request.executor_id}-{spec.seat_id}"
    source.mkdir(mode=0o700)
    (source / "repo").mkdir(mode=0o700)
    (source / "context").mkdir(mode=0o700)
    (source / "repo" / "base.txt").write_text("original baseline\n")
    (source / "context" / "full-plan.md").write_text("original complete plan\n")
    manifest = snapshot_manifest(source, identity)
    with ArtifactStore(controller.root / "quarantine" / "input-material-store") as store:
        ref = store.write_json(f"{request.executor_id}-{spec.seat_id}/manifest.json", asdict(manifest))
    info = source.stat()
    material = write_evidence(controller, "container-input-material", {
        "schema_version": "InputMaterialV1", "identity": asdict(identity),
        "source_device": info.st_dev, "source_inode": info.st_ino,
        "original_handle": asdict(handle), "workspace_volume": spec.workspace_volume,
        "run_inputs_sha256": request.inputs_digest, "task_packet_sha256": "5" * 64,
        "target_baseline_sha256": "6" * 64, "workspace_sha256": "7" * 64,
        "skill_delivery_sha256": request.skill_delivery_sha256,
        "dependency_context_sha256": "9" * 64,
        "provider_context_sha256": request.context_sha256,
        "skill_bundle_sha256": request.skill_bundle_sha256, "manifest_ref": asdict(ref),
    })
    grant = issue_input_source_grant(controller, request, transport, admission, source, *material)
    guest = controller.root / "quarantine" / f"guest-{request.executor_id}-{spec.seat_id}"
    guest.mkdir(mode=0o700)
    transport._transfer_popen, _ = _guest_popen(guest, [], container_id=handle.container_id)
    receipt = transport.stage_inputs(request, grant)
    return receipt.evidence_name, receipt.evidence_digest


@pytest.fixture
def seat(tmp_path, request):
    from shared.lib.fanout.container_boundary import (
        ContainerAdmission, PinnedDockerTransport, SessionStore,
    )
    controller = create_lifecycle_controller(tmp_path / "controller")
    workspace_volume, session_volume = private_volume_names(
        "run-one", "task-one", "target-one", "seat-one")
    spec = ContainerSpec("run-one", "task-one", "target-one", "seat-one", IMAGE,
                         workspace_volume, session_volume,
                         "/run/fanout/seat-one.sock", CLI)
    handle = ContainerHandle(CONTAINER_ID, "d" * 64,
                             workspace_volume, session_volume)
    vm = VMProbe("fanout-isolated", "unix:///private/fanout/docker.sock",
                 "1" * 64, "2" * 64, "3" * 64, "4" * 64)
    socket = GuestSocketAttestation(spec.socket_path, 65532,
                                    stat.S_IFSOCK | 0o600, 11, 12, 13)
    provider = getattr(request, "param", "claude")
    session_id = ("11111111-1111-4111-8111-111111111111"
                  if provider == "claude" else None)
    profile = ProviderRegistry.default().select(provider, "repo-write", "standard")
    request = ProviderRequest(
        executor_id=provider, prompt="synthetic", cwd=tmp_path,
        execution_class="repo-write", profile=profile,
        run_id=spec.run_id, task_id=spec.task_id, target_id=spec.target_id,
        seat_id=spec.seat_id, inputs_digest="e" * 64,
        context_sha256="a" * 64, skill_bundle_sha256="b" * 64,
        skill_delivery_sha256="8" * 64,
        session_id=session_id,
    )
    permit = BrokerPermit(
        provider=provider, run_id=spec.run_id, task_id=spec.task_id,
        target_id=spec.target_id, seat_id=spec.seat_id, route="initial",
        model=profile.requested_model, placeholder_key="disposable-placeholder",
        max_requests=2, max_request_bytes=100_000,
        max_response_bytes=100_000, max_output_tokens=1024,
        inputs_digest=request.inputs_digest, session_id=request.session_id,
        upstream_route_digest="a" * 64,
        upstream_origin="https://eu-aiplatform.googleapis.com",
        upstream_model=profile.requested_model,
        upstream_path="/v1/pinned-route",
    )
    ledger = PermitLedger(controller.root / "locks" / "spend", controller)
    permit_id = ledger.register(permit, 1)
    origin = write_evidence(controller, "container-seat-launch", {
        "spec": asdict(spec), "handle": asdict(handle), "route": "start",
        "volumes": __import__("test_fanout_container_runtime")._volumes(spec),
    })
    launcher = RunningLauncher(handle)
    transport = PinnedDockerTransport(
        controller, spec, handle, launcher, lambda: vm, lambda _: socket,
        lambda: IMAGE, lambda _: CLI, lambda _: None, origin,
    )
    transport.close = lambda: setattr(transport, "_quarantined", True)
    admission = ContainerAdmission(
        broker_permit=permit, permit_id=permit_id, epoch=1,
        ledger=ledger, image_digest=IMAGE, cli_digest=CLI,
        socket_attestation=socket, transport=transport,
        session_store=SessionStore(controller, spec.run_id, spec.task_id,
                                   spec.target_id, spec.seat_id, session_volume),
        handle_evidence_name=origin[0], handle_evidence_digest=origin[1],
    )
    source = controller.root / "quarantine" / "source"
    source.mkdir(mode=0o700)
    (source / "repo").mkdir(mode=0o700)
    (source / "context").mkdir(mode=0o700)
    (source / "context" / "skills").mkdir(mode=0o700)
    (source / "context" / "dependencies").mkdir(mode=0o700)
    (source / "repo" / "base.txt").write_text("baseline\n")
    (source / "context" / "full-plan.md").write_text("complete plan\n")
    (source / "context" / "skills" / "manifest.json").write_text('{"skill":"synthetic"}\n')
    (source / "context" / "dependencies" / "peer.json").write_text('{"peer":"verified"}\n')
    identity = TransferIdentity(spec.run_id, spec.task_id, spec.target_id,
                                spec.seat_id, request.inputs_digest)
    manifest = snapshot_manifest(source, identity)
    store_root = controller.root / "quarantine" / "input-material-store"
    with ArtifactStore(store_root) as store:
        manifest_ref = store.write_json("expected/manifest.json", asdict(manifest))
    source_info = source.stat()
    material = {
        "schema_version": "InputMaterialV1",
        "identity": asdict(identity),
        "source_device": source_info.st_dev,
        "source_inode": source_info.st_ino,
        "original_handle": asdict(handle),
        "workspace_volume": workspace_volume,
        "run_inputs_sha256": request.inputs_digest,
        "task_packet_sha256": "5" * 64,
        "target_baseline_sha256": "6" * 64,
        "workspace_sha256": "7" * 64,
        "skill_delivery_sha256": "8" * 64,
        "dependency_context_sha256": "9" * 64,
        "provider_context_sha256": "a" * 64,
        "skill_bundle_sha256": "b" * 64,
        "manifest_ref": asdict(manifest_ref),
    }
    material_ref = write_evidence(controller, "container-input-material", material)
    return (controller, request, transport, admission, source,
            material_ref, manifest, vm, launcher, tmp_path)


def test_initial_grant_retains_actual_complete_original_permit(seat):
    from shared.lib.fanout.container_transfer_transport import issue_input_source_grant
    controller, request, transport, admission, source, material, *_ = seat
    grant = issue_input_source_grant(controller, request, transport, admission, source, *material)
    payload = read_evidence(controller, "container-input-grant", grant.evidence_name, grant.evidence_digest)
    assert payload.get("original_permit") == asdict(admission.broker_permit)
    assert payload["original_permit"]["route"] == "initial"


def _guest_popen(tmp_path, calls, *, mode="normal", container_id=CONTAINER_ID):
    workspace = tmp_path / "guest-workspace"
    workspace.mkdir(mode=0o700, exist_ok=True)

    def popen(argv, **kwargs):
        calls.append(tuple(argv))
        assert tuple(argv[:10]) == (
            "docker", "--context", "colima-fanout-isolated", "exec", "-i",
            "--user", "65532:65532", "--workdir", "/workspace", container_id,
        )
        assert tuple(argv[10:12]) == ("python", "/opt/fanout/container_transfer.py")
        if mode == "wrong-ack" and argv[-1] == "import":
            script = "import sys;sys.stdin.buffer.read();sys.stdout.write('0'*64+'\\n')"
        elif mode == "hang-after-eof":
            script = "import sys,time;sys.stdin.buffer.read();time.sleep(30)"
        elif mode == "bad-export" and argv[-1] == "export":
            script = "import sys;sys.stdin.buffer.read();sys.stdout.buffer.write(b'wrong')"
        else:
            script = (
                "import sys;from pathlib import Path;"
                "from shared.lib.fanout.container_transfer import main;"
                "raise SystemExit(main([sys.argv[2]],workspace=Path(sys.argv[1])))"
            )
        kwargs["cwd"] = Path(__file__).resolve().parents[1]
        return subprocess.Popen(
            [sys.executable, "-c", script, str(workspace), argv[-1]],
            **kwargs,
        )

    return popen, workspace


def test_import_uses_fixed_exec_and_closes_stdin_before_ack(seat):
    from shared.lib.fanout.container_transfer_transport import (
        issue_input_source_grant, stage_inputs,
    )
    controller, request, transport, admission, source, material_ref, manifest, _, launcher, tmp_path = seat
    calls = []
    transport._transfer_popen, workspace = _guest_popen(tmp_path, calls)
    grant = issue_input_source_grant(
        controller, request, transport, admission, source, *material_ref)
    receipt = stage_inputs(controller, request, transport, grant)
    assert calls == [(
        "docker", "--context", "colima-fanout-isolated", "exec", "-i",
        "--user", "65532:65532", "--workdir", "/workspace", CONTAINER_ID,
        "python", "/opt/fanout/container_transfer.py", "import",
    )]
    assert receipt.manifest_sha256 != hashlib.sha256(canonical_json(asdict(manifest))).hexdigest()
    assert receipt.workdir.endswith("/published/repo")
    assert (workspace / receipt.workdir.removeprefix("/workspace/") / "base.txt").read_text() == "baseline\n"
    imported = (workspace / receipt.workdir.removeprefix("/workspace/")).parent / "context"
    assert (imported / "full-plan.md").read_text() == "complete plan\n"
    assert (imported / "skills" / "manifest.json").read_text() == '{"skill":"synthetic"}\n'
    assert (imported / "dependencies" / "peer.json").read_text() == '{"peer":"verified"}\n'
    assert launcher.attestations >= 1


def test_fresh_vm_stdio_challenge_keeps_source_grant_valid(seat):
    from shared.lib.fanout.container_transfer_transport import (
        issue_input_source_grant, stage_inputs,
    )
    controller, request, transport, admission, source, material_ref, _, vm, _, tmp_path = seat
    calls = []
    transport._transfer_popen, _ = _guest_popen(tmp_path, calls)
    grant = issue_input_source_grant(
        controller, request, transport, admission, source, *material_ref)
    transport._vm_attestor = lambda: replace(
        vm, stdio_roundtrip_sha256=hashlib.sha256(b"fresh VM nonce for staging").hexdigest())

    receipt = stage_inputs(controller, request, transport, grant)

    assert receipt.identity == grant.identity
    assert [call[-1] for call in calls] == ["import"]


@pytest.mark.parametrize("field,value", (
    ("profile", "other-profile"),
    ("docker_endpoint", "unix:///private/other/docker.sock"),
    ("effective_mounts_sha256", "0" * 64),
    ("guest_mounts_sha256", "0" * 64),
    ("ssh_host_key_sha256", "0" * 64),
))
def test_changed_stable_vm_field_refuses_source_grant(seat, field, value):
    from shared.lib.fanout.container_transfer_transport import (
        issue_input_source_grant, stage_inputs,
    )
    controller, request, transport, admission, source, material_ref, _, vm, _, tmp_path = seat
    calls = []
    transport._transfer_popen, _ = _guest_popen(tmp_path, calls)
    grant = issue_input_source_grant(
        controller, request, transport, admission, source, *material_ref)
    transport._vm_attestor = lambda: replace(vm, **{field: value})

    with pytest.raises(ProviderRequestError, match="attestation"):
        stage_inputs(controller, request, transport, grant)
    assert calls == []


@pytest.mark.parametrize("malformation", (
    "missing", "null", "extra", "invalid_nonce_digest",
))
def test_malformed_sealed_vm_probe_refuses_source_grant(seat, malformation):
    from shared.lib.fanout.container_transfer_transport import (
        issue_input_source_grant, stage_inputs,
    )
    controller, request, transport, admission, source, material_ref, _, _, _, tmp_path = seat
    calls = []
    transport._transfer_popen, _ = _guest_popen(tmp_path, calls)
    grant = issue_input_source_grant(
        controller, request, transport, admission, source, *material_ref)
    payload = read_evidence(controller, "container-input-grant",
                            grant.evidence_name, grant.evidence_digest)
    if malformation == "missing":
        payload.pop("vm_probe")
    elif malformation == "null":
        payload["vm_probe"] = None
    elif malformation == "extra":
        payload["vm_probe"]["unexpected"] = "field"
    else:
        payload["vm_probe"]["stdio_roundtrip_sha256"] = "not-a-sha256"
    name, digest = write_evidence(controller, "container-input-grant", payload)
    grant = replace(grant, evidence_name=name, evidence_digest=digest)

    with pytest.raises(ProviderRequestError, match="VM|attestation"):
        stage_inputs(controller, request, transport, grant)
    assert calls == []


def test_source_change_or_incomplete_material_refuses_before_exec(seat):
    from shared.lib.fanout.container_transfer_transport import (
        issue_input_source_grant, stage_inputs,
    )
    controller, request, transport, admission, source, material_ref, _, _, _, tmp_path = seat
    calls = []
    transport._transfer_popen, _ = _guest_popen(tmp_path, calls)
    grant = issue_input_source_grant(
        controller, request, transport, admission, source, *material_ref)
    (source / "context" / "full-plan.md").unlink()
    with pytest.raises(ProviderRequestError, match="source|manifest|material"):
        stage_inputs(controller, request, transport, grant)
    assert calls == []


def test_wrong_ack_claims_identity_and_cannot_restage(seat):
    from shared.lib.fanout.container_transfer_transport import (
        issue_input_source_grant, stage_inputs,
    )
    controller, request, transport, admission, source, material_ref, _, _, _, tmp_path = seat
    calls = []
    transport._transfer_popen, _ = _guest_popen(tmp_path, calls, mode="wrong-ack")
    grant = issue_input_source_grant(
        controller, request, transport, admission, source, *material_ref)
    with pytest.raises(ProviderRequestError, match="ACK"):
        stage_inputs(controller, request, transport, grant)
    with pytest.raises(ProviderRequestError, match="claim|pending|restage"):
        stage_inputs(controller, request, transport, grant)
    assert len(calls) == 1


def test_export_closes_identity_stdin_and_seals_distinct_manifest(seat):
    from shared.lib.fanout.container_boundary import _turn_digest, issue_container_boundary
    from shared.lib.fanout.container_transfer_transport import (
        export_candidate, issue_input_source_grant, stage_inputs,
    )
    controller, request, transport, admission, source, material_ref, _, vm, _, tmp_path = seat
    calls = []
    transport._transfer_popen, workspace = _guest_popen(tmp_path, calls)
    grant = issue_input_source_grant(
        controller, request, transport, admission, source, *material_ref)
    input_receipt = stage_inputs(controller, request, transport, grant)
    boundary = issue_container_boundary(
        controller, request, vm, transport.handle,
        replace(admission, input_receipt=input_receipt))
    guest_repo = workspace / input_receipt.workdir.removeprefix("/workspace/")
    (guest_repo / "base.txt").write_text("edited\n")
    export_root = controller.root / "quarantine" / "candidate"
    export_root.mkdir(mode=0o700)
    boundary.session_store.reserve(
        request.executor_id, _turn_digest(request, boundary), initial=True,
        upstream_route_digest=boundary.upstream_route_digest,
        initial_session_id=request.session_id)
    receipt = export_candidate(request, boundary, export_root)
    assert receipt.root == export_root / "published"
    assert (receipt.root / "base.txt").read_text() == "edited\n"
    assert receipt.manifest_sha256 != input_receipt.manifest_sha256
    assert receipt.container_inspect_sha256 == transport.handle.inspect_sha256
    assert receipt.session_volume == transport.handle.session_volume
    assert calls[-1][-1] == "export"


@pytest.mark.parametrize("seat", ("claude", "codex"), indirect=True)
def test_boundary_refuses_missing_input_receipt_before_provider_send(seat):
    from shared.lib.fanout.container_boundary import issue_container_boundary
    controller, request, transport, admission, _, _, _, vm, _, _ = seat
    sent = []
    transport._process_runner = lambda command: sent.append(command)
    with pytest.raises(ProviderRequestError, match="input receipt"):
        issue_container_boundary(controller, request, vm, transport.handle,
                                 admission)
    assert sent == []


@pytest.mark.parametrize("seat", ("codex",), indirect=True)
def test_codex_changed_input_evidence_refuses_before_provider_send(seat):
    from shared.lib.fanout.container_boundary import issue_container_boundary, run_container_turn
    from shared.lib.fanout.container_transfer_transport import (
        issue_input_source_grant, stage_inputs,
    )
    controller, request, transport, admission, source, material_ref, _, vm, _, tmp_path = seat
    transport._transfer_popen, _ = _guest_popen(tmp_path, [])
    grant = issue_input_source_grant(
        controller, request, transport, admission, source, *material_ref)
    receipt = stage_inputs(controller, request, transport, grant)
    boundary = issue_container_boundary(
        controller, request, vm, transport.handle,
        replace(admission, input_receipt=receipt))
    sent = []
    transport._process_runner = lambda command: sent.append(command)

    with pytest.raises(ProviderRequestError):
        run_container_turn(request, replace(boundary, input_receipt_digest="0" * 64))

    assert sent == []


def test_valid_turn_exports_before_exact_stop_and_settle(seat, monkeypatch):
    from shared.lib.fanout.container_boundary import issue_container_boundary, run_container_turn
    from shared.lib.fanout.container_transfer_transport import (
        issue_input_source_grant, stage_inputs,
    )
    controller, request, transport, admission, source, material_ref, _, vm, _, tmp_path = seat
    events = []
    popen, _ = _guest_popen(tmp_path, events)
    transport._transfer_popen = popen
    grant = issue_input_source_grant(
        controller, request, transport, admission, source, *material_ref)
    receipt = stage_inputs(controller, request, transport, grant)
    boundary = issue_container_boundary(
        controller, request, vm, transport.handle,
        replace(admission, input_receipt=receipt))
    stdout = (Path(__file__).resolve().parents[1] /
              "tests/fixtures/fanout_providers/claude-success.json").read_bytes()
    import json
    data = json.loads(stdout)
    data["session_id"] = request.session_id
    transport._process_runner = lambda _: (
        events.append("provider-send") or
        ProcessResult(ProcessStatus.EXIT, 0, json.dumps(data).encode(), b""))
    transport.close = lambda: events.append("exact-stop")
    original_settle = type(boundary.session_store).settle

    def settle(store, *args, **kwargs):
        events.append("settle")
        return original_settle(store, *args, **kwargs)

    monkeypatch.setattr(type(boundary.session_store), "settle", settle)
    result = run_container_turn(request, boundary)
    assert result.valid
    assert result.candidate_export is not None
    assert [item[-1] if isinstance(item, tuple) else item for item in events] == [
        "import", "provider-send", "export", "exact-stop", "settle",
    ]


def test_hung_import_is_reaped_and_claim_remains_pending(seat):
    from shared.lib.fanout.container_transfer_transport import (
        issue_input_source_grant, stage_inputs,
    )
    controller, request, transport, admission, source, material_ref, _, _, _, tmp_path = seat
    calls = []
    transport._transfer_popen, _ = _guest_popen(tmp_path, calls, mode="hang-after-eof")
    transport._transfer_timeout = 0.1
    grant = issue_input_source_grant(
        controller, request, transport, admission, source, *material_ref)
    with pytest.raises(ProviderRequestError, match="timed out"):
        stage_inputs(controller, request, transport, grant)
    assert transport._quarantined
    with pytest.raises(ProviderRequestError, match="restage"):
        stage_inputs(controller, request, transport, grant)


@pytest.mark.parametrize("changed", ("inspect", "image", "cli", "socket", "vm"))
def test_changed_live_state_refuses_before_input_exec(seat, changed):
    from shared.lib.fanout.container_transfer_transport import (
        issue_input_source_grant, stage_inputs,
    )
    controller, request, transport, admission, source, material_ref, _, vm, launcher, tmp_path = seat
    calls = []
    transport._transfer_popen, _ = _guest_popen(tmp_path, calls)
    grant = issue_input_source_grant(
        controller, request, transport, admission, source, *material_ref)
    if changed == "inspect":
        launcher.handle = replace(transport.handle, inspect_sha256="0" * 64)
    elif changed == "image":
        transport._image_attestor = lambda: "changed@sha256:" + "0" * 64
    elif changed == "cli":
        transport._cli_attestor = lambda _: "0" * 64
    elif changed == "socket":
        transport._socket_attestor = lambda _: replace(
            admission.socket_attestation, inode=99)
    else:
        transport._vm_attestor = lambda: replace(vm, guest_mounts_sha256="0" * 64)
    with pytest.raises(ProviderRequestError, match="attestation"):
        stage_inputs(controller, request, transport, grant)
    assert calls == []


def test_material_missing_component_refuses_before_grant(seat):
    from shared.lib.fanout.container_transfer_transport import issue_input_source_grant
    controller, request, transport, admission, source, material_ref, _, _, _, _ = seat
    material = read_evidence(controller, "container-input-material", *material_ref)
    material.pop("task_packet_sha256")
    incomplete = write_evidence(controller, "container-input-material", material)
    with pytest.raises(ProviderRequestError, match="material"):
        issue_input_source_grant(
            controller, request, transport, admission, source, *incomplete)


@pytest.mark.parametrize("field", (
    "context_sha256", "skill_bundle_sha256", "skill_delivery_sha256",
))
def test_material_refuses_changed_request_context_or_skills(seat, field):
    from shared.lib.fanout.container_transfer_transport import issue_input_source_grant
    controller, request, transport, admission, source, material_ref, _, _, _, _ = seat
    assert issue_input_source_grant(
        controller, request, transport, admission, source, *material_ref)
    changed = replace(request, **{field: "0" * 64})
    with pytest.raises(ProviderRequestError, match="material"):
        issue_input_source_grant(
            controller, changed, transport, admission, source, *material_ref)


def test_export_failure_stops_seat_and_does_not_settle(seat):
    from shared.lib.fanout.container_boundary import issue_container_boundary, run_container_turn
    from shared.lib.fanout.container_transfer_transport import (
        issue_input_source_grant, stage_inputs,
    )
    controller, request, transport, admission, source, material_ref, _, vm, _, tmp_path = seat
    calls = []
    transport._transfer_popen, _ = _guest_popen(tmp_path, calls)
    grant = issue_input_source_grant(
        controller, request, transport, admission, source, *material_ref)
    receipt = stage_inputs(controller, request, transport, grant)
    boundary = issue_container_boundary(
        controller, request, vm, transport.handle,
        replace(admission, input_receipt=receipt))
    stdout = (Path(__file__).resolve().parents[1] /
              "tests/fixtures/fanout_providers/claude-success.json").read_text()
    import json
    terminal = json.loads(stdout)
    terminal["session_id"] = request.session_id
    transport._process_runner = lambda _: ProcessResult(
        ProcessStatus.EXIT, 0, json.dumps(terminal).encode(), b"")
    transport._transfer_popen, _ = _guest_popen(tmp_path, calls, mode="bad-export")
    stopped = []
    transport.close = lambda: stopped.append(True)
    with pytest.raises(ProviderRequestError, match="export stream"):
        run_container_turn(request, boundary)
    assert stopped == [True]
    assert [call[-1] for call in calls] == ["import", "export"]
    with pytest.raises(ProviderRequestError, match="uncertain"):
        boundary.session_store.resume("claude")


@pytest.mark.parametrize("seat", ("claude", "codex"), indirect=True)
def test_cold_resume_reopens_original_receipt_with_new_handle(seat):
    from shared.lib.fanout.container_boundary import (
        ContainerAdmission, PinnedDockerTransport, SessionStore,
        issue_container_boundary, run_container_turn,
    )
    from shared.lib.fanout.container_transfer_transport import (
        issue_input_source_grant, stage_inputs,
    )
    controller, request, transport, admission, source, material_ref, _, vm, _, tmp_path = seat
    calls = []
    transport._transfer_popen, workspace = _guest_popen(tmp_path, calls)
    grant = issue_input_source_grant(
        controller, request, transport, admission, source, *material_ref)
    receipt = stage_inputs(controller, request, transport, grant)
    boundary = issue_container_boundary(
        controller, request, vm, transport.handle,
        replace(admission, input_receipt=receipt))
    fixtures = Path(__file__).resolve().parents[1] / "tests/fixtures/fanout_providers"
    if request.executor_id == "codex":
        stdout = (fixtures / "codex-success.ndjson").read_bytes()
        session_id = "codex-thread-1"
    else:
        import json
        terminal = json.loads((fixtures / "claude-success.json").read_text())
        terminal["session_id"] = request.session_id
        stdout = json.dumps(terminal).encode()
        session_id = request.session_id

    def process_runner(turn_request, turn_transport):
        def run(command):
            if turn_request.executor_id == "codex":
                assert turn_transport._model_workdir == receipt.workdir
                guest_repo = workspace / receipt.workdir.removeprefix("/workspace/")
                assert (guest_repo / "base.txt").read_text() == "baseline\n"
                assert all(not (path / ".git").exists()
                           for path in (guest_repo, *guest_repo.parents))
                # Codex 0.159.0 rejects Git-free inputs before starting its client.
                if "--skip-git-repo-check" not in command.argv:
                    return ProcessResult(
                        ProcessStatus.EXIT, 1, b"",
                        b"Not inside a trusted directory and --skip-git-repo-check was not specified.\n",
                    )
                assert command.argv[:5] == (
                    "codex", "exec", "--ignore-user-config", "--strict-config",
                    "--skip-git-repo-check",
                )
                assert command.argv.count("--skip-git-repo-check") == 1
                if turn_request.resume:
                    assert command.argv[5] == "resume"
                overrides_and_route = command.argv[6:] if turn_request.resume else command.argv[5:]
                assert overrides_and_route[:3] == ("-c", 'model_provider="fanout"', "-c")
                assert 'base_url="http://127.0.0.1:43187/v1"' in overrides_and_route[3]
                assert 'x-fanout-seat="disposable-placeholder"' in overrides_and_route[3]
                assert overrides_and_route[4:8] == (
                    "-c", 'approval_policy="never"', "-c", 'approvals_reviewer="user"',
                )
                if turn_request.resume:
                    native_argv = (
                        "codex", "exec", "resume", "-c", 'sandbox_mode="workspace-write"',
                        "-m", "gpt-6.1-sol", "-c", 'model_reasoning_effort="xhigh"',
                        "--json", "codex-thread-1", "-",
                    )
                    routed_argv = (
                        "-c", 'sandbox_mode="danger-full-access"',
                        "-m", "gpt-6.1-sol", "-c", 'model_reasoning_effort="xhigh"',
                        "--json", "codex-thread-1", "-",
                    )
                else:
                    native_argv = (
                        "codex", "exec", "-", "--json", "--sandbox", "workspace-write",
                        "-m", "gpt-6.1-sol", "-c", 'model_reasoning_effort="xhigh"',
                    )
                    routed_argv = (
                        "-", "--json", "--sandbox", "danger-full-access",
                        "-m", "gpt-6.1-sol", "-c", 'model_reasoning_effort="xhigh"',
                    )
                native = ProviderRegistry.default().require("codex").build_command(turn_request)
                assert native.argv == native_argv
                assert overrides_and_route[8:10] == ("-c", "allow_login_shell=false")
                assert overrides_and_route[10:] == routed_argv
                assert command.stdin == turn_request.prompt_bytes
                assert command.cwd == turn_request.cwd
            return ProcessResult(ProcessStatus.EXIT, 0, stdout, b"")
        return run

    transport._process_runner = process_runner(request, transport)
    initial_result = run_container_turn(request, boundary)
    assert initial_result.valid, initial_result.stderr
    assert initial_result.session_id == session_id
    assert initial_result.candidate_export.container_id == transport.handle.container_id
    assert boundary.session_store.resume(request.executor_id).session_id == session_id
    admission.ledger.fence_epoch(admission.permit_id, 1)
    resumed_request = replace(request, resume=True, session_id=session_id, prompt="second turn")
    recovered_handle = replace(transport.handle, container_id="f" * 64,
                               inspect_sha256="0" * 64)
    recovered_spec = transport.spec
    origin = write_evidence(controller, "container-seat-launch", {
        "spec": asdict(recovered_spec), "handle": asdict(recovered_handle),
        "route": "recover",
        "volumes": read_evidence(controller, "container-seat-launch",
                                  receipt.handle_evidence_name, receipt.handle_evidence_digest)["volumes"],
        "original_input": [receipt.evidence_name, receipt.evidence_digest],
        "recovery": {"spec": asdict(recovered_spec), "handle": asdict(recovered_handle), "predecessors": []},
    })
    recovered = PinnedDockerTransport(
        controller, recovered_spec, recovered_handle,
        RunningLauncher(recovered_handle), lambda: vm,
        transport._socket_attestor, lambda: IMAGE, lambda _: CLI,
        lambda _: None, origin,
    )
    recovered.close = lambda: None
    resumed_permit = replace(admission.broker_permit, route="resume", session_id=session_id)
    permit_id = admission.ledger.register(resumed_permit, 2)
    cold_admission = ContainerAdmission(
        broker_permit=resumed_permit, permit_id=permit_id, epoch=2,
        ledger=admission.ledger, image_digest=IMAGE, cli_digest=CLI,
        socket_attestation=admission.socket_attestation,
        transport=recovered,
        session_store=SessionStore(controller, request.run_id, request.task_id,
                                   request.target_id, request.seat_id,
                                   recovered_handle.session_volume),
        handle_evidence_name=origin[0], handle_evidence_digest=origin[1],
    )
    resumed_boundary = issue_container_boundary(
        controller, resumed_request, vm, recovered_handle, cold_admission)
    assert resumed_boundary.input_receipt_digest == receipt.evidence_digest
    assert resumed_boundary.container_id == recovered_handle.container_id
    assert resumed_boundary.container_id != receipt.original_handle.container_id
    recovered._process_runner = process_runner(resumed_request, recovered)
    recovered._transfer_popen, _ = _guest_popen(
        tmp_path, calls, container_id=recovered_handle.container_id)
    resumed_result = run_container_turn(resumed_request, resumed_boundary)
    assert resumed_result.valid, resumed_result.stderr
    assert resumed_result.session_id == session_id
    assert resumed_boundary.session_store.resume(request.executor_id).session_id == session_id
    assert resumed_result.candidate_export.container_id == recovered_handle.container_id
    assert resumed_result.candidate_export.container_inspect_sha256 == recovered_handle.inspect_sha256
    assert [call[-1] for call in calls] == ["import", "export", "export"]


def test_claim_refuses_lost_live_control_lease_before_exec(seat):
    from shared.lib.fanout.container_transfer_transport import (
        issue_input_source_grant, stage_inputs,
    )
    controller, request, transport, admission, source, material_ref, _, _, _, tmp_path = seat
    calls = []
    transport._transfer_popen, _ = _guest_popen(tmp_path, calls)
    grant = issue_input_source_grant(
        controller, request, transport, admission, source, *material_ref)
    transport._control = SimpleNamespace(_lease=None)
    with pytest.raises(ProviderRequestError, match="lease|control"):
        stage_inputs(controller, request, transport, grant)
    assert calls == []


def test_production_edge_cannot_drop_control_and_use_fake_popen(seat):
    from shared.lib.fanout.container_transfer_transport import (
        issue_input_source_grant, stage_inputs,
    )
    controller, request, transport, admission, source, material_ref, _, _, _, tmp_path = seat
    calls = []
    transport._transfer_popen, _ = _guest_popen(tmp_path, calls)
    grant = issue_input_source_grant(
        controller, request, transport, admission, source, *material_ref)
    transport._production_edge = True
    with pytest.raises(ProviderRequestError, match="control|edge"):
        stage_inputs(controller, request, transport, grant)
    assert calls == []


def test_visible_complete_after_failed_persistence_cannot_cold_resume(seat, monkeypatch):
    from shared.lib.fanout import container_transfer_transport as transfer
    controller, request, transport, admission, source, material_ref, _, _, _, tmp_path = seat
    calls = []
    transport._transfer_popen, _ = _guest_popen(tmp_path, calls)
    grant = transfer.issue_input_source_grant(
        controller, request, transport, admission, source, *material_ref)
    original_write = transfer._InputClaimJournal._write
    failed = []

    def fail_after_publish(journal, payload):
        original_write(journal, payload)
        if payload["state"] == "complete" and not failed:
            failed.append(True)
            raise OSError("synthetic directory fsync failure after replace")

    monkeypatch.setattr(transfer._InputClaimJournal, "_write", fail_after_publish)
    with pytest.raises(OSError, match="fsync failure"):
        transfer.stage_inputs(controller, request, transport, grant)
    journal = transfer._InputClaimJournal(controller, grant.identity,
                                          transport.handle.workspace_volume)
    with pytest.raises(ProviderRequestError, match="not complete"):
        journal.completed()
    assert calls[-1][-1] == "import"


def test_boundary_issuance_failure_abandons_completed_input_claim(seat):
    from shared.lib.fanout import container_transfer_transport as transfer
    from shared.lib.fanout.container_boundary import issue_container_boundary
    controller, request, transport, admission, source, material_ref, _, vm, _, tmp_path = seat
    calls = []
    transport._transfer_popen, _ = _guest_popen(tmp_path, calls)
    grant = transfer.issue_input_source_grant(
        controller, request, transport, admission, source, *material_ref)
    receipt = transfer.stage_inputs(controller, request, transport, grant)
    with pytest.raises(ProviderRequestError, match="VM probe"):
        issue_container_boundary(
            controller, request, replace(vm, profile="wrong"),
            transport.handle, replace(admission, input_receipt=receipt))
    journal = transfer._InputClaimJournal(controller, grant.identity,
                                          transport.handle.workspace_volume)
    with pytest.raises(ProviderRequestError, match="not complete"):
        journal.completed()


def test_grant_refuses_prompt_substitution_before_import(seat):
    from shared.lib.fanout.container_transfer_transport import (
        issue_input_source_grant, stage_inputs,
    )
    controller, request, transport, admission, source, material_ref, _, _, _, tmp_path = seat
    calls = []
    transport._transfer_popen, _ = _guest_popen(tmp_path, calls)
    grant = issue_input_source_grant(
        controller, request, transport, admission, source, *material_ref)
    with pytest.raises(ProviderRequestError, match="grant|prompt"):
        stage_inputs(controller, replace(request, prompt="different"),
                     transport, grant)
    assert calls == []


@pytest.mark.parametrize("changed", [None, "receipt", "root", "tree", "stop", "target",
    "recovery", "recovery-missing", "recovery-extra", "recovery-spec", "recovery-handle",
    "start-recovery"])
def test_export_reopen_authenticates_persisted_tree_and_stopped_seat(seat, changed):
    from shared.lib.fanout import container_transfer_transport as transfer
    from shared.lib.fanout.container_boundary import issue_container_boundary, _turn_digest
    from shared.lib.fanout.controller import resume_lifecycle_controller
    controller, request, transport, admission, source, material_ref, _, vm, _, tmp_path = seat
    transport._transfer_popen, workspace = _guest_popen(tmp_path, [])
    grant = transfer.issue_input_source_grant(
        controller, request, transport, admission, source, *material_ref)
    inputs = transfer.stage_inputs(controller, request, transport, grant)
    boundary = issue_container_boundary(
        controller, request, vm, transport.handle, replace(admission, input_receipt=inputs))
    guest_repo = workspace / inputs.workdir.removeprefix("/workspace/")
    (guest_repo / "base.txt").write_text("container edit\n")
    boundary.session_store.reserve(request.executor_id, _turn_digest(request, boundary),
        initial=True, upstream_route_digest=boundary.upstream_route_digest,
        initial_session_id=request.session_id)
    quarantine = controller.root / "quarantine" / "candidate-reopen"
    quarantine.mkdir(mode=0o700)
    exported = transfer.export_candidate(request, boundary, quarantine)
    from shared.lib.fanout.container_runtime import ContainerLauncher
    inspect = [{
        "Id": transport.handle.container_id,
        "Config": {"Labels": ContainerLauncher._labels(transport.spec), "Image": IMAGE},
        "State": {"Running": False}, "HostConfig": {"RestartPolicy": {"Name": "no"}},
        "Mounts": [{"Destination": "/workspace", "Type": "volume", "Name": transport.spec.workspace_volume},
                   {"Destination": "/session", "Type": "volume", "Name": transport.spec.session_volume},
                   {"Destination": "/run/fanout/broker.sock", "Type": "bind", "Source": transport.spec.socket_path}],
    }]
    with ArtifactStore(controller.root / "quarantine" / "runtime-proofs") as proof_store:
        raw_ref = proof_store.write_json(f"stopped/{transport.handle.container_id}.json", inspect)
    launch_name, launch_digest = transport.handle_evidence_name, transport.handle_evidence_digest
    if changed is not None and (changed.startswith("recovery") or changed == "start-recovery"):
        recovery = {"spec": asdict(transport.spec), "handle": asdict(transport.handle), "predecessors": []}
        launch = {"spec": asdict(transport.spec), "handle": asdict(transport.handle),
                  "route": "recover", "recovery": recovery,
                  "volumes": read_evidence(controller, "container-seat-launch",
                                            inputs.handle_evidence_name, inputs.handle_evidence_digest)["volumes"],
                  "original_input": [inputs.evidence_name, inputs.evidence_digest]}
        if changed == "recovery-missing":
            launch.pop("recovery")
        elif changed == "recovery-extra":
            recovery["extra"] = True
        elif changed == "recovery-spec":
            recovery["spec"]["cli_digest"] = "f" * 64
        elif changed == "recovery-handle":
            recovery["handle"]["inspect_sha256"] = "f" * 64
        elif changed == "start-recovery":
            launch["route"] = "start"
        launch_name, launch_digest = write_evidence(controller, "container-seat-launch", launch)
    stop = write_evidence(controller, "container-seat-stopped", {
        "spec": asdict(transport.spec), "handle": asdict(transport.handle),
        "launch_evidence_name": launch_name,
        "launch_evidence_digest": launch_digest,
        "raw_inspect": asdict(raw_ref),
    })
    identity = exported.identity
    if changed == "receipt":
        exported = replace(exported, manifest_sha256="0" * 64)
    elif changed == "root":
        exported = replace(exported, root=source / "repo")
    elif changed == "tree":
        (exported.root / "base.txt").chmod(0o600)
        (exported.root / "base.txt").write_text("swapped bytes\n")
    elif changed == "stop":
        stop = (stop[0], "0" * 64)
    elif changed == "target":
        identity = replace(identity, target_id="other-target")
    cold = resume_lifecycle_controller(controller.root, controller.capability)
    reopen = getattr(transfer, "reopen_candidate_export", None)
    assert callable(reopen), "authenticated exported candidate recovery is missing"
    if changed in (None, "recovery"):
        reopened = reopen(cold, exported, identity=identity,
            turn_digest=_turn_digest(request, boundary), stop_evidence=stop)
        assert (reopened.root / "base.txt").read_text() == "container edit\n"
        assert (source / "repo" / "base.txt").read_text() == "baseline\n"
    else:
        with pytest.raises(ProviderRequestError):
            reopen(cold, exported, identity=identity,
                turn_digest=_turn_digest(request, boundary), stop_evidence=stop)


@pytest.mark.parametrize("changed", [None, "missing", "raw"])
def test_coordinator_fences_only_authenticated_stop_before_resume(seat, changed):
    from shared.lib.fanout import container_coordinator as helper
    from shared.lib.fanout.container_runtime import ContainerLauncher
    controller, request, transport, admission, _, _, _, _, _, _ = seat
    spec, handle = transport.spec, transport.handle
    inspect = [{"Id": handle.container_id, "Config": {"Labels": ContainerLauncher._labels(spec), "Image": spec.image_digest},
        "State": {"Running": False}, "HostConfig": {"RestartPolicy": {"Name": "no"}}, "Mounts": [
        {"Destination": "/workspace", "Type": "volume", "Name": spec.workspace_volume},
        {"Destination": "/session", "Type": "volume", "Name": spec.session_volume},
        {"Destination": "/run/fanout/broker.sock", "Type": "bind", "Source": spec.socket_path}]}]
    with ArtifactStore(controller.root / "quarantine/runtime-proofs") as store:
        raw = store.write_json(f"stopped/{handle.container_id}.json", inspect)
    transport.stop_evidence = write_evidence(controller, "container-seat-stopped", {
        "spec": asdict(spec), "handle": asdict(handle), "raw_inspect": asdict(raw),
        "launch_evidence_name": transport.handle_evidence_name, "launch_evidence_digest": transport.handle_evidence_digest})
    if changed == "missing":
        transport.stop_evidence = None
    elif changed == "raw":
        payload = read_evidence(controller, "container-seat-stopped", *transport.stop_evidence)
        path = controller.root / "quarantine/runtime-proofs" / payload["raw_inspect"]["path"]
        path.write_bytes(b"[]\n")
    fence = getattr(helper, "fence_stopped_turn", None)
    assert callable(fence), "coordinator lacks authenticated stopped epoch fencing"
    resume = replace(admission.broker_permit, route="resume", session_id="synthetic-resume")
    with pytest.raises(Exception):
        admission.ledger.register(resume, 2)
    if changed:
        with pytest.raises(Exception):
            fence(controller, admission.ledger, transport, admission.permit_id, admission.epoch)
        with pytest.raises(Exception):
            admission.ledger.register(resume, 2)
    else:
        fence(controller, admission.ledger, transport, admission.permit_id, admission.epoch)
        next_id = admission.ledger.register(resume, 2)
        assert next_id != admission.permit_id
        assert admission.ledger.attempt_counts(next_id, 2) == (0, 0)
