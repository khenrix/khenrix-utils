"""Docker seat admission through a runner that never contacts a daemon."""
from __future__ import annotations

import copy
import ast
import hashlib
import json
import posixpath
import stat
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from contextlib import ExitStack
from types import SimpleNamespace
from itertools import permutations
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.lib.fanout.container_runtime import (  # noqa: E402
    ContainerLauncher,
    ContainerSpec,
    GuestSocketAttestation,
    private_volume_names,
)
from shared.lib.fanout.errors import ProviderRequestError  # noqa: E402


IDENTITY_HASH = "f1c24470349d744a6c514b36b63978f858925f2fb807dff0a06f5557ace5e13d"
WORKSPACE = f"fanout-workspace-{IDENTITY_HASH}"
SESSION = f"fanout-session-{IDENTITY_HASH}"
CONTAINER_ID = "c" * 64
IMAGE_ID = "d" * 64
NONE_NETWORK_ID = "e6178a4978836a65447ef6416854d5c8aead9262b289367524c3f85201f762e7"
NONE_ENDPOINT_ID = "45460ea76d231436574dee94573171126c59c7698c0e8e007c0339fb0a0e485b"
IMAGE = "fanout-seat@sha256:" + "a" * 64
CLI = "b" * 64
SOCKET = "/run/fanout/seat-claude.sock"
DOCKER = ("docker", "--context", "colima-fanout-isolated")
BOUNDARY = "io.khenrix.fanout."


class SharedDocker:
    """Independent complete seat fixtures sharing one deterministic daemon inventory."""

    def __init__(self, *, create_gate=None):
        self.items = {}
        self.members = []
        self.guard = threading.RLock()
        self.create_gate = create_gate
        self.mutations = []

    def seat(self, label, current, *, spec=None):
        workspace, session = private_volume_names("run-synthetic", "task-plan", "target-one", label)
        spec = spec or ContainerSpec("run-synthetic", "task-plan", "target-one", label,
                                    IMAGE, workspace, session, f"/run/fanout/{label}.sock", CLI)
        workspace, session = spec.workspace_volume, spec.session_volume
        names = {"_labels", "_volume_labels", "_volumes", "_mounts", "_host_mounts", "_inspect",
                 "_no_spend_running_inspect", "_stopped_inspect", "FakeDocker"}
        source = ast.parse(Path(__file__).read_text())
        nodes = [node for node in source.body if isinstance(node, ast.Assign)
                 or isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names]
        namespace = {"copy": copy, "json": json, "stat": stat, "subprocess": subprocess,
                     "ContainerSpec": ContainerSpec}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), __file__, "exec"), namespace)
        namespace.update(WORKSPACE=workspace, SESSION=session, SOCKET=spec.socket_path,
                         CONTAINER_ID=current, IMAGE=spec.image_digest,
                         IMAGE_ID=spec.image_digest[7:] if spec.image_digest.startswith("sha256:") else IMAGE_ID,
                         IDENTITY_HASH=workspace.removeprefix("fanout-workspace-"))
        docker = namespace["FakeDocker"](spec)
        self.members.append(docker)

        def run(argv, stdin):
            args = argv[3:]
            if self.create_gate is not None and args[0] == "create":
                self.create_gate.wait(timeout=5)
            with self.guard:
                docker.old = {cid: item for cid, item in self.items.items() if cid != current}
                if args[:2] == ("volume", "inspect"):
                    volume = next((item for member in self.members for item in member.volumes.values()
                                   if item["Name"] == args[2]), None)
                    return docker._ok([volume] if volume else [])
                if args[:2] == ("network", "inspect"):
                    network = copy.deepcopy(docker.none_network)
                    network["Containers"] = {
                        cid: {"Name": item["Name"].removeprefix("/"),
                              "EndpointID": item["NetworkSettings"]["Networks"]["none"]["EndpointID"],
                              "MacAddress": "", "IPv4Address": "", "IPv6Address": ""}
                        for cid, item in self.items.items() if item["State"]["Running"]
                    }
                    return docker._ok([network])
                result = docker(argv, stdin)
                if args[0] == "create":
                    self.items[current] = docker.inspect
                if args[0] in ("update", "stop"):
                    self.mutations.append((label, args[0], args[-1]))
                return result

        socket = GuestSocketAttestation(spec.socket_path, 65532, stat.S_IFSOCK | 0o600, 4, 17, 12345)
        launcher = ContainerLauncher(run, guest_socket_attestor=lambda _: socket)
        launcher._volume_expectations = (spec, copy.deepcopy(docker.original_volumes))
        return spec, launcher, docker, namespace

    def previous(self, seat, container_id):
        item = copy.deepcopy(seat[2].inspect)
        item["Id"] = container_id
        seat[3]["_stopped_inspect"](item)
        self.items[container_id] = item


def _shared_resume_fixture(*, create_gate=None):
    daemon = SharedDocker(create_gate=create_gate)
    first = daemon.seat("seat-claude", "c" * 64)
    second = daemon.seat("seat-codex", "d" * 64)
    daemon.previous(first, "1" * 64)
    daemon.previous(second, "2" * 64)
    return daemon, first, second


class LifecycleHarness:
    """Real journal/ledger/lease/transport ownership; only daemon and relay I/O are synthetic."""

    def __init__(self, tmp_path):
        from shared.lib.fanout import runstate, container_coordinator as helper
        from shared.lib.fanout.controller import create_lifecycle_controller
        from shared.lib.fanout.container_broker import PermitLedger
        class Anchor:
            identity = "synthetic-external-anchor"
            records = {}
            def create(self, key, value):
                assert key not in self.records
                self.records[key] = runstate.AnchorRevision(1, value)
                return self.records[key]
            def read(self, key):
                return self.records[key]
            def compare_and_set(self, key, revision, value):
                assert self.records[key].revision == revision
                self.records[key] = runstate.AnchorRevision(revision + 1, value)
                return self.records[key]
        self.authority = runstate._test_anchor_authority(Anchor())
        self.inputs = runstate.RunInputs(run_id="run-synthetic", compiled_plan_sha256="a" * 64,
            source_sha256="b" * 64, draft_sha256="c" * 64, compiler_sha256="d" * 64,
            parser_sha256="e" * 64, provider_profiles={"claude": "f" * 64},
            skill_manifests={"task-plan": "1" * 64}, repo_baseline_sha256="2" * 64)
        self.journal, self.owner = runstate.RunJournal._create_for_test(
            tmp_path / "run", self.inputs, anchor_store=self.authority)
        self.controller = create_lifecycle_controller(tmp_path / "controller")
        self.ledger = PermitLedger(self.controller.root / "locks/spend", self.controller)
        self.stack = ExitStack()
        self.coordinator = SimpleNamespace(journal=self.journal, owner=self.owner,
            lifecycle_controller=self.controller, _journal_guard=threading.Lock())
        self.scope = helper._coordinator_lifecycle(self.coordinator)
        self.history = {}

    def original_turn(self, seat, provider):
        from dataclasses import asdict
        from shared.lib.fanout import container_boundary as boundary
        from shared.lib.fanout.broker import BrokerPermit
        from shared.lib.fanout.container_runtime import ContainerHandle
        from shared.lib.fanout.container_vm import VMProbe
        from shared.lib.fanout.controller import write_evidence
        from shared.lib.fanout.providers import ProviderRequest, ProviderRegistry
        from test_fanout_container_boundary import FakeTransport, _terminal, SESSION_IDS
        from test_fanout_container_transfer_transport import _complete_original_inputs
        spec, launcher, docker, _ = seat
        if spec.seat_id in self.history:
            return self.history[spec.seat_id]
        profile = ProviderRegistry.default().select(provider, "repo-write", "standard")
        native_id = SESSION_IDS[provider]
        request = ProviderRequest(provider, "original prior turn", cwd=self.controller.root,
            execution_class="repo-write", profile=profile, run_id=spec.run_id,
            task_id=spec.task_id, target_id=spec.target_id, seat_id=spec.seat_id,
            inputs_digest=self.inputs.digest, session_id=native_id if provider == "claude" else None,
            context_sha256="a" * 64, skill_bundle_sha256="b" * 64, skill_delivery_sha256="8" * 64)
        permit = BrokerPermit(provider, spec.run_id, spec.task_id, spec.target_id, spec.seat_id,
            "initial", profile.requested_model, "disposable-placeholder", 4, 1048576, 8388608, 8192,
            inputs_digest=self.inputs.digest, session_id=request.session_id, upstream_route_digest="3" * 64,
            upstream_origin="synthetic.invalid", upstream_model=profile.requested_model, upstream_path="/v1/messages")
        permit_id = self.ledger.register(permit, 1)
        handle = ContainerHandle(("1" if provider == "claude" else "2") * 64, "7" * 64,
                                 spec.workspace_volume, spec.session_volume)
        origin = write_evidence(self.controller, "container-seat-launch", {
            "spec": asdict(spec), "handle": asdict(handle), "route": "start",
            "volumes": copy.deepcopy(docker.original_volumes)})
        socket = launcher._socket_attestor(spec.socket_path)
        vm = VMProbe("fanout-isolated", "unix:///private/dedicated/docker.sock",
                     "2" * 64, "3" * 64, "4" * 64, "5" * 64)
        sessions = boundary.SessionStore(self.controller, spec.run_id, spec.task_id,
                                        spec.target_id, spec.seat_id, spec.session_volume)
        _complete_original_inputs(self.controller, request, spec, handle, permit, self.ledger,
                                  permit_id, 1, origin, vm, socket, sessions)
        transport = FakeTransport(vm, handle, socket, spec.cli_digest, _terminal(provider, session_id=native_id))
        transport.attest_image = lambda: spec.image_digest
        admission = boundary.ContainerAdmission(permit, permit_id, 1, self.ledger,
            spec.image_digest, spec.cli_digest, socket, transport, sessions, *origin)
        sealed = boundary.issue_container_boundary(self.controller, request, vm, handle, admission)
        assert boundary.run_container_turn(request, sealed).valid
        assert sessions.resume(provider).session_id == native_id
        self.ledger.fence_epoch(permit_id, 1)
        resumed = replace(request, resume=True, session_id=native_id, prompt="current resumed turn")
        self.history[spec.seat_id] = resumed
        return resumed

    def start(self, seat, *, process_runner=lambda _: None, epoch=2):
        from shared.lib.fanout.broker import BrokerPermit
        from shared.lib.fanout.container_boundary import PinnedDockerTransport, PinnedSSHSeatControl, HostSessionLease
        from shared.lib.fanout.controller import write_evidence
        spec, launcher, _, _ = seat
        provider = "codex" if spec.seat_id == "seat-codex" else "claude"
        request = self.original_turn(seat, provider)
        permit = BrokerPermit(provider, spec.run_id, spec.task_id, spec.target_id, spec.seat_id,
            "resume", request.profile.requested_model, "disposable-placeholder", 4, 1048576, 8388608, 8192,
            inputs_digest=self.inputs.digest, session_id=request.session_id, upstream_route_digest="3" * 64,
            upstream_origin="synthetic.invalid", upstream_model=request.profile.requested_model, upstream_path="/v1/messages")
        context = write_evidence(self.controller, "container-coordinator-context", {
            "schema_version": "fanout-container-coordinator-context-v1", "release_source_binding": self.scope.source,
            "inputs_digest": self.inputs.digest, "run_id": spec.run_id, "task_id": spec.task_id,
            "target_id": spec.target_id, "seat_id": spec.seat_id,
            "attempt": (epoch - 1) // 3 + 1, "round": (epoch - 1) % 3 + 1,
            "request": {"run_id": spec.run_id, "task_id": spec.task_id, "target_id": spec.target_id,
                "seat_id": spec.seat_id, "inputs_digest": self.inputs.digest, "session_id": permit.session_id,
                "executor_id": provider, "resume": True}})
        permit_id = self.scope.reserve(context, spec, self.ledger, permit, epoch)
        control = object.__new__(PinnedSSHSeatControl)
        control.controller, control.spec, control.ledger = self.controller, spec, self.ledger
        from shared.lib.fanout.broker import Broker
        control.permit_id, control.epoch, control.broker = permit_id, epoch, Broker(
            permit, credential="synthetic-controller-only", send=lambda *_: None)
        control._lease = self.stack.enter_context(HostSessionLease(self.controller, permit_id, epoch))
        control.close = lambda: control._lease.__exit__()
        transport = PinnedDockerTransport.start(self.controller, spec, launcher=launcher,
            vm_attestor=lambda: None, socket_attestor=launcher._socket_attestor,
            image_attestor=lambda: spec.image_digest, cli_attestor=lambda _: spec.cli_digest,
            process_runner=process_runner, recover=True, lifecycle_scope=self.scope,
            context_ref=context, session_control=control, request=request)
        self.stack.callback(transport.close)
        return transport, context

    def close(self):
        try:
            self.stack.close()
        finally:
            self.journal.close()


@pytest.mark.parametrize("changed", [
    "provider", "model", "run_id", "task_id", "target_id", "seat_id",
    "inputs_digest", "session_id", "route", "upstream_route_digest",
    "upstream_origin", "upstream_model", "upstream_path", "permit_id", "epoch", "profile",
])
def test_recovery_requires_authenticated_current_permit_before_allocation(tmp_path, changed):
    from shared.lib.fanout.broker import Broker, BrokerPermit
    from shared.lib.fanout.container_boundary import PinnedDockerTransport
    daemon, seat, _ = _shared_resume_fixture()
    harness = LifecycleHarness(tmp_path)
    try:
        request = harness.original_turn(seat, "claude")
        spec, launcher, _, _ = seat
        with harness.ledger._locked() as state:
            original_permits = copy.deepcopy(state["permits"])
        permit = BrokerPermit("claude", spec.run_id, spec.task_id, spec.target_id, spec.seat_id,
            "resume", request.profile.requested_model, "current-disposable-placeholder",
            4, 1048576, 8388608, 8192, inputs_digest=request.inputs_digest,
            session_id=request.session_id, upstream_route_digest="3" * 64,
            upstream_origin="synthetic.invalid", upstream_model=request.profile.requested_model,
            upstream_path="/v1/messages")
        changes = {"provider": "codex", "model": "different-model", "run_id": "different-run",
            "task_id": "different-task", "target_id": "different-target", "seat_id": "different-seat",
            "inputs_digest": "f" * 64, "session_id": "different-session", "route": "initial",
            "upstream_route_digest": "f" * 64, "upstream_origin": "different.invalid",
            "upstream_model": "different-model", "upstream_path": "/different"}
        if changed in changes:
            permit = replace(permit, **{changed: changes[changed]})
        if changed == "profile":
            request = replace(request, profile=replace(request.profile,
                cli_version="different-version", profile_sha256=None))
        permit_id = harness.ledger.register(permit, 2)
        assert permit_id not in original_permits
        broker = Broker(permit, credential="synthetic-controller-only", send=lambda *_: None)
        control = SimpleNamespace(broker=broker, ledger=harness.ledger,
            permit_id="f" * 64 if changed == "permit_id" else permit_id,
            epoch=3 if changed == "epoch" else 2)
        before = len(seat[2].calls)
        with pytest.raises(ProviderRequestError):
            PinnedDockerTransport.start(harness.controller, spec, launcher=launcher,
                vm_attestor=lambda: None, socket_attestor=launcher._socket_attestor,
                image_attestor=lambda: spec.image_digest, cli_attestor=lambda _: spec.cli_digest,
                process_runner=lambda _: pytest.fail("model must not run"),
                recover=True, request=request, session_control=control)
        assert len(seat[2].calls) == before
        assert harness.ledger.attempt_counts(permit_id, 2) == (0, 0)
        with harness.ledger._locked() as state:
            assert all(state["permits"][key] == value for key, value in original_permits.items())
    finally:
        harness.close()


@pytest.mark.parametrize("changed", ["workspace-missing", "session-missing", "both-missing", "created-at"])
def test_lifecycle_resume_refuses_missing_or_recreated_original_volume_before_prepare(tmp_path, changed):
    daemon, seat, _ = _shared_resume_fixture()
    harness = LifecycleHarness(tmp_path)
    try:
        harness.original_turn(seat, "claude")
        docker = seat[2]
        if changed in {"workspace-missing", "both-missing"}:
            docker.volumes.pop("workspace")
        if changed in {"session-missing", "both-missing"}:
            docker.volumes.pop("session")
        if changed == "created-at":
            docker.volumes["workspace"]["CreatedAt"] = "2026-10-03T00:00:00Z"
        before = len(docker.calls)
        with pytest.raises(ProviderRequestError):
            harness.start(seat, process_runner=lambda _: pytest.fail("model must not run"))
        assert not daemon.mutations
        assert all(call[3] not in {"create", "start", "stop", "update"}
                   and call[3:5] != ("volume", "create") for call in docker.calls[before:])
    finally:
        harness.close()


