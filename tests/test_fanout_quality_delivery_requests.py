"""Semantic request joins; synthetic context/native fixtures are not installed proof."""
from dataclasses import asdict, replace
import importlib
import json
import subprocess
from types import SimpleNamespace

import pytest

from test_fanout_quality_case import _dormant_service


def test_delivery_records_keep_actual_native_backend_and_refuse_reinterpretation(tmp_path, monkeypatch):
    from shared.lib.fanout import quality_case as module
    from test_fanout_quality_predecessor import _fixture
    f = _fixture(tmp_path, monkeypatch)
    try:
        outer, command, terminal = module._quality_delivery_records(f.controller, f.proof, "native")
        assert outer["schema"] == "fanout-quality-readonly-turn-v1"
        assert command["registration"]["row"]["role"] == "candidate-seat"
        assert command["registration"]["row"]["phase"] == "initial"
        assert terminal["valid"] is True and terminal["process_status"] == "exit"
        assert terminal["session_id"] == f.request.session_id
        with pytest.raises(module.ProviderRequestError, match="backend"):
            module._quality_delivery_records(f.controller, f.proof, "container-read-only")
    finally:
        f.store.close()


def _context(tmp_path):
    helper, module, runtime, opened, value = _dormant_service(tmp_path)
    preparation = next(iter(opened.service.preparations.values()))
    seat = next(seat for seat in preparation.seats if seat.executor_id == "claude")
    row = {"role": "candidate-seat", "phase": "initial", "initial": None,
        "permit": {"session_id": "fd6f840b-4b79-4fd2-91d0-14d3d8fba655"}}
    return module, runtime, opened, value, preparation, seat, row


@pytest.mark.parametrize("resume", [False, True])
def test_agy_quality_command_preserves_text_and_utf8_byte_prompt_semantics(tmp_path, resume):
    from shared.lib import fanout
    from shared.lib.fanout.eval_provider import quality_session_command
    text = 'Räkna € först, sedan "税".\nDo not change rounding.'
    profile = fanout.ProviderRegistry.default().select("agy", "read-only", "standard")
    request = fanout.ProviderRequest("agy", text, cwd=tmp_path, profile=profile, timeout=30,
        resume=resume, session_id="actual-own-session" if resume else None)
    command = quality_session_command(request)
    encoded = quality_session_command(replace(request, prompt=text.encode("utf-8")))
    assert encoded == command
    assert json.loads(encoded.stdin) == {"event": "user", "message": {"content": text}}
    assert "--effort" in command.argv and command.argv[command.argv.index("--effort") + 1] == profile.requested_effort
    assert command.stdin.endswith(b"\n") and "€".encode() in command.stdin


def test_agy_quality_command_refuses_invalid_utf8_before_command_creation(tmp_path):
    from shared.lib import fanout
    from shared.lib.fanout.eval_provider import quality_session_command
    from shared.lib.fanout.errors import ProcessValidationError
    request = fanout.ProviderRequest("agy", b"invalid \xff prompt", cwd=tmp_path, timeout=30,
        profile=fanout.ProviderRegistry.default().select("agy", "read-only", "standard"))
    with pytest.raises(ProcessValidationError, match="UTF-8"):
        quality_session_command(request)


