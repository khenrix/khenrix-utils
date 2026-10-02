"""Controller-issued native file boundary for disposable provider seats."""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from dataclasses import replace
from multiprocessing.shared_memory import SharedMemory
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
FANOUT_ROOT = ROOT / "shared/lib/fanout"
SPEC = importlib.util.spec_from_file_location(
    "_fanout_native_boundary_contracts", FANOUT_ROOT / "__init__.py",
    submodule_search_locations=[str(FANOUT_ROOT)],
)
assert SPEC and SPEC.loader
fanout = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fanout
SPEC.loader.exec_module(fanout)
broker_module = importlib.import_module(f"{SPEC.name}.broker")


@pytest.fixture
def seat(request):
    disposable = tempfile.TemporaryDirectory(prefix="fanout-native-seat-", dir="/private/tmp")
    request.addfinalizer(disposable.cleanup)
    tmp_path = Path(disposable.name)
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(("git", "init", "-q", str(repository)), check=True)
    (repository / "fixture.txt").write_text("disposable fixture\n")
    subprocess.run(("git", "-C", str(repository), "add", "-A"), check=True)
    subprocess.run((
        "git", "-C", str(repository), "-c", "user.name=Test",
        "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture",
    ), check=True)
    baseline = fanout.capture_repository_baseline(repository)
    controller = fanout.create_lifecycle_controller(tmp_path / "controller")
    workspace = fanout.create_seat_workspace(
        baseline, controller.root / "workspaces", "codex-seat",
    )
    verification = fanout.verify_seat_workspace(
        baseline, workspace, controller=controller,
    )
    sibling = controller.root / "workspaces" / "seats" / "sibling"
    sibling.mkdir(mode=0o700)
    (sibling / "secret.txt").write_text("sibling seat\n")
    other = tmp_path / "other-target"
    other.mkdir(mode=0o700)
    (other / "secret.txt").write_text("other target\n")
    owner = tmp_path / "owner"
    owner.mkdir(mode=0o700)
    (owner / "run.json").write_text("owner state\n")
    adapter = fanout.CodexAdapter()
    profile = fanout.ExecutorProfile(
        "codex", adapter, adapter.capabilities, "repo-write", "standard",
        "diagnostic-model", "low", "diagnostic-cli", ("/bin/sh",),
        ("/bin/sh", "--resume", "{session_id}"), 10, 10,
    )
    request = fanout.ProviderRequest(
        "codex", "diagnostic only", cwd=workspace.root,
        execution_class="repo-write", profile=profile, run_id="run-1",
        task_id="task-1", target_id="target-1", inputs_digest="a" * 64,
        seat_id="codex-seat",
    )
    command = fanout.ProcessCommand(
        argv=("/bin/sh", "-c", "printf diagnostic"),
        stdin=b"", cwd=workspace.root,
    )
    return controller, verification, request, command, other, owner


def _boundary(seat, command=None):
    controller, verification, request, original, other, owner = seat
    return fanout.issue_native_boundary(
        controller, verification, run_id="run-1", task_id="task-1",
        target_id="target-1", inputs_digest="a" * 64, request=request,
        command=original if command is None else command,
        other_target_roots=(other, controller.root / "workspaces/seats/sibling"),
        denied_owner_roots=(owner,),
    )


def test_descriptor_authenticates_exact_request_command_and_controller(seat):
    controller, verification, request, command, _, _ = seat
    boundary = _boundary(seat)
    fanout.validate_native_boundary(boundary, request, command, controller, verification)
    wrapped = fanout.wrap_native_command(
        command, boundary, request=request, controller=controller,
        verification=verification,
    )
    assert wrapped.argv[:3] == ("/usr/bin/sandbox-exec", "-f", str(boundary.profile_path))
    assert wrapped.argv[3] == str(Path("/bin/sh").resolve())


def test_codex_native_environment_uses_private_session_home(seat):
    controller, verification, request, command, _, _ = seat
    boundary = _boundary(seat)
    private = boundary.state_root / "home" / ".codex"
    assert dict(boundary.frozen_environment)["CODEX_HOME"] == str(private)
    assert private.is_dir() and private.stat().st_mode & 0o777 == 0o700
    fanout.validate_native_boundary(boundary, request, command, controller, verification)


def _broker_listener(request, *, target_id="target-1", wire_model=None,
                     inputs_digest=None, session_id="request"):
    model = ("gemini-3.8-flash" if request.executor_id == "agy" and wire_model is None
             else request.profile.requested_model if wire_model is None else wire_model)
    permit = broker_module.BrokerPermit(
        provider=request.executor_id, run_id=request.run_id,
        task_id=request.task_id, target_id=target_id, seat_id=request.seat_id,
        route="resume" if request.resume else "initial",
        model=model,
        inputs_digest=(request.inputs_digest if inputs_digest is None else inputs_digest),
        session_id=request.session_id if session_id == "request" else session_id,
        placeholder_key="disposable-placeholder", max_requests=2,
        max_request_bytes=4096, max_response_bytes=4096,
        max_output_tokens=128,
    )
    broker = broker_module.Broker(
        permit, credential="controller-only-credential",
        send=lambda *_args: broker_module.BrokerResponse(
            200, {"content-type": "application/json"}, b"{}"),
    )
    return broker_module.BrokerListener(broker)


def _read_only_claude(seat):
    _, _, original, _, _, _ = seat
    profile = fanout.ProviderRegistry.default().select("claude", "read-only", "standard")
    request = replace(
        original, executor_id="claude", execution_class="read-only", profile=profile,
        session_id="fd6f840b-4b79-4fd2-91d0-14d3d8fba655",
    )
    return request, profile.adapter.build_command(request, brokered_diagnostic=True)


def test_direct_brokered_boundary_without_admission_is_refused(seat):
    controller, verification, _, _, other, owner = seat
    request, command = _read_only_claude(seat)
    with _broker_listener(request) as listener:
        with pytest.raises(fanout.ProviderRequestError, match="broker.*admission"):
            fanout.issue_native_boundary(
                controller, verification, run_id="run-1", task_id="task-1",
                target_id="target-1", inputs_digest="a" * 64, request=request,
                command=command, other_target_roots=(other,), denied_owner_roots=(owner,),
                broker_listener=listener,
            )


def test_read_only_boundary_without_broker_refuses_shell_command(seat):
    controller, verification, _, _, other, owner = seat
    request, _ = _read_only_claude(seat)
    shell_profile = replace(
        request.profile, initial_argv=("/bin/sh",), resume_argv=("/bin/sh",),
        profile_sha256=None,
    )
    request = replace(request, profile=shell_profile)
    command = fanout.ProcessCommand(
        argv=("/bin/sh", "-c", "printf bypass > unbrokered-write.txt"),
        stdin=request.prompt_bytes, cwd=request.cwd,
    )
    with pytest.raises(fanout.ProviderRequestError, match="read-only.*broker"):
        fanout.issue_native_boundary(
            controller, verification, run_id="run-1", task_id="task-1",
            target_id="target-1", inputs_digest="a" * 64, request=request,
            command=command, other_target_roots=(other,), denied_owner_roots=(owner,),
        )


def test_broker_admission_refuses_custom_shell_profile(seat):
    controller, verification, _, _, _, _ = seat
    request, _ = _read_only_claude(seat)
    shell_profile = replace(
        request.profile, initial_argv=("/bin/bash",), resume_argv=("/bin/bash",),
        profile_sha256=None,
    )
    shell_request = replace(request, profile=shell_profile)
    shell_command = fanout.ProcessCommand(
        argv=("/bin/bash", "-c", "printf bypass"), stdin=shell_request.prompt_bytes,
        cwd=shell_request.cwd,
    )
    module = sys.modules[f"{SPEC.name}.native_boundary"]
    with _broker_listener(shell_request) as listener:
        with pytest.raises(fanout.ProviderRequestError, match="broker.*profile"):
            module.issue_broker_admission(
                controller, verification, shell_request, shell_command, listener,
            )