@pytest.mark.parametrize("provider,fault", (
    *((provider, None) for provider in ("claude", "codex", "agy")),
    *(("claude", fault) for fault in ("missing", "tampered", "provider", "route",
        "known-session", "permit-id", "epoch", "extra", "malformed")),
    ("codex", "filled-unknown-session"), ("agy", "filled-unknown-session"),
))
def test_recovery_authenticates_complete_original_grant_before_allocation(tmp_path, provider, fault):
    from dataclasses import asdict
    from shared.lib.fanout import canonical_json
    from shared.lib.fanout.broker import Broker, BrokerPermit
    from shared.lib.fanout.container_broker import _digest
    from shared.lib.fanout.container_boundary import PinnedDockerTransport
    from shared.lib.fanout.container_transfer import TransferIdentity
    from shared.lib.fanout.container_transfer_transport import _InputClaimJournal
    from shared.lib.fanout.controller import read_evidence, write_evidence
    daemon = SharedDocker()
    seat = daemon.seat("seat-" + provider, "c" * 64)
    daemon.previous(seat, ("1" if provider == "claude" else "2") * 64)
    harness = LifecycleHarness(tmp_path)
    try:
        request = harness.original_turn(seat, provider)
        spec, launcher, docker, _ = seat
        identity = TransferIdentity(spec.run_id, spec.task_id, spec.target_id,
                                    spec.seat_id, request.inputs_digest)
        journal = _InputClaimJournal(harness.controller, identity, spec.workspace_volume)
        original_receipt_ref = journal.completed()
        receipt = read_evidence(harness.controller, "container-input-transfer", *original_receipt_ref)
        original_grant_ref = (receipt["grant_evidence_name"], receipt["grant_evidence_digest"])
        grant = read_evidence(harness.controller, "container-input-grant", *original_grant_ref)
        original = BrokerPermit(**grant["original_permit"])
        assert original.route == "initial"
        assert original.session_id == (request.session_id if provider == "claude" else None)
        assert request.resume and request.session_id is not None
        with harness.ledger._locked() as state:
            original_entries = copy.deepcopy(state["permits"])
        permit = replace(original, route="resume", session_id=request.session_id,
                         placeholder_key="current-disposable-placeholder")
        if fault in {"provider", "known-session", "filled-unknown-session"}:
            if fault == "provider":
                alternate = replace(original, provider="codex", session_id=None,
                                    placeholder_key="alternate-original-placeholder")
                alternate_epoch = 2
            else:
                alternate = replace(original,
                    session_id=("22222222-2222-4222-8222-222222222222"
                                if fault == "known-session" else request.session_id),
                    placeholder_key="alternate-original-placeholder")
                alternate_epoch = 2
            alternate_id = harness.ledger.register(alternate, alternate_epoch)
            harness.ledger.fence_epoch(alternate_id, alternate_epoch)
            grant.update(original_permit=asdict(alternate), permit_digest=_digest(alternate),
                         permit_id=alternate_id, permit_epoch=alternate_epoch)
        elif fault == "missing":
            grant.pop("original_permit")
        elif fault == "tampered":
            grant["original_permit"]["max_requests"] += 1
        elif fault == "permit-id":
            grant["permit_id"] = "f" * 64
        elif fault == "epoch":
            grant["permit_epoch"] = 2
        elif fault == "extra":
            grant["original_permit"]["unissued"] = True
        elif fault == "malformed":
            grant["original_permit"] = "not-an-object"
        permit_id = harness.ledger.register(permit, 3)
        broker = Broker(permit, credential="synthetic-controller-only",
                        send=lambda *_: pytest.fail("provider must not run"))
        control = SimpleNamespace(broker=broker, ledger=harness.ledger, permit_id=permit_id, epoch=3)
        if fault == "route":
            grant.update(original_permit=asdict(permit), permit_digest=_digest(permit),
                         permit_id=permit_id, permit_epoch=3)
        if fault is not None:
            # Sign a new fault artifact and bind its real completed claim; the
            # genuine historical grant/receipt and original ledger stay intact.
            grant_ref = write_evidence(harness.controller, "container-input-grant", grant)
            receipt.update(grant_evidence_name=grant_ref[0], grant_evidence_digest=grant_ref[1])
            receipt_ref = write_evidence(harness.controller, "container-input-transfer", receipt)
            claim = journal._read()
            claim.update(grant_evidence_name=grant_ref[0], grant_evidence_digest=grant_ref[1],
                         receipt_evidence_name=receipt_ref[0], receipt_evidence_digest=receipt_ref[1])
            journal._write(claim)
            confirmation = journal._confirmation_payload(claim)
            journal.confirmation.write_bytes(canonical_json({"payload": confirmation,
                "mac": journal._confirmation_mac(confirmation)}))
            assert journal.completed() == receipt_ref
            assert read_evidence(harness.controller, "container-input-grant", *grant_ref) == grant
            assert read_evidence(harness.controller, "container-input-transfer", *receipt_ref) == receipt
        before = len(docker.calls)
        def recover():
            return PinnedDockerTransport.start(harness.controller, spec, launcher=launcher,
                vm_attestor=lambda: None, socket_attestor=launcher._socket_attestor,
                image_attestor=lambda: spec.image_digest, cli_attestor=lambda _: spec.cli_digest,
                process_runner=lambda _: pytest.fail("model must not run"),
                recover=True, request=request, session_control=control)
        if fault is None:
            transport = recover()
            assert transport.handle.workspace_volume == spec.workspace_volume
            assert transport.handle.session_volume == spec.session_volume
            assert docker.created and docker.inspect["State"]["Running"] is True
            transport.close()
        else:
            with pytest.raises(ProviderRequestError, match="original permit authentication failed") as rejected:
                recover()
            cause = rejected.value.__cause__
            if fault == "missing":
                assert isinstance(cause, KeyError) and cause.args == ("original_permit",)
            elif fault == "permit-id":
                assert isinstance(cause, KeyError) and cause.args == ("f" * 64,)
            else:
                expected = {"tampered": "original initial association", "route": "original initial association",
                    "filled-unknown-session": "original initial association", "provider": "original request association",
                    "known-session": "original request association", "epoch": "original ledger association",
                    "extra": "complete original permit", "malformed": "complete original permit"}
                assert isinstance(cause, ValueError) and str(cause) == expected[fault]
            assert len(docker.calls) == before and not docker.created
            assert not daemon.mutations
        assert harness.ledger.attempt_counts(permit_id, 3) == (0, 0)
        with harness.ledger._locked() as state:
            assert all(state["permits"][key] == value for key, value in original_entries.items())
            assert state["permits"][permit_id]["fenced"] is False
        genuine = read_evidence(harness.controller, "container-input-grant", *original_grant_ref)
        assert genuine["original_permit"] == asdict(original)
        genuine_receipt = read_evidence(harness.controller, "container-input-transfer", *original_receipt_ref)
        assert (genuine_receipt["grant_evidence_name"], genuine_receipt["grant_evidence_digest"]) == original_grant_ref
    finally:
        harness.close()


def test_shared_daemon_later_resume_never_stops_active_sibling(tmp_path):
    daemon, first, second = _shared_resume_fixture()
    harness = LifecycleHarness(tmp_path)
    entered = threading.Event()
    finished = threading.Event()

    def first_turn():
        transport, _ = harness.start(first)
        transport.attest_container()
        entered.set()
        assert finished.wait(5)
        assert first[2].inspect["State"]["Running"] is True
        return transport.attest_container()

    def second_turn():
        assert entered.wait(5)
        try:
            transport, _ = harness.start(second)
            return transport.attest_container()
        finally:
            finished.set()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(first_turn), pool.submit(second_turn)
            a.result(timeout=10)
            b.result(timeout=10)
        assert ("seat-codex", "stop", "c" * 64) not in daemon.mutations
    finally:
        harness.close()


def test_shared_daemon_simultaneous_resumes_attest_both_new_siblings(tmp_path):
    daemon, first, second = _shared_resume_fixture()
    harness = LifecycleHarness(tmp_path)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(harness.start, first), pool.submit(harness.start, second)
            first_transport, second_transport = a.result(timeout=10)[0], b.result(timeout=10)[0]
        assert first[2].inspect["State"]["Running"] is second[2].inspect["State"]["Running"] is True
        assert not any(cid in ("c" * 64, "d" * 64) for _, operation, cid in daemon.mutations if operation == "stop")
        assert first_transport.attest_container() == first_transport.handle
        assert second_transport.attest_container() == second_transport.handle
    finally:
        harness.close()


def test_scoped_model_turns_overlap_and_completed_sibling_stays_stopped(tmp_path, monkeypatch):
    from shared.lib.fanout import container_boundary as boundary
    from shared.lib.fanout.process import ProcessCommand, ProcessResult, ProcessStatus
    daemon, first, second = _shared_resume_fixture()
    harness = LifecycleHarness(tmp_path)
    overlap = threading.Barrier(2)
    completed = threading.Event()
    def model_a(_):
        overlap.wait(5)
        return ProcessResult(ProcessStatus.EXIT, 0, b"synthetic model A", b"")
    def model_b(_):
        overlap.wait(5)
        assert completed.wait(5)
        assert second[2].inspect["State"]["Running"] is True
        return ProcessResult(ProcessStatus.EXIT, 0, b"synthetic model B", b"")
    # This guard-focused test replaces only unrelated full admission setup.
    harness.original_turn(first, "claude")
    harness.original_turn(second, "codex")
    monkeypatch.setattr(boundary, "_check_live", lambda admitted: admitted.transport.attest_container())
    try:
        a, _ = harness.start(first, process_runner=model_a)
        b, _ = harness.start(second, process_runner=model_b)
        command = ProcessCommand(("claude",), b"synthetic prompt")
        def admitted(transport):
            control = transport._control
            return SimpleNamespace(transport=transport, input_workdir="/workspace",
                admission=SimpleNamespace(ledger=control.ledger, permit_id=control.permit_id,
                    epoch=control.epoch, broker_permit=control.broker.permit))
        def turn_a():
            result = a.run(command, admitted(a))
            a.close()
            completed.set()
            return result
        def turn_b():
            result = b.run(command, admitted(b))
            assert b.attest_container() == b.handle
            assert not first[2].inspect["State"]["Running"]
            return result
        with ThreadPoolExecutor(max_workers=2) as pool:
            aa, bb = pool.submit(turn_a), pool.submit(turn_b)
            assert aa.result(10).stdout == b"synthetic model A"
            assert bb.result(10).stdout == b"synthetic model B"
        assert a.handle.container_id in harness.scope.completed
        assert b.handle.container_id in harness.scope.active
    finally:
        harness.close()


@pytest.mark.parametrize("mutation", [None, "missing", "reordered", "extra"])
def test_scoped_saved_runtime_replay_without_docker_or_live_leases(tmp_path, monkeypatch, mutation):
    from dataclasses import asdict
    from shared.lib.fanout import container_coordinator as helper
    daemon, first, second = _shared_resume_fixture()
    harness = LifecycleHarness(tmp_path)
    try:
        a, _ = harness.start(first)
        b, context = harness.start(second)
        a.close()
        b.attest_container()
        snapshot, runtime = b.lifecycle_snapshot, asdict(b.runtime_proof)
        observations = copy.deepcopy(b.runtime_observation)
        if mutation == "missing":
            observations.pop()
        elif mutation == "reordered":
            observations[0], observations[1] = observations[1], observations[0]
        elif mutation == "extra":
            observations.append({"argv": ["unexpected"], "stdout": "eA=="})
        if mutation:
            runtime = asdict(helper._scope_material(harness.controller, observations))
        socket = b.attest_socket()
        harness.close()
        monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: pytest.fail("ambient Docker reached"))
        if mutation:
            with pytest.raises(ProviderRequestError):
                helper._replay_lifecycle_runtime(harness.controller, snapshot, runtime,
                    spec=b.spec, handle=b.handle, context_ref=context, socket=socket)
        else:
            member = helper._replay_lifecycle_runtime(harness.controller, snapshot, runtime,
                spec=b.spec, handle=b.handle, context_ref=context, socket=socket)
            assert member["handle"] == asdict(b.handle)
    finally:
        harness.close()


@pytest.mark.parametrize("mutation", ["unknown", "predecessor-changed", "predecessor-missing",
    "unsafe-peer", "foreign-overlap", "permit-epoch", "lease-lost", "owner", "lock", "member", "opening", "scope-cleared"])
def test_scoped_live_authority_refuses_drift(tmp_path, monkeypatch, mutation):
    from shared.lib.fanout import runstate, container_coordinator as helper
    from shared.lib.fanout.controller import write_evidence
    daemon, first, second = _shared_resume_fixture()
    harness = LifecycleHarness(tmp_path)
    try:
        a, _ = harness.start(first)
        b, _ = harness.start(second)
        a.attest_container()
        with monkeypatch.context() as patch:
            if mutation == "unknown":
                patch.setitem(daemon.items, "9" * 64, {**copy.deepcopy(second[2].inspect), "Id": "9" * 64})
            elif mutation == "predecessor-changed":
                patch.setitem(daemon.items["1" * 64]["Config"], "Env", ["changed=true"])
            elif mutation == "predecessor-missing":
                patch.delitem(daemon.items, "1" * 64)
            elif mutation == "unsafe-peer":
                patch.setitem(second[2].inspect["HostConfig"], "Privileged", True)
            elif mutation == "foreign-overlap":
                foreign = copy.deepcopy(second[2].inspect)
                foreign["Id"], foreign["Config"]["Labels"] = "9" * 64, {}
                patch.setitem(daemon.items, "9" * 64, foreign)
            elif mutation == "permit-epoch":
                patch.setattr(b._control, "epoch", 3)
            elif mutation == "lease-lost":
                patch.setattr(b._control._lease, "_fd", None)
            elif mutation == "owner":
                patch.setattr(harness.coordinator, "owner", runstate.OwnerCapability("invalid-owner-capability" * 2))
            elif mutation == "lock":
                patch.setattr(harness.journal, "_lock_identity", (0, 0))
            elif mutation == "member":
                entry = harness.scope.active[b.handle.container_id]
                member = {**entry["member"], "permit_epoch": 3}
                patch.setitem(entry, "ref", write_evidence(harness.controller, "container-coordinator-member", member))
            elif mutation == "scope-cleared":
                patch.setattr(a, "_lifecycle_scope", None)
            else:
                patch.setattr(harness.scope, "opening", {**harness.scope.opening, "session_id": "0" * 64})
            with pytest.raises(Exception):
                a.attest_container()
        assert first[2].inspect["State"]["Running"] is second[2].inspect["State"]["Running"] is True
    finally:
        harness.close()


def test_same_journal_second_coordinator_is_refused_before_fencing(tmp_path):
    from shared.lib.fanout import container_coordinator as helper
    harness = LifecycleHarness(tmp_path)
    try:
        duplicate = SimpleNamespace(**vars(harness.coordinator))
        with pytest.raises(Exception, match="another container lifecycle owner"):
            helper._coordinator_lifecycle(duplicate)
        assert helper._coordinator_lifecycle(harness.coordinator) is harness.scope
        assert harness.scope.opening is None
    finally:
        harness.close()


def test_cold_journal_lifetime_fences_disposed_resurrected_orphan_not_old_registry(tmp_path):
    from shared.lib.fanout import runstate, container_coordinator as helper
    daemon, first, _ = _shared_resume_fixture()
    harness = LifecycleHarness(tmp_path)
    try:
        a, _ = harness.start(first)
        a.attest_container()
        a.close()
        original_scope = harness.scope
        harness.stack.close()
        harness.journal.close()
        # The old epoch has a real exact stop disposition; Docker later resurrects it.
        first[3]["_no_spend_running_inspect"](first[2].inspect)
        harness.journal = runstate.RunJournal._resume_for_test(
            tmp_path / "run", harness.inputs, harness.owner, anchor_store=harness.authority)
        harness.coordinator = SimpleNamespace(journal=harness.journal, owner=harness.owner,
            lifecycle_controller=harness.controller, _journal_guard=threading.Lock())
        harness.scope = helper._coordinator_lifecycle(harness.coordinator)
        assert harness.scope.session_id != original_scope.session_id and not harness.scope.active
        new = daemon.seat("seat-claude", "8" * 64)
        b, _ = harness.start(new, epoch=3)
        assert b.attest_container() == b.handle
        assert not first[2].inspect["State"]["Running"]
        assert ("seat-claude", "stop", a.handle.container_id) in daemon.mutations
        assert a.handle.container_id not in harness.scope.active
    finally:
        harness.close()


def test_cold_unfenced_same_seat_stays_uncertain_without_launch_or_provider(tmp_path):
    from shared.lib.fanout import runstate, container_coordinator as helper
    from shared.lib.fanout.broker import BrokerRejected
    from shared.lib.fanout.errors import RunStateError
    daemon, first, _ = _shared_resume_fixture()
    harness = LifecycleHarness(tmp_path)
    try:
        a, _ = harness.start(first, process_runner=lambda _: pytest.fail("provider called"))
        a.attest_container()
        for event in ("dispatch-intent", "process-started", "provider-terminal", "artifacts-durable",
                      "publication-intent", "checkpoint-published", "checkpoint-verified"):
            harness.journal.append(event, task_id=first[0].task_id, seat_id=first[0].seat_id,
                attempt=1, round=1, owner=harness.owner)
        for event in ("dispatch-intent", "process-started"):
            harness.journal.append(event, task_id=first[0].task_id, seat_id=first[0].seat_id,
                attempt=1, round=3, owner=harness.owner)
        # Lose the old owner lifetime without falsely publishing a stopped terminal.
        a._control.close()
        harness.stack.pop_all()
        harness.journal.close()
        harness.journal = runstate.RunJournal._resume_for_test(
            tmp_path / "run", harness.inputs, harness.owner, anchor_store=harness.authority)
        harness.coordinator = SimpleNamespace(journal=harness.journal, owner=harness.owner,
            lifecycle_controller=harness.controller, _journal_guard=threading.Lock())
        harness.scope = helper._coordinator_lifecycle(harness.coordinator)
        new = daemon.seat("seat-claude", "8" * 64)
        before = list(daemon.mutations)
        with pytest.raises(BrokerRejected, match="prior seat epoch must be fenced"):
            harness.start(new, epoch=3)
        assert harness.scope.blocked
        assert daemon.mutations == before and not new[2].created
        with harness.ledger._locked() as state:
            assert state["permits"][a._control.permit_id]["fenced"] is False
        assert harness.journal.recover_uncertain(harness.owner) == 1
        assert harness.journal.state.seat_phase(first[0].task_id, first[0].seat_id, 1, 3) == "uncertain-attempt"
        with pytest.raises(RunStateError, match="uncertain"):
            harness.journal.append("dispatch-intent", task_id=first[0].task_id, seat_id=first[0].seat_id,
                attempt=2, round=3, owner=harness.owner)
        # Teardown stops the exact old synthetic seat without fabricating an owner terminal.
        a._close()
    finally:
        harness.close()


