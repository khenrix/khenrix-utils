"""Container authority readers; local storage fixtures are not live certification."""
from dataclasses import asdict, replace
import hashlib
import importlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from shared.lib.fanout import diagnostic
from shared.lib.fanout.artifacts import ArtifactStore, canonical_json
from shared.lib.fanout.broker import BrokerPermit
from shared.lib.fanout.container_broker import PermitLedger
from shared.lib.fanout.container_source import runtime_source_digest
from shared.lib.fanout.controller import create_lifecycle_controller, read_evidence, write_evidence
from shared.lib.fanout.container_transfer_transport import QualityProvisioning, _quality_context
from shared.lib.fanout.errors import ProviderRequestError
from shared.lib.fanout.providers import ProviderRegistry

from test_fanout_container_transfer_transport import quality_provisioning
from test_fanout_container_upstream import connection_factory
from test_fanout_quality_container_turn import quality_container_provisioning, _turn, _resume, _model


def _issued_container_workspace(q, seat_id):
    from shared.lib import fanout
    repository = q.tmp_path / ("workspace-baseline-" + seat_id)
    repository.mkdir(mode=0o700)
    subprocess.run(("git", "init", "-q", str(repository)), check=True)
    (repository / "baseline.txt").write_bytes(b"actual local baseline\n")
    subprocess.run(("git", "-C", str(repository), "add", "baseline.txt"), check=True)
    subprocess.run(("git", "-C", str(repository), "-c", "user.name=Test",
        "-c", "user.email=test@example.invalid", "commit", "-qm", "baseline"), check=True)
    baseline = fanout.capture_repository_baseline(repository)
    workspace = fanout.create_seat_workspace(baseline, q.controller.root / "workspaces", seat_id)
    return fanout.verify_seat_workspace(baseline, workspace, controller=q.controller)


def _bind_container_workspace(q):
    from shared.lib.fanout import container_boundary as boundary
    from shared.lib.fanout.container_certification import _reopen_vm
    from shared.lib.fanout.container_runtime import ContainerLauncher, GuestSocketAttestation
    import stat
    verification = _issued_container_workspace(q, q.request.seat_id)
    q.request = replace(q.request, cwd=verification.workspace.root,
        artifact_store=q.store, artifact_prefix="coordinator/actual-initial")
    ref = diagnostic._retain_quality_request(q.store, q.request, "quality/verified-workspace-request")
    descriptor = q.store.read_json(ref)
    context = read_evidence(q.controller, "container-coordinator-context", *q.context)
    context.update(request=descriptor["controls"], request_material=asdict(ref),
        request_digest=diagnostic._quality_request_identity(q.request))
    q.context = write_evidence(q.controller, "container-coordinator-context", context)
    socket = GuestSocketAttestation(q.spec.socket_path, 65532, stat.S_IFSOCK | 0o600, 4, 17, 12345)
    launcher = ContainerLauncher(q.runner, guest_socket_attestor=lambda _: socket)
    vm = _reopen_vm(q.control.vm_observation)
    def start():
        q.scope.reserve_registered(q.context, q.spec, q.ledger, q.permit, q.row["epoch"])
        return boundary.PinnedDockerTransport.start(q.controller, q.spec, launcher=launcher,
            vm_attestor=lambda: vm, socket_attestor=lambda _: socket,
            image_attestor=lambda: q.spec.image_digest, cli_attestor=lambda _: q.spec.cli_digest,
            process_runner=lambda _: pytest.fail("unexpected model process"),
            lifecycle_scope=q.scope, context_ref=q.context, session_control=q.control)
    q.start = start
    return verification


def test_container_genuine_predecessor_and_delivery_join_original_stopped_turn(
        quality_container_provisioning, connection_factory):
    q = quality_container_provisioning
    verification = _bind_container_workspace(q)
    turn, proof = _turn(q, connection_factory)
    resumed = replace(q.request, resume=True, prompt=b"actual subsequent peer prompt")
    marker = q.controller.root / "locks" / f"quality-docker-{q.authority[1]}"
    before = marker.read_bytes()
    predecessor = diagnostic.reopen_quality_predecessor(q.controller, q.store, resumed, proof,
        verification=verification, authority=q.authority, bindings=q.bindings,
        initial_digest=diagnostic.quality_row_identity(q.row), backend_attachment=q.attachment)
    assert predecessor == diagnostic.QualityContainerPredecessor(proof, q.attachment,
        diagnostic.quality_row_identity(q.row), turn.result.session_id)
    value, command, terminal = diagnostic.reopen_quality_container_delivery_turn(
        q.controller, q.store, q.request, proof, authority=q.authority, bindings=q.bindings,
        row=q.row, backend_attachment=q.attachment)
    assert command["registration"]["row"] == q.row
    assert terminal["session_id"] == turn.result.session_id and terminal["stop"]
    assert q.store.read_bytes(diagnostic.ArtifactRef(**value["final"])) == turn.result.answer.encode()
    assert marker.read_bytes() == before


