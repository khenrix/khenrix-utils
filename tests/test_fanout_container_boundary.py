"""Controller-sealed Linux seat and exact-session contracts; no provider spend."""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.lib.fanout.broker import Broker, BrokerPermit, BrokerResponse  # noqa: E402
from shared.lib.fanout.artifacts import ArtifactStore  # noqa: E402
from shared.lib.fanout.container_broker import PermitLedger  # noqa: E402
from shared.lib.fanout.container_runtime import (  # noqa: E402
    ContainerHandle, ContainerLauncher, ContainerSpec, GuestSocketAttestation,
    private_volume_names,
)
from shared.lib.fanout.container_vm import VMProbe  # noqa: E402
from shared.lib.fanout.container_transfer import TransferIdentity, snapshot_manifest  # noqa: E402
from shared.lib.fanout.container_transfer_transport import issue_input_source_grant  # noqa: E402
from shared.lib.fanout.controller import (  # noqa: E402
    create_lifecycle_controller, read_evidence, write_evidence,
)
from shared.lib.fanout.errors import ProviderRequestError  # noqa: E402
from shared.lib.fanout.process import ProcessCommand, ProcessResult, ProcessStatus  # noqa: E402
from shared.lib.fanout.providers import (  # noqa: E402
    ProviderRegistry, ProviderRequest, run_provider,
)
import test_fanout_container_runtime as runtime_tests  # noqa: E402
import test_fanout_container_transfer_transport as transfer_transport_tests  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/fanout_providers"
IMAGE = "fanout-seat@sha256:" + "a" * 64
CLI = "b" * 64
CLAUDE_ID = "11111111-1111-4111-8111-111111111111"
SESSION_IDS = {"claude": CLAUDE_ID, "codex": "codex-thread-1",
               "agy": "agy-conversation-1"}


def _private_failure_control(module, tmp_path):
    controller = create_lifecycle_controller(tmp_path / "private-control")
    permit = BrokerPermit("claude", "run-control", "task", "target", "seat", "initial",
                          "test-model", "disposable-placeholder", 1, 1024, 1024, 32)
    ledger = PermitLedger(controller.root / "locks" / "spend", controller)
    control = object.__new__(module.PinnedSSHSeatControl)
    control.controller = controller
    control.broker = Broker(permit, credential="controller-secret", send=lambda *_: None)
    control.ledger = ledger
    control.permit_id = ledger.register(permit, 1)
    control.epoch = 1
    workspace, session = private_volume_names("run-control", "task", "target", "seat")
    control.spec = ContainerSpec("run-control", "task", "target", "seat", IMAGE,
                                 workspace, session, "/run/fanout/seat.sock", CLI)
    control._argv = lambda _: ("ssh", "pinned", "PRIVATE_ARGV_CANARY")
    return control


@pytest.mark.parametrize("kind,stderr,want_text,omission", [
    ("exit1", b"actual bounded stderr", "actual bounded stderr", None),
    ("exit255", b"", "", None),
    ("timeout", b"actual timed out stderr", "actual timed out stderr", None),
    ("spawn", b"ignored", None, "unavailable"),
    ("stdout_budget", b"bounded", "bounded", None),
    ("exit1", b"x" * 8193, None, "stderr_budget"),
    ("exit1", b"\xff", None, "unsafe_stderr"),
    ("exit1", b"controller-secret", None, "unsafe_stderr"),
    ("exit1", b'{"key":"controller\\u002dsecret"}', None, "unsafe_stderr"),
], ids=["exit1", "exit255-empty", "timeout", "spawn", "stdout-budget", "stderr-budget", "non-utf8", "reflection", "escaped-reflection"])
def test_private_ssh_failure_retains_only_bounded_actual_control_fields(
    boundary_module, tmp_path, monkeypatch, kind, stderr, want_text, omission,
):
    control = _private_failure_control(boundary_module, tmp_path)
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if kind == "timeout":
            raise subprocess.TimeoutExpired("EXCEPTION_CMD_CANARY", 30,
                                             output=b"OUTPUT_CANARY", stderr=stderr)
        if kind == "spawn": raise OSError("EXCEPTION_CANARY")
        return subprocess.CompletedProcess(argv, 255 if kind == "exit255" else (0 if kind == "stdout_budget" else 1),
                                           b"x" * 1048577 if kind == "stdout_budget" else b"STDOUT_CANARY", stderr)
    monkeypatch.setattr(boundary_module.subprocess, "run", run)
    try:
        with pytest.raises(ProviderRequestError):
            control._ssh(("trusted",), b"INPUT_CANARY", operation="relay_drain")
    except TypeError: pytest.fail("pinned SSH failure does not retain fixed-operation diagnostics")
    assert len(calls) == 1
    paths = list((control.controller.root / "receipts").glob("*.json"))
    assert len(paths) == 1
    raw = paths[0].read_bytes()
    proof = read_evidence(control.controller, "container-ssh-control-failure", paths[0].name,
                          __import__("hashlib").sha256(raw).hexdigest())
    assert proof["operation"] == "relay_drain" and proof["permit_id"] == control.permit_id
    assert proof["stderr"] == want_text and proof["stderr_omission"] == omission
    assert proof["timeout"] is (kind == "timeout")
    assert proof["returncode"] == (None if kind in {"timeout", "spawn"} else 255 if kind == "exit255" else 0 if kind == "stdout_budget" else 1)
    assert len(proof["source_sha256"]) == len(proof["permit_digest"]) == 64
    assert all(marker not in raw.decode() for marker in ("CANARY", "controller-secret"))


def test_private_ssh_success_unknown_operation_and_capture_failure_are_closed(boundary_module, tmp_path, monkeypatch):
    control = _private_failure_control(boundary_module, tmp_path)
    calls = []
    def run(argv, **kwargs):
        calls.append(True)
        return subprocess.CompletedProcess(argv, 0, b"unchanged success", b"")
    monkeypatch.setattr(boundary_module.subprocess, "run", run)
    try: assert control._ssh(("trusted",), operation="vm_nonce") == b"unchanged success"
    except TypeError: pytest.fail("pinned SSH fixed-operation diagnostic interface is absent")
    with pytest.raises(ProviderRequestError): control._ssh(("trusted",), operation="attacker-command")
    assert len(calls) == 1 and list((control.controller.root / "receipts").glob("*.json")) == []
    monkeypatch.setattr(boundary_module.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, b"", b"safe"))
    writes = []
    def fail_write(*args):
        writes.append(True)
        raise OSError("PRIVATE_WRITER_CANARY")
    monkeypatch.setattr(boundary_module, "write_evidence", fail_write)
    with pytest.raises(ProviderRequestError) as caught: control._ssh(("trusted",), operation="socket_closed_check")
    assert writes == [True] and "PRIVATE_WRITER_CANARY" not in str(caught.value)


@pytest.mark.parametrize("failure", ("writer_base", "malformed_source"))
def test_private_ssh_capture_cannot_bless_malformed_source_or_leak_writer_error(boundary_module, tmp_path, monkeypatch, failure):
    control = _private_failure_control(boundary_module, tmp_path)
    monkeypatch.setattr(boundary_module.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, b"", b"safe"))
    if failure == "writer_base":
        def broken(*_args): raise SystemExit("PRIVATE_WRITER_CANARY")
        monkeypatch.setattr(boundary_module, "write_evidence", broken)
    else:
        monkeypatch.setattr(boundary_module, "runtime_source_digest", lambda _: "MALFORMED_SOURCE_CANARY")
    with pytest.raises(ProviderRequestError) as caught: control._ssh(("trusted",), operation="socket_closed_check")
    assert "CANARY" not in str(caught.value)
    assert list((control.controller.root / "receipts").glob("*.json")) == []


@pytest.fixture
def boundary_module():
    # Resolve inside test bodies so a missing API is RED, not a setup error.
    if importlib.util.find_spec("shared.lib.fanout.container_boundary") is None:
        class MissingBoundary:
            def __getattr__(self, _name):
                assert False, "container boundary API is missing"
        return MissingBoundary()
    return importlib.import_module("shared.lib.fanout.container_boundary")