def test_broker_admission_refuses_write_capable_argv_under_read_only_profile(seat):
    controller, verification, _, _, _, _ = seat
    request, command = _read_only_claude(seat)
    write_command = replace(command, argv=(*command.argv, "--dangerously-skip-permissions"))
    module = sys.modules[f"{SPEC.name}.native_boundary"]
    with _broker_listener(request) as listener:
        with pytest.raises(fanout.ProviderRequestError, match="broker.*command"):
            module.issue_broker_admission(
                controller, verification, request, write_command, listener,
            )


def test_broker_admission_refuses_resume_without_authenticated_initial(seat):
    controller, verification, _, _, _, _ = seat
    initial, _ = _read_only_claude(seat)
    request = replace(initial, resume=True)
    command = request.profile.adapter.build_command(request, brokered_diagnostic=True)
    module = sys.modules[f"{SPEC.name}.native_boundary"]
    missing = ("0" * 64 + ".json", "0" * 64)
    with _broker_listener(request) as listener:
        with pytest.raises(fanout.ProviderRequestError, match="initial"):
            module.issue_broker_admission(
                controller, verification, request, command, listener,
                initial_receipt=missing,
            )


def test_broker_admission_refuses_incomplete_signed_initial_receipt(seat):
    controller, verification, _, _, _, _ = seat
    initial, _ = _read_only_claude(seat)
    request = replace(initial, resume=True)
    command = request.profile.adapter.build_command(request, brokered_diagnostic=True)
    evidence = importlib.import_module(f"{SPEC.name}.controller")
    incomplete = evidence.write_evidence(controller, "brokered-provider-diagnostic", {
        "schema_version": "fanout-brokered-diagnostic-v1",
        "run_id": request.run_id, "task_id": request.task_id,
        "target_id": request.target_id, "seat_id": request.seat_id,
        "inputs_digest": request.inputs_digest,
        "profile_digest": request.profile.digest,
        "route": f"initial:assigned:{request.session_id}",
        "session_id": request.session_id,
        "valid": True, "reason": "ok", "process_status": "exit", "exit_code": 0,
    })
    module = sys.modules[f"{SPEC.name}.native_boundary"]
    with _broker_listener(request) as listener:
        with pytest.raises(fanout.ProviderRequestError, match="initial"):
            module.issue_broker_admission(
                controller, verification, request, command, listener,
                initial_receipt=incomplete,
            )


def _signed_initial_result(controller, request, boundary_digest):
    evidence = importlib.import_module(f"{SPEC.name}.controller")
    return evidence.write_evidence(controller, "brokered-provider-diagnostic", {
        "schema_version": "fanout-brokered-diagnostic-v1",
        "run_id": request.run_id, "task_id": request.task_id,
        "target_id": request.target_id, "seat_id": request.seat_id,
        "inputs_digest": request.inputs_digest,
        "profile_digest": request.profile.digest,
        "boundary_receipt_digest": boundary_digest,
        "route": f"initial:assigned:{request.session_id}",
        "session_id": request.session_id,
        "valid": True, "reason": "ok", "process_status": "exit", "exit_code": 0,
        "stdout_sha256": "1" * 64, "stderr_sha256": "2" * 64,
        "observed_model": request.profile.requested_model,
        "broker_model": request.profile.requested_model,
        "effective_output_tokens": [128],
        "refused_agy_auxiliary_requests": 0,
    })


def test_broker_resume_refuses_initial_boundary_without_admission_chain(seat):
    controller, verification, _, _, _, _ = seat
    initial, _ = _read_only_claude(seat)
    request = replace(initial, resume=True)
    command = request.profile.adapter.build_command(request, brokered_diagnostic=True)
    evidence = importlib.import_module(f"{SPEC.name}.controller")
    old_boundary = evidence.write_evidence(controller, "native-seat-boundary", {
        "schema_version": "fanout-native-seat-boundary-v1",
        "run_id": request.run_id, "task_id": request.task_id,
        "target_id": request.target_id, "seat_id": request.seat_id,
        "inputs_digest": request.inputs_digest,
        "profile_digest": request.profile.digest,
        "route": f"initial:assigned:{request.session_id}",
        "broker_port": 54321,
    })
    prior = _signed_initial_result(controller, request, old_boundary[1])
    module = sys.modules[f"{SPEC.name}.native_boundary"]
    with _broker_listener(request) as listener:
        with pytest.raises(fanout.ProviderRequestError, match="initial boundary"):
            module.issue_broker_admission(
                controller, verification, request, command, listener,
                initial_receipt=prior,
            )


def test_broker_resume_refuses_prior_boundary_with_write_capable_policy(seat):
    controller, verification, _, _, other, owner = seat
    initial, initial_command = _read_only_claude(seat)
    module = sys.modules[f"{SPEC.name}.native_boundary"]
    evidence = importlib.import_module(f"{SPEC.name}.controller")
    with _broker_listener(initial) as listener:
        admission = module.issue_broker_admission(
            controller, verification, initial, initial_command, listener,
        )
        boundary = fanout.issue_native_boundary(
            controller, verification, run_id="run-1", task_id="task-1",
            target_id="target-1", inputs_digest="a" * 64, request=initial,
            command=initial_command, other_target_roots=(other,),
            denied_owner_roots=(owner,), broker_listener=listener,
            broker_admission=admission,
        )
    payload = evidence.read_evidence(controller, "native-seat-boundary",
                                     boundary.receipt_name, boundary.receipt_digest)
    permissive = module._policy(initial.cwd, boundary.state_root, boundary.executable_path,
                                 boundary.denied_paths, execution_class="repo-write",
                                 broker_port=boundary.broker_port)
    payload["profile_sha256"] = hashlib.sha256(permissive).hexdigest()
    permissive_path = controller.root / "native-profiles" / f'{payload["profile_sha256"]}.sb'
    permissive_path.write_bytes(permissive)
    permissive_path.chmod(0o400)
    payload["profile_path"] = str(permissive_path)
    old_boundary = evidence.write_evidence(controller, "native-seat-boundary", payload)
    request = replace(initial, resume=True)
    command = request.profile.adapter.build_command(request, brokered_diagnostic=True)
    prior = _signed_initial_result(controller, request, old_boundary[1])
    with _broker_listener(request) as listener:
        with pytest.raises(fanout.ProviderRequestError, match="initial boundary"):
            module.issue_broker_admission(
                controller, verification, request, command, listener,
                initial_receipt=prior,
            )


@pytest.mark.parametrize("change", ("shell-profile", "write-argv"))
def test_native_issue_rechecks_command_even_with_signed_broker_admission(seat, change):
    controller, verification, _, _, other, owner = seat
    request, command = _read_only_claude(seat)
    module = sys.modules[f"{SPEC.name}.native_boundary"]
    evidence = importlib.import_module(f"{SPEC.name}.controller")
    with _broker_listener(request) as listener:
        admission = module.issue_broker_admission(
            controller, verification, request, command, listener,
        )
        payload = evidence.read_evidence(controller, "native-broker-diagnostic-admission",
                                         *admission)
        if change == "shell-profile":
            profile = replace(request.profile, initial_argv=("/bin/bash",),
                              resume_argv=("/bin/bash",), profile_sha256=None)
            request = replace(request, profile=profile)
            command = fanout.ProcessCommand(
                argv=("/bin/bash", "-c", "printf bypass"), stdin=request.prompt_bytes,
                cwd=request.cwd,
            )
            payload["profile_digest"] = profile.digest
        else:
            command = replace(command, argv=(*command.argv, "--dangerously-skip-permissions"))
        payload["command_digest"] = module._command_digest(command)
        forged = evidence.write_evidence(controller, "native-broker-diagnostic-admission",
                                         payload)
        with pytest.raises(fanout.ProviderRequestError, match="broker diagnostic"):
            fanout.issue_native_boundary(
                controller, verification, run_id="run-1", task_id="task-1",
                target_id="target-1", inputs_digest="a" * 64, request=request,
                command=command, other_target_roots=(other,), denied_owner_roots=(owner,),
                broker_listener=listener, broker_admission=forged,
            )