@pytest.mark.parametrize("failure", ["registration", "stop", "publication"])
def test_scoped_lifecycle_failure_has_durable_quarantine_and_owner_block(tmp_path, monkeypatch, failure):
    from shared.lib.fanout import container_coordinator as helper, container_boundary as boundary
    daemon, first, _ = _shared_resume_fixture()
    harness = LifecycleHarness(tmp_path)
    try:
        with monkeypatch.context() as patch:
            if failure == "registration":
                original = helper.write_evidence
                def fail_member(controller, category, value):
                    if category == "container-coordinator-member":
                        raise OSError("synthetic registration failed")
                    return original(controller, category, value)
                patch.setattr(helper, "write_evidence", fail_member)
                with pytest.raises(OSError, match="registration failed") as raised:
                    harness.start(first)
                assert first[2].inspect["State"]["Running"] is False
            else:
                a, _ = harness.start(first)
                if failure == "stop":
                    patch.setattr(first[2], "stop_noop", True)
                else:
                    original = boundary.write_evidence
                    def fail_stop(controller, category, value):
                        if category == "container-seat-stopped":
                            raise OSError("synthetic stop publication failed")
                        return original(controller, category, value)
                    patch.setattr(boundary, "write_evidence", fail_stop)
                with pytest.raises((ProviderRequestError, OSError)) as raised:
                    a.close()
                assert a.handle.container_id not in harness.scope.completed
            assert harness.scope.blocked
            assert any("receipt=" in note for note in raised.value.__notes__)
            store = boundary.SessionStore(harness.controller, first[0].run_id, first[0].task_id,
                first[0].target_id, first[0].seat_id, first[0].session_volume)
            assert any(entry == {"provider": "claude", "state": "uncertain"} for entry in store._read()["turns"].values())
            with pytest.raises(Exception, match="unavailable"):
                helper._coordinator_lifecycle(harness.coordinator)
        if failure != "registration":
            # Failed relay ownership is not reused; test teardown stops the exact seat directly.
            harness.stack.pop_all()
            a._close()
    finally:
        harness.close()


def test_same_launcher_scoped_captures_cannot_interleave_save_restore(tmp_path, monkeypatch):
    from dataclasses import asdict
    from shared.lib.fanout import container_coordinator as helper
    daemon, first, _ = _shared_resume_fixture()
    harness = LifecycleHarness(tmp_path)
    entered, release = threading.Event(), threading.Event()
    try:
        a, context = harness.start(first)
        original = a.launcher._run
        def held(*argv):
            if argv[:2] == ("image", "inspect") and not entered.is_set():
                entered.set()
                assert release.wait(5)
            return original(*argv)
        monkeypatch.setattr(a.launcher, "_run", held)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_capture = pool.submit(a.attest_container)
            assert entered.wait(5)
            second_capture = pool.submit(a.attest_container)
            assert not second_capture.done()
            release.set()
            assert first_capture.result(10) == second_capture.result(10) == a.handle
        assert a.launcher._run is held
        helper._replay_lifecycle_runtime(harness.controller, a.lifecycle_snapshot, asdict(a.runtime_proof),
            spec=a.spec, handle=a.handle, context_ref=context, socket=a.attest_socket())
    finally:
        release.set()
        harness.close()


def test_transport_refuses_counterfeit_lifecycle_scope_before_docker(tmp_path):
    from shared.lib.fanout.container_boundary import PinnedDockerTransport
    from shared.lib.fanout.container_runtime import _InventoryScope
    from shared.lib.fanout.controller import create_lifecycle_controller
    daemon = SharedDocker()
    spec, launcher, docker, _ = daemon.seat("seat-claude", "c" * 64)
    controller = create_lifecycle_controller(tmp_path / "controller")
    counterfeit = SimpleNamespace(guard=threading.Lock(), blocked=False,
        prepare=lambda *_: _InventoryScope((), ()), register=lambda *_: None,
        fail=lambda *_: None)
    try:
        with pytest.raises(ProviderRequestError, match="lifecycle"):
            PinnedDockerTransport.start(controller, spec, launcher=launcher,
                vm_attestor=lambda: None, socket_attestor=launcher._socket_attestor,
                image_attestor=lambda: spec.image_digest, cli_attestor=lambda _: spec.cli_digest,
                process_runner=lambda _: pytest.fail("provider called"), recover=True,
                lifecycle_scope=counterfeit, context_ref=("absent", "0" * 64), session_control=object())
        assert not docker.created
    finally:
        if docker.created:
            PinnedDockerTransport._quarantine_handle(spec, launcher._recovery.handle, launcher)


@pytest.mark.parametrize("mutation", ["missing-snapshot", "forged-snapshot", "opening-source", "opening-inputs",
    "subject", "member-missing", "member-spec", "member-handle", "member-epoch", "peer-launch", "peer-context", "peer-stop"])
def test_saved_lifecycle_refuses_incomplete_or_changed_signed_associations(tmp_path, mutation):
    from dataclasses import asdict
    from shared.lib.fanout import container_coordinator as helper
    from shared.lib.fanout.controller import read_evidence, write_evidence
    daemon, first, second = _shared_resume_fixture()
    harness = LifecycleHarness(tmp_path)
    try:
        a, _ = harness.start(first)
        b, context = harness.start(second)
        a.close()
        b.attest_container()
        snapshot_ref, runtime_ref, socket = b.lifecycle_snapshot, asdict(b.runtime_proof), b.attest_socket()
        snapshot = read_evidence(harness.controller, "container-coordinator-snapshot", *snapshot_ref)
        members = helper._read_scope_material(harness.controller, snapshot["members"])
        if mutation == "missing-snapshot":
            snapshot_ref = ("absent.json", "0" * 64)
        elif mutation == "forged-snapshot":
            snapshot_ref = (snapshot_ref[0], "0" * 64)
        else:
            if mutation.startswith("opening-"):
                opening = read_evidence(harness.controller, "container-coordinator-opening", *snapshot["opening"])
                if mutation == "opening-source":
                    opening["release_source_binding"]["runtime_sha256"] = "0" * 64
                else:
                    opening["inputs_digest"] = "0" * 64
                snapshot["opening"] = list(write_evidence(harness.controller, "container-coordinator-opening", opening))
            elif mutation == "subject":
                snapshot["subject"] = members["completed"][0][0]
            elif mutation == "member-missing":
                members["active"] = []
            elif mutation.startswith("member-"):
                member = read_evidence(harness.controller, "container-coordinator-member", *members["active"][0])
                if mutation == "member-spec":
                    member["spec"]["socket_path"] = "/run/fanout/wrong.sock"
                elif mutation == "member-handle":
                    member["handle"]["container_id"] = "9" * 64
                else:
                    member["permit_epoch"] = 3
                ref = list(write_evidence(harness.controller, "container-coordinator-member", member))
                snapshot["subject"], members["active"] = ref, [ref]
            elif mutation in ("peer-launch", "peer-context"):
                member = read_evidence(harness.controller, "container-coordinator-member", *members["completed"][0][0])
                member["launch" if mutation == "peer-launch" else "context"] = ["absent.json", "0" * 64]
                members["completed"][0][0] = list(write_evidence(harness.controller, "container-coordinator-member", member))
            else:
                members["completed"][0][1] = ["absent.json", "0" * 64]
            snapshot["members"] = asdict(helper._scope_material(harness.controller, members))
            snapshot_ref = write_evidence(harness.controller, "container-coordinator-snapshot", snapshot)
        harness.close()
        with pytest.raises(Exception):
            helper._replay_lifecycle_runtime(harness.controller, snapshot_ref, runtime_ref,
                spec=b.spec, handle=b.handle, context_ref=context, socket=socket)
    finally:
        harness.close()


def test_live_relay_open_failure_blocks_pending_owner_with_signed_cleanup(tmp_path, monkeypatch):
    from shared.lib.fanout.broker import Broker, BrokerPermit
    from shared.lib.fanout.container_boundary import PinnedDockerTransport, PinnedSSHSeatControl
    from shared.lib.fanout.controller import read_evidence, write_evidence
    daemon, first, second = _shared_resume_fixture()
    harness = LifecycleHarness(tmp_path)
    try:
        a, context_ref = harness.start(first)
        request = harness.original_turn(second, "codex")
        context = read_evidence(harness.controller, "container-coordinator-context", *context_ref)
        context["seat_id"] = context["request"]["seat_id"] = second[0].seat_id
        context["request"]["executor_id"] = "codex"
        context["request"]["session_id"] = request.session_id
        context_ref = write_evidence(harness.controller, "container-coordinator-context", context)
        spec = second[0]
        permit = BrokerPermit("codex", spec.run_id, spec.task_id, spec.target_id, spec.seat_id,
            "resume", request.profile.requested_model, "disposable-placeholder", 4, 1048576, 8388608, 8192,
            inputs_digest=request.inputs_digest, session_id=request.session_id, upstream_route_digest="3" * 64,
            upstream_origin="synthetic.invalid", upstream_model=request.profile.requested_model, upstream_path="/v1/messages")
        permit_id = harness.scope.reserve(context_ref, second[0], harness.ledger, permit, 2)
        broker = Broker(permit, credential="synthetic-only-credential", send=lambda *_: pytest.fail("provider called"))
        def fail_open(_):
            raise OSError("synthetic relay open failed")
        monkeypatch.setattr(PinnedSSHSeatControl, "open", fail_open)
        with pytest.raises(OSError, match="relay open failed") as raised:
            PinnedDockerTransport.start_live(harness.controller, second[0], ledger=harness.ledger,
                broker=broker, permit_id=permit_id, epoch=2, recover=True,
                lifecycle_scope=harness.scope, context_ref=context_ref, request=request)
        assert harness.scope.blocked
        assert any("receipt=" in note for note in raised.value.__notes__)
        assert not second[2].created
        with harness.ledger._locked() as state:
            assert state["permits"][permit_id]["fenced"] is False
        assert harness.ledger.attempt_counts(permit_id, 2) == (0, 0)
    finally:
        harness.close()


@pytest.mark.parametrize("peers", [1, 5])
def test_scoped_transcript_overhead_is_finite_and_derived_from_peer_count(peers):
    from shared.lib.fanout.container_runtime import RuntimeTranscript, _MAX_RUNTIME_OBSERVATIONS
    observations = [{"argv": ["observation", str(index)], "stdout": ""}
                    for index in range(_MAX_RUNTIME_OBSERVATIONS + 2 * peers)]
    transcript = RuntimeTranscript(observations, active_peers=peers)
    with pytest.raises(ProviderRequestError, match="budget"):
        transcript.append(("overflow",), b"")
    with pytest.raises(ProviderRequestError, match="peer count"):
        RuntimeTranscript(observations, active_peers=6)


@pytest.fixture
def spec() -> ContainerSpec:
    return ContainerSpec(
        run_id="run-synthetic", task_id="task-plan", target_id="target-one",
        seat_id="seat-claude", image_digest=IMAGE, workspace_volume=WORKSPACE,
        session_volume=SESSION, socket_path=SOCKET, cli_digest=CLI,
    )


def _labels(spec: ContainerSpec) -> dict[str, str]:
    return {
        BOUNDARY + "boundary": "container-seat-v1",
        BOUNDARY + "run": spec.run_id,
        BOUNDARY + "task": spec.task_id,
        BOUNDARY + "target": spec.target_id,
        BOUNDARY + "seat": spec.seat_id,
        BOUNDARY + "image_digest": spec.image_digest,
        BOUNDARY + "cli_digest": spec.cli_digest,
        BOUNDARY + "workspace_volume": spec.workspace_volume,
        BOUNDARY + "session_volume": spec.session_volume,
        BOUNDARY + "socket_path": spec.socket_path,
    }


@pytest.mark.parametrize("length", [91, 128])
def test_linux_socket_limit_refuses_before_any_docker_operation(spec, length):
    seat = "s" * length
    workspace, session = private_volume_names(spec.run_id, spec.task_id, spec.target_id, seat)
    changed = replace(spec, seat_id=seat, workspace_volume=workspace, session_volume=session,
                      socket_path=f"/run/fanout/{seat}.sock")
    calls = []
    launcher = ContainerLauncher(lambda argv, data: calls.append(argv),
                                 guest_socket_attestor=lambda _: None)
    with pytest.raises(ProviderRequestError):
        launcher.start(changed)
    assert calls == []


def test_linux_socket_limit_accepts_exact_90_byte_seat(spec):
    seat = "s" * 90
    workspace, session = private_volume_names(spec.run_id, spec.task_id, spec.target_id, seat)
    changed = replace(spec, seat_id=seat, workspace_volume=workspace, session_volume=session,
                      socket_path=f"/run/fanout/{seat}.sock")
    ContainerLauncher._validate_spec(changed)


def _volume_labels(spec: ContainerSpec, kind: str) -> dict[str, str]:
    return {key: value for key, value in _labels(spec).items()
            if key in {BOUNDARY + field for field in ("boundary", "run", "task", "target", "seat")}} | {
                BOUNDARY + "volume_kind": kind,
            }


def _volumes(spec):
    return {kind: {"Name": name, "Driver": "local", "Options": None,
                   "Scope": "local", "Labels": _volume_labels(spec, kind),
                   "CreatedAt": "2026-10-04T12:34:56.123456789Z",
                   "Mountpoint": "/var/lib/docker/volumes/" + name + "/_data"}
            for kind, name in (("workspace", spec.workspace_volume),
                               ("session", spec.session_volume))}


def _mounts(spec: ContainerSpec) -> list[dict[str, object]]:
    return [
        {"Type": "volume", "Name": WORKSPACE,
         "Source": "/var/lib/docker/volumes/" + WORKSPACE + "/_data",
         "Destination": "/workspace", "Driver": "local", "RW": True},
        {"Type": "volume", "Name": SESSION,
         "Source": "/var/lib/docker/volumes/" + SESSION + "/_data",
         "Destination": "/session", "Driver": "local", "RW": True},
        {"Type": "bind", "Name": "", "Source": spec.socket_path,
         "Destination": "/run/fanout/broker.sock", "Driver": "", "RW": False},
    ]


def _host_mounts(spec: ContainerSpec) -> list[dict[str, object]]:
    return [
        {"Type": "volume", "Source": WORKSPACE, "Target": "/workspace"},
        {"Type": "volume", "Source": SESSION, "Target": "/session"},
        {"Type": "bind", "Source": spec.socket_path,
         "Target": "/run/fanout/broker.sock", "ReadOnly": True},
    ]


def _inspect(spec: ContainerSpec) -> dict[str, object]:
    return {
        "Id": CONTAINER_ID,
        "Image": "sha256:" + IMAGE_ID,
        "Name": "/fanout-seat-" + IDENTITY_HASH + "-0000000000000000",
        "Config": {"Image": spec.image_digest, "User": "65532:65532", "Labels": _labels(spec)},
        "HostConfig": {
            "NetworkMode": "none", "ReadonlyRootfs": True,
            "CapDrop": ["ALL"], "CapAdd": None,
            "SecurityOpt": ["no-new-privileges"], "Privileged": False,
            "PidsLimit": 256, "Memory": 4 * 1024**3,
            "MemorySwap": 4 * 1024**3, "NanoCpus": 2_000_000_000,
            "OomKillDisable": False,
            "PidMode": "", "IpcMode": "private", "CgroupnsMode": "private",
            "UTSMode": "",
            "Binds": None, "Mounts": _host_mounts(spec), "VolumesFrom": None,
            "Tmpfs": None,
            "Devices": None, "DeviceRequests": None,
            "PortBindings": None, "PublishAllPorts": False,
            "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
        },
        "Mounts": _mounts(spec),
        "NetworkSettings": {"Networks": {"none": {
            "IPAMConfig": None, "Links": None, "Aliases": None,
            "DriverOpts": None, "GwPriority": 0, "NetworkID": "",
            "EndpointID": "", "Gateway": "", "IPAddress": "",
            "MacAddress": "", "IPPrefixLen": 0, "IPv6Gateway": "",
            "GlobalIPv6Address": "", "GlobalIPv6PrefixLen": 0,
            "DNSNames": None,
        }},
                            "Ports": {}, "SandboxID": "", "SandboxKey": ""},
        "State": {"Status": "created", "Running": False,
                  "Paused": False, "Restarting": False, "Dead": False,
                  "OOMKilled": False},
    }


def _no_spend_running_inspect(item):
    # The nested IDs mirror the observed created-to-running none-network delta.
    item["State"] = {"Status": "running", "Running": True,
                     "Paused": False, "Restarting": False, "Dead": False,
                     "OOMKilled": False}
    item["NetworkSettings"]["SandboxID"] = "e" * 64
    item["NetworkSettings"]["SandboxKey"] = "/var/run/docker/netns/" + "e" * 12
    item["NetworkSettings"]["Networks"]["none"]["NetworkID"] = NONE_NETWORK_ID
    item["NetworkSettings"]["Networks"]["none"]["EndpointID"] = NONE_ENDPOINT_ID
    item["HostConfig"]["OomKillDisable"] = None