class FakeTransport:
    def __init__(self, vm_probe, handle, socket, cli_digest, result):
        self.vm_probe = vm_probe
        self.handle = handle
        self.socket = socket
        self.cli_digest = cli_digest
        self.result = result
        self.calls = []

    def attest_vm(self):
        return self.vm_probe

    def attest_container(self):
        return self.handle

    def attest_socket(self):
        return self.socket

    def attest_cli(self, _provider):
        return self.cli_digest

    def attest_image(self):
        return IMAGE

    def run(self, command, _boundary):
        self.calls.append(command)
        return self.result


def _terminal(provider: str, *, session_id: str | None = None) -> ProcessResult:
    if provider == "claude":
        data = json.loads((FIXTURES / "claude-success.json").read_text())
        data["session_id"] = session_id
        stdout = json.dumps(data).encode()
    elif provider == "codex":
        rows = [json.loads(line) for line in
                (FIXTURES / "codex-success.ndjson").read_text().splitlines()]
        if session_id is None:
            rows.pop(0)
        else:
            rows[0]["thread_id"] = session_id
        stdout = b"\n".join(json.dumps(row).encode() for row in rows) + b"\n"
    else:
        rows = [json.loads(line) for line in
                (FIXTURES / "agy-success.json").read_text().splitlines()]
        if session_id is None:
            rows[0].pop("conversation_id")
            rows[1]["step_update"].pop("conversation_id")
            rows[2]["result"].pop("conversation_id")
        else:
            rows[0]["conversation_id"] = session_id
            rows[1]["step_update"]["conversation_id"] = session_id
            rows[2]["result"]["conversation_id"] = session_id
        stdout = b"\n".join(json.dumps(row).encode() for row in rows) + b"\n"
    return ProcessResult(ProcessStatus.EXIT, 0, stdout, b"")


def _seat(module, tmp_path: Path, provider: str, *, controller=None,
          store=None, ledger=None, epoch=1, resume=False,
          session_id=None, terminal_id=None, prompt=None,
          route_digest="a" * 64):
    if controller is None:
        controller = create_lifecycle_controller(tmp_path / "controller")
    if ledger is None:
        ledger = PermitLedger(controller.root / "locks" / "spend", controller)
    workspace, volume = private_volume_names("run-one", "task-one", "target-one",
                                              "seat-one")
    if store is None:
        store = module.SessionStore(controller, "run-one", "task-one",
                                    "target-one", "seat-one", volume)
    profile = ProviderRegistry.default().select(provider, "repo-write", "standard")
    request = ProviderRequest(
        executor_id=provider, prompt=prompt or f"{provider} turn {epoch}", cwd=tmp_path,
        execution_class="repo-write", profile=profile, run_id="run-one",
        task_id="task-one", target_id="target-one", seat_id="seat-one",
        inputs_digest="1" * 64, session_id=session_id, resume=resume,
    )
    vm_probe = VMProbe("fanout-isolated", "unix:///private/dedicated/docker.sock",
                       "2" * 64, "3" * 64, "4" * 64, "5" * 64)
    handle = ContainerHandle("6" * 64, "7" * 64, workspace, volume)
    socket = GuestSocketAttestation("/run/fanout/seat-one.sock", 65532,
                                    stat.S_IFSOCK | 0o600, 1, 2, 3)
    broker_permit = BrokerPermit(
        provider=provider, run_id=request.run_id, task_id=request.task_id,
        target_id=request.target_id, seat_id=request.seat_id,
        route="resume" if resume else "initial", model=profile.requested_model,
        placeholder_key="disposable-placeholder", max_requests=2,
        max_request_bytes=100_000, max_response_bytes=100_000,
        max_output_tokens=1024, inputs_digest=request.inputs_digest,
        session_id=session_id,
        upstream_route_digest=route_digest,
        upstream_origin=("https://eu-aiplatform.googleapis.com"
                         if route_digest else None),
        upstream_model=profile.requested_model if route_digest else None,
        upstream_path="/v1/pinned-route" if route_digest else None,
    )
    permit_id = ledger.register(broker_permit, epoch)
    transport = FakeTransport(vm_probe, handle, socket, CLI,
                              _terminal(provider, session_id=terminal_id))
    spec = ContainerSpec(request.run_id, request.task_id, request.target_id,
                         request.seat_id, IMAGE, workspace, volume, socket.path, CLI)
    name, digest = write_evidence(controller, "container-seat-launch", {
        "spec": {field: getattr(spec, field) for field in spec.__dataclass_fields__},
        "handle": {field: getattr(handle, field) for field in handle.__dataclass_fields__},
        "route": "recover" if resume else "start",
    })
    admission = module.ContainerAdmission(
        broker_permit=broker_permit, permit_id=permit_id, epoch=epoch,
        ledger=ledger, image_digest=IMAGE, cli_digest=CLI,
        socket_attestation=socket, transport=transport, session_store=store,
        handle_evidence_name=name, handle_evidence_digest=digest,
    )
    boundary = module.issue_container_boundary(controller, request, vm_probe,
                                               handle, admission)
    return request, boundary, transport, store, ledger, controller


@pytest.mark.parametrize("changed", (
    "image_digest", "cli_digest", "vm_effective_mounts_sha256",
    "container_inspect_sha256", "workspace_volume", "session_volume",
    "socket_path", "permit_epoch", "profile_digest",
    "upstream_route_digest",
))
def test_changed_boundary_fails_before_model_send(boundary_module, tmp_path, changed):
    request, boundary, transport, *_ = _seat(
        boundary_module, tmp_path, "claude", session_id=CLAUDE_ID,
        terminal_id=CLAUDE_ID,
    )
    old = getattr(boundary, changed)
    tampered = replace(boundary, **{changed: "f" * 64 if isinstance(old, str) else old + 1})
    with pytest.raises(ProviderRequestError):
        boundary_module.run_container_turn(request, tampered)
    assert transport.calls == []


def test_changed_prompt_under_same_boundary_fails_before_model_send(
    boundary_module, tmp_path,
):
    request, boundary, transport, *_ = _seat(
        boundary_module, tmp_path, "claude", session_id=CLAUDE_ID,
        terminal_id=CLAUDE_ID,
    )
    with pytest.raises(ProviderRequestError, match="prompt|association"):
        boundary_module.run_container_turn(
            replace(request, prompt="different task"), boundary)
    assert transport.calls == []


@pytest.mark.parametrize("changes", (
    {"timeout": 30.0},
    {"timeout_override_reason": "reviewed", "timeout_override_review_sha256": "a" * 64},
))
def test_changed_timeout_controls_under_same_boundary_fail_before_send(
    boundary_module, tmp_path, changes,
):
    request, boundary, transport, *_ = _seat(
        boundary_module, tmp_path, "claude", session_id=CLAUDE_ID,
        terminal_id=CLAUDE_ID,
    )
    with pytest.raises(ProviderRequestError, match="association"):
        boundary_module.run_container_turn(replace(request, **changes), boundary)
    assert transport.calls == []


@pytest.mark.parametrize("provider", ("claude", "codex", "agy"))
def test_successful_initial_binds_exact_id_for_cold_resume(boundary_module, tmp_path, provider):
    session_id = SESSION_IDS[provider]
    initial_id = session_id if provider == "claude" else None
    request, boundary, transport, store, ledger, controller = _seat(
        boundary_module, tmp_path, provider, session_id=initial_id,
        terminal_id=session_id,
    )
    result = boundary_module.run_container_turn(request, boundary)
    assert result.valid and result.session_id == session_id
    assert len(transport.calls) == 1
    assert store.resume(provider).session_id == session_id

    # A new store instance proves the binding survived process memory loss.
    ledger.fence_epoch(boundary.permit_id, 1)
    cold_store = boundary_module.SessionStore(controller, "run-one", "task-one",
                                               "target-one", "seat-one",
                                               boundary.session_volume)
    resumed, cold_boundary, cold_transport, *_ = _seat(
        boundary_module, tmp_path, provider, controller=controller,
        store=cold_store, ledger=ledger, epoch=2, resume=True,
        session_id=session_id, terminal_id=session_id,
    )
    resumed_result = boundary_module.run_container_turn(resumed, cold_boundary)
    assert resumed_result.valid and resumed_result.session_id == session_id
    assert len(cold_transport.calls) == 1


