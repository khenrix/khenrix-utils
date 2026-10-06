"""Journal-owned scope fixtures; no installed/full-case or native admission claim."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import sys
import threading
import subprocess
from contextlib import ExitStack
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

SOURCE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(SOURCE), str(SOURCE / "tests")]
from shared.lib.fanout import container_boundary as boundary, container_coordinator as lifecycle, diagnostic, quality_case
from shared.lib.fanout.artifacts import ArtifactStore, canonical_json
from shared.lib.fanout.broker import Broker, BrokerPermit, BrokerRejected
from shared.lib.fanout.container_broker import PermitLedger
from shared.lib.fanout.collaboration import CollaborationCoordinator
from shared.lib.fanout.container_runtime import ContainerLauncher
from shared.lib.fanout.container_source import release_source_binding, runtime_source_digest
from shared.lib.fanout.container_transfer import TransferIdentity, snapshot_manifest
from shared.lib.fanout.container_transfer_transport import (
    QualityProvisioning, _prepare_quality_handoff, admitted_request_digest, issue_input_source_grant,
)
from shared.lib.fanout.container_vm import VMProbe
from shared.lib.fanout.controller import LifecycleError, create_lifecycle_controller, read_evidence, write_evidence
from shared.lib.fanout.errors import CollaborationDurabilityError, ProviderRequestError
from shared.lib.fanout.process import ProcessResult, ProcessStatus
from shared.lib.fanout.providers import ProviderRegistry, ProviderRequest
from shared.lib.fanout.runstate import LocalAnchorAuthority, RunAuthorizationError, RunInputs, RunJournal
from test_fanout_container_runtime import SharedDocker, IMAGE, CLI
from test_fanout_container_transfer_transport import _guest_popen
from test_fanout_container_upstream import connection_factory
from test_fanout_collaboration import _packet, _ExactMemory, ArtifactStore as FixtureStore

def _sha(value):
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _vm_observation():
    from shared.lib.fanout.container_certification import _reopen_vm
    from shared.lib.fanout.container_vm import VMProfileSpec, parse_profile_config, require_vm_probe
    colima, lima = b"arch: x86_64\nvmType: qemu\nmounts: null\n", b"mounts: []\n"
    context = {"Name": "colima-fanout-isolated", "Endpoints": {"docker": {
        "Host": f"unix://{Path.home()}/.colima/fanout-isolated/docker.sock"}}}
    value = {name: base64.b64encode(raw).decode() for name, raw in {
        "status": b'{"status":"Running"}', "context": canonical_json(context), "info": b"27.0\n",
        "colima": colima, "lima": lima, "mounts": b"proc /proc proc rw 0 0\n",
        "nonce": b"n" * 32, "reply": b"n" * 32}.items()}
    value["host_key"] = "ssh-ed25519 " + "A" * 44
    vm = require_vm_probe(VMProfileSpec("fanout-isolated", context["Name"], "x86_64"),
        parse_profile_config(colima.decode(), lima.decode(), context["Name"]), ["proc /proc proc rw 0 0"],
        context["Endpoints"]["docker"]["Host"], b"n" * 32, b"n" * 32,
        ssh_host_key=value["host_key"], pinned_host_key=value["host_key"])
    value["probe"] = asdict(vm)
    value["profile_identity"] = {"schema": 2, "profile": vm.profile, "context": context["Name"],
        "endpoint": vm.docker_endpoint, "ssh_host_key_sha256": vm.ssh_host_key_sha256,
        "profile_dir": {"device": 1, "inode": 2}, "lima_dir": {"device": 1, "inode": 3},
        "profile_config": {"device": 1, "inode": 4, "sha256": hashlib.sha256(colima).hexdigest()},
        "lima_config": {"device": 1, "inode": 5, "sha256": hashlib.sha256(lima).hexdigest()},
        "lima_disk": {"device": 1, "inode": 6},
        "context_metadata_dir": {"path": "/synthetic/context", "device": 1, "inode": 7},
        "context_metadata": {"device": 1, "inode": 8, "sha256": "8" * 64}}
    value["profile_binding"] = copy.deepcopy(value["profile_identity"])
    assert _reopen_vm(value) == vm
    return value, vm


@pytest.fixture
def quality_container_provisioning(tmp_path, monkeypatch):
    """Real local owner authority, registry and importer; only daemon/relay I/O synthetic."""
    profile = ProviderRegistry.default().select("claude", "read-only", "standard")
    inputs = RunInputs(run_id="run-synthetic", compiled_plan_sha256="a" * 64,
        source_sha256="b" * 64, draft_sha256="c" * 64, compiler_sha256="d" * 64,
        parser_sha256="e" * 64, provider_profiles={"claude": profile.digest},
        skill_manifests={"task-plan": "1" * 64}, repo_baseline_sha256="2" * 64)
    repo = tmp_path / "repository"
    repo.mkdir(mode=0o700)
    anchor = LocalAnchorAuthority.bootstrap(tmp_path / "authority", run_root=tmp_path / "run", repo_root=repo)
    journal, owner = RunJournal.create(tmp_path / "run", inputs, anchor_store=anchor)
    controller = create_lifecycle_controller(tmp_path / "controller")
    store = ArtifactStore(tmp_path / "allocation")
    coordinator = CollaborationCoordinator(artifacts=store, journal=journal, owner=owner,
        lifecycle_controller=controller, memory=_ExactMemory(),
        provider_runner=lambda *_args, **_kwargs: pytest.fail("model dispatch"))
    scope = lifecycle._coordinator_lifecycle(coordinator)
    guest_bin = tmp_path / 'guest-bin'
    guest_bin.mkdir(mode=0o700)
    cli_file = guest_bin / 'claude'
    cli_file.write_bytes(b'controlled fixture CLI bytes, never executed\n')
    cli_file.chmod(0o700)
    daemon = SharedDocker()
    from shared.lib.fanout.container_runtime import ContainerSpec, private_volume_names
    spec = ContainerSpec('run-synthetic', 'task-plan', 'target-one', 'seat-claude', IMAGE,
        *private_volume_names('run-synthetic', 'task-plan', 'target-one', 'seat-claude'),
        '/run/fanout/seat-claude.sock', hashlib.sha256(cli_file.read_bytes()).hexdigest())
    spec, first_launcher, first, _ = daemon.seat("seat-claude", "c" * 64, spec=spec)
    ro_spec = replace(spec, workspace_readonly=True)
    _, second_launcher, second, _ = daemon.seat("seat-claude", "9" * 64, spec=ro_spec)
    for mount in second.inspect["Mounts"]:
        if mount["Destination"] == "/workspace":
            mount["RW"] = False
    for mount in second.inspect["HostConfig"]["Mounts"]:
        if mount["Target"] == "/workspace":
            mount["ReadOnly"] = True
    switched = False
    model_launchers = [second_launcher]
    physical_calls = []
    def physical(command, **_kwargs):
        nonlocal switched
        physical_calls.append(command)
        assert command.stdin == b""
        if command.argv[3:4] == ('exec',):
            import subprocess
            result = subprocess.run((sys.executable, '-c', command.argv[-2], command.argv[-1]),
                env={'PATH': str(guest_bin)}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
            return ProcessResult(ProcessStatus.EXIT, result.returncode, result.stdout, result.stderr)
        if command.argv[3] == "create" and "c" * 64 in daemon.items:
            switched = True
        result = (model_launchers[0] if switched else first_launcher)._docker(command.argv, None)
        return ProcessResult(ProcessStatus.EXIT, result.returncode, result.stdout, result.stderr)
    monkeypatch.setattr(boundary, "_run_bounded_command", physical)
    socket = first_launcher._socket_attestor(spec.socket_path)
    request = ProviderRequest("claude", b"original narrow scope fixture question", tmp_path,
        profile=profile, session_id="11111111-1111-4111-8111-111111111111", timeout=30,
        artifact_store=store, artifact_prefix="providers/claude/initial",
        run_id=spec.run_id, task_id=spec.task_id, target_id=spec.target_id,
        seat_id=spec.seat_id, inputs_digest=inputs.digest, context_sha256="a" * 64,
        skill_bundle_sha256="b" * 64, skill_delivery_sha256="8" * 64)
    from test_fanout_container_upstream import _route
    route = _route('claude')
    permit = BrokerPermit("claude", spec.run_id, spec.task_id, spec.target_id, spec.seat_id,
        "initial", profile.requested_model, "inert-test-placeholder", 1, 100_000, 100_000, 8192,
        inputs_digest=inputs.digest, session_id=request.session_id,
        upstream_route_digest=route.digest, upstream_origin=route.profile.origin,
        upstream_model=profile.requested_model, upstream_path='/v1/projects/fictional-project/locations/europe-west1/publishers/anthropic/models/claude-opus-5-5:streamRawPredict')
    ledger = PermitLedger(controller.root / "locks/spend", controller)
    row = {"role": "candidate-seat", "phase": "initial", "condition": "candidate", "epoch": 1,
        "permit": asdict(permit), "blocker": None, "initial": None, "profile": profile.digest,
        "catalog": None, "process_seconds": 30, "outer_seconds": 60}
    resume_permit = replace(permit, route="resume")
    resume_row = {**row, "phase": "resume", "epoch": 2, "permit": asdict(resume_permit),
        "initial": diagnostic.quality_row_identity(row)}
    case_input = store.write_json("quality/input.json", {"schema": "independent-scope-input-v1",
                                                         "question": "original narrow scope fixture question"})
    value = dict.fromkeys(quality_case._FIELDS)
    value.update(schema="fanout-quality-installed-case-attachment-v2", backend="container-read-only",
        container={"image_digest": IMAGE, "cli_digests": dict.fromkeys(("claude", "codex", "agy"), spec.cli_digest),
                   "docker_controls": ["0" * 64 + ".json", "0" * 64]},
        case_id="normal-01", run_id=spec.run_id, task_id=spec.task_id, target_id=spec.target_id,
        condition="candidate", execution_class="read-only", inputs_digest=inputs.digest,
        case_input=asdict(case_input), catalog=None,
        seats={"claude": {"seat_id": spec.seat_id, "workspace_digest": "7" * 64,
                          "profile_digest": profile.digest, "route_id": "scope-fixture"}})
    bindings = dict.fromkeys(("campaign", "casebook", "source", "projection", "profile", "inputs"), "b" * 64)
    bindings.update(source=runtime_source_digest(), profile=profile.digest, inputs=inputs.digest,
                    projection=_sha(quality_case._quality_input_projection(value)))
    allocation = store.write_json("quality/case.json", {"schema": "fanout-quality-case-allocation-v1",
        "case_id": "normal-01", "controller_id": controller.controller_id, "bindings": bindings,
        "maka_output_tokens": 8192, "rows": [row, resume_row, dict(row, role="owner-grade", epoch=3)]})
    authority = diagnostic.seal_quality_allocation(controller, store, allocation, bindings=bindings)
    runner = boundary.quality_docker_runner(controller, allocation_digest=authority[1],
        max_commands=200, command_seconds=5, outer_seconds=60)
    value.update(allocation=list(authority), bindings=bindings)
    value["container"]["docker_controls"] = list(runner.controls)
    attachment = write_evidence(controller, "quality-installed-case-attachment", value)
    permit_id = diagnostic.register_quality_permit(controller, store, authority, ledger,
        role=row["role"], phase=row["phase"], condition=row["condition"], permit=permit,
        epoch=row["epoch"], bindings=bindings, backend_attachment=attachment)
    claim = {"allocation": authority[1], "row": row}
    registration = json.loads((controller.root / "locks" / ("quality-registration-" + _sha(claim))).read_bytes())["authority"]
    retained = diagnostic._retain_quality_request(store, request, "quality/original-request")
    descriptor = store.read_json(retained)
    context = write_evidence(controller, "container-coordinator-context", {
        "schema_version": "fanout-quality-container-context-v1",
        **{key: getattr(request, key) for key in ("run_id", "task_id", "target_id", "seat_id", "inputs_digest")},
        "release_source_binding": release_source_binding().to_dict(), "request": descriptor["controls"],
        "request_material": asdict(retained), "request_digest": admitted_request_digest(request),
        "task_packet_sha256": "5" * 64, "backend_attachment": list(attachment),
        "allocation": list(authority), "bindings": bindings, "registration": registration,
        "store": str(store.root), "row": row})
    control = object.__new__(boundary.PinnedSSHSeatControl)
    control.controller, control.spec, control.ledger = controller, spec, ledger
    control.broker = Broker(permit, credential="inert-test-controller", send=lambda *_: pytest.fail("model send"))
    control.permit_id, control.epoch = permit_id, row["epoch"]
    control._relay_error, control._process, control._thread, control._socket_at_open = None, None, None, None
    stack = ExitStack()
    control._lease = stack.enter_context(boundary.HostSessionLease(controller, permit_id, row["epoch"]))
    launcher = ContainerLauncher(runner, guest_socket_attestor=lambda _: socket)
    control.vm_observation, vm = _vm_observation()
    def start():
        assert scope.reserve_registered(context, spec, ledger, permit, row["epoch"]) == permit_id
        return boundary.PinnedDockerTransport.start(controller, spec, launcher=launcher,
            vm_attestor=lambda: vm, socket_attestor=lambda _: socket, image_attestor=lambda: IMAGE,
            cli_attestor=lambda _: spec.cli_digest, process_runner=lambda _: pytest.fail("model process"),
            lifecycle_scope=scope, context_ref=context, session_control=control)
    sessions = boundary.SessionStore(controller, spec.run_id, spec.task_id, spec.target_id,
                                     spec.seat_id, spec.session_volume)
    frozen_ledger = (ledger.root / "ledger.json").read_bytes()
    frozen_allocation = store.read_bytes(allocation)
    q = SimpleNamespace(controller=controller, journal=journal, owner=owner, inputs=inputs,
        coordinator=coordinator, scope=scope, daemon=daemon, spec=spec, row=row, request=request,
        permit=permit, ledger=ledger, permit_id=permit_id, context=context, registration=registration,
        store=store, authority=authority, bindings=bindings, allocation=allocation,
        attachment=attachment, value=value, runner=runner, control=control, start=start,
        sessions=sessions, physical_calls=physical_calls, backend_attachment=attachment,
        resume_row=resume_row, model_launchers=model_launchers,
        route=route, tmp_path=tmp_path, model_calls=[], frozen_ledger=frozen_ledger,
        frozen_allocation=frozen_allocation, transport=None)
    try:
        yield q
    finally:
        stack.close()
        store.close()
        journal.close()


@pytest.mark.parametrize("operation", ["reservation", "exclusion"])
def test_actual_coordinator_guard_allows_nested_owner_scope_without_unlocking_siblings(tmp_path, operation):
    child = """
