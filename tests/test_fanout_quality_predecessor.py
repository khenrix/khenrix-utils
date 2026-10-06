"""Synthetic durable fixtures; no native launch, credential load or model evidence."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.lib import fanout
from shared.lib.fanout import diagnostic, native_boundary
from shared.lib.fanout.artifacts import ArtifactRef, ArtifactStore, canonical_json
from shared.lib.fanout.broker import BrokerPermit
from shared.lib.fanout.container_broker import OutboundRequestIdentity, PermitLedger
from shared.lib.fanout.container_source import runtime_source_digest
from shared.lib.fanout.container_upstream import HostUpstream
from shared.lib.fanout.controller import read_evidence, write_evidence
from shared.lib.fanout.errors import ProviderRequestError
from scripts.fanout_container_certify import _capture_private_wire
from test_fanout_container_upstream import _route
from test_fanout_provider_diagnostic import _claude_response, _agy_response


def _request_artifact(store, request):
    controls = {name: getattr(request, name) for name in (
        "executor_id", "execution_class", "session_id", "resume", "timeout",
        "timeout_override_reason", "timeout_override_review_sha256", "retries",
        "artifact_prefix", "slot_cap", "slot_timeout", "context_sha256",
        "skill_bundle_sha256", "skill_delivery_sha256", "run_id", "task_id",
        "target_id", "seat_id", "inputs_digest")}
    controls.update({name: None if getattr(request, name) is None else str(getattr(request, name))
                     for name in ("cwd", "slot_root", "staged_skill_root")})
    return store.write_json("quality/request.json", {
        "schema": "fanout-quality-request-v1", "controls": controls,
        "prompt": asdict(store.write_bytes("quality/prompt.bin", request.prompt_bytes)),
        "profile": asdict(store.write_json("quality/profile.json", request.profile.to_dict()))})


def _fixture(tmp_path, monkeypatch, *, provider="claude", auxiliary=0, skills=False):
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(("git", "init", "-q", str(repository)), check=True)
    (repository / "fixture.txt").write_text("synthetic durable fixture\n")
    subprocess.run(("git", "-C", str(repository), "add", "-A"), check=True)
    subprocess.run(("git", "-C", str(repository), "-c", "user.name=Test",
                    "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"), check=True)
    baseline = fanout.capture_repository_baseline(repository)
    controller = fanout.create_lifecycle_controller(tmp_path / "controller")
    workspace = fanout.create_seat_workspace(baseline, controller.root / "workspaces", "seat")
    verification = fanout.verify_seat_workspace(baseline, workspace, controller=controller)
    store = ArtifactStore(tmp_path / "artifacts")
    ledger = PermitLedger(controller.root / "locks" / "spend", controller)
    profile = fanout.ProviderRegistry.default().select(provider, "read-only", "standard")
    session = "fd6f840b-4b79-4fd2-91d0-14d3d8fba655"
    request = fanout.ProviderRequest(provider, "synthetic initial question", cwd=workspace.root,
        profile=profile, timeout=30, session_id=session if provider == "claude" else None,
        run_id="run", task_id="task", target_id="target", seat_id="seat", inputs_digest="a" * 64)
    if skills:
        source = tmp_path / "skill-source"
        source.mkdir(mode=0o700)
        (source / "example").mkdir(mode=0o700)
        (source / "example" / "SKILL.md").write_text("# Example\nSynthetic admitted skill.\n")
        resolver = fanout.SkillResolver((fanout.SkillRoot("root", source, 0),))
        skill_admission = resolver.admit(("example",), task_id="task", seat_id="seat",
                                         provider=provider, session_id=session)
        skill_admission = resolver.stage(skill_admission, tmp_path / "skill-stage")
        event = fanout.SkillLoadEvidence.engine(skill_admission.skills[0], skill_admission)
        skill_admission = resolver.verify_engine_delivery(skill_admission, (event,))
        delivery = canonical_json([{
            "kind": event.kind, "provider": event.provider, "seat_id": event.seat_id,
            "session_id": event.session_id, "skill": event.skill, "source": event.source,
            "tree_hash": event.tree_hash, "truncated": event.truncated}])
        delivery_ref = store.write_bytes("skill-delivery.json", delivery)
        request = replace(request, staged_skill_root=skill_admission.staged_root,
            staged_skill_admission=skill_admission, skill_delivery_ref=delivery_ref,
            skill_delivery_sha256=delivery_ref.digest,
            skill_bundle_sha256=hashlib.sha256(canonical_json({
                "schema_version": "fanout-seat-skill-bundle-v1",
                "skills": skill_admission.manifest_dict()["skills"]})).hexdigest())
    resumed = replace(request, resume=True, session_id=session, prompt="synthetic peer packet")
    route = _route(provider)
    if provider == "claude":
        path = "/v1/messages"
        upstream_path = "/v1/projects/fictional-project/locations/europe-west1/publishers/anthropic/models/claude-opus-5-5:streamRawPredict"
        body = canonical_json({"model": profile.requested_model, "max_tokens": 16,
            "stream": True, "messages": [{"role": "user", "content": request.prompt}], "tools": []})
        response = _claude_response(profile.requested_model, "synthetic final")
        raw = canonical_json({"type": "result", "subtype": "success", "is_error": False,
            "session_id": session, "result": "synthetic final",
            "modelUsage": {profile.requested_model: {}}})
    else:
        path = "/v1beta/models/gemini-3.8-flash:streamGenerateContent?alt=sse"
        upstream_path = "/v1/projects/fictional-project/locations/europe-west1/publishers/google/models/gemini-3.8-flash:streamGenerateContent?alt=sse"
        body = canonical_json({"generationConfig": {"maxOutputTokens": 16},
                              "contents": [{"role": "user", "parts": [{"text": request.prompt}]}], "tools": []})
        response = _agy_response("synthetic final")
        response_document = json.loads(response.body.decode().removeprefix("data: ").strip())
        response_document.update(responseId="synthetic-response", modelVersion="gemini-3.8-flash")
        response = replace(response, body=b"data: " + canonical_json(response_document).rstrip(b"\n") + b"\n\n")
        raw = b"".join(canonical_json(event) for event in (
            {"event": "init", "init": {}, "conversation_id": session},
            {"event": "result", "result": {"conversation_id": session, "status": "SUCCESS",
                                             "response": "synthetic final"}}))
    permit = BrokerPermit(provider, "run", "task", "target", "seat", "initial",
        profile.requested_model, "disposable-placeholder", 1, 1024 * 1024, 1024 * 1024, 8192,
        inputs_digest="a" * 64, session_id=request.session_id,
        upstream_route_digest=route.digest, upstream_origin=route.profile.origin,
        upstream_model=route.profile.effective_model, upstream_path=upstream_path)
    bindings = {name: "b" * 64 for name in
        ("campaign", "casebook", "source", "projection", "profile", "inputs")}
    bindings.update(source=runtime_source_digest(), inputs="a" * 64)
    row = {"role": "candidate-seat", "phase": "initial", "condition": "candidate", "epoch": 1,
        "permit": asdict(permit), "blocker": None, "initial": None, "profile": profile.digest,
        "catalog": None, "process_seconds": 30, "outer_seconds": 60}
    row_digest = diagnostic.quality_row_identity(row)
    resume_row = {**row, "phase": "resume", "epoch": 2, "initial": row_digest,
                  "permit": {**asdict(permit), "route": "resume"}}
    allocation = store.write_json("allocation.json", {"schema": "fanout-quality-case-allocation-v1",
        "case_id": "normal-01", "controller_id": controller.controller_id, "bindings": bindings,
        "maka_output_tokens": 8192, "rows": [row, resume_row]})
    authority = diagnostic.seal_quality_allocation(controller, store, allocation, bindings=bindings)
    permit_id = diagnostic.register_quality_permit(controller, store, authority, ledger,
        role="candidate-seat", phase="initial", condition="candidate", permit=permit,
        epoch=1, bindings=bindings)
    if provider == "agy":
        state = native_boundary._private_state(controller.root, "run", "task", "target", "seat")
        (state / "home" / ".gemini").mkdir(mode=0o700)
        (state / "home" / ".gemini" / "config").mkdir(mode=0o700)
    command_proof = diagnostic.issue_quality_native_command(controller, store, authority,
        request=request, ledger=ledger, permit=permit, epoch=1, bindings=bindings,
        role="candidate-seat", phase="initial", condition="candidate")
    command, _ = diagnostic.reopen_quality_native_command(controller, request, command_proof)
    for _ in range(auxiliary):
        ledger.record_attempt(permit_id, 1)
        ledger.record_auxiliary_refusal(permit_id, 1)
    sender = HostUpstream(permit, route, max_response_bytes=permit.max_response_bytes,
                          deadline_seconds=permit.response_deadline)
    prepared = sender.preview("POST", path, {"content-type": "application/json"}, body)
    identity = OutboundRequestIdentity("POST", path, hashlib.sha256(body).hexdigest(), 16,
        profile.requested_model if provider == "claude" else route.profile.wire_model,
        route.digest, prepared.origin, prepared.path, hashlib.sha256(prepared.body).hexdigest(),
        prepared.effective_model)
    ledger.record_attempt(permit_id, 1)
    ledger.reserve(permit_id, 1, "synthetic-request", identity)
    ledger.claim_send(permit_id, 1, "synthetic-request", permit, identity)
    wire = _capture_private_wire(ledger, permit_id, 1, store.root, "synthetic", {
        "method": "POST", "provider_path": path, "normalized_body": body,
        "effective_body": prepared.body, "upstream_origin": prepared.origin,
        "upstream_path": prepared.path, "upstream_model": prepared.effective_model,
        "upstream_route_digest": route.digest, "http_status": 200,
        "content_type": "text/event-stream", "response_body": response.body,
        "delivered_body": response.body})
    ledger.settle("synthetic-request", hashlib.sha256(response.body).hexdigest())
    ledger.fence_epoch(permit_id, 1)
    executable = tmp_path / "inert-provider"
    executable.write_bytes(b"synthetic fixture executable; never launched\n")
    monkeypatch.setattr(native_boundary, "_executable", lambda _name: executable)
    state = controller.root / "native-seat-state" / hashlib.sha256(
        canonical_json(("run", "task", "target", "seat"))).hexdigest()
    command_digest = native_boundary._command_digest(command)
    permit_digest = hashlib.sha256(canonical_json(asdict(permit))).hexdigest()
    admission = write_evidence(controller, "native-broker-diagnostic-admission",
        native_boundary._broker_admission_payload(request, verification.evidence_digest,
            command_digest, (12345, "synthetic-closed-listener", permit_digest, ""), None,
            quality_admission=command_proof))
    policy = native_boundary._policy(workspace.root, state, executable, (repository,), request.staged_skill_root,
                                     execution_class="read-only", broker_port=12345)
    policy_digest = hashlib.sha256(policy).hexdigest()
    policy_path = native_boundary._profile_path(controller.root, policy_digest)
    native_boundary._write_immutable(policy_path, policy)
    info = workspace.root.stat()
    boundary_payload = {
        "schema_version": "fanout-native-seat-boundary-v1", "controller_id": controller.controller_id,
        "run_id": "run", "task_id": "task", "target_id": "target", "seat_id": "seat",
        "inputs_digest": "a" * 64, "workspace": str(workspace.root),
        "workspace_device": info.st_dev, "workspace_inode": info.st_ino,
        "workspace_receipt": verification.evidence_digest, "cwd": str(workspace.root),
        "profile_digest": profile.digest, "route": native_boundary._route(request),
        "state_root": str(state), "executable": str(executable),
        "executable_digest": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "resolved_argv": [str(executable), *command.argv[1:]], "command_digest": command_digest,
        "environment_digest": "c" * 64, "profile_path": str(policy_path),
        "profile_sha256": policy_digest, "denied_paths": [str(repository)], "broker_port": 12345,
        "broker_binding_id": "synthetic-closed-listener", "broker_permit_sha256": permit_digest,
        "broker_admission_name": admission[0], "broker_admission_digest": admission[1]}
    if skills:
        boundary_payload["staged_skill_grant"] = native_boundary._staged_skill_grant(
            request, (repository,), workspace.root, controller.root)
    boundary = write_evidence(controller, "native-seat-boundary", boundary_payload)
    terminal = write_evidence(controller, "brokered-provider-diagnostic", {
        "schema_version": "fanout-brokered-diagnostic-v1", "run_id": "run", "task_id": "task",
        "target_id": "target", "seat_id": "seat", "inputs_digest": "a" * 64,
        "profile_digest": profile.digest, "boundary_receipt_digest": boundary[1],
        "route": native_boundary._route(request), "session_id": session,
        "valid": True, "reason": "ok", "process_status": "exit", "exit_code": 0,
        "stdout_sha256": hashlib.sha256(raw).hexdigest(), "stderr_sha256": hashlib.sha256(b"").hexdigest(),
        "observed_model": profile.requested_model if provider == "claude" else None,
        "broker_model": profile.requested_model, "effective_output_tokens": [16],
        "refused_agy_auxiliary_requests": auxiliary})
    request_ref = _request_artifact(store, request)
    proof = write_evidence(controller, "quality-readonly-turn", {
        "schema": "fanout-quality-readonly-turn-v1", "command": list(command_proof),
        "terminal": list(terminal), "request": asdict(request_ref),
        "stdout": asdict(store.write_bytes("stdout.bin", raw)),
        "stderr": asdict(store.write_bytes("stderr.bin", b"")),
        "final": asdict(store.write_bytes("final.txt", b"synthetic final")),
        "records": list(ledger.request_records(permit_id, 1)), "prepared": [],
        "wires": {"synthetic-request": wire}, "route": asdict(route),
        "main_attempts": auxiliary + 1, "auxiliary_attempts": auxiliary,
        "elapsed": 1.0, "outer_seconds": 60})
    return SimpleNamespace(**locals())


def _reopen(f, **changes):
    assert hasattr(diagnostic, "reopen_quality_predecessor"), "cold quality predecessor API is missing"
    arguments = dict(controller=f.controller, store=f.store, request=f.resumed, proof=f.proof,
        verification=f.verification, authority=f.authority, bindings=f.bindings,
        initial_digest=f.row_digest)
    arguments.update(changes)
    return diagnostic.reopen_quality_predecessor(**arguments)


def test_cold_quality_predecessor_reopens_only_the_original_receipt_and_session(tmp_path, monkeypatch):
    f = _fixture(tmp_path, monkeypatch)
    controller = fanout.resume_lifecycle_controller(f.controller.root, f.controller.capability)
    with ArtifactStore.open_existing(f.store.root) as store:
        pointer = _reopen(f, controller=controller, store=store)
    assert pointer.quality_proof == f.proof
    assert (pointer.receipt_name, pointer.receipt_digest) == f.terminal
    assert pointer.row_digest == f.row_digest
    assert pointer.session_id == "fd6f840b-4b79-4fd2-91d0-14d3d8fba655"
    assert not hasattr(pointer, "boundary") and not hasattr(pointer, "result")
    with pytest.raises(AttributeError):
        pointer.session_id = "forged"


@pytest.mark.parametrize("field,value", [
    ("session_id", "foreign-session"), ("run_id", "foreign-run"), ("task_id", "foreign-task"),
    ("target_id", "foreign-target"), ("seat_id", "foreign-seat"), ("inputs_digest", "e" * 64),
    ("context_sha256", "e" * 64), ("skill_bundle_sha256", "e" * 64)])
def test_cold_quality_predecessor_refuses_current_request_substitution(tmp_path, monkeypatch, field, value):
    f = _fixture(tmp_path, monkeypatch)
    with pytest.raises(ProviderRequestError):
        _reopen(f, request=replace(f.resumed, **{field: value}))


@pytest.mark.parametrize("tamper", ["foreign-controller", "row", "bindings", "profile", "prompt",
    "stdout", "final", "unfenced", "uncertain", "terminal-session", "terminal-route",
    "unrelated-command", "missing-boundary", "missing-admission", "workspace"])
def test_cold_quality_predecessor_rejects_material_authority_and_byte_tampering(tmp_path, monkeypatch, tamper):
    f = _fixture(tmp_path, monkeypatch)
    arguments = {}
    receipt = read_evidence(f.controller, "quality-readonly-turn", *f.proof)
    if tamper == "foreign-controller":
        arguments["controller"] = fanout.create_lifecycle_controller(tmp_path / "foreign")
    elif tamper == "row":
        arguments["initial_digest"] = "e" * 64
    elif tamper == "bindings":
        arguments["bindings"] = {**f.bindings, "inputs": "e" * 64}
    elif tamper == "profile":
        arguments["request"] = replace(f.resumed, profile=replace(f.profile,
            requested_model="foreign-model", profile_sha256=None))
    elif tamper in {"prompt", "stdout", "final"}:
        descriptor = f.store.read_json(f.request_ref)
        ref = descriptor["prompt"] if tamper == "prompt" else receipt[tamper]
        (f.store.root / ref["path"]).write_bytes(b"substituted bytes")
    elif tamper in {"unfenced", "uncertain"}:
        with f.ledger._locked() as state:
            if tamper == "unfenced":
                state["permits"][f.permit_id]["fenced"] = False
            else:
                state["requests"]["synthetic-request"].update(state="uncertain", response_digest=None)
            f.ledger._save(state)
    elif tamper in {"terminal-session", "terminal-route"}:
        terminal = read_evidence(f.controller, "brokered-provider-diagnostic", *f.terminal)
        terminal["session_id" if tamper == "terminal-session" else "route"] = "foreign"
        receipt["terminal"] = list(write_evidence(f.controller, "brokered-provider-diagnostic", terminal))
        arguments["proof"] = write_evidence(f.controller, "quality-readonly-turn", receipt)
    elif tamper == "unrelated-command":
        command = read_evidence(f.controller, "quality-native-command", *f.command_proof)
        command["allocation"] = [f.authority[0], "e" * 64]
        receipt["command"] = list(write_evidence(f.controller, "quality-native-command", command))
        arguments["proof"] = write_evidence(f.controller, "quality-readonly-turn", receipt)
    elif tamper == "workspace":
        (f.workspace.root / "fixture.txt").write_text("different observed workspace\n")
        subprocess.run(("git", "-C", str(f.workspace.root), "add", "-A"), check=True)
        subprocess.run(("git", "-C", str(f.workspace.root), "-c", "user.name=Test",
            "-c", "user.email=test@example.invalid", "commit", "-qm", "changed"), check=True)
        changed = fanout.capture_repository_baseline(f.workspace.root)
        arguments["verification"] = fanout.verify_seat_workspace(changed,
            replace(f.workspace, baseline_digest=changed.digest), controller=f.controller)
    else:
        category, ref = ("native-seat-boundary", f.boundary) if tamper == "missing-boundary" else (
            "native-broker-diagnostic-admission", f.admission)
        (f.controller.root / "receipts" / ref[0]).unlink()
    with pytest.raises(ProviderRequestError):
        _reopen(f, **arguments)


def test_quality_auxiliary_attempts_are_counted_once_without_replenishing_main_cap(tmp_path, monkeypatch):
    f = _fixture(tmp_path, monkeypatch, provider="agy", auxiliary=3)
    receipt = read_evidence(f.controller, "quality-readonly-turn", *f.proof)
    receipt.pop("request")
    proof = write_evidence(f.controller, "quality-readonly-turn", receipt)
    value = diagnostic.validate_quality_readonly_turn(f.controller, f.store, f.request, proof)
    assert (value["main_attempts"], value["auxiliary_attempts"]) == (4, 3)
    assert len(value["records"]) == 1
    with pytest.raises(fanout.broker.BrokerRejected, match="fenced"):
        f.ledger.reserve(f.permit_id, 1, "second-main", f.identity)


def test_claimed_session_and_unattached_pointer_fail_before_installed_probe(tmp_path, monkeypatch):
    f = _fixture(tmp_path, monkeypatch)
    pointer = _reopen(f)
    monkeypatch.setattr(fanout.ProviderRegistry, "assert_installed_version",
                        lambda *_args: pytest.fail("unattached pointer reached installed probe"))
    for initial in (f.session, pointer):
        with pytest.raises(ProviderRequestError):
            diagnostic.run_brokered_provider_diagnostic(f.resumed, controller=f.controller,
                verification=f.verification, broker_listener=None, other_target_roots=(),
                denied_owner_roots=(), initial=initial)


def _resume_admission(f):
    permit = replace(f.permit, route="resume", session_id=f.session)
    diagnostic.register_quality_permit(f.controller, f.store, f.authority, f.ledger,
        role="candidate-seat", phase="resume", condition="candidate", permit=permit,
        epoch=2, bindings=f.bindings, initial=f.proof)
    return diagnostic.issue_quality_native_command(f.controller, f.store, f.authority,
        request=f.resumed, ledger=f.ledger, permit=permit, epoch=2, bindings=f.bindings,
        role="candidate-seat", phase="resume", condition="candidate")


class NativeLaunchPrevented(Exception):
    pass


def test_cold_quality_resume_consumes_exact_receipt_tuple_and_fences_replay(tmp_path, monkeypatch):
    f = _fixture(tmp_path, monkeypatch)
    pointer = _reopen(f)
    admission = _resume_admission(f)
    controller = fanout.resume_lifecycle_controller(f.controller.root, f.controller.capability)
    monkeypatch.setattr(fanout.ProviderRegistry, "assert_installed_version", lambda *_args: None)
    admitted = []
    def stop_before_native(_controller, _verification, _request, _command, _listener, **arguments):
        admitted.append(arguments)
        raise NativeLaunchPrevented
    monkeypatch.setattr(diagnostic, "issue_broker_admission", stop_before_native)
    with pytest.raises(NativeLaunchPrevented):
        diagnostic.run_brokered_provider_diagnostic(f.resumed, controller=controller,
            verification=f.verification, broker_listener=None, other_target_roots=(),
            denied_owner_roots=(), initial=pointer, quality_admission=admission)
    assert admitted == [{"initial_receipt": f.terminal, "quality_admission": admission}]
    controller = fanout.resume_lifecycle_controller(f.controller.root, f.controller.capability)
    with pytest.raises(ProviderRequestError, match="already attempted"):
        diagnostic.run_brokered_provider_diagnostic(f.resumed, controller=controller,
            verification=f.verification, broker_listener=None, other_target_roots=(),
            denied_owner_roots=(), initial=pointer, quality_admission=admission)
    assert len(admitted) == 1


@pytest.mark.parametrize("field,value", [
    ("session_id", "claimed-session"), ("row_digest", "e" * 64),
    ("receipt_digest", "e" * 64), ("quality_proof", ("foreign.json", "e" * 64))])
def test_cold_pointer_substitution_fails_before_installed_probe(tmp_path, monkeypatch, field, value):
    f = _fixture(tmp_path, monkeypatch)
    pointer = replace(_reopen(f), **{field: value})
    admission = _resume_admission(f)
    monkeypatch.setattr(fanout.ProviderRegistry, "assert_installed_version",
                        lambda *_args: pytest.fail("substituted pointer reached installed probe"))
    with pytest.raises(ProviderRequestError):
        diagnostic.run_brokered_provider_diagnostic(f.resumed, controller=f.controller,
            verification=f.verification, broker_listener=None, other_target_roots=(),
            denied_owner_roots=(), initial=pointer, quality_admission=admission)


def test_cold_resume_prompt_substitution_fails_before_installed_probe(tmp_path, monkeypatch):
    f = _fixture(tmp_path, monkeypatch)
    pointer = _reopen(f)
    admission = _resume_admission(f)
    monkeypatch.setattr(fanout.ProviderRegistry, "assert_installed_version",
                        lambda *_args: pytest.fail("changed resume prompt reached installed probe"))
    with pytest.raises(ProviderRequestError):
        diagnostic.run_brokered_provider_diagnostic(replace(f.resumed, prompt="substituted peer packet"),
            controller=f.controller, verification=f.verification, broker_listener=None,
            other_target_roots=(), denied_owner_roots=(), initial=pointer, quality_admission=admission)


def test_other_valid_signed_quality_receipt_cannot_replace_the_allocated_predecessor(tmp_path, monkeypatch):
    f = _fixture(tmp_path, monkeypatch)
    receipt = read_evidence(f.controller, "quality-readonly-turn", *f.proof)
    receipt["elapsed"] = 2.0
    other_proof = write_evidence(f.controller, "quality-readonly-turn", receipt)
    pointer = _reopen(f, proof=other_proof)
    assert pointer.quality_proof != f.proof
    admission = _resume_admission(f)
    monkeypatch.setattr(fanout.ProviderRegistry, "assert_installed_version",
                        lambda *_args: pytest.fail("other valid receipt reached installed probe"))
    with pytest.raises(ProviderRequestError):
        diagnostic.run_brokered_provider_diagnostic(f.resumed, controller=f.controller,
            verification=f.verification, broker_listener=None, other_target_roots=(),
            denied_owner_roots=(), initial=pointer, quality_admission=admission)


def test_quality_predecessor_refuses_raw_session_even_when_hashes_are_resigned(tmp_path, monkeypatch):
    f = _fixture(tmp_path, monkeypatch)
    raw = json.loads(f.raw)
    raw["session_id"] = "foreign-session"
    raw_ref = f.store.write_bytes("other-stdout.bin", canonical_json(raw))
    terminal = read_evidence(f.controller, "brokered-provider-diagnostic", *f.terminal)
    terminal["stdout_sha256"] = raw_ref.digest
    receipt = read_evidence(f.controller, "quality-readonly-turn", *f.proof)
    receipt["stdout"] = asdict(raw_ref)
    receipt["terminal"] = list(write_evidence(f.controller, "brokered-provider-diagnostic", terminal))
    proof = write_evidence(f.controller, "quality-readonly-turn", receipt)
    with pytest.raises(ProviderRequestError):
        _reopen(f, proof=proof)


@pytest.mark.parametrize("tamper", ["missing-request", "control-injection", "oversized-request",
                                    "changed-profile", "changed-controls", "changed-row-lock"])
def test_cold_quality_request_ref_is_closed_bounded_and_authenticated(tmp_path, monkeypatch, tamper):
    f = _fixture(tmp_path, monkeypatch)
    receipt = read_evidence(f.controller, "quality-readonly-turn", *f.proof)
    descriptor = f.store.read_json(f.request_ref)
    if tamper == "missing-request":
        receipt.pop("request")
    elif tamper == "oversized-request":
        receipt["request"] = asdict(f.store.write_bytes("oversized-request.json", b" " * (64 * 1024 + 1)))
    elif tamper == "changed-row-lock":
        claim = {"allocation": f.authority[1], "row": f.row}
        digest = hashlib.sha256(canonical_json(claim)).hexdigest()
        (f.controller.root / "locks" / f"quality-row-{digest}").write_bytes(b"{}\n")
    else:
        if tamper == "control-injection":
            descriptor["controls"]["environment"] = {"CREDENTIAL": "fictional"}
        elif tamper == "changed-controls":
            descriptor["controls"]["slot_cap"] = 1
        else:
            profile = f.profile.to_dict()
            profile["requested_model"] = "foreign-model"
            descriptor["profile"] = asdict(f.store.write_json("changed-profile.json", profile))
        receipt["request"] = asdict(f.store.write_json("changed-request.json", descriptor))
    proof = write_evidence(f.controller, "quality-readonly-turn", receipt)
    with pytest.raises(ProviderRequestError):
        _reopen(f, proof=proof)


def test_unassigned_agy_cold_predecessor_retains_observed_session(tmp_path, monkeypatch):
    f = _fixture(tmp_path, monkeypatch, provider="agy", auxiliary=3)
    pointer = _reopen(f)
    assert f.request.session_id is None
    assert pointer.session_id == "fd6f840b-4b79-4fd2-91d0-14d3d8fba655"
    assert read_evidence(f.controller, "brokered-provider-diagnostic", *f.terminal)["route"] == "initial:unassigned"


def test_quality_request_retention_preserves_exact_prompt_and_profile_refs(tmp_path, monkeypatch):
    f = _fixture(tmp_path, monkeypatch)
    assert hasattr(diagnostic, "_retain_quality_request"), "durable original request retention is missing"
    ref = diagnostic._retain_quality_request(f.store, f.request, "retained")
    descriptor = f.store.read_json(ref)
    assert set(descriptor) == {"schema", "controls", "prompt", "profile"}
    assert descriptor["schema"] == "fanout-quality-request-v1"
    assert f.store.read_bytes(ArtifactRef(**descriptor["prompt"])) == b"synthetic initial question"
    assert f.store.read_json(ArtifactRef(**descriptor["profile"])) == f.profile.to_dict()
    assert diagnostic._reopen_quality_request(f.store, ref, f.resumed).prompt_bytes == b"synthetic initial question"


def test_quality_request_retention_refuses_oversized_controls(tmp_path, monkeypatch):
    f = _fixture(tmp_path, monkeypatch)
    request = replace(f.request, timeout_override_reason="x" * (64 * 1024),
                      timeout_override_review_sha256="e" * 64)
    with pytest.raises(ProviderRequestError, match="bound"):
        diagnostic._retain_quality_request(f.store, request, "oversized-controls")


@pytest.mark.parametrize("component,limit", [("descriptor", 64 * 1024),
    ("profile", 64 * 1024), ("prompt", 1024 * 1024)])
def test_quality_actual_underclaimed_request_bytes_refused_before_any_read_or_allocation(
    tmp_path, monkeypatch, component, limit,
):
    f = _fixture(tmp_path, monkeypatch)
    oversized = f.store.write_bytes("oversized-" + component, b" " * (limit + 1))
    claimed = replace(oversized, size=1)
    descriptor = f.store.read_json(f.request_ref)
    if component == "descriptor":
        request_ref = claimed
    else:
        descriptor[component] = asdict(claimed)
        request_ref = f.store.write_json("underclaimed-request.json", descriptor)
    receipt = read_evidence(f.controller, "quality-readonly-turn", *f.proof)
    receipt["request"] = asdict(request_ref)
    f.proof = write_evidence(f.controller, "quality-readonly-turn", receipt)
    inode = (f.store.root / oversized.path).stat().st_ino
    original_read, read_bytes = os.read, []
    def count_read(fd, size):
        chunk = original_read(fd, size)
        if os.fstat(fd).st_ino == inode:
            read_bytes.append(len(chunk))
        return chunk
    monkeypatch.setattr(os, "read", count_read)
    with pytest.raises(ProviderRequestError):
        _run_resume(f)
    assert sum(read_bytes) == 0
    with f.ledger._locked() as state:
        assert len(state["permits"]) == 1


def test_quality_bounded_fd_read_refuses_actual_growth_without_reading_past_cap(tmp_path, monkeypatch):
    store = ArtifactStore(tmp_path / "bounded-artifacts")
    limit = 64 * 1024
    ref = store.write_bytes("growing.bin", b"a" * limit)
    path = store.root / ref.path
    inode = path.stat().st_ino
    original_read, observed = os.read, []
    def grow_on_read(fd, size):
        if os.fstat(fd).st_ino == inode and not observed:
            with path.open("ab") as output:
                output.write(b"b")
        chunk = original_read(fd, size)
        if os.fstat(fd).st_ino == inode:
            observed.append(len(chunk))
        return chunk
    monkeypatch.setattr(os, "read", grow_on_read)
    with pytest.raises(ProviderRequestError, match="changed during bounded read"):
        diagnostic._read_quality_request_artifact(store, ref, limit)
    assert sum(observed) == limit


@pytest.mark.parametrize("tamper", ["unknown", "traversal", "nonclosure", "hash",
    "current-origin", "incidental-origin", "canonical-foreign-origin", "canonical-fanout-foreign-origin",
    "missing-core"])
def test_quality_prior_source_closure_rejects_absent_claims_and_current_origin_drift(
    tmp_path, monkeypatch, tamper,
):
    from scripts import fanout_council_campaign
    f = _fixture(tmp_path, monkeypatch)
    native = read_evidence(f.controller, "quality-readonly-turn", *f.proof)
    command = read_evidence(f.controller, "quality-native-command", *native["command"])
    if tamper in {"unknown", "traversal", "nonclosure"}:
        name = {"unknown": "scripts/absent-quality-source.py", "traversal": "../outside.py",
                "nonclosure": "tests/test_fanout_quality_predecessor.py"}[tamper]
        command["loaded"][name] = "e" * 64
    elif tamper == "hash":
        command["loaded"]["shared/lib/fanout/diagnostic.py"] = "e" * 64
    elif tamper == "missing-core":
        command["loaded"].pop("shared/lib/fanout/providers.py")
    elif tamper == "current-origin":
        monkeypatch.setattr(native_boundary, "__file__", str(tmp_path / "foreign-native.py"))
    elif tamper == "canonical-foreign-origin":
        foreign = str(tmp_path / "foreign-campaign.py")
        monkeypatch.setattr(fanout_council_campaign, "__file__", foreign)
        monkeypatch.setattr(fanout_council_campaign.__spec__, "origin", foreign)
    elif tamper == "canonical-fanout-foreign-origin":
        from shared.lib.fanout import quality
        foreign = str(tmp_path / "foreign-quality.py")
        monkeypatch.setattr(quality, "__file__", foreign)
        monkeypatch.setattr(quality.__spec__, "origin", foreign)
    else:
        monkeypatch.setattr(fanout_council_campaign, "__file__", str(tmp_path / "foreign-campaign.py"))
    proof = write_evidence(f.controller, "quality-native-command", command)
    with pytest.raises(ProviderRequestError, match="source|origin"):
        diagnostic.validate_quality_prior_command(f.controller, f.resumed, proof)
    with f.ledger._locked() as state:
        assert len(state["permits"]) == 1


def test_quality_prior_source_ignores_unrelated_module_with_same_basename(tmp_path, monkeypatch):
    import importlib.machinery
    from types import ModuleType
    f = _fixture(tmp_path, monkeypatch)
    unrelated = ModuleType("unrelated.fanout_council_campaign")
    unrelated.__file__ = str(tmp_path / "foreign/fanout_council_campaign.py")
    unrelated.__spec__ = importlib.machinery.ModuleSpec(unrelated.__name__, None,
        origin=unrelated.__file__)
    monkeypatch.setitem(sys.modules, unrelated.__name__, unrelated)
    native = read_evidence(f.controller, "quality-readonly-turn", *f.proof)
    diagnostic.validate_quality_prior_command(f.controller, f.resumed, native["command"])
    with f.ledger._locked() as state:
        assert len(state["permits"]) == 1


def test_packaged_execute_helper_alias_uses_fixed_active_source_and_refuses_origin_drift(tmp_path):
    source = Path(__file__).resolve().parents[1]
    skill = tmp_path / "packaged-execute"
    runtime = skill / ".runtime"
    for name in ("shared/lib/fanout", "components/fanout-container", "shared/skills/llm-fanout-plan",
                 "shared/skills/llm-fanout-execute", "scripts"):
        shutil.copytree(source / name, runtime / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in ("capabilities.toml", "mise.toml", "mise.lock"):
        shutil.copy2(source / name, runtime / name)
    shutil.copytree(source / "shared/skills/llm-fanout-execute", skill, dirs_exist_ok=True)
    child = """