def test_controller_issued_broker_listener_is_exact_and_authenticated(seat):
    controller, verification, _, _, other, owner = seat
    request, command = _read_only_claude(seat)
    module = sys.modules[f"{SPEC.name}.native_boundary"]
    with _broker_listener(request) as listener:
        admission = module.issue_broker_admission(
            controller, verification, request, command, listener,
        )
        boundary = fanout.issue_native_boundary(
            controller, verification, run_id="run-1", task_id="task-1",
            target_id="target-1", inputs_digest="a" * 64, request=request,
            command=command, other_target_roots=(other,), denied_owner_roots=(owner,),
            broker_listener=listener, broker_admission=admission,
        )
        policy = boundary.profile_path.read_text()
        assert '(deny network-outbound)' in policy
        assert f'(allow network-outbound (remote tcp "localhost:{listener.port}"))' in policy
        assert '(deny network-bind)' in policy
        assert '(deny network-inbound)' in policy
        assert '(deny mach-lookup)' in policy
        assert '(deny ipc-posix-shm)' in policy
        assert boundary.broker_port == listener.port
        assert dict(boundary.frozen_environment)["LLM_FANOUT_BROKER_URL"] == (
            f"http://127.0.0.1:{listener.port}")
        fanout.validate_native_boundary(boundary, request, command, controller, verification)
        with pytest.raises(fanout.ProviderRequestError, match="boundary"):
            fanout.validate_native_boundary(replace(boundary, broker_port=listener.port - 1),
                                            request, command, controller, verification)
        with pytest.raises(fanout.ProviderRequestError, match="boundary"):
            fanout.validate_native_boundary(
                replace(boundary, broker_admission=("0" * 64 + ".json", "0" * 64)),
                request, command, controller, verification,
            )
        with pytest.raises(fanout.ProviderRequestError, match="boundary"):
            fanout.validate_native_boundary(
                boundary, replace(request, prompt="changed diagnostic prompt"), None,
                controller, verification,
            )
    with pytest.raises(fanout.ProviderRequestError, match="boundary"):
        fanout.validate_native_boundary(boundary, request, command, controller, verification)
    with _broker_listener(request, target_id="wrong-target") as wrong_listener:
        with pytest.raises(fanout.ProviderRequestError, match="admission"):
            module.issue_broker_admission(
                controller, verification, request, command, wrong_listener,
            )
    with _broker_listener(request, inputs_digest="b" * 64) as wrong_inputs:
        with pytest.raises(fanout.ProviderRequestError, match="admission"):
            module.issue_broker_admission(
                controller, verification, request, command, wrong_inputs,
            )
    with pytest.raises(fanout.ProviderRequestError, match="admission"):
        module.issue_broker_admission(
            controller, verification, request, command, object(),
        )


def test_broker_listener_binds_exact_resume_session(seat):
    original, _ = _read_only_claude(seat)
    request = replace(original, resume=True)
    module = sys.modules[f"{SPEC.name}.native_boundary"]
    with _broker_listener(request) as listener:
        module._broker_binding(listener, request)
    with _broker_listener(request, session_id="other-session") as wrong:
        with pytest.raises(ValueError, match="permit differs"):
            module._broker_binding(wrong, request)


@pytest.mark.parametrize("executor_id,endpoint_key,placeholder_key", (
    ("claude", "ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY"),
    ("agy", "GOOGLE_GEMINI_BASE_URL", "GEMINI_API_KEY"),
))
def test_brokered_provider_environment_contains_only_placeholder(
    seat, executor_id, endpoint_key, placeholder_key,
):
    controller, verification, original, _, other, owner = seat
    profile = fanout.ProviderRegistry.default().select(executor_id, "read-only", "standard")
    request = replace(
        original, executor_id=executor_id, execution_class="read-only", profile=profile,
        session_id=("11111111-1111-4111-8111-111111111111"
                    if executor_id == "claude" else None),
    )
    command = profile.adapter.build_command(request, brokered_diagnostic=True)
    module = sys.modules[f"{SPEC.name}.native_boundary"]
    with _broker_listener(request) as listener:
        admission = module.issue_broker_admission(
            controller, verification, request, command, listener,
        )
        boundary = fanout.issue_native_boundary(
            controller, verification, run_id="run-1", task_id="task-1",
            target_id="target-1", inputs_digest="a" * 64, request=request,
            command=command, other_target_roots=(other,), denied_owner_roots=(owner,),
            broker_listener=listener, broker_admission=admission,
        )
        child = dict(boundary.frozen_environment)
        endpoint = f"http://127.0.0.1:{listener.port}"
        assert child[endpoint_key] == endpoint
        assert child[placeholder_key] == "disposable-placeholder"
        assert "controller-only-credential" not in repr(child)
        assert "GOOGLE_APPLICATION_CREDENTIALS" not in child
        assert "SSH_AUTH_SOCK" not in child
        fanout.validate_native_boundary(boundary, request, command, controller, verification)


def test_agy_native_binding_pins_measured_cli_to_wire_model(seat):
    _, _, original, _, _, _ = seat
    profile = fanout.ProviderRegistry.default().select("agy", "read-only", "standard")
    request = replace(original, executor_id="agy", execution_class="read-only",
                      profile=profile)
    module = sys.modules[f"{SPEC.name}.native_boundary"]
    with _broker_listener(request, wire_model="gemini-3.8-flash") as listener:
        module._broker_binding(listener, request)
    with _broker_listener(request, wire_model="gemini-3.8-pro") as wrong:
        with pytest.raises(ValueError, match="permit differs"):
            module._broker_binding(wrong, request)


def test_native_launch_uses_issued_environment_after_ambient_changes(seat, monkeypatch):
    controller, verification, request, _, other, owner = seat
    monkeypatch.setenv("LLM_FANOUT_DEPTH", "0")
    monkeypatch.setenv("TERM", "fanout-issued-term")
    command = fanout.ProcessCommand(
        argv=("/bin/sh", "-c", 'printf "%s|%s|%s" "$TERM" "$LLM_FANOUT_DEPTH" "$HOME"'),
        stdin=b"", cwd=request.cwd,
    )
    boundary = fanout.issue_native_boundary(
        controller, verification, run_id="run-1", task_id="task-1",
        target_id="target-1", inputs_digest="a" * 64,
        request=request, command=command, other_target_roots=(other,),
        denied_owner_roots=(owner,),
    )
    resolved = (str(Path("/bin/sh").resolve()), *command.argv[1:])
    assert boundary.resolved_argv == resolved
    assert boundary.frozen_environment == tuple(sorted(dict(boundary.frozen_environment).items()))
    issued_environment = dict(boundary.frozen_environment)
    monkeypatch.setenv("LLM_FANOUT_DEPTH", "2")
    monkeypatch.setenv("TERM", "fanout-changed-term")
    process = sys.modules[f"{SPEC.name}.process"]
    real_popen = process.subprocess.Popen
    spawned = []
    def capture_popen(argv, **kwargs):
        if argv[0] == "/usr/bin/sandbox-exec":
            spawned.append((tuple(argv), dict(kwargs["env"])))
        return real_popen(argv, **kwargs)
    monkeypatch.setattr(process.subprocess, "Popen", capture_popen)
    wrapped = fanout.wrap_native_command(
        command, boundary, request=request, controller=controller,
        verification=verification,
    )
    result = fanout.run_command(wrapped)
    assert result.returncode == 0, result.stderr
    assert spawned == [(wrapped.argv, issued_environment)]
    assert wrapped.argv[3:] == resolved
    assert result.stdout.decode() == (
        f"fanout-issued-term|1|{boundary.state_root / 'home'}"
    )