def _stopped_inspect(item):
    item["State"] = {"Status": "exited", "Running": False,
                     "Paused": False, "Restarting": False,
                     "Dead": False, "OOMKilled": False}
    item["HostConfig"]["OomKillDisable"] = None
    item["NetworkSettings"]["SandboxID"] = ""
    item["NetworkSettings"]["SandboxKey"] = ""
    item["NetworkSettings"]["Networks"]["none"]["NetworkID"] = NONE_NETWORK_ID
    item["NetworkSettings"]["Networks"]["none"]["EndpointID"] = ""


class FakeDocker:
    def __init__(self, spec: ContainerSpec):
        self.spec = spec
        self.calls: list[tuple[str, ...]] = []
        self.events: list[str] = []
        self.inspect = _inspect(spec)
        self.inspect_count = 0
        self.mutate_second_inspect = None
        self.fail_inspect_at: int | None = None
        self.create_output: bytes | None = None
        self.create_returncode = 0
        self.start_returncode = 0
        self.mutate_after_failed_start = None
        self.stop_noop = False
        self.volume_inspect_count = 0
        self.mutate_volume_on_recheck = None
        self.image_repo_digests = [IMAGE]
        self.info = {"ServerVersion": "29.5.2", "CgroupVersion": "2",
                     "OomKillDisable": False}
        self.info_output: bytes | None = None
        self.info_returncode = 0
        self.none_network = {
            "Name": "none", "Id": NONE_NETWORK_ID,
            "Created": "2026-09-29T23:12:29.04208269+02:00",
            "Scope": "local", "Driver": "null", "EnableIPv4": True,
            "EnableIPv6": False,
            "IPAM": {"Driver": "default", "Options": None, "Config": None},
            "Internal": False, "Attachable": False, "Ingress": False,
            "ConfigFrom": {"Network": ""}, "ConfigOnly": False,
            "Options": {}, "Labels": {},
            "Containers": {CONTAINER_ID: {
                "Name": self.inspect["Name"].removeprefix("/"),
                "EndpointID": NONE_ENDPOINT_ID, "MacAddress": "",
                "IPv4Address": "", "IPv6Address": "",
            }},
            "Status": {"IPAM": {}},
        }
        self.old: dict[str, dict[str, object]] = {}
        self.created = False
        self.volumes = _volumes(spec)
        self.original_volumes = copy.deepcopy(self.volumes)

    def add_orphan(self, *, run_id: str | None = None, seat_id: str | None = None,
                   changed_mount: bool = False, restart: str = "no") -> str:
        old_id = f"{len(self.old) + 14:064x}"
        old = _inspect(self.spec)
        old["Id"] = old_id
        old["State"] = {"Status": "running", "Running": True}
        old["Config"]["Labels"][BOUNDARY + "run"] = run_id or self.spec.run_id
        old["Config"]["Labels"][BOUNDARY + "seat"] = seat_id or self.spec.seat_id
        old["HostConfig"]["RestartPolicy"]["Name"] = restart
        if changed_mount:
            old["Mounts"][0]["Name"] = "foreign-volume"
            old["Config"]["Image"] = "other@sha256:" + "f" * 64
        self.old[old_id] = old
        return old_id

    def __call__(self, argv: tuple[str, ...], stdin: bytes | None) -> subprocess.CompletedProcess[bytes]:
        assert stdin is None
        self.calls.append(argv)
        if argv[:3] != DOCKER:
            raise AssertionError("Docker context alias or ambient Docker invocation")
        args = argv[3:]
        if args[:2] == ("info", "--format"):
            assert args == ("info", "--format", "{{json .}}")
            if self.info_output is not None:
                output = self.info_output
            else:
                output = json.dumps(self.info).encode()
            return subprocess.CompletedProcess((), self.info_returncode, output, b"")
        if args[:2] == ("image", "inspect"):
            assert args[2] == self.spec.image_digest
            return self._ok([{"Id": "sha256:" + IMAGE_ID,
                              "RepoDigests": self.image_repo_digests}])
        if args[:2] == ("network", "inspect"):
            assert args == ("network", "inspect", "none")
            return self._ok([self.none_network] if self.none_network is not None else [])
        if args[:2] == ("volume", "create"):
            name = args[-1]
            self.events.append("volume-create")
            kind = next(kind for kind, item in self.original_volumes.items() if item["Name"] == name)
            if kind not in self.volumes:
                self.volumes[kind] = copy.deepcopy(self.original_volumes[kind])
            return self._ok(name + "\n", json_output=False)
        if args[:2] == ("volume", "inspect"):
            self.volume_inspect_count += 1
            if self.volume_inspect_count == 3 and self.mutate_volume_on_recheck:
                self.mutate_volume_on_recheck(self.volumes)
            item = next((v for v in self.volumes.values() if v["Name"] == args[2]), None)
            return self._ok([item] if item else [])
        if args[:2] == ("container", "ls"):
            self.events.append("inventory")
            filters = [args[index + 1] for index, value in enumerate(args)
                       if value == "--filter"]
            assert all(value.startswith("label=") for value in filters)

            def matches(item):
                labels = item["Config"]["Labels"]
                for value in filters:
                    key, has_value, wanted = value.removeprefix("label=").partition("=")
                    if key not in labels or has_value and labels[key] != wanted:
                        return False
                return True

            inventory = list(self.old.items())
            if self.created:
                inventory.append((CONTAINER_ID, self.inspect))
            return self._ok("".join(old_id + "\n" for old_id, item in inventory
                                    if matches(item)), json_output=False)
        if args[:2] == ("inspect", "--type"):
            assert args[2] == "container"
            item = self.old.get(args[3])
            if item is None:
                if args[3] not in (CONTAINER_ID, self.inspect["Name"].removeprefix("/")):
                    return subprocess.CompletedProcess((), 1, b"", b"container not found")
                if args[3] == CONTAINER_ID:
                    self.inspect_count += 1
                    if self.inspect_count == self.fail_inspect_at:
                        return subprocess.CompletedProcess((), 1, b"", b"inspect unavailable")
                    if self.inspect_count == 2 and self.mutate_second_inspect:
                        self.mutate_second_inspect(self.inspect)
                item = self.inspect
            return self._ok([copy.deepcopy(item)])
        if args[0] == "create":
            self.events.append("create")
            self.created = True
            self.inspect["Name"] = "/" + args[args.index("--name") + 1]
            self.none_network["Containers"][CONTAINER_ID]["Name"] = self.inspect["Name"][1:]
            if self.create_output is not None or self.create_returncode:
                return subprocess.CompletedProcess(
                    (), self.create_returncode,
                    self.create_output if self.create_output is not None else b"", b"create failed")
            return self._ok(CONTAINER_ID + "\n", json_output=False)
        if args[:2] == ("stop", "--time"):
            old_id = args[-1]
            item = self.inspect if old_id == CONTAINER_ID else self.old[old_id]
            if not self.stop_noop:
                _stopped_inspect(item)
            self.events.append("stop-new" if old_id == CONTAINER_ID else "stop-orphan")
            return self._ok(old_id + "\n", json_output=False)
        if args[:2] == ("update", "--restart"):
            assert args[2] == "no"
            old_id = args[3]
            item = self.inspect if old_id == CONTAINER_ID else self.old[old_id]
            item["HostConfig"]["RestartPolicy"]["Name"] = "no"
            self.events.append("disable-restart-new" if old_id == CONTAINER_ID else "disable-restart")
            return self._ok(old_id + "\n", json_output=False)
        if args[0] == "start":
            self.events.append("start")
            _no_spend_running_inspect(self.inspect)
            if self.start_returncode:
                if self.mutate_after_failed_start:
                    self.mutate_after_failed_start(self.inspect)
                return subprocess.CompletedProcess((), self.start_returncode, b"", b"start failed")
            return self._ok(CONTAINER_ID + "\n", json_output=False)
        raise AssertionError(f"unexpected docker operation: {args}")

    @staticmethod
    def _ok(value, *, json_output: bool = True) -> subprocess.CompletedProcess[bytes]:
        data = json.dumps(value).encode() if json_output else value.encode()
        return subprocess.CompletedProcess((), 0, data, b"")


class FakeAttestor:
    def __init__(self):
        self.calls: list[str] = []
        self.proof = GuestSocketAttestation(SOCKET, 65532, stat.S_IFSOCK | 0o600, 4, 17, 12345)
        self.second_proof: GuestSocketAttestation | None = None

    def __call__(self, path: str) -> GuestSocketAttestation:
        self.calls.append(path)
        return self.second_proof if len(self.calls) > 1 and self.second_proof else self.proof


@pytest.fixture
def fake_docker(spec):
    return FakeDocker(spec)


@pytest.fixture
def attestor():
    return FakeAttestor()


def _launcher(fake_docker, attestor):
    launcher = ContainerLauncher(fake_docker, guest_socket_attestor=attestor)
    launcher._volume_expectations = (fake_docker.spec, copy.deepcopy(fake_docker.original_volumes))
    return launcher


@pytest.mark.parametrize("missing", [("workspace",), ("session",), ("workspace", "session")])
def test_authenticated_recovery_never_recreates_missing_original_volumes(spec, fake_docker, attestor, missing):
    for kind in missing:
        fake_docker.volumes.pop(kind)
    with pytest.raises(ProviderRequestError):
        _launcher(fake_docker, attestor).recover(spec)
    assert not any(argv[3:5] == ("volume", "create") or argv[3] in ("create", "start")
                   for argv in fake_docker.calls)


def test_same_name_volume_creation_metadata_change_refuses_before_recovery(spec, fake_docker, attestor):
    fake_docker.volumes["workspace"]["CreatedAt"] = "2026-10-05T12:34:56Z"
    with pytest.raises(ProviderRequestError):
        _launcher(fake_docker, attestor).recover(spec)
    assert not any(argv[3] in ("create", "start") for argv in fake_docker.calls)


def test_recovery_without_original_volume_authority_refuses_before_create(spec, fake_docker, attestor):
    with pytest.raises(ProviderRequestError):
        ContainerLauncher(fake_docker, guest_socket_attestor=attestor).recover(spec)
    assert not any(argv[3:5] == ("volume", "create") or argv[3] in ("create", "start")
                   for argv in fake_docker.calls)


def _has_start(fake_docker) -> bool:
    return any(argv[3:4] == ("start",) for argv in fake_docker.calls)


def _foreign_orphan(fake_docker, spec, *, conflict=None):
    old_id = fake_docker.add_orphan(run_id="foreign-run", seat_id="foreign-seat")
    old = fake_docker.old[old_id]
    labels = old["Config"]["Labels"]
    labels.update({
        BOUNDARY + "workspace_volume": "foreign-workspace",
        BOUNDARY + "session_volume": "foreign-session",
        BOUNDARY + "socket_path": "/run/fanout/foreign-seat.sock",
    })
    for mount, host_mount, name in zip(old["Mounts"][:2],
                                        old["HostConfig"]["Mounts"][:2],
                                        ("foreign-workspace", "foreign-session")):
        mount["Name"] = name
        mount["Source"] = f"/var/lib/docker/volumes/{name}/_data"
        host_mount["Source"] = name
    old["Mounts"][2]["Source"] = "/run/fanout/foreign-seat.sock"
    old["HostConfig"]["Mounts"][2]["Source"] = "/run/fanout/foreign-seat.sock"
    if conflict == "seat":
        labels[BOUNDARY + "seat"] = spec.seat_id
    elif conflict == "workspace":
        old["Mounts"][0]["Name"] = spec.workspace_volume
    elif conflict == "session":
        old["HostConfig"]["Mounts"][1]["Source"] = spec.session_volume
    elif conflict == "socket":
        old["Mounts"][2]["Source"] = spec.socket_path
    else:
        assert conflict is None
    return old_id


def _foreign_bind(fake_docker, spec, source):
    old_id = _foreign_orphan(fake_docker, spec)
    old = fake_docker.old[old_id]
    old["Mounts"].append({"Type": "bind", "Source": source,
                          "Destination": "/borrowed", "RW": True})
    old["HostConfig"]["Mounts"].append({"Type": "bind", "Source": source,
                                          "Target": "/borrowed"})
    return old_id


def test_private_volume_names_are_bound_to_all_four_identity_fields():
    assert private_volume_names("run-synthetic", "task-plan", "target-one", "seat-claude") == (WORKSPACE, SESSION)
    assert private_volume_names("run-synthetic", "task-plan", "target-two", "seat-claude") != (WORKSPACE, SESSION)


def test_start_uses_explicit_context_and_fixed_seat_constraints(spec, fake_docker, attestor):
    handle = _launcher(fake_docker, attestor).start(spec)
    assert handle.container_id == CONTAINER_ID
    assert handle.workspace_volume == WORKSPACE
    assert handle.session_volume == SESSION
    assert len(handle.inspect_sha256) == 64
    assert attestor.calls == [SOCKET, SOCKET]
    assert fake_docker.inspect_count == 2
    create = next(argv for argv in fake_docker.calls if argv[3:4] == ("create",))
    assert create[:3] == DOCKER
    assert create[-1] == IMAGE
    for flag, value in (("--network", "none"), ("--ipc", "private"),
                        ("--cgroupns", "private"),
                        ("--cap-drop", "ALL"),
                        ("--security-opt", "no-new-privileges"),
                        ("--pids-limit", "256"), ("--memory", "4g"),
                        ("--memory-swap", "4g"), ("--cpus", "2"),
                        ("--user", "65532:65532"), ("--pull", "never")):
        assert create[create.index(flag) + 1] == value
    assert "--read-only" in create
    assert "--pid" not in create
    assert "--uts" not in create
    assert "--privileged" not in create
    mounts = [create[index + 1] for index, arg in enumerate(create) if arg == "--mount"]
    assert mounts == [
        f"type=volume,src={WORKSPACE},dst=/workspace",
        f"type=volume,src={SESSION},dst=/session",
        f"type=bind,src={SOCKET},dst=/run/fanout/broker.sock,readonly",
    ]
    assert fake_docker.events.index("volume-create") < fake_docker.events.index("create") < fake_docker.events.index("start")


def _workspace_readonly_docker(spec):
    readonly = replace(spec, workspace_readonly=True)
    docker = FakeDocker(readonly)
    docker.inspect["HostConfig"]["Mounts"][0]["ReadOnly"] = True
    docker.inspect["Mounts"][0]["RW"] = False
    return readonly, docker


def test_workspace_readonly_create_and_running_attestation(spec, attestor):
    readonly, docker = _workspace_readonly_docker(spec)
    launcher = _launcher(docker, attestor)
    handle = launcher.start(readonly)
    assert launcher.attest_running(readonly, handle) == handle
    create = next(argv for argv in docker.calls if argv[3] == "create")
    mounts = [create[index + 1] for index, arg in enumerate(create) if arg == "--mount"]
    assert mounts == [
        f"type=volume,src={WORKSPACE},dst=/workspace,readonly",
        f"type=volume,src={SESSION},dst=/session",
        f"type=bind,src={SOCKET},dst=/run/fanout/broker.sock,readonly",
    ]
    assert ContainerLauncher._mount_args(spec)[1] == f"type=volume,src={WORKSPACE},dst=/workspace"
    assert "--read-only" in create
    assert create[create.index("--network") + 1] == "none"
    with pytest.raises(ProviderRequestError, match="launch association"):
        launcher.attest_running(spec, handle)


@pytest.mark.parametrize("view", ["host", "inventory"])
@pytest.mark.parametrize("phase", ["created", "running"])
def test_workspace_readonly_refuses_writable_mount_substitution(spec, attestor, view, phase):
    readonly, docker = _workspace_readonly_docker(spec)
    launcher = _launcher(docker, attestor)
    handle = launcher.start(readonly) if phase == "running" else None
    if view == "host":
        docker.inspect["HostConfig"]["Mounts"][0]["ReadOnly"] = False
    else:
        docker.inspect["Mounts"][0]["RW"] = True
    with pytest.raises(ProviderRequestError, match="mount"):
        if phase == "created":
            launcher.start(readonly)
        else:
            launcher.attest_running(readonly, handle)
    assert _has_start(docker) is (phase == "running")


@pytest.mark.parametrize("view,index,value", [
    ("host", 1, True), ("inventory", 1, False),
    ("host", 2, False), ("inventory", 2, True),
])
def test_workspace_readonly_preserves_session_and_socket_mount_policy(spec, attestor, view, index, value):
    readonly, docker = _workspace_readonly_docker(spec)
    if view == "host":
        docker.inspect["HostConfig"]["Mounts"][index]["ReadOnly"] = value
    else:
        docker.inspect["Mounts"][index]["RW"] = value
    with pytest.raises(ProviderRequestError, match="mount"):
        _launcher(docker, attestor).start(readonly)
    assert not _has_start(docker)


@pytest.mark.parametrize("value", [None, 0, 1, "true"])
def test_workspace_readonly_requires_a_bool_before_docker(spec, fake_docker, attestor, value):
    invalid = replace(spec, workspace_readonly=value)
    with pytest.raises(ProviderRequestError, match="workspace mount policy"):
        _launcher(fake_docker, attestor).start(invalid)
    assert fake_docker.calls == []