def test_actual_coordinator_agy_byte_prompt_issues_and_reopens_allocated_command(tmp_path, monkeypatch):
    from test_fanout_container_upstream import _route
    from test_fanout_execute_skill import fanout
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
    _, module, runtime, opened, value = _dormant_service(tmp_path)
    diagnostic = importlib.import_module(runtime.__name__ + ".diagnostic")
    BrokerPermit = importlib.import_module(runtime.__name__ + ".broker").BrokerPermit
    PermitLedger = importlib.import_module(runtime.__name__ + ".container_broker").PermitLedger
    runtime_source_digest = importlib.import_module(runtime.__name__ + ".container_source").runtime_source_digest
    admitted_request_digest = importlib.import_module(runtime.__name__ + ".container_transfer_transport").admitted_request_digest
    monkeypatch.setattr(module.HostCredentialSource, "load", lambda *_args: pytest.fail("credential load"))
    with opened:
        service = opened.service
        preparation = next(iter(service.preparations.values()))
        seat = next(seat for seat in preparation.seats if seat.executor_id == "agy")
        request = module._quality_case_request(service, value, preparation, seat,
            {"role": "candidate-seat", "phase": "initial", "permit": {"session_id": None}})
        assert type(request.prompt) is bytes
        assert b"Which invoice rounding order is correct?" in request.prompt_bytes
        route = _route("agy")
        permit = BrokerPermit("agy", value["run_id"], value["task_id"], value["target_id"], seat.seat_id,
            "initial", request.profile.requested_model, "disposable-placeholder", 1, 1024 * 1024,
            1024 * 1024, 8192, inputs_digest=value["inputs_digest"],
            upstream_route_digest=route.digest, upstream_origin=route.profile.origin,
            upstream_model=route.profile.effective_model,
            upstream_path="/v1/projects/fictional-project/locations/europe-west1/publishers/google/models/gemini-3.8-flash:streamGenerateContent?alt=sse")
        bindings = {key: "a" * 64 for key in ("campaign", "casebook", "source", "projection", "profile", "inputs")}
        bindings.update(source=runtime_source_digest(), inputs=value["inputs_digest"])
        row = {"role": "candidate-seat", "phase": "initial", "condition": "candidate", "epoch": 1,
            "permit": asdict(permit), "blocker": None, "initial": None, "profile": request.profile.digest,
            "catalog": None, "process_seconds": request.timeout, "outer_seconds": request.timeout + 60}
        ref = service.artifacts.write_json("byte-prompt-allocation.json", {
            "schema": "fanout-quality-case-allocation-v1", "case_id": "normal-01",
            "controller_id": service.lifecycle_controller.controller_id, "bindings": bindings,
            "maka_output_tokens": 8192, "rows": [row]})
        authority = diagnostic.seal_quality_allocation(service.lifecycle_controller, service.artifacts, ref, bindings=bindings)
        ledger = PermitLedger(service.lifecycle_controller.root / "locks/spend", service.lifecycle_controller)
        permit_id = diagnostic.register_quality_permit(service.lifecycle_controller, service.artifacts, authority, ledger,
            role="candidate-seat", phase="initial", condition="candidate", permit=permit, epoch=1, bindings=bindings)
        proof = diagnostic.issue_quality_native_command(service.lifecycle_controller, service.artifacts, authority,
            request=request, ledger=ledger, permit=permit, epoch=1, bindings=bindings,
            role="candidate-seat", phase="initial", condition="candidate")
        command, registration = diagnostic.reopen_quality_native_command(service.lifecycle_controller, request, proof)
        authenticated = module.read_evidence(service.lifecycle_controller, "quality-native-command", *proof)
        assert authenticated["request"] == admitted_request_digest(request)
        assert registration["permit_id"] == permit_id and registration["row"] == row
        assert module.json.loads(command.stdin)["message"]["content"] == request.prompt_bytes.decode("utf-8")
        assert command.argv[0] == str(importlib.import_module(runtime.__name__ + ".native_boundary")._AGY_DIRECT_BINARY)
        assert ledger.attempt_counts(permit_id, 1) == (0, 0)
        assert not service.journal.state.seat_phases


@pytest.mark.parametrize("change", ["prompt", "slot", "prefix", "context", "identity"])
def test_admitted_initial_request_join_refuses_semantic_or_control_substitution(tmp_path, change):
    module, _, opened, value, preparation, seat, row = _context(tmp_path)
    with opened:
        expected = module._quality_case_request(opened.service, value, preparation, seat, row)
        assert b"Which invoice rounding order is correct?" in expected.prompt_bytes
        module._require_quality_case_request(expected, expected)
        changed = {
            "prompt": {"prompt": b"unrelated authenticated provider question"},
            "slot": {"slot_cap": 1}, "prefix": {"artifact_prefix": "foreign-prefix"},
            "context": {"context_sha256": "f" * 64}, "identity": {"task_id": "unrelated-task"},
        }[change]
        with pytest.raises(module.ProviderRequestError, match="admitted.*request|prompt"):
            module._require_quality_case_request(replace(expected, **changed), expected)
        assert not opened.journal.state.seat_phases