def test_native_descriptor_rejects_changed_frozen_environment(seat):
    controller, verification, request, command, _, _ = seat
    boundary = _boundary(seat)
    assert dict(boundary.frozen_environment)["HOME"] == str(boundary.state_root / "home")
    changed = replace(boundary, frozen_environment=(("HOME", "/"),))
    with pytest.raises(fanout.ProviderRequestError, match="boundary"):
        fanout.validate_native_boundary(changed, request, command,
                                        controller, verification)


def test_native_launch_rejects_caller_environment_override(seat):
    controller, verification, request, command, _, _ = seat
    boundary = _boundary(seat)
    wrapped = fanout.wrap_native_command(
        command, boundary, request=request, controller=controller,
        verification=verification,
    )
    with pytest.raises(fanout.ProcessValidationError, match="fixed at boundary issuance"):
        fanout.run_command(wrapped, base_environment={"TERM": "caller-choice"})


def test_native_launch_rejects_post_issue_timeout_change(seat):
    controller, verification, request, command, _, _ = seat
    boundary = _boundary(seat)
    wrapped = fanout.wrap_native_command(
        command, boundary, request=request, controller=controller,
        verification=verification,
    )
    with pytest.raises(fanout.ProcessValidationError, match="wrapper changed"):
        fanout.run_command(replace(wrapped, timeout=command.timeout + 1))


def test_claude_native_environment_sets_private_internal_temp(seat):
    controller, verification, request, command, other, owner = seat
    adapter = fanout.ClaudeAdapter()
    profile = fanout.ExecutorProfile(
        "claude", adapter, adapter.capabilities, "repo-write", "standard",
        "diagnostic-model", "low", "diagnostic-cli", ("/bin/sh",),
        ("/bin/sh", "--resume", "{session_id}"), 10, 10,
    )
    claude = replace(request, executor_id="claude", profile=profile,
                     session_id="123e4567-e89b-42d3-a456-426614174000")
    boundary = fanout.issue_native_boundary(
        controller, verification, run_id="run-1", task_id="task-1",
        target_id="target-1", inputs_digest="a" * 64,
        request=claude, command=command, other_target_roots=(other,),
        denied_owner_roots=(owner,),
    )
    assert dict(boundary.frozen_environment)["CLAUDE_CODE_TMPDIR"] == str(
        boundary.state_root / "tmp"
    )


def test_descriptor_rejects_cwd_route_profile_and_workspace_swaps(seat):
    controller, verification, request, command, _, _ = seat
    boundary = _boundary(seat)
    other_cwd = request.cwd.parent
    with pytest.raises(fanout.ProviderRequestError, match="boundary"):
        fanout.validate_native_boundary(boundary, replace(request, cwd=other_cwd), command,
                                        controller, verification)
    with pytest.raises(fanout.ProviderRequestError, match="boundary"):
        fanout.validate_native_boundary(boundary, replace(request, resume=True,
            session_id="exact-session"), command, controller, verification)
    with pytest.raises(fanout.ProviderRequestError, match="boundary"):
        fanout.validate_native_boundary(replace(boundary, profile_sha256="0" * 64),
                                        request, command, controller, verification)
    replacement = request.cwd.parent / "old-workspace"
    request.cwd.rename(replacement)
    request.cwd.mkdir()
    with pytest.raises(fanout.ProviderRequestError, match="boundary"):
        fanout.validate_native_boundary(boundary, request, command, controller, verification)


def test_descriptor_binds_initial_assigned_session_id(seat):
    controller, verification, request, command, other, owner = seat
    assigned = replace(request, session_id="initial-session-a")
    boundary = fanout.issue_native_boundary(
        controller, verification, run_id="run-1", task_id="task-1",
        target_id="target-1", inputs_digest="a" * 64,
        request=assigned, command=command, other_target_roots=(other,),
        denied_owner_roots=(owner,),
    )
    fanout.validate_native_boundary(boundary, assigned, command, controller, verification)
    for changed in (replace(assigned, session_id="initial-session-b"),
                    replace(assigned, session_id=None)):
        with pytest.raises(fanout.ProviderRequestError, match="boundary"):
            fanout.validate_native_boundary(boundary, changed, command,
                                            controller, verification)


def test_exact_resume_reuses_private_seat_state_without_reusing_route_evidence(seat):
    controller, verification, request, _, other, owner = seat
    initial = replace(request, session_id="exact-session")
    initial_command = fanout.ProcessCommand(
        argv=("/bin/sh", "-c", 'printf session-marker > "$HOME/session.txt"'),
        stdin=b"", cwd=request.cwd,
    )
    initial_boundary = fanout.issue_native_boundary(
        controller, verification, run_id="run-1", task_id="task-1",
        target_id="target-1", inputs_digest="a" * 64,
        request=initial, command=initial_command,
        other_target_roots=(other,), denied_owner_roots=(owner,),
    )
    initial_result = fanout.run_command(fanout.wrap_native_command(
        initial_command, initial_boundary, request=initial,
        controller=controller, verification=verification,
    ))
    assert initial_result.returncode == 0

    resumed = replace(initial, resume=True)
    resume_command = fanout.ProcessCommand(
        argv=("/bin/sh", "-c", 'IFS= read -r value < "$HOME/session.txt"; test "$value" = session-marker'),
        stdin=b"", cwd=request.cwd,
    )
    resume_boundary = fanout.issue_native_boundary(
        controller, verification, run_id="run-1", task_id="task-1",
        target_id="target-1", inputs_digest="a" * 64,
        request=resumed, command=resume_command,
        other_target_roots=(other,), denied_owner_roots=(owner,),
    )
    assert resume_boundary.state_root == initial_boundary.state_root
    assert resume_boundary.route != initial_boundary.route
    assert resume_boundary.command_digest != initial_boundary.command_digest
    assert resume_boundary.receipt_digest != initial_boundary.receipt_digest
    resume_result = fanout.run_command(fanout.wrap_native_command(
        resume_command, resume_boundary, request=resumed,
        controller=controller, verification=verification,
    ))
    assert resume_result.returncode == 0

    baseline = fanout.capture_repository_baseline(controller.root.parent / "repository")
    other_workspace = fanout.create_seat_workspace(
        baseline, controller.root / "workspaces", "codex-seat-other",
    )
    other_verification = fanout.verify_seat_workspace(
        baseline, other_workspace, controller=controller,
    )
    other_seat = replace(initial, cwd=other_workspace.root, seat_id="codex-seat-other")
    other_command = replace(initial_command, cwd=other_workspace.root)
    other_boundary = fanout.issue_native_boundary(
        controller, other_verification, run_id="run-1", task_id="task-1",
        target_id="target-1", inputs_digest="a" * 64,
        request=other_seat, command=other_command,
        other_target_roots=(other,), denied_owner_roots=(owner,),
    )
    assert other_boundary.state_root != initial_boundary.state_root