def test_workspace_readonly_sealed_launch_and_stop_reopen_without_policy_substitution(spec, attestor, tmp_path):
    from shared.lib.fanout.container_boundary import PinnedDockerTransport
    from shared.lib.fanout.container_transfer_transport import authenticate_launch_record, reopen_stopped_seat
    from shared.lib.fanout.controller import create_lifecycle_controller, read_evidence

    readonly, docker = _workspace_readonly_docker(spec)
    controller = create_lifecycle_controller(tmp_path / "controller")
    transport = PinnedDockerTransport.start(controller, readonly,
        launcher=_launcher(docker, attestor), socket_attestor=attestor,
        vm_attestor=lambda: pytest.fail("VM operation"),
        image_attestor=lambda: pytest.fail("image operation"),
        cli_attestor=lambda _: pytest.fail("CLI operation"),
        process_runner=lambda _: pytest.fail("provider process"))
    launch = read_evidence(controller, "container-seat-launch",
        transport.handle_evidence_name, transport.handle_evidence_digest)
    assert launch["spec"]["workspace_readonly"] is True
    authenticate_launch_record(controller, launch, readonly, transport.handle)
    fresh = ContainerLauncher(docker, guest_socket_attestor=attestor)
    fresh.restore_launch(launch, readonly, transport.handle, recover=False)
    assert fresh.attest_running(readonly, transport.handle) == transport.handle
    with pytest.raises(ProviderRequestError, match="launch association"):
        fresh.restore_launch(launch, spec, transport.handle, recover=False)
    transport.close()
    stopped = read_evidence(controller, "container-seat-stopped", *transport.stop_evidence)
    assert stopped["spec"]["workspace_readonly"] is True
    before = len(docker.calls)
    reopen_stopped_seat(controller, transport.stop_evidence, spec=readonly, handle=transport.handle)
    with pytest.raises(ProviderRequestError, match="stopped-seat authentication"):
        reopen_stopped_seat(controller, transport.stop_evidence, spec=spec, handle=transport.handle)
    assert len(docker.calls) == before


@pytest.mark.parametrize("value", [False, None, 0, 1, "true", "omitted"])
def test_workspace_readonly_launch_decoder_refuses_foreign_serialized_policy(spec, attestor, value):
    from dataclasses import asdict
    from shared.lib.fanout.container_runtime import decode_launch_record

    readonly, docker = _workspace_readonly_docker(spec)
    launcher = _launcher(docker, attestor)
    handle = launcher.start(readonly)
    launch = {"spec": asdict(readonly), "handle": asdict(handle), "route": "start",
              "volumes": copy.deepcopy(docker.original_volumes)}
    if value == "omitted":
        launch["spec"].pop("workspace_readonly")
    else:
        launch["spec"]["workspace_readonly"] = value
    with pytest.raises(ProviderRequestError, match="launch association"):
        decode_launch_record(launch, readonly, handle, recover=False)


def test_workspace_readonly_recovery_decoder_refuses_numeric_policy(spec, attestor):
    from dataclasses import asdict
    from shared.lib.fanout.container_runtime import decode_launch_record

    readonly, docker = _workspace_readonly_docker(spec)
    launcher = _launcher(docker, attestor)
    handle = launcher.recover(readonly)
    assert launcher.attest_running(readonly, handle) == handle
    launch = {"spec": asdict(readonly), "handle": asdict(handle), "route": "recover",
        "volumes": copy.deepcopy(docker.original_volumes),
        "original_input": ["f" * 64 + ".json", "f" * 64],
        "recovery": json.loads(json.dumps(asdict(launcher._recovery)))}
    assert decode_launch_record(launch, readonly, handle, recover=True).spec == readonly
    launch["recovery"]["spec"]["workspace_readonly"] = 1
    with pytest.raises(ProviderRequestError, match="recovery association"):
        decode_launch_record(launch, readonly, handle, recover=True)


@pytest.mark.parametrize("output,status", [(b"", 1), (b"not-a-container-id\n", 0)])
def test_workspace_readonly_ambiguous_create_fences_exact_owned_seat(spec, attestor, output, status):
    readonly, docker = _workspace_readonly_docker(spec)
    docker.create_output, docker.create_returncode = output, status
    with pytest.raises(ProviderRequestError, match="Docker (created an invalid|create operation failed)"):
        _launcher(docker, attestor).start(readonly)
    assert "disable-restart-new" in docker.events
    assert "stop-new" not in docker.events
    assert not _has_start(docker)
    assert docker.inspect["State"]["Running"] is False
    assert not any(argv[3:5] == ("volume", "rm") for argv in docker.calls)


def test_workspace_readonly_failed_start_fences_exact_owned_seat(spec, attestor):
    readonly, docker = _workspace_readonly_docker(spec)
    docker.start_returncode = 1
    with pytest.raises(ProviderRequestError, match="Docker start operation failed"):
        _launcher(docker, attestor).start(readonly)
    assert docker.inspect["State"]["Running"] is False
    assert docker.events.index("start") < docker.events.index("disable-restart-new") < docker.events.index("stop-new")
    assert not any(argv[3:5] == ("volume", "rm") for argv in docker.calls)


@pytest.mark.parametrize("view", ["host", "inventory"])
def test_workspace_readonly_failed_start_does_not_stop_a_different_mount_policy(spec, attestor, view):
    readonly, docker = _workspace_readonly_docker(spec)
    docker.start_returncode = 1
    def substitute(item):
        if view == "host":
            item["HostConfig"]["Mounts"][0]["ReadOnly"] = False
        else:
            item["Mounts"][0]["RW"] = True
    docker.mutate_after_failed_start = substitute
    with pytest.raises(ProviderRequestError, match="Docker start operation failed") as caught:
        _launcher(docker, attestor).start(readonly)
    assert docker.inspect["State"]["Running"] is True
    assert "disable-restart-new" not in docker.events
    assert "stop-new" not in docker.events
    assert any("retained" in note for note in caught.value.__notes__)


@pytest.fixture(params=[False, True])
def workspace_readonly_stopped(spec, attestor, tmp_path, request):
    from shared.lib.fanout.container_boundary import PinnedDockerTransport
    from shared.lib.fanout.controller import create_lifecycle_controller, read_evidence

    selected, docker = _workspace_readonly_docker(spec) if request.param else (spec, FakeDocker(spec))
    controller = create_lifecycle_controller(tmp_path / "controller")
    transport = PinnedDockerTransport.start(controller, selected,
        launcher=_launcher(docker, attestor), socket_attestor=attestor,
        vm_attestor=lambda: pytest.fail("VM operation"),
        image_attestor=lambda: pytest.fail("image operation"),
        cli_attestor=lambda _: pytest.fail("CLI operation"),
        process_runner=lambda _: pytest.fail("provider process"))
    transport.close()
    stopped = read_evidence(controller, "container-seat-stopped", *transport.stop_evidence)
    return controller, transport, docker, stopped


@pytest.mark.parametrize("value", [0, 1, None, "true", "omitted"])
def test_workspace_readonly_stopped_reader_refuses_nonbool_policy(workspace_readonly_stopped, value):
    from shared.lib.fanout.container_transfer_transport import reopen_stopped_seat
    from shared.lib.fanout.controller import write_evidence

    controller, transport, docker, stopped = workspace_readonly_stopped
    reopen_stopped_seat(controller, transport.stop_evidence, spec=transport.spec, handle=transport.handle)
    if value == "omitted":
        stopped["spec"].pop("workspace_readonly")
    else:
        stopped["spec"]["workspace_readonly"] = value
    resigned = write_evidence(controller, "container-seat-stopped", stopped)
    before = len(docker.calls)
    with pytest.raises(ProviderRequestError, match="stopped-seat authentication"):
        reopen_stopped_seat(controller, resigned, spec=transport.spec, handle=transport.handle)
    assert len(docker.calls) == before


@pytest.mark.parametrize("value", [0, 1])
def test_workspace_readonly_stopped_reader_validates_spec_before_evidence(workspace_readonly_stopped, value):
    from shared.lib.fanout.container_transfer_transport import reopen_stopped_seat

    controller, transport, _, _ = workspace_readonly_stopped
    with pytest.raises(ProviderRequestError, match="workspace mount policy"):
        reopen_stopped_seat(controller, ("absent.json", "0" * 64),
            spec=replace(transport.spec, workspace_readonly=value), handle=transport.handle)


@pytest.mark.parametrize("view,index,change", [
    ("host", 0, "flip"), ("inventory", 0, "flip"),
    ("host", 1, "flip"), ("inventory", 1, "flip"),
    ("host", 2, "flip"), ("inventory", 2, "flip"),
    ("host", 0, "numeric"), ("inventory", 0, "numeric"),
    ("host", 0, "extra"), ("inventory", 0, "extra"),
    ("host", 0, "missing"), ("inventory", 0, "missing"),
    ("host", 0, "source"),
])
def test_workspace_readonly_stopped_reader_refuses_authenticated_mount_drift(
        workspace_readonly_stopped, view, index, change):
    from shared.lib.fanout.container_transfer_transport import reopen_stopped_seat
    from shared.lib.fanout.controller import write_evidence

    controller, transport, docker, stopped = workspace_readonly_stopped
    reopen_stopped_seat(controller, transport.stop_evidence, spec=transport.spec, handle=transport.handle)
    path = controller.root / "quarantine/runtime-proofs" / stopped["raw_inspect"]["path"]
    raw = json.loads(path.read_bytes())
    mounts = raw[0]["HostConfig"]["Mounts"] if view == "host" else raw[0]["Mounts"]
    field = "ReadOnly" if view == "host" else "RW"
    if change == "flip":
        mounts[index][field] = not mounts[index].get(field, False)
    elif change == "numeric":
        mounts[index][field] = int(mounts[index].get(field, False))
    elif change == "extra":
        mounts.append(copy.deepcopy(mounts[0]))
    elif change == "missing":
        mounts.pop(index)
    else:
        mounts[index]["Source"] = "foreign-volume"
    wire = json.dumps(raw).encode()
    path.write_bytes(wire)
    stopped["raw_inspect"].update(digest=hashlib.sha256(wire).hexdigest(), size=len(wire))
    resigned = write_evidence(controller, "container-seat-stopped", stopped)
    before = len(docker.calls)
    with pytest.raises(ProviderRequestError, match="stopped-seat authentication"):
        reopen_stopped_seat(controller, resigned, spec=transport.spec, handle=transport.handle)
    assert len(docker.calls) == before


def test_launch_without_authenticated_guest_attestor_fails_before_docker(spec, fake_docker):
    with pytest.raises(ProviderRequestError, match="socket attestor"):
        ContainerLauncher(fake_docker).start(spec)
    assert fake_docker.calls == []


@pytest.mark.parametrize("changed", [
    "path", "uid", "mode", "device", "inode", "ctime_ns",
])
def test_guest_socket_attestation_refuses_wrong_or_changed_object(spec, fake_docker, attestor, changed):
    values = {"path": "/run/fanout/other.sock", "uid": 0,
              "mode": stat.S_IFREG | 0o600, "device": 5, "inode": 18,
              "ctime_ns": 12346}
    attestor.second_proof = replace(attestor.proof, **{changed: values[changed]})
    with pytest.raises(ProviderRequestError, match="socket"):
        _launcher(fake_docker, attestor).start(spec)
    assert "create" in fake_docker.events
    assert not _has_start(fake_docker)


@pytest.mark.parametrize("field,value", [
    ("workspace_volume", "shared-workspace"),
    ("session_volume", "shared-session"),
    ("socket_path", "/run/fanout/other.sock"),
    ("image_digest", "fanout-seat:latest"),
    ("cli_digest", "not-a-digest"),
    ("seat_id", "../other"),
])
def test_bad_spec_refuses_before_docker(spec, fake_docker, attestor, field, value):
    with pytest.raises(ProviderRequestError):
        _launcher(fake_docker, attestor).start(replace(spec, **{field: value}))
    assert fake_docker.calls == []


@pytest.mark.parametrize("mutate,match", [
    (lambda data: data["HostConfig"].update(NetworkMode="bridge"), "network"),
    (lambda data: data["NetworkSettings"].update(Networks={"bridge": {}}), "network"),
    (lambda data: data["HostConfig"].update(PortBindings={"80/tcp": []}), "network"),
    (lambda data: data["Config"].update(Image="other@sha256:" + "f" * 64), "image"),
    (lambda data: data["Config"]["Labels"].update({BOUNDARY + "cli_digest": "f" * 64}), "CLI"),
    (lambda data: data["Config"].update(User="0"), "user"),
    (lambda data: data["HostConfig"].update(PidMode="host"), "PID"),
    (lambda data: data["HostConfig"].update(PidMode="private"), "PID"),
    (lambda data: data["HostConfig"].update(IpcMode="host"), "IPC"),
    (lambda data: data["HostConfig"].update(IpcMode=""), "IPC"),
    (lambda data: data["HostConfig"].update(UTSMode="host"), "UTS"),
    (lambda data: data["HostConfig"].update(CgroupnsMode=""), "cgroup"),
    (lambda data: data["HostConfig"].update(CapDrop=[]), "capabilit"),
    (lambda data: data["HostConfig"].update(CapAdd=["SYS_ADMIN"]), "capabilit"),
    (lambda data: data["HostConfig"].update(Privileged=True), "privileg"),
    (lambda data: data["HostConfig"].update(SecurityOpt=[]), "privileg"),
    (lambda data: data["HostConfig"].update(ReadonlyRootfs=False), "read-only"),
    (lambda data: data["HostConfig"].update(Memory=0), "resource"),
    (lambda data: data["HostConfig"].update(NanoCpus=0), "resource"),
    (lambda data: data["HostConfig"].update(PidsLimit=0), "resource"),
    (lambda data: data["Mounts"].append({"Type": "bind", "Source": "/Users/owner", "Destination": "/home", "RW": True}), "mount"),
    (lambda data: data["Mounts"][0].update(Name=SESSION), "mount"),
    (lambda data: data["Mounts"][2].update(Source="/run/fanout/other.sock"), "mount"),
    (lambda data: data["Mounts"][2].update(RW=True), "mount"),
    (lambda data: data["HostConfig"].update(Binds=["/Users/owner:/home"]), "mount"),
    (lambda data: data["HostConfig"]["Mounts"][2].update(Source="/run/fanout/other.sock"), "mount"),
    (lambda data: data["Config"]["Labels"].update({BOUNDARY + "run": "foreign-run"}), "label"),
])
def test_post_create_inspect_refuses_security_drift_before_start(spec, fake_docker, attestor, mutate, match):
    mutate(fake_docker.inspect)
    with pytest.raises(ProviderRequestError, match=match):
        _launcher(fake_docker, attestor).start(spec)
    assert fake_docker.inspect_count == 1
    assert not _has_start(fake_docker)


def test_pre_start_inspect_refuses_new_drift(spec, fake_docker, attestor):
    fake_docker.mutate_second_inspect = lambda data: data["HostConfig"].update(NetworkMode="bridge")
    with pytest.raises(ProviderRequestError, match="network"):
        _launcher(fake_docker, attestor).start(spec)
    assert fake_docker.inspect_count == 2
    assert not _has_start(fake_docker)


def test_failed_start_after_docker_became_running_stops_exact_created_seat(
        spec, fake_docker, attestor):
    fake_docker.start_returncode = 1
    with pytest.raises(ProviderRequestError, match="Docker start operation failed"):
        _launcher(fake_docker, attestor).start(spec)
    assert fake_docker.inspect["State"]["Running"] is False
    assert fake_docker.inspect["HostConfig"]["RestartPolicy"]["Name"] == "no"
    assert fake_docker.events.index("start") < fake_docker.events.index(
        "disable-restart-new") < fake_docker.events.index("stop-new")
    assert not any(argv[3:5] == ("volume", "rm") for argv in fake_docker.calls)


@pytest.mark.parametrize("failure", ["inspect", "socket", "volume"])
def test_post_create_failure_fences_authenticated_created_seat(
        spec, fake_docker, attestor, failure):
    if failure == "inspect":
        fake_docker.fail_inspect_at = 1
    elif failure == "socket":
        attestor.second_proof = replace(attestor.proof, inode=18)
    else:
        fake_docker.mutate_volume_on_recheck = lambda volumes: volumes["workspace"].update(
            Options={"device": "/Users/owner", "o": "bind"})
    with pytest.raises(ProviderRequestError):
        _launcher(fake_docker, attestor).start(spec)
    assert "create" in fake_docker.events
    assert "disable-restart-new" in fake_docker.events
    assert "stop-new" not in fake_docker.events
    assert fake_docker.inspect["State"]["Running"] is False
    assert not _has_start(fake_docker)


@pytest.mark.parametrize("create_output,create_returncode", [
    (b"not-a-container-id\n", 0), (b"", 1),
])
def test_ambiguous_create_result_fences_container_found_by_generated_name(
        spec, fake_docker, attestor, create_output, create_returncode):
    fake_docker.create_output = create_output
    fake_docker.create_returncode = create_returncode
    with pytest.raises(ProviderRequestError, match="Docker (created an invalid|create operation failed)"):
        _launcher(fake_docker, attestor).start(spec)
    assert "disable-restart-new" in fake_docker.events
    assert fake_docker.inspect["State"]["Running"] is False
    assert "stop-new" not in fake_docker.events


@pytest.mark.parametrize("mutate", [
    lambda item: item["Config"]["Labels"].update({BOUNDARY + "run": "foreign-run"}),
    lambda item: item.update(Image="sha256:" + "f" * 64),
    lambda item: item["Mounts"][0].update(Name="foreign-volume"),
    lambda item: item["HostConfig"]["Mounts"][0].update(Source="foreign-volume"),
])
def test_failed_launch_never_stops_seat_with_unproved_ownership(
        spec, fake_docker, attestor, mutate):
    fake_docker.start_returncode = 1
    fake_docker.mutate_after_failed_start = mutate
    with pytest.raises(ProviderRequestError, match="Docker start operation failed") as caught:
        _launcher(fake_docker, attestor).start(spec)
    assert fake_docker.inspect["State"]["Running"] is True
    assert "disable-restart-new" not in fake_docker.events
    assert "stop-new" not in fake_docker.events
    assert any("retained" in note for note in caught.value.__notes__)