def test_fresh_child_reopens_genuine_container_predecessor_and_delivery_without_launch(
        quality_container_provisioning, connection_factory):
    import os
    q = quality_container_provisioning
    verification = _bind_container_workspace(q)
    _, proof = _turn(q, connection_factory)
    value = read_evidence(q.controller, "quality-readonly-turn", *proof)
    marker = q.controller.root / "locks" / f"quality-docker-{q.authority[1]}"
    before = marker.read_bytes()
    files_before = {path.relative_to(q.store.root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in q.store.root.rglob("*") if path.is_file()}
    payload = {"controller": str(q.controller.root), "capability": q.controller.capability.export_token(),
        "store": str(q.store.root), "request": value["request"], "proof": proof,
        "authority": q.authority, "bindings": q.bindings, "row": q.row,
        "backend_attachment": q.attachment, "workspace": {
            "root": str(verification.workspace.root), "seat_id": verification.workspace.seat_id,
            "baseline_digest": verification.workspace.baseline_digest},
        "verification": verification.evidence_digest}
    child = """
import json, sys
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from shared.lib.fanout import diagnostic, container_boundary
from shared.lib.fanout.artifacts import ArtifactStore, ArtifactRef
from shared.lib.fanout.controller import LifecycleCapability, resume_lifecycle_controller, read_evidence
from shared.lib.fanout.repo import SeatWorkspace, resume_seat_workspace
v = json.load(sys.stdin)
c = resume_lifecycle_controller(Path(v['controller']), LifecycleCapability(v['capability']))
attachment = read_evidence(c, 'quality-installed-case-attachment', *v['backend_attachment'])
controls = read_evidence(c, 'quality-docker-controls', *attachment['container']['docker_controls'])
container_boundary.time = SimpleNamespace(monotonic=lambda: controls['deadline_monotonic'] + 1,
                                         time=lambda: controls['deadline_wall'] + 1)
workspace = SeatWorkspace(Path(v['workspace']['root']), v['workspace']['seat_id'], v['workspace']['baseline_digest'])
verification = resume_seat_workspace(workspace, controller=c, evidence_digest=v['verification'])
with ArtifactStore.open_existing(v['store']) as store:
    original = diagnostic._read_quality_container_request(store, ArtifactRef(**v['request']))
    request = replace(original, resume=True, prompt=b'actual subsequent peer prompt')
    predecessor = diagnostic.reopen_quality_predecessor(c, store, request, tuple(v['proof']),
        verification=verification, authority=tuple(v['authority']), bindings=v['bindings'],
        initial_digest=diagnostic.quality_row_identity(v['row']), backend_attachment=tuple(v['backend_attachment']))
    value, command, terminal = diagnostic.reopen_quality_container_delivery_turn(c, store, original,
        tuple(v['proof']), authority=tuple(v['authority']), bindings=v['bindings'], row=v['row'],
        backend_attachment=tuple(v['backend_attachment']))
    print(json.dumps({'predecessor': asdict(predecessor), 'session': terminal['session_id']}))
"""
    source = str(Path(__file__).resolve().parents[1])
    result = subprocess.run((sys.executable, "-B", "-c", child), input=canonical_json(payload),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=q.tmp_path, timeout=30,
        env={"PATH": os.defpath, "PYTHONPATH": source, "MISE_AUTO_INSTALL": "0", "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode == 0, result.stderr.decode()
    expected = asdict(diagnostic.QualityContainerPredecessor(proof, q.attachment,
        diagnostic.quality_row_identity(q.row), q.request.session_id))
    assert json.loads(result.stdout) == {"predecessor": json.loads(canonical_json(expected)),
                                         "session": q.request.session_id}
    assert marker.read_bytes() == before and len(q.model_calls) == 1
    assert {path.relative_to(q.store.root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in q.store.root.rglob("*") if path.is_file()} == files_before


def test_genuine_container_delivery_independently_rejects_retained_byte_substitution(
        quality_container_provisioning, connection_factory):
    q = quality_container_provisioning
    _, proof = _turn(q, connection_factory)
    value = read_evidence(q.controller, "quality-readonly-turn", *proof)
    def reopen():
        return diagnostic.reopen_quality_container_delivery_turn(q.controller, q.store, q.request, proof,
            authority=q.authority, bindings=q.bindings, row=q.row, backend_attachment=q.attachment)
    for key in ("stdout", "stderr", "final"):
        path = q.store.root / value[key]["path"]
        original = path.read_bytes()
        try:
            path.write_bytes(b"unrelated retained " + key.encode())
            with pytest.raises(ProviderRequestError):
                reopen()
        finally:
            path.write_bytes(original)
        assert reopen()[0] == value
    assert len(q.model_calls) == 1


def test_container_predecessor_refuses_unrelated_genuine_workspace(
        quality_container_provisioning, connection_factory):
    q = quality_container_provisioning
    verification = _issued_container_workspace(q, "foreign-seat")
    _, proof = _turn(q, connection_factory)
    resumed = replace(q.request, resume=True, prompt=b"actual subsequent peer prompt")
    with pytest.raises(ProviderRequestError):
        diagnostic.reopen_quality_predecessor(q.controller, q.store, resumed, proof,
            verification=verification, authority=q.authority, bindings=q.bindings,
            initial_digest=diagnostic.quality_row_identity(q.row), backend_attachment=q.attachment)


def test_genuine_container_predecessor_cannot_switch_original_controls_or_refs(
        quality_container_provisioning, connection_factory):
    q = quality_container_provisioning
    verification = _bind_container_workspace(q)
    _, proof = _turn(q, connection_factory)
    resumed = replace(q.request, resume=True, prompt=b"actual subsequent peer prompt")
    arguments = {"verification": verification, "authority": q.authority, "bindings": q.bindings,
        "initial_digest": diagnostic.quality_row_identity(q.row), "backend_attachment": q.attachment}
    def reopen(request=resumed, pointer=proof, **changes):
        return diagnostic.reopen_quality_predecessor(q.controller, q.store, request, pointer,
            **{**arguments, **changes})
    assert reopen().session_id == q.request.session_id
    for request in (replace(resumed, session_id="22222222-2222-4222-8222-222222222222"),
                    replace(resumed, cwd=q.tmp_path), replace(resumed, seat_id="foreign-seat")):
        with pytest.raises(ProviderRequestError):
            reopen(request)
        assert reopen().session_id == q.request.session_id
    for changes in ({"initial_digest": "f" * 64}, {"backend_attachment": None},
                    {"backend_attachment": ("f" * 64 + ".json", "f" * 64)}):
        with pytest.raises(ProviderRequestError):
            reopen(**changes)
        assert reopen().session_id == q.request.session_id
    value = read_evidence(q.controller, "quality-readonly-turn", *proof)
    terminal = read_evidence(q.controller, "quality-container-terminal", *value["terminal"])
    terminal["stop"] = list(q.transport.stop_evidence)
    changed = write_evidence(q.controller, "quality-container-terminal", terminal)
    substitute = write_evidence(q.controller, "quality-readonly-turn", {**value, "terminal": list(changed)})
    with pytest.raises(ProviderRequestError):
        reopen(pointer=substitute)
    assert reopen().quality_proof == proof and len(q.model_calls) == 1


def test_genuine_container_reader_refuses_changed_source_identity_and_actual_session(
        quality_container_provisioning, connection_factory):
    q = quality_container_provisioning
    _, proof = _turn(q, connection_factory)
    def reopen():
        return diagnostic.reopen_quality_container_delivery_turn(q.controller, q.store, q.request, proof,
            authority=q.authority, bindings=q.bindings, row=q.row, backend_attachment=q.attachment)
    assert reopen()[1]["registration"]["row"] == q.row
    source = q.controller.root / "quarantine/original-source"
    held = q.controller.root / "quarantine/original-source-held"
    source.rename(held)
    source.mkdir(mode=0o700)
    try:
        with pytest.raises(ProviderRequestError):
            reopen()
    finally:
        source.rmdir()
        held.rename(source)
    assert reopen()[1]["registration"]["row"] == q.row
    for path in (source / "repo/base.txt", q.sessions._record):
        original = path.read_bytes()
        try:
            path.write_bytes(b"unrelated original state")
            with pytest.raises(ProviderRequestError):
                reopen()
        finally:
            path.write_bytes(original)
        assert reopen()[1]["registration"]["row"] == q.row
    assert len(q.model_calls) == 1


def test_genuine_container_resume_uses_original_registration_and_cold_delivery(
        quality_container_provisioning, connection_factory, monkeypatch):
    from shared.lib.fanout import container_boundary as boundary
    q = quality_container_provisioning
    initial_request, initial_row = q.request, q.row
    initial, proof = _turn(q, connection_factory)
    allocation = diagnostic.reopen_quality_allocation(q.controller, q.store, q.authority, bindings=q.bindings)
    resolved = diagnostic._quality_resolved_permit(q.controller, q.store, q.authority, allocation,
        q.resume_row, proof, backend_attachment=q.attachment)
    assert asdict(resolved) == q.resume_row["permit"]
    q.admission, original, predecessor = _resume(q, proof, monkeypatch)
    assert original == initial_request and predecessor.quality_proof == proof
    run = _model(q, connection_factory)
    def docker_exec(_handle, command, _workdir, *, before_spawn):
        before_spawn()
        return run(command)
    monkeypatch.setattr(boundary.PinnedDockerTransport, "_docker_exec", staticmethod(docker_exec))
    resumed, resumed_proof = boundary.run_quality_container_readonly_turn(q.request,
        controller=q.controller, store=q.store, authority=q.authority, admission=q.admission,
        backend_attachment=q.attachment, handoff=q.admission.transport.handoff_evidence,
        original_request=original, bindings=q.bindings, role=q.row["role"], phase=q.row["phase"],
        condition=q.row["condition"], route=q.route, credential="inert-test-controller", predecessor=predecessor)
    marker = q.controller.root / "locks" / f"quality-docker-{q.authority[1]}"
    before = marker.read_bytes()
    for request, pointer, row in ((initial_request, proof, initial_row), (q.request, resumed_proof, q.row)):
        value, command, terminal = diagnostic.reopen_quality_container_delivery_turn(q.controller,
            q.store, request, pointer, authority=q.authority, bindings=q.bindings,
            row=row, backend_attachment=q.attachment)
        assert command["registration"]["row"] == row
        assert terminal["session_id"] == initial.result.session_id == resumed.result.session_id
        assert value["main_attempts"] == 1
    with pytest.raises(ProviderRequestError):
        diagnostic.register_quality_permit(q.controller, q.store, q.authority, q.ledger,
            role=q.row["role"], phase=q.row["phase"], condition=q.row["condition"], permit=q.permit,
            epoch=q.row["epoch"], bindings=q.bindings, initial=proof, backend_attachment=q.attachment)
    with q.ledger._locked() as state:
        assert len(state["permits"]) == 2 and all(entry["fenced"] for entry in state["permits"].values())
    assert marker.read_bytes() == before
    assert q.store.read_bytes(q.allocation) == q.frozen_allocation and len(q.model_calls) == 2


@pytest.fixture
def backend_registration(tmp_path, request):
    from shared.lib.fanout import quality_case, container_boundary
    controller = create_lifecycle_controller(tmp_path / "controller")
    store = ArtifactStore(tmp_path / "artifacts")
    ledger = PermitLedger(controller.root / "locks/spend", controller)
    profile = ProviderRegistry.default().select("claude", "read-only", "standard")
    permit = BrokerPermit("claude", "run", "task", "target", "seat", "initial",
        profile.requested_model, "disposable-placeholder", 1, 1024, 2048, 8192,
        inputs_digest="a" * 64, session_id="11111111-1111-4111-8111-111111111111")
    row = {"role": getattr(request, "param", "candidate-seat"), "phase": "initial", "condition": "candidate",
        "epoch": 1, "permit": asdict(permit), "blocker": None, "initial": None,
        "profile": profile.digest, "catalog": None, "process_seconds": 30, "outer_seconds": 60}
    value = dict.fromkeys(quality_case._FIELDS)
    value.update(schema="fanout-quality-installed-case-attachment-v2", backend="container-read-only",
        case_id="normal-01", run_id="run", task_id="task", target_id="target",
        condition="candidate", execution_class="read-only", inputs_digest="a" * 64,
        case_input=asdict(store.write_bytes("case-input.json", b'{"prompt":"original question"}\n')),
        catalog=None, seats={"claude": {"seat_id": "seat", "workspace_digest": "b" * 64,
            "profile_digest": profile.digest, "route_id": "claude-eu"}},
        container={"image_digest": "fanout-seat@sha256:" + "c" * 64,
            "cli_digests": {provider: "d" * 64 for provider in ("claude", "codex", "agy")},
            "docker_controls": ["e" * 64 + ".json", "e" * 64]})
    bindings = {name: "b" * 64 for name in
        ("campaign", "casebook", "source", "projection", "profile", "inputs")}
    bindings.update(source=runtime_source_digest(), inputs=permit.inputs_digest,
        projection=hashlib.sha256(canonical_json(quality_case._quality_input_projection(value))).hexdigest())
    ref = store.write_json("allocation.json", {"schema": "fanout-quality-case-allocation-v1",
        "case_id": value["case_id"], "controller_id": controller.controller_id,
        "bindings": bindings, "maka_output_tokens": 8192, "rows": [row]})
    authority = diagnostic.seal_quality_allocation(controller, store, ref, bindings=bindings)
    runner = container_boundary.quality_docker_runner(controller, allocation_digest=authority[1],
        max_commands=2, command_seconds=5, outer_seconds=60)
    value.update(allocation=list(authority), bindings=bindings)
    value["container"]["docker_controls"] = list(runner.controls)
    pointer = write_evidence(controller, "quality-installed-case-attachment", value)
    yield SimpleNamespace(**locals())
    store.close()


def _register_backend(f, **changes):
    arguments = dict(role=f.row["role"], phase=f.row["phase"], condition=f.row["condition"],
        permit=f.permit, epoch=f.row["epoch"], bindings=f.bindings, backend_attachment=f.pointer)
    arguments.update(changes)
    return diagnostic.register_quality_permit(f.controller, f.store, f.authority, f.ledger, **arguments)


def test_container_registration_binds_actual_attachment_before_consumption(backend_registration):
    from shared.lib.fanout.container_certification import _read_owned_json
    f = backend_registration
    permit_id = _register_backend(f)
    claim = {"allocation": f.authority[1], "row": f.row}
    digest = hashlib.sha256(canonical_json(claim)).hexdigest()
    pointer, _ = _read_owned_json(f.controller.root / "locks" / f"quality-registration-{digest}")
    registration = read_evidence(f.controller, "quality-permit-registration", *pointer["authority"])
    assert registration == {**claim, "permit_id": permit_id, "epoch": 1, "permit": asdict(f.permit),
        "initial_receipt": None, "backend_attachment": list(f.pointer)}
    with pytest.raises(ProviderRequestError, match="consumed"):
        _register_backend(f)
    assert not list((f.controller.root / "locks").glob("quality-docker-*-attempt-*"))


@pytest.mark.parametrize("failure_at", ("registration", "pointer", "pointer-fsync"))
def test_container_registration_publication_failure_fences_its_actual_permit(
        backend_registration, monkeypatch, failure_at):
    f = backend_registration
    claim = {"allocation": f.authority[1], "row": f.row}
    digest = hashlib.sha256(canonical_json(claim)).hexdigest()
    path = f.controller.root / "locks" / f"quality-registration-{digest}"
    failure = OSError("inert publication failure")
    actual_write, actual_open, actual_fsync = diagnostic.write_evidence, diagnostic.os.open, diagnostic.os.fsync
    pointer_fd = None
    failed = False

    def write(controller, category, value):
        if failure_at == "registration" and category == "quality-permit-registration":
            raise failure
        return actual_write(controller, category, value)

    def open_pointer(name, flags, *args, **kwargs):
        nonlocal pointer_fd, failed
        if name == path:
            if failure_at == "pointer" and not failed:
                failed = True
                raise failure
            pointer_fd = actual_open(name, flags, *args, **kwargs)
            return pointer_fd
        return actual_open(name, flags, *args, **kwargs)

    def fsync(fd):
        nonlocal failed
        if failure_at == "pointer-fsync" and fd == pointer_fd and not failed:
            failed = True
            raise failure
        return actual_fsync(fd)

    monkeypatch.setattr(diagnostic, "write_evidence", write)
    monkeypatch.setattr(diagnostic.os, "open", open_pointer)
    monkeypatch.setattr(diagnostic.os, "fsync", fsync)
    with pytest.raises(OSError) as observed:
        _register_backend(f)
    assert observed.value is failure
    assert (f.controller.root / "locks" / f"quality-row-{digest}").read_bytes() == canonical_json(claim)
    with f.ledger._locked() as state:
        assert len(state["permits"]) == 1
        permit_id, entry = next(iter(state["permits"].items()))
        assert entry["fenced"] is True
        assert entry["epoch"] == f.row["epoch"] and entry["attempts"] == 0 and entry["used"] == 0
        assert state["requests"] == {}
    with pytest.raises(ProviderRequestError, match="consumed"):
        _register_backend(f)
    with f.ledger._locked() as state:
        assert set(state["permits"]) == {permit_id}


def test_container_registration_retains_primary_if_fencing_storage_also_fails(
        backend_registration, monkeypatch):
    f = backend_registration
    failure = OSError("inert primary publication failure")
    attempted = []
    actual_write = diagnostic.write_evidence
    def write(controller, category, value):
        if category == "quality-permit-registration":
            raise failure
        return actual_write(controller, category, value)
    def fence(ledger, permit_id, epoch):
        assert ledger is f.ledger
        attempted.append((permit_id, epoch))
        raise OSError("inert secondary fencing failure")
    monkeypatch.setattr(diagnostic, "write_evidence", write)
    monkeypatch.setattr(PermitLedger, "fence_epoch", fence)
    with pytest.raises(OSError) as observed:
        _register_backend(f)
    assert observed.value is failure
    assert observed.value.__notes__ == ["quality original permit fencing also failed"]
    with f.ledger._locked() as state:
        permit_id = next(iter(state["permits"]))
        assert len(state["permits"]) == 1 and state["permits"][permit_id]["fenced"] is False
    assert attempted == [(permit_id, f.row["epoch"])]
    with pytest.raises(ProviderRequestError, match="consumed"):
        _register_backend(f)


@pytest.mark.parametrize("change", ("native", "allocation", "bindings", "run", "task", "target",
    "inputs", "provider-seat", "profile", "backend", "extra", "foreign-controls"))
def test_container_registration_refuses_foreign_actual_attachment_before_claim(backend_registration, change):
    f = backend_registration
    value = read_evidence(f.controller, "quality-installed-case-attachment", *f.pointer)
    if change == "native":
        value["schema"] = "fanout-quality-installed-case-attachment-v1"
        value.pop("backend")
        value.pop("container")
    elif change == "allocation":
        value["allocation"] = ["f" * 64 + ".json", "f" * 64]
    elif change == "bindings":
        value["bindings"]["campaign"] = "f" * 64
    elif change in {"run", "task", "target"}:
        value[change + "_id"] = "foreign"
    elif change == "inputs":
        value["inputs_digest"] = "f" * 64
    elif change == "provider-seat":
        value["seats"]["claude"]["seat_id"] = "foreign-seat"
    elif change == "profile":
        value["seats"]["claude"]["profile_digest"] = "f" * 64
    elif change == "backend":
        value["backend"] = "native"
    elif change == "extra":
        value["runner"] = "foreign"
    else:
        foreign = f.container_boundary.quality_docker_runner(f.controller, allocation_digest="f" * 64,
            max_commands=2, command_seconds=5, outer_seconds=60)
        value["container"]["docker_controls"] = list(foreign.controls)
    pointer = write_evidence(f.controller, "quality-installed-case-attachment", value)
    with pytest.raises(ProviderRequestError):
        _register_backend(f, backend_attachment=pointer)
    assert not list((f.controller.root / "locks").glob("quality-row-*"))
    with f.ledger._locked() as state:
        assert state["permits"] == {}


@pytest.mark.parametrize("change", ("raw", "pending", "exhausted", "monotonic", "wall", "marker"))
def test_container_registration_reopens_original_finite_controls_before_claim(backend_registration, monkeypatch, change):
    f = backend_registration
    api = f.container_boundary
    calls = []
    def inert(command, **kwargs):
        from shared.lib.fanout.process import ProcessResult, ProcessStatus
        calls.append(command)
        actual = subprocess.run((sys.executable, "-B", "-c", "print('inert control result')"),
                                capture_output=True, timeout=command.timeout)
        return ProcessResult(ProcessStatus.EXIT, actual.returncode, actual.stdout, actual.stderr)
    monkeypatch.setattr(api, "_run_bounded_command", inert)
    if change in {"raw", "pending", "exhausted"}:
        f.runner((*api.DOCKER, "container", "ls", "-a", "--no-trunc", "--format", "{{.ID}}"), None)
    if change == "raw":
        path = f.controller.root / "quarantine" / f"quality-docker-{f.authority[1]}" / "1/stdout.bin"
        path.write_bytes(b"substituted result")
    elif change == "pending":
        path = f.controller.root / "locks" / f"quality-docker-{f.authority[1]}"
        lines = path.read_bytes().splitlines(keepends=True)
        path.write_bytes(b"".join(lines[:-1]))
    elif change == "exhausted":
        f.runner((*api.DOCKER, "container", "ls", "-a", "--no-trunc", "--format", "{{.ID}}"), None)
    elif change in {"monotonic", "wall"}:
        proof = read_evidence(f.controller, "quality-docker-controls", *f.runner.controls)
        monkeypatch.setattr(api, "time", SimpleNamespace(
            monotonic=lambda: proof["deadline_monotonic"] + (1 if change == "monotonic" else -1),
            time=lambda: proof["deadline_wall"] + (1 if change == "wall" else -1)))
    else:
        (f.controller.root / "locks" / f"quality-docker-{f.authority[1]}").chmod(0o640)
    before = len(calls)
    with pytest.raises(ProviderRequestError):
        _register_backend(f)
    assert len(calls) == before
    assert not list((f.controller.root / "locks").glob("quality-row-*"))
    with f.ledger._locked() as state:
        assert state["permits"] == {}


@pytest.mark.parametrize("backend_registration", ("candidate-grade", "owner-grade"), indirect=True)
def test_container_backend_cannot_consume_nonwork_allocation_rows(backend_registration):
    f = backend_registration
    with pytest.raises(ProviderRequestError):
        _register_backend(f)
    assert not list((f.controller.root / "locks").glob("quality-row-*"))
    with f.ledger._locked() as state:
        assert state["permits"] == {}


def test_historical_quality_provisioning_context_keeps_exact_original_fields(quality_provisioning):
    q = quality_provisioning
    context = q.admission.quality_provisioning
    assert _quality_context(context) == {
        "store_root": str(context.store_root), "allocation": list(context.allocation),
        "bindings": context.bindings, "role": context.role, "phase": context.phase,
        "condition": context.condition, "catalog": context.catalog}


def test_container_quality_provisioning_carries_explicit_attachment_pointer(quality_provisioning):
    context = quality_provisioning.admission.quality_provisioning
    pointer = ("a" * 64 + ".json", "a" * 64)
    declared = replace(context, backend_attachment=pointer)
    assert _quality_context(declared) == {
        **_quality_context(context), "backend_attachment": list(pointer)}
    assert type(declared) is QualityProvisioning


@pytest.mark.parametrize("pointer", (["a" * 64 + ".json", "a" * 64],
    ("foreign.json", "a" * 64), ("a" * 64 + ".json", "b" * 64),
    ("a" * 64 + ".json", "a" * 64, "extra"), ("../a.json", "a" * 64)))
def test_container_quality_provisioning_refuses_noncanonical_pointer(quality_provisioning, pointer):
    context = replace(quality_provisioning.admission.quality_provisioning,
                      backend_attachment=pointer)
    with pytest.raises(ProviderRequestError):
        _quality_context(context)


def test_native_initial_registration_retains_exact_six_field_authority(quality_provisioning):
    from shared.lib.fanout.container_certification import _read_owned_json
    from shared.lib.fanout.controller import read_evidence
    from shared.lib.fanout.artifacts import canonical_json
    import hashlib
    q = quality_provisioning
    claim = {"allocation": q.authority[1], "row": q.row}
    digest = hashlib.sha256(canonical_json(claim)).hexdigest()
    pointer, _ = _read_owned_json(q.controller.root / "locks" / f"quality-registration-{digest}")
    value = read_evidence(q.controller, "quality-permit-registration", *pointer["authority"])
    assert value == {**claim, "permit_id": q.admission.permit_id, "epoch": q.row["epoch"],
        "permit": asdict(q.admission.broker_permit), "initial_receipt": None}


def test_native_command_cannot_adopt_container_registration_even_when_resigned(tmp_path, monkeypatch):
    from test_fanout_quality_predecessor import _fixture
    from shared.lib.fanout.artifacts import canonical_json
    from shared.lib.fanout.controller import read_evidence, write_evidence
    import hashlib
    f = _fixture(tmp_path, monkeypatch)
    try:
        command = read_evidence(f.controller, "quality-native-command", *f.command_proof)
        command["registration"]["backend_attachment"] = ["a" * 64 + ".json", "a" * 64]
        pointer = write_evidence(f.controller, "quality-permit-registration", command["registration"])
        claim = {"allocation": f.authority[1], "row": f.row}
        digest = hashlib.sha256(canonical_json(claim)).hexdigest()
        (f.controller.root / "locks" / f"quality-registration-{digest}").write_bytes(
            canonical_json({"authority": list(pointer)}))
        substitute = write_evidence(f.controller, "quality-native-command", command)
        with pytest.raises(ProviderRequestError):
            diagnostic.validate_quality_prior_command(f.controller, f.request, substitute)
    finally:
        f.store.close()


@pytest.mark.parametrize("provider", ("claude", "agy"))
def test_native_completion_cannot_supply_container_delivery_or_predecessor(tmp_path, monkeypatch, provider):
    from test_fanout_quality_predecessor import _fixture
    f = _fixture(tmp_path, monkeypatch, provider=provider)
    try:
        diagnostic.validate_quality_readonly_turn(f.controller, f.store, f.request, f.proof)
        pointer = ("a" * 64 + ".json", "a" * 64)
        before = f.ledger.request_records(f.permit_id, 1)
        with pytest.raises(ProviderRequestError):
            diagnostic.reopen_quality_container_delivery_turn(f.controller, f.store, f.request, f.proof,
                authority=f.authority, bindings=f.bindings, row=f.row, backend_attachment=pointer)
        with pytest.raises(ProviderRequestError):
            diagnostic.reopen_quality_predecessor(f.controller, f.store, f.resumed, f.proof,
                verification=f.verification, authority=f.authority, bindings=f.bindings,
                initial_digest=f.row_digest, backend_attachment=pointer)
        assert f.ledger.request_records(f.permit_id, 1) == before
        with f.ledger._locked() as state:
            assert len(state["permits"]) == 1
    finally:
        f.store.close()


@pytest.mark.parametrize("quality_provisioning", ["container-authority"], indirect=True)
def test_container_handoff_reopens_actual_explicit_registration_history(quality_provisioning):
    from test_fanout_container_transfer_transport import _quality_import
    from shared.lib.fanout.container_transfer_transport import reopen_quality_handoff_history
    q = quality_provisioning
    _quality_import(q)
    model = q.transport.handoff_quality_inputs(q.request, q.admission)
    try:
        history = reopen_quality_handoff_history(q.controller, q.request, model.handoff_evidence)
        registration = read_evidence(q.controller, "quality-permit-registration", *history["registration"])
        assert registration["backend_attachment"] == list(q.admission.quality_provisioning.backend_attachment)
        assert q.model_calls == []
        assert q.admission.ledger.attempt_counts(q.admission.permit_id, q.admission.epoch) == (0, 0)
    finally:
        model.close()


@pytest.mark.parametrize("quality_provisioning", ["container-authority"], indirect=True)
@pytest.mark.parametrize("change", ("missing", "foreign"))
def test_container_import_cannot_silently_reinterpret_its_registration(quality_provisioning, change):
    from shared.lib.fanout.container_transfer_transport import issue_input_source_grant
    q = quality_provisioning
    context = q.admission.quality_provisioning
    altered = replace(context, backend_attachment=None if change == "missing"
        else ("f" * 64 + ".json", "f" * 64))
    admission = replace(q.admission, quality_provisioning=altered)
    with pytest.raises(ProviderRequestError):
        issue_input_source_grant(q.controller, q.request, q.transport, admission, q.source, *q.material)
    assert q.model_calls == []
    assert q.admission.ledger.attempt_counts(q.admission.permit_id, q.admission.epoch) == (0, 0)


@pytest.mark.parametrize("quality_provisioning", ["container-authority"], indirect=True)
def test_fresh_child_reopens_explicit_handoff_after_activity_without_new_controls(quality_provisioning):
    from test_fanout_container_transfer_transport import _quality_import
    q = quality_provisioning
    _quality_import(q)
    model = q.transport.handoff_quality_inputs(q.request, q.admission)
    model.close()
    q.admission.ledger.record_attempt(q.admission.permit_id, q.admission.epoch)
    q.admission.ledger.fence_epoch(q.admission.permit_id, q.admission.epoch)
    sessions = q.admission.session_store
    sessions.reserve("claude", "6" * 64, initial=True,
        upstream_route_digest="a" * 64, initial_session_id=q.request.session_id)
    sessions.settle("claude", q.request.session_id, "6" * 64, initial=True,
                    upstream_route_digest="a" * 64)
    request_ref = diagnostic._retain_quality_request(q.store, q.request, "cold/original")
    marker = q.controller.root / "locks" / f"quality-docker-{q.authority[1]}"
    before = marker.read_bytes()
    payload = {"controller": str(q.controller.root), "capability": q.controller.capability.export_token(),
        "store": str(q.store.root), "request": asdict(request_ref), "proof": model.handoff_evidence,
        "backend_attachment": q.admission.quality_provisioning.backend_attachment}
    child = """
import json, sys
from pathlib import Path
from types import SimpleNamespace
from shared.lib.fanout import diagnostic, container_boundary
from shared.lib.fanout.artifacts import ArtifactStore, ArtifactRef
from shared.lib.fanout.controller import LifecycleCapability, resume_lifecycle_controller, read_evidence
from shared.lib.fanout.providers import ProviderRegistry, ProviderRequest, ExecutorProfile
from shared.lib.fanout.container_transfer_transport import reopen_quality_handoff_history
v = json.load(sys.stdin)
c = resume_lifecycle_controller(Path(v['controller']), LifecycleCapability(v['capability']))
attachment = read_evidence(c, 'quality-installed-case-attachment', *v['backend_attachment'])
controls = read_evidence(c, 'quality-docker-controls', *attachment['container']['docker_controls'])
container_boundary.time = SimpleNamespace(monotonic=lambda: controls['deadline_monotonic'] + 1,
                                         time=lambda: controls['deadline_wall'] + 1)
with ArtifactStore.open_existing(v['store']) as store:
    raw = diagnostic._read_quality_request_json(store, ArtifactRef(**v['request']))
    profile = ExecutorProfile.from_dict(diagnostic._read_quality_request_json(store, ArtifactRef(**raw['profile'])),
        adapter=ProviderRegistry.default().require(raw['controls']['executor_id']))
    request = ProviderRequest(**raw['controls'], prompt=diagnostic._read_quality_request_artifact(
        store, ArtifactRef(**raw['prompt']), 1024 * 1024), profile=profile, artifact_store=store)
    history = reopen_quality_handoff_history(c, request, tuple(v['proof']))
    registration = read_evidence(c, 'quality-permit-registration', *history['registration'])
    assert registration['backend_attachment'] == v['backend_attachment']
    assert history['zero_activity']['requests'] == []
    print(json.dumps({'registration': history['registration'], 'backend_attachment': registration['backend_attachment']}))
"""
    actual = subprocess.run((sys.executable, "-B", "-c", child), input=canonical_json(payload),
        cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=30)
    assert actual.returncode == 0, actual.stderr.decode()
    reopened = json.loads(actual.stdout)
    assert reopened["backend_attachment"] == list(q.admission.quality_provisioning.backend_attachment)
    assert marker.read_bytes() == before
    assert q.admission.ledger.attempt_counts(q.admission.permit_id, q.admission.epoch) == (1, 0)
    assert q.model_calls == []


@pytest.mark.parametrize("provider", ("claude", "codex", "agy"))
def test_cold_container_request_is_reconstructed_from_actual_retained_bytes(tmp_path, provider):
    from shared.lib.fanout.providers import ProviderRequest
    from shared.lib.fanout.container_transfer_transport import admitted_request_digest
    profile = ProviderRegistry.default().select(provider, "read-only", "standard")
    request = ProviderRequest(provider, b"actual retained question", tmp_path, profile=profile,
        session_id="11111111-1111-4111-8111-111111111111" if provider == "claude" else None,
        timeout=30, retries=0, run_id="run", task_id="task", target_id="target",
        seat_id="seat", inputs_digest="a" * 64)
    with ArtifactStore(tmp_path / "artifacts") as store:
        ref = diagnostic._retain_quality_request(store, request, "original")
        original = diagnostic._read_quality_container_request(store, ref)
        assert type(original) is ProviderRequest
        assert original.prompt_bytes == request.prompt_bytes
        assert admitted_request_digest(original) == admitted_request_digest(request)
        assert original.session_id == request.session_id


@pytest.mark.parametrize("change", ("prompt", "profile", "extra-control", "writer", "retry", "initial-session"))
def test_cold_container_request_refuses_changed_bytes_or_declared_controls(tmp_path, change):
    from shared.lib.fanout.providers import ProviderRequest
    from shared.lib.fanout.artifacts import ArtifactRef
    request = ProviderRequest("codex", b"original question", tmp_path,
        profile=ProviderRegistry.default().select("codex", "read-only", "standard"))
    with ArtifactStore(tmp_path / "artifacts") as store:
        ref = diagnostic._retain_quality_request(store, request, "original")
        value = store.read_json(ref)
        if change in {"prompt", "profile"}:
            (store.root / value[change]["path"]).write_bytes(b"substituted bytes")
        else:
            if change == "extra-control":
                value["controls"]["runner"] = "claimed"
            elif change == "writer":
                value["controls"]["execution_class"] = "repo-write"
            elif change == "retry":
                value["controls"]["retries"] = 1
            else:
                value["controls"]["session_id"] = "claimed-own-session"
            ref = store.write_json("altered.json", value)
        with pytest.raises(ProviderRequestError):
            diagnostic._read_quality_container_request(store, ref)


@pytest.mark.parametrize("change", (None, "source-answer", "source-reference", "owner-prompt", "owner-identity"))
def test_container_owner_request_reopens_actual_cross_cli_sources_and_fresh_identity(tmp_path, change):
    from test_fanout_quality_delivery_requests import _context
    module, runtime, opened, value, preparation, seat, row = _context(tmp_path)
    reader = importlib.import_module(runtime.__name__ + ".diagnostic")
    with opened:
        store = opened.service.artifacts
        value.update(schema="fanout-quality-installed-case-attachment-v2", backend="container-read-only")
        value["case_input"] = asdict(store.write_json("owner-input.json", {"prompt": "original invoice question"}))
        sources = {assigned.seat_id: store.write_bytes(f"cross-cli/{assigned.executor_id}.txt",
            f"{assigned.executor_id} final: round each line before summing".encode()) for assigned in preparation.seats}
        row = {**row, "role": "owner-synthesis", "permit": {**row["permit"],
            "seat_id": seat.seat_id + "-quality-owner"}}
        expected = module._quality_case_request(opened.service, value, preparation, seat, row, sources=sources)
        assert expected.seat_id == seat.seat_id + "-quality-owner" and expected.resume is False
        assert expected.cwd == seat.workspace_verification.workspace.root
        prompt = json.loads(expected.prompt_bytes)
        assert prompt["question"] == "original invoice question"
        assert prompt["sources"] == [{"seat_id": source_id, "artifact": asdict(ref),
            "answer": store.read_bytes(ref).decode()} for source_id, ref in sorted(sources.items())]
        descriptor = {"request": asdict(reader._retain_quality_request(store, expected, "owner/current"))}
        altered_sources = dict(sources)
        if change in {"source-answer", "source-reference"}:
            source_id = next(assigned.seat_id for assigned in preparation.seats if assigned.executor_id == "codex")
            old = sources[source_id]
            altered_sources[source_id] = store.write_bytes("cross-cli/codex-altered.txt",
                b"codex final: round only after summing" if change == "source-answer" else store.read_bytes(old))
        elif change in {"owner-prompt", "owner-identity"}:
            altered = replace(expected, **({"prompt": expected.prompt_bytes.replace(b"round each line", b"round only once")}
                if change == "owner-prompt" else {"seat_id": seat.seat_id}))
            descriptor = {"request": asdict(reader._retain_quality_request(store, altered, "owner/altered"))}
        if change is None:
            actual = module._reopen_quality_case_request(opened.service, value, preparation, seat, row,
                descriptor, sources=sources)
            module._require_quality_case_request(actual, expected)
        else:
            with pytest.raises(module.ProviderRequestError, match="prompt|controls"):
                module._reopen_quality_case_request(opened.service, value, preparation, seat, row,
                    descriptor, sources=altered_sources)
        assert not opened.journal.state.seat_phases
        assert not (opened.service.lifecycle_controller.root / "locks/spend").exists()
