"""Disposable provider turns through the controller's native broker boundary."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from dataclasses import asdict

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.lib import fanout  # noqa: E402
from shared.lib.fanout import diagnostic  # noqa: E402
from shared.lib.fanout.broker import (  # noqa: E402
    Broker, BrokerListener, BrokerPermit, BrokerResponse,
)
from shared.lib.fanout.controller import read_evidence  # noqa: E402
from shared.lib.fanout.errors import ProviderRequestError  # noqa: E402
from shared.lib.fanout.native_boundary import issue_broker_admission  # noqa: E402


def test_quality_allocation_consumes_authenticated_row_before_registration(tmp_path):
    from shared.lib.fanout.artifacts import ArtifactStore
    from shared.lib.fanout.container_broker import PermitLedger
    controller = fanout.create_lifecycle_controller(tmp_path / "owner")
    ledger = PermitLedger(controller.root / "locks" / "spend", controller)
    permit = BrokerPermit("claude", "run", "task", "target", "seat", "initial",
                          "claude-opus-5-5", "disposable-placeholder", 1,
                          1024, 2048, 8192, inputs_digest="a" * 64)
    bindings = {name: "b" * 64 for name in (
        "campaign", "casebook", "source", "projection", "profile", "inputs")}
    bindings["inputs"] = "a" * 64
    row = {"role": "baseline-seat", "phase": "initial", "condition": "baseline",
           "epoch": 1, "permit": asdict(permit), "blocker": None,
           "initial": None, "profile": "b" * 64, "catalog": None,
           "process_seconds": 30, "outer_seconds": 60}
    with ArtifactStore(tmp_path / "artifacts") as store:
        ref = store.write_json("quality/normal-01.json", {
            "schema": "fanout-quality-case-allocation-v1", "case_id": "normal-01",
            "controller_id": controller.controller_id, "bindings": bindings,
            "maka_output_tokens": 8192, "rows": [row]})
        authority = diagnostic.seal_quality_allocation(controller, store, ref,
                                                       bindings=bindings)
        registered = diagnostic.register_quality_permit(
            controller, store, authority, ledger, role="baseline-seat", phase="initial",
            condition="baseline", permit=permit, epoch=1, bindings=bindings)
        ledger.require_permit(registered, 1, permit)
        with pytest.raises(ProviderRequestError, match="consumed"):
            diagnostic.register_quality_permit(
                controller, store, authority, ledger, role="baseline-seat", phase="initial",
                condition="baseline", permit=permit, epoch=1, bindings=bindings)
        with pytest.raises(ProviderRequestError, match="allocation"):
            diagnostic.register_quality_permit(
                controller, store, authority, ledger, role="off-list", phase="initial",
                condition="baseline", permit=permit, epoch=2, bindings=bindings)
        with pytest.raises(ProviderRequestError, match="allocation"):
            diagnostic.reopen_quality_allocation(controller, store, authority,
                bindings=dict(bindings, source="c" * 64))


def test_quality_native_command_reopens_consumed_row_and_loaded_source(tmp_path):
    from shared.lib.fanout.artifacts import ArtifactStore
    from shared.lib.fanout.container_broker import PermitLedger
    from shared.lib.fanout.container_source import runtime_source_digest
    controller = fanout.create_lifecycle_controller(tmp_path / "owner")
    ledger = PermitLedger(controller.root / "locks" / "spend", controller)
    profile = fanout.ProviderRegistry.default().select("claude", "read-only", "standard")
    request = fanout.ProviderRequest("claude", "inert command composition", cwd=tmp_path,
        profile=profile, session_id="fd6f840b-4b79-4fd2-91d0-14d3d8fba655",
        run_id="run", task_id="task", target_id="target", seat_id="seat",
        inputs_digest="a" * 64, timeout=30)
    permit = BrokerPermit("claude", "run", "task", "target", "seat", "initial",
        profile.requested_model, "disposable-placeholder", 1, 1024, 2048, 8192,
        inputs_digest="a" * 64, session_id=request.session_id)
    bindings = {name: "b" * 64 for name in
        ("campaign", "casebook", "source", "projection", "profile", "inputs")}
    bindings.update(source=runtime_source_digest(), inputs="a" * 64)
    row = {"role": "baseline-seat", "phase": "initial", "condition": "baseline",
        "epoch": 1, "permit": asdict(permit), "blocker": None, "initial": None,
        "profile": profile.digest, "catalog": None,
        "process_seconds": 30, "outer_seconds": 60}
    with ArtifactStore(tmp_path / "artifacts") as store:
        ref = store.write_json("quality/case.json", {
            "schema": "fanout-quality-case-allocation-v1", "case_id": "normal-01",
            "controller_id": controller.controller_id, "bindings": bindings,
            "maka_output_tokens": 8192, "rows": [row]})
        authority = diagnostic.seal_quality_allocation(controller, store, ref, bindings=bindings)
        with pytest.raises(ProviderRequestError, match="consumed"):
            diagnostic.issue_quality_native_command(controller, store, authority,
                request=request, ledger=ledger, permit=permit, epoch=1, bindings=bindings,
                role="baseline-seat", phase="initial", condition="baseline")
        permit_id = diagnostic.register_quality_permit(controller, store, authority, ledger,
            role="baseline-seat", phase="initial", condition="baseline", permit=permit,
            epoch=1, bindings=bindings)
        proof = diagnostic.issue_quality_native_command(controller, store, authority,
            request=request, ledger=ledger, permit=permit, epoch=1, bindings=bindings,
            role="baseline-seat", phase="initial", condition="baseline")
        command, row_proof = diagnostic.reopen_quality_native_command(controller, request, proof)
        assert command.stdin == request.prompt_bytes
        assert "--tools" in command.argv and "--no-session-persistence" not in command.argv
        assert command.max_output_bytes == 8 * 1024 * 1024
        assert row_proof["permit_id"] == permit_id
        assert "shared/lib/fanout/diagnostic.py" in read_evidence(controller,
            "quality-native-command", *proof)["loaded"]
        with pytest.raises(ProviderRequestError, match="quality"):
            diagnostic.reopen_quality_native_command(controller,
                replace(request, prompt="substituted"), proof)
        with pytest.raises(ProviderRequestError, match="quality"):
            diagnostic.reopen_quality_native_command(controller,
                replace(request, session_id="00000000-0000-0000-0000-000000000000"), proof)


def test_quality_sender_retains_genuine_prepared_body_and_refuses_tools_before_send(tmp_path):
    from test_fanout_container_upstream import _route, _routed_permit
    from shared.lib.fanout.artifacts import ArtifactStore, ArtifactRef
    from shared.lib.fanout.container_upstream import HostUpstream
    route = _route("claude")
    path = "/v1/projects/fictional-project/locations/europe-west1/publishers/anthropic/models/claude-opus-5-5:streamRawPredict"
    permit = _routed_permit(route, path)
    sender = HostUpstream(permit, route, max_response_bytes=permit.max_response_bytes,
                          deadline_seconds=permit.response_deadline)
    with ArtifactStore(tmp_path / "raw") as store:
        guarded = diagnostic.quality_upstream_sender(sender, store, "baseline-seat")
        body = json.dumps({"model": permit.model, "max_tokens": 16, "stream": True,
            "messages": [{"role": "user", "content": "inert source composition"}], "tools": []}).encode()
        headers = {"content-type": "application/json"}
        prepared = guarded.preview("POST", "/v1/messages", headers, body)
        captured = store.read_json(guarded.captures[hashlib.sha256(body).hexdigest()])
        assert store.read_bytes(ArtifactRef(**captured["effective_body"])) == prepared.body
        assert captured["route"] == route.digest
        assert json.loads(prepared.body)["tools"] == []
        assert guarded.preview("POST", "/v1/messages", headers, body) == prepared
        assert len(guarded.captures) == 1
        forbidden = json.dumps({**json.loads(body), "tools": [{"name": "write_file"}]}).encode()
        with pytest.raises(fanout.broker.BrokerRejected, match="tool"):
            guarded.preview("POST", "/v1/messages", headers, forbidden)


@pytest.mark.parametrize("provider", ("codex", "agy"))
def test_quality_future_resume_slot_is_frozen_without_inventing_native_session(tmp_path, provider):
    from shared.lib.fanout.artifacts import ArtifactStore
    from shared.lib.fanout.container_broker import PermitLedger
    controller = fanout.create_lifecycle_controller(tmp_path / "owner")
    ledger = PermitLedger(controller.root / "locks/spend", controller)
    profile = fanout.ProviderRegistry.default().select(provider, "read-only", "standard")
    permit = BrokerPermit(provider, "run", "task", "target", "seat", "initial",
        profile.requested_model, "disposable-placeholder", 1, 1024, 2048, 8192,
        inputs_digest="a" * 64)
    bindings = {name: "b" * 64 for name in
        ("campaign", "casebook", "source", "projection", "profile", "inputs")}
    bindings["inputs"] = "a" * 64
    original = {"role": "candidate-seat", "phase": "initial", "condition": "candidate",
        "epoch": 1, "permit": asdict(permit), "blocker": None, "initial": None,
        "profile": profile.digest, "catalog": "c" * 64 if provider == "codex" else None,
        "process_seconds": 30, "outer_seconds": 60}
    resume = {**original, "phase": "resume", "epoch": 2,
              "permit": {**asdict(permit), "route": "resume"},
              "initial": diagnostic.quality_row_identity(original)}
    with ArtifactStore(tmp_path / "raw") as store:
        ref = store.write_json("allocation.json", {"schema": "fanout-quality-case-allocation-v1",
            "case_id": "normal-01", "controller_id": controller.controller_id,
            "bindings": bindings, "maka_output_tokens": 8192, "rows": [original, resume]})
        authority = diagnostic.seal_quality_allocation(controller, store, ref, bindings=bindings)
        diagnostic.register_quality_permit(controller, store, authority, ledger,
            role="candidate-seat", phase="initial", condition="candidate", permit=permit,
            epoch=1, bindings=bindings)
        unresolved = replace(permit, route="resume", session_id="foreign-invented-session")
        with pytest.raises(ProviderRequestError, match="completed original"):
            diagnostic.register_quality_permit(controller, store, authority, ledger,
                role="candidate-seat", phase="resume", condition="candidate", permit=unresolved,
                epoch=2, bindings=bindings)
        assert store.read_json(ref)["rows"] == [original, resume]
        with ledger._locked() as state:
            assert len(state["permits"]) == 1


@pytest.fixture
def seat(tmp_path):
    if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        pytest.skip("provider diagnostic requires macOS Seatbelt")
    root = tmp_path.resolve()
    repository = root / "repository"
    repository.mkdir()
    subprocess.run(("git", "init", "-q", str(repository)), check=True)
    (repository / "fixture.txt").write_text("disposable fixture\n")
    subprocess.run(("git", "-C", str(repository), "add", "-A"), check=True)
    subprocess.run(("git", "-C", str(repository), "-c", "user.name=Test",
                    "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"),
                   check=True)
    baseline = fanout.capture_repository_baseline(repository)
    controller = fanout.create_lifecycle_controller(root / "controller")
    workspace = fanout.create_seat_workspace(baseline, controller.root / "workspaces",
                                             "diagnostic-seat")
    verification = fanout.verify_seat_workspace(baseline, workspace, controller=controller)
    other = root / "other-target"
    other.mkdir(mode=0o700)
    (other / "private.txt").write_text("not for the seat\n")
    return controller, verification, repository, other


def _listener(request, response):
    wire_model = ("gemini-3.8-flash" if request.executor_id == "agy"
                  else request.profile.requested_model)
    permit = BrokerPermit(
        provider=request.executor_id, run_id=request.run_id, task_id=request.task_id,
        target_id=request.target_id, seat_id=request.seat_id,
        route="resume" if request.resume else "initial",
        model=wire_model, inputs_digest=request.inputs_digest,
        session_id=request.session_id, placeholder_key="disposable-placeholder",
        max_requests=1, max_request_bytes=1024 * 1024,
        max_response_bytes=1024 * 1024, max_output_tokens=8192,
        clamp_output_tokens=request.executor_id in {"claude", "agy"},
    )
    broker = Broker(permit, credential="fake-upstream-only", send=response)
    return BrokerListener(broker)


def _sse(events):
    return "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
                   for event in events).encode()


def _claude_response(model, text):
    events = (
        {"type": "message_start", "message": {
            "id": "msg_fake_provider", "type": "message", "role": "assistant",
            "content": [], "model": model, "stop_reason": None,
            "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 0},
        }},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
         "usage": {"output_tokens": 1}},
        {"type": "message_stop"},
    )
    return BrokerResponse(200, {"content-type": "text/event-stream"}, _sse(events))


def _codex_response(text):
    events = (
        {"type": "response.created", "response": {"id": "resp_fake_provider"}},
        {"type": "response.output_item.done", "item": {
            "type": "message", "role": "assistant", "id": "msg_fake_provider",
            "content": [{"type": "output_text", "text": text}],
        }},
        {"type": "response.completed", "response": {
            "id": "resp_fake_provider", "usage": {
                "input_tokens": 0, "input_tokens_details": None,
                "output_tokens": 0, "output_tokens_details": None,
                "total_tokens": 0,
            },
        }},
    )
    return BrokerResponse(200, {"content-type": "text/event-stream"}, _sse(events))


def _agy_response(text):
    data = {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]},
                            "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1}}
    return BrokerResponse(200, {"content-type": "text/event-stream"},
                          ("data: " + json.dumps(data) + "\n\n").encode())


@pytest.mark.parametrize("executor,version,upgraded", [
    ("claude", "2.2.0", "2.3.0"), ("codex", "0.160.0", "0.161.0"),
])
def test_installed_profile_reaches_real_broker_gate_and_recorded_drift_refuses(
    seat, monkeypatch, executor, version, upgraded,
):
    controller, verification, repository, other = seat
    current = {"version": version}
    monkeypatch.setattr(fanout.providers, "_installed_cli_version", lambda _executor: current["version"])
    registry = fanout.ProviderRegistry.for_installed((executor,))
    profile = registry.admit(executor, "read-only", "standard")
    request = fanout.ProviderRequest(
        executor, "canonical gate fixture", cwd=verification.workspace.root, profile=profile,
        session_id="fd6f840b-4b79-4fd2-91d0-14d3d8fba655" if executor == "claude" else None,
        run_id="run-1", task_id="task-1", target_id="target-1", inputs_digest="a" * 64,
        seat_id=verification.workspace.seat_id,
    )

    with _listener(request, lambda *_args: pytest.fail("canonical preflight sent a provider request")) as listener:
        command = (profile.adapter.build_command(request, brokered_diagnostic=True)
                   if executor == "claude" else diagnostic._codex_broker_command(
                       request, profile.adapter.build_command(request), listener,
                   ))
        admission = issue_broker_admission(controller, verification, request, command, listener)
        assert read_evidence(controller, "native-broker-diagnostic-admission", *admission)[
            "profile_digest"
        ] == profile.digest
        boundary = fanout.issue_native_boundary(
            controller, verification, run_id=request.run_id, task_id=request.task_id,
            target_id=request.target_id, inputs_digest=request.inputs_digest, request=request,
            command=command, other_target_roots=(other,), denied_owner_roots=(repository,),
            broker_listener=listener, broker_admission=admission,
        )
        assert boundary.profile_digest == profile.digest
        assert boundary.executable_digest == hashlib.sha256(boundary.executable_path.read_bytes()).hexdigest()
        fanout.validate_native_boundary(boundary, request, command, controller, verification)
        fanout.validate_native_boundary(boundary, request, None, controller, verification)
        recorded = {path: path.read_bytes() for path in (controller.root / "receipts").iterdir()}
        current["version"] = upgraded
        with pytest.raises(ProviderRequestError, match="CLI version drift"):
            issue_broker_admission(controller, verification, request, command, listener)
        with pytest.raises(ProviderRequestError, match="native seat boundary validation") as error:
            fanout.validate_native_boundary(boundary, request, command, controller, verification)
        assert "CLI version drift" in str(error.value.__cause__)
        assert {path: path.read_bytes() for path in (controller.root / "receipts").iterdir()} == recorded
        assert listener.effective_output_tokens == ()


@pytest.mark.parametrize("tamper", ("argv", "capabilities", "command", "model"))
def test_installed_profile_canonical_broker_gate_refuses_tampering(seat, monkeypatch, tamper):
    controller, verification, _, _ = seat
    monkeypatch.setattr(fanout.providers, "_installed_cli_version", lambda _executor: "2.2.0")
    profile = fanout.ProviderRegistry.for_installed(("claude",)).admit("claude", "read-only", "standard")
    request = fanout.ProviderRequest(
        "claude", "canonical gate fixture", cwd=verification.workspace.root, profile=profile,
        session_id="fd6f840b-4b79-4fd2-91d0-14d3d8fba655", run_id="run-1",
        task_id="task-1", target_id="target-1", inputs_digest="a" * 64,
        seat_id=verification.workspace.seat_id,
    )
    command = profile.adapter.build_command(request, brokered_diagnostic=True)
    changes = {
        "argv": {"initial_argv": (*profile.initial_argv, "--dangerously-skip-permissions")},
        "capabilities": {"capabilities": replace(profile.capabilities, explicit_resume=False)},
        "model": {"requested_model": "unapproved-model"},
    }
    if tamper == "command":
        command = replace(command, argv=(*command.argv, "--dangerously-skip-permissions"))
    else:
        request = replace(request, profile=replace(profile, **changes[tamper], profile_sha256=None))
    with _listener(request, lambda *_args: pytest.fail("tampered request sent a provider request")) as listener:
        with pytest.raises(ProviderRequestError, match=f"broker diagnostic {tamper if tamper == 'command' else 'profile'}"):
            issue_broker_admission(controller, verification, request, command, listener)
        assert listener.effective_output_tokens == ()


def test_claude_fake_initial_and_exact_resume_use_native_broker(seat):
    controller, verification, repository, other = seat
    profile = fanout.ProviderRegistry.default().select("claude", "read-only", "standard")
    request = fanout.ProviderRequest(
        "claude", "first fake turn", cwd=verification.workspace.root,
        profile=profile, session_id="fd6f840b-4b79-4fd2-91d0-14d3d8fba655",
        run_id="run-1", task_id="task-1", target_id="target-1",
        seat_id=verification.workspace.seat_id, inputs_digest="a" * 64,
        slot_root=controller.root / "slots", timeout=30,
    )
    seen = []

    def upstream(method, path, headers, body):
        document = json.loads(body)
        seen.append((method, path, document.get("model"), document.get("max_tokens")))
        return _claude_response(profile.requested_model,
                                "fake-first" if len(seen) == 1 else "fake-second")

    with _listener(request, upstream) as listener:
        initial = diagnostic.run_brokered_provider_diagnostic(
            request, controller=controller, verification=verification,
            broker_listener=listener, other_target_roots=(other,),
            denied_owner_roots=(repository,),
        )
    assert initial.result.valid, (seen, initial.result.stdout[-2000:],
                                  initial.result.stderr[-2000:])
    assert initial.result.session_id == request.session_id
    assert initial.result.answer == "fake-first"
    assert initial.result.observed_model == profile.requested_model
    assert initial.effective_output_tokens == (8192,)
    receipt = read_evidence(controller, "brokered-provider-diagnostic",
                            initial.receipt_name, initial.receipt_digest)
    assert receipt["broker_model"] == profile.requested_model
    assert receipt["refused_agy_auxiliary_requests"] == 0
    assert seen[0][0:3] == ("POST", "/v1/messages", profile.requested_model)
    assert isinstance(seen[0][3], int) and seen[0][3] > 0

    for changes in (
        {"session_id": "3c1789a6-e1a6-44e4-9bb2-c154a2ca74ec"},
        {"target_id": "target-2"},
        {"inputs_digest": "b" * 64},
    ):
        mismatched = replace(request, resume=True, prompt="untrusted resume", **changes)
        with _listener(mismatched, upstream) as listener:
            with pytest.raises(ProviderRequestError,
                               match="exact diagnostic resume differs from initial evidence"):
                diagnostic.run_brokered_provider_diagnostic(
                    mismatched, controller=controller, verification=verification,
                    broker_listener=listener, other_target_roots=(other,),
                    denied_owner_roots=(repository,), initial=initial,
                )

    resumed = replace(request, resume=True, prompt="second fake turn")
    with _listener(resumed, upstream) as listener:
        second = diagnostic.run_brokered_provider_diagnostic(
            resumed, controller=controller, verification=verification,
            broker_listener=listener, other_target_roots=(other,),
            denied_owner_roots=(repository,), initial=initial,
        )
    assert second.result.valid, second.result.stderr[-2000:]
    assert second.result.session_id == initial.result.session_id
    assert second.result.answer == "fake-second"
    assert second.result.observed_model == profile.requested_model
    assert second.boundary.state_root == initial.boundary.state_root
    assert second.boundary.route != initial.boundary.route
    assert len(seen) == 2


def test_claude_resume_refuses_changed_workspace_verification(seat):
    controller, verification, repository, other = seat
    profile = fanout.ProviderRegistry.default().select("claude", "read-only", "standard")
    request = fanout.ProviderRequest(
        "claude", "first fake turn", cwd=verification.workspace.root,
        profile=profile, session_id="fd6f840b-4b79-4fd2-91d0-14d3d8fba655",
        run_id="run-1", task_id="task-1", target_id="target-1",
        seat_id=verification.workspace.seat_id, inputs_digest="a" * 64,
        slot_root=controller.root / "slots", timeout=30,
    )
    seen = []

    def upstream(*_args):
        seen.append(1)
        return _claude_response(profile.requested_model, "fake-answer")

    with _listener(request, upstream) as listener:
        initial = diagnostic.run_brokered_provider_diagnostic(
            request, controller=controller, verification=verification,
            broker_listener=listener, other_target_roots=(other,),
            denied_owner_roots=(repository,),
        )
    assert initial.result.valid
    workspace = verification.workspace.root
    (workspace / "fixture.txt").write_text("changed disposable baseline\n")
    subprocess.run(("git", "-C", str(workspace), "add", "-A"), check=True)
    subprocess.run(("git", "-C", str(workspace), "-c", "user.name=Test",
                    "-c", "user.email=test@example.invalid", "commit", "-qm", "changed"),
                   check=True)
    changed = fanout.capture_repository_baseline(workspace)
    renewed = fanout.verify_seat_workspace(
        changed, replace(verification.workspace, baseline_digest=changed.digest),
        controller=controller,
    )
    assert renewed.workspace.root == verification.workspace.root
    assert renewed.root_inode == verification.root_inode
    assert renewed.evidence_digest != verification.evidence_digest
    resumed = replace(request, resume=True, prompt="second fake turn")
    with _listener(resumed, upstream) as listener:
        with pytest.raises(ProviderRequestError, match="workspace"):
            diagnostic.run_brokered_provider_diagnostic(
                resumed, controller=controller, verification=renewed,
                broker_listener=listener, other_target_roots=(other,),
                denied_owner_roots=(repository,), initial=initial,
            )
    assert len(seen) == 1


def test_claude_brokered_resume_keeps_admitted_skill_read_grant(seat):
    controller, verification, repository, other = seat
    profile = fanout.ProviderRegistry.default().select("claude", "read-only", "standard")
    source = controller.root.parent / "skill-source"
    source.mkdir(mode=0o700)
    resolver = fanout.SkillResolver((fanout.SkillRoot("root", source, 0),))
    admission = resolver.admit(
        (), task_id="task-1", seat_id=verification.workspace.seat_id,
        provider="claude", session_id="fd6f840b-4b79-4fd2-91d0-14d3d8fba655",
    )
    admission = resolver.stage(admission, controller.root.parent / "skill-stage")
    admission = resolver.verify_engine_delivery(admission, ())
    request = fanout.ProviderRequest(
        "claude", "first fake turn", cwd=verification.workspace.root,
        profile=profile, session_id=admission.session_id,
        run_id="run-1", task_id="task-1", target_id="target-1",
        seat_id=verification.workspace.seat_id, inputs_digest="a" * 64,
        slot_root=controller.root / "slots", timeout=30,
        staged_skill_root=admission.staged_root, staged_skill_admission=admission,
        skill_bundle_sha256=hashlib.sha256(fanout.canonical_json({
            "schema_version": "fanout-seat-skill-bundle-v1",
            "skills": admission.manifest_dict()["skills"],
        })).hexdigest(),
        skill_delivery_sha256=hashlib.sha256(fanout.canonical_json([])).hexdigest(),
    )

    def upstream(_method, _path, _headers, _body):
        return _claude_response(profile.requested_model, "fake-answer")

    with _listener(request, upstream) as listener:
        initial = diagnostic.run_brokered_provider_diagnostic(
            request, controller=controller, verification=verification,
            broker_listener=listener, other_target_roots=(other,),
            denied_owner_roots=(repository,),
        )
    assert initial.result.valid
    assert initial.boundary.staged_skill_grant_digest is not None

    resumed = replace(request, resume=True, prompt="second fake turn")
    with _listener(resumed, upstream) as listener:
        second = diagnostic.run_brokered_provider_diagnostic(
            resumed, controller=controller, verification=verification,
            broker_listener=listener, other_target_roots=(other,),
            denied_owner_roots=(repository,), initial=initial,
        )
    assert second.result.valid
    assert second.boundary.staged_skill_grant_digest == initial.boundary.staged_skill_grant_digest


def test_diagnostic_requires_live_exact_permit_without_lifting_public_gate(seat):
    controller, verification, repository, other = seat
    profile = fanout.ProviderRegistry.default().select("claude", "read-only", "standard")
    request = fanout.ProviderRequest(
        "claude", "fake turn", cwd=verification.workspace.root, profile=profile,
        session_id="fd6f840b-4b79-4fd2-91d0-14d3d8fba655",
        run_id="run-1", task_id="task-1", target_id="target-1",
        seat_id=verification.workspace.seat_id, inputs_digest="a" * 64,
    )
    arguments = dict(controller=controller, verification=verification,
                     other_target_roots=(other,), denied_owner_roots=(repository,))
    with pytest.raises(ProviderRequestError, match="native seat boundary"):
        fanout.run_provider(request)
    with pytest.raises(ProviderRequestError, match="broker diagnostic admission failed"):
        diagnostic.run_brokered_provider_diagnostic(
            request, broker_listener=_listener(request, lambda *_: None), **arguments,
        )
    different = replace(request, target_id="target-2")
    with _listener(different, lambda *_: None) as listener:
        with pytest.raises(ProviderRequestError, match="broker diagnostic admission failed"):
            diagnostic.run_brokered_provider_diagnostic(
                request, broker_listener=listener, **arguments,
            )


@pytest.mark.parametrize("executor_id", ("claude", "codex", "agy"))
def test_diagnostic_refuses_repo_write_before_boundary_or_spawn(seat, monkeypatch,
                                                               executor_id):
    controller, verification, repository, other = seat
    profile = fanout.ProviderRegistry.default().select(executor_id, "repo-write", "standard")
    request = fanout.ProviderRequest(
        executor_id, "disposable prompt", cwd=verification.workspace.root,
        execution_class="repo-write", profile=profile,
        session_id="fd6f840b-4b79-4fd2-91d0-14d3d8fba655" if executor_id == "claude" else None,
        run_id="run-1", task_id="task-1", target_id="target-1",
        seat_id=verification.workspace.seat_id, inputs_digest="a" * 64,
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("repo-write diagnostic reached boundary issuance or process spawn")

    monkeypatch.setattr(diagnostic, "issue_native_boundary", forbidden)
    monkeypatch.setattr(diagnostic, "run_command", forbidden)
    with _listener(request, lambda *_: None) as listener:
        with pytest.raises(ProviderRequestError, match="read-only diagnostics"):
            diagnostic.run_brokered_provider_diagnostic(
                request, controller=controller, verification=verification,
                broker_listener=listener, other_target_roots=(other,),
                denied_owner_roots=(repository,),
            )
        assert listener.effective_output_tokens == ()


@pytest.mark.parametrize("resume", (False, True))
def test_codex_diagnostic_config_is_derived_from_exact_live_broker(seat, resume):
    controller, verification, _, _ = seat
    profile = fanout.ProviderRegistry.default().select("codex", "read-only", "standard")
    request = fanout.ProviderRequest(
        "codex", "fake turn", cwd=verification.workspace.root, profile=profile,
        resume=resume, session_id="thread-fake-123" if resume else None,
        run_id="run-1", task_id="task-1", target_id="target-1",
        seat_id=verification.workspace.seat_id, inputs_digest="a" * 64,
        slot_root=controller.root / "slots", timeout=30,
    )
    command = profile.adapter.build_command(request)
    with _listener(request, lambda *_: None) as listener:
        configured = diagnostic._codex_broker_command(request, command, listener)
        config = tuple(configured.argv[index + 1] for index, arg in
                       enumerate(configured.argv[:-1]) if arg == "-c")
        assert f'model_providers.fanout_broker.base_url="http://127.0.0.1:{listener.port}/v1"' in config
        assert "model_provider=fanout_broker" in config
        assert "model_providers.fanout_broker.wire_api=responses" in config
        assert "model_providers.fanout_broker.requires_openai_auth=false" in config
        assert "model_providers.fanout_broker.request_max_retries=0" in config
        assert "model_providers.fanout_broker.stream_max_retries=0" in config
        assert 'model_providers.fanout_broker.http_headers={"X-Fanout-Seat"="disposable-placeholder"}' in config
        assert configured.environment == {}
        prefix = 3 if resume else 2
        assert configured.argv[:prefix] == command.argv[:prefix]
        assert configured.argv[-len(command.argv) + prefix:] == command.argv[prefix:]
    with pytest.raises(ProviderRequestError, match="Codex broker binding is unavailable"):
        diagnostic._codex_broker_command(request, command, listener)


def test_codex_native_fake_initial_stays_blocked_before_broker(seat):
    controller, verification, repository, other = seat
    profile = fanout.ProviderRegistry.for_installed(("codex",)).admit(
        "codex", "read-only", "standard",
    )
    assert profile.characterized is False
    request = fanout.ProviderRequest(
        "codex", "first fake turn", cwd=verification.workspace.root,
        profile=profile, run_id="run-1", task_id="task-1",
        target_id="target-1", seat_id=verification.workspace.seat_id,
        inputs_digest="a" * 64, slot_root=controller.root / "slots", timeout=30,
    )
    seen = []

    def upstream(method, path, headers, body):
        document = json.loads(body)
        seen.append((method, path, document.get("model"),
                     document.get("max_output_tokens")))
        return _codex_response("fake-first" if len(seen) == 1 else "fake-second")

    with _listener(request, upstream) as listener:
        initial = diagnostic.run_brokered_provider_diagnostic(
            request, controller=controller, verification=verification,
            broker_listener=listener, other_target_roots=(other,),
            denied_owner_roots=(repository,),
        )
    # Its managed-preference sync needs IPC that bypasses this seat's read boundary.
    assert not initial.result.valid
    assert initial.result.session_id is None
    assert initial.effective_output_tokens == ()
    assert seen == []
    assert b"Failed to synchronize managed preferences (code -32600)" in initial.process.stderr


def test_agy_native_fake_initial_stays_blocked_before_broker(seat):
    controller, verification, repository, other = seat
    profile = fanout.ProviderRegistry.default().select("agy", "read-only", "standard")
    request = fanout.ProviderRequest(
        "agy", "first fake turn", cwd=verification.workspace.root, profile=profile,
        run_id="run-1", task_id="task-1", target_id="target-1",
        seat_id=verification.workspace.seat_id, inputs_digest="a" * 64,
        slot_root=controller.root / "slots", timeout=25,
    )
    seen = []

    def upstream(method, path, headers, body):
        document = json.loads(body)
        seen.append((method, path, document["generationConfig"]["maxOutputTokens"]))
        return _agy_response("fake-agy-answer")

    with _listener(request, upstream) as listener:
        initial = diagnostic.run_brokered_provider_diagnostic(
            request, controller=controller, verification=verification,
            broker_listener=listener, other_target_roots=(other,),
            denied_owner_roots=(repository,),
        )
    # The direct CLI binds a private loopback listener before its Gemini POST.
    assert not initial.result.valid
    assert initial.result.session_id is None
    assert initial.effective_output_tokens == ()
    assert initial.broker_model == "gemini-3.8-flash"
    assert initial.refused_agy_auxiliary_requests == 0
    assert seen == []
    assert b"listen tcp 127.0.0.1:0: bind: operation not permitted" in initial.process.stderr


@pytest.mark.parametrize("entrypoint", ("project", "reopen"))
def test_quality_catalog_uses_explicit_responses_tools_without_changing_effort(tmp_path, monkeypatch, entrypoint):
    import copy
    from types import SimpleNamespace
    from shared.lib.fanout.artifacts import ArtifactRef, ArtifactStore, canonical_json

    model = {"slug": "gpt-6.1-sol", "use_responses_lite": True,
             "shell_type": "unified_exec", "apply_patch_tool_type": "freeform",
             "experimental_supported_tools": ["time"], "supports_search_tool": True,
             "tool_mode": "code_mode", "multi_agent_reasoning_effort": "xhigh",
             "supported_reasoning_levels": [{"effort": "xhigh"}, {"effort": "ultra"}],
             "default_reasoning_level": "low", "supports_reasoning_effort_updates": True}
    original = {"models": [model, *({"slug": f"other-{index}"} for index in range(10))]}
    raw = canonical_json(original)
    descriptor = diagnostic.json_catalog_bytes(model)
    original_hash = "fd219bd9f061278275f528939f82f54d2eb97df4b25c23b022adbe48813d920b"
    descriptor_hash = "56373206bcf4c478be9aa324af04e2324219a836005e1044260910307f2cceeb"
    # Synthetic authentication seam; the test exercises projection/reopening, not vendor provenance.
    real_sha256 = hashlib.sha256

    def source_hash(value):
        digest = original_hash if value == raw else descriptor_hash if value == descriptor else None
        return SimpleNamespace(hexdigest=lambda: digest) if digest else real_sha256(value)

    monkeypatch.setattr(diagnostic, "hashlib", SimpleNamespace(sha256=source_hash))
    expected = copy.deepcopy(original)
    expected["models"][0].update(shell_type="disabled", apply_patch_tool_type=None,
        experimental_supported_tools=[], supports_search_tool=False, tool_mode="direct",
        use_responses_lite=False)
    store = ArtifactStore(tmp_path / "catalog-artifacts")
    stored = store.write_bytes("original.json", raw)
    refs = {"original": ArtifactRef(stored.path, original_hash, stored.size),
            "projected": store.write_json("projected.json", expected)}
    if entrypoint == "project":
        # Projection's ordinary source read uses the real artifact digest.
        projected = diagnostic.project_quality_catalog(store, stored, "second-projection.json")
        actual = store.read_json(projected)
    else:
        actual = json.loads(diagnostic._reopen_quality_catalog(store,
            {key: asdict(ref) for key, ref in refs.items()}))
    assert actual == expected
    assert actual["models"][0]["use_responses_lite"] is False
    for key in ("multi_agent_reasoning_effort", "supported_reasoning_levels",
                "default_reasoning_level", "supports_reasoning_effort_updates"):
        assert actual["models"][0][key] == model[key]