def test_descriptor_rejects_changed_argv_stdin_and_route_command(seat):
    controller, verification, request, command, _, _ = seat
    boundary = _boundary(seat)
    for changed in (
        replace(command, argv=("/bin/sh", "-c", "printf different")),
        replace(command, stdin=b"different private prompt"),
        replace(command, argv=("/bin/sh", "--resume", "foreign-session")),
    ):
        with pytest.raises(fanout.ProviderRequestError, match="boundary"):
            fanout.validate_native_boundary(boundary, request, changed,
                                            controller, verification)
    with pytest.raises(fanout.ProviderRequestError, match="boundary"):
        fanout.validate_native_boundary(
            replace(boundary, resolved_argv=(str(Path("/bin/sh").resolve()), "-c", "printf forged")),
            request, command, controller, verification,
        )


def test_native_boundary_rejects_home_dependent_executable_wrapper(seat):
    controller, verification, request, command, other, owner = seat
    wrapper = controller.root.parent / "home-dependent-wrapper"
    wrapper.write_text('#!/bin/sh\nexec "$HOME/.local/bin/agy-bin" "$@"\n')
    wrapper.chmod(0o700)
    profile = replace(request.profile, initial_argv=(str(wrapper),),
                      resume_argv=(str(wrapper), "--resume", "{session_id}"),
                      profile_sha256=None)
    wrapped_request = replace(request, profile=profile)
    wrapped_command = replace(command, argv=(str(wrapper),))
    with pytest.raises(fanout.ProviderRequestError, match="issuance failed") as raised:
        _boundary((controller, verification, wrapped_request, wrapped_command, other, owner))
    assert "direct executable" in str(raised.value.__cause__)


@pytest.mark.parametrize("invalid_session", (
    None, "not-a-uuid", "123E4567-E89B-42D3-A456-426614174000",
))
def test_claude_initial_native_route_requires_preassigned_session(seat, invalid_session):
    controller, verification, request, command, other, owner = seat
    adapter = fanout.ClaudeAdapter()
    profile = replace(request.profile, executor_id="claude", adapter=adapter,
                      capabilities=adapter.capabilities, profile_sha256=None)
    unassigned = replace(request, executor_id="claude", profile=profile,
                         session_id=invalid_session)
    with pytest.raises(fanout.ProviderRequestError, match="issuance failed") as raised:
        _boundary((controller, verification, unassigned, command, other, owner))
    assert "preassigned session" in str(raised.value.__cause__)
    assigned = replace(unassigned, session_id="123e4567-e89b-42d3-a456-426614174000")
    boundary = _boundary((controller, verification, assigned, command, other, owner))
    assert boundary.route == "initial:assigned:123e4567-e89b-42d3-a456-426614174000"


def test_descriptor_rejects_unadmitted_staged_skill_read_grant(seat):
    controller, verification, request, command, other, owner = seat
    staged = controller.root / "receipts"
    changed = replace(request, staged_skill_root=staged)
    with pytest.raises(fanout.ProviderRequestError, match="staged skill"):
        fanout.issue_native_boundary(
            controller, verification, run_id="run-1", task_id="task-1",
            target_id="target-1", inputs_digest="a" * 64, request=changed,
            command=command, other_target_roots=(other,), denied_owner_roots=(owner,),
        )


def _staged_skill_request(seat):
    controller, _, request, _, _, _ = seat
    stage_parent = controller.root.parent / "skill-stage"
    stage_parent.mkdir(mode=0o700)
    source = controller.root.parent / "skill-source"
    source.mkdir(mode=0o700)
    resolver = fanout.SkillResolver((fanout.SkillRoot("root", source, 0),))
    admission = resolver.admit(
        (), task_id="task-1", seat_id="codex-seat", provider="codex",
        session_id="skill-session",
    )
    admission = resolver.stage(admission, stage_parent / "codex-seat")
    admission = resolver.verify_engine_delivery(admission, ())
    admitted = replace(
        request, staged_skill_root=admission.staged_root,
        staged_skill_admission=admission,
        skill_bundle_sha256=hashlib.sha256(fanout.canonical_json({
            "schema_version": "fanout-seat-skill-bundle-v1",
            "skills": admission.manifest_dict()["skills"],
        })).hexdigest(),
        skill_delivery_sha256=hashlib.sha256(fanout.canonical_json([])).hexdigest(),
    )
    command = fanout.ProcessCommand(
        argv=("/bin/sh", "-c", 'IFS= read -r value < "$1"', "sh",
              str(admission.staged_root / "manifest.json")),
        stdin=b"", cwd=request.cwd,
    )
    return admission, admitted, command


def test_descriptor_grants_only_controller_admitted_staged_skill_read(seat):
    controller, verification, _, _, other, owner = seat
    admission, admitted, command = _staged_skill_request(seat)
    boundary = fanout.issue_native_boundary(
        controller, verification, run_id="run-1", task_id="task-1",
        target_id="target-1", inputs_digest="a" * 64, request=admitted,
        command=command, other_target_roots=(other,), denied_owner_roots=(owner,),
    )
    fanout.validate_native_boundary(boundary, admitted, command, controller, verification)
    assert f'(subpath "{admission.staged_root}")' in boundary.profile_path.read_text()
    observed = fanout.run_command(fanout.wrap_native_command(
        command, boundary, request=admitted, controller=controller,
        verification=verification,
    ))
    assert observed.returncode == 0


@pytest.mark.parametrize(("digest", "message"), (
    ("", "staged skill bundle digest is required"),
    ("0" * 64, "staged skill bundle digest changed"),
))
def test_descriptor_rejects_staged_skill_with_missing_or_mismatched_bundle_digest(
    seat, digest, message,
):
    controller, verification, _, _, other, owner = seat
    _, admitted, command = _staged_skill_request(seat)
    changed = replace(admitted, skill_bundle_sha256=digest)
    with pytest.raises(fanout.ProviderRequestError, match=message):
        fanout.issue_native_boundary(
            controller, verification, run_id="run-1", task_id="task-1",
            target_id="target-1", inputs_digest="a" * 64,
            request=changed, command=command,
            other_target_roots=(other,), denied_owner_roots=(owner,),
        )


def test_descriptor_refuses_missing_changed_or_tampered_skill_grant(seat):
    controller, verification, _, _, other, owner = seat
    admission, admitted, command = _staged_skill_request(seat)
    with pytest.raises(fanout.ProviderRequestError, match="staged skill"):
        fanout.issue_native_boundary(
            controller, verification, run_id="run-1", task_id="task-1",
            target_id="target-1", inputs_digest="a" * 64,
            request=replace(admitted, staged_skill_admission=None), command=command,
            other_target_roots=(other,), denied_owner_roots=(owner,),
        )
    boundary = fanout.issue_native_boundary(
        controller, verification, run_id="run-1", task_id="task-1",
        target_id="target-1", inputs_digest="a" * 64, request=admitted,
        command=command, other_target_roots=(other,), denied_owner_roots=(owner,),
    )
    with pytest.raises(fanout.ProviderRequestError, match="boundary"):
        fanout.validate_native_boundary(
            boundary, replace(admitted, target_id="other-target"), command,
            controller, verification,
        )
    manifest = admission.staged_root / "manifest.json"
    manifest.chmod(0o600)
    manifest.write_bytes(b"tampered")
    manifest.chmod(0o400)
    with pytest.raises(fanout.ProviderRequestError, match="boundary"):
        fanout.validate_native_boundary(boundary, admitted, command, controller, verification)