def test_resume_request_join_uses_exact_peer_bytes_and_own_session(tmp_path):
    module, runtime, opened, value, preparation, seat, row = _context(tmp_path)
    with opened:
        row = {**row, "phase": "resume", "initial": "f" * 64}
        # Only the authenticated-context seam is synthetic here. Actual coordinator
        # prompt rendering and scalar request authentication remain under test.
        answer = "exact first-barrier answer"
        payload = runtime.canonical_json({"schema_version": "fanout-peer-packet-v1",
            "run_id": preparation.packet.run_id, "task_id": preparation.packet.task_id,
            "attempt": 1, "source_round": 1, "target_round": 2, "target_seat_id": seat.seat_id,
            "target_session_id": "actual-own-session", "untrusted_evidence": True,
            "peers": [{"answer": answer, "answer_length": len(answer),
                "answer_sha256": module.hashlib.sha256(answer.encode()).hexdigest(), "checkpoint_digest": "b" * 64,
                "executor_id": "codex", "provider_session_id": "peer-session", "source_seat_id": "peer-seat"}]})
        peer = runtime.PeerPacket(preparation.packet.run_id, preparation.packet.task_id, 1, 1, 2,
            seat.seat_id, "actual-own-session", payload, module.hashlib.sha256(payload).hexdigest())
        expected = module._quality_case_request(opened.service, value, preparation, seat, row, peer=peer)
        assert expected.session_id == "actual-own-session" and expected.resume is True
        assert payload in expected.prompt_bytes
        for changed in (replace(expected, prompt=expected.prompt_bytes.replace(b"exact first-barrier", b"different barrier")),
                        replace(expected, session_id="claimed-other-session")):
            with pytest.raises(module.ProviderRequestError, match="admitted.*request|prompt"):
                module._require_quality_case_request(changed, expected)
        assert not opened.journal.state.seat_phases


def test_owner_request_join_reads_original_question_and_exact_final_source_refs(tmp_path):
    module, runtime, opened, value, preparation, seat, row = _context(tmp_path)
    with opened:
        store = opened.service.artifacts
        value["case_input"] = asdict(store.write_json("synthetic-case-input.json", {
            "schema": "fanout-quality-case-input-v1", "case_id": "normal-01", "casebook": "a" * 64,
            "prompt": "original synthetic admitted question"}))
        sources = {"first-seat": store.write_bytes("first-final.txt", b"first actual artifact answer"),
                   "second-seat": store.write_bytes("second-final.txt", b"second actual artifact answer")}
        row = {**row, "role": "owner-synthesis"}
        expected = module._quality_case_request(opened.service, value, preparation, seat, row, sources=sources)
        prompt = module.json.loads(expected.prompt_bytes)
        assert prompt["question"] == "original synthetic admitted question"
        assert prompt["sources"] == [
            {"seat_id": "first-seat", "artifact": asdict(sources["first-seat"]), "answer": "first actual artifact answer"},
            {"seat_id": "second-seat", "artifact": asdict(sources["second-seat"]), "answer": "second actual artifact answer"}]
        for raw in (expected.prompt_bytes.replace(b"original synthetic admitted", b"unrelated synthetic prompt"),
                    expected.prompt_bytes.replace(b"first actual artifact answer", b"unrelated final answer")):
            with pytest.raises(module.ProviderRequestError, match="admitted.*request|prompt"):
                module._require_quality_case_request(replace(expected, prompt=raw), expected)
        assert not opened.journal.state.seat_phases