def test_failed_launch_accepts_inherited_image_label_when_owner_fields_match(
        spec, fake_docker, attestor):
    fake_docker.inspect["Config"]["Labels"]["org.example.image"] = "base"
    fake_docker.start_returncode = 1
    with pytest.raises(ProviderRequestError, match="Docker start operation failed"):
        _launcher(fake_docker, attestor).start(spec)
    assert fake_docker.inspect["State"]["Running"] is False
    assert "stop-new" in fake_docker.events


def test_failed_launch_refuses_ambiguous_running_state_before_docker_mutation(
        spec, fake_docker, attestor):
    fake_docker.start_returncode = 1
    fake_docker.mutate_after_failed_start = lambda item: item["State"].update(
        Status="restarting", Restarting=True)
    with pytest.raises(ProviderRequestError, match="Docker start operation failed") as caught:
        _launcher(fake_docker, attestor).start(spec)
    assert "disable-restart-new" not in fake_docker.events
    assert "stop-new" not in fake_docker.events
    assert any("retained" in note for note in caught.value.__notes__)


def test_wrong_valid_create_id_does_not_authorize_named_container_cleanup(
        spec, fake_docker, attestor):
    fake_docker.create_output = ("f" * 64 + "\n").encode()
    with pytest.raises(ProviderRequestError, match="Docker inspect operation failed") as caught:
        _launcher(fake_docker, attestor).start(spec)
    assert "disable-restart-new" not in fake_docker.events
    assert "stop-new" not in fake_docker.events
    assert any("retained" in note for note in caught.value.__notes__)


def test_failed_launch_reports_unverified_stop_without_hiding_start_error(
        spec, fake_docker, attestor):
    fake_docker.start_returncode = 1
    fake_docker.stop_noop = True
    with pytest.raises(ProviderRequestError, match="Docker start operation failed") as caught:
        _launcher(fake_docker, attestor).start(spec)
    assert fake_docker.inspect["State"]["Running"] is True
    assert "stop-new" in fake_docker.events
    assert any("retained" in note for note in caught.value.__notes__)


@pytest.mark.parametrize("kind", ["workspace", "session"])
def test_changed_private_volume_driver_or_identity_is_rejected(spec, fake_docker, attestor, kind):
    fake_docker.volumes[kind]["Options"] = {"type": "none", "device": "/Users/owner", "o": "bind"}
    with pytest.raises(ProviderRequestError, match="volume"):
        _launcher(fake_docker, attestor).start(spec)
    assert "create" not in fake_docker.events


def test_image_id_must_match_pinned_digest_reference(spec, fake_docker, attestor):
    fake_docker.inspect["Image"] = "sha256:" + "f" * 64
    with pytest.raises(ProviderRequestError, match="image"):
        _launcher(fake_docker, attestor).start(spec)
    assert not _has_start(fake_docker)


def test_image_without_exact_repo_digest_refuses_cleanly(spec, fake_docker, attestor):
    fake_docker.image_repo_digests = None
    with pytest.raises(ProviderRequestError, match="image"):
        _launcher(fake_docker, attestor).start(spec)
    assert "create" not in fake_docker.events


def test_inspect_mount_order_does_not_change_admission(spec, fake_docker, attestor):
    fake_docker.inspect["Mounts"].reverse()
    fake_docker.inspect["HostConfig"]["Mounts"].reverse()
    handle = _launcher(fake_docker, attestor).start(spec)
    assert handle.container_id == CONTAINER_ID


def test_extra_tmpfs_mount_refuses_before_start(spec, fake_docker, attestor):
    fake_docker.inspect["HostConfig"]["Tmpfs"] = {"/home": "rw"}
    with pytest.raises(ProviderRequestError, match="mount"):
        _launcher(fake_docker, attestor).start(spec)
    assert not _has_start(fake_docker)


def test_recovery_fences_orphan_before_new_seat_even_when_old_image_and_mount_changed(spec, fake_docker, attestor):
    old_id = fake_docker.add_orphan(changed_mount=True)
    handle = _launcher(fake_docker, attestor).recover(spec)
    assert handle.container_id != old_id
    assert fake_docker.events.index("inventory") < fake_docker.events.index("stop-orphan") < fake_docker.events.index("create")
    assert fake_docker.old[old_id]["State"]["Running"] is False


def test_recovery_disables_old_restart_policy_before_stopping(spec, fake_docker, attestor):
    old_id = fake_docker.add_orphan(restart="always")
    _launcher(fake_docker, attestor).recover(spec)
    assert fake_docker.events.index("disable-restart") < fake_docker.events.index("stop-orphan")
    assert fake_docker.old[old_id]["HostConfig"]["RestartPolicy"]["Name"] == "no"


def test_recovery_ignores_unrelated_foreign_run(spec, fake_docker, attestor):
    old_id = _foreign_orphan(fake_docker, spec)
    _launcher(fake_docker, attestor).recover(spec)
    assert fake_docker.old[old_id]["State"]["Running"] is True
    assert "stop-orphan" not in fake_docker.events


@pytest.mark.parametrize("conflict", ["seat", "workspace", "session", "socket"])
def test_recovery_refuses_foreign_run_resource_conflict_without_stopping_it(spec, fake_docker, attestor, conflict):
    old_id = _foreign_orphan(fake_docker, spec, conflict=conflict)
    with pytest.raises(ProviderRequestError, match="foreign.*conflict"):
        _launcher(fake_docker, attestor).recover(spec)
    assert fake_docker.old[old_id]["State"]["Running"] is True
    assert "create" not in fake_docker.events


def test_recovery_fences_owned_orphan_even_when_foreign_conflict_blocks_create(spec, fake_docker, attestor):
    own_id = fake_docker.add_orphan()
    foreign_id = _foreign_orphan(fake_docker, spec, conflict="socket")
    with pytest.raises(ProviderRequestError, match="foreign.*conflict"):
        _launcher(fake_docker, attestor).recover(spec)
    assert fake_docker.old[own_id]["State"]["Running"] is False
    assert fake_docker.old[foreign_id]["State"]["Running"] is True
    assert "create" not in fake_docker.events


def test_recovery_refuses_unlabeled_socket_conflict_without_stopping_foreign_container(spec, fake_docker, attestor):
    foreign_id = _foreign_orphan(fake_docker, spec, conflict="socket")
    fake_docker.old[foreign_id]["Config"]["Labels"].pop(BOUNDARY + "boundary")
    with pytest.raises(ProviderRequestError, match="foreign.*conflict"):
        _launcher(fake_docker, attestor).recover(spec)
    assert fake_docker.old[foreign_id]["State"]["Running"] is True
    assert "create" not in fake_docker.events


def test_recovery_ignores_unlabeled_unrelated_container(spec, fake_docker, attestor):
    foreign_id = _foreign_orphan(fake_docker, spec)
    fake_docker.old[foreign_id]["Config"]["Labels"].pop(BOUNDARY + "boundary")
    _launcher(fake_docker, attestor).recover(spec)
    assert fake_docker.old[foreign_id]["State"]["Running"] is True


def test_recovery_ignores_unlabeled_container_with_no_mounts(spec, fake_docker, attestor):
    foreign_id = _foreign_orphan(fake_docker, spec)
    foreign = fake_docker.old[foreign_id]
    foreign["Config"]["Labels"] = None
    foreign["Mounts"] = []
    foreign["HostConfig"]["Mounts"] = None
    _launcher(fake_docker, attestor).recover(spec)
    assert fake_docker.old[foreign_id]["State"]["Running"] is True


def test_recovery_refuses_foreign_bind_of_socket_parent(spec, fake_docker, attestor):
    foreign_id = _foreign_bind(fake_docker, spec, "/run/fanout")
    with pytest.raises(ProviderRequestError, match="conflict"):
        _launcher(fake_docker, attestor).recover(spec)
    assert fake_docker.old[foreign_id]["State"]["Running"] is True
    assert "create" not in fake_docker.events


@pytest.mark.parametrize("kind", ["workspace", "session"])
@pytest.mark.parametrize("overlap", ["ancestor", "descendant"])
def test_recovery_refuses_foreign_bind_overlapping_private_volume(spec, fake_docker, attestor, kind, overlap):
    volume_root = f"/var/lib/docker/private-mounts/{kind}/{fake_docker.volumes[kind]['Name']}"
    fake_docker.volumes[kind]["Mountpoint"] = volume_root
    fake_docker.original_volumes[kind]["Mountpoint"] = volume_root
    source = posixpath.dirname(volume_root) if overlap == "ancestor" else volume_root + "/secret"
    foreign_id = _foreign_bind(fake_docker, spec, source)
    with pytest.raises(ProviderRequestError, match="conflict"):
        _launcher(fake_docker, attestor).recover(spec)
    assert fake_docker.old[foreign_id]["State"]["Running"] is True
    assert "create" not in fake_docker.events


def test_initial_start_scans_foreign_socket_parent_without_fencing(spec, fake_docker, attestor):
    foreign_id = _foreign_bind(fake_docker, spec, "/run/fanout")
    with pytest.raises(ProviderRequestError, match="conflict"):
        _launcher(fake_docker, attestor).start(spec)
    assert fake_docker.old[foreign_id]["State"]["Running"] is True
    assert "inventory" in fake_docker.events
    assert "stop-orphan" not in fake_docker.events
    assert "disable-restart" not in fake_docker.events
    assert "create" not in fake_docker.events


def test_initial_start_refuses_existing_same_run_seat_without_fencing(spec, fake_docker, attestor):
    own_id = fake_docker.add_orphan()
    with pytest.raises(ProviderRequestError, match="conflict"):
        _launcher(fake_docker, attestor).start(spec)
    assert fake_docker.old[own_id]["State"]["Running"] is True
    assert "stop-orphan" not in fake_docker.events
    assert "create" not in fake_docker.events


def test_recovery_refuses_unresolved_foreign_bind_source(spec, fake_docker, attestor):
    foreign_id = _foreign_bind(fake_docker, spec, "/unrelated")
    fake_docker.old[foreign_id]["Mounts"][-1]["Source"] = None
    fake_docker.old[foreign_id]["HostConfig"]["Mounts"] = None
    with pytest.raises(ProviderRequestError, match="mount path is invalid"):
        _launcher(fake_docker, attestor).recover(spec)
    assert fake_docker.old[foreign_id]["State"]["Running"] is True
    assert "create" not in fake_docker.events


def test_recovery_rechecks_socket_and_inspect_before_start(spec, fake_docker, attestor):
    fake_docker.add_orphan()
    fake_docker.mutate_second_inspect = lambda data: data["Mounts"][2].update(Source="/Users/owner.sock")
    with pytest.raises(ProviderRequestError, match="mount"):
        _launcher(fake_docker, attestor).recover(spec)
    assert "stop-orphan" in fake_docker.events
    assert not _has_start(fake_docker)


@pytest.mark.parametrize("already_stopped", [False, True])
@pytest.mark.parametrize("predecessor_count", [1, 3, 5])
def test_recovery_attestation_retains_only_fenced_stopped_predecessors(
        spec, fake_docker, attestor, already_stopped, predecessor_count):
    old_ids = [fake_docker.add_orphan() for _ in range(predecessor_count)]
    if already_stopped:
        for old_id in old_ids:
            _stopped_inspect(fake_docker.old[old_id])
    launcher = _launcher(fake_docker, attestor)
    handle = launcher.recover(spec)
    retained = copy.deepcopy(fake_docker.old)
    calls_before = len(fake_docker.calls)

    assert launcher.attest_running(spec, handle) == handle
    assert fake_docker.old == retained
    assert set(retained) == set(old_ids)
    assert all(item["State"]["Status"] == "exited"
               and item["HostConfig"]["OomKillDisable"] is None
               and item["NetworkSettings"]["Networks"]["none"]["EndpointID"] == ""
               for item in retained.values())
    assert all(argv[3] not in {"create", "start", "stop", "update", "rm"}
               for argv in fake_docker.calls[calls_before:])


@pytest.mark.parametrize("order", list(permutations(range(3))))
def test_recovery_attestation_accepts_stopped_mount_permutation(
        spec, fake_docker, attestor, order):
    old_id = fake_docker.add_orphan()
    launcher = _launcher(fake_docker, attestor)
    handle = launcher.recover(spec)
    mounts = fake_docker.old[old_id]["Mounts"]
    fake_docker.old[old_id]["Mounts"] = [mounts[index] for index in order]
    retained = copy.deepcopy(fake_docker.old)
    volumes = copy.deepcopy(fake_docker.volumes)
    calls_before = len(fake_docker.calls)

    assert launcher.attest_running(spec, handle) == handle
    assert fake_docker.old == retained
    assert fake_docker.volumes == volumes
    assert retained[old_id]["State"]["Status"] == "exited"
    assert retained[old_id]["HostConfig"]["RestartPolicy"]["Name"] == "no"
    assert all(argv[3] not in {"create", "start", "stop", "update", "rm"}
               for argv in fake_docker.calls[calls_before:])


@pytest.mark.parametrize("destination", [
    "/workspace", "/workspace/", "/workspace/.", "/session/../workspace",
])
@pytest.mark.parametrize("after_capture", [False, True])
def test_recovery_refuses_duplicate_stopped_mount_destinations(
        spec, fake_docker, attestor, destination, after_capture):
    old_id = fake_docker.add_orphan()
    _stopped_inspect(fake_docker.old[old_id])
    launcher = _launcher(fake_docker, attestor)
    if after_capture:
        handle = launcher.recover(spec)
    duplicate = copy.deepcopy(fake_docker.old[old_id]["Mounts"][0])
    duplicate["Destination"] = destination
    fake_docker.old[old_id]["Mounts"].append(duplicate)
    retained = copy.deepcopy(fake_docker.old)
    calls_before = len(fake_docker.calls)
    with pytest.raises(ProviderRequestError, match="duplicate.*mount"):
        if after_capture:
            launcher.attest_running(spec, handle)
        else:
            launcher.recover(spec)
    assert fake_docker.old == retained
    if after_capture:
        assert all(argv[3] not in {"create", "start", "stop", "update", "rm"}
                   for argv in fake_docker.calls[calls_before:])
    else:
        assert "create" not in fake_docker.events


@pytest.mark.parametrize("field,value", [
    ("Type", None), ("Type", "tmpfs"), ("Source", None),
    ("Source", "relative"), ("Destination", None), ("Destination", "relative"),
    ("Destination", "//workspace"), ("RW", "true"), ("Name", False), ("Driver", []),
])
def test_recovery_refuses_malformed_permuted_mount_without_mutation(
        spec, fake_docker, attestor, field, value):
    old_id = fake_docker.add_orphan()
    launcher = _launcher(fake_docker, attestor)
    handle = launcher.recover(spec)
    fake_docker.old[old_id]["Mounts"][0][field] = value
    fake_docker.old[old_id]["Mounts"].reverse()
    retained = copy.deepcopy(fake_docker.old)
    calls_before = len(fake_docker.calls)
    with pytest.raises(ProviderRequestError):
        launcher.attest_running(spec, handle)
    assert fake_docker.old == retained
    assert all(argv[3] not in {"create", "start", "stop", "update", "rm"}
               for argv in fake_docker.calls[calls_before:])


@pytest.mark.parametrize("change", [
    lambda item: item["Mounts"][0].update(Type="bind"),
    lambda item: item["Mounts"][0].update(Source="/changed"),
    lambda item: item["Mounts"][0].update(Destination="/changed"),
    lambda item: item["Mounts"][0].update(Name="changed-volume"),
    lambda item: item["Mounts"][0].update(Driver="changed-driver"),
    lambda item: item["Mounts"][0].update(RW=False),
    lambda item: item["Mounts"][0].update(Mode="ro"),
    lambda item: item["Mounts"][0].update(Propagation="rshared"),
    lambda item: item["Mounts"][0]["Extension"].update(kept=False),
    lambda item: item["HostConfig"]["Mounts"].reverse(),
    lambda item: item["HostConfig"]["SecurityOpt"].reverse(),
    lambda item: item["HostConfig"].update(Privileged=True),
    lambda item: item["HostConfig"].pop("Tmpfs"),
    lambda item: item["Config"].update(User="0"),
    lambda item: item["NetworkSettings"]["Networks"]["none"].update(EndpointID="f" * 64),
    lambda item: item["State"].update(ExitCode=1),
])
def test_recovery_refuses_permuted_predecessor_drift_without_mutation(
        spec, fake_docker, attestor, change):
    old_id = fake_docker.add_orphan()
    old = fake_docker.old[old_id]
    _stopped_inspect(old)
    old["Mounts"][0].update(Mode="rw", Propagation="rprivate", Extension={"kept": True})
    old["HostConfig"]["SecurityOpt"].append("label=synthetic")
    old["State"]["ExitCode"] = 0
    launcher = _launcher(fake_docker, attestor)
    handle = launcher.recover(spec)
    change(old)
    old["Mounts"].reverse()
    retained = copy.deepcopy(fake_docker.old)
    calls_before = len(fake_docker.calls)
    with pytest.raises(ProviderRequestError):
        launcher.attest_running(spec, handle)
    assert fake_docker.old == retained
    assert all(argv[3] not in {"create", "start", "stop", "update", "rm"}
               for argv in fake_docker.calls[calls_before:])


def test_stopped_observation_preserves_input_mount_order(spec, fake_docker, attestor):
    old_id = fake_docker.add_orphan()
    _launcher(fake_docker, attestor).recover(spec)
    item = copy.deepcopy(fake_docker.old[old_id])
    item["Mounts"] = item["Mounts"][1:] + item["Mounts"][:1]
    retained = copy.deepcopy(item)
    ContainerLauncher._stopped_observation(old_id, spec, item)
    assert item == retained


