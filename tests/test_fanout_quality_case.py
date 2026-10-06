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


def _full_frozen_allocation(tmp_path, *, work_tamper=None):
    from shared.lib import fanout
    from shared.lib.fanout import diagnostic
    from shared.lib.fanout.broker import BrokerPermit
    controller = fanout.create_lifecycle_controller(tmp_path / "controller")
    store = fanout.ArtifactStore(tmp_path / "artifacts")
    bindings = {key: "a" * 64 for key in ("campaign", "casebook", "source", "projection", "profile", "inputs")}
    rows = []
    for index, provider in enumerate(("claude", "codex", "agy")):
        profile = fanout.ProviderRegistry.default().select(provider, "read-only", "standard")
        permit = BrokerPermit(provider, "run", "question", "quality-normal-01", provider + "-seat",
            "initial", profile.requested_model, "synthetic-placeholder", 1, 1024, 2048, 8192,
            inputs_digest="a" * 64,
            session_id="fd6f840b-4b79-4fd2-91d0-14d3d8fba655" if provider == "claude" else None)
        original = {"role": "candidate-seat", "phase": "initial", "condition": "candidate",
            "epoch": 2 * index + 1, "permit": asdict(permit), "blocker": None, "initial": None,
            "profile": profile.digest, "catalog": "b" * 64 if provider == "codex" else None,
            "process_seconds": 30, "outer_seconds": 60}
        rows.extend((original, {**original, "phase": "resume", "epoch": 2 * index + 2,
            "initial": diagnostic.quality_row_identity(original),
            "permit": {**asdict(permit), "route": "resume"}}))
    rows.append({**rows[0], "role": "owner-synthesis", "epoch": 7})
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