@pytest.mark.parametrize("role", ["candidate-seat", "owner-synthesis"])
@pytest.mark.parametrize("change", ["prompt", "controls", "fields"])
def test_retained_delivery_reader_joins_actual_question_sources_and_closed_controls(tmp_path, role, change):
    module, runtime, opened, value, preparation, seat, row = _context(tmp_path)
    diagnostic = importlib.import_module(runtime.__name__ + ".diagnostic")
    with opened:
        store, sources = opened.service.artifacts, None
        row = {**row, "role": role}
        if role == "owner-synthesis":
            value["case_input"] = asdict(store.write_json("synthetic-owner-case-input.json", {
                "prompt": "original owner reader question"}))
            sources = {"first-seat": store.write_bytes("reader-first.txt", b"actual first final answer"),
                       "second-seat": store.write_bytes("reader-second.txt", b"actual second final answer")}
        expected = module._quality_case_request(opened.service, value, preparation, seat, row, sources=sources)
        ref = diagnostic._retain_quality_request(store, expected, "reader-original")
        native = {"request": asdict(ref)}
        actual = module._reopen_quality_case_request(opened.service, value, preparation, seat, row, native, sources=sources)
        module._require_quality_case_request(actual, expected)
        descriptor = module.json.loads(store.read_bytes(ref))
        if change == "prompt":
            descriptor["prompt"] = asdict(store.write_bytes("reader-unrelated.bin", b"unrelated retained prompt"))
        elif change == "controls":
            descriptor["controls"]["slot_cap"] = 1
        else:
            descriptor["controls"]["claimed_callback"] = "not a capability"
        native = {"request": asdict(store.write_json("reader-substitution.json", descriptor))}
        with pytest.raises(module.ProviderRequestError, match="prompt|controls|fields"):
            module._reopen_quality_case_request(opened.service, value, preparation, seat, row, native, sources=sources)
        assert not opened.journal.state.seat_phases
        assert not (opened.service.lifecycle_controller.root / "locks/spend").exists()