def test_cold_resume_refuses_changed_upstream_route(boundary_module, tmp_path):
    initial, boundary, _, _, ledger, controller = _seat(
        boundary_module, tmp_path, "claude", session_id=CLAUDE_ID,
        terminal_id=CLAUDE_ID,
    )
    assert boundary_module.run_container_turn(initial, boundary).valid
    ledger.fence_epoch(boundary.permit_id, 1)
    cold_store = boundary_module.SessionStore(controller, "run-one", "task-one",
                                               "target-one", "seat-one",
                                               boundary.session_volume)
    with pytest.raises(ProviderRequestError, match="route|session"):
        _seat(boundary_module, tmp_path, "claude", controller=controller,
              store=cold_store, ledger=ledger, epoch=2, resume=True,
              session_id=CLAUDE_ID, terminal_id=CLAUDE_ID,
              route_digest="b" * 64)


def test_private_boundary_requires_route_digest(boundary_module, tmp_path):
    with pytest.raises(ProviderRequestError, match="route"):
        _seat(boundary_module, tmp_path, "claude", session_id=CLAUDE_ID,
              terminal_id=CLAUDE_ID, route_digest=None)


def test_live_launch_refuses_null_route_before_host_control(boundary_module, tmp_path):
    controller = create_lifecycle_controller(tmp_path / "controller")
    workspace, volume = private_volume_names("run-one", "task-one", "target-one",
                                              "seat-one")
    spec = ContainerSpec("run-one", "task-one", "target-one", "seat-one",
                         IMAGE, workspace, volume, "/run/fanout/seat-one.sock", CLI)
    permit = BrokerPermit(
        provider="claude", run_id=spec.run_id, task_id=spec.task_id,
        target_id=spec.target_id, seat_id=spec.seat_id, route="initial",
        model="test-model", placeholder_key="disposable-placeholder",
        max_requests=1, max_request_bytes=1024, max_response_bytes=1024,
        max_output_tokens=32,
    )
    ledger = PermitLedger(controller.root / "locks" / "spend", controller)
    permit_id = ledger.register(permit, 1)
    broker = Broker(permit, credential="controller-secret",
                    send=lambda *_: BrokerResponse(200, {}, b""))
    with pytest.raises(ProviderRequestError, match="upstream route"):
        boundary_module.PinnedDockerTransport.start_live(
            controller, spec, ledger=ledger, broker=broker,
            permit_id=permit_id, epoch=1)


@pytest.mark.parametrize("provider", ("claude", "codex", "agy"))
def test_identical_prompt_can_be_a_second_authenticated_resume(
    boundary_module, tmp_path, provider,
):
    session_id = SESSION_IDS[provider]
    initial, boundary, _, store, ledger, controller = _seat(
        boundary_module, tmp_path, provider,
        session_id=session_id if provider == "claude" else None,
        terminal_id=session_id,
    )
    assert boundary_module.run_container_turn(initial, boundary).valid
    ledger.fence_epoch(boundary.permit_id, 1)
    first, first_boundary, _, *_ = _seat(
        boundary_module, tmp_path, provider, controller=controller,
        store=store, ledger=ledger, epoch=2, resume=True,
        session_id=session_id, terminal_id=session_id,
        prompt="identical resumed prompt",
    )
    assert boundary_module.run_container_turn(first, first_boundary).valid
    ledger.fence_epoch(first_boundary.permit_id, 2)
    second, second_boundary, transport, *_ = _seat(
        boundary_module, tmp_path, provider, controller=controller,
        store=store, ledger=ledger, epoch=3, resume=True,
        session_id=session_id, terminal_id=session_id,
        prompt="identical resumed prompt",
    )
    assert boundary_module.run_container_turn(second, second_boundary).valid
    assert len(transport.calls) == 1
    with pytest.raises(ProviderRequestError, match="replay"):
        boundary_module.run_container_turn(second, second_boundary)


def test_settle_rejects_session_id_outside_broker_grammar(boundary_module, tmp_path):
    _, _, _, store, *_ = _seat(boundary_module, tmp_path, "codex")
    turn = "a" * 64
    store.reserve("codex", turn, initial=True, upstream_route_digest="a" * 64)
    with pytest.raises(ProviderRequestError, match="session identity"):
        store.settle("codex", "bad id with spaces", turn, initial=True,
                     upstream_route_digest="a" * 64)
    assert store.read("codex") is None


def test_claude_id_is_durable_before_initial_process(boundary_module, tmp_path):
    request, boundary, transport, store, *_ = _seat(
        boundary_module, tmp_path, "claude", session_id=CLAUDE_ID,
        terminal_id=CLAUDE_ID,
    )
    original_run = transport.run

    def observe_binding(command, sealed_boundary):
        assert store.read("claude").session_id == CLAUDE_ID
        with pytest.raises(ProviderRequestError, match="uncertain"):
            store.resume("claude")
        return original_run(command, sealed_boundary)

    transport.run = observe_binding
    assert boundary_module.run_container_turn(request, boundary).valid


@pytest.mark.parametrize("provider", ("codex", "agy"))
def test_settled_session_refuses_another_initial_before_send(
    boundary_module, tmp_path, provider,
):
    request, boundary, _, store, ledger, controller = _seat(
        boundary_module, tmp_path, provider, terminal_id=SESSION_IDS[provider],
    )
    assert boundary_module.run_container_turn(request, boundary).valid
    ledger.fence_epoch(boundary.permit_id, 1)
    another, new_boundary, transport, *_ = _seat(
        boundary_module, tmp_path, provider, controller=controller,
        store=store, ledger=ledger, epoch=2,
        terminal_id=SESSION_IDS[provider],
    )
    with pytest.raises(ProviderRequestError, match="initial session already bound"):
        boundary_module.run_container_turn(another, new_boundary)
    assert transport.calls == []


def test_transport_flag_cannot_bypass_live_host_lease(boundary_module, tmp_path):
    request, boundary, transport, *_ = _seat(
        boundary_module, tmp_path, "claude", session_id=CLAUDE_ID,
        terminal_id=CLAUDE_ID,
    )
    transport.holds_lease = True
    with boundary_module.HostSessionLease(boundary.controller,
                                          boundary.permit_id,
                                          boundary.permit_epoch):
        with pytest.raises(ProviderRequestError, match="lease"):
            boundary_module.run_container_turn(request, boundary)
    assert transport.calls == []


@pytest.mark.parametrize("provider", ("codex", "agy"))
def test_success_without_observed_id_cannot_resume(boundary_module, tmp_path, provider):
    request, boundary, transport, store, *_ = _seat(
        boundary_module, tmp_path, provider, terminal_id=None,
    )
    result = boundary_module.run_container_turn(request, boundary)
    assert not result.valid
    assert len(transport.calls) == 1
    assert store.read(provider) is None
    with pytest.raises(ProviderRequestError, match="exact session"):
        store.resume(provider)


def test_host_version_match_cannot_override_changed_linux_bytes(boundary_module, tmp_path):
    request, boundary, transport, *_ = _seat(
        boundary_module, tmp_path, "claude", session_id=CLAUDE_ID,
        terminal_id=CLAUDE_ID,
    )
    assert request.profile.cli_version == "2.1.284"
    transport.cli_digest = "e" * 64
    with pytest.raises(ProviderRequestError, match="CLI"):
        boundary_module.run_container_turn(request, boundary)
    assert transport.calls == []


def test_second_host_session_cannot_hold_live_permit(boundary_module, tmp_path):
    _, boundary, _, _, _, controller = _seat(
        boundary_module, tmp_path, "claude", session_id=CLAUDE_ID,
        terminal_id=CLAUDE_ID,
    )
    child = """
import sys
from shared.lib.fanout.controller import LifecycleCapability, resume_lifecycle_controller
from shared.lib.fanout.container_boundary import HostSessionLease
from shared.lib.fanout.errors import ProviderRequestError
owner = resume_lifecycle_controller(sys.argv[1], LifecycleCapability(sys.stdin.read()))
try:
    with HostSessionLease(owner, sys.argv[2], int(sys.argv[3])):
        pass
except ProviderRequestError:
    raise SystemExit(17)
"""

    def other_process():
        return subprocess.run(
            [sys.executable, "-c", child, str(controller.root), boundary.permit_id,
             str(boundary.permit_epoch)],
            input=controller.capability.export_token().encode(),
            cwd=ROOT, capture_output=True, check=False,
        )

    with boundary_module.HostSessionLease(controller, boundary.permit_id,
                                          boundary.permit_epoch):
        denied = other_process()
        assert denied.returncode == 17, denied.stderr.decode()
    allowed = other_process()
    assert allowed.returncode == 0, allowed.stderr.decode()