@pytest.mark.parametrize("path", [
    ("Image",), ("Name",), ("Mounts",), ("NetworkSettings",),
    ("Config", "Image"), ("Config", "User"),
    ("Config", "Labels", BOUNDARY + "task"),
    ("Config", "Labels", BOUNDARY + "cli_digest"),
    *[("HostConfig", field) for field in (
        "NetworkMode", "PidMode", "IpcMode", "UTSMode", "CgroupnsMode",
        "ReadonlyRootfs", "Privileged", "PublishAllPorts", "PidsLimit",
        "Memory", "MemorySwap", "NanoCpus", "OomKillDisable", "SecurityOpt",
        "CapDrop", "CapAdd", "Devices", "DeviceRequests", "Binds",
        "VolumesFrom", "PortBindings", "Mounts")],
    ("HostConfig", "RestartPolicy", "MaximumRetryCount"),
    *[("Mounts", 0, field) for field in (
        "Type", "Name", "Source", "Destination", "Driver", "RW")],
    *[("HostConfig", "Mounts", 0, field) for field in ("Type", "Source", "Target")],
    *[("NetworkSettings", field) for field in ("Networks", "Ports", "SandboxID", "SandboxKey")],
    *[("NetworkSettings", "Networks", "none", field) for field in (
        "NetworkID", "EndpointID", "IPAddress", "Gateway", "GlobalIPv6Address",
        "IPv6Gateway", "MacAddress", "IPPrefixLen", "GlobalIPv6PrefixLen",
        "IPAMConfig", "Links", "Aliases", "DriverOpts", "DNSNames")],
])
def test_recovery_refuses_missing_predecessor_observation_at_capture(
        spec, fake_docker, attestor, path):
    old_id = fake_docker.add_orphan()
    _stopped_inspect(fake_docker.old[old_id])
    parent = fake_docker.old[old_id]
    for key in path[:-1]:
        parent = parent[key]
    del parent[path[-1]]
    with pytest.raises(ProviderRequestError):
        _launcher(fake_docker, attestor).recover(spec)
    assert fake_docker.old[old_id]["State"]["Running"] is False
    assert "create" not in fake_docker.events


@pytest.mark.parametrize("path,value", [
    (("Image",), None), (("Name",), 7), (("Mounts",), None),
    (("Mounts",), [None]), (("NetworkSettings",), []),
    (("Config", "Image"), None), (("Config", "User"), 0),
    (("Config", "Labels", BOUNDARY + "task"), []),
    (("HostConfig", "ReadonlyRootfs"), "true"),
    (("HostConfig", "PidsLimit"), True),
    (("HostConfig", "OomKillDisable"), "false"),
    (("HostConfig", "SecurityOpt"), "no-new-privileges"),
    (("HostConfig", "CapDrop"), [None]),
    (("HostConfig", "Devices"), "none"),
    (("HostConfig", "Mounts"), None),
    (("HostConfig", "Mounts"), [None]),
    (("HostConfig", "RestartPolicy", "MaximumRetryCount"), "0"),
    (("HostConfig", "Mounts", 0, "Source"), None),
    (("HostConfig", "Mounts", 0, "ReadOnly"), "false"),
    (("Mounts", 0, "RW"), "true"),
    (("Mounts", 0, "Source"), None),
    (("NetworkSettings", "Ports"), []),
    (("NetworkSettings", "Networks"), []),
    (("NetworkSettings", "Networks", "none", "EndpointID"), None),
    (("NetworkSettings", "Networks", "none", "IPPrefixLen"), "0"),
    (("NetworkSettings", "Networks", "none", "Aliases"), "none"),
])
def test_recovery_refuses_malformed_predecessor_observation_at_capture(
        spec, fake_docker, attestor, path, value):
    old_id = fake_docker.add_orphan()
    _stopped_inspect(fake_docker.old[old_id])
    parent = fake_docker.old[old_id]
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = value
    with pytest.raises(ProviderRequestError):
        _launcher(fake_docker, attestor).recover(spec)
    assert fake_docker.old[old_id]["State"]["Running"] is False
    assert "create" not in fake_docker.events


def test_recovery_attests_complete_stopped_quarantine_with_old_image_and_mount_values(
        spec, fake_docker, attestor):
    old_id = fake_docker.add_orphan(changed_mount=True)
    launcher = _launcher(fake_docker, attestor)
    handle = launcher.recover(spec)
    retained = copy.deepcopy(fake_docker.old[old_id])
    assert launcher.attest_running(spec, handle) == handle
    assert fake_docker.old[old_id] == retained
    assert retained["Mounts"][0]["Name"] == "foreign-volume"
    assert retained["Config"]["Image"] == "other@sha256:" + "f" * 64


@pytest.mark.parametrize("tmpfs", ["omitted", None, {}, {"/home": "rw"}])
def test_recovery_attestation_accepts_stopped_docker_tmpfs_shapes(
        spec, fake_docker, attestor, tmpfs):
    old_id = fake_docker.add_orphan()
    _stopped_inspect(fake_docker.old[old_id])
    host = fake_docker.old[old_id]["HostConfig"]
    if tmpfs == "omitted":
        del host["Tmpfs"]
    else:
        host["Tmpfs"] = tmpfs
    launcher = _launcher(fake_docker, attestor)
    handle = launcher.recover(spec)
    retained = copy.deepcopy(fake_docker.old[old_id])
    assert launcher.attest_running(spec, handle) == handle
    assert fake_docker.old[old_id] == retained


@pytest.mark.parametrize("tmpfs", [[], "rw", False, {"/home": []}, {"/home": False}])
def test_recovery_refuses_present_malformed_tmpfs_at_capture(
        spec, fake_docker, attestor, tmpfs):
    old_id = fake_docker.add_orphan()
    _stopped_inspect(fake_docker.old[old_id])
    fake_docker.old[old_id]["HostConfig"]["Tmpfs"] = tmpfs
    with pytest.raises(ProviderRequestError):
        _launcher(fake_docker, attestor).recover(spec)
    assert "create" not in fake_docker.events


@pytest.mark.parametrize("initially_omitted", [False, True])
def test_recovery_attestation_refuses_tmpfs_omission_presence_drift(
        spec, fake_docker, attestor, initially_omitted):
    old_id = fake_docker.add_orphan()
    _stopped_inspect(fake_docker.old[old_id])
    host = fake_docker.old[old_id]["HostConfig"]
    if initially_omitted:
        del host["Tmpfs"]
    launcher = _launcher(fake_docker, attestor)
    handle = launcher.recover(spec)
    if initially_omitted:
        host["Tmpfs"] = None
    else:
        del host["Tmpfs"]
    with pytest.raises(ProviderRequestError):
        launcher.attest_running(spec, handle)


def _nested_stopped_quarantine(item):
    host = item["HostConfig"]
    host["Devices"] = [{"PathOnHost": "/dev/null", "PathInContainer": "/dev/example",
                        "CgroupPermissions": "r"}]
    host["DeviceRequests"] = [{"Driver": "synthetic", "Count": -1,
                              "DeviceIDs": ["device-one"],
                              "Capabilities": [["gpu"], None], "Options": {"mode": "stopped"}}]
    ports = {"80/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8080"}], "81/tcp": None}
    host["PortBindings"] = copy.deepcopy(ports)
    item["NetworkSettings"]["Ports"] = copy.deepcopy(ports)
    endpoint = item["NetworkSettings"]["Networks"]["none"]
    endpoint.update(Links=["parent"], Aliases=["stopped"], DNSNames=["stopped.example"],
                    DriverOpts={"mode": "stopped"},
                    IPAMConfig={"IPv4Address": "192.0.2.1", "IPv6Address": "2001:db8::1",
                                "LinkLocalIPs": ["169.254.0.1"], "Extension": {"kept": True}})


@pytest.mark.parametrize("path,value", [
    (("HostConfig", "Devices", 0), {}),
    *[(("HostConfig", "Devices", 0, field), value)
      for field in ("PathOnHost", "PathInContainer", "CgroupPermissions")
      for value in ("missing", False)],
    (("HostConfig", "DeviceRequests", 0), {}),
    *[(("HostConfig", "DeviceRequests", 0, field), "missing")
      for field in ("Driver", "Count", "DeviceIDs", "Capabilities", "Options")],
    (("HostConfig", "DeviceRequests", 0, "Driver"), False),
    (("HostConfig", "DeviceRequests", 0, "Count"), True),
    (("HostConfig", "DeviceRequests", 0, "DeviceIDs"), False),
    (("HostConfig", "DeviceRequests", 0, "DeviceIDs"), [False]),
    (("HostConfig", "DeviceRequests", 0, "Capabilities"), False),
    (("HostConfig", "DeviceRequests", 0, "Capabilities"), [False]),
    (("HostConfig", "DeviceRequests", 0, "Capabilities"), [[False]]),
    (("HostConfig", "DeviceRequests", 0, "Options"), False),
    (("HostConfig", "DeviceRequests", 0, "Options"), {"mode": False}),
    *[(path + ("80/tcp",), value)
      for path in (("HostConfig", "PortBindings"), ("NetworkSettings", "Ports"))
      for value in (False, {}, [False], [{}])],
    *[(path + ("80/tcp", 0, field), value)
      for path in (("HostConfig", "PortBindings"), ("NetworkSettings", "Ports"))
      for field in ("HostIp", "HostPort") for value in ("missing", False)],
    *[(("NetworkSettings", "Networks", "none", field), [False])
      for field in ("Links", "Aliases", "DNSNames")],
    (("NetworkSettings", "Networks", "none", "DriverOpts"), {"mode": False}),
    *[(("NetworkSettings", "Networks", "none", "IPAMConfig", field), False)
      for field in ("IPv4Address", "IPv6Address", "LinkLocalIPs")],
    (("NetworkSettings", "Networks", "none", "IPAMConfig", "LinkLocalIPs"), [False]),
])
def test_recovery_refuses_malformed_nested_observation_at_capture(
        spec, fake_docker, attestor, path, value):
    old_id = fake_docker.add_orphan()
    _stopped_inspect(fake_docker.old[old_id])
    _nested_stopped_quarantine(fake_docker.old[old_id])
    parent = fake_docker.old[old_id]
    for key in path[:-1]:
        parent = parent[key]
    if value == "missing":
        del parent[path[-1]]
    else:
        parent[path[-1]] = value
    with pytest.raises(ProviderRequestError):
        _launcher(fake_docker, attestor).recover(spec)
    assert "create" not in fake_docker.events
    assert fake_docker.old[old_id]["State"]["Running"] is False


@pytest.mark.parametrize("shape", ["nonempty", "nullable", "empty-ipam", "nullable-link-local", "nil-capability-row"])
def test_recovery_attests_well_formed_nested_stopped_quarantine(
        spec, fake_docker, attestor, shape):
    old_id = fake_docker.add_orphan()
    _stopped_inspect(fake_docker.old[old_id])
    _nested_stopped_quarantine(fake_docker.old[old_id])
    host = fake_docker.old[old_id]["HostConfig"]
    endpoint = fake_docker.old[old_id]["NetworkSettings"]["Networks"]["none"]
    if shape == "nullable":
        host["Devices"] = None
        host["DeviceRequests"][0].update(DeviceIDs=None, Capabilities=None, Options=None)
        host["PortBindings"] = None
        fake_docker.old[old_id]["NetworkSettings"]["Ports"] = None
        endpoint.update(Links=None, Aliases=None, DNSNames=None, DriverOpts=None, IPAMConfig=None)
    elif shape == "empty-ipam":
        endpoint["IPAMConfig"] = {}
    elif shape == "nullable-link-local":
        endpoint["IPAMConfig"] = {"LinkLocalIPs": None}
    elif shape == "nil-capability-row":
        host["DeviceRequests"][0]["Capabilities"] = [None]
    launcher = _launcher(fake_docker, attestor)
    handle = launcher.recover(spec)
    retained = copy.deepcopy(fake_docker.old[old_id])
    assert launcher.attest_running(spec, handle) == handle
    assert fake_docker.old[old_id] == retained


@pytest.mark.parametrize("path,value", [
    (("HostConfig", "Devices", 0, "PathInContainer"), "/dev/changed"),
    (("HostConfig", "DeviceRequests", 0, "Count"), 1),
    (("HostConfig", "PortBindings", "80/tcp", 0, "HostPort"), "8081"),
    (("NetworkSettings", "Ports", "80/tcp", 0, "HostIp"), "127.0.0.2"),
    (("NetworkSettings", "Networks", "none", "DNSNames", 0), "changed.example"),
    (("NetworkSettings", "Networks", "none", "IPAMConfig", "Extension", "kept"), False),
])
def test_recovery_attestation_refuses_nested_observation_drift_without_mutation(
        spec, fake_docker, attestor, path, value):
    old_id = fake_docker.add_orphan()
    _stopped_inspect(fake_docker.old[old_id])
    _nested_stopped_quarantine(fake_docker.old[old_id])
    launcher = _launcher(fake_docker, attestor)
    handle = launcher.recover(spec)
    parent = fake_docker.old[old_id]
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = value
    retained = copy.deepcopy(fake_docker.old[old_id])
    calls_before = len(fake_docker.calls)
    with pytest.raises(ProviderRequestError):
        launcher.attest_running(spec, handle)
    assert fake_docker.old[old_id] == retained
    assert all(argv[3] not in {"create", "start", "stop", "update", "rm"}
               for argv in fake_docker.calls[calls_before:])


@pytest.mark.parametrize("change", [
    lambda item: item["State"].update(Status="running", Running=True),
    lambda item: item["State"].update(Status="restarting", Restarting=True),
    lambda item: item["State"].update(Status="created"),
    lambda item: item["State"].update(Running=0),
    lambda item: item["State"].pop("Paused"),
    lambda item: item["State"].update(Dead=True),
    lambda item: item["State"].update(OOMKilled=True),
    lambda item: item.update(State=None),
    lambda item: item.update(Id="f" * 64),
    lambda item: item.update(Image="sha256:" + "f" * 64),
    lambda item: item.update(Name="/another-container"),
    lambda item: item["Config"]["Labels"].update({BOUNDARY + "run": "foreign-run"}),
    lambda item: item["Config"]["Labels"].update({BOUNDARY + "boundary": "foreign"}),
    lambda item: item["Config"]["Labels"].update({BOUNDARY + "task": "changed-task"}),
    lambda item: item["Config"].update(User="0"),
    lambda item: item["HostConfig"]["RestartPolicy"].update(Name="always"),
    lambda item: item["HostConfig"].update(Privileged=True),
    lambda item: item["HostConfig"].update(OomKillDisable=True),
    lambda item: item["HostConfig"]["Mounts"][0].update(Source="foreign-volume"),
    lambda item: item["Mounts"][0].update(Source="/foreign-volume"),
    lambda item: item.update(Mounts=None),
    lambda item: item["NetworkSettings"]["Networks"]["none"].update(EndpointID="f" * 64),
])
def test_recovery_attestation_refuses_changed_predecessor_without_mutation(
        spec, fake_docker, attestor, change):
    old_id = fake_docker.add_orphan()
    launcher = _launcher(fake_docker, attestor)
    handle = launcher.recover(spec)
    change(fake_docker.old[old_id])
    retained = copy.deepcopy(fake_docker.old)
    calls_before = len(fake_docker.calls)
    with pytest.raises(ProviderRequestError):
        launcher.attest_running(spec, handle)
    assert fake_docker.old == retained
    assert all(argv[3] not in {"create", "start", "stop", "update", "rm"}
               for argv in fake_docker.calls[calls_before:])


@pytest.mark.parametrize("stopped", [False, True])
def test_recovery_attestation_refuses_new_owned_overlap(spec, fake_docker, attestor, stopped):
    fake_docker.add_orphan()
    launcher = _launcher(fake_docker, attestor)
    handle = launcher.recover(spec)
    new_id = fake_docker.add_orphan()
    if stopped:
        _stopped_inspect(fake_docker.old[new_id])
    with pytest.raises(ProviderRequestError, match="conflict"):
        launcher.attest_running(spec, handle)


def test_recovery_association_does_not_transfer_to_fresh_launcher(spec, fake_docker, attestor):
    fake_docker.add_orphan()
    handle = _launcher(fake_docker, attestor).recover(spec)
    with pytest.raises(ProviderRequestError, match="conflict"):
        _launcher(fake_docker, attestor).attest_running(spec, handle)


@pytest.mark.parametrize("changed_spec", [False, True])
def test_recovery_association_refuses_other_valid_spec_or_launch_handle(
        spec, fake_docker, attestor, changed_spec):
    fake_docker.add_orphan()
    launcher = _launcher(fake_docker, attestor)
    launcher.recover(spec)
    other_spec = replace(spec, cli_digest="f" * 64) if changed_spec else spec
    other_docker = FakeDocker(other_spec)
    other_handle = _launcher(other_docker, attestor).start(other_spec)
    fake_docker.inspect = other_docker.inspect
    fake_docker.none_network = other_docker.none_network
    with pytest.raises(ProviderRequestError, match="association"):
        launcher.attest_running(other_spec, other_handle)


def test_recovery_attestation_refuses_missing_predecessor(spec, fake_docker, attestor):
    old_id = fake_docker.add_orphan()
    launcher = _launcher(fake_docker, attestor)
    handle = launcher.recover(spec)
    del fake_docker.old[old_id]
    with pytest.raises(ProviderRequestError, match="predecessor"):
        launcher.attest_running(spec, handle)