def test_retained_resume_reader_reconstructs_actual_peer_packet_and_own_session(tmp_path, monkeypatch):
    module, runtime, opened, value, preparation, seat, row = _context(tmp_path)
    diagnostic = importlib.import_module(runtime.__name__ + ".diagnostic")
    with opened:
        service, packet, store = opened.service, preparation.packet, opened.service.artifacts
        terminals, receipts = [], []
        for index, assigned in enumerate(preparation.seats[:2]):
            answer = store.write_bytes(f"synthetic-barrier/answer-{index}.txt", f"first-barrier actual answer {index}".encode())
            stdout = store.write_bytes(f"synthetic-barrier/stdout-{index}.bin", b"synthetic stdout")
            stderr = store.write_bytes(f"synthetic-barrier/stderr-{index}.bin", b"")
            terminal = runtime.TerminalSeatResult(run_id=packet.run_id, task_id=packet.task_id,
                seat_id=assigned.seat_id, executor_id=assigned.executor_id, attempt=packet.attempt, round=1,
                context_sha256=packet.context_sha256, compiled_plan_sha256=packet.compiled_plan_sha256,
                skill_manifest_sha256=packet.skill_manifest_sha256, skill_bundle_sha256=assigned.skill_bundle_sha256,
                staged_manifest_sha256=assigned.staged_manifest_sha256,
                delivery_evidence_sha256=assigned.delivery_evidence_sha256,
                workspace_evidence_sha256=assigned.workspace_verification.evidence_digest,
                state="valid", valid=True, reason="synthetic-barrier-fixture", session_id=f"own-session-{index}",
                answer_ref=answer, stdout_ref=stdout, stderr_ref=stderr, answer_sha256=answer.digest)
            terminals.append(terminal)
            checkpoint = service.coordinator._checkpoint(packet, terminal)
            publication = runtime.CheckpointPublication(identity=checkpoint.identity, checkpoint_key=checkpoint.identity.key,
                checkpoint_digest=checkpoint.digest, project=checkpoint.project, observation_id=index + 1,
                controller_sha256="c" * 64, checkpoint_ref=store.write_bytes(f"synthetic-barrier/checkpoint-{index}.json", checkpoint.text.encode()),
                intent_ref=store.write_json(f"synthetic-barrier/intent-{index}.json", {"synthetic": True}))
            publication = replace(publication,
                publication_ref=store.write_json(f"synthetic-barrier/publication-{index}.json", publication.to_dict()))
            receipt = runtime.CheckpointReceipt(publication=publication, memory_session_id="synthetic-memory-session",
                observation_sha256="d" * 64)
            receipts.append(replace(receipt,
                verification_ref=store.write_json(f"synthetic-barrier/receipt-{index}.json", receipt.to_dict())))
        barrier = runtime.BarrierResult(run_id=packet.run_id, task_id=packet.task_id, attempt=packet.attempt, round=1,
            status="round-complete", minimum_success=2, expected_seat_ids=tuple(item.seat_id for item in terminals),
            terminals=tuple(terminals), receipts=tuple(receipts), publications=tuple(item.publication for item in receipts))
        # The persisted service-state seam is synthetic, not an installed completion.
        # Peer bytes and own-session selection are produced by the actual coordinator.
        monkeypatch.setattr(service, "_execution_state", lambda _task: SimpleNamespace(barriers=("first", "final")))
        restored = []
        def restore(ref, actual_packet):
            restored.append(ref)
            assert ref == "first" and actual_packet is packet
            return barrier
        monkeypatch.setattr(service, "_restore_barrier", restore)
        row = {**row, "phase": "resume", "initial": "f" * 64}
        peer = service.coordinator.build_peer_packet(packet, barrier, target_seat_id=seat.seat_id)
        expected = module._quality_case_request(service, value, preparation, seat, row, peer=peer)
        native = {"request": asdict(diagnostic._retain_quality_request(store, expected, "reader-resume"))}
        actual = module._reopen_quality_case_request(service, value, preparation, seat, row, native)
        assert actual.session_id == barrier.valid_terminals[0].session_id
        assert peer.payload in actual.prompt_bytes and restored == ["first"]
        wrong = replace(expected, prompt=expected.prompt_bytes.replace(b"first-barrier actual answer", b"unrelated peer answer"))
        native = {"request": asdict(diagnostic._retain_quality_request(store, wrong, "reader-resume-substitution"))}
        with pytest.raises(module.ProviderRequestError, match="prompt"):
            module._reopen_quality_case_request(service, value, preparation, seat, row, native)
        assert not opened.journal.state.seat_phases


def test_authentic_unrelated_native_prompt_requires_the_separate_case_semantic_join(tmp_path, monkeypatch):
    from test_fanout_quality_predecessor import _fixture
    from shared.lib.fanout import diagnostic, quality_case
    from shared.lib.fanout.controller import read_evidence
    native_root = tmp_path / "native"
    native_root.mkdir()
    f = _fixture(native_root, monkeypatch, skills=True)
    native = read_evidence(f.controller, "quality-readonly-turn", *f.proof)
    command = read_evidence(f.controller, "quality-native-command", *native["command"])
    diagnostic.validate_quality_readonly_turn(f.controller, f.store, f.request, f.proof)
    quality_case._require_turn_chain(SimpleNamespace(lifecycle_controller=f.controller,
        artifacts=f.store, baseline=f.baseline), {"allocation": list(f.authority), "bindings": f.bindings},
        f.request, native, command, SimpleNamespace(workspace_verification=f.verification))
    context_root = tmp_path / "context"
    context_root.mkdir()
    module, runtime, opened, value, preparation, seat, row = _context(context_root)
    with opened:
        expected = module._quality_case_request(opened.service, value, preparation, seat, row)
        with pytest.raises(quality_case.ProviderRequestError, match="prompt"):
            quality_case._require_quality_case_request(f.request, expected)
        with f.ledger._locked() as state:
            assert len(state["permits"]) == 1
        assert not opened.journal.state.seat_phases