import importlib.util, json, sys
from pathlib import Path
helper_path = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location('fixed_packaged_execute', helper_path)
helper = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = helper
spec.loader.exec_module(helper)
runtime = helper._runtime()
diagnostic = __import__(runtime.__name__ + '.diagnostic', fromlist=['_quality_loaded_sources'])
from shared.lib.fanout.container_source import runtime_source_digest
runtime_source_digest()
key = 'shared/skills/llm-fanout-execute/scripts/execute.py'
assert key in diagnostic._quality_loaded_sources()
original = helper.__file__
helper.__file__ = str(helper_path.parent / 'foreign-helper.py')
try:
    diagnostic._quality_loaded_sources()
except runtime.ProviderRequestError:
    pass
else:
    raise AssertionError('foreign installed helper origin accepted')
helper.__file__ = original
print(json.dumps({'alias': key, 'origin_refused': True, 'synthetic_bundle_not_install_attestation': True}))
"""
    process = subprocess.run((sys.executable, "-B", "-c", child, str(skill / "scripts/execute.py")),
        cwd=tmp_path, capture_output=True, text=True)
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout)["origin_refused"] is True


def _run_resume(f, request=None):
    return diagnostic.run_quality_readonly_turn(request or f.resumed,
        controller=f.controller, store=f.store, authority=f.authority, ledger=f.ledger,
        permit=replace(f.permit, route="resume"), epoch=2, bindings=f.bindings,
        role="candidate-seat", phase="resume", condition="candidate", route=f.route,
        credential="fictional", verification=f.verification, other_target_roots=(),
        denied_owner_roots=(), initial=_reopen(f), initial_digest=f.row_digest,
        initial_proof=f.proof)


def test_quality_retained_request_bound_failure_does_not_consume_resume_row(tmp_path, monkeypatch):
    f = _fixture(tmp_path, monkeypatch)
    request = replace(f.resumed, timeout_override_reason="x" * (64 * 1024),
                      timeout_override_review_sha256="e" * 64)
    with pytest.raises(ProviderRequestError, match="bound"):
        _run_resume(f, request)
    with f.ledger._locked() as state:
        assert len(state["permits"]) == 1
    resume_row = diagnostic.reopen_quality_allocation(
        f.controller, f.store, f.authority, bindings=f.bindings)["rows"][1]
    digest = hashlib.sha256(canonical_json({"allocation": f.authority[1], "row": resume_row})).hexdigest()
    assert not (f.controller.root / "locks" / f"quality-row-{digest}").exists()


def test_quality_retained_request_storage_failure_fences_registered_resume(tmp_path, monkeypatch):
    f = _fixture(tmp_path, monkeypatch)
    f.store.write_bytes("quality/candidate-seat/resume/2/prompt.bin", b"already present")
    monkeypatch.setattr(diagnostic, "BrokerListener",
                        lambda *_args, **_kwargs: pytest.fail("failed publication reached listener"))
    with pytest.raises(fanout.ArtifactExistsError):
        _run_resume(f)
    with f.ledger._locked() as state:
        assert len(state["permits"]) == 2
        assert all(permit["fenced"] for permit in state["permits"].values())
    with pytest.raises(ProviderRequestError, match="already consumed"):
        _run_resume(f)


def test_quality_resume_refuses_claimed_session_before_consuming_allocated_row(tmp_path, monkeypatch):
    f = _fixture(tmp_path, monkeypatch)
    with pytest.raises(ProviderRequestError):
        diagnostic.run_quality_readonly_turn(f.resumed, controller=f.controller, store=f.store,
            authority=f.authority, ledger=f.ledger, permit=replace(f.permit, route="resume"),
            epoch=2, bindings=f.bindings, role="candidate-seat", phase="resume", condition="candidate",
            route=f.route, credential="fictional", verification=f.verification,
            other_target_roots=(), denied_owner_roots=(), initial=f.session,
            initial_digest=f.row_digest, initial_proof=f.proof)
    with f.ledger._locked() as state:
        assert len(state["permits"]) == 1


@pytest.mark.parametrize("tamper", ["claimed-verification", "removed-workspace-evidence"])
def test_cold_quality_predecessor_requires_actual_workspace_authority(tmp_path, monkeypatch, tamper):
    f = _fixture(tmp_path, monkeypatch)
    verification = f.verification
    if tamper == "claimed-verification":
        verification = SimpleNamespace(evidence_digest=verification.evidence_digest)
    else:
        (f.controller.root / "receipts" / verification.evidence_name).unlink()
    with pytest.raises(ProviderRequestError):
        _reopen(f, verification=verification)


@pytest.mark.parametrize("tamper", [None, "absent-admission", "changed-engine-evidence", "changed-manifest"])
def test_cold_quality_predecessor_revalidates_original_staged_skill_authority(tmp_path, monkeypatch, tamper):
    f = _fixture(tmp_path, monkeypatch, skills=True)
    request = f.resumed
    if tamper is None:
        pointer = _reopen(f)
        assert pointer.session_id == "fd6f840b-4b79-4fd2-91d0-14d3d8fba655"
        return
    if tamper == "absent-admission":
        request = replace(request, staged_skill_admission=None)
    elif tamper == "changed-engine-evidence":
        admission = replace(f.skill_admission,
                            engine_evidence=(replace(f.event, truncated=True),))
        request = replace(request, staged_skill_admission=admission)
    else:
        manifest = f.skill_admission.staged_root / "manifest.json"
        manifest.chmod(0o600)
        manifest.write_bytes(b"{}\n")
    with pytest.raises(ProviderRequestError):
        _reopen(f, request=request)


def test_fresh_child_reopens_predecessor_without_any_prior_result_or_listener(tmp_path, monkeypatch):
    # A real fixed source module may be loaded by parent collection without being
    # required by the child's fixed protected assembly. Do not import receipt paths.
    from scripts import fanout_council_campaign  # noqa: F401
    f = _fixture(tmp_path, monkeypatch)
    payload = {
        "controller": str(f.controller.root), "capability": f.controller.capability.export_token(),
        "store": str(f.store.root), "workspace": str(f.workspace.root),
        "workspace_baseline": f.workspace.baseline_digest,
        "workspace_evidence": f.verification.evidence_digest,
        "request": asdict(f.request_ref), "session": f.session,
        "proof": f.proof, "authority": f.authority, "bindings": f.bindings,
        "initial_digest": f.row_digest, "inert_executable": str(f.executable)}
    child = """