def test_uncertain_turn_is_not_replayed_after_recovery(boundary_module, tmp_path):
    request, boundary, transport, store, _, controller = _seat(
        boundary_module, tmp_path, "claude", session_id=CLAUDE_ID,
        terminal_id=CLAUDE_ID,
    )
    transport.result = ProcessResult(ProcessStatus.TIMEOUT, None, b"", b"")
    assert not boundary_module.run_container_turn(request, boundary).valid
    cold_store = boundary_module.SessionStore(controller, "run-one", "task-one",
                                               "target-one", "seat-one",
                                               boundary.session_volume)
    cold_boundary = replace(boundary, session_store=cold_store)
    with pytest.raises(ProviderRequestError, match="uncertain|replay"):
        boundary_module.run_container_turn(request, cold_boundary)
    assert len(transport.calls) == 1


def test_uncertain_resume_blocks_a_new_prompt_for_same_seat(boundary_module, tmp_path):
    request, boundary, _, store, ledger, controller = _seat(
        boundary_module, tmp_path, "claude", session_id=CLAUDE_ID,
        terminal_id=CLAUDE_ID,
    )
    assert boundary_module.run_container_turn(request, boundary).valid
    ledger.fence_epoch(boundary.permit_id, 1)
    resumed, resume_boundary, transport, *_ = _seat(
        boundary_module, tmp_path, "claude", controller=controller,
        store=store, ledger=ledger, epoch=2, resume=True,
        session_id=CLAUDE_ID, terminal_id=CLAUDE_ID,
    )
    transport.result = ProcessResult(ProcessStatus.TIMEOUT, None, b"", b"")
    assert not boundary_module.run_container_turn(resumed, resume_boundary).valid
    cold_store = boundary_module.SessionStore(controller, "run-one", "task-one",
                                               "target-one", "seat-one",
                                               boundary.session_volume)
    with pytest.raises(ProviderRequestError, match="uncertain"):
        cold_store.resume("claude")


def test_public_provider_entry_stays_closed_with_container_descriptor(boundary_module, tmp_path):
    request, boundary, transport, *_ = _seat(
        boundary_module, tmp_path, "claude", session_id=CLAUDE_ID,
        terminal_id=CLAUDE_ID,
    )
    with pytest.raises(ProviderRequestError):
        run_provider(replace(request, container_boundary=boundary))
    assert transport.calls == []


def _launched_seat(module, tmp_path, *, before_start=None):
    controller = create_lifecycle_controller(tmp_path / "controller")
    spec = ContainerSpec(
        "run-synthetic", "task-plan", "target-one", "seat-claude", IMAGE,
        runtime_tests.WORKSPACE, runtime_tests.SESSION, runtime_tests.SOCKET, CLI,
    )
    class SeatDocker(runtime_tests.FakeDocker):
        fail_stop = False

        def __call__(self, argv, stdin):
            args = argv[3:]
            if args == ("update", "--restart", "no", runtime_tests.CONTAINER_ID):
                self.calls.append(argv)
                self.inspect["HostConfig"]["RestartPolicy"]["Name"] = "no"
                self.events.append("disable-restart-seat")
                return self._ok(runtime_tests.CONTAINER_ID + "\n", json_output=False)
            if args == ("stop", "--time", "0", runtime_tests.CONTAINER_ID):
                self.calls.append(argv)
                if self.fail_stop:
                    self.events.append("failed-stop-seat")
                    return subprocess.CompletedProcess(argv, 1, b"", b"synthetic stop failure")
                self.inspect["State"]["Status"] = "exited"
                self.inspect["State"]["Running"] = False
                self.events.append("stop-seat")
                return self._ok(runtime_tests.CONTAINER_ID + "\n", json_output=False)
            return super().__call__(argv, stdin)

    fake_docker = SeatDocker(spec)
    if before_start is not None:
        before_start(fake_docker)
    socket_attestor = runtime_tests.FakeAttestor()
    launcher = ContainerLauncher(fake_docker, guest_socket_attestor=socket_attestor)
    vm_probe = VMProbe("fanout-isolated", "unix:///private/dedicated/docker.sock",
                       "2" * 64, "3" * 64, "4" * 64, "5" * 64)
    sent = []

    def process_runner(command):
        sent.append(command)
        return _terminal("claude", session_id=CLAUDE_ID)

    transport = module.PinnedDockerTransport.start(
        controller, spec, launcher=launcher, vm_attestor=lambda: vm_probe,
        socket_attestor=socket_attestor, image_attestor=lambda: IMAGE,
        cli_attestor=lambda _provider: CLI, process_runner=process_runner,
    )
    profile = ProviderRegistry.default().select("claude", "repo-write", "standard")
    request = ProviderRequest(
        executor_id="claude", prompt="synthetic", cwd=tmp_path,
        execution_class="repo-write", profile=profile, run_id=spec.run_id,
        task_id=spec.task_id, target_id=spec.target_id, seat_id=spec.seat_id,
        inputs_digest="1" * 64, session_id=CLAUDE_ID,
        context_sha256="a" * 64, skill_bundle_sha256="b" * 64,
        skill_delivery_sha256="8" * 64,
    )
    broker_permit = BrokerPermit(
        provider="claude", run_id=spec.run_id, task_id=spec.task_id,
        target_id=spec.target_id, seat_id=spec.seat_id, route="initial",
        model=profile.requested_model, placeholder_key="disposable-placeholder",
        max_requests=2, max_request_bytes=100_000,
        max_response_bytes=100_000, max_output_tokens=1024,
        inputs_digest=request.inputs_digest, session_id=CLAUDE_ID,
        upstream_route_digest="a" * 64,
        upstream_origin="https://eu-aiplatform.googleapis.com",
        upstream_model=profile.requested_model,
        upstream_path="/v1/pinned-route",
    )
    ledger = PermitLedger(controller.root / "locks" / "spend", controller)
    permit_id = ledger.register(broker_permit, 1)
    store = module.SessionStore(controller, spec.run_id, spec.task_id,
                                spec.target_id, spec.seat_id, spec.session_volume)
    admission = module.ContainerAdmission(
        broker_permit=broker_permit, permit_id=permit_id, epoch=1,
        ledger=ledger, image_digest=IMAGE, cli_digest=CLI,
        socket_attestation=socket_attestor.proof, transport=transport,
        session_store=store,
        handle_evidence_name=transport.handle_evidence_name,
        handle_evidence_digest=transport.handle_evidence_digest,
    )
    source = controller.root / "quarantine" / "source"
    source.mkdir(mode=0o700)
    (source / "repo").mkdir(mode=0o700)
    (source / "context").mkdir(mode=0o700)
    (source / "repo" / "base.txt").write_text("baseline\n")
    (source / "context" / "full-plan.md").write_text("complete plan\n")
    identity = TransferIdentity(spec.run_id, spec.task_id, spec.target_id,
                                spec.seat_id, request.inputs_digest)
    manifest = snapshot_manifest(source, identity)
    with ArtifactStore(controller.root / "quarantine" / "input-material-store") as artifacts:
        manifest_ref = artifacts.write_json("expected/manifest.json", asdict(manifest))
    info = source.stat()
    material_ref = write_evidence(controller, "container-input-material", {
        "schema_version": "InputMaterialV1", "identity": asdict(identity),
        "source_device": info.st_dev, "source_inode": info.st_ino,
        "original_handle": asdict(transport.handle),
        "workspace_volume": spec.workspace_volume,
        "run_inputs_sha256": request.inputs_digest,
        "task_packet_sha256": "5" * 64,
        "target_baseline_sha256": "6" * 64,
        "workspace_sha256": "7" * 64,
        "skill_delivery_sha256": "8" * 64,
        "dependency_context_sha256": "9" * 64,
        "provider_context_sha256": request.context_sha256,
        "skill_bundle_sha256": request.skill_bundle_sha256,
        "manifest_ref": asdict(manifest_ref),
    })
    transport._transfer_popen, _ = transfer_transport_tests._guest_popen(
        tmp_path, [])
    grant = issue_input_source_grant(
        controller, request, transport, admission, source, *material_ref)
    admission = replace(admission, input_receipt=transport.stage_inputs(request, grant))
    boundary = module.issue_container_boundary(controller, request, vm_probe,
                                               transport.handle, admission)
    return request, boundary, transport, fake_docker, socket_attestor, sent