import sys
import threading
from pathlib import Path
import pytest
sys.path.insert(0, str(Path.cwd() / "tests"))
from test_fanout_quality_container_turn import quality_container_provisioning
from shared.lib.fanout.collaboration import CollaborationCoordinator

with pytest.MonkeyPatch.context() as monkeypatch:
    fixture = quality_container_provisioning.__wrapped__(Path(sys.argv[1]), monkeypatch)
    q = next(fixture)
    try:
        assert type(q.coordinator) is CollaborationCoordinator
        assert q.scope.guard is q.coordinator._journal_guard
        print("before nested owner scope", flush=True)
        with q.scope.guard:
            if sys.argv[2] == "reservation":
                assert q.scope.reserve_registered(q.context, q.spec, q.ledger, q.permit, q.row["epoch"]) == q.permit_id
                assert (q.permit_id, q.row["epoch"]) in q.scope.pending
                q.scope.authorize()
            else:
                attempted, acquired = threading.Event(), threading.Event()
                failures = []
                def sibling():
                    try:
                        early = q.scope.guard.acquire(blocking=False)
                        if early:
                            q.scope.guard.release()
                        assert not early, "sibling entered the held outer owner scope"
                        attempted.set()
                        with q.scope.guard:
                            acquired.set()
                    except BaseException as error:
                        failures.append(error)
                        attempted.set()
                worker = threading.Thread(target=sibling, daemon=True)
                worker.start()
                assert attempted.wait(2)
                assert not acquired.is_set()
                with q.scope.guard:
                    q.scope.authorize()
                assert not acquired.is_set(), "inner release unlocked the outer owner scope"
        if sys.argv[2] == "exclusion":
            worker.join(2)
            assert not worker.is_alive() and acquired.is_set() and not failures
        assert (q.ledger.root / "ledger.json").read_bytes() == q.frozen_ledger
        assert q.store.read_bytes(q.allocation) == q.frozen_allocation
        assert q.ledger.attempt_counts(q.permit_id, q.row["epoch"]) == (0, 0)
        assert q.ledger.request_records(q.permit_id, q.row["epoch"]) == ()
        assert q.physical_calls == [] and q.model_calls == []
        print("owner scope completed without dispatch", flush=True)
    finally:
        fixture.close()