import json, sys
from dataclasses import asdict, replace
from pathlib import Path
from shared.lib import fanout
from shared.lib.fanout import diagnostic, native_boundary
from scripts import fanout_container_certify
value = json.load(sys.stdin)
controller = fanout.resume_lifecycle_controller(Path(value['controller']),
    fanout.LifecycleCapability(value['capability']))
workspace = fanout.SeatWorkspace(Path(value['workspace']), 'seat', value['workspace_baseline'])
verification = fanout.resume_seat_workspace(workspace, controller=controller,
    evidence_digest=value['workspace_evidence'])
with fanout.ArtifactStore.open_existing(value['store']) as store:
    descriptor = store.read_json(fanout.ArtifactRef(**value['request']))
    profile = fanout.ExecutorProfile.from_dict(store.read_json(fanout.ArtifactRef(**descriptor['profile'])),
        adapter=fanout.ProviderRegistry.default().require('claude'))
    request = fanout.ProviderRequest(**descriptor['controls'], prompt=b'synthetic initial question', profile=profile)
    request = replace(request, resume=True, session_id=value['session'], prompt=b'synthetic peer packet')
    native_boundary._executable = lambda _name: Path(value['inert_executable'])
    from shared.lib.fanout.controller import read_evidence
    from shared.lib.fanout.container_source import runtime_source_digest
    native = read_evidence(controller, 'quality-readonly-turn', *value['proof'])
    command = read_evidence(controller, 'quality-native-command', *native['command'])
    loaded = diagnostic._quality_loaded_sources()
    print(json.dumps({'runtime_match': runtime_source_digest() == command['bindings']['source'],
        'missing_loaded': sorted(set(command['loaded']) - set(loaded)),
        'changed_loaded': sorted(name for name in command['loaded'] if name in loaded and command['loaded'][name] != loaded[name]),
        'identity_match': command['registration']['permit']['provider'] == request.executor_id and all(
            command['registration']['permit'][name] == getattr(request, name)
            for name in ('run_id', 'task_id', 'target_id', 'seat_id', 'inputs_digest')),
        'session_match': command['registration']['permit']['session_id'] == request.session_id}), file=sys.stderr)
    pointer = diagnostic.reopen_quality_predecessor(controller, store, request, tuple(value['proof']),
        verification=verification, authority=tuple(value['authority']), bindings=value['bindings'],
        initial_digest=value['initial_digest'])
    print(json.dumps(asdict(pointer), sort_keys=True))
"""
    process = subprocess.run((sys.executable, "-B", "-c", child), input=canonical_json(payload),
                             cwd=Path(__file__).resolve().parents[1], capture_output=True)
    assert process.returncode == 0, process.stderr.decode()
    pointer = json.loads(process.stdout)
    assert pointer == {"quality_proof": list(f.proof), "receipt_name": f.terminal[0],
        "receipt_digest": f.terminal[1], "row_digest": f.row_digest,
        "session_id": "fd6f840b-4b79-4fd2-91d0-14d3d8fba655"}