def test_turn_stops_exact_owned_seat_after_success(boundary_module, tmp_path):
    request, boundary, _, docker, _, sent = _launched_seat(boundary_module, tmp_path)
    assert boundary_module.run_container_turn(request, boundary).valid
    assert len(sent) == 1
    assert docker.inspect["State"]["Running"] is False
    assert docker.events[-2:] == ["disable-restart-seat", "stop-seat"]


def test_successful_close_retains_authenticated_full_stopped_inspect(boundary_module, tmp_path):
    _, boundary, transport, docker, _, _ = _launched_seat(boundary_module, tmp_path)
    transport.close()
    name, digest = transport.stop_evidence
    proof = read_evidence(boundary.controller, "container-seat-stopped", name, digest)
    assert proof["handle"]["container_id"] == transport.handle.container_id
    assert proof["launch_evidence_digest"] == transport.handle_evidence_digest
    from shared.lib.fanout.artifacts import ArtifactRef, ArtifactStore
    with ArtifactStore.open_existing(boundary.controller.root / "quarantine" / "runtime-proofs") as store:
        raw = store.read_bytes(ArtifactRef(**proof["raw_inspect"]))
    assert json.loads(raw)[0]["State"]["Running"] is False
    assert json.loads(raw)[0]["Id"] == docker.inspect["Id"]
    transport.close()
    assert transport.stop_evidence == (name, digest)


def test_running_attestation_retains_exact_raw_validation_observations(boundary_module, tmp_path):
    _, _, transport, docker, _, _ = _launched_seat(boundary_module, tmp_path)
    assert transport.attest_container() == transport.handle
    observations = transport.runtime_observation
    import base64
    raw = next(base64.b64decode(item["stdout"]) for item in observations
               if item["argv"] == ["inspect", "--type", "container", transport.handle.container_id])
    assert json.loads(raw)[0] == docker.inspect


def test_vm_attestation_retains_complete_observed_bytes(boundary_module, tmp_path, monkeypatch):
    import base64
    from scripts import fanout_container_vm_probe as vm_probe
    monkeypatch.setattr(Path, "home", classmethod(lambda _: tmp_path))
    control = _private_failure_control(boundary_module, tmp_path)
    control._host_key = "ssh-ed25519 " + "A" * 44
    colima = tmp_path / ".colima/fanout-isolated/colima.yaml"
    lima = tmp_path / ".colima/_lima/colima-fanout-isolated/lima.yaml"
    colima.parent.mkdir(parents=True)
    lima.parent.mkdir(parents=True)
    colima.write_text("arch: x86_64\nvmType: qemu\nmounts: null\n")
    lima.write_text("mounts: []\n")
    context = {"Name": "colima-fanout-isolated", "Endpoints": {"docker": {
        "Host": f"unix://{tmp_path}/.colima/fanout-isolated/docker.sock"}}}
    def run(argv, **_kwargs):
        output = (json.dumps(context).encode() if "context" in argv else
                  b"27.0\n" if "info" in argv else b'{"status":"Running"}')
        return subprocess.CompletedProcess(argv, 0, output, b"")
    monkeypatch.setattr(boundary_module.subprocess, "run", run)
    monkeypatch.setattr(vm_probe, "_profile_identity", lambda *_: {"synthetic": True})
    monkeypatch.setattr(vm_probe, "_read_binding", lambda *_: {"synthetic": True})
    monkeypatch.setattr(vm_probe, "_require_profile_binding", lambda *_: None)
    control._ssh = lambda argv, data=None, **_kwargs: data if data is not None else b"proc /proc proc rw 0 0\n"
    proof = control.attest_vm()
    raw = control.vm_observation
    assert base64.b64decode(raw["colima"]) == colima.read_bytes()
    assert base64.b64decode(raw["lima"]) == lima.read_bytes()
    assert base64.b64decode(raw["nonce"]) == base64.b64decode(raw["reply"])
    assert raw["probe"] == asdict(proof)
    assert raw["profile_identity"] == {"synthetic": True}
    assert raw["profile_binding"] == {"synthetic": True}


def test_model_docker_exec_uses_sealed_published_repo_workdir(
    boundary_module, tmp_path, monkeypatch,
):
    request, boundary, transport, _, _, _ = _launched_seat(boundary_module, tmp_path)
    wrapped = []

    def bounded(command):
        wrapped.append(command.argv)
        return _terminal("claude", session_id=CLAUDE_ID)

    monkeypatch.setattr(boundary_module, "_run_bounded_command", bounded)
    transport._process_runner = lambda command: transport._docker_exec(
        transport.handle, command, transport._model_workdir)
    result = boundary_module.run_container_turn(request, boundary)
    assert result.valid
    assert wrapped[0][:10] == (
        "docker", "--context", "colima-fanout-isolated", "exec", "-i",
        "--user", "65532:65532", "--workdir", boundary.input_workdir,
        transport.handle.container_id,
    )
    assert boundary.input_workdir.endswith("/published/repo")


def test_fixed_docker_execution_retains_raw_result_and_clears_failed_attempt(
    boundary_module, tmp_path, monkeypatch,
):
    _, boundary, transport, _, _, callback_calls = _launched_seat(boundary_module, tmp_path)
    transport._production_edge = True
    terminal = ProcessResult(ProcessStatus.EXIT, 0, b"raw stdout\r\n", b"raw stderr\n")
    commands = []

    def docker_io(command):
        commands.append(command)
        if len(commands) == 2:
            raise RuntimeError("fixed execution failed")
        return terminal

    monkeypatch.setattr(boundary_module, "_run_bounded_command", docker_io)
    command = ProcessCommand(("claude", "--print"), b"private synthetic input")
    assert transport.run(command, boundary) is terminal
    assert transport.process_result is terminal
    assert transport.process_result.stdout == b"raw stdout\r\n"
    assert transport.process_result.stderr == b"raw stderr\n"
    with pytest.raises(RuntimeError, match="fixed execution failed"):
        transport.run(command, boundary)
    assert transport.process_result is None
    assert callback_calls == []
    assert len(commands) == 2
    assert commands[0].argv[:10] == (
        "docker", "--context", "colima-fanout-isolated", "exec", "-i",
        "--user", "65532:65532", "--workdir", boundary.input_workdir,
        transport.handle.container_id,
    )
    transport.close()


def test_refusal_stops_exact_owned_seat_without_prompt_send(boundary_module, tmp_path):
    request, boundary, _, docker, socket, sent = _launched_seat(boundary_module, tmp_path)
    socket.proof = replace(socket.proof, inode=socket.proof.inode + 1)
    with pytest.raises(ProviderRequestError, match="socket"):
        boundary_module.run_container_turn(request, boundary)
    assert sent == []
    assert docker.inspect["State"]["Running"] is False


def test_process_failure_stops_seat_and_retains_uncertain_turn(
    boundary_module, tmp_path,
):
    request, boundary, transport, docker, _, sent = _launched_seat(
        boundary_module, tmp_path)

    def failed_process(_command):
        raise ProviderRequestError("container process output limit exceeded")

    transport._process_runner = failed_process
    with pytest.raises(ProviderRequestError, match="output limit"):
        boundary_module.run_container_turn(request, boundary)
    assert sent == []
    assert docker.inspect["State"]["Running"] is False
    with pytest.raises(ProviderRequestError, match="uncertain"):
        boundary.session_store.resume("claude")