def test_recovery_attestation_requires_proof_for_new_nonoverlapping_same_run_seat(spec, fake_docker, attestor):
    launcher = _launcher(fake_docker, attestor)
    handle = launcher.recover(spec)
    old_id = _foreign_orphan(fake_docker, spec)
    fake_docker.old[old_id]["Config"]["Labels"][BOUNDARY + "run"] = spec.run_id
    with pytest.raises(ProviderRequestError, match="predecessor"):
        launcher.attest_running(spec, handle)


def test_runtime_transcript_refuses_count_beyond_complete_inventory_bound():
    from shared.lib.fanout.container_runtime import RuntimeTranscript
    observations = [{"argv": ["inspect", "--type", "container", f"{index:064x}"], "stdout": ""}
                    for index in range(16_139)]
    with pytest.raises(ProviderRequestError, match="observations"):
        RuntimeTranscript(observations)


def test_new_start_clears_successful_recovery_association(spec, fake_docker, attestor):
    fake_docker.add_orphan()
    launcher = _launcher(fake_docker, attestor)
    launcher.recover(spec)
    retained = fake_docker.old
    fake_docker.old = {}
    fake_docker.created = False
    fake_docker.inspect = _inspect(spec)
    handle = launcher.start(spec)
    fake_docker.old = retained
    with pytest.raises(ProviderRequestError, match="conflict"):
        launcher.attest_running(spec, handle)


def test_initial_start_refuses_stopped_same_owner_overlap(spec, fake_docker, attestor):
    old_id = fake_docker.add_orphan()
    _stopped_inspect(fake_docker.old[old_id])
    with pytest.raises(ProviderRequestError, match="conflict"):
        _launcher(fake_docker, attestor).start(spec)
    assert "create" not in fake_docker.events


@pytest.mark.parametrize("operation", ["start", "recover"])
def test_failed_new_launch_clears_prior_recovery_association(
        spec, fake_docker, attestor, operation):
    fake_docker.add_orphan()
    launcher = _launcher(fake_docker, attestor)
    handle = launcher.recover(spec)
    with pytest.raises(ProviderRequestError):
        getattr(launcher, operation)(replace(spec, cli_digest="invalid"))
    with pytest.raises(ProviderRequestError, match="conflict"):
        launcher.attest_running(spec, handle)


def test_failed_recovery_inventory_clears_prior_association(spec, fake_docker, attestor):
    fake_docker.add_orphan()
    launcher = _launcher(fake_docker, attestor)
    handle = launcher.recover(spec)
    before = copy.deepcopy(fake_docker.inspect)
    foreign_id = _foreign_orphan(fake_docker, spec, conflict="socket")
    with pytest.raises(ProviderRequestError, match="foreign.*conflict"):
        launcher.recover(spec)
    del fake_docker.old[foreign_id]
    fake_docker.inspect = before
    with pytest.raises(ProviderRequestError, match="conflict"):
        launcher.attest_running(spec, handle)


def test_failed_recovery_create_clears_prior_association(spec, fake_docker, attestor):
    fake_docker.add_orphan()
    launcher = _launcher(fake_docker, attestor)
    handle = launcher.recover(spec)
    before = copy.deepcopy(fake_docker.inspect)
    network_before = copy.deepcopy(fake_docker.none_network)
    fake_docker.create_returncode = 1
    with pytest.raises(ProviderRequestError, match="create operation failed"):
        launcher.recover(spec)
    fake_docker.inspect = before
    fake_docker.none_network = network_before
    with pytest.raises(ProviderRequestError, match="conflict"):
        launcher.attest_running(spec, handle)


def test_running_attestation_accepts_no_spend_created_to_running_delta(spec, fake_docker, attestor):
    launcher = _launcher(fake_docker, attestor)
    handle = launcher.start(spec)
    calls_before = len(fake_docker.calls)
    assert launcher.attest_running(spec, handle) == handle
    assert attestor.calls == [SOCKET, SOCKET, SOCKET]
    assert any(argv[3:5] == ("container", "ls") for argv in fake_docker.calls[calls_before:])
    assert any(argv[3:4] == ("inspect",) and argv[-1] == CONTAINER_ID
               for argv in fake_docker.calls[calls_before:])
    assert any(argv[3:] == ("network", "inspect", "none")
               for argv in fake_docker.calls[calls_before:])
    assert any(argv[3:] == ("info", "--format", "{{json .}}")
               for argv in fake_docker.calls[calls_before:])
    assert all(argv[:3] == DOCKER and argv[3:5] in {
        ("image", "inspect"), ("volume", "inspect"),
        ("container", "ls"), ("inspect", "--type"),
        ("network", "inspect"), ("info", "--format"),
    } for argv in fake_docker.calls[calls_before:])


def test_running_attestation_accepts_unchanged_false_oom_policy_without_info(
        spec, fake_docker, attestor):
    launcher = _launcher(fake_docker, attestor)
    handle = launcher.start(spec)
    fake_docker.inspect["HostConfig"]["OomKillDisable"] = False
    calls_before = len(fake_docker.calls)
    assert launcher.attest_running(spec, handle) == handle
    assert not any(argv[3] == "info" for argv in fake_docker.calls[calls_before:])


@pytest.mark.parametrize("value", [None, True, "false", "missing"])
def test_created_attestation_requires_explicit_false_oom_policy(
        spec, fake_docker, attestor, value):
    host = fake_docker.inspect["HostConfig"]
    if value == "missing":
        del host["OomKillDisable"]
    else:
        host["OomKillDisable"] = value
    with pytest.raises(ProviderRequestError, match="OOM"):
        _launcher(fake_docker, attestor).start(spec)
    assert not _has_start(fake_docker)


@pytest.mark.parametrize("value", [True, "false", "missing"])
def test_running_attestation_refuses_non_null_oom_policy_drift(
        spec, fake_docker, attestor, value):
    launcher = _launcher(fake_docker, attestor)
    handle = launcher.start(spec)
    host = fake_docker.inspect["HostConfig"]
    if value == "missing":
        del host["OomKillDisable"]
    else:
        host["OomKillDisable"] = value
    with pytest.raises(ProviderRequestError, match="OOM"):
        launcher.attest_running(spec, handle)


@pytest.mark.parametrize("change", [
    lambda fake: fake.info.update(CgroupVersion="1"),
    lambda fake: fake.info.update(CgroupVersion=2),
    lambda fake: fake.info.pop("CgroupVersion"),
    lambda fake: fake.info.update(OomKillDisable=True),
    lambda fake: fake.info.update(OomKillDisable="false"),
    lambda fake: fake.info.pop("OomKillDisable"),
    lambda fake: setattr(fake, "info", []),
    lambda fake: setattr(fake, "info_output", b"{invalid json"),
    lambda fake: setattr(fake, "info_output", b" " * (1024 * 1024 + 1)),
    lambda fake: setattr(fake, "info_returncode", 1),
])
def test_running_attestation_refuses_inconsistent_oom_capability(
        spec, fake_docker, attestor, change):
    launcher = _launcher(fake_docker, attestor)
    handle = launcher.start(spec)
    change(fake_docker)
    with pytest.raises(ProviderRequestError, match="OOM|info"):
        launcher.attest_running(spec, handle)
    assert any(argv[3:] == ("info", "--format", "{{json .}}")
               for argv in fake_docker.calls)


@pytest.mark.parametrize("field,value", [
    ("Memory", 0), ("MemorySwap", 0),
    ("PidsLimit", 0), ("NanoCpus", 0),
])
def test_running_attestation_still_refuses_resource_limit_drift(
        spec, fake_docker, attestor, field, value):
    launcher = _launcher(fake_docker, attestor)
    handle = launcher.start(spec)
    fake_docker.inspect["HostConfig"][field] = value
    with pytest.raises(ProviderRequestError, match="resource"):
        launcher.attest_running(spec, handle)


@pytest.mark.parametrize("field,value", [
    ("NetworkID", None), ("EndpointID", None),
    ("NetworkID", "f" * 64), ("EndpointID", "f" * 64),
])
def test_created_attestation_requires_empty_network_ids(spec, fake_docker, attestor, field, value):
    attachment = fake_docker.inspect["NetworkSettings"]["Networks"]["none"]
    if value is None:
        del attachment[field]
    else:
        attachment[field] = value
    with pytest.raises(ProviderRequestError, match="network"):
        _launcher(fake_docker, attestor).start(spec)
    assert not _has_start(fake_docker)


@pytest.mark.parametrize("field,value", [
    ("NetworkID", None), ("EndpointID", None),
    ("NetworkID", ""), ("EndpointID", ""),
    ("NetworkID", "not-hex"), ("EndpointID", "not-hex"),
    ("NetworkID", "f" * 64), ("EndpointID", "f" * 64),
])
def test_running_attestation_refuses_missing_malformed_or_mismatched_ids(
        spec, fake_docker, attestor, field, value):
    launcher = _launcher(fake_docker, attestor)
    handle = launcher.start(spec)
    attachment = fake_docker.inspect["NetworkSettings"]["Networks"]["none"]
    if value is None:
        del attachment[field]
    else:
        attachment[field] = value
    with pytest.raises(ProviderRequestError, match="network"):
        launcher.attest_running(spec, handle)


@pytest.mark.parametrize("change", [
    lambda data: data.update(Name="bridge"),
    lambda data: data.update(Driver="bridge"),
    lambda data: data.update(Id="f" * 64),
    lambda data: data.update(Containers={}),
    lambda data: data["Containers"][CONTAINER_ID].update(Name="other-seat"),
    lambda data: data["Containers"][CONTAINER_ID].update(EndpointID="f" * 64),
    lambda data: data["Containers"][CONTAINER_ID].update(MacAddress="02:00:00:00:00:01"),
    lambda data: data["Containers"][CONTAINER_ID].update(IPv4Address="10.0.0.2/24"),
    lambda data: data["Containers"][CONTAINER_ID].update(IPv6Address="fd00::2/64"),
])
def test_running_attestation_refuses_forged_none_network_inventory(
        spec, fake_docker, attestor, change):
    launcher = _launcher(fake_docker, attestor)
    handle = launcher.start(spec)
    change(fake_docker.none_network)
    with pytest.raises(ProviderRequestError, match="network"):
        launcher.attest_running(spec, handle)


def test_running_attestation_requires_available_none_network_inventory(spec, fake_docker, attestor):
    launcher = _launcher(fake_docker, attestor)
    handle = launcher.start(spec)
    fake_docker.none_network = None
    with pytest.raises(ProviderRequestError, match="network"):
        launcher.attest_running(spec, handle)


@pytest.mark.parametrize("field,value", [
    ("Aliases", ["other"]), ("Links", ["other"]),
    ("DNSNames", ["other"]), ("DriverOpts", {"foo": "bar"}),
    ("IPPrefixLen", 24), ("GlobalIPv6PrefixLen", 64),
    ("MacAddress", "02:00:00:00:00:01"),
])
def test_running_attestation_refuses_extra_network_attachment_data(
        spec, fake_docker, attestor, field, value):
    launcher = _launcher(fake_docker, attestor)
    handle = launcher.start(spec)
    fake_docker.inspect["NetworkSettings"]["Networks"]["none"][field] = value
    with pytest.raises(ProviderRequestError, match="network"):
        launcher.attest_running(spec, handle)


def test_running_attestation_refuses_another_network_attachment(spec, fake_docker, attestor):
    launcher = _launcher(fake_docker, attestor)
    handle = launcher.start(spec)
    fake_docker.inspect["NetworkSettings"]["Networks"]["bridge"] = {}
    with pytest.raises(ProviderRequestError, match="network"):
        launcher.attest_running(spec, handle)


@pytest.mark.parametrize("state_change", [
    {"Status": "exited", "Running": False},
    {"Status": "paused", "Paused": True},
    {"Status": "restarting", "Restarting": True},
    {"Dead": True},
    {"OOMKilled": True},
])
def test_running_attestation_refuses_non_running_state(spec, fake_docker, attestor, state_change):
    launcher = _launcher(fake_docker, attestor)
    handle = launcher.start(spec)
    fake_docker.inspect["State"].update(state_change)
    with pytest.raises(ProviderRequestError):
        launcher.attest_running(spec, handle)


@pytest.mark.parametrize("mutate", [
    lambda item: item.update(Image="sha256:" + "f" * 64),
    lambda item: item["Config"].update(User="0"),
    lambda item: item["Config"]["Labels"].update({BOUNDARY + "cli_digest": "f" * 64}),
    lambda item: item["HostConfig"].update(NetworkMode="bridge"),
    lambda item: item["HostConfig"].update(CgroupnsMode="host"),
    lambda item: item["Mounts"][2].update(Source="/run/fanout/other.sock"),
    lambda item: item["Config"].update(Env=["SNEAK=1"]),
    lambda item: item["HostConfig"].update(AutoRemove=True),
    lambda item: item["Mounts"][0].update(Propagation="rshared"),
])
def test_running_attestation_refuses_security_digest_drift(spec, fake_docker, attestor, mutate):
    launcher = _launcher(fake_docker, attestor)
    handle = launcher.start(spec)
    mutate(fake_docker.inspect)
    with pytest.raises(ProviderRequestError):
        launcher.attest_running(spec, handle)


@pytest.mark.parametrize("mutate", [
    lambda item: item["NetworkSettings"].update(IPAddress="10.0.0.2"),
    lambda item: item["NetworkSettings"].update(Ports={"80/tcp": [{"HostPort": "8080"}]}),
    lambda item: item["NetworkSettings"]["Networks"]["none"].update(IPv6Gateway="fe80::1"),
    lambda item: item["NetworkSettings"]["Networks"]["none"].update(
        IPAMConfig={"IPv4Address": "10.0.0.2"}),
    lambda item: item["NetworkSettings"].update(SandboxID="not-hex"),
    lambda item: item["NetworkSettings"].update(SandboxKey="/proc/1/ns/net"),
])
def test_running_attestation_refuses_network_address_or_port(spec, fake_docker, attestor, mutate):
    launcher = _launcher(fake_docker, attestor)
    handle = launcher.start(spec)
    mutate(fake_docker.inspect)
    with pytest.raises(ProviderRequestError):
        launcher.attest_running(spec, handle)


def test_running_attestation_refuses_changed_private_volume(spec, fake_docker, attestor):
    launcher = _launcher(fake_docker, attestor)
    handle = launcher.start(spec)
    fake_docker.volumes["session"]["Options"] = {"device": "/Users/owner", "o": "bind"}
    with pytest.raises(ProviderRequestError):
        launcher.attest_running(spec, handle)


def test_running_attestation_refuses_changed_guest_socket(spec, fake_docker, attestor):
    launcher = _launcher(fake_docker, attestor)
    handle = launcher.start(spec)
    attestor.second_proof = replace(attestor.proof, uid=0)
    with pytest.raises(ProviderRequestError):
        launcher.attest_running(spec, handle)


@pytest.mark.parametrize("source", [
    "/run/fanout",
    "/var/lib/docker/volumes/" + WORKSPACE + "/_data/secret",
])
def test_running_attestation_refuses_foreign_overlap_without_mutation(spec, fake_docker, attestor, source):
    launcher = _launcher(fake_docker, attestor)
    handle = launcher.start(spec)
    foreign_id = _foreign_bind(fake_docker, spec, source)
    calls_before = len(fake_docker.calls)
    with pytest.raises(ProviderRequestError):
        launcher.attest_running(spec, handle)
    assert fake_docker.old[foreign_id]["State"]["Running"] is True
    assert all(argv[3] not in {"create", "start", "stop", "update"}
               for argv in fake_docker.calls[calls_before:])


def test_running_attestation_excludes_only_validated_exact_self(spec, fake_docker, attestor):
    launcher = _launcher(fake_docker, attestor)
    handle = launcher.start(spec)
    assert launcher.attest_running(spec, handle) == handle
    duplicate_id = fake_docker.add_orphan()
    with pytest.raises(ProviderRequestError):
        launcher.attest_running(spec, handle)
    assert fake_docker.old[duplicate_id]["State"]["Running"] is True


@pytest.mark.parametrize("change", [
    {"inspect_sha256": "f" * 64},
    {"workspace_volume": "foreign-workspace"},
    {"session_volume": "foreign-session"},
])
def test_running_attestation_refuses_mismatched_handle(spec, fake_docker, attestor, change):
    launcher = _launcher(fake_docker, attestor)
    handle = launcher.start(spec)
    with pytest.raises(ProviderRequestError):
        launcher.attest_running(spec, replace(handle, **change))


def test_running_attestation_refuses_another_container_id_in_handle(spec, fake_docker, attestor):
    launcher = _launcher(fake_docker, attestor)
    handle = launcher.start(spec)
    other_id = fake_docker.add_orphan()
    _no_spend_running_inspect(fake_docker.old[other_id])
    fake_docker.none_network["Containers"][other_id] = {
        "Name": fake_docker.old[other_id]["Name"][1:],
        "EndpointID": NONE_ENDPOINT_ID, "MacAddress": "",
        "IPv4Address": "", "IPv6Address": "",
    }
    other_before = copy.deepcopy(fake_docker.old[other_id])
    with pytest.raises(ProviderRequestError, match="launch association"):
        launcher.attest_running(spec, replace(handle, container_id=other_id))
    assert fake_docker.old[other_id] == other_before


def test_running_attestation_requires_authenticated_socket_attestor(spec, fake_docker, attestor):
    handle = _launcher(fake_docker, attestor).start(spec)
    calls_before = len(fake_docker.calls)
    with pytest.raises(ProviderRequestError):
        ContainerLauncher(fake_docker).attest_running(spec, handle)
    assert len(fake_docker.calls) == calls_before