def test_descriptor_refuses_staged_skill_inside_denied_owner_root(seat):
    controller, verification, _, _, other, owner = seat
    admission, admitted, command = _staged_skill_request(seat)
    stage_parent = owner / "skill-stage"
    stage_parent.mkdir(mode=0o700)
    moved = stage_parent / "seat"
    admission.staged_root.parent.rename(moved)
    new_root = moved / "ready"
    changed_admission = replace(admission, staged_root=new_root)
    changed_request = replace(
        admitted, staged_skill_root=new_root,
        staged_skill_admission=changed_admission,
    )
    changed_command = replace(command, argv=(*command.argv[:-1], str(new_root / "manifest.json")))
    with pytest.raises(fanout.ProviderRequestError, match="staged skill"):
        fanout.issue_native_boundary(
            controller, verification, run_id="run-1", task_id="task-1",
            target_id="target-1", inputs_digest="a" * 64, request=changed_request,
            command=changed_command, other_target_roots=(other,), denied_owner_roots=(owner,),
        )


def test_descriptor_rejects_symlink_and_hardlink_aliases(seat):
    _, _, request, _, other, _ = seat
    (request.cwd / "alias").symlink_to(other, target_is_directory=True)
    with pytest.raises(fanout.ProviderRequestError, match="alias"):
        _boundary(seat)
    (request.cwd / "alias").unlink()
    (request.cwd / "alias").symlink_to(seat[0].root / "receipts", target_is_directory=True)
    with pytest.raises(fanout.ProviderRequestError, match="alias"):
        _boundary(seat)
    (request.cwd / "alias").unlink()
    os.link(other / "secret.txt", request.cwd / "hardlink")
    with pytest.raises(fanout.ProviderRequestError, match="alias"):
        _boundary(seat)


def test_descriptor_rejects_staged_skill_root_with_symlink_ancestor(seat):
    controller, verification, request, _, other, owner = seat
    parent = owner / "real"
    parent.mkdir()
    staged = parent / "staged"
    staged.mkdir()
    alias = owner / "alias"
    alias.symlink_to(parent, target_is_directory=True)
    changed = replace(request, staged_skill_root=alias / "staged")
    with pytest.raises(fanout.ProviderRequestError, match="staged skill"):
        fanout.issue_native_boundary(
            controller, verification, run_id="run-1", task_id="task-1",
            target_id="target-1", inputs_digest="a" * 64, request=changed,
            command=seat[3],
            other_target_roots=(other,), denied_owner_roots=(owner,),
        )


def test_descriptor_rejects_tampered_profile_and_foreign_controller(seat, tmp_path):
    controller, verification, request, command, _, _ = seat
    boundary = _boundary(seat)
    boundary.profile_path.chmod(0o600)
    boundary.profile_path.write_text("(version 1) (allow default)")
    with pytest.raises(fanout.ProviderRequestError, match="boundary"):
        fanout.validate_native_boundary(boundary, request, command, controller, verification)
    foreign = fanout.create_lifecycle_controller(tmp_path / "foreign")
    with pytest.raises(fanout.ProviderRequestError, match="boundary"):
        fanout.validate_native_boundary(boundary, request, command, foreign, verification)


def test_native_command_rejects_credential_environment(seat):
    controller, verification, request, command, _, _ = seat
    boundary = _boundary(seat)
    credential_command = replace(command, environment={
        "GOOGLE_APPLICATION_CREDENTIALS": str(request.cwd / "fake-adc.json"),
    })
    with pytest.raises(fanout.ProviderRequestError, match="boundary"):
        fanout.wrap_native_command(
            credential_command, boundary, request=request,
            controller=controller, verification=verification,
        )


def test_process_revalidates_profile_immediately_before_spawn(seat, monkeypatch):
    controller, verification, request, command, _, _ = seat
    boundary = _boundary(seat)
    wrapped = fanout.wrap_native_command(
        command, boundary, request=request, controller=controller,
        verification=verification,
    )
    boundary.profile_path.chmod(0o600)
    boundary.profile_path.write_text("(version 1) (allow default)")
    process = sys.modules[f"{SPEC.name}.process"]
    real_popen = process.subprocess.Popen
    def guarded_popen(argv, **kwargs):
        if argv[0] == "/usr/bin/sandbox-exec":
            pytest.fail("spawned after native profile changed")
        return real_popen(argv, **kwargs)
    monkeypatch.setattr(process.subprocess, "Popen", guarded_popen)
    with pytest.raises(fanout.ProviderRequestError, match="boundary"):
        fanout.run_command(wrapped)


def test_process_rejects_changed_wrapped_stdin_before_spawn(seat, monkeypatch):
    controller, verification, request, command, _, _ = seat
    boundary = _boundary(seat)
    wrapped = fanout.wrap_native_command(
        command, boundary, request=request, controller=controller,
        verification=verification,
    )
    process = sys.modules[f"{SPEC.name}.process"]
    real_popen = process.subprocess.Popen
    def guarded_popen(argv, **kwargs):
        if argv[0] == "/usr/bin/sandbox-exec":
            pytest.fail("spawned with changed command")
        return real_popen(argv, **kwargs)
    monkeypatch.setattr(process.subprocess, "Popen", guarded_popen)
    with pytest.raises(fanout.ProcessValidationError, match="wrapper changed"):
        fanout.run_command(replace(wrapped, stdin=b"different private prompt"))


def test_public_provider_launch_stays_closed_with_a_valid_descriptor(seat, monkeypatch):
    controller, verification, request, _, _, _ = seat
    boundary = _boundary(seat)
    bound = replace(request, native_boundary=boundary, native_controller=controller,
                    native_verification=verification)
    providers = sys.modules[f"{SPEC.name}.providers"]
    monkeypatch.setattr(fanout.CodexAdapter, "build_command", lambda *_args:
                        pytest.fail("adapter ran before live route gate"))
    monkeypatch.setattr(providers, "_installed_cli_version", lambda _executor:
                        pytest.fail("version probe preceded live route gate"))
    monkeypatch.setattr(providers, "run_command", lambda _command:
                        pytest.fail("provider spent before live route certificate"))
    with pytest.raises(fanout.ProviderRequestError, match="live-certified"):
        fanout.run_provider(bound)
    with pytest.raises(fanout.ProviderRequestError, match="live-certified"):
        fanout.run_many((bound,), max_workers=1)


def test_v2_read_only_provider_request_needs_native_boundary_before_spend(seat, monkeypatch):
    _, _, request, _, _, _ = seat
    read_only = replace(
        request, execution_class="read-only",
        profile=fanout.ProviderRegistry.default().select("codex", "read-only", "standard"),
    )
    providers = sys.modules[f"{SPEC.name}.providers"]
    monkeypatch.setattr(providers, "_installed_cli_version", lambda _executor:
                        pytest.fail("v2 version probe preceded native boundary gate"))
    with pytest.raises(fanout.ProviderRequestError, match="native seat boundary"):
        fanout.run_provider(read_only)
    with pytest.raises(fanout.ProviderRequestError, match="native seat boundary"):
        fanout.run_many((read_only,), max_workers=1)


def test_native_probe_receipt_records_observed_denials_and_workspace_write(seat):
    controller, verification, request, command, other, owner = seat
    boundary = _boundary(seat)
    request = replace(request, artifact_store=fanout.ArtifactStore(
        controller.root / "probe-artifacts"), artifact_prefix="native-probe")
    oracles = {"other": other / "secret.txt", "owner": owner / "run.json",
               "profile": boundary.profile_path}
    receipt = fanout.run_native_boundary_probe(
        boundary, request, command, controller=controller,
        verification=verification, oracle_paths=oracles,
    )
    assert receipt.exit_code == 0
    assert receipt.denied_paths == tuple(str(path) for path in oracles.values())
    assert receipt.allowed_workspace_write is True
    assert receipt.credential_blind is False
    assert request.artifact_store.read_bytes(receipt.transcript)