def test_valid_terminal_is_not_settled_when_exact_stop_fails(
    boundary_module, tmp_path,
):
    request, boundary, _, docker, _, sent = _launched_seat(boundary_module, tmp_path)
    docker.fail_stop = True
    with pytest.raises(ProviderRequestError, match="stop|shutdown"):
        boundary_module.run_container_turn(request, boundary)
    assert len(sent) == 1
    assert docker.inspect["State"]["Running"] is True
    cold = boundary_module.SessionStore(boundary.controller, request.run_id,
                                        request.task_id, request.target_id,
                                        request.seat_id, boundary.session_volume)
    with pytest.raises(ProviderRequestError, match="uncertain"):
        cold.resume("claude")


@pytest.mark.parametrize("mismatch", ("request", "boundary"))
def test_mismatched_turn_cannot_close_another_owned_seat(
    boundary_module, tmp_path, mismatch,
):
    request, boundary, _, docker, _, sent = _launched_seat(boundary_module, tmp_path)
    if mismatch == "request":
        request = replace(request, target_id="different-target")
    else:
        boundary = replace(boundary, target_id="different-target")
    with pytest.raises(ProviderRequestError):
        boundary_module.run_container_turn(request, boundary)
    assert sent == []
    assert docker.inspect["State"]["Running"] is True
    assert "stop-seat" not in docker.events


def test_changed_retry_policy_cannot_close_repo_write_seat(
    boundary_module, tmp_path,
):
    request, boundary, _, docker, _, sent = _launched_seat(boundary_module, tmp_path)
    changed = replace(request, retries=1)
    with pytest.raises(ProviderRequestError):
        boundary_module.run_container_turn(changed, boundary)
    assert sent == []
    assert docker.inspect["State"]["Running"] is True


def test_transient_shutdown_authority_failure_refuses_before_prompt_send(
    boundary_module, tmp_path, monkeypatch,
):
    request, boundary, _, docker, _, sent = _launched_seat(
        boundary_module, tmp_path)
    original = boundary_module.read_evidence
    attempts = 0

    def first_admission_read_fails(controller, category, name, digest):
        nonlocal attempts
        if category == "container-seat-admission":
            attempts += 1
            if attempts == 1:
                raise OSError("transient admission evidence read failure")
        return original(controller, category, name, digest)

    monkeypatch.setattr(boundary_module, "read_evidence",
                        first_admission_read_fails)
    with pytest.raises(ProviderRequestError, match="shutdown authority"):
        boundary_module.run_container_turn(request, boundary)
    assert attempts >= 1
    assert sent == []
    assert docker.inspect["State"]["Running"] is True
    assert "stop-seat" not in docker.events


def test_primary_process_failure_survives_failed_shutdown(
    boundary_module, tmp_path,
):
    request, boundary, transport, docker, _, _ = _launched_seat(
        boundary_module, tmp_path)
    docker.fail_stop = True

    def fail_process(_command):
        raise ProviderRequestError("primary process failure")

    transport._process_runner = fail_process
    with pytest.raises(ProviderRequestError, match="primary process failure") as raised:
        boundary_module.run_container_turn(request, boundary)
    assert any("cleanup" in note.lower() for note in raised.value.__notes__)
    with pytest.raises(ProviderRequestError, match="uncertain"):
        boundary.session_store.resume("claude")
    receipts = boundary.controller.root / "receipts"
    cleanup = [path for path in receipts.glob("*.json") if
               json.loads(path.read_text()).get("category") ==
               "container-seat-cleanup-failure"]
    assert len(cleanup) == 1
    proof = read_evidence(boundary.controller, "container-seat-cleanup-failure",
                          cleanup[0].name, cleanup[0].stem)
    assert proof["container_id"] == boundary.container_id
    assert proof["primary_type"] == "ProviderRequestError"
    assert proof["cleanup_type"] == "ProviderRequestError"


def test_failed_shutdown_after_owned_pre_reservation_refusal_blocks_seat(
    boundary_module, tmp_path,
):
    request, boundary, _, docker, socket, sent = _launched_seat(
        boundary_module, tmp_path)
    docker.fail_stop = True
    socket.proof = replace(socket.proof, inode=socket.proof.inode + 1)
    with pytest.raises(ProviderRequestError, match="guest socket changed") as raised:
        boundary_module.run_container_turn(request, boundary)
    assert sent == []
    assert any("cleanup" in note.lower() for note in raised.value.__notes__)
    with pytest.raises(ProviderRequestError, match="uncertain"):
        boundary.session_store.assert_clear("claude")
    cold = boundary_module.SessionStore(boundary.controller, request.run_id,
                                        request.task_id, request.target_id,
                                        request.seat_id, boundary.session_volume)
    with pytest.raises(ProviderRequestError, match="uncertain"):
        cold.assert_clear("claude")


def test_issue_primary_error_survives_failed_owned_cleanup(
    boundary_module, tmp_path, monkeypatch,
):
    request, boundary, transport, docker, _, _ = _launched_seat(
        boundary_module, tmp_path)
    docker.fail_stop = True
    original = boundary_module.write_evidence

    def fail_admission(controller, category, payload):
        if category == "container-seat-admission":
            raise OSError("primary admission receipt failure")
        return original(controller, category, payload)

    monkeypatch.setattr(boundary_module, "write_evidence", fail_admission)
    with pytest.raises(OSError, match="primary admission receipt failure") as raised:
        boundary_module.issue_container_boundary(
            boundary.controller, request, transport.attest_vm(),
            transport.handle, boundary.admission)
    assert any("cleanup" in note.lower() for note in raised.value.__notes__)
    with pytest.raises(ProviderRequestError, match="uncertain"):
        boundary.session_store.assert_clear("claude")


def test_start_live_primary_error_survives_failed_relay_cleanup(
    boundary_module, tmp_path, monkeypatch,
):
    controller = create_lifecycle_controller(tmp_path / "controller")

    class BrokenControl:
        def __init__(self, *_args):
            pass

        def open(self):
            raise RuntimeError("primary relay-open failure")

        def close(self):
            raise RuntimeError("secondary relay-close failure")

    monkeypatch.setattr(boundary_module, "PinnedSSHSeatControl", BrokenControl)
    permit = BrokerPermit(
        provider="claude", run_id="run-one", task_id="task-one",
        target_id="target-one", seat_id="seat-one", route="initial",
        model="test-model", placeholder_key="disposable-placeholder",
        max_requests=1, max_request_bytes=1024, max_response_bytes=1024,
        max_output_tokens=32, upstream_route_digest="a" * 64,
        upstream_origin="https://eu-aiplatform.googleapis.com",
        upstream_model="test-model", upstream_path="/v1/pinned-route",
    )
    broker = Broker(permit, credential="controller-secret",
                    send=lambda *_: BrokerResponse(200, {}, b""))
    with pytest.raises(RuntimeError, match="primary relay-open failure") as raised:
        boundary_module.PinnedDockerTransport.start_live(
            controller, None, ledger=None, broker=broker, permit_id="a" * 64,
            epoch=1)
    assert any("cleanup" in note.lower() for note in raised.value.__notes__)
    receipts = controller.root / "receipts"
    cleanup = [path for path in receipts.glob("*.json") if
               json.loads(path.read_text()).get("category") ==
               "container-seat-cleanup-failure"]
    assert len(cleanup) == 1
    proof = read_evidence(controller, "container-seat-cleanup-failure",
                          cleanup[0].name, cleanup[0].stem)
    assert proof["phase"] == "relay-start"


def test_close_refuses_to_stop_changed_owner_labels(boundary_module, tmp_path):
    _, _, transport, docker, _, _ = _launched_seat(boundary_module, tmp_path)
    docker.inspect["Config"]["Labels"]["io.khenrix.fanout.run"] = "foreign-run"
    with pytest.raises(ProviderRequestError, match="ownership|identity"):
        transport.close()
    assert docker.inspect["State"]["Running"] is True
    assert "stop-seat" not in docker.events