def test_actual_retained_request_reader_refuses_authenticated_unrelated_prompt(tmp_path, monkeypatch):
    from test_fanout_quality_predecessor import _fixture
    from shared.lib import fanout
    from shared.lib.fanout import diagnostic, quality_case
    from shared.lib.fanout.controller import read_evidence
    native_root = tmp_path / "native"
    native_root.mkdir()
    f = _fixture(native_root, monkeypatch, skills=True)
    native = read_evidence(f.controller, "quality-readonly-turn", *f.proof)
    diagnostic.validate_quality_readonly_turn(f.controller, f.store, f.request, f.proof)
    context_root = tmp_path / "context"
    context_root.mkdir()
    _, _, opened, _, original_preparation, _, _ = _context(context_root)
    with opened:
        plan = opened.service.plan.to_dict()
        plan["tasks"][0].update(id="task", required_skills=["example"], none_reason=None)
        plan = fanout.FanoutPlanV1.from_dict(plan)
        compiled, task = fanout.canonical_json(plan.to_dict()), fanout.canonical_json(plan.tasks[0].to_dict())
        bundle = fanout.canonical_json({"schema_version": "fanout-seat-skill-bundle-v1",
            "skills": f.skill_admission.manifest_dict()["skills"]})
        packet = fanout.TaskPacket("run", "task", 1, compiled, quality_case.hashlib.sha256(compiled).hexdigest(),
            original_preparation.packet.source_markdown, task, quality_case.hashlib.sha256(task).hexdigest(),
            bundle, quality_case.hashlib.sha256(bundle).hexdigest(), (), "read-only", f.baseline.repository)
        seat = fanout.SeatAssignment.from_admission(f.skill_admission, skill_bundle=bundle,
            artifacts=f.store, workspace_verification=f.verification)
        registry = fanout.ProviderRegistry.default()
        inputs = fanout.RunInputs(run_id="run", compiled_plan_sha256=packet.compiled_plan_sha256,
            source_sha256=plan.source.sha256, draft_sha256="b" * 64, compiler_sha256="b" * 64,
            parser_sha256="b" * 64, provider_profiles=registry.profile_digests,
            skill_manifests={"task": packet.skill_manifest_sha256}, repo_baseline_sha256=f.baseline.digest,
            profile_shape="class-tier")
        owner = fanout.OwnerCapability("f" * 64)
        anchor = fanout.LocalAnchorAuthority.bootstrap(tmp_path / "reader-anchor", run_root=tmp_path / "reader-run",
            repo_root=f.baseline.repository)
        journal, _ = fanout.RunJournal.create(tmp_path / "reader-run", inputs,
            owner_capability=owner, anchor_store=anchor)
        with journal:
            def no_memory(*_args, **_kwargs):
                pytest.fail("request reconstruction contacted Memory")
            memory = SimpleNamespace(publish=no_memory, recover=no_memory, verify_existing=no_memory, fetch_verified=no_memory)
            coordinator = fanout.CollaborationCoordinator(artifacts=f.store, journal=journal, owner=owner,
                memory=memory, registry=registry, lifecycle_controller=f.controller, repository_baseline=f.baseline)
            preparation = SimpleNamespace(packet=packet,
                policy=fanout.RoundPolicy.from_provider_policy(packet.provider_policy), seats=(seat,))
            service = SimpleNamespace(coordinator=coordinator, inputs=inputs, artifacts=f.store)
            value = {"run_id": "run", "task_id": "task", "target_id": "target", "inputs_digest": "a" * 64}
            with pytest.raises(quality_case.ProviderRequestError, match="prompt"):
                quality_case._reopen_quality_case_request(service, value, preparation, seat, f.row, native)
        with f.ledger._locked() as state:
            assert len(state["permits"]) == 1
