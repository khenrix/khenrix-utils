"""Controlled-clock production startup; no SSH, Docker, credentials, or provider sends."""
import io
import json
import stat

import pytest

from shared.lib.fanout import container_boundary as boundary, container_coordinator as helper
from shared.lib.fanout.broker import Broker, BrokerPermit
from shared.lib.fanout.controller import read_evidence, write_evidence
from shared.lib.fanout.errors import ProviderRequestError
from shared.lib.fanout.process import ProcessResult, ProcessStatus
from test_fanout_container_runtime import LifecycleHarness, SharedDocker


class Clock:
    now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Process:
    def __init__(self):
        self.returncode = None
        self.stdin, self.stdout = io.BytesIO(), io.BytesIO()

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0

    def wait(self, **_kwargs):
        return self.returncode


@pytest.fixture
def startup(tmp_path, monkeypatch):
    harness = LifecycleHarness(tmp_path)
    spec, *_ = SharedDocker().seat("seat-claude", "c" * 64)
    permit = BrokerPermit("claude", spec.run_id, spec.task_id, spec.target_id, spec.seat_id,
        "initial", "test-model", "disposable-placeholder", 4, 1048576, 8388608, 8192,
        inputs_digest=harness.inputs.digest, upstream_route_digest="3" * 64,
        upstream_origin="synthetic.invalid", upstream_model="test-model", upstream_path="/v1/messages")
    context = write_evidence(harness.controller, "container-coordinator-context", {
        "schema_version": "fanout-container-coordinator-context-v1", "release_source_binding": harness.scope.source,
        "inputs_digest": harness.inputs.digest, "run_id": spec.run_id, "task_id": spec.task_id,
        "target_id": spec.target_id, "seat_id": spec.seat_id, "attempt": 1, "round": 1,
        "request_digest": "4" * 64, "task_packet_sha256": "5" * 64,
        "request": {"run_id": spec.run_id, "task_id": spec.task_id, "target_id": spec.target_id,
            "seat_id": spec.seat_id, "inputs_digest": harness.inputs.digest, "session_id": None,
            "executor_id": "claude", "resume": False}})
    permit_id = harness.scope.reserve(context, spec, harness.ledger, permit, 1)
    sent = []
    def forbidden_send(*_args):
        sent.append(True)
        pytest.fail("startup reached a provider send")
    control = object.__new__(boundary.PinnedSSHSeatControl)
    control.controller, control.spec, control.ledger = harness.controller, spec, harness.ledger
    control.broker = Broker(permit, credential="SYNTHETIC_SECRET_CANARY", send=forbidden_send)
    control.permit_id, control.epoch = permit_id, 1
    control._lease = control._process = control._thread = control._relay_error = None
    control._guest_relay_path = control._socket_at_open = None
    control._bootstrap = lambda: setattr(control, "_guest_relay_path", "/run/fanout/relay.py")
    control._argv = lambda _: ("FORBIDDEN_NATIVE_EXEC_CANARY",)
    clock, process = Clock(), Process()
    class Thread:
        ident = None
        def __init__(self, **_kwargs):
            pass
        def start(self):
            pass
        def is_alive(self):
            return False
    monkeypatch.setattr(boundary, "time", clock)
    monkeypatch.setattr(boundary.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(boundary.threading, "Thread", Thread)
    monkeypatch.setattr(boundary, "runtime_source_digest", lambda *_: harness.scope.source["components"]["runtime"])
    proof = {"path": spec.socket_path, "uid": 65532, "mode": stat.S_IFSOCK | 0o600,
             "device": 4, "inode": 17, "ctime_ns": 12345}
    probes = []
    def probes_from(steps):
        steps = iter(steps)
        def run(_argv, *, data, timeout):
            if control._socket_at_open is not None:
                return ProcessResult(ProcessStatus.EXIT, 0, b"", b"")
            probes.append(timeout)
            seconds, outcome = next(steps)
            clock.now += seconds
            if isinstance(outcome, BaseException):
                raise outcome
            if outcome == "missing":
                return ProcessResult(ProcessStatus.EXIT, 1, b"", b"FileNotFoundError: [Errno 2]")
            return ProcessResult(ProcessStatus.EXIT, 0, json.dumps(proof).encode(), b"")
        monkeypatch.setattr(boundary, "_run_control_command", run)
    def live():
        monkeypatch.setattr(boundary, "PinnedSSHSeatControl", lambda *_args: control)
        return boundary.PinnedDockerTransport.start_live(harness.controller, spec,
            ledger=harness.ledger, broker=control.broker, permit_id=permit_id, epoch=1,
            lifecycle_scope=harness.scope, context_ref=context)
    data = type("Startup", (), {})()
    data.harness, data.control, data.context = harness, control, context
    data.clock, data.process, data.probes = clock, process, probes
    data.probes_from, data.live = probes_from, live
    yield data
    control._socket_at_open = None
    control._relay_error = None
    control.close()
    assert sent == []
    state = harness.ledger._load()
    assert state["requests"] == {} and state["send_claims"] == {}
    entry = state["permits"][permit_id]
    assert entry["used"] == entry["attempts"] == 0 and entry["fenced"] is False
    harness.close()


def _proofs(startup, category):
    controller = startup.harness.controller
    return [read_evidence(controller, category, path.name, path.stem)
            for path in (controller.root / "receipts").glob("*.json")
            if json.loads(path.read_bytes())["category"] == category]


def test_socket_missing_then_ready_uses_only_remaining_readiness_budget(startup):
    startup.probes_from([(4, "missing"), (2, "ready")])
    startup.control.open()
    assert startup.control._socket_at_open.inode == 17
    assert startup.probes == pytest.approx([10, 5.9])


@pytest.mark.parametrize("outcome", ["missing", "ready"])
def test_readiness_rejects_deadline_and_late_success_without_admission(startup, outcome):
    startup.probes_from([(10, outcome)])
    with pytest.raises(ProviderRequestError, match="readiness expired"):
        startup.control.open()
    assert startup.probes == [10]
    assert startup.clock.now == 10
    assert startup.control._process is None and startup.control._lease is None
    assert _proofs(startup, "container-seat-launch") == []
    assert _proofs(startup, "container-coordinator-turn") == []


@pytest.mark.parametrize("failure,category", [
    ("process", "relay_process_exit"), ("worker", "relay_worker_failure"),
])
def test_startup_process_or_worker_failure_is_retained_before_uncertain_settlement(startup, failure, category):
    if failure == "process":
        startup.process.returncode = 1
    else:
        startup.control._relay_error = RuntimeError("SYNTHETIC_SECRET_CANARY worker")
    startup.probes_from([])
    with pytest.raises(ProviderRequestError):
        startup.live()
    failures = _proofs(startup, "container-coordinator-turn-failure")
    assert len(failures) == 1
    proof = failures[0]
    assert proof["category"] == category and proof["primary_type"] == "ProviderRequestError"
    assert proof["context"] == list(startup.context) and proof["request_digest"] == "4" * 64
    assert proof["release_source_binding"] == startup.harness.scope.source
    assert proof["permit_id"] == startup.control.permit_id and proof["permit_epoch"] == 1
    assert proof["ssh_failure"] is None and proof["raw_refs"] == []
    assert startup.harness.scope.blocked
    assert _proofs(startup, "container-coordinator-turn") == []
    receipts = b"".join(path.read_bytes() for path in
                        (startup.harness.controller.root / "receipts").glob("*.json"))
    assert b"CANARY" not in receipts


def test_failure_keeps_genuine_ssh_receipt_but_no_raw_exception_strings(startup):
    startup.probes_from([(10, OSError("SYNTHETIC_SECRET_CANARY argv=model-body"))])
    with pytest.raises(ProviderRequestError):
        startup.live()
    [proof] = _proofs(startup, "container-coordinator-turn-failure")
    ssh = read_evidence(startup.harness.controller, "container-ssh-control-failure", *proof["ssh_failure"])
    assert ssh["category"] == "spawn_failure" and ssh["stderr"] is None
    assert proof["phase"] == "relay_open" and proof["category"] == "readiness_expired"
    assert proof["raw_refs"] == []
    assert proof["request_digest"] == "4" * 64
    assert _proofs(startup, "container-seat-cleanup-failure") == []
    receipts = b"".join(path.read_bytes() for path in
                        (startup.harness.controller.root / "receipts").glob("*.json"))
    assert b"CANARY" not in receipts and b"model-body" not in receipts


def test_cleanup_ssh_failure_cannot_replace_the_original_startup_observation(startup, monkeypatch):
    probes = iter([(1, "missing"), (9, "ready")])
    def run(_argv, *, data, timeout):
        if startup.control._socket_at_open is not None:
            return ProcessResult(ProcessStatus.EXIT, 1, b"", b"cleanup control failed")
        seconds, result = next(probes)
        startup.clock.now += seconds
        if result == "missing":
            return ProcessResult(ProcessStatus.EXIT, 1, b"", b"FileNotFoundError: [Errno 2]")
        spec = startup.control.spec
        proof = {"path": spec.socket_path, "uid": 65532, "mode": stat.S_IFSOCK | 0o600,
                 "device": 4, "inode": 17, "ctime_ns": 12345}
        return ProcessResult(ProcessStatus.EXIT, 0, json.dumps(proof).encode(), b"")
    monkeypatch.setattr(boundary, "_run_control_command", run)
    with pytest.raises(ProviderRequestError, match="readiness expired"):
        startup.live()
    [failure] = _proofs(startup, "container-coordinator-turn-failure")
    original = read_evidence(startup.harness.controller, "container-ssh-control-failure", *failure["ssh_failure"])
    assert original["operation"] == "socket_attestation"
    assert original["stderr"] == "FileNotFoundError: [Errno 2]"
    assert _proofs(startup, "container-seat-cleanup-failure")


def test_unknown_exception_type_and_notes_cannot_become_persisted_diagnostics(startup):
    error = type("SYNTHETIC_TYPE_CANARY", (RuntimeError,), {})("SYNTHETIC_SECRET_CANARY")
    error.add_note("SYNTHETIC_NOTE_CANARY")
    with startup.harness.scope.guard:
        startup.harness.scope.fail(startup.control.spec, None, startup.control, error)
    [proof] = _proofs(startup, "container-coordinator-turn-failure")
    assert proof["primary_type"] == "other" and proof["category"] == "primary_failure"
    assert proof["phase"] == "lifecycle"
    assert _proofs(startup, "container-seat-cleanup-failure") == []
    receipts = b"".join(path.read_bytes() for path in
                        (startup.harness.controller.root / "receipts").glob("*.json"))
    assert b"CANARY" not in receipts


def test_foreign_context_cannot_receive_a_bound_failure_receipt(startup):
    key = startup.control.permit_id, 1
    old = startup.harness.scope.pending[key]
    foreign = write_evidence(startup.harness.controller, "container-coordinator-context", {
        "schema_version": "fanout-container-coordinator-context-v1", "release_source_binding": startup.harness.scope.source,
        "inputs_digest": "0" * 64})
    startup.harness.scope.pending[key] = (old[0], foreign, old[2], old[3])
    error = ProviderRequestError("SYNTHETIC_SECRET_CANARY")
    with startup.harness.scope.guard:
        startup.harness.scope.fail(startup.control.spec, None, startup.control, error)
    assert _proofs(startup, "container-coordinator-turn-failure") == []
    assert startup.harness.scope.blocked
    assert "receipt=unavailable" in error.__notes__[-1]


@pytest.mark.parametrize("failure,category", [
    ("process", "relay_process_exit"), ("worker", "relay_worker_failure"),
])
def test_socket_probe_cannot_admit_a_relay_that_failed_during_the_probe(startup, monkeypatch, failure, category):
    attest = startup.control.attest_socket
    def fails_during_probe(path, **kwargs):
        proof = attest(path, **kwargs)
        if failure == "process":
            startup.process.returncode = 1
        else:
            startup.control._relay_error = RuntimeError("SYNTHETIC_SECRET_CANARY")
        return proof
    monkeypatch.setattr(startup.control, "attest_socket", fails_during_probe)
    startup.probes_from([(1, "ready")])
    with pytest.raises(ProviderRequestError, match="before readiness"):
        startup.control.open()
    assert startup.control._startup_failure == category