def test_close_refuses_extra_unissued_owner_label(boundary_module, tmp_path):
    _, _, transport, docker, _, _ = _launched_seat(boundary_module, tmp_path)
    docker.inspect["Config"]["Labels"]["io.khenrix.fanout.extra"] = "unissued"
    with pytest.raises(ProviderRequestError, match="ownership"):
        transport.close()
    assert docker.inspect["State"]["Running"] is True
    assert "stop-seat" not in docker.events


def test_close_preserves_stop_failure_when_relay_close_also_fails(
    boundary_module, tmp_path,
):
    _, _, transport, docker, _, _ = _launched_seat(boundary_module, tmp_path)
    docker.fail_stop = True

    class BrokenRelay:
        def close(self):
            raise RuntimeError("secondary relay close failure")

    transport._control = BrokenRelay()
    with pytest.raises(ProviderRequestError, match="Docker stop operation failed") as raised:
        transport.close()
    assert any("relay" in note.lower() for note in raised.value.__notes__)
    assert docker.inspect["State"]["Running"] is True


def test_transport_retains_relay_control_after_failed_close(
    boundary_module, tmp_path,
):
    _, _, transport, docker, _, _ = _launched_seat(boundary_module, tmp_path)

    class RetryRelay:
        calls = 0

        def close(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("relay remains live")

    relay = RetryRelay()
    transport._control = relay
    with pytest.raises(RuntimeError, match="relay remains live"):
        transport.close()
    assert transport._control is relay
    assert docker.inspect["State"]["Running"] is False
    transport.close()
    assert relay.calls == 2
    assert transport._control is None


def _partial_ssh_control(module, tmp_path, monkeypatch, process):
    class Lease:
        entered = False
        released = False

        def __init__(self, *_args):
            pass

        def __enter__(self):
            type(self).entered = True
            return self

        def __exit__(self, *_args):
            type(self).released = True

    controller = create_lifecycle_controller(tmp_path / "controller")
    control = object.__new__(module.PinnedSSHSeatControl)
    control.controller = controller
    control.spec = SimpleNamespace(seat_id="seat-test", socket_path="/run/fanout/seat-test.sock")
    control.broker = SimpleNamespace(permit=SimpleNamespace(
        provider="claude", model="test-model", max_requests=1,
        max_request_bytes=1024, max_response_bytes=1024))
    control.ledger = object()
    control.permit_id = "a" * 64
    control.epoch = 1
    control._lease = None
    control._process = None
    control._thread = None
    control._relay_error = None
    control._guest_relay_path = None
    control._socket_at_open = None
    control._bootstrap = lambda: setattr(control, "_guest_relay_path", "/run/fanout/relay.py")
    control._argv = lambda _guest: ("ssh", "pinned")
    monkeypatch.setattr(module, "HostSessionLease", Lease)
    monkeypatch.setattr(module.subprocess, "Popen", lambda *_args, **_kwargs: process)
    return control, Lease


def test_relay_thread_start_failure_preserves_primary_and_releases_lease(
    boundary_module, tmp_path, monkeypatch,
):
    class Process:
        terminated = False
        stdin = object()
        stdout = object()

        def poll(self):
            return 0 if self.terminated else None

        def terminate(self):
            self.terminated = True

        def wait(self, *, timeout):
            assert timeout == 3
            return 0

    class UnstartedThread:
        ident = None

        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("primary thread start failure")

        def join(self, *, timeout):
            raise RuntimeError("cannot join thread before it is started")

        def is_alive(self):
            return False

    process = Process()
    control, lease = _partial_ssh_control(boundary_module, tmp_path,
                                           monkeypatch, process)
    monkeypatch.setattr(boundary_module.threading, "Thread", UnstartedThread)
    with pytest.raises(RuntimeError, match="primary thread start failure"):
        control.open()
    assert process.terminated
    assert control._lease is None
    assert lease.entered and lease.released


def test_agy_high_guest_receives_attempt_ceiling_while_permit_keeps_spend_limit(
    boundary_module, tmp_path, monkeypatch,
):
    class Process:
        stdin = object()
        stdout = object()

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, *, timeout):
            return 0

    class UnstartedThread:
        ident = None

        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("stop before relay thread")

        def join(self, *, timeout):
            pass

        def is_alive(self):
            return False

    process = Process()
    control, _ = _partial_ssh_control(boundary_module, tmp_path, monkeypatch, process)
    control.broker.permit = BrokerPermit(
        provider="agy", run_id="run", task_id="task", target_id="target",
        seat_id="seat-test", route="initial", model="gemini-3.8-flash-high",
        placeholder_key="disposable-placeholder", max_requests=32,
        max_request_bytes=1024, max_response_bytes=1024, max_output_tokens=32)
    captured = []
    monkeypatch.setattr(boundary_module, "_as_guest_relay_uid", lambda *argv: (
        captured.append(argv), argv)[1])
    monkeypatch.setattr(boundary_module.threading, "Thread", UnstartedThread)
    with pytest.raises(RuntimeError, match="stop before relay thread"):
        control.open()
    assert captured
    arguments = captured[0]
    assert arguments[arguments.index("--max-requests") + 1] == "34"
    assert control.broker.permit.max_requests == 32


def test_relay_open_preserves_primary_when_process_cleanup_also_fails(
    boundary_module, tmp_path, monkeypatch,
):
    class Process:
        terminated = False
        stdin = object()
        stdout = object()

        def poll(self):
            return 0 if self.terminated else None

        def terminate(self):
            self.terminated = True
            raise OSError("secondary terminate failure")

        def wait(self, *, timeout):
            return 0

    class UnstartedThread:
        ident = None

        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("primary thread start failure")

        def is_alive(self):
            return False

    control, lease = _partial_ssh_control(boundary_module, tmp_path,
                                           monkeypatch, Process())
    monkeypatch.setattr(boundary_module.threading, "Thread", UnstartedThread)
    with pytest.raises(RuntimeError, match="primary thread start failure") as raised:
        control.open()
    assert lease.released
    assert any("cleanup" in note.lower() for note in raised.value.__notes__)
    receipts = control.controller.root / "receipts"
    cleanup = [path for path in receipts.glob("*.json") if
               json.loads(path.read_text()).get("category") ==
               "container-seat-cleanup-failure"]
    assert len(cleanup) == 1
    proof = read_evidence(control.controller, "container-seat-cleanup-failure",
                          cleanup[0].name, cleanup[0].stem)
    assert proof["phase"] == "relay-open"
    assert proof["primary_type"] == "RuntimeError"
    assert proof["cleanup_type"] == "OSError"


def test_live_relay_thread_prevents_successful_close_and_lease_release(
    boundary_module, tmp_path, monkeypatch,
):
    class Process:
        def poll(self):
            return 0

        def wait(self, *, timeout):
            return 0

    control, lease = _partial_ssh_control(boundary_module, tmp_path,
                                           monkeypatch, Process())
    control._lease = lease()
    control._lease.__enter__()
    control._process = Process()
    release = threading.Event()
    thread = threading.Thread(target=lambda: release.wait(10), daemon=True)
    thread.start()
    control._thread = thread
    try:
        with pytest.raises(ProviderRequestError, match="relay|thread"):
            control.close()
        assert thread.is_alive()
        assert control._thread is thread
        assert control._lease is not None
        assert not lease.released
    finally:
        release.set()
        thread.join(timeout=1)
        control.close()
    assert lease.released
    assert control._lease is None


@pytest.mark.parametrize("stream", (1, 2))
def test_host_capture_reaps_child_on_excess_output(boundary_module, tmp_path, stream):
    pid_file = tmp_path / "child.pid"
    script = (
        "import os,sys,time;"
        "open(sys.argv[1],'w').write(str(os.getpid()));"
        "[os.write(int(sys.argv[2]),b'x'*8192) for _ in range(100)];"
        "time.sleep(30)"
    )
    command = ProcessCommand(
        (sys.executable, "-c", script, str(pid_file), str(stream)), b"",
        cwd=tmp_path, timeout=5, slot_root=tmp_path / "slots",
        term_grace=0.01, reap_timeout=1,
    )
    with pytest.raises(ProviderRequestError, match="output limit"):
        boundary_module._run_bounded_command(command, stdout_cap=1024,
                                             stderr_cap=1024)
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_host_capture_timeout_is_bounded_terminal(boundary_module, tmp_path):
    command = ProcessCommand(
        (sys.executable, "-c", "import os,time;os.write(1,b'partial');time.sleep(30)"),
        b"", cwd=tmp_path, timeout=0.1, slot_root=tmp_path / "slots",
        term_grace=0.01, reap_timeout=1,
    )
    result = boundary_module._run_bounded_command(command, stdout_cap=1024,
                                                  stderr_cap=1024)
    assert result.status == ProcessStatus.TIMEOUT
    assert result.stdout == b"partial"
    assert result.timeout is not None and result.timeout.reaped