"""
    try:
        result = subprocess.run((sys.executable, "-B", "-c", child, str(tmp_path), operation),
            cwd=SOURCE, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired as error:
        pytest.fail(f"actual coordinator deadlocked at nested owner scope; child progress: {error.stdout!r}")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "owner scope completed without dispatch" in result.stdout


def _import(q, tmp_path):
    q.transport = q.start()
    q.admission = boundary.ContainerAdmission(q.permit, q.permit_id, q.row["epoch"], q.ledger,
        IMAGE, q.spec.cli_digest, q.transport._socket_attestor(q.spec.socket_path), q.transport, q.sessions,
        q.transport.handle_evidence_name, q.transport.handle_evidence_digest,
        quality_provisioning=QualityProvisioning(q.store.root, q.authority, q.bindings,
            q.row["role"], q.row["phase"], q.row["condition"], backend_attachment=q.attachment))
    source = q.controller.root / "quarantine/original-source"
    source.mkdir(mode=0o700)
    (source / "repo").mkdir(mode=0o700)
    (source / "context").mkdir(mode=0o700)
    (source / "repo/base.txt").write_bytes(b"original baseline\n")
    (source / "context/full-plan.md").write_bytes(b"original scope fixture context\n")
    (source / "context/quality-attachment.json").write_bytes(canonical_json(q.value))
    identity = TransferIdentity(q.spec.run_id, q.spec.task_id, q.spec.target_id, q.spec.seat_id, q.inputs.digest)
    manifest = snapshot_manifest(source, identity)
    with ArtifactStore(q.controller.root / "quarantine/input-material-store") as material_store:
        manifest_ref = material_store.write_json("original/manifest.json", asdict(manifest))
    info = source.stat()
    material = write_evidence(q.controller, "container-input-material", {
        "schema_version": "InputMaterialV1", "identity": asdict(identity),
        "source_device": info.st_dev, "source_inode": info.st_ino,
        "original_handle": asdict(q.transport.handle), "workspace_volume": q.spec.workspace_volume,
        "run_inputs_sha256": q.inputs.digest, "task_packet_sha256": "5" * 64,
        "target_baseline_sha256": "6" * 64, "workspace_sha256": "7" * 64,
        "skill_delivery_sha256": q.request.skill_delivery_sha256,
        "dependency_context_sha256": "9" * 64, "provider_context_sha256": q.request.context_sha256,
        "skill_bundle_sha256": q.request.skill_bundle_sha256, "manifest_ref": asdict(manifest_ref)})
    q.transport._transfer_popen, q.guest = _guest_popen(tmp_path, [], container_id=q.transport.handle.container_id)
    grant = issue_input_source_grant(q.controller, q.request, q.transport, q.admission, source, *material)
    q.receipt = q.transport.stage_inputs(q.request, grant)
    q.admission = replace(q.admission, input_receipt=q.receipt)
    return q.receipt


@pytest.mark.parametrize("quality_container_provisioning", ("claude-container",), indirect=True)
def test_quality_container_real_handoff_cannot_enter_ordinary_writer_turn(quality_container_provisioning):
    from shared.lib.fanout.container_boundary import _issue_container_boundary
    q = quality_container_provisioning
    receipt = _import(q, q.tmp_path)
    model = q.transport.handoff_quality_inputs(q.request, q.admission)
    admission = replace(q.admission, transport=model,
        handle_evidence_name=model.handle_evidence_name, handle_evidence_digest=model.handle_evidence_digest)
    with pytest.raises(ProviderRequestError, match="repo-write"):
        _issue_container_boundary(q.controller, q.request, model.attest_vm(), model.handle, admission)
    assert admission.input_receipt == receipt


@pytest.mark.parametrize("quality_container_provisioning", ("claude-container",), indirect=True)
def test_quality_container_command_refuses_native_backend_before_any_process(quality_container_provisioning):
    from shared.lib.fanout import container_boundary as boundary
    q = quality_container_provisioning
    _import(q, q.tmp_path)
    model = q.transport.handoff_quality_inputs(q.request, q.admission)
    admission = replace(q.admission, transport=model,
        handle_evidence_name=model.handle_evidence_name, handle_evidence_digest=model.handle_evidence_digest)
    foreign = write_evidence(q.controller, "quality-installed-case-attachment", {"schema": "fanout-quality-installed-case-attachment-v1"})
    with pytest.raises(ProviderRequestError, match="backend"):
        boundary.issue_quality_container_command(q.controller, q.store, q.authority,
            request=q.request, admission=admission, backend_attachment=foreign,
            handoff=model.handoff_evidence, original_request=q.request, bindings=q.bindings,
            role=q.row["role"], phase=q.row["phase"], condition=q.row["condition"])
    assert q.model_calls == []
    assert q.admission.ledger.attempt_counts(q.admission.permit_id, 1) == (0, 0)


def _model(q, connection_factory, *, observed_model=True, served_model=True):
    from shared.lib.fanout.container_broker import RelayRequest, dispatch_once
    from test_fanout_container_boundary import _terminal
    from test_fanout_container_certification import _served_response
    _, response = connection_factory
    served = _served_response("claude")
    if not served_model:
        served.pop("model")
    response.chunks = [canonical_json(served)]
    def run(command):
        q.model_calls.append(command)
        q.ledger.record_attempt(q.permit_id, q.row["epoch"])
        body = canonical_json({"model": q.permit.model, "max_tokens": 16,
            "messages": [{"role": "user", "content": q.request.prompt.decode()}], "stream": True, "tools": []})
        reply = dispatch_once(RelayRequest(q.permit_id, q.row["epoch"], f"quality-model-request-{q.row['epoch']}", "POST",
            "/v1/messages", {"x-api-key": q.permit.placeholder_key, "content-type": "application/json"}, body),
            q.ledger, q.control.broker)
        delivered = b"".join(reply.body)
        reply.body.acknowledge(hashlib.sha256(delivered).hexdigest())
        process = _terminal("claude", session_id=q.request.session_id)
        parsed = json.loads(process.stdout)
        if observed_model:
            parsed["modelUsage"] = {q.request.profile.requested_model: {"inputTokens": 1, "outputTokens": 1}}
        return replace(process, stdout=canonical_json(parsed))
    return run


def _turn(q, connection_factory, *, observed_model=True, served_model=True):
    _import(q, q.tmp_path)
    model = q.transport.handoff_quality_inputs(q.request, q.admission)
    model._process_runner = _model(q, connection_factory, observed_model=observed_model, served_model=served_model)
    q.admission = replace(q.admission, transport=model, socket_attestation=model.attest_socket(),
        handle_evidence_name=model.handle_evidence_name, handle_evidence_digest=model.handle_evidence_digest)
    return boundary.run_quality_container_readonly_turn(q.request, controller=q.controller, store=q.store,
        authority=q.authority, admission=q.admission, backend_attachment=q.attachment,
        handoff=model.handoff_evidence, original_request=q.request, bindings=q.bindings,
        role=q.row["role"], phase=q.row["phase"], condition=q.row["condition"],
        route=q.route, credential="inert-test-controller")


def test_quality_container_actual_registered_turn_has_cold_terminal(quality_container_provisioning, connection_factory):
    q = quality_container_provisioning
    turn, proof = _turn(q, connection_factory)
    before = {str(path): path.read_bytes() for path in q.store.root.rglob('*') if path.is_file()}
    retained_request = replace(q.request, artifact_store=q.store)
    reopened = boundary.validate_quality_container_readonly_turn(q.controller, q.store, retained_request, proof,
        backend_attachment=q.attachment)
    boundary.validate_quality_container_readonly_turn(q.controller, q.store, retained_request, proof,
        backend_attachment=q.attachment)
    assert before == {str(path): path.read_bytes() for path in q.store.root.rglob('*') if path.is_file()}
    assert turn.result.valid and turn.result.session_id == q.request.session_id
    assert reopened["main_attempts"] == 1 and reopened["auxiliary_attempts"] == 0
    assert len(q.model_calls) == 1
    assert q.store.read_bytes(q.allocation) == q.frozen_allocation
    assert q.sessions.resume("claude").session_id == q.request.session_id


def test_quality_container_optional_cli_model_uses_actual_served_wire(quality_container_provisioning, connection_factory):
    q = quality_container_provisioning
    turn, proof = _turn(q, connection_factory, observed_model=False)
    assert turn.result.observed_model is None
    boundary.validate_quality_container_readonly_turn(q.controller, q.store, q.request, proof,
        backend_attachment=q.attachment)
    assert len(q.model_calls) == 1


def test_quality_container_actual_wire_must_observe_model(quality_container_provisioning, connection_factory):
    q = quality_container_provisioning
    with pytest.raises(BrokerRejected, match="uncertain"):
        _turn(q, connection_factory, observed_model=False, served_model=False)
    assert len(q.model_calls) == 1
    with q.ledger._locked() as state:
        assert state["permits"][q.permit_id]["fenced"] is True


def _resume(q, proof, monkeypatch, *, claimed_row=None):
    original = q.request
    q.request = replace(original, prompt=b"resume the original own answer", resume=True,
        artifact_prefix="providers/claude/resume")
    q.row = q.resume_row
    q.permit = BrokerPermit(**q.row["permit"])
    q.permit_id = diagnostic.register_quality_permit(q.controller, q.store, q.authority, q.ledger,
        role=q.row["role"], phase=q.row["phase"], condition=q.row["condition"], permit=q.permit,
        epoch=q.row["epoch"], bindings=q.bindings, initial=proof, backend_attachment=q.attachment)
    claim = {"allocation": q.authority[1], "row": q.row}
    registration = json.loads((q.controller.root / "locks" / ("quality-registration-" + _sha(claim))).read_bytes())["authority"]
    retained = diagnostic._retain_quality_request(q.store, q.request, "quality/resume-request")
    descriptor = q.store.read_json(retained)
    context = read_evidence(q.controller, "container-coordinator-context", *q.context)
    context.update(request=descriptor["controls"], request_material=asdict(retained),
        request_digest=admitted_request_digest(q.request), registration=registration, row=q.row)
    q.context = write_evidence(q.controller, "container-coordinator-context", context)
    spec = replace(q.spec, workspace_readonly=True)
    assert q.scope.reserve_registered(q.context, spec, q.ledger, q.permit, q.row["epoch"]) == q.permit_id
    _, launcher, physical, _ = q.daemon.seat("seat-claude", "8" * 64, spec=spec)
    for mount in physical.inspect["Mounts"]:
        if mount["Destination"] == "/workspace":
            mount["RW"] = False
    for mount in physical.inspect["HostConfig"]["Mounts"]:
        if mount["Target"] == "/workspace":
            mount["ReadOnly"] = True
    q.model_launchers[0] = launcher
    from scripts import fanout_container_vm_probe
    monkeypatch.setattr(fanout_container_vm_probe, "_read_pinned_host_key", lambda _: "ssh-ed25519 " + "A" * 44)
    socket = launcher._socket_attestor(spec.socket_path)
    def open_control(control):
        control._lease = boundary.HostSessionLease(q.controller, q.permit_id, q.row["epoch"]).__enter__()
        q.control = control
    def vm(control):
        control.vm_observation, value = _vm_observation()
        return value
    monkeypatch.setattr(boundary.PinnedSSHSeatControl, "open", open_control)
    monkeypatch.setattr(boundary.PinnedSSHSeatControl, "attest_vm", vm)
    monkeypatch.setattr(boundary.PinnedSSHSeatControl, "attest_socket", lambda *_: socket)
    monkeypatch.setattr(boundary.PinnedDockerTransport, "_attest_linux_cli", staticmethod(lambda *_: spec.cli_digest))
    predecessor = diagnostic.QualityContainerPredecessor(proof, q.attachment, q.row["initial"], q.request.session_id)
    if claimed_row is not None:
        predecessor = replace(predecessor, row_digest=claimed_row)
    broker = Broker(q.permit, credential="inert-test-controller", send=lambda *_: pytest.fail("uncaptured send"))
    return boundary.reopen_quality_container_transport(q.request, controller=q.controller, store=q.store,
        authority=q.authority, ledger=q.ledger, permit=q.permit, permit_id=q.permit_id, epoch=q.row["epoch"],
        session_store=q.sessions, broker=broker, backend_attachment=q.attachment,
        handoff=q.admission.transport.handoff_evidence, original_request=original, predecessor=predecessor,
        bindings=q.bindings, lifecycle_scope=q.scope, context_ref=q.context), original, predecessor


def test_quality_container_completed_initial_reopens_exact_own_resume(quality_container_provisioning, connection_factory, monkeypatch):
    q = quality_container_provisioning
    initial, proof = _turn(q, connection_factory)
    q.admission, original, predecessor = _resume(q, proof, monkeypatch)
    run = _model(q, connection_factory)
    def docker_exec(_handle, command, _workdir, *, before_spawn):
        before_spawn()
        return run(command)
    monkeypatch.setattr(boundary.PinnedDockerTransport, "_docker_exec", staticmethod(docker_exec))
    resumed, resume_proof = boundary.run_quality_container_readonly_turn(q.request,
        controller=q.controller, store=q.store, authority=q.authority, admission=q.admission,
        backend_attachment=q.attachment, handoff=q.admission.transport.handoff_evidence,
        original_request=original, bindings=q.bindings, role=q.row["role"], phase=q.row["phase"],
        condition=q.row["condition"], route=q.route, credential="inert-test-controller", predecessor=predecessor)
    boundary.validate_quality_container_readonly_turn(q.controller, q.store, q.request, resume_proof,
        backend_attachment=q.attachment)
    assert resumed.result.session_id == initial.result.session_id
    assert len(q.model_calls) == 2
    assert q.store.read_bytes(q.allocation) == q.frozen_allocation


@pytest.mark.parametrize("field,changed", [("main_attempts", True), ("prepared", [])])
def test_quality_container_resigned_outer_requires_actual_accounting_and_prepared_set(
    quality_container_provisioning, connection_factory, field, changed,
):
    q = quality_container_provisioning
    _, proof = _turn(q, connection_factory)
    value = read_evidence(q.controller, "quality-readonly-turn", *proof)
    altered = write_evidence(q.controller, "quality-readonly-turn", {**value, field: changed})
    with pytest.raises(ProviderRequestError, match="accounting|prepared"):
        boundary.validate_quality_container_readonly_turn(q.controller, q.store, q.request, altered,
            backend_attachment=q.attachment)


def test_quality_container_cold_command_refuses_resigned_material_substitutions(quality_container_provisioning, connection_factory):
    q = quality_container_provisioning
    _, proof = _turn(q, connection_factory)
    outer = read_evidence(q.controller, "quality-readonly-turn", *proof)
    command = read_evidence(q.controller, "quality-container-command", *outer["command"])
    changes = {"canonical_digest": "0" * 64, "guest_digest": "0" * 64,
        "request_digest": "0" * 64, "workdir": "/workspace/unrelated/repo",
        "spec": {**command["spec"], "workspace_readonly": False},
        "registration": {**command["registration"], "row": q.resume_row},
        "predecessor": list(proof), "cli_terminal": ["0" * 64 + ".json", "0" * 64]}
    for field, value in changes.items():
        changed = write_evidence(q.controller, "quality-container-command", {**command, field: value})
        with pytest.raises(ProviderRequestError, match="association"):
            boundary.reopen_quality_container_command(q.controller, q.store, q.request, changed,
                backend_attachment=q.attachment)
    assert len(q.model_calls) == 1


def test_quality_container_actual_storage_failure_before_model_fences_original_row(
    quality_container_provisioning, connection_factory, monkeypatch,
):
    q = quality_container_provisioning
    original = ArtifactStore.write_json
    def write(store, path, value):
        if path.endswith("/vm.json"):
            raise OSError("actual injected durable publication failure")
        return original(store, path, value)
    monkeypatch.setattr(ArtifactStore, "write_json", write)
    with pytest.raises(OSError, match="durable publication"):
        _turn(q, connection_factory)
    assert q.model_calls == []
    assert q.ledger.attempt_counts(q.permit_id, q.row["epoch"]) == (0, 0)
    with q.ledger._locked() as state:
        assert state["permits"][q.permit_id]["fenced"] is True
    assert q.admission.transport.stop_evidence is not None
    q.sessions.assert_clear("claude")


@pytest.mark.parametrize("changed", ["prompt", "profile"])
def test_quality_container_current_request_drift_refused_before_cli_or_model(quality_container_provisioning, changed):
    q = quality_container_provisioning
    _import(q, q.tmp_path)
    model = q.transport.handoff_quality_inputs(q.request, q.admission)
    admission = replace(q.admission, transport=model,
        handle_evidence_name=model.handle_evidence_name, handle_evidence_digest=model.handle_evidence_digest)
    request = replace(q.request, prompt=b"unrelated authenticated-looking prompt") if changed == "prompt" else replace(
        q.request, profile=ProviderRegistry.default().select("claude", "read-only", "deep"))
    before = len(q.physical_calls)
    with pytest.raises(ProviderRequestError, match="prompt|request"):
        boundary.issue_quality_container_command(q.controller, q.store, q.authority, request=request,
            admission=admission, backend_attachment=q.attachment, handoff=model.handoff_evidence,
            original_request=q.request, bindings=q.bindings, role=q.row["role"], phase=q.row["phase"],
            condition=q.row["condition"])
    assert len(q.physical_calls) == before
    assert q.model_calls == []


def test_quality_container_producer_refs_enter_actual_coordinator_terminal(quality_container_provisioning, connection_factory):
    from shared.lib.fanout.collaboration import CollaborationCoordinator, SeatAssignment, TaskPacket
    from shared.lib.fanout.artifacts import ArtifactRef
    q = quality_container_provisioning
    turn, proof = _turn(q, connection_factory)
    with FixtureStore(q.tmp_path / "packet-material") as fixtures:
        fixture = _packet(fixtures, q.tmp_path / "packet-workspace")
    plan, task = json.loads(fixture.compiled_plan), json.loads(fixture.task)
    task["id"] = q.request.task_id
    plan["tasks"] = [task]
    plan_bytes, task_bytes = canonical_json(plan), canonical_json(task)
    packet = TaskPacket(q.request.run_id, q.request.task_id, 1, plan_bytes, hashlib.sha256(plan_bytes).hexdigest(),
        fixture.source_markdown, task_bytes, hashlib.sha256(task_bytes).hexdigest(), fixture.skill_bundle,
        fixture.skill_manifest_sha256, tuple(ArtifactRef(**asdict(ref)) for ref in fixture.dependency_artifacts),
        "read-only", q.request.cwd)
    from shared.lib.fanout.skills import SkillResolver, SkillRoot, SkillLoadEvidence
    resolver = SkillResolver((SkillRoot("test-root", q.tmp_path / "packet-workspace/skill-source", 0),))
    admission = resolver.admit(("test-skill",), task_id=q.request.task_id, seat_id=q.request.seat_id,
        provider="claude", session_id="fixture-admission")
    admission = resolver.stage(admission, q.tmp_path / "coordinator-skill-seat")
    admission = resolver.verify_engine_delivery(admission,
        tuple(SkillLoadEvidence.engine(skill, admission) for skill in admission.skills))
    seat = SeatAssignment.from_admission(admission, skill_bundle=packet.skill_bundle, artifacts=q.store)
    coordinator = CollaborationCoordinator(artifacts=q.store, journal=q.journal, owner=q.owner, memory=_ExactMemory())
    terminal = coordinator._terminal_from_provider(packet, seat, 1, q.request, turn.result)
    assert terminal.valid is True
    for ref, raw in ((turn.result.answer_ref, turn.result.answer.encode()),
                     (turn.result.stdout_ref, turn.process.stdout), (turn.result.stderr_ref, turn.process.stderr)):
        assert ref is not None and ref.path.startswith(q.request.artifact_prefix + "/")
        assert q.store.read_bytes(ref) == raw
    before = {str(path): path.read_bytes() for path in q.store.root.rglob('*') if path.is_file()}
    boundary.validate_quality_container_readonly_turn(q.controller, q.store, q.request, proof,
        backend_attachment=q.attachment)
    assert before == {str(path): path.read_bytes() for path in q.store.root.rglob('*') if path.is_file()}


def test_quality_container_primary_publication_failure_survives_fence_storage_failure(
    quality_container_provisioning, connection_factory, monkeypatch,
):
    q = quality_container_provisioning
    original = ArtifactStore.write_json
    def write(store, path, value):
        if path.endswith("/vm.json"):
            raise OSError("original durable publication failure")
        return original(store, path, value)
    def fence(*_):
        raise OSError("secondary fence storage failure")
    monkeypatch.setattr(ArtifactStore, "write_json", write)
    monkeypatch.setattr(PermitLedger, "fence_epoch", fence)
    with pytest.raises(OSError, match="original durable publication") as captured:
        _turn(q, connection_factory)
    assert any("fence" in note and "OSError" in note for note in captured.value.__notes__)
    assert q.model_calls == []


def test_quality_container_recovery_primary_survives_fence_storage_failure(
    quality_container_provisioning, connection_factory, monkeypatch,
):
    q = quality_container_provisioning
    _, proof = _turn(q, connection_factory)
    def fence(*_):
        raise OSError("secondary recovery fence storage failure")
    monkeypatch.setattr(PermitLedger, "fence_epoch", fence)
    with pytest.raises(ProviderRequestError, match="predecessor") as captured:
        _resume(q, proof, monkeypatch, claimed_row="0" * 64)
    assert any("fence" in note and "OSError" in note for note in captured.value.__notes__)
    assert len(q.model_calls) == 1


def test_quality_container_fence_only_storage_failure_remains_visible(
    quality_container_provisioning, connection_factory, monkeypatch,
):
    q = quality_container_provisioning
    original = PermitLedger.fence_epoch
    fences = 0
    def fence(ledger, permit_id, epoch):
        nonlocal fences
        fences += 1
        if fences == 2:
            raise OSError("sole final fence storage failure")
        return original(ledger, permit_id, epoch)
    monkeypatch.setattr(PermitLedger, "fence_epoch", fence)
    with pytest.raises(OSError, match="sole final fence storage failure"):
        _turn(q, connection_factory)
    assert len(q.model_calls) == 1


@pytest.mark.parametrize("quality_container_provisioning", ("claude-container",), indirect=True)
def test_quality_container_consumed_permit_fenced_on_actual_bounded_prompt_refusal(quality_container_provisioning):
    from shared.lib.fanout.container_boundary import run_quality_container_readonly_turn
    q = quality_container_provisioning
    _import(q, q.tmp_path)
    with pytest.raises(ProviderRequestError, match="bound"):
        run_quality_container_readonly_turn(replace(q.request, prompt=b"x" * (1024 * 1024 + 1)),
            controller=q.controller, store=q.store, authority=q.authority, admission=q.admission,
            backend_attachment=q.backend_attachment, handoff=("unread", "a" * 64),
            original_request=q.request, bindings=q.bindings, role=q.row["role"],
            phase=q.row["phase"], condition=q.row["condition"], route=q.route, credential=object())
    with q.admission.ledger._locked() as state:
        assert state["permits"][q.admission.permit_id]["fenced"] is True
    assert q.admission.ledger.attempt_counts(q.admission.permit_id, 1) == (0, 0)
    assert q.model_calls == []