def test_native_probe_rejects_reachable_oracle_without_a_receipt(seat):
    controller, verification, request, command, _, _ = seat
    boundary = _boundary(seat)
    request = replace(request, artifact_store=fanout.ArtifactStore(
        controller.root / "probe-artifacts"), artifact_prefix="native-probe")
    with pytest.raises(fanout.ProviderRequestError, match="oracle"):
        fanout.run_native_boundary_probe(
            boundary, request, command, controller=controller,
            verification=verification,
            oracle_paths={"seat-file": request.cwd / "fixture.txt"},
        )
    assert not tuple((controller.root / "probe-artifacts").rglob("native-probe-*.json"))


def test_native_probe_fail_open_policy_preserves_protected_bytes(seat, monkeypatch):
    controller, verification, request, command, other, _ = seat
    module = sys.modules[f"{SPEC.name}.native_boundary"]
    monkeypatch.setattr(module, "_policy", lambda *_args, **_kwargs:
                            b"(version 1) (allow default)\n")
    boundary = _boundary(seat)
    request = replace(request, artifact_store=fanout.ArtifactStore(
        controller.root / "probe-artifacts"), artifact_prefix="native-probe")
    before = (other / "secret.txt").read_bytes()
    with pytest.raises(fanout.ProviderRequestError, match="oracle"):
        fanout.run_native_boundary_probe(
            boundary, request, command, controller=controller,
            verification=verification,
            oracle_paths={"other": other / "secret.txt"},
        )
    assert (other / "secret.txt").read_bytes() == before
    assert not tuple((controller.root / "probe-artifacts").rglob("native-probe-*.json"))