def test_host_capture_preserves_slot_and_fixed_environment(
    boundary_module, tmp_path, monkeypatch,
):
    observed = []
    real_slot = boundary_module.executor_slot

    @contextmanager
    def recording_slot(**kwargs):
        observed.append(kwargs)
        with real_slot(**kwargs):
            yield

    monkeypatch.setattr(boundary_module, "executor_slot", recording_slot)
    monkeypatch.setenv("TASK5_FAKE_SECRET", "must-not-reach-child")
    command = ProcessCommand(
        (sys.executable, "-c", "import os;print(os.getenv('TASK5_FAKE_SECRET','absent'));"
         "print(os.getenv('LLM_FANOUT_DEPTH'));print(os.getenv('GIT_CONFIG_GLOBAL'))"),
        b"", cwd=tmp_path, timeout=2, slot_cap=2, slot_timeout=0.4,
        slot_root=tmp_path / "slots",
    )
    result = boundary_module._run_bounded_command(command)
    assert result.status == ProcessStatus.EXIT and result.returncode == 0
    assert result.stdout.splitlines() == [b"absent", b"1", os.devnull.encode()]
    assert observed == [{"root": command.slot_root, "cap": 2, "timeout": 0.4}]


def test_selector_construction_failure_reaps_host_client(
    boundary_module, tmp_path, monkeypatch,
):
    pid_file = tmp_path / "client.pid"
    command = ProcessCommand(
        (sys.executable, "-c", "import os,sys,time;"
         "open(sys.argv[1],'w').write(str(os.getpid()));time.sleep(30)",
         str(pid_file)), b"", cwd=tmp_path, timeout=3,
        slot_root=tmp_path / "slots", term_grace=0.01, reap_timeout=1,
    )

    def selector_fails():
        deadline = time.monotonic() + 1
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        raise OSError("synthetic selector failure")

    monkeypatch.setattr(boundary_module.selectors, "DefaultSelector", selector_fails)
    try:
        with pytest.raises(OSError, match="selector failure"):
            boundary_module._run_bounded_command(command)
        pid = int(pid_file.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        if pid_file.exists():
            pid = int(pid_file.read_text())
            try:
                os.killpg(pid, 9)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass


def test_launch_receipt_failure_quarantines_known_owned_seat(
    boundary_module, tmp_path, monkeypatch,
):
    docker = []
    original = boundary_module.write_evidence

    def fail_launch_receipt(controller, category, payload):
        if category == "container-seat-launch":
            raise OSError("synthetic receipt write failure")
        return original(controller, category, payload)

    monkeypatch.setattr(boundary_module, "write_evidence", fail_launch_receipt)
    with pytest.raises(OSError, match="synthetic receipt"):
        _launched_seat(boundary_module, tmp_path,
                       before_start=docker.append)
    assert len(docker) == 1
    assert docker[0].inspect["State"]["Running"] is False


def test_real_launcher_reinspects_running_container_before_spawn(boundary_module, tmp_path):
    request, boundary, _, fake_docker, _, sent = _launched_seat(boundary_module, tmp_path)
    admission_inspects = fake_docker.inspect_count
    assert admission_inspects >= 3
    fake_docker.inspect["HostConfig"]["NetworkMode"] = "bridge"
    with pytest.raises(ProviderRequestError, match="attestation|inspect|network"):
        boundary_module.run_container_turn(request, boundary)
    assert fake_docker.inspect_count > admission_inspects
    assert sent == []


def test_same_metadata_socket_replacement_is_refused_before_spawn(boundary_module, tmp_path):
    request, boundary, _, _, attestor, sent = _launched_seat(boundary_module, tmp_path)
    attestor.proof = replace(attestor.proof, inode=attestor.proof.inode + 1,
                             ctime_ns=attestor.proof.ctime_ns + 1)
    with pytest.raises(ProviderRequestError, match="socket"):
        boundary_module.run_container_turn(request, boundary)
    assert sent == []


def test_unissued_handle_is_not_admitted(boundary_module, tmp_path):
    request, boundary, transport, docker, _, sent = _launched_seat(boundary_module, tmp_path)
    forged = replace(transport.handle, inspect_sha256="e" * 64)
    with pytest.raises(ProviderRequestError, match="handle|provenance"):
        boundary_module.issue_container_boundary(
            boundary.controller, request, transport.attest_vm(), forged,
            boundary.admission,
        )
    assert sent == []
    assert docker.inspect["State"]["Running"] is True


def test_changed_target_issue_cannot_close_original_seat(boundary_module, tmp_path):
    request, boundary, transport, docker, _, sent = _launched_seat(
        boundary_module, tmp_path)
    changed = replace(request, target_id="different-target")
    with pytest.raises(ProviderRequestError):
        boundary_module.issue_container_boundary(
            boundary.controller, changed, transport.attest_vm(),
            transport.handle, boundary.admission)
    assert sent == []
    assert docker.inspect["State"]["Running"] is True
    assert "stop-seat" not in docker.events


def test_codex_explicit_loopback_provider_on_initial_and_resume(boundary_module, tmp_path):
    request, boundary, transport, store, ledger, controller = _seat(
        boundary_module, tmp_path, "codex", terminal_id=SESSION_IDS["codex"],
    )
    assert boundary_module.run_container_turn(request, boundary).valid
    ledger.fence_epoch(boundary.permit_id, 1)
    resumed, resume_boundary, resume_transport, *_ = _seat(
        boundary_module, tmp_path, "codex", controller=controller,
        store=store, ledger=ledger, epoch=2, resume=True,
        session_id=SESSION_IDS["codex"], terminal_id=SESSION_IDS["codex"],
    )
    assert boundary_module.run_container_turn(resumed, resume_boundary).valid
    for observed in (transport.calls[0], resume_transport.calls[0]):
        argv = observed.argv
        assert argv[:3] == ("codex", "exec", "--ignore-user-config")
        assert 'model_provider="fanout"' in argv
        assert any('base_url="http://127.0.0.1:43187/v1"' in arg
                   and 'x-fanout-seat="disposable-placeholder"' in arg
                   and "request_max_retries=0" in arg for arg in argv)
        assert "--ignore-user-config" in argv
        assert "--strict-config" in argv


def test_guest_relay_numeric_uid_drop_avoids_passwd_lookup(boundary_module):
    argv = boundary_module._as_guest_relay_uid("-c", "print(1)")
    assert argv[:4] == ("sudo", "-n", "python3", "-c")
    assert "-u" not in argv
    assert "os.setgid(65532)" in argv[4]
    assert "os.setuid(65532)" in argv[4]


def test_agy_session_settings_are_exact_and_fail_closed(boundary_module, tmp_path):
    root = tmp_path / "session"
    root.mkdir(mode=0o700)
    settings = root / ".gemini/antigravity-cli/settings.json"
    command = (sys.executable, "-c", boundary_module._AGY_SETTINGS_SCRIPT,
               str(root), str(os.getuid()))
    assert subprocess.run(command, capture_output=True, check=False).returncode == 0
    assert settings.read_bytes() == b'{"modelProvider":"gemini"}\n'
    assert stat.S_IMODE(settings.stat().st_mode) == 0o600
    assert subprocess.run(command, capture_output=True, check=False).returncode == 0
    settings.write_bytes(b'{"modelProvider":"other"}\n')
    assert subprocess.run(command, capture_output=True, check=False).returncode != 0
    settings.unlink()
    settings.symlink_to(tmp_path / "other")
    assert subprocess.run(command, capture_output=True, check=False).returncode != 0
