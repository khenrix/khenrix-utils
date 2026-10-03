"""Container-specific composition of the existing coordinator and fixed runtime."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
from types import SimpleNamespace
from dataclasses import asdict, replace
from pathlib import Path

from .artifacts import ArtifactExistsError, ArtifactRef, ArtifactStore, canonical_json
from .controller import controller_directory, read_evidence, write_evidence
from .errors import CollaborationDurabilityError, CollaborationValidationError
from .repo import _materialize_worktree, validate_seat_workspace_baseline
from .runstate import RunInputs, _read_json_file
from .container_transfer import TransferIdentity, snapshot_manifest


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _scope_material(controller, value):
    raw = canonical_json(value)
    with ArtifactStore(controller_directory(controller, "quarantine") / "runtime-proofs") as store:
        ref = ArtifactRef(f"scope/{_sha(raw)}.json", _sha(raw), len(raw))
        try:
            return store.write_bytes(ref.path, raw)
        except ArtifactExistsError:
            store.read_bytes(ref)
            return ref


def _read_scope_material(controller, value, *, canonical=True):
    ref = ArtifactRef(**value)
    with ArtifactStore.open_existing(controller_directory(controller, "quarantine") / "runtime-proofs") as store:
        raw = store.read_bytes(ref)
    decoded = json.loads(raw)
    if canonical and canonical_json(decoded) != raw:
        raise ValueError("canonical lifecycle material")
    return decoded


def _scope_context(controller, ref, spec, inputs_digest, source, permit):
    context = read_evidence(controller, "container-coordinator-context", *ref)
    if (context["schema_version"] != "fanout-container-coordinator-context-v1"
            or context["release_source_binding"] != source
            or context["inputs_digest"] != inputs_digest
            or tuple(context[field] for field in ("run_id", "task_id", "target_id", "seat_id")) != (
                spec.run_id, spec.task_id, spec.target_id, spec.seat_id)
            or any(getattr(permit, field) != context["request"][field] for field in (
                "run_id", "task_id", "target_id", "seat_id", "inputs_digest", "session_id"))
            or permit.provider != context["request"]["executor_id"]
            or permit.route != ("resume" if context["request"]["resume"] else "initial")
            or permit.upstream_route_digest is None):
        raise ValueError("exact lifecycle turn context")
    return context


def _read_scope_member(controller, ref, opening_ref, opening):
    from .broker import BrokerPermit
    from .container_runtime import ContainerHandle, ContainerSpec, decode_launch_record
    member = read_evidence(controller, "container-coordinator-member", *ref)
    if (set(member) != {"schema_version", "opening", "spec", "handle", "launch", "context",
                       "permit", "permit_id", "permit_epoch", "lease"}
            or member["schema_version"] != "fanout-container-coordinator-member-v1"
            or member["opening"] != list(opening_ref)):
        raise ValueError("original lifecycle member")
    spec, handle = ContainerSpec(**member["spec"]), ContainerHandle(**member["handle"])
    permit = BrokerPermit(**member["permit"])
    context = _scope_context(controller, member["context"], spec, opening["inputs_digest"],
                             opening["release_source_binding"], permit)
    launch = read_evidence(controller, "container-seat-launch", *member["launch"])
    recovery = decode_launch_record(launch, spec, handle, recover=context["request"]["resume"])
    lease = member["lease"]
    expected_lease = controller_directory(controller, "locks") / (
        f"relay-{member['permit_id']}-{member['permit_epoch']}.lock")
    if (spec.run_id != opening["run_id"] or set(lease) != {"path", "device", "inode"}
            or lease["path"] != str(expected_lease)
            or any(type(lease[field]) is not int or lease[field] <= 0 for field in ("device", "inode"))
            or type(member["permit_epoch"]) is not int
            or member["permit_epoch"] != (context["attempt"] - 1) * 3 + context["round"]
            or len(member["permit_id"]) != 64
            or any(char not in "0123456789abcdef" for char in member["permit_id"])):
        raise ValueError("original lifecycle lease association")
    return member, spec, handle, recovery


def _completed_scope_member(controller, member_ref, stop_ref, opening_ref, opening):
    from .container_runtime import ContainerLauncher
    from .container_transfer_transport import reopen_stopped_seat
    member, spec, handle, _ = _read_scope_member(controller, member_ref, opening_ref, opening)
    reopen_stopped_seat(controller, tuple(stop_ref), spec=spec, handle=handle)
    proof = read_evidence(controller, "container-seat-stopped", *stop_ref)
    if [proof["launch_evidence_name"], proof["launch_evidence_digest"]] != member["launch"]:
        raise ValueError("exact completed lifecycle launch")
    raw = _read_scope_material(controller, proof["raw_inspect"], canonical=False)
    if not isinstance(raw, list) or len(raw) != 1:
        raise ValueError("complete stopped lifecycle observation")
    return handle.container_id, ContainerLauncher._stopped_observation(handle.container_id, spec, raw[0])


def _read_lifecycle_snapshot(controller, ref, *, spec, handle, context_ref):
    from .container_source import release_source_binding
    from .container_runtime import _InventoryScope, _MAX_ACTIVE_SEATS, _MAX_INVENTORY_IDS
    snapshot = read_evidence(controller, "container-coordinator-snapshot", *ref)
    if (set(snapshot) != {"schema_version", "opening", "subject", "members"}
            or snapshot["schema_version"] != "fanout-container-coordinator-snapshot-v1"):
        raise ValueError("complete lifecycle snapshot")
    opening_ref = tuple(snapshot["opening"])
    opening = read_evidence(controller, "container-coordinator-opening", *opening_ref)
    if (set(opening) != {"schema_version", "session_id", "run_id", "inputs_digest",
                        "release_source_binding", "cold_baseline"}
            or opening["schema_version"] != "fanout-container-coordinator-opening-v1"
            or opening["run_id"] != spec.run_id
            or opening["release_source_binding"] != release_source_binding().to_dict()
            or not isinstance(opening["session_id"], str) or len(opening["session_id"]) != 64
            or any(char not in "0123456789abcdef" for char in opening["session_id"])):
        raise ValueError("original lifecycle opening")
    baseline = _read_scope_material(controller, opening["cold_baseline"])
    members = _read_scope_material(controller, snapshot["members"])
    if (not isinstance(baseline, list) or len(baseline) > _MAX_INVENTORY_IDS
            or set(members) != {"active", "completed"}
            or not isinstance(members["active"], list) or not 1 <= len(members["active"]) <= _MAX_ACTIVE_SEATS
            or not isinstance(members["completed"], list)
            or len(baseline) + len(members["completed"]) > _MAX_INVENTORY_IDS):
        raise ValueError("bounded lifecycle members")
    stopped = [tuple(entry) for entry in baseline]
    active = []
    subject = None
    seen_refs = set()
    for member_ref in members["active"]:
        if tuple(member_ref) in seen_refs:
            raise ValueError("duplicate lifecycle member")
        seen_refs.add(tuple(member_ref))
        member, peer_spec, peer_handle, _ = _read_scope_member(controller, member_ref, opening_ref, opening)
        active.append((peer_spec, peer_handle))
        if member_ref == snapshot["subject"]:
            if (peer_spec != spec or peer_handle != handle or member["context"] != list(context_ref)):
                raise ValueError("exact lifecycle subject")
            subject = member
    for member_ref, stop_ref in members["completed"]:
        if tuple(member_ref) in seen_refs:
            raise ValueError("duplicate lifecycle member")
        seen_refs.add(tuple(member_ref))
        stopped.append(_completed_scope_member(controller, member_ref, stop_ref, opening_ref, opening))
    if subject is None:
        raise ValueError("missing lifecycle subject")
    return _InventoryScope(tuple(stopped), tuple(active)), subject


def _replay_lifecycle_runtime(controller, snapshot_ref, runtime_ref, *, spec, handle, context_ref, socket):
    from .container_runtime import ContainerLauncher, RuntimeTranscript, decode_launch_record
    scope, subject = _read_lifecycle_snapshot(controller, snapshot_ref,
        spec=spec, handle=handle, context_ref=context_ref)
    observations = RuntimeTranscript(_read_scope_material(controller, runtime_ref), active_peers=len(scope.active) - 1)
    launcher = ContainerLauncher(guest_socket_attestor=lambda _: socket)
    launcher._run = observations.replay
    launch = read_evidence(controller, "container-seat-launch", *subject["launch"])
    launcher._recovery = decode_launch_record(launch, spec, handle, recover=launch["route"] == "recover")
    launcher.attest_running(spec, handle, scope=scope)
    observations.require_complete()
    return subject


class _CoordinatorLifecycle:
    """Current journal ownership only; archived snapshots never repopulate this registry."""

    def __init__(self, coordinator):
        from .container_source import release_source_binding
        self.coordinator = coordinator
        self.journal = coordinator.journal
        self.controller = coordinator.lifecycle_controller
        self.guard = coordinator._journal_guard
        self.session_id = secrets.token_hex(32)
        self.source = release_source_binding().to_dict()
        self.opening = self.opening_ref = None
        self.active = {}
        self.completed = {}
        self.pending = {}
        self.blocked = False

    def authorize(self, *, cleanup=False):
        from .container_source import release_source_binding
        self.journal.authorize_owner(self.coordinator.owner)
        stored = RunInputs.from_dict(_read_json_file(
            self.journal._root_fd, self.journal._INPUTS_FILE, self.journal.limits.max_snapshot_bytes))
        if (self.coordinator.journal is not self.journal or self.coordinator._journal_guard is not self.guard
                or self.coordinator.lifecycle_controller is not self.controller
                or getattr(self.journal, "_container_lifecycle", None) is not self
                or stored != self.journal.inputs or release_source_binding().to_dict() != self.source
                or self.blocked and not cleanup):
            raise CollaborationDurabilityError("current container lifecycle owner is unavailable")
        if self.opening_ref is not None and read_evidence(self.controller, "container-coordinator-opening",
                                                        *self.opening_ref) != self.opening:
            raise CollaborationDurabilityError("current container lifecycle opening changed")

    def reserve(self, context_ref, spec, ledger, permit, epoch):
        from .container_runtime import ContainerLauncher, _MAX_ACTIVE_SEATS
        with self.guard:
            self.authorize()
            ContainerLauncher._validate_spec(spec)
            context = _scope_context(self.controller, context_ref, spec, self.journal.inputs.digest, self.source, permit)
            if (ledger.controller is not self.controller or ledger.root != self.controller.root / "locks/spend"
                    or epoch != (context["attempt"] - 1) * 3 + context["round"]):
                raise CollaborationDurabilityError("exact lifecycle permit epoch changed")
            if len(self.active) + len(self.pending) >= _MAX_ACTIVE_SEATS:
                raise CollaborationDurabilityError("current container lifecycle seat budget exceeded")
            if any(entry["spec"] == spec for entry in self.active.values()) or any(
                    entry[0] == spec for entry in self.pending.values()):
                raise CollaborationDurabilityError("container lifecycle seat is already active")
            try:
                permit_id = ledger.register(permit, epoch)
            except BaseException as error:
                self.fail(spec, None, None, error)
                raise
            self.pending[(permit_id, epoch)] = spec, tuple(context_ref), permit, ledger
            return permit_id

    def _control(self, spec, context_ref, control):
        from .container_boundary import HostSessionLease, PinnedSSHSeatControl
        from .container_broker import PermitLedger
        if (type(control) is not PinnedSSHSeatControl or control.controller is not self.controller
                or control.spec != spec or type(control._lease) is not HostSessionLease
                or not isinstance(control.ledger, PermitLedger)
                or control.ledger.controller is not self.controller
                or control.ledger.root != self.controller.root / "locks/spend"):
            raise CollaborationDurabilityError("container lifecycle relay association changed")
        expected = self.pending.get((control.permit_id, control.epoch))
        if expected is not None and expected != (spec, tuple(context_ref), control.broker.permit, control.ledger):
            raise CollaborationDurabilityError("container lifecycle pending association changed")
        _scope_context(self.controller, context_ref, spec, self.journal.inputs.digest, self.source, control.broker.permit)
        control.ledger.require_permit(control.permit_id, control.epoch, control.broker.permit)
        return control._lease.require_held()

    def _inventory(self):
        from .container_runtime import _InventoryScope
        for entry in self.active.values():
            member, _, _, _ = _read_scope_member(self.controller, entry["ref"], self.opening_ref, self.opening)
            transport = entry["transport"]
            lease = self._control(entry["spec"], member["context"], getattr(transport, "_control", None))
            if (member != entry["member"] or transport.spec != entry["spec"] or transport.handle != entry["handle"]
                    or [transport.handle_evidence_name, transport.handle_evidence_digest] != member["launch"]
                    or lease != member["lease"]):
                raise CollaborationDurabilityError("live container lifecycle member changed")
        baseline = _read_scope_material(self.controller, self.opening["cold_baseline"])
        completed = []
        for cid, entry in self.completed.items():
            observed = _completed_scope_member(self.controller, entry["ref"], entry["stop"], self.opening_ref, self.opening)
            if observed != (cid, entry["digest"]):
                raise CollaborationDurabilityError("completed lifecycle proof changed")
            completed.append(observed)
        stopped = tuple(tuple(entry) for entry in baseline) + tuple(completed)
        return _InventoryScope(stopped, tuple((entry["spec"], entry["handle"]) for entry in self.active.values()))

    def prepare(self, launcher, spec, context_ref, control):
        self.authorize()
        self._control(spec, context_ref, control)
        if (control.permit_id, control.epoch) not in self.pending:
            raise CollaborationDurabilityError("container lifecycle lacks exact pending permit")
        if self.opening is None:
            launcher._validate_spec(spec)
            launcher._image_id(spec)
            sources = (launcher._volume(spec, "workspace", create=True), launcher._volume(spec, "session", create=True))
            baseline = launcher._inventory(spec, sources, fence_owned=True)
            # The ledger lock is released before every Docker operation above.
            with control.ledger._locked() as state:
                abandoned = [(permit_id, entry["epoch"]) for permit_id, entry in state["permits"].items()
                             if (permit_id, entry["epoch"]) not in self.pending and not entry["fenced"]]
            for permit_id, epoch in abandoned:
                control.ledger.fence_epoch(permit_id, epoch)
            baseline_ref = _scope_material(self.controller, list(baseline))
            self.opening = {"schema_version": "fanout-container-coordinator-opening-v1",
                "session_id": self.session_id, "run_id": self.journal.inputs.run_id,
                "inputs_digest": self.journal.inputs.digest, "release_source_binding": self.source,
                "cold_baseline": asdict(baseline_ref)}
            self.opening_ref = write_evidence(self.controller, "container-coordinator-opening", self.opening)
        return self._inventory()

    def register(self, transport, context_ref):
        self.authorize()
        control = transport._control
        lease = self._control(transport.spec, context_ref, control)
        member = {"schema_version": "fanout-container-coordinator-member-v1", "opening": list(self.opening_ref),
            "spec": asdict(transport.spec), "handle": asdict(transport.handle),
            "launch": [transport.handle_evidence_name, transport.handle_evidence_digest], "context": list(context_ref),
            "permit": asdict(control.broker.permit), "permit_id": control.permit_id,
            "permit_epoch": control.epoch, "lease": lease}
        ref = write_evidence(self.controller, "container-coordinator-member", member)
        decoded, _, _, _ = _read_scope_member(self.controller, ref, self.opening_ref, self.opening)
        if decoded != member or transport.handle.container_id in self.active:
            raise CollaborationDurabilityError("container lifecycle registration changed")
        self.active[transport.handle.container_id] = {"ref": ref, "member": member, "spec": transport.spec,
                                                    "handle": transport.handle, "transport": transport}
        del self.pending[(control.permit_id, control.epoch)]

    def snapshot(self, transport):
        self.authorize()
        self._inventory()
        entry = self.active.get(transport.handle.container_id)
        if entry is None or entry["transport"] is not transport:
            raise CollaborationDurabilityError("container lifecycle subject is not active")
        members = _scope_material(self.controller, {
            "active": [list(entry["ref"]) for entry in self.active.values()],
            "completed": [[list(entry["ref"]), list(entry["stop"])] for entry in self.completed.values()],
        })
        ref = write_evidence(self.controller, "container-coordinator-snapshot", {
            "schema_version": "fanout-container-coordinator-snapshot-v1", "opening": list(self.opening_ref),
            "subject": list(entry["ref"]), "members": asdict(members),
        })
        inventory, _ = _read_lifecycle_snapshot(self.controller, ref, spec=transport.spec, handle=transport.handle,
                                               context_ref=entry["member"]["context"])
        return ref, inventory

    def complete(self, transport):
        cid = transport.handle.container_id
        if cid in self.completed:
            if self.completed[cid]["stop"] != transport.stop_evidence:
                raise CollaborationDurabilityError("completed container lifecycle stop changed")
            return
        entry = self.active.get(cid)
        if entry is None or entry["transport"] is not transport or transport.stop_evidence is None:
            raise CollaborationDurabilityError("container lifecycle stop is not registered")
        observed_cid, digest = _completed_scope_member(self.controller, entry["ref"], transport.stop_evidence,
                                                       self.opening_ref, self.opening)
        if observed_cid != cid:
            raise CollaborationDurabilityError("exact completed lifecycle container changed")
        transport._control.ledger.fence_epoch(entry["member"]["permit_id"], entry["member"]["permit_epoch"])
        self.completed[cid] = {"ref": entry["ref"], "stop": transport.stop_evidence, "digest": digest}
        del self.active[cid]

    def fail(self, spec, handle, control, error):
        from .container_boundary import SessionStore, _cleanup_failure_note
        self.blocked = True
        store = None
        provider = permit_id = None
        if control is not None:
            provider, permit_id = control.broker.permit.provider, control.permit_id
            store = SessionStore(self.controller, spec.run_id, spec.task_id, spec.target_id, spec.seat_id, spec.session_volume)
        error.add_note(_cleanup_failure_note(self.controller, "coordinator-lifecycle", error,
            spec=spec, handle=handle, permit_id=permit_id, provider=provider, store=store))


def _coordinator_lifecycle(coordinator):
    with coordinator._journal_guard:
        coordinator.journal.authorize_owner(coordinator.owner)
        scope = getattr(coordinator.journal, "_container_lifecycle", None)
        if scope is None:
            scope = _CoordinatorLifecycle(coordinator)
            coordinator.journal._container_lifecycle = scope
        if type(scope) is not _CoordinatorLifecycle or scope.coordinator is not coordinator:
            raise CollaborationDurabilityError("journal already has another container lifecycle owner")
        scope.authorize()
        return scope


def authenticate_inputs(coordinator, packet):
    journal = coordinator.journal
    try:
        journal.authorize_owner(coordinator.owner)
        stored = RunInputs.from_dict(_read_json_file(
            journal._root_fd, journal._INPUTS_FILE, journal.limits.max_snapshot_bytes))
        if (stored != journal.inputs or packet.run_inputs != stored
                or packet.run_id != stored.run_id
                or packet.compiled_plan_sha256 != stored.compiled_plan_sha256
                or packet.skill_manifest_sha256 != stored.skill_manifests.get(packet.task_id)):
            raise ValueError("immutable run inputs")
        baseline = coordinator._baseline_for_packet(packet, stored)
        return stored, baseline
    except Exception as error:
        raise CollaborationDurabilityError("container immutable input authority changed") from error


def transcript_store(coordinator) -> ArtifactStore:
    controller = coordinator.lifecycle_controller
    expected = controller_directory(controller, "quarantine") / "provider-transcripts"
    store = getattr(coordinator, "_container_transcripts", None)
    evidence = getattr(coordinator, "_container_transcript_evidence", None)
    if store is None:
        if evidence is not None:
            raise CollaborationDurabilityError("container transcript store was replaced")
        path = f"runs/{coordinator.journal.inputs.run_id}/container-transcripts.json"
        try:
            persisted = coordinator._read_named(path, required=False)
            if persisted is None:
                if expected.exists():
                    raise ValueError("unbound existing transcript directory")
                store = ArtifactStore(expected)
                info = os.stat(expected, follow_symlinks=False)
                evidence = write_evidence(controller, "container-transcript-store", {
                    "run_id": coordinator.journal.inputs.run_id,
                    "inputs_digest": coordinator.journal.inputs.digest,
                    "root": str(expected), "device": info.st_dev, "inode": info.st_ino,
                })
                coordinator._write_exact(path, canonical_json({"evidence": list(evidence)}))
            else:
                value = json.loads(persisted[1])
                if set(value) != {"evidence"} or canonical_json(value) != persisted[1]:
                    raise ValueError("transcript reference")
                evidence = tuple(value["evidence"])
                if len(evidence) != 2 or not expected.exists():
                    raise ValueError("transcript authority")
                store = ArtifactStore(expected)
        except Exception as error:
            if store is not None:
                store.close()
            raise CollaborationDurabilityError("container transcript authority cannot be reopened") from error
        coordinator._container_transcripts = store
        coordinator._container_transcript_evidence = evidence
    try:
        value = read_evidence(controller, "container-transcript-store", *evidence)
        fd = store._root_fd_copy()
        try:
            info = os.fstat(fd)
        finally:
            os.close(fd)
        path_info = os.stat(expected, follow_symlinks=False)
        if (store.root != expected or stat.S_IMODE(info.st_mode) != 0o700
                or info.st_uid != os.getuid() or not stat.S_ISDIR(info.st_mode)
                or (path_info.st_dev, path_info.st_ino) != (info.st_dev, info.st_ino)
                or value != {"run_id": coordinator.journal.inputs.run_id,
                    "inputs_digest": coordinator.journal.inputs.digest, "root": str(expected),
                    "device": info.st_dev, "inode": info.st_ino}):
            raise ValueError("transcript root")
        return store
    except Exception as error:
        raise CollaborationDurabilityError("container private transcript store changed") from error


def _certificate(coordinator):
    from .container_certification import _owner_paths, read_certificate
    trial = getattr(coordinator, "trial_certificate", None)
    return trial if trial is not None else read_certificate(_owner_paths()[2])


def admit_profile(coordinator, inputs, seat, policy):
    from .container_certification import TrialCertificate, _owner_authority
    from .container_source import release_source_binding
    try:
        certificate = _certificate(coordinator)
        roots = tuple(binding.root for _, binding in sorted(inputs.targets.items()))
        authority = _owner_authority()
        admit = authority.admit_trial_profile if type(certificate) is TrialCertificate else authority.admit_release_profile
        profile = admit(certificate, roots, seat.executor_id,
            source_hash=release_source_binding().digest, quality_tier=policy.quality_tier)
        if (inputs.provider_profiles.get(profile.binding_key) != profile.digest
                or coordinator.registry.select(*profile.key).digest != profile.digest):
            raise ValueError("planner/executor profile")
        return profile
    except Exception as error:
        raise CollaborationValidationError("certificate-bound container profile admission failed") from error


def bind_turn_context(coordinator, packet, seat, round, request, peer):
    from .container_source import release_source_binding
    from .container_transfer_transport import admitted_request_digest
    try:
        inputs, baseline = authenticate_inputs(coordinator, packet)
        coordinator._verify_staged_seat(seat, packet)
        private = transcript_store(coordinator)
        if (request.artifact_store is not private
                or request.artifact_prefix != coordinator._provider_prefix(packet, round, seat.seat_id)
                or request.prompt_bytes != coordinator._prompt(packet, seat, round, peer)
                or request.inputs_digest != inputs.digest or Path(request.cwd) != baseline.repository):
            raise ValueError("exact prompt or transcript")
        peer_ref = None
        if round > 1:
            if (peer is None or peer.artifact_ref is None or not request.resume
                    or peer.target_session_id != request.session_id
                    or (peer.run_id, peer.task_id, peer.attempt, peer.target_round, peer.target_seat_id) !=
                       (packet.run_id, packet.task_id, packet.attempt, round, seat.seat_id)
                    or coordinator.artifacts.read_bytes(peer.artifact_ref) != peer.payload):
                raise ValueError("persisted peer")
            peer_ref = asdict(peer.artifact_ref)
        elif peer is not None or request.resume:
            raise ValueError("blind initial")
        prompt_ref = ArtifactRef(request.artifact_prefix + "/prompt.bin", _sha(request.prompt_bytes), len(request.prompt_bytes))
        try:
            private.write_bytes(prompt_ref.path, request.prompt_bytes)
        except ArtifactExistsError:
            if private.read_bytes(prompt_ref) != request.prompt_bytes:
                raise ValueError("persisted prompt bytes")
        controls = {name: getattr(request, name) for name in (
            "executor_id", "execution_class", "session_id", "resume", "timeout",
            "timeout_override_reason", "timeout_override_review_sha256", "retries", "artifact_prefix",
            "slot_cap", "slot_timeout", "context_sha256", "skill_bundle_sha256", "skill_delivery_sha256",
            "run_id", "task_id", "target_id", "seat_id", "inputs_digest")}
        controls.update({name: str(getattr(request, name)) if getattr(request, name) is not None else None
                         for name in ("cwd", "slot_root", "staged_skill_root")})
        return write_evidence(coordinator.lifecycle_controller, "container-coordinator-context", {
            "schema_version": "fanout-container-coordinator-context-v1",
            "run_id": packet.run_id, "task_id": packet.task_id, "target_id": request.target_id,
            "seat_id": seat.seat_id, "attempt": packet.attempt, "round": round,
            "inputs_digest": inputs.digest, "baseline_sha256": baseline.digest,
            "task_packet_sha256": _sha(canonical_json(packet.to_dict())),
            "prompt_sha256": _sha(request.prompt_bytes), "prompt": asdict(prompt_ref),
            "request": controls,
            "request_digest": admitted_request_digest(request),
            "peer": peer_ref, "imported_peer": False,
            "transcript_store": list(coordinator._container_transcript_evidence),
            "artifact_prefix": request.artifact_prefix,
            "release_source_binding": release_source_binding().to_dict(),
        })
    except Exception as error:
        raise CollaborationDurabilityError("container turn context authority changed") from error


def stage_input_material(coordinator, packet, seat, request, handle):
    from .collaboration import build_verified_dependency_context, MAX_DEPENDENCY_BYTES, _delivery_evidence_bytes
    try:
        inputs, baseline = authenticate_inputs(coordinator, packet)
        coordinator._verify_staged_seat(seat, packet)
        if coordinator.artifacts.read_bytes(seat.delivery_evidence_ref) != _delivery_evidence_bytes(seat.admission.engine_evidence):
            raise ValueError("skill delivery")
        validate_seat_workspace_baseline(baseline, coordinator.lifecycle_controller,
                                         seat.workspace_verification)
        identity = TransferIdentity(packet.run_id, packet.task_id, packet.target_binding.spec.id,
                                    seat.seat_id, inputs.digest)
        if (request.run_id, request.task_id, request.target_id, request.seat_id, request.inputs_digest) != (
                identity.run_id, identity.task_id, identity.target_id, identity.seat_id, identity.inputs_digest):
            raise ValueError("request identity")
        dependencies = build_verified_dependency_context(packet.dependency_records,
            store=coordinator.artifacts, scheduler=coordinator.dependency_scheduler,
            max_bytes=MAX_DEPENDENCY_BYTES)
        skill_files = coordinator._staged_skill_context(seat)
        files = {
            "run-inputs.json": canonical_json(inputs.to_dict()),
            "task-packet.json": canonical_json(packet.to_dict()),
            "compiled-plan.json": packet.compiled_plan, "source.md": packet.source_markdown,
            "task.json": packet.task, "provider-context.json": canonical_json(packet.context_document()),
            "skill-bundle.json": packet.skill_bundle, "skill-files.json": skill_files,
            "skill-delivery.json": coordinator.artifacts.read_bytes(seat.delivery_evidence_ref),
            "dependencies.json": dependencies,
        }
        source = controller_directory(coordinator.lifecycle_controller, "quarantine") / (
            "input-" + _sha(canonical_json(asdict(identity))))
        fresh = not source.exists()
        if fresh:
            os.mkdir(source, 0o700)
            os.mkdir(source / "repo", 0o700)
            os.mkdir(source / "context", 0o700)
            _materialize_worktree(source / "repo", baseline.entries, baseline.directories)
            for name, raw in files.items():
                fd = os.open(source / "context" / name,
                             os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as output:
                    output.write(raw)
                    output.flush()
                    os.fsync(output.fileno())
        manifest = snapshot_manifest(source, identity)
        expected_files = {"repo/" + entry.path: (entry.kind, entry.mode & 0o777, _sha(entry.data), len(entry.data))
                          for entry in baseline.entries}
        expected_files.update({"context/" + name: ("file", 0o600, _sha(raw), len(raw)) for name, raw in files.items()})
        expected_dirs = {"repo": 0o700, "context": 0o700,
                         **{"repo/" + entry.path: entry.mode for entry in baseline.directories}}
        actual_files = {entry.path: (entry.kind, entry.mode, entry.sha256, entry.length)
                        for entry in manifest.entries if entry.kind != "directory"}
        if (actual_files != expected_files or
                {entry.path: entry.mode for entry in manifest.entries if entry.kind == "directory"} != expected_dirs):
            raise ValueError("complete source inventory")
        store_root = controller_directory(coordinator.lifecycle_controller, "quarantine") / "input-material-store"
        with ArtifactStore(store_root) as store:
            raw = canonical_json(asdict(manifest))
            ref = ArtifactRef(f"expected/{_sha(raw)}.json", _sha(raw), len(raw))
            try:
                store.write_bytes(ref.path, raw)
            except ArtifactExistsError:
                store.read_bytes(ref)
        info = os.stat(source, follow_symlinks=False)
        material = {
            "schema_version": "InputMaterialV1", "identity": asdict(identity),
            "source_device": info.st_dev, "source_inode": info.st_ino,
            "original_handle": asdict(handle), "workspace_volume": handle.workspace_volume,
            "run_inputs_sha256": inputs.digest, "task_packet_sha256": _sha(files["task-packet.json"]),
            "target_baseline_sha256": baseline.digest,
            "workspace_sha256": seat.workspace_verification.evidence_digest,
            "skill_delivery_sha256": seat.delivery_evidence_sha256,
            "dependency_context_sha256": _sha(dependencies),
            "provider_context_sha256": packet.context_sha256,
            "skill_bundle_sha256": packet.skill_bundle_sha256, "manifest_ref": asdict(ref),
        }
        name, digest = write_evidence(coordinator.lifecycle_controller, "container-input-material", material)
        return source, name, digest
    except (CollaborationDurabilityError, CollaborationValidationError):
        raise
    except Exception as error:
        raise CollaborationValidationError("complete container input material is invalid") from error


def persist_export_candidate(coordinator, packet, seat, round, request, result, completion_evidence):
    from .container_boundary import _turn_digest
    from .container_source import release_source_binding
    from .container_transfer_transport import admitted_request_digest, reopen_candidate_export
    from .verification import create_candidate_from_export, source_candidate_artifact_path
    try:
        inputs, baseline = authenticate_inputs(coordinator, packet)
        controller = coordinator.lifecycle_controller
        completion = read_evidence(controller, "container-coordinator-turn", *completion_evidence)
        if (completion["schema_version"] != "fanout-container-coordinator-turn-v1"
                or completion["request_digest"] != admitted_request_digest(request)
                or completion["task_packet_sha256"] != _sha(canonical_json(packet.to_dict()))
                or completion["release_source_binding"] != release_source_binding().to_dict()):
            raise ValueError("turn completion")
        boundary = read_evidence(controller, "container-seat-admission", *completion["boundary"])
        turn_digest = _turn_digest(request, SimpleNamespace(**boundary))
        if boundary["request_digest"] != admitted_request_digest(request) or turn_digest != completion["turn_digest"]:
            raise ValueError("turn admission")
        identity = TransferIdentity(packet.run_id, packet.task_id, packet.target_binding.spec.id,
                                    seat.seat_id, inputs.digest)
        receipt = result.candidate_export
        if (receipt is None or (receipt.evidence_name, receipt.evidence_digest) != tuple(completion["export"])
                or receipt.identity.inputs_digest != inputs.digest
                or baseline.digest != inputs.targets[receipt.identity.target_id].baseline_sha256):
            raise ValueError("target baseline or export")
        reopened = reopen_candidate_export(controller, receipt, identity=identity,
            turn_digest=turn_digest, stop_evidence=tuple(completion["stop"]))
        candidate = create_candidate_from_export(baseline, reopened.root, identity,
                                                 reopened.manifest_sha256)
        path = source_candidate_artifact_path(packet.run_id, packet.task_id, packet.attempt, round, seat.seat_id)
        ref = coordinator._write_exact(path, candidate.manifest_bytes)
        authority = write_evidence(controller, "container-source-candidate", {
            "run_id": packet.run_id, "task_id": packet.task_id, "seat_id": seat.seat_id,
            "attempt": packet.attempt, "round": round, "target_id": identity.target_id,
            "inputs_digest": inputs.digest, "baseline_sha256": baseline.digest,
            "candidate": asdict(ref), "completion": list(completion_evidence),
        })
        coordinator._write_exact(path + ".authority.json", canonical_json({"evidence": list(authority)}))
        return ref
    except Exception as error:
        raise CollaborationDurabilityError("container terminal candidate lacks authenticated export authority") from error


def persist_turn_completion(coordinator, packet, seat, round, request, result):
    from .container_source import release_source_binding
    from .container_transfer_transport import admitted_request_digest
    try:
        authenticate_inputs(coordinator, packet)
        proof = read_evidence(coordinator.lifecycle_controller, "container-coordinator-turn", *result.container_completion)
        if (proof["schema_version"] != "fanout-container-coordinator-turn-v1"
                or proof["request_digest"] != admitted_request_digest(request)
                or proof["task_packet_sha256"] != _sha(canonical_json(packet.to_dict()))
                or proof["release_source_binding"] != release_source_binding().to_dict()
                or proof["session_id"] != result.session_id or proof["valid"] is not result.valid
                or proof["process"]["stdout"] != asdict(result.stdout_ref)
                or proof["process"]["stderr"] != asdict(result.stderr_ref)
                or proof["process"]["answer"] != (asdict(result.answer_ref) if result.answer_ref else None)):
            raise ValueError("fixed completion captures")
        context = read_evidence(coordinator.lifecycle_controller, "container-coordinator-context", *proof["context"])
        if (context["request_digest"] != proof["request_digest"]
                or context["inputs_digest"] != coordinator.journal.inputs.digest
                or context["prompt_sha256"] != _sha(request.prompt_bytes)):
            raise ValueError("fixed completion context")
        coordinator._write_exact(coordinator._provider_prefix(packet, round, seat.seat_id) + "/container-turn.json",
                                 canonical_json({"evidence": list(result.container_completion)}))
    except Exception as error:
        raise CollaborationDurabilityError("container terminal lacks fixed authenticated process completion") from error


def reopen_source_candidate(coordinator, packet, seat_id, round, ref):
    from .container_source import release_source_binding
    from .container_transfer_transport import CandidateExportReceipt, reopen_candidate_export
    from .verification import create_candidate_from_export, source_candidate_artifact_path
    try:
        inputs, baseline = authenticate_inputs(coordinator, packet)
        path = source_candidate_artifact_path(packet.run_id, packet.task_id, packet.attempt, round, seat_id)
        if ref.path != path:
            raise ValueError("candidate association")
        exact = coordinator._read_named(path + ".authority.json", required=True)
        saved = json.loads(exact[1])
        if set(saved) != {"evidence"} or canonical_json(saved) != exact[1]:
            raise ValueError("candidate reference")
        controller = coordinator.lifecycle_controller
        proof = read_evidence(controller, "container-source-candidate", *saved["evidence"])
        if ({key: proof[key] for key in ("run_id", "task_id", "seat_id", "attempt", "round", "target_id",
                "inputs_digest", "baseline_sha256", "candidate")} != {
                    "run_id": packet.run_id, "task_id": packet.task_id, "seat_id": seat_id,
                    "attempt": packet.attempt, "round": round, "target_id": packet.target_binding.spec.id,
                    "inputs_digest": inputs.digest, "baseline_sha256": baseline.digest, "candidate": asdict(ref)}):
            raise ValueError("candidate original authority")
        completion = read_evidence(controller, "container-coordinator-turn", *proof["completion"])
        if (completion["schema_version"] != "fanout-container-coordinator-turn-v1"
                or completion["task_packet_sha256"] != _sha(canonical_json(packet.to_dict()))
                or completion["release_source_binding"] != release_source_binding().to_dict()):
            raise ValueError("candidate source authority")
        exported = read_evidence(controller, "container-candidate-export", *completion["export"])
        exported["identity"] = TransferIdentity(**exported["identity"])
        exported["root"] = Path(exported["root"])
        receipt = CandidateExportReceipt(**exported,
            evidence_name=completion["export"][0], evidence_digest=completion["export"][1])
        identity = TransferIdentity(packet.run_id, packet.task_id, packet.target_binding.spec.id, seat_id, inputs.digest)
        reopened = reopen_candidate_export(controller, receipt, identity=identity,
            turn_digest=completion["turn_digest"], stop_evidence=tuple(completion["stop"]))
        candidate = create_candidate_from_export(baseline, reopened.root, identity, reopened.manifest_sha256)
        if candidate.manifest_bytes != coordinator.artifacts.read_bytes(ref):
            raise ValueError("candidate immutable manifest")
        return candidate
    except Exception as error:
        raise CollaborationDurabilityError("container cold candidate authority changed") from error


def reopen_turn_completion(coordinator, packet, seat, terminal, peer):
    """Read the original prompt, process, import and settled wire proof without sending."""
    from scripts import fanout_container_certify as probe
    from .broker import BrokerPermit
    from .container_boundary import _turn_digest, _authenticated_launch
    from .container_certification import _certificate as parse_certificate, _read_owned_json, _require_certificate_turn_binding
    from .container_broker import PermitLedger, _digest, attempt_limit
    from .container_runtime import ContainerHandle, ContainerSpec
    from .container_source import release_source_binding
    from .container_transfer_transport import InputTransferReceipt, validate_input_receipt, admitted_request_digest, reopen_stopped_seat
    from .container_upstream import HostUpstream
    from .providers import ProviderRequest
    inputs, baseline = authenticate_inputs(coordinator, packet)
    prefix = coordinator._provider_prefix(packet, terminal.round, seat.seat_id)
    exact = coordinator._read_named(prefix + "/container-turn.json", required=True)
    saved = json.loads(exact[1])
    if set(saved) != {"evidence"} or canonical_json(saved) != exact[1]:
        raise ValueError("complete process reference")
    controller, private = coordinator.lifecycle_controller, transcript_store(coordinator)
    proof = read_evidence(controller, "container-coordinator-turn", *saved["evidence"])
    context = read_evidence(controller, "container-coordinator-context", *proof["context"])
    prompt = private.read_bytes(ArtifactRef(**context["prompt"]))
    profile = coordinator.registry.select(seat.executor_id, "repo-write", packet.provider_policy.quality_tier)
    request = ProviderRequest(prompt=prompt, profile=profile, **context["request"])
    if (context["schema_version"] != "fanout-container-coordinator-context-v1"
            or proof["schema_version"] != "fanout-container-coordinator-turn-v1"
            or context["release_source_binding"] != release_source_binding().to_dict()
            or proof["release_source_binding"] != context["release_source_binding"]
            or context["transcript_store"] != list(coordinator._container_transcript_evidence)
            or context["task_packet_sha256"] != _sha(canonical_json(packet.to_dict()))
            or proof["task_packet_sha256"] != context["task_packet_sha256"]
            or context["inputs_digest"] != inputs.digest or context["baseline_sha256"] != baseline.digest
            or context["request_digest"] != admitted_request_digest(request)
            or proof["request_digest"] != context["request_digest"]
            or prompt != coordinator._prompt(packet, seat, terminal.round, peer)
            or context["prompt_sha256"] != _sha(prompt) or context["imported_peer"] is not False
            or context["peer"] != (asdict(peer.artifact_ref) if peer else None)
            or (context["run_id"], context["task_id"], context["target_id"], context["seat_id"], context["attempt"], context["round"]) !=
               (packet.run_id, packet.task_id, packet.target_binding.spec.id, seat.seat_id, packet.attempt, terminal.round)
            or request.executor_id != seat.executor_id or request.cwd != str(baseline.repository)
            or request.resume != (terminal.round > 1) or request.artifact_prefix != prefix
            or request.timeout != terminal.effective_timeout or request.inputs_digest != inputs.digest
            or request.skill_delivery_sha256 != seat.delivery_evidence_sha256
            or request.context_sha256 != packet.context_sha256 or request.skill_bundle_sha256 != packet.skill_bundle_sha256):
        raise ValueError("original complete turn context")
    boundary = read_evidence(controller, "container-seat-admission", *proof["boundary"])
    trial = getattr(coordinator, "trial_certificate", None)
    if trial is None:
        value, _ = _read_owned_json(Path(inputs.targets[packet.target_binding.spec.id].root).parent.parent / "trial-certificate.json")
        trial = parse_certificate(value)
    _require_certificate_turn_binding(request, trial, boundary, proof.get("vm_profile_identity"))
    permit = BrokerPermit(**proof["permit"])
    if (proof["valid"] is not True or proof["session_id"] != terminal.session_id
            or boundary["controller_id"] != controller.controller_id
            or boundary["request_digest"] != proof["request_digest"]
            or proof["turn_digest"] != _turn_digest(request, SimpleNamespace(**boundary))
            or boundary["permit_digest"] != _digest(permit)
            or boundary["permit_id"] != proof["permit_id"] or boundary["permit_epoch"] != proof["permit_epoch"]
            or permit.provider != request.executor_id or permit.model != profile.requested_model
            or any(getattr(permit, name) != getattr(request, name)
                   for name in ("run_id", "task_id", "target_id", "seat_id", "inputs_digest", "session_id"))):
        raise ValueError("fixed admission and permit")
    handle = ContainerHandle(boundary["container_id"], boundary["container_inspect_sha256"],
                             boundary["workspace_volume"], boundary["session_volume"])
    spec = ContainerSpec(request.run_id, request.task_id, request.target_id, request.seat_id,
        boundary["image_digest"], handle.workspace_volume, handle.session_volume, boundary["socket_path"], boundary["cli_digest"])
    _authenticated_launch(controller, request, handle, SimpleNamespace(**boundary))
    from .container_runtime import GuestSocketAttestation
    member = _replay_lifecycle_runtime(controller, proof["lifecycle_snapshot"], proof["runtime_proof"],
        spec=spec, handle=handle, context_ref=proof["context"],
        socket=GuestSocketAttestation(boundary["socket_path"], boundary["socket_uid"], boundary["socket_mode"],
                                      boundary["socket_device"], boundary["socket_inode"], boundary["socket_ctime_ns"]))
    if (member["permit"] != proof["permit"] or member["permit_id"] != proof["permit_id"]
            or member["permit_epoch"] != proof["permit_epoch"]
            or member["launch"] != [boundary["handle_evidence_name"], boundary["handle_evidence_digest"]]):
        raise ValueError("original runtime lifecycle admission")
    stopped = read_evidence(controller, "container-seat-stopped", *proof["stop"])
    if (stopped["launch_evidence_name"], stopped["launch_evidence_digest"]) != (
            boundary["handle_evidence_name"], boundary["handle_evidence_digest"]):
        raise ValueError("original stopped admission")
    reopen_stopped_seat(controller, tuple(proof["stop"]), spec=spec, handle=handle)
    imported = read_evidence(controller, "container-input-transfer", boundary["input_receipt_name"], boundary["input_receipt_digest"])
    receipt = InputTransferReceipt(TransferIdentity(**imported["identity"]), imported["manifest_sha256"],
        imported["workspace_volume"], imported["workdir"], ContainerHandle(**imported["original_handle"]),
        imported["handle_evidence_name"], imported["handle_evidence_digest"], imported["material_evidence_name"],
        imported["material_evidence_digest"], imported["grant_evidence_name"], imported["grant_evidence_digest"],
        boundary["input_receipt_name"], boundary["input_receipt_digest"])
    validate_input_receipt(controller, request, receipt, boundary["workspace_volume"])
    if (boundary["input_manifest_sha256"], boundary["input_workdir"]) != (receipt.manifest_sha256, receipt.workdir):
        raise ValueError("original published input admission")
    process = proof["process"]
    stdout = private.read_bytes(ArtifactRef(**process["stdout"]))
    private.read_bytes(ArtifactRef(**process["stderr"]))
    parsed = coordinator.registry.require(seat.executor_id).parse(stdout)
    if (process["status"] != "exit" or process["returncode"] != 0 or process["capture_complete"] is not True
            or process["stdout"] != asdict(terminal.stdout_ref) or process["stderr"] != asdict(terminal.stderr_ref)
            or process["answer"] != asdict(terminal.answer_ref) or parsed is None or not parsed.final
            or parsed.error or parsed.session_id != terminal.session_id
            or parsed.observed_model != terminal.observed_model
            or parsed.observed_model is not None and parsed.observed_model != profile.requested_model
            or parsed.answer.encode() != private.read_bytes(terminal.answer_ref)):
        raise ValueError("actual raw CLI terminal")
    snapshot = probe._ledger_snapshot(controller, proof["permit_id"], proof["permit_epoch"])
    if canonical_json(snapshot) != canonical_json(proof["ledger"]):
        raise ValueError("complete durable spend ledger")
    records, attempts, auxiliary, fenced = snapshot
    ledger = PermitLedger(controller.root / "locks/spend", controller)
    with ledger._locked() as current:
        entry = current["permits"][proof["permit_id"]]
        if entry["permit_digest"] != _digest(permit):
            raise ValueError("original complete permit")
        refusals = entry["auxiliary_refusals"]
    if (not 1 <= len(records) <= probe._MAX_MAIN_REQUESTS or not fenced
            or type(attempts) is not int or type(auxiliary) is not int or type(refusals) is not int
            or auxiliary < 0 or attempts != len(records) + auxiliary
            or attempts > attempt_limit(permit) or refusals != min(1, auxiliary)
            or (seat.executor_id != "agy" and auxiliary != 0)):
        raise ValueError("settled main request budget")
    # Reopen the original normalization and credential fingerprint without sending.
    probe_root = Path(json.loads(trial.payload)["probe_root"])
    route_receipt = probe._read_private_receipt(probe_root)
    _, routes = probe._admit_route_set(Path(route_receipt["manifest_path"]), Path(route_receipt["policy_path"]),
        {provider: route["route_id"] for provider, route in route_receipt["machine_routes"].items()})
    sender = HostUpstream(permit, routes[seat.executor_id], max_response_bytes=permit.max_response_bytes, deadline_seconds=2)
    from .broker import _safe_response_body, _safe_stream_body
    from .container_credentials import HostCredentialSource
    import time
    credentials = HostCredentialSource(routes[seat.executor_id].profile)
    credential = credentials.load()
    if credentials.fingerprint != proof["credential_source_fingerprint"]:
        raise ValueError("original host credential source")
    captured = {item["request_id"]: item for item in proof["wire"]}
    if set(captured) != {record["request_id"] for record in records}:
        raise ValueError("complete raw model exchange")
    for record in records:
        item, outbound = captured[record["request_id"]], record["identity"]
        normal = private.read_bytes(ArtifactRef(**item["normalized_body"]))
        prepared = sender.preview("POST", outbound["path"], {"content-type": "application/json"}, normal)
        effective = private.read_bytes(ArtifactRef(**item["effective_body"]))
        response = private.read_bytes(ArtifactRef(**item["response_body"]))
        delivered = private.read_bytes(ArtifactRef(**item["delivered_body"]))
        normalized_response = (b"".join(_safe_stream_body((response,), credential, permit.max_response_bytes,
            time.monotonic() + permit.response_deadline)) if item["content_type"] == "text/event-stream" else
            _safe_response_body(response, item["content_type"], credential))
        if (record["state"] != "settled" or prepared.body != effective
                or _sha(normal) != outbound["body_sha256"] or _sha(effective) != outbound["upstream_body_sha256"]
                or prepared.route_digest != outbound["upstream_route_digest"] or prepared.origin != outbound["upstream_origin"]
                or prepared.path != outbound["upstream_path"] or prepared.effective_model != outbound["upstream_model"]
                or delivered != normalized_response or _sha(delivered) != record["response_digest"] or not response or not delivered
                or len(response) > permit.max_response_bytes or len(delivered) > permit.max_response_bytes
                or type(item["http_status"]) is not int or not 200 <= item["http_status"] < 300
                or item["content_type"] not in {"application/json", "text/event-stream"}):
            raise ValueError("actual model wire bytes")
    return proof


def fence_stopped_turn(controller, ledger, transport, permit_id, epoch):
    from .container_transfer_transport import reopen_stopped_seat
    reopen_stopped_seat(controller, transport.stop_evidence, spec=transport.spec, handle=transport.handle)
    ledger.fence_epoch(permit_id, epoch)


def run_coordinator_turn(coordinator, packet, seat, round, request, peer):
    from scripts import fanout_container_certify as probe
    from .broker import Broker, BrokerPermit
    from .container_boundary import ContainerAdmission, PinnedDockerTransport, SessionStore, issue_container_boundary, _turn_digest
    from .container_broker import PermitLedger
    from .container_certification import TrialCertificate, run_trial_turn
    from .container_runtime import ContainerSpec, private_volume_names
    from .container_source import release_source_binding
    from .container_transfer_transport import admitted_request_digest, issue_input_source_grant
    from .container_credentials import HostCredentialSource
    from .container_upstream import HostUpstream
    from .providers import run_provider
    from .process import ProcessResult
    inputs, _ = authenticate_inputs(coordinator, packet)
    context = bind_turn_context(coordinator, packet, seat, round, request, peer)
    if coordinator.provider_runner is not run_provider:
        raise CollaborationDurabilityError("container coordinator requires the fixed provider runner")
    profile = admit_profile(coordinator, inputs, seat, packet.provider_policy)
    if request.profile is None or request.profile.digest != profile.digest:
        raise CollaborationDurabilityError("container request profile differs from admitted plan")
    certificate = _certificate(coordinator)
    bound = json.loads(certificate.payload)
    receipt = probe._read_private_receipt(Path(bound["probe_root"]))
    _, routes = probe._admit_route_set(Path(receipt["manifest_path"]), Path(receipt["policy_path"]),
        {provider: route["route_id"] for provider, route in receipt["machine_routes"].items()})
    route = routes[request.executor_id]
    if route.digest != bound["route_digests"].get(request.executor_id):
        raise CollaborationDurabilityError("container upstream route differs from certificate")
    controller = coordinator.lifecycle_controller
    workspace, session = private_volume_names(request.run_id, request.task_id, request.target_id, request.seat_id)
    sessions = SessionStore(controller, request.run_id, request.task_id, request.target_id, request.seat_id, session)
    if request.resume and sessions.resume(request.executor_id).session_id != request.session_id:
        raise CollaborationDurabilityError("container resume session differs from durable terminal")
    credential_source = HostCredentialSource(route.profile)
    credential = credential_source.load()
    permit = BrokerPermit(request.executor_id, request.run_id, request.task_id, request.target_id,
        request.seat_id, "resume" if request.resume else "initial", profile.requested_model,
        "disposable-placeholder", probe._MAX_MAIN_REQUESTS, 1024 * 1024, 8 * 1024 * 1024,
        probe._SPEND_CEILING["max_output_tokens_per_call"],
        response_deadline=300, inputs_digest=inputs.digest, session_id=request.session_id,
        clamp_output_tokens=request.executor_id in {"claude", "agy"},
        upstream_route_digest=route.digest, upstream_origin=route.profile.origin,
        upstream_model=route.profile.effective_model,
        upstream_path=probe._effective_route_path(route.profile, stream=True))
    ledger = PermitLedger(controller.root / "locks" / "spend", controller)
    epoch = (packet.attempt - 1) * 3 + round
    spec = ContainerSpec(request.run_id, request.task_id, request.target_id, request.seat_id,
        bound["image_digest"], workspace, session, f"/run/fanout/{request.seat_id}.sock",
        bound["cli_digests"][request.executor_id])
    lifecycle = _coordinator_lifecycle(coordinator)
    permit_id = lifecycle.reserve(context, spec, ledger, permit, epoch)
    captures = {}
    def capture(payload):
        item = probe._capture_private_wire(ledger, permit_id, epoch, request.artifact_store.root,
                                           request.artifact_prefix, payload)
        captures[item["request_id"]] = item
    broker = Broker(permit, credential=credential, send=HostUpstream(permit, route,
        max_response_bytes=permit.max_response_bytes, deadline_seconds=permit.response_deadline,
        success_sink=capture))
    transport = None
    try:
        transport = PinnedDockerTransport.start_live(controller, spec, ledger=ledger, broker=broker,
            permit_id=permit_id, epoch=epoch, recover=request.resume,
            lifecycle_scope=lifecycle, context_ref=context)
        admission = ContainerAdmission(permit, permit_id, epoch, ledger, spec.image_digest, spec.cli_digest,
            transport.attest_socket(), transport, sessions,
            transport.handle_evidence_name, transport.handle_evidence_digest)
        if not request.resume:
            source, name, digest = stage_input_material(coordinator, packet, seat, request, transport.handle)
            grant = issue_input_source_grant(controller, request, transport, admission, source, name, digest)
            admission = replace(admission, input_receipt=transport.stage_inputs(request, grant))
        boundary = issue_container_boundary(controller, request, transport.attest_vm(), transport.handle, admission)
        launched = replace(request, container_boundary=boundary)
        bind_turn_context(coordinator, packet, seat, round, request, peer)
        vm_profile_identity = json.loads(canonical_json(transport._control.vm_observation["profile_identity"]))
        result = (run_trial_turn(launched, certificate, request.certificate_roots)
                  if type(certificate) is TrialCertificate else run_provider(launched))
        process = transport.process_result
        if not isinstance(process, ProcessResult) or process.stdout != result.stdout or process.stderr != result.stderr:
            raise CollaborationDurabilityError("container fixed process capture is unavailable")
        if transport.stop_evidence is None:
            raise CollaborationDurabilityError("container stopped-seat proof is unavailable")
        if transport.lifecycle_snapshot is None or transport.runtime_proof is None:
            raise CollaborationDurabilityError("container runtime lifecycle proof is unavailable")
        fence_stopped_turn(controller, ledger, transport, permit_id, epoch)
        completed = write_evidence(controller, "container-coordinator-turn", {
            "schema_version": "fanout-container-coordinator-turn-v1",
            "request_digest": admitted_request_digest(request),
            "task_packet_sha256": _sha(canonical_json(packet.to_dict())),
            "release_source_binding": release_source_binding().to_dict(),
            "context": list(context), "boundary": [boundary.evidence_name, boundary.evidence_digest],
            "lifecycle_snapshot": list(transport.lifecycle_snapshot), "runtime_proof": asdict(transport.runtime_proof),
            "export": ([result.candidate_export.evidence_name, result.candidate_export.evidence_digest]
                       if result.candidate_export is not None else None),
            "stop": list(transport.stop_evidence), "turn_digest": _turn_digest(request, boundary),
            "process": {"status": process.status.value, "returncode": process.returncode,
                "capture_complete": process.capture_complete,
                "stdout": asdict(result.stdout_ref), "stderr": asdict(result.stderr_ref),
                "answer": asdict(result.answer_ref) if result.answer_ref is not None else None},
            "permit": asdict(permit), "permit_id": permit_id, "permit_epoch": epoch,
            "ledger": probe._ledger_snapshot(controller, permit_id, epoch),
            "wire": [captures[key] for key in sorted(captures)],
            "credential_source_fingerprint": credential_source.fingerprint,
            "vm_profile_identity": vm_profile_identity,
            "session_id": result.session_id, "valid": result.valid,
        })
        return replace(result, container_completion=completed)
    finally:
        if transport is not None:
            transport.close()
            if transport.stop_evidence is not None:
                fence_stopped_turn(controller, ledger, transport, permit_id, epoch)


def _execute_source():
    import importlib.util
    import sys
    from .container_source import _SOURCE_ROOT
    name = "_fanout_container_trial_execute"
    path = Path(_SOURCE_ROOT) / "shared/skills/llm-fanout-execute/scripts/execute.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise CollaborationDurabilityError("fixed checkout execution source is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module, sys.modules[__package__]


def _trial_root(root):
    import tempfile
    root = Path(root)
    info = root.lstat()
    if (not root.is_absolute() or root.resolve() != root or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700
            or not any(base in root.parents for base in (Path("/private/tmp"), Path(tempfile.gettempdir()).resolve()))):
        raise CollaborationValidationError("trial requires an exact private disposable root")
    return root


def prepare_container_trial_targets(trial_root):
    """Prepare only inert disposable Git targets, before owner trial issuance."""
    from .repo import _git
    root = _trial_root(trial_root)
    if any(root.iterdir()):
        raise CollaborationValidationError("trial target preparation requires a fresh empty root")
    targets = root / "targets"
    targets.mkdir(mode=0o700)
    roots = []
    for name in ("repo-alpha", "repo-beta"):
        target = targets / name
        target.mkdir(mode=0o700)
        _git(target, "init", "-q", "-b", "main")
        _git(target, "config", "user.name", "Fanout synthetic trial")
        _git(target, "config", "user.email", "fanout-trial@example.invalid")
        _git(target, "config", "remote.origin.url", f"https://github.com/fanout-fixture/{name}.git")
        (target / "README.md").write_bytes(b"immutable synthetic trial input\n")
        _git(target, "add", "README.md")
        _git(target, "commit", "-qm", "synthetic baseline")
        roots.append(target)
    return tuple(roots)


def _open_trial_authority(root):
    from .container_source import _SOURCE_ROOT
    execute, runtime = _execute_source()
    descriptor = execute.load_private_descriptor(root / "private")
    run = Path(descriptor["run_root"])
    if (run != root / "run" or descriptor["authority_root"] != str(root / "authority")
            or descriptor["schema_version"] != "fanout-execute-private-v2"
            or not (run / "artifacts/target-preparations.json").is_file()
            or not (run / ".fanout-execution.record.json").is_file()):
        raise ValueError("original durable trial execution")
    source = Path(_SOURCE_ROOT) / "components/memory/memory_exchange.py"
    memory_path = root / "reopen-memory/memory_exchange.py"
    memory = runtime.MemoryController(memory_path, executable_sha256=_sha(source.read_bytes()))
    return execute.open_run(root / "private", runtime=runtime,
        memory_factory=lambda artifacts, _health: runtime.MemoryCheckpointExchange(memory, artifacts),
        memory_preflight=lambda: False)


def completed_trial_root_bindings(trial_root):
    from .branch_handover import verify_git_handover_proof
    root = _trial_root(trial_root)
    with _open_trial_authority(root) as opened:
        service = opened.service
        inputs = opened.journal.inputs
        if {binding.root for binding in inputs.targets.values()} != {
                root / "targets/repo-alpha", root / "targets/repo-beta"}:
            raise ValueError("original exact synthetic roots")
        result = []
        for target_id, binding in inputs.targets.items():
            info = binding.root.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o700 or binding.root.resolve() != binding.root):
                raise ValueError("completed root identity")
            writers = [task for task in service.plan.tasks if task.kind == "work"
                       and task.execution_class == "repo-write" and task.target_id == target_id]
            if len(writers) != 1:
                raise ValueError("one exact delivered writer per target")
            task = writers[0]
            intent, terminal = opened.journal.state.branch_handovers[task.id]
            if terminal is None or service.scheduler.handover_terminal_for(task.id) != terminal:
                raise ValueError("authenticated completed CAS")
            verify_git_handover_proof(intent, terminal, inputs=inputs, artifacts=opened.artifacts,
                controller=service.lifecycle_controller, journal=opened.journal)
            baseline = service.repository_baselines[target_id]
            if baseline.digest != binding.baseline_sha256 or baseline.head != binding.base_oid:
                raise ValueError("original journal baseline")
            result.append({"path": str(binding.root), "device": info.st_dev, "inode": info.st_ino,
                           "baseline_sha256": baseline.digest, "head": baseline.head})
        return sorted(result, key=lambda item: item["path"])


def produce_container_trial(trial_root, certificate, admission_packet, skill_roots, *, allow_paid=False):
    """Run the reviewed synthetic DAG through the fixed production container path."""
    from scripts import fanout_container_certify as probe
    from .container_certification import TrialCertificate, TRIAL_CHECKS, _owner_authority
    from .container_source import release_source_binding, _SOURCE_ROOT
    from .container_boundary import SessionStore
    from .container_runtime import private_volume_names
    from .branch_handover import prepare_branch_handover, deliver_branch_candidate
    from .runstate import _read_private_file
    root = _trial_root(trial_root)
    roots = tuple(root / "targets" / name for name in ("repo-alpha", "repo-beta"))
    binding = release_source_binding()
    if (allow_paid is not True or type(certificate) is not TrialCertificate
            or not _owner_authority().verify_trial(certificate, roots, binding.digest).passed):
        raise CollaborationValidationError("paid synthetic trial requires exact owner-issued trial authority")
    if (set(path.name for path in root.iterdir()) - {"targets", "trial-certificate.json"}
            or root.with_name(root.name + ".controller-capability").exists()):
        raise CollaborationValidationError("synthetic trial contains prior execution state")
    execute, runtime = _execute_source()
    resolver = execute._resolver(runtime, skill_roots)
    plan = execute.verify_admission(admission_packet, runtime, resolver).plan
    work = [task for task in plan.tasks if task.kind == "work" and task.execution_class != "orchestrator-action"]
    if (plan.schema_version != "v2" or len(work) != 2
            or {target.id for target in plan.targets} != {"repo-alpha", "repo-beta"}
            or {task.target_id for task in work} != {"repo-alpha", "repo-beta"}
            or len({target.ticket_key for target in plan.targets}) != 1
            or any(task.execution_class != "repo-write" or any(mode != "artifact" for mode in task.dependency_modes.values())
                   for task in work)
            or any((policy.executor_ids, policy.rounds, policy.minimum_success, policy.retries, policy.quality_tier) !=
                   (("claude", "codex", "agy"), 2, 2, 0, "standard")
                   for task in work for policy in (task.provider_policy or plan.defaults,))):
        raise CollaborationValidationError("trial admission must bind the exact two-target standard artifact DAG")
    trial_controller = runtime.create_lifecycle_controller(root / "controller")
    probe._write_private(root.with_name(root.name + ".controller-capability"), trial_controller.capability.export_token().encode("ascii"))
    with ArtifactStore(root / "raw") as raw:
        private = root / "private"
        private.mkdir(mode=0o700)
        inert = root / "reopen-memory"
        inert.mkdir(mode=0o700)
        executable = inert / "memory_exchange.py"
        fd = os.open(executable, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o700)
        with os.fdopen(fd, "wb") as output:
            output.write((Path(_SOURCE_ROOT) / "components/memory/memory_exchange.py").read_bytes())
            output.flush()
            os.fsync(output.fileno())
        execute.start_v2_run(admission_packet, target_roots={path.name: path for path in roots},
            run_root=root / "run", authority_root=root / "authority", private_root=private,
            skill_roots=skill_roots, budget=12, runtime=runtime)
        deliveries = []
        with execute.open_run(private, runtime=runtime, trial_certificate=certificate, for_dispatch=True) as opened:
            service = opened.service
            service.start(owner=opened.owner)
            first_task = work[0]
            if first_task.depends_on:
                raise CollaborationValidationError("trial fault requires a ready first writer")
            service.scheduler.mark_active(first_task.id, owner=opened.owner)
            packet = service._packet_for(first_task)
            preparation = service.preparations[first_task.id]
            initial = service.coordinator.execute_round(packet, preparation.seats, preparation.policy,
                round=1, expected_inputs=service.inputs)
            if initial.status != "round-complete" or len(initial.valid_terminals) != 3:
                raise CollaborationDurabilityError("trial requires three actual initial terminals before fault")
            failed = next(item for item in initial.valid_terminals if item.executor_id == "agy")
            _, volume = private_volume_names(packet.run_id, packet.task_id, packet.target_binding.spec.id, failed.seat_id)
            sessions = SessionStore(service.lifecycle_controller, packet.run_id, packet.task_id,
                packet.target_binding.spec.id, failed.seat_id, volume)
            if sessions.resume("agy").session_id != failed.session_id:
                raise CollaborationDurabilityError("trial fault lacks the exact ready session")
            original = execute._read_file(str(sessions._record), limit=64 * 1024)
            preserved = raw.write_json("fault/session-ready.json", {"bytes": original.decode()})
            fault_dir = service.lifecycle_controller.root / "quarantine/session-read-fault"
            fault_dir.mkdir(mode=0o700)
            moved = fault_dir / sessions._record.name
            os.rename(sessions._record, moved)
            fault = write_evidence(trial_controller, "container-trial-session-read-fault", {
                "run_id": packet.run_id, "task_id": packet.task_id, "target_id": packet.target_binding.spec.id,
                "seat_id": failed.seat_id, "provider": "agy", "session_id": failed.session_id, "round": 2,
                "initial_barrier": asdict(initial.barrier_ref), "session_record": asdict(preserved),
                "original_record": str(sessions._record), "moved_record": str(moved)})
            for task in work:
                service.resume(owner=opened.owner)
                state = service._execution_state(task.id)
                final = service._restore_barrier(state.barriers[-1], service._packet_for(task))
                if final.status != "round-complete" or len(final.valid_terminals) < 2:
                    raise CollaborationDurabilityError("trial lost minimum actual surviving resumes")
                sources = tuple(runtime.CandidateBundle.from_manifest(opened.artifacts.read_bytes(ref))
                                for _, ref in final.candidate_sources)
                selected = sources[0]
                candidate = runtime.synthesize_candidate(service.repository_baselines[task.target_id],
                    sources=sources, entries=selected.entries, deleted_paths=selected.deleted_paths)
                verification = service.verify_repo_synthesis(task.id, candidate, owner=opened.owner)
                if not verification.valid or not candidate.entries and not candidate.deleted_paths:
                    raise CollaborationDurabilityError("trial candidate failed fresh declared checks")
                service.submit(task.id, verification, owner=opened.owner)
                issued = runtime.issue_target_candidate(candidate, task_id=task.id, plan=service.plan,
                    inputs=service.inputs, store=opened.artifacts, controller=service.lifecycle_controller)
                verified, receipt = runtime.verify_target_candidate(issued, baseline=service.repository_baselines[task.target_id],
                    plan=service.plan, inputs=service.inputs, store=opened.artifacts, controller=service.lifecycle_controller)
                if not receipt.valid:
                    raise CollaborationDurabilityError("trial target verification failed")
                deliveries.append((task, issued, verified))
            # Fresh trial admission keeps both baselines fixed through every paid turn.
            for task, issued, verified in deliveries:
                transaction = prepare_branch_handover(service.inputs.targets[task.target_id], issued, verified,
                    plan=service.plan, inputs=service.inputs, artifacts=opened.artifacts,
                    scheduler=service.scheduler, controller=service.lifecycle_controller,
                    journal=opened.journal, owner=opened.owner, task_id=task.id)
                deliver_branch_candidate(transaction, plan=service.plan, inputs=service.inputs,
                    artifacts=opened.artifacts, scheduler=service.scheduler, controller=service.lifecycle_controller,
                    journal=opened.journal, owner=opened.owner)
        with _open_trial_authority(root) as cold:
            cold.journal.recover_uncertain(cold.owner)
            events = _read_private_file(cold.journal._root_fd, cold.journal._EVENT_FILE, cold.journal.limits.max_journal_bytes)
            event_ref = raw.write_bytes("cold/events.jsonl", events)
            cold_proof = write_evidence(trial_controller, "container-trial-cold-recovery", {
                "run_id": cold.journal.inputs.run_id, "inputs_digest": cold.journal.inputs.digest,
                "fault": list(fault), "events": asdict(event_ref)})
            common = {"schema_version": "fanout-container-trial-core-v1", "private_root": str(private),
                "run_id": cold.journal.inputs.run_id, "inputs_digest": cold.journal.inputs.digest,
                "source_binding": binding.to_dict(), "fault": list(fault), "cold": list(cold_proof),
                "tasks": {task.id: [asdict(ref) for ref in cold.service._execution_state(task.id).barriers] for task in work}}
        checks = {name: {**common, "check": name} for name in TRIAL_CHECKS}
        if not validate_trial_core(trial_controller, root, checks, binding).passed:
            raise CollaborationDurabilityError("trial core proofs could not be semantically reopened")
        refs = {name: {"artifact": asdict(raw.write_json(f"core/{name}.json", checks[name]))} for name in TRIAL_CHECKS}
        unsigned = {"schema_version": "fanout-synthetic-trial-result-v1", "private_run_root": str(root),
            "trial_certificate": certificate.to_dict(), "release_source_binding": binding.to_dict(), "checks": refs}
        name, digest = write_evidence(trial_controller, "container-synthetic-trial-complete",
            {"trial_result_sha256": _sha(canonical_json(unsigned))})
        result = {**unsigned, "completion_evidence_name": name, "completion_evidence_digest": digest}
        probe._write_private(root / "trial-result.json", canonical_json(result))
        return result


def validate_trial_core(controller, trial_root, checks, source_binding):
    """Reopen the five domain proofs; authenticated scalar success is not evidence."""
    from .certification import CertificationDecision
    from .container_certification import TRIAL_CHECKS, _read_owned_json, _certificate, _owner_authority
    from .container_source import release_source_binding
    from .controller import assert_controller
    from .branch_handover import _envelopes, verify_git_handover_proof
    from .runstate import _parse_events, _read_private_file
    try:
        root = _trial_root(trial_root)
        assert_controller(controller)
        if controller.root != root / "controller" or source_binding != release_source_binding():
            raise ValueError("trial controller or source")
        fields = {"schema_version", "check", "private_root", "run_id", "inputs_digest", "source_binding",
                  "tasks", "fault", "cold"}
        if not isinstance(checks, dict) or set(checks) != set(TRIAL_CHECKS):
            raise ValueError("five exact core checks")
        common = None
        for name in TRIAL_CHECKS:
            value = checks[name]
            if (not isinstance(value, dict) or set(value) != fields
                    or value["schema_version"] != "fanout-container-trial-core-v1"
                    or value["check"] != name or value["private_root"] != str(root / "private")
                    or value["source_binding"] != source_binding.to_dict()):
                raise ValueError("complete core material")
            material = {key: item for key, item in value.items() if key != "check"}
            if common is not None and material != common:
                raise ValueError("core check run association")
            common = material
        value, _ = _read_owned_json(root / "trial-certificate.json")
        trial = _certificate(value)
        if not _owner_authority()._verify_completed_trial(trial, root).passed:
            raise ValueError("original completed trial certificate")
        # This reopens authority only. No memory query, send, or verifier command is run.
        with _open_trial_authority(root) as opened:
            service, journal, store = opened.service, opened.journal, opened.artifacts
            service.coordinator.trial_certificate = trial
            if (journal.inputs.run_id != common["run_id"] or journal.inputs.digest != common["inputs_digest"]
                    or {binding.root for binding in journal.inputs.targets.values()} !=
                        {root / "targets/repo-alpha", root / "targets/repo-beta"}
                    or len({binding.spec.ticket_key for binding in journal.inputs.targets.values()}) != 1):
                raise ValueError("two exact target bindings")
            work = [task for task in service.plan.tasks if task.kind == "work" and task.execution_class == "repo-write"]
            if (len(work) != 2 or {task.target_id for task in work} != set(journal.inputs.targets)
                    or set(common["tasks"]) != {task.id for task in work}):
                raise ValueError("two writer outcomes")
            fault = read_evidence(controller, "container-trial-session-read-fault", *common["fault"])
            cold = read_evidence(controller, "container-trial-cold-recovery", *common["cold"])
            with ArtifactStore.open_existing(root / "raw") as raw:
                archived = raw.read_json(ArtifactRef(**fault["session_record"]))
                original = json.loads(archived["bytes"].encode())
                from .container_boundary import SessionStore
                from .container_runtime import private_volume_names
                identity = tuple(fault[name] for name in ("run_id", "task_id", "target_id", "seat_id"))
                _, volume = private_volume_names(*identity)
                sessions = SessionStore(service.lifecycle_controller, *identity, volume)
                if (fault["run_id"] != journal.inputs.run_id or fault["round"] != 2
                        or fault["original_record"] != str(sessions._record)
                        or sessions._record.exists() or sessions._mac(original["payload"]) != original["mac"]
                        or original["payload"]["identity"] != list(identity)
                        or cold["run_id"] != journal.inputs.run_id or cold["inputs_digest"] != journal.inputs.digest
                        or cold["fault"] != common["fault"]):
                    raise ValueError("preserved coordination fault")
                moved = Path(fault["moved_record"])
                if (moved.parent != service.lifecycle_controller.root / "quarantine/session-read-fault"
                        or moved.name != sessions._record.name):
                    raise ValueError("preserved original session bytes")
                original_bytes = archived["bytes"].encode()
                with ArtifactStore.open_existing(moved.parent) as fault_store:
                    if fault_store.read_bytes(ArtifactRef(moved.name, _sha(original_bytes), len(original_bytes))) != original_bytes:
                        raise ValueError("preserved private session bytes")
                archived_session = SessionStore(service.lifecycle_controller, *identity, volume)
                archived_session._record = moved
                if archived_session._read() != original["payload"]:
                    raise ValueError("original ready session authority")
                event_bytes = _read_private_file(journal._root_fd, journal._EVENT_FILE, journal.limits.max_journal_bytes)
                events, offset, tail = _parse_events(event_bytes, journal.limits, opened.owner._mac_key())
                if tail or offset != len(event_bytes):
                    raise ValueError("complete authenticated journal")
                observed = []
                for event in events:
                    payload = event["payload"]
                    if (payload.get("task_id"), payload.get("seat_id"), payload.get("attempt"), payload.get("round")) == (
                            fault["task_id"], fault["seat_id"], 1, 2):
                        observed.append(event["type"])
                if ("process-started" not in observed or "uncertain-attempt" not in observed
                        or observed.index("process-started") > observed.index("uncertain-attempt")
                        or "provider-terminal" in observed
                        or raw.read_bytes(ArtifactRef(**cold["events"])) != event_bytes):
                    raise ValueError("cold uncertainty without replay")
                ledger = service.lifecycle_controller.root / "locks/spend"
                from .container_broker import PermitLedger
                with PermitLedger(ledger, service.lifecycle_controller)._locked() as spent:
                    scope = _sha(canonical_json(dict(zip(("run_id", "task_id", "target_id", "seat_id"), identity))))
                    if any(record["seat_scope"] == scope and record["epoch"] == 2
                           for record in spent["permits"].values()):
                        raise ValueError("fault seat crossed provider send")
            partial_seen = False
            for task in work:
                packet = service._packet_for(task)
                state = service._execution_state(task.id)
                if [asdict(ref) for ref in state.barriers] != common["tasks"][task.id] or len(state.barriers) != 2:
                    raise ValueError("authenticated barriers")
                barriers = tuple(service._restore_barrier(ref, packet) for ref in state.barriers)
                first, final = barriers
                if (first.status != "round-complete" or len(first.valid_terminals) != 3
                        or final.status != "round-complete" or len(final.valid_terminals) < 2):
                    raise ValueError("actual initial and resumed survival")
                if task.id == fault["task_id"] and (fault["provider"] != "agy"
                        or fault["target_id"] != task.target_id
                        or fault["initial_barrier"] != asdict(state.barriers[0])):
                    raise ValueError("fault after exact initial barrier")
                seat_map = {seat.seat_id: seat for seat in service.preparations[task.id].seats}
                for barrier in barriers:
                    for observed_terminal in barrier.valid_terminals:
                        checkpoint = service.coordinator._checkpoint_from_barrier(barrier, observed_terminal)
                        receipt = service.coordinator._receipt_for(barrier, observed_terminal)
                        if journal.state.evidence_digest("checkpoint-verified", task.id, observed_terminal.seat_id,
                                packet.attempt, barrier.round) != receipt.verification_ref.digest:
                            raise ValueError("original verified checkpoint authority")
                        service.coordinator.memory._verify_receipt_artifacts(checkpoint, receipt)
                        peer = next((item for item in barrier.peer_packets
                                     if item.target_seat_id == observed_terminal.seat_id), None)
                        reopened = reopen_turn_completion(service.coordinator, packet,
                            seat_map[observed_terminal.seat_id], observed_terminal, peer)
                        if task.id == fault["task_id"] and barrier.round == 1 and observed_terminal.seat_id == fault["seat_id"]:
                            expected_binding = {"provider": "agy", "session_id": observed_terminal.session_id,
                                "volume": volume, "turn_digest": reopened["turn_digest"],
                                "upstream_route_digest": reopened["permit"]["upstream_route_digest"]}
                            if (observed_terminal.executor_id != "agy" or fault["session_id"] != observed_terminal.session_id
                                    or original["payload"]["bindings"] != {"agy": expected_binding}
                                    or original["payload"]["turns"] != {reopened["turn_digest"]: {"provider": "agy", "state": "settled"}}):
                                raise ValueError("original ready session and terminal")
                        from .container_transfer_transport import CandidateExportReceipt, reopen_candidate_export
                        exported = read_evidence(service.lifecycle_controller, "container-candidate-export", *reopened["export"])
                        exported["identity"] = TransferIdentity(**exported["identity"])
                        exported["root"] = Path(exported["root"])
                        export = CandidateExportReceipt(**exported,
                            evidence_name=reopened["export"][0], evidence_digest=reopened["export"][1])
                        reopen_candidate_export(service.lifecycle_controller, export,
                            identity=TransferIdentity(packet.run_id, packet.task_id, packet.target_binding.spec.id,
                                                      observed_terminal.seat_id, journal.inputs.digest),
                            turn_digest=reopened["turn_digest"], stop_evidence=tuple(reopened["stop"]))
                for terminal in final.valid_terminals:
                    original_terminal = next(item for item in first.valid_terminals if item.seat_id == terminal.seat_id)
                    peer = next(item for item in final.peer_packets if item.target_seat_id == terminal.seat_id)
                    if (terminal.session_id != original_terminal.session_id
                            or peer.payload != service.coordinator.build_peer_packet(packet, first,
                                target_seat_id=terminal.seat_id).payload):
                        raise ValueError("verified peer exact-session exchange")
                for seat_id, ref in final.candidate_sources:
                    reopen_source_candidate(service.coordinator, packet, seat_id, 2, ref)
                if task.id == fault["task_id"]:
                    failed = next(item for item in final.terminals if item.seat_id == fault["seat_id"])
                    partial_seen = failed.state == "uncertain-attempt" and not failed.valid and len(final.valid_terminals) == 2
                intent, terminal = journal.state.branch_handovers[task.id]
                if terminal is None or terminal != service.scheduler.handover_terminal_for(task.id):
                    raise ValueError("actual Git CAS terminal")
                candidate, verification = _envelopes(intent, journal.inputs.targets[task.target_id],
                    service.plan, journal.inputs, store, service.lifecycle_controller)
                if (not verification.valid or verification.checks != task.checks
                        or len(candidate.source_candidate_digests) < 2
                        or sorted(candidate.source_candidate_digests) != sorted(ref.digest for _, ref in final.candidate_sources)
                        or not candidate.entries and not candidate.deleted_paths):
                    raise ValueError("fresh candidate verification provenance")
                verify_git_handover_proof(intent, terminal, inputs=journal.inputs, artifacts=store,
                    controller=service.lifecycle_controller, journal=journal)
            if not partial_seen or release_source_binding() != source_binding:
                raise ValueError("partial coordination and final source")
        return CertificationDecision(True, ())
    except Exception:
        return CertificationDecision(False, ("complete authenticated container trial core proof is unavailable",))