def test_generated_policy_resolves_localhost_without_widening_boundary(seat):
    if sys.platform != "darwin":
        pytest.skip("macOS Seatbelt required")
    controller, _, request, _, other, owner = seat
    module = sys.modules[f"{SPEC.name}.native_boundary"]
    state = controller.root / "native-seat-state" / "resolver-probe"
    (state / "home").mkdir(parents=True)
    (state / "tmp").mkdir()
    python = Path("/Library/Developer/CommandLineTools/usr/bin/python3").resolve()
    policy = module._policy(request.cwd, state, python, (other, owner),
                            execution_class="repo-write")
    profile = controller.root / "resolver.sb"
    profile.write_bytes(policy)
    probe = (
        "import errno, os, socket, sys\n"
        "addresses = socket.getaddrinfo('localhost', 0, socket.AF_INET, socket.SOCK_STREAM)\n"
        "assert any(address[4][0] == '127.0.0.1' for address in addresses)\n"
        "assert os.stat('/etc')\n"
        "with open('/etc/hosts', 'rb') as hosts: assert hosts.read(1)\n"
        "try: open(sys.argv[1], 'rb').read(1)\n"
        "except PermissionError: pass\n"
        "else: raise AssertionError('protected read allowed')\n"
        "with socket.socket() as connection:\n"
        "    assert connection.connect_ex(('127.0.0.1', 9)) in (errno.EPERM, errno.EACCES)\n"
        "print('resolver and denials verified')\n"
    )
    result = subprocess.run(
        ("/usr/bin/sandbox-exec", "-f", str(profile), str(python),
         "-I", "-B", "-c", probe, str(other / "secret.txt")),
        cwd=request.cwd,
        env={"HOME": str(state / "home"), "TMPDIR": str(state / "tmp"),
             "PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode == 71 and "sandbox_apply: Operation not permitted" in result.stderr:
        pytest.skip("enclosing sandbox denied Seatbelt application; host probe required")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "resolver and denials verified\n"


def test_generated_policy_allows_only_controller_broker_port(seat):
    if sys.platform != "darwin":
        pytest.skip("macOS Seatbelt required")
    controller, _, request, _, other, owner = seat
    module = sys.modules[f"{SPEC.name}.native_boundary"]
    state = controller.root / "native-seat-state" / "broker-probe"
    (state / "home").mkdir(parents=True)
    (state / "tmp").mkdir()
    python = Path("/Library/Developer/CommandLineTools/usr/bin/python3").resolve()
    workspace_socket_path = request.cwd / "allowed.sock"
    with (socket.socket(socket.AF_UNIX) as workspace_socket,
          socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as datagram,
          socket.socket() as listener):
        workspace_socket.bind(str(workspace_socket_path))
        workspace_socket.listen(1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        datagram.bind(("127.0.0.1", port))
        policy = module._policy(request.cwd, state, python, (other, owner),
                                execution_class="read-only", broker_port=port)
        profile = controller.root / "broker-probe.sb"
        profile.write_bytes(policy)
        repository = controller.root.parent / "repository"
        oid = subprocess.run(
            ("git", "-C", str(repository), "rev-parse", "HEAD"),
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        protected = (
            other / "secret.txt", owner / "run.json",
            controller.root / "controller.json",
            controller.root / "workspaces" / "seats" / "sibling" / "secret.txt",
            repository / ".git" / "objects" / oid[:2] / oid[2:], profile,
        )
        assert all(path.is_file() for path in protected)
        probe = (
            "import errno, socket, sys\n"
            "port = int(sys.argv[1])\n"
            "with socket.socket() as connection:\n"
            "    assert connection.connect_ex(('127.0.0.1', port)) == 0\n"
            "with socket.socket() as adjacent:\n"
            "    assert adjacent.connect_ex(('127.0.0.1', port - 1)) in "
            "(errno.EPERM, errno.EACCES)\n"
            "with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:\n"
            "    try:\n"
            "        udp.sendto(b'probe', ('127.0.0.1', port))\n"
            "    except OSError as error:\n"
            "        assert error.errno in (errno.EPERM, errno.EACCES)\n"
            "    else:\n"
            "        raise AssertionError('UDP reached broker port')\n"
            "with socket.socket() as remote:\n"
            "    remote.settimeout(0.5)\n"
            "    assert remote.connect_ex(('198.51.100.1', 443)) in "
            "(errno.EPERM, errno.EACCES)\n"
            "with socket.socket() as inbound:\n"
            "    try:\n"
            "        inbound.bind(('127.0.0.1', 0))\n"
            "    except OSError as error:\n"
            "        assert error.errno in (errno.EPERM, errno.EACCES)\n"
            "    else:\n"
            "        raise AssertionError('seat could bind an inbound listener')\n"
            "with socket.socket(socket.AF_UNIX) as unix:\n"
            "    assert unix.connect_ex(sys.argv[2]) in (errno.EPERM, errno.EACCES)\n"
            "for forbidden in sys.argv[3:]:\n"
            "    try:\n"
            "        open(forbidden, 'rb').read(1)\n"
            "    except PermissionError:\n"
            "        pass\n"
            "    else:\n"
            "        raise AssertionError('seat read a protected file')\n"
            "print('exact broker port verified')\n"
        )
        result = subprocess.run(
            ("/usr/bin/sandbox-exec", "-f", str(profile), str(python),
             "-I", "-B", "-c", probe, str(port), str(workspace_socket_path),
             *(str(path) for path in protected)),
            cwd=request.cwd,
            env={"HOME": str(state / "home"), "TMPDIR": str(state / "tmp"),
                 "PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
            capture_output=True, text=True, timeout=10,
        )
    if result.returncode == 71 and "sandbox_apply: Operation not permitted" in result.stderr:
        pytest.skip("enclosing sandbox denied Seatbelt application; host probe required")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "exact broker port verified\n"


@pytest.mark.parametrize("broker_port", (None, 54321))
def test_read_only_policy_denies_workspace_write_but_keeps_private_state_writable(
    seat, broker_port,
):
    if sys.platform != "darwin":
        pytest.skip("macOS Seatbelt required")
    controller, _, request, _, other, owner = seat
    module = sys.modules[f"{SPEC.name}.native_boundary"]
    state = controller.root / "native-seat-state" / "read-only-probe"
    (state / "home").mkdir(parents=True)
    (state / "tmp").mkdir()
    profile = controller.root / "read-only-probe.sb"
    profile.write_bytes(module._policy(request.cwd, state, Path("/bin/bash"),
                                       (other, owner), execution_class="read-only",
                                       broker_port=broker_port))
    forbidden = request.cwd / "read-only-write.txt"
    allowed = state / "home" / "session.txt"
    result = subprocess.run(
        ("/usr/bin/sandbox-exec", "-f", str(profile), "/bin/bash", "-c",
         'if printf blocked > "$1" 2>/dev/null; then exit 23; fi; printf allowed > "$2"',
         "sh", str(forbidden), str(allowed)),
        cwd=request.cwd,
        env={"HOME": str(state / "home"), "TMPDIR": str(state / "tmp"),
             "PATH": "/usr/bin:/bin", "LANG": "C"},
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode == 71 and "sandbox_apply: Operation not permitted" in result.stderr:
        pytest.skip("enclosing sandbox denied Seatbelt application; host probe required")
    assert not forbidden.exists()
    assert result.returncode == 0, result.stderr
    assert allowed.read_text() == "allowed"


def test_brokered_policy_denies_host_posix_shared_memory(seat):
    if sys.platform != "darwin":
        pytest.skip("macOS Seatbelt required")
    controller, _, request, _, other, owner = seat
    module = sys.modules[f"{SPEC.name}.native_boundary"]
    shared = SharedMemory(create=True, size=16)
    try:
        host_read = SharedMemory(name=shared.name)
        host_read.close()
        state = controller.root / "native-seat-state" / "shared-memory-probe"
        (state / "home").mkdir(parents=True)
        (state / "tmp").mkdir()
        python = Path("/Library/Developer/CommandLineTools/usr/bin/python3").resolve()
        profile = controller.root / "shared-memory-probe.sb"
        profile.write_bytes(module._policy(request.cwd, state, python, (other, owner),
                                           execution_class="read-only", broker_port=54321))
        program = (
            "import errno, sys\n"
            "from multiprocessing.shared_memory import SharedMemory\n"
            "try:\n"
            "    attached = SharedMemory(name=sys.argv[1])\n"
            "except OSError as error:\n"
            "    assert error.errno in (errno.EPERM, errno.EACCES)\n"
            "else:\n"
            "    attached.close()\n"
            "    raise AssertionError('seat opened host shared memory')\n"
            "print('shared memory denied')\n"
        )
        result = subprocess.run(
            ("/usr/bin/sandbox-exec", "-f", str(profile),
             str(python), "-I", "-B", "-c", program, shared.name),
            cwd=request.cwd,
            env={"HOME": str(state / "home"), "TMPDIR": str(state / "tmp"),
                 "PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 71 and "sandbox_apply: Operation not permitted" in result.stderr:
            pytest.skip("enclosing sandbox denied Seatbelt application; host probe required")
        assert result.returncode == 0, result.stderr
        assert result.stdout == "shared memory denied\n"
    finally:
        shared.close()
        shared.unlink()


@pytest.mark.parametrize("executor_id", ("claude", "codex", "agy"))
def test_native_policy_launches_pinned_provider_version_without_credentials(
    seat, executor_id,
):
    if sys.platform != "darwin":
        pytest.skip("macOS Seatbelt required")
    controller, _, request, _, other, owner = seat
    module = sys.modules[f"{SPEC.name}.native_boundary"]
    candidate = (Path.home() / ".local/libexec/agy-bin" if executor_id == "agy"
                 else Path(shutil.which(executor_id) or "/no-installed-provider"))
    if not candidate.is_file():
        pytest.skip(f"pinned {executor_id} executable is unavailable")
    executable = module._executable(str(candidate))
    executable_digest = module._hash_file(executable)
    state = controller.root / "native-seat-state" / f"version-{executor_id}"
    (state / "home").mkdir(parents=True)
    (state / "tmp").mkdir()
    environment = {
        "HOME": str(state / "home"), "TMPDIR": str(state / "tmp"),
        "PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C",
        "AGY_CLI_DISABLE_AUTO_UPDATE": "true",
    }
    observed = subprocess.run(
        (str(executable), "--version"), cwd=request.cwd, env=environment,
        capture_output=True, text=True, timeout=15,
    )
    assert observed.returncode == 0, observed.stderr
    assert observed.stdout.strip()
    assert module._executable(str(candidate)) == executable
    assert module._hash_file(executable) == executable_digest
    profile = controller.root / f"version-{executor_id}.sb"
    profile.write_bytes(module._policy(request.cwd, state, executable, (other, owner),
                                       execution_class="repo-write"))
    result = subprocess.run(
        ("/usr/bin/sandbox-exec", "-f", str(profile), str(executable), "--version"),
        cwd=request.cwd,
        env=environment,
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == observed.stdout
    assert module._executable(str(candidate)) == executable
    assert module._hash_file(executable) == executable_digest


def test_real_native_denial_and_workspace_write(seat):
    if sys.platform != "darwin":
        pytest.skip("macOS Seatbelt required")
    controller, verification, request, _, other, owner = seat
    boundary = _boundary(seat)
    probe = 'if [ "$1" = read ]; then IFS= read -r x < "$2"; else printf probe > "$2"; fi'
    for operation, path in (
        ("read", other / "secret.txt"),
        ("write", other / "secret.txt"),
        ("read", controller.root / "workspaces/seats/sibling/secret.txt"),
        ("write", controller.root / "workspaces/seats/sibling/secret.txt"),
        ("read", owner / "run.json"),
        ("read", controller.root / "controller.json"),
        ("read", boundary.profile_path),
        ("write", boundary.profile_path),
        ("read", Path("/tmp") / (other / "secret.txt").relative_to("/private/tmp")),
        ("read", Path("/tmp") / boundary.profile_path.relative_to("/private/tmp")),
    ):
        command = replace(seat[3], argv=("/bin/sh", "-c", probe, "sh",
                                         operation, str(path)))
        exact = _boundary(seat, command)
        wrapped = fanout.wrap_native_command(
            command, exact, request=request, controller=controller,
            verification=verification,
        )
        result = fanout.run_command(wrapped)
        if result.returncode == 71 and b"sandbox_apply: Operation not permitted" in result.stderr:
            pytest.skip("enclosing sandbox denied Seatbelt application; host probe required")
        assert result.returncode == 1, (operation, path, result)
        assert b"Operation not permitted" in result.stderr
    allowed = request.cwd / "allowed.txt"
    command = replace(seat[3], argv=("/bin/sh", "-c", probe, "sh",
                                     "write", str(allowed)))
    result = fanout.run_command(fanout.wrap_native_command(
        command, _boundary(seat, command), request=request, controller=controller,
        verification=verification,
    ))
    assert result.returncode == 0, result.stderr
    assert allowed.read_text() == "probe"
    listing = replace(seat[3], argv=(
        "/bin/sh", "-c", 'root=$1; set -- "$root"/*; [ "$1" = "$root/*" ]',
        "sh", str(controller.root),
    ))
    result = fanout.run_command(fanout.wrap_native_command(
        listing, _boundary(seat, listing), request=request, controller=controller,
        verification=verification,
    ))
    assert result.returncode == 0, "seat listed controller authority directory"
    child = replace(seat[3], argv=(
        "/bin/sh", "-c", '/bin/sh -c \'IFS= read -r x < "$1"\' sh "$1"',
        "sh", str(other / "secret.txt"),
    ))
    result = fanout.run_command(fanout.wrap_native_command(
        child, _boundary(seat, child), request=request, controller=controller,
        verification=verification,
    ))
    assert result.returncode == 1, result.stderr
    assert b"Operation not permitted" in result.stderr
