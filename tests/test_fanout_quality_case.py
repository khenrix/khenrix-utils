"""Closed quality entry contracts; synthetic fixtures are not native evidence."""
from __future__ import annotations

import importlib.util
import hashlib
import importlib
from dataclasses import asdict, replace
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "shared/skills/llm-fanout-execute/scripts/execute.py"


def _execute():
    spec = importlib.util.spec_from_file_location("quality_execute_contract", HELPER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("injection", ["runtime", "registry", "memory_factory", "memory_preflight", "provider_runner"])
def test_quality_case_closed_entry_refuses_injected_composition_before_open(tmp_path, injection):
    helper = _execute()
    assert hasattr(helper, "open_quality_case"), "fixed quality entry is missing"
    with pytest.raises(TypeError):
        helper.open_quality_case(tmp_path, **{injection: lambda: pytest.fail("injection invoked")})


def test_quality_private_pointer_is_exact_bounded_exclusive_and_capability_free(tmp_path):
    helper = _execute()
    tmp_path.chmod(0o700)
    pointer = {"schema_version": "fanout-quality-case-pointer-v1",
               "attachment": ["a" * 64 + ".json", "a" * 64]}
    helper.save_private_descriptor(tmp_path, pointer, name="quality-case.json")
    assert helper.load_private_descriptor(tmp_path, name="quality-case.json") == pointer
    assert (tmp_path / "quality-case.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        helper.save_private_descriptor(tmp_path, pointer, name="quality-case.json")
    for injected in ({**pointer, "owner_token": "claimed"}, {**pointer, "attachment": ["a.json", "a" * 65536]}):
        with pytest.raises(ValueError):
            helper.save_private_descriptor(tmp_path, injected, name="quality-case.json")


@pytest.mark.parametrize("for_dispatch", [False, True])
def test_quality_attachment_presence_refuses_ordinary_dispatch_before_runtime_load(tmp_path, monkeypatch, for_dispatch):
    helper = _execute()
    tmp_path.chmod(0o700)
    (tmp_path / "quality-case.json").write_bytes(b"malformed presence must still refuse")
    monkeypatch.setattr(helper, "_runtime", lambda: pytest.fail("attached ordinary dispatch loaded runtime"))
    with pytest.raises(ValueError, match="quality-case"):
        helper.open_run(tmp_path, for_dispatch=for_dispatch)


def test_default_ordinary_open_cannot_resume_an_attached_actual_service(tmp_path, monkeypatch):
    helper, module, runtime, opened, value = _dormant_service(tmp_path)
    from test_fanout_execute_skill import _registry, _NeverMemory
    monkeypatch.setattr(module.HostCredentialSource, "load", lambda *_args: pytest.fail("credentials loaded"))
    with opened:
        inputs, owner, repository = opened.service.inputs, opened.owner, opened.service.baseline.repository
        proof = module.write_evidence(opened.service.lifecycle_controller,
            "quality-installed-case-attachment", value)
        helper.save_private_descriptor(tmp_path / "private", {
            "schema_version": "fanout-quality-case-pointer-v1", "attachment": list(proof)},
            name="quality-case.json")
    with pytest.raises(ValueError, match="closed quality-case"):
        with helper.open_run(tmp_path / "private", runtime=runtime, registry=_registry(),
                memory_factory=_NeverMemory, memory_preflight=lambda: True,
                provider_runner=lambda *_args, **_kwargs: pytest.fail("ordinary provider called")) as reopened:
            reopened.service.resume(owner=reopened.owner)
    anchor = runtime.LocalAnchorAuthority(tmp_path / "authority", run_root=tmp_path / "run", repo_root=repository)
    inspection = runtime.RunJournal.inspect(tmp_path / "run", inputs, owner, anchor_store=anchor)
    assert not inspection.state.seat_phases
    assert not (tmp_path / "run/lifecycle/locks/spend").exists()


def test_fresh_helper_quality_entry_requires_attachment_without_native_or_credentials(tmp_path):
    tmp_path.chmod(0o700)
    result = subprocess.run((sys.executable, "-B", str(HELPER), "quality-case", "--private-root", str(tmp_path)),
        capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode == 2
    assert "quality attachment pointer" in result.stderr
    assert not list(tmp_path.iterdir())


def test_parallel_quality_installation_doctors_keep_process_stdout_and_per_call_reports(tmp_path, monkeypatch):
    import io
    import threading
    from types import SimpleNamespace
    from shared.lib.fanout import quality_case
    from test_skill_delivery import fixture, skillctl
    repo, home, config = fixture(tmp_path)
    skillctl.apply(config, expect=None, as_json=False)
    validator = repo / "components/skills/skillctl.py"
    validator.parent.mkdir(parents=True)
    validator.write_bytes((ROOT / "components/skills/skillctl.py").read_bytes())
    helper_path = home / ".agents/skills/llm-fanout-execute/scripts/execute.py"
    helper_path.parent.mkdir(parents=True)
    helper_path.write_bytes(HELPER.read_bytes())
    spec = importlib.util.spec_from_file_location("_private_quality_stdout_helper", helper_path)
    helper = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, helper)
    spec.loader.exec_module(helper)
    monkeypatch.setattr(quality_case.pwd, "getpwuid", lambda _: SimpleNamespace(pw_dir=str(home)))
    receipt = config.state_dir / skillctl.RECEIPT_NAME
    value = {"repo_root": str(repo), "helper_path": str(helper_path),
        "helper_digest": hashlib.sha256(helper_path.read_bytes()).hexdigest(),
        "receipt_digest": hashlib.sha256(receipt.read_bytes()).hexdigest(), "runtime_digest": "d" * 64}
    before_receipt, before_helper = receipt.read_bytes(), helper_path.read_bytes()
    entered = {name: threading.Event() for name in ("doctor-a", "doctor-b")}
    release = {name: threading.Event() for name in entered}
    reports, seen_stdout, failures = {}, {}, []
    execute_module = importlib.machinery.SourceFileLoader.exec_module

    def schedule_actual_doctor(loader, module):
        execute_module(loader, module)
        if module.__name__ != "_fanout_quality_skillctl":
            return
        actual = module.doctor

        def scheduled(config, **kwargs):
            name = threading.current_thread().name
            reports[name] = kwargs.get("output")
            seen_stdout[name] = sys.stdout
            entered[name].set()
            assert release[name].wait(5), "parallel doctor schedule did not settle"
            return actual(config, **kwargs)

        module.doctor = scheduled

    monkeypatch.setattr(importlib.machinery.SourceFileLoader, "exec_module", schedule_actual_doctor)
    def worker():
        try:
            quality_case.require_quality_installation(value, helper_path, value["runtime_digest"])
        except BaseException as error:
            failures.append(error)

    original_stdout = sys.stdout
    workers = [threading.Thread(target=worker, name=name, daemon=True) for name in entered]
    try:
        workers[0].start()
        assert entered["doctor-a"].wait(5)
        workers[1].start()
        assert entered["doctor-b"].wait(5)
        stdout_during_overlap = sys.stdout is original_stdout
        release["doctor-a"].set()
        workers[0].join(5)
        assert not workers[0].is_alive()
        release["doctor-b"].set()
        workers[1].join(5)
        stdout_after = sys.stdout is original_stdout
    finally:
        for event in release.values():
            event.set()
        for thread in workers:
            if thread.ident is not None:
                thread.join(5)
        sys.stdout = original_stdout
    assert not any(thread.is_alive() for thread in workers)
    if failures:
        raise failures[0]
    assert stdout_during_overlap and stdout_after, "parallel installation doctors changed process stdout"
    assert all(stream is original_stdout for stream in seen_stdout.values())
    assert all(isinstance(report, io.StringIO) for report in reports.values())
    assert reports["doctor-a"] is not reports["doctor-b"]
    assert [json.loads(reports[name].getvalue()) for name in entered] == [{
        "ok": True, "receipt": str(receipt), "problems": [], "managed_skills": list(config.skills)}] * 2
    assert receipt.read_bytes() == before_receipt and helper_path.read_bytes() == before_helper


def _dormant_service(tmp_path):
    from test_fanout_execute_skill import _question_packet, _repository, _registry, _NeverMemory, fanout
    helper = _execute()
    packet, resolver = _question_packet(tmp_path, executors=("claude", "codex", "agy"))
    packet["draft"]["tasks"][0]["reconciliation_policy"] = "synthesis-required"
    packet["compiled"] = fanout.compile_superpowers_plan(packet["source_markdown"].encode(),
        source_path=packet["compiled"]["plan"]["source"]["path"],
        draft_bytes=fanout.canonical_json(packet["draft"]), resolver=resolver).to_dict()
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)
    common = dict(runtime=fanout, registry=_registry(), memory_factory=_NeverMemory,
        memory_preflight=lambda: True, provider_runner=lambda *_args, **_kwargs: pytest.fail("provider launched"))
    helper.prepare_run(packet, repo_root=_repository(tmp_path), run_root=run,
        authority_root=authority, private_root=private, skill_roots=(tmp_path / "skills",), budget=12, **common)
    opened = helper.open_run(private, **common)
    module = importlib.import_module(fanout.__name__ + ".quality_case")
    service = opened.service
    task = next(iter(service.preparations))
    value = dict.fromkeys(module._FIELDS)
    value.update(schema="fanout-quality-installed-case-attachment-v1", case_id="normal-01",
        run_id=service.inputs.run_id, task_id=task, target_id="quality-normal-01", condition="candidate",
        execution_class="read-only", inputs_digest=service.inputs.digest,
        seats={provider: {} for provider in ("claude", "codex", "agy")},
        installation={"repo_root": str(ROOT), "receipt_digest": "a" * 64,
            "helper_path": str(HELPER), "helper_digest": hashlib.sha256(HELPER.read_bytes()).hexdigest(),
            "runtime_digest": helper._runtime_digest(fanout)})
    return helper, module, fanout, opened, value


def _guarded_dormant_quality_entry(tmp_path, monkeypatch, *, attached=True):
    from test_fanout_execute_skill import fanout, _NeverMemory
    guard_module = importlib.import_module(fanout.__name__ + ".agy_guard")
    binary = tmp_path / "version-only-agy"
    binary.write_bytes(b"synthetic version-only guard fixture\n")
    binary.chmod(0o700)
    monkeypatch.setattr(guard_module, "_BINARY", binary)
    monkeypatch.setattr(guard_module, "default_claude_adc_path", lambda: tmp_path / "unused-adc-reference")
    monkeypatch.setenv("AGY_ADC_AUTH", "true")
    real_run = subprocess.run

    def version_only(argv, *args, **kwargs):
        if argv[0] == str(binary):
            assert tuple(argv) == (str(binary), "--version")
            return subprocess.CompletedProcess(argv, 0, b"1.2.13\n", b"")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(guard_module.subprocess, "run", version_only)
    helper, module, runtime, opened, value = _dormant_service(tmp_path)
    registry = opened.service.coordinator.registry
    monkeypatch.setattr(helper, "_runtime", lambda: runtime)
    monkeypatch.setattr(helper, "_real_memory", lambda _runtime: (_NeverMemory, lambda: True))
    monkeypatch.setattr(runtime.ProviderRegistry, "default", lambda **_kwargs: registry)
    monkeypatch.setattr(module.HostCredentialSource, "load", lambda *_args: pytest.fail("credentials loaded"))
    with opened:
        preparation = next(iter(opened.service.preparations.values()))
        seat = next(seat for seat in preparation.seats if seat.executor_id == "agy")
        assert seat.agy_guard is None
        assert seat.agy_guard_receipt_sha256 is not None
        if attached:
            proof = module.write_evidence(opened.service.lifecycle_controller,
                "quality-installed-case-attachment", value)
            helper.save_private_descriptor(tmp_path / "private", {
                "schema_version": "fanout-quality-case-pointer-v1", "attachment": list(proof)},
                name="quality-case.json")
    return helper, module, runtime, preparation.digest, seat.agy_guard_receipt_sha256


def test_declared_container_never_falls_back_to_native_credentials(tmp_path, monkeypatch):
    from test_fanout_container_upstream import _route
    helper, module, runtime, opened, value = _dormant_service(tmp_path)
    BrokerPermit = importlib.import_module(runtime.__name__ + ".broker").BrokerPermit
    routes = importlib.import_module(runtime.__name__ + ".container_routes")
    route = _route("claude")
    route = routes.RouteAdmission(**{**asdict(route), "profile": routes.RouteProfile(**asdict(route.profile))})
    with opened:
        service = opened.service
        preparation = next(iter(service.preparations.values()))
        seat = next(assigned for assigned in preparation.seats if assigned.executor_id == "claude")
        profile, timeout = service.coordinator._selected_profile(
            preparation.packet, seat, preparation.policy, service.inputs)
        permit = BrokerPermit("claude", value["run_id"], value["task_id"], value["target_id"],
            seat.seat_id, "initial", profile.requested_model, "inert-placeholder", 1,
            1024, 2048, 8192, inputs_digest=value["inputs_digest"],
            session_id="11111111-1111-4111-8111-111111111111")
        row = {"role": "candidate-seat", "phase": "initial", "condition": "candidate",
            "permit": asdict(permit), "epoch": 1, "profile": profile.digest,
            "process_seconds": timeout, "outer_seconds": timeout + 60, "initial": None,
            "catalog": None, "blocker": None}
        request = module._quality_case_request(service, value, preparation, seat, row)
        value.update(schema="fanout-quality-installed-case-attachment-v2",
                     backend="container-read-only", container={},
                     allocation=["a" * 64 + ".json", "a" * 64])
        runner = module.QualityCaseRunner(module.QualityCaseAttachment("a" * 64 + ".json", "a" * 64),
            helper=helper.__file__, runtime_digest=helper._runtime_digest(runtime))
        runner._service = service
        monkeypatch.setattr(module, "HostCredentialSource",
            lambda *_: pytest.fail("declared container fell back to native credential construction"))
        with pytest.raises(module.ProviderRequestError, match="container"):
            runner._turn(request, value, row, {"claude": route}, seat)
        assert not service.journal.state.seat_phases
        assert not (service.lifecycle_controller.root / "locks/spend").exists()


def test_container_owner_initial_uses_a_fresh_declared_identity_after_worker_settles(tmp_path):
    _, module, runtime, opened, value = _dormant_service(tmp_path)
    SessionStore = importlib.import_module(runtime.__name__ + ".container_boundary").SessionStore
    private_volume_names = importlib.import_module(runtime.__name__ + ".container_runtime").private_volume_names
    with opened:
        service = opened.service
        preparation = next(iter(service.preparations.values()))
        seat = next(seat for seat in preparation.seats if seat.executor_id == "claude")
        value.update(schema="fanout-quality-installed-case-attachment-v2",
                     backend="container-read-only")
        question = service.artifacts.write_json("quality/owner-question.json", {"prompt": "Original question"})
        value["case_input"] = asdict(question)
        sources = {name: service.artifacts.write_bytes(f"quality/{name}.txt", answer)
                   for name, answer in (("codex", b"First final answer"), ("agy", b"Second final answer"))}
        row = {"role": "owner-synthesis", "phase": "initial",
               "permit": {"session_id": "36c75d5f-564e-4c3c-bbf9-03189455fc87",
                          "seat_id": seat.seat_id + "-quality-owner"}}
        request = module._quality_case_request(service, value, preparation, seat, row, sources=sources)
        assert request.seat_id == seat.seat_id + "-quality-owner"
        assert request.cwd == seat.workspace_verification.workspace.root
        assert request.staged_skill_admission is seat.admission
        identity = (value["run_id"], value["task_id"], value["target_id"])
        worker_volume = private_volume_names(*identity, seat.seat_id)[1]
        worker = SessionStore(service.lifecycle_controller, *identity, seat.seat_id, volume=worker_volume)
        worker.reserve("claude", "1" * 64, initial=True, upstream_route_digest="a" * 64)
        worker.settle("claude", "worker-session", "1" * 64, initial=True, upstream_route_digest="a" * 64)
        owner_volume = private_volume_names(*identity, request.seat_id)[1]
        owner = SessionStore(service.lifecycle_controller, *identity, request.seat_id, volume=owner_volume)
        owner.reserve("claude", "2" * 64, initial=True, upstream_route_digest="a" * 64,
                      initial_session_id=request.session_id)
        owner.settle("claude", request.session_id, "2" * 64, initial=True, upstream_route_digest="a" * 64)
        assert worker.read("claude").session_id == "worker-session"
        assert owner.read("claude").session_id == request.session_id
        assert owner_volume != worker_volume
        prompt = json.loads(request.prompt_bytes)
        assert prompt["question"] == "Original question"
        assert {item["answer"] for item in prompt["sources"]} == {"First final answer", "Second final answer"}


@pytest.mark.parametrize("tamper", ["legacy-extra", "v2-without-backend", "unknown-backend",
    "missing-guest", "bool-digest", "extra-container", "foreign-controls", "unpinned-image"])
def test_quality_backend_rejects_undeclared_or_substituted_container_projection(tamper):
    from shared.lib.fanout import quality_case as module
    value = dict.fromkeys(module._FIELDS)
    value.update(schema="fanout-quality-installed-case-attachment-v2", backend="container-read-only",
        run_id="run", task_id="task", target_id="target", container={
            "image_digest": "fanout-seat@sha256:" + "a" * 64,
            "cli_digests": {provider: "b" * 64 for provider in ("claude", "codex", "agy")},
            "docker_controls": ["c" * 64 + ".json", "c" * 64]})
    assert module._quality_backend(value) == "container-read-only"
    if tamper == "legacy-extra":
        value["schema"] = "fanout-quality-installed-case-attachment-v1"
    elif tamper == "v2-without-backend":
        del value["backend"]
    elif tamper == "unknown-backend":
        value["backend"] = "automatic"
    elif tamper == "missing-guest":
        del value["container"]["cli_digests"]["agy"]
    elif tamper == "bool-digest":
        value["container"]["cli_digests"]["codex"] = True
    elif tamper == "extra-container":
        value["container"]["environment"] = {}
    elif tamper == "foreign-controls":
        value["container"]["docker_controls"][0] = "d" * 64 + ".json"
    else:
        value["container"]["image_digest"] = "fanout-seat:latest"
    with pytest.raises(module.ProviderRequestError):
        module._quality_backend(value)


def test_quality_material_refuses_an_injected_authority_before_source_staging(tmp_path):
    _, _, runtime, opened, _ = _dormant_service(tmp_path)
    material = importlib.import_module(runtime.__name__ + ".container_coordinator")
    with opened:
        service = opened.service
        preparation = next(iter(service.preparations.values()))
        with pytest.raises(runtime.CollaborationValidationError):
            material.stage_input_material(service.coordinator, preparation.packet,
                preparation.seats[0], None, None, quality=object())
        assert not list((service.lifecycle_controller.root / "quarantine").glob("input-*"))


@pytest.mark.parametrize("operation", ["reserve_registered", "complete_handoff"])
def test_quality_sibling_scope_refuses_missing_registration_without_adding_a_permit(tmp_path, operation):
    _, _, runtime, opened, value = _dormant_service(tmp_path)
    component = importlib.import_module(runtime.__name__ + ".container_coordinator")
    boundary = importlib.import_module(runtime.__name__ + ".container_boundary")
    container = importlib.import_module(runtime.__name__ + ".container_runtime")
    broker = importlib.import_module(runtime.__name__ + ".broker")
    errors = importlib.import_module(runtime.__name__ + ".errors")
    with opened:
        service = opened.service
        scope = component._coordinator_lifecycle(service.coordinator)
        ledger = boundary.PermitLedger(service.lifecycle_controller.root / "locks/spend", service.lifecycle_controller)
        permit = broker.BrokerPermit("codex", value["run_id"], value["task_id"], value["target_id"],
            "codex-seat", "initial", "gpt-6.1-sol", "fixed-inert-placeholder", 1, 1024, 1024, 32,
            inputs_digest=value["inputs_digest"])
        spec = container.ContainerSpec(value["run_id"], value["task_id"], value["target_id"], "codex-seat",
            "fanout-seat@sha256:" + "a" * 64,
            *container.private_volume_names(value["run_id"], value["task_id"], value["target_id"], "codex-seat"),
            container.seat_socket_path("codex-seat"), "b" * 64)
        with pytest.raises((ValueError, errors.LifecycleError, runtime.CollaborationDurabilityError)):
            if operation == "reserve_registered":
                scope.reserve_registered(("c" * 64 + ".json", "c" * 64), spec, ledger, permit, 1)
            else:
                scope.complete_handoff(None, None, None, ("c" * 64 + ".json", "c" * 64))
        assert scope.pending == scope.active == scope.completed == {}
        with ledger._locked() as state:
            assert state["permits"] == state["requests"] == {}


def test_closed_quality_entry_restores_actual_agy_guard_before_coordinator_dispatch(tmp_path, monkeypatch):
    helper, module, runtime, preparation_digest, guard_digest = _guarded_dormant_quality_entry(tmp_path, monkeypatch)
    real_bind = module.QualityCaseRunner.bind
    observed = []

    def inspect_dispatch_then_bind(runner, service):
        preparation = next(iter(service.preparations.values()))
        seat = next(seat for seat in preparation.seats if seat.executor_id == "agy")
        evidence = service.coordinator._dispatch_evidence(preparation.packet, seat, 1, preparation.policy)
        assert len(evidence) == 64
        assert type(seat.agy_guard) is runtime.AgyReadOnlyGuard
        assert seat.agy_guard.receipt_sha256 == guard_digest
        assert preparation.digest == preparation_digest
        assert not service.journal.state.seat_phases
        assert not (service.lifecycle_controller.root / "locks/spend").exists()
        observed.append(evidence)
        return real_bind(runner, service)

    monkeypatch.setattr(module.QualityCaseRunner, "bind", inspect_dispatch_then_bind)
    with pytest.raises(runtime.ProviderRequestError, match="installed helper"):
        helper.open_quality_case(tmp_path / "private")
    assert len(observed) == 1


@pytest.mark.parametrize("tamper", ["policy", "binary", "adc-mode"])
def test_closed_quality_entry_rejects_tampered_guard_before_binding_or_dispatch(tmp_path, monkeypatch, tamper):
    helper, module, runtime, _, _ = _guarded_dormant_quality_entry(tmp_path, monkeypatch)
    if tamper == "policy":
        policy = next((tmp_path / "run/lifecycle/agy-homes").glob("*/policy.json"))
        policy.chmod(0o600)
        policy.write_bytes(b"{}\n")
        policy.chmod(0o400)
    elif tamper == "binary":
        (tmp_path / "version-only-agy").write_bytes(b"changed synthetic binary\n")
    else:
        monkeypatch.delenv("AGY_ADC_AUTH")
    monkeypatch.setattr(module.QualityCaseRunner, "bind", lambda *_args: pytest.fail("tampered guard bound runner"))
    with pytest.raises(runtime.ProviderRequestError, match="guard"):
        helper.open_quality_case(tmp_path / "private")
    assert not (tmp_path / "run/lifecycle/locks/spend").exists()
    assert not list((tmp_path / "run/lifecycle/locks").glob("quality-row-*"))


def test_ordinary_cold_status_keeps_receipt_only_guard_without_live_auth_or_mutation(tmp_path, monkeypatch):
    from test_fanout_execute_skill import _private_inventory
    helper, _, _, preparation_digest, guard_digest = _guarded_dormant_quality_entry(
        tmp_path, monkeypatch, attached=False)
    monkeypatch.delenv("AGY_ADC_AUTH")
    (tmp_path / "version-only-agy").write_bytes(b"changed synthetic binary\n")
    roots = tuple(tmp_path / name for name in ("private", "run", "authority", "repo"))
    before = _private_inventory(*roots)
    with helper.open_run(tmp_path / "private") as opened:
        preparation = next(iter(opened.service.preparations.values()))
        seat = next(seat for seat in preparation.seats if seat.executor_id == "agy")
        assert seat.agy_guard is None
        assert seat.agy_guard_receipt_sha256 == guard_digest
        assert preparation.digest == preparation_digest
        status = opened.service.status()
        assert status.tasks[0].task_id == preparation.packet.task_id
        assert status.tasks[0].completed_rounds == 0
    assert _private_inventory(*roots) == before


@pytest.mark.parametrize("tamper", ["foreign-owner", "input", "identity", "repo-write", "extra-field", "checkout-attestation"])
def test_real_dormant_service_attachment_refuses_before_credentials_or_turns(tmp_path, monkeypatch, tamper):
    helper, module, runtime, opened, value = _dormant_service(tmp_path)
    monkeypatch.setattr(module, "HostCredentialSource", lambda *_args: pytest.fail("refusal reached credentials"))
    with opened:
        owner = opened.owner
        if tamper == "foreign-owner":
            owner = runtime.OwnerCapability("f" * 64)
        elif tamper == "input":
            value["inputs_digest"] = "b" * 64
        elif tamper == "identity":
            value["task_id"] = "foreign-task"
        elif tamper == "repo-write":
            value["execution_class"] = "repo-write"
        elif tamper == "extra-field":
            value["provider_runner"] = "claimed-callback"
        with pytest.raises((runtime.ExecutionError, runtime.RunStateError, runtime.ProviderRequestError)):
            module.adopt_quality_case(opened.service, owner, value, helper=HELPER,
                runtime_digest=helper._runtime_digest(runtime))
        assert not opened.journal.state.seat_phases
        assert not opened.service._execution_state(value["task_id"] if tamper != "identity" else next(iter(opened.service.preparations))).barriers
        assert not (opened.service.lifecycle_controller.root / "locks/spend").exists()


def test_foreign_controller_attachment_and_claimed_pointer_never_bind_runner(tmp_path, monkeypatch):
    helper, module, runtime, opened, value = _dormant_service(tmp_path)
    monkeypatch.setattr(module, "HostCredentialSource", lambda *_args: pytest.fail("refusal reached credentials"))
    with opened:
        other = runtime.create_lifecycle_controller(tmp_path / "other-controller")
        proof = module.write_evidence(other, "quality-installed-case-attachment", value)
        runner = module.QualityCaseRunner(module.QualityCaseAttachment(*proof), helper=HELPER,
            runtime_digest=helper._runtime_digest(runtime))
        with pytest.raises(Exception):
            runner.bind(opened.service)
        assert runner._service is None
        with pytest.raises(runtime.ProviderRequestError):
            module.QualityCaseRunner({"name": proof[0], "digest": proof[1]}, helper=HELPER,
                runtime_digest=helper._runtime_digest(runtime))
        assert not opened.journal.state.seat_phases


def test_quality_loaded_source_closure_includes_actual_execute_helper(tmp_path):
    helper = _execute()
    from shared.lib.fanout import diagnostic
    assert diagnostic._quality_loaded_sources()["shared/skills/llm-fanout-execute/scripts/execute.py"] == hashlib.sha256(HELPER.read_bytes()).hexdigest()


@pytest.mark.parametrize("tamper", ["file", "spec"])
def test_quality_checkout_helper_loaded_origin_drift_refuses(tmp_path, monkeypatch, tamper):
    helper = _execute()
    from shared.lib.fanout import diagnostic
    foreign = str(tmp_path / "foreign-execute.py")
    if tamper == "file":
        monkeypatch.setattr(helper, "__file__", foreign)
    else:
        monkeypatch.setattr(helper.__spec__, "origin", foreign)
    with pytest.raises(diagnostic.ProviderRequestError, match="origin"):
        diagnostic._quality_loaded_sources()


def test_quality_question_join_uses_actual_planner_source_not_generic_task_objective(tmp_path):
    helper, module, runtime, opened, _ = _dormant_service(tmp_path)
    with opened:
        preparation = next(iter(opened.service.preparations.values()))
        question = "Which invoice rounding order is correct?\n"
        assert opened.service.plan.tasks[0].objective != question
        assert hasattr(module, "require_quality_question"), "exact compiled question join is missing"
        module.require_quality_question(opened.service, preparation, question, "normal")
        with pytest.raises(runtime.ProviderRequestError):
            module.require_quality_question(opened.service, preparation, "Different original question", "normal")
        assert not opened.journal.state.seat_phases


def _full_frozen_allocation(tmp_path, *, work_tamper=None, bindings=None, owner_seat=None,
                            route_digest=None, placeholder="synthetic-placeholder"):
    from shared.lib import fanout
    from shared.lib.fanout import diagnostic
    from shared.lib.fanout.broker import BrokerPermit
    controller = fanout.create_lifecycle_controller(tmp_path / "controller")
    store = fanout.ArtifactStore(tmp_path / "artifacts")
    if bindings is None:
        bindings = {key: "a" * 64 for key in ("campaign", "casebook", "source", "projection", "profile", "inputs")}
    rows = []
    for index, provider in enumerate(("claude", "codex", "agy")):
        profile = fanout.ProviderRegistry.default().select(provider, "read-only", "standard")
        permit = BrokerPermit(provider, "run", "question", "quality-normal-01", provider + "-seat",
            "initial", profile.requested_model, placeholder, 1, 1024, 2048, 8192,
            inputs_digest="a" * 64,
            session_id="fd6f840b-4b79-4fd2-91d0-14d3d8fba655" if provider == "claude" else None,
            upstream_route_digest=route_digest,
            upstream_origin="https://provider.example.test" if route_digest else None,
            upstream_model=profile.requested_model if route_digest else None,
            upstream_path="/v1/provider" if route_digest else None)
        original = {"role": "candidate-seat", "phase": "initial", "condition": "candidate",
            "epoch": 2 * index + 1, "permit": asdict(permit), "blocker": None, "initial": None,
            "profile": profile.digest, "catalog": "b" * 64 if provider == "codex" else None,
            "process_seconds": 30, "outer_seconds": 60}
        rows.extend((original, {**original, "phase": "resume", "epoch": 2 * index + 2,
            "initial": diagnostic.quality_row_identity(original),
            "permit": {**asdict(permit), "route": "resume"}}))
    rows.append({**rows[0], "role": "owner-synthesis", "epoch": 7})
    if owner_seat is not None:
        rows[6] = {**rows[6], "permit": {**rows[6]["permit"], "seat_id": owner_seat}}
    rows.append({**rows[2], "role": "candidate-grade", "phase": "grade", "epoch": 8,
        "permit": {**rows[2]["permit"], "task_id": "grade-normal-01", "seat_id": "grading-seat"}})
    if work_tamper == "extra-seat":
        rows.append({**rows[0], "epoch": 9})
    elif work_tamper == "missing-owner":
        rows.pop(6)
    elif work_tamper == "extra-owner":
        rows.append({**rows[6], "epoch": 9})
    document = {"schema": "fanout-quality-case-allocation-v1", "case_id": "normal-01",
        "controller_id": controller.controller_id, "bindings": bindings, "maka_output_tokens": 8192,
        "rows": rows}
    ref = store.write_json("full-original-allocation.json", document)
    authority = diagnostic.seal_quality_allocation(controller, store, ref, bindings=bindings)
    return diagnostic, controller, store, bindings, ref, authority, document


def _declared_container_backend(tmp_path, *, shared_owner=False, route_digest=None,
                                placeholder="disposable-placeholder"):
    from shared.lib.fanout import quality_case as module
    from shared.lib.fanout.artifacts import canonical_json
    from shared.lib.fanout.container_source import runtime_source_digest
    from shared.lib.fanout.providers import ProviderRegistry
    value = dict.fromkeys(module._FIELDS)
    value.update(schema="fanout-quality-installed-case-attachment-v2", backend="container-read-only",
        run_id="run", task_id="question", target_id="quality-normal-01", inputs_digest="a" * 64,
        condition="candidate", execution_class="read-only", case_input={"original": "question"},
        catalog={"original": "catalog"}, container={
            "image_digest": "fanout-seat@sha256:" + "b" * 64,
            "cli_digests": {provider: "c" * 64 for provider in ("claude", "codex", "agy")},
            "docker_controls": ["d" * 64 + ".json", "d" * 64]},
        seats={provider: {"seat_id": provider + "-seat", "workspace_digest": "e" * 64,
            "profile_digest": ProviderRegistry.default().select(provider, "read-only", "standard").digest,
            "route_id": "local-fixture"} for provider in ("claude", "codex", "agy")})
    projection = {"case_input": {"original": "question"}, "catalog": {"original": "catalog"},
        "backend": {"kind": "container-read-only", "image_digest": "fanout-seat@sha256:" + "b" * 64,
                    "cli_digests": {provider: "c" * 64 for provider in ("claude", "codex", "agy")}}}
    bindings = {key: "a" * 64 for key in ("campaign", "casebook", "source", "projection", "profile", "inputs")}
    bindings.update(source=runtime_source_digest(), projection=hashlib.sha256(canonical_json(projection)).hexdigest())
    _, controller, store, _, _, authority, allocation = _full_frozen_allocation(tmp_path,
        bindings=bindings, owner_seat="claude-seat" if shared_owner else "claude-seat-quality-owner",
        route_digest=route_digest, placeholder=placeholder)
    value.update(allocation=list(authority), bindings=bindings)
    return module, controller, store, authority, allocation, value, bindings


@pytest.mark.parametrize("tamper", [None, "epoch-type", "request-controls", "image", "raw-prompt", "extra-field"])
def test_quality_sibling_scope_uses_the_registered_row_epoch_not_worker_round_formula(tmp_path, tamper):
    from shared.lib.fanout import container_coordinator as component, diagnostic
    from shared.lib.fanout.artifacts import canonical_json
    from shared.lib.fanout.container_boundary import quality_docker_runner
    from shared.lib.fanout.container_broker import PermitLedger
    from shared.lib.fanout.container_runtime import ContainerSpec, private_volume_names, seat_socket_path
    from shared.lib.fanout.container_source import release_source_binding
    from shared.lib.fanout.controller import read_evidence, write_evidence
    from shared.lib.fanout.providers import ProviderRequest, ProviderRegistry
    module, controller, store, authority, allocation, value, bindings = _declared_container_backend(
        tmp_path, route_digest="f" * 64)
    with store:
        row = allocation["rows"][2]
        permit = diagnostic._quality_row_permit(row)
        controls = quality_docker_runner(controller, allocation_digest=authority[1],
            max_commands=32, command_seconds=1, outer_seconds=60)
        value["container"]["docker_controls"] = list(controls.controls)
        attachment = write_evidence(controller, "quality-installed-case-attachment", value)
        ledger = PermitLedger(controller.root / "locks/spend", controller)
        permit_id = diagnostic.register_quality_permit(controller, store, authority, ledger,
            role=row["role"], phase=row["phase"], condition=row["condition"], permit=permit,
            epoch=3, bindings=bindings, backend_attachment=attachment)
        claim = {"allocation": authority[1], "row": row}
        claim_digest = hashlib.sha256(canonical_json(claim)).hexdigest()
        registration = json.loads((controller.root / "locks" / ("quality-registration-" + claim_digest)).read_bytes())["authority"]
        request = ProviderRequest("codex", b"actual original scoped prompt", cwd=tmp_path,
            profile=ProviderRegistry.default().select("codex", "read-only", "standard"), timeout=30,
            run_id="run", task_id="question", target_id="quality-normal-01", seat_id="codex-seat",
            inputs_digest="a" * 64, artifact_store=store, artifact_prefix="quality/scope")
        retained = diagnostic._retain_quality_request(store, request, "quality/scope")
        descriptor = store.read_json(retained)
        source = release_source_binding().to_dict()
        context = {"schema_version": "fanout-quality-container-context-v1",
            "run_id": "run", "task_id": "question", "target_id": "quality-normal-01", "seat_id": "codex-seat",
            "inputs_digest": "a" * 64, "release_source_binding": source,
            "request": descriptor["controls"], "request_material": asdict(retained),
            "request_digest": module.admitted_request_digest(request), "task_packet_sha256": "a" * 64,
            "backend_attachment": list(attachment), "allocation": list(authority), "bindings": bindings,
            "registration": registration, "store": str(store.root), "row": row}
        spec = ContainerSpec("run", "question", "quality-normal-01", "codex-seat",
            "fanout-seat@sha256:" + "b" * 64,
            *private_volume_names("run", "question", "quality-normal-01", "codex-seat"),
            seat_socket_path("codex-seat"), "c" * 64)
        if tamper == "epoch-type":
            context["row"] = {**row, "epoch": 3.0}
        elif tamper == "request-controls":
            context["request"] = {**context["request"], "timeout": True}
        elif tamper == "image":
            spec = replace(spec, image_digest="fanout-seat@sha256:" + "e" * 64)
        elif tamper == "raw-prompt":
            (store.root / descriptor["prompt"]["path"]).write_bytes(b"substituted original question")
        elif tamper == "extra-field":
            context["allow_unknown_peers"] = True
        proof = write_evidence(controller, "container-coordinator-context", context)
        if tamper is None:
            decoded = component._scope_context(controller, proof, spec, "a" * 64, source, permit)
            assert decoded == context
            assert component._scope_epoch(decoded) == 3
        else:
            from shared.lib.fanout.errors import ArtifactError
            with pytest.raises((ValueError, module.ProviderRequestError, ArtifactError)):
                component._scope_context(controller, proof, spec, "a" * 64, source, permit)
        assert ledger.attempt_counts(permit_id, 3) == (0, 0)
        assert ledger.request_records(permit_id, 3) == ()
        assert read_evidence(controller, "quality-docker-controls", *controls.controls)["limits"]["max_commands"] == 32


@pytest.mark.parametrize("tamper", [None, "backend", "image", "guest", "source", "row", "shared-owner"])
def test_container_backend_reader_joins_frozen_projection_without_consuming_a_row(tmp_path, tamper):
    module, controller, store, authority, allocation, value, bindings = _declared_container_backend(
        tmp_path, shared_owner=tamper == "shared-owner")
    with store:
        row = allocation["rows"][6]
        if tamper == "backend":
            value["backend"] = "native"
        elif tamper == "image":
            value["container"]["image_digest"] = "fanout-seat@sha256:" + "f" * 64
        elif tamper == "guest":
            value["container"]["cli_digests"]["agy"] = "f" * 64
        elif tamper == "source":
            value["bindings"] = {**bindings, "source": "f" * 64}
        elif tamper == "row":
            row = {**row, "epoch": 9}
        pointer = module.write_evidence(controller, "quality-installed-case-attachment", value)
        frozen = (controller.root / "locks/quality-allocation.json").read_bytes()
        if tamper is None:
            assert module._read_quality_backend_attachment(controller, store, pointer,
                authority=authority, bindings=bindings, row=row) == value
        else:
            with pytest.raises(module.ProviderRequestError):
                module._read_quality_backend_attachment(controller, store, pointer,
                    authority=authority, bindings=bindings, row=row)
        assert (controller.root / "locks/quality-allocation.json").read_bytes() == frozen
        assert not list((controller.root / "locks").glob("quality-row-*"))
        assert not (controller.root / "locks/spend").exists()


def test_quality_work_rows_accept_full_frozen_allocation_without_consuming_grade(tmp_path):
    from shared.lib.fanout import quality_case
    diagnostic, controller, store, bindings, ref, authority, original = _full_frozen_allocation(tmp_path)
    with store:
        frozen = (controller.root / "locks/quality-allocation.json").read_bytes()
        allocation = diagnostic.reopen_quality_allocation(controller, store, authority, bindings=bindings)
        work = quality_case._candidate_work_rows(allocation)
        assert len(work) == 7
        assert [row["role"] for row in work] == ["candidate-seat"] * 6 + ["owner-synthesis"]
        assert [row["permit"]["seat_id"] for row in work] == ["claude-seat", "claude-seat",
            "codex-seat", "codex-seat", "agy-seat", "agy-seat", "claude-seat"]
        assert allocation == original == store.read_json(ref)
        assert allocation["rows"][7]["permit"]["seat_id"] == "grading-seat"
        assert (controller.root / "locks/quality-allocation.json").read_bytes() == frozen
        assert not list((controller.root / "locks").glob("quality-row-*"))
        assert not (controller.root / "locks/spend").exists()
        with pytest.raises(quality_case.ProviderRequestError, match="already frozen"):
            diagnostic.seal_quality_allocation(controller, store, ref, bindings=bindings)


def test_quality_full_allocation_reopening_does_not_ignore_tampered_grading_row(tmp_path):
    diagnostic, controller, store, bindings, ref, authority, _ = _full_frozen_allocation(tmp_path)
    with store:
        path = store.root / ref.path
        raw = path.read_bytes().replace(b'"grading-seat"', b'"foreign-seat"')
        path.write_bytes(raw)
        with pytest.raises(diagnostic.ProviderRequestError):
            diagnostic.reopen_quality_allocation(controller, store, authority, bindings=bindings)
        assert not list((controller.root / "locks").glob("quality-row-*"))
        assert not (controller.root / "locks/spend").exists()


@pytest.mark.parametrize("tamper", ["extra-seat", "missing-owner", "extra-owner"])
def test_quality_full_allocation_work_roster_still_requires_exact_seven_rows(tmp_path, tamper):
    from shared.lib.fanout import quality_case
    diagnostic, controller, store, bindings, _, authority, _ = _full_frozen_allocation(tmp_path, work_tamper=tamper)
    with store:
        allocation = diagnostic.reopen_quality_allocation(controller, store, authority, bindings=bindings)
        with pytest.raises(quality_case.ProviderRequestError, match="six seat rows and one synthesis"):
            quality_case._candidate_work_rows(allocation)
        assert not list((controller.root / "locks").glob("quality-row-*"))
        assert not (controller.root / "locks/spend").exists()
