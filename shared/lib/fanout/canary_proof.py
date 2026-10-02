"""Authenticated actual-turn wire proof for the synthetic v1 Codex canary."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import re
import stat
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from .artifacts import ArtifactRef, canonical_json
from .broker import Broker, BrokerListener, BrokerPermit, BrokerRejected, _json_object
from .container_broker import PermitLedger
from .container_routes import RouteAdmission, _read_owned_json, _validate_profile
from .controller import read_evidence, write_evidence
from .errors import FanoutError, ProviderRequestError
from .native_boundary import _command_digest, _executable, _hash_file
from .providers import CodexAdapter, ProviderRegistry, ProviderRequest
from .repo import validate_seat_workspace


MODEL = "gpt-6.1-sol"
_CATEGORY = "codex-readonly-canary"
_ISSUER = object()


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def route_record(route):
    descriptor = asdict(route)
    descriptor["profile"].pop("credential_ref")
    descriptor["profile"]["execution_classes"] = list(route.profile.execution_classes)
    return {**descriptor, "digest": route.digest}


def _route_check(route):
    if type(route) is not RouteAdmission:
        raise ProviderRequestError("canary requires one admitted company OpenAI route")
    profile = asdict(route.profile)
    profile["execution_classes"] = list(route.profile.execution_classes)
    _validate_profile(profile)
    if (route.profile.auth_family not in {"openai-keychain", "openai-1password"}
            or route.profile.provider != "codex" or "read-only" not in route.profile.execution_classes
            or route.profile.requested_model != MODEL):
        raise ProviderRequestError("canary requires one admitted company OpenAI route")


def _request_record(request):
    return {"executor_id": request.executor_id, "prompt_sha256": _sha(request.prompt_bytes),
        "cwd": str(request.cwd), "execution_class": request.execution_class,
        "session_id": request.session_id, "resume": request.resume,
        "profile_digest": request.profile.digest, "artifact_prefix": request.artifact_prefix,
        "timeout": request.timeout, "retries": request.retries,
        "context_sha256": request.context_sha256, "skill_bundle_sha256": request.skill_bundle_sha256,
        "skill_delivery_sha256": request.skill_delivery_sha256,
        "skill_delivery_ref": asdict(request.skill_delivery_ref),
        "slot_cap": request.slot_cap, "slot_timeout": request.slot_timeout,
        "slot_root": str(request.slot_root)}


def _expected_request(coordinator, packet, seat, round_number, prior=None, peer=None):
    from scripts import fanout_smoke as smoke
    from .collaboration import MAX_SEATS, PeerPacket, TerminalSeatResult
    coordinator.journal.authorize_owner(coordinator.owner)
    inputs = coordinator.journal.inputs
    tier = packet.provider_policy.quality_tier
    expected_packet = smoke._packet(Path(packet.cwd), smoke._plan(live=True, quality_tier=tier), run_id=packet.run_id)
    if (packet.to_dict() != expected_packet.to_dict() or packet.schema_version != "fanout-task-packet-v1"
            or packet.run_id != inputs.run_id or packet.task_id != smoke.TASK_ID
            or packet.execution_class != "read-only" or packet.attempt != 1
            or round_number not in (1, 2) or seat.executor_id != "codex" or seat.seat_id != "seat-codex"
            or inputs.compiled_plan_sha256 != packet.compiled_plan_sha256
            or inputs.skill_manifests.get(packet.task_id) != packet.skill_manifest_sha256
            or inputs.source_sha256 != smoke._live_source_hash()
            or inputs.skill_manifests.get("release-source") != smoke.release_source_binding().digest
            or (round_number == 1 and (prior is not None or peer is not None))
            or (round_number == 2 and (prior is None or peer is None))):
        raise ProviderRequestError("canary is outside the exact current synthetic v1 turn")
    if round_number == 2:
        try:
            first = coordinator.discover_barrier(packet, round=1)
            if (first is None or first.status != "round-complete"
                    or len(first.valid_terminals) != len(smoke.EXECUTORS)
                    or {item.executor_id for item in first.valid_terminals} != set(smoke.EXECUTORS)
                    or type(prior) is not TerminalSeatResult or type(peer) is not PeerPacket
                    or prior != next(item for item in first.valid_terminals if item.seat_id == seat.seat_id)):
                raise ProviderRequestError("canary requires its original complete first barrier")
            for terminal in first.valid_terminals:
                checkpoint = coordinator._checkpoint_from_barrier(first, terminal)
                receipt = coordinator._receipt_for(first, terminal)
                coordinator.memory._verify_receipt_artifacts(checkpoint, receipt)
                for kind, reference in (
                    ("provider-terminal", terminal.terminal_ref),
                    ("checkpoint-published", receipt.publication.publication_ref),
                    ("checkpoint-verified", receipt.verification_ref),
                ):
                    if coordinator.journal.state.evidence_digest(kind, packet.task_id,
                            terminal.seat_id, packet.attempt, 1) != reference.digest:
                        raise ProviderRequestError("canary original first barrier changed authority")
            if peer != coordinator.build_peer_packet(packet, first, target_seat_id=seat.seat_id):
                raise ProviderRequestError("canary peer differs from its original first barrier")
        except (FanoutError, ValueError, TypeError, StopIteration) as error:
            raise ProviderRequestError("canary original first barrier or peer is unavailable") from error
    run_root = coordinator.lifecycle_controller.root.parent
    if (smoke._live_root(run_root, new=False) != run_root
            or Path(packet.cwd) != run_root / "repo" or packet.run_id != smoke._live_run_id(run_root)
            or coordinator.repository_baseline.digest != inputs.repo_baseline_sha256
            or [(entry.path, entry.data) for entry in coordinator.repository_baseline.entries]
                != [("source.txt", b"dirty caller input\n")]):
        raise ProviderRequestError("canary target is not the exact disposable synthetic repository")
    coordinator._verify_staged_seat(seat, packet)
    validate_seat_workspace(coordinator.lifecycle_controller, seat.workspace_verification)
    profile = coordinator.registry.admit("codex", "read-only", tier)
    if inputs.provider_profiles.get(profile.binding_key) != profile.digest:
        raise ProviderRequestError("canary profile differs from authenticated original inputs")
    timeout = packet.provider_policy.effective_timeout(profile)
    catalog = ProviderRegistry.for_installed(("codex",), version_probe=lambda _: profile.cli_version)
    if type(profile.adapter) is not CodexAdapter or profile.digest != catalog.select("codex", "read-only", tier).digest:
        raise ProviderRequestError("canary custom adapter or profile is unsupported")
    return ProviderRequest(executor_id="codex", prompt=coordinator._prompt(packet, seat, round_number, peer),
        cwd=seat.workspace_verification.workspace.root, execution_class="read-only",
        session_id=prior.session_id if prior is not None else None, resume=prior is not None,
        timeout=timeout, retries=0, profile=profile, artifact_store=coordinator.artifacts,
        artifact_prefix=coordinator._provider_prefix(packet, round_number, seat.seat_id),
        slot_cap=MAX_SEATS, slot_root=coordinator.slot_root,
        context_sha256=packet.context_sha256, skill_bundle_sha256=packet.skill_bundle_sha256,
        staged_skill_root=seat.staged_root, staged_skill_admission=seat.admission,
        skill_delivery_sha256=seat.delivery_evidence_sha256, skill_delivery_ref=seat.delivery_evidence_ref)


def _binding(coordinator, packet, seat, round_number, request):
    return {"run_id": packet.run_id, "task_id": packet.task_id, "seat_id": seat.seat_id,
        "round": round_number, "inputs_digest": coordinator.journal.inputs.digest,
        "source_sha256": coordinator.journal.inputs.source_sha256,
        "packet_sha256": _sha(canonical_json(packet.to_dict())),
        "workspace_evidence_digest": seat.workspace_verification.evidence_digest,
        "request": _request_record(request)}


def _native_record(result):
    return {"session_id": result.session_id, "requested_model": result.requested_model,
        "observed_model": result.observed_model,
        **{name: asdict(getattr(result, name)) for name in ("stdout_ref", "stderr_ref", "answer_ref")}}


def _private_home(home):
    info = home.lstat()
    if (home.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700 or home != home.resolve(strict=True)):
        raise ValueError("canary private HOME is unsafe")
    return {"path": str(home), "device": info.st_dev, "inode": info.st_ino}


def _session_capture(home, session_id):
    root = home / ".codex/sessions"
    found = []
    if root.is_symlink():
        raise ValueError("canary session root is unsafe")
    for path in root.rglob("*.jsonl"):
        if len(found) > 4 or any(parent.is_symlink() for parent in path.parents if parent != home):
            raise ValueError("canary private session state is unsafe")
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 8 * 1024 * 1024:
            raise ValueError("canary private session state is unsafe")
        raw = path.read_bytes()
        first = _json_object(raw.splitlines()[0])
        if first.get("type") == "session_meta" and first.get("payload", {}).get("id") == session_id:
            found.append({"path": str(path.relative_to(home)), "sha256": _sha(raw), "size": len(raw)})
    if len(found) != 1:
        raise ValueError("canary exact private session state is missing or ambiguous")
    return found[0]


@dataclass(frozen=True)
class CanaryTurn:
    coordinator: object = field(repr=False, compare=False)
    packet: object = field(repr=False, compare=False)
    seat: object = field(repr=False, compare=False)
    round_number: int
    route: RouteAdmission = field(repr=False)
    request: ProviderRequest = field(repr=False)
    evidence_name: str
    evidence_digest: str
    initial: dict | None = field(default=None, repr=False)
    peer: object | None = field(default=None, repr=False)
    _issuer: object = field(default=None, repr=False, compare=False)
    _held: dict = field(default_factory=dict, repr=False, compare=False)

    def validate(self, request):
        if self._issuer is not _ISSUER or self._held.get("started"):
            raise ProviderRequestError("canary authority is missing or already consumed")
        if (request.execution_class != "read-only" or request.executor_id != "codex" or request.retries != 0
                or any(getattr(request, name) is not None for name in (
                    "run_id", "task_id", "target_id", "inputs_digest", "seat_id", "native_boundary",
                    "native_controller", "native_verification", "container_boundary", "agy_guard"))
                or request.certificate_roots or request.artifact_store is not self.coordinator.artifacts
                or request.staged_skill_root != self.request.staged_skill_root
                or request.staged_skill_admission != self.request.staged_skill_admission
                or _request_record(request) != _request_record(self.request)):
            raise ProviderRequestError("canary request differs from its exact synthetic turn")
        prior = None if self.initial is None else self._held["prior"]
        expected = _expected_request(self.coordinator, self.packet, self.seat, self.round_number, prior, self.peer)
        record = read_evidence(self.coordinator.lifecycle_controller, _CATEGORY,
            self.evidence_name, self.evidence_digest)
        if (record["binding"] != _binding(self.coordinator, self.packet, self.seat, self.round_number, expected)
                or record["route"] != route_record(self.route)
                or self.coordinator.journal.inputs.skill_manifests.get("codex-canary-route") != self.route.digest):
            raise ProviderRequestError("canary authorization source/input/route changed")
        _route_check(self.route)
        home = Path(record["private_home"]["path"])
        if _private_home(home) != record["private_home"]:
            raise ProviderRequestError("canary exact private HOME changed")
        if self.initial is not None:
            state = self.initial["session_state"]
            path = home / state["path"]
            if (path.is_symlink() or _hash_file(path) != state["sha256"]
                    or path.stat().st_size != state["size"]):
                raise ProviderRequestError("canary exact initial session bytes changed")
        executable = Path(record["executable"])
        if _executable("codex") != executable or _hash_file(executable) != record["executable_sha256"]:
            raise ProviderRequestError("canary executable changed")
        return record

    @contextmanager
    def launch(self, request, command):
        from .container_credentials import HostCredentialSource
        from .container_upstream import HostUpstream
        from .diagnostic import codex_broker_command
        record = self.validate(request)
        if command.environment or _command_digest(command) != record["command_digest"]:
            raise ProviderRequestError("canary original command changed")
        held = self._held
        held["started"] = True
        controller = self.coordinator.lifecycle_controller
        ledger = PermitLedger(controller.root / "codex-canary-ledger", controller)
        permit = BrokerPermit("codex", self.packet.run_id, self.packet.task_id, "synthetic-v1",
            self.seat.seat_id, "resume" if request.resume else "initial", MODEL,
            secrets.token_hex(24), 4, 1024 * 1024, 8 * 1024 * 1024, 1024,
            response_deadline=300, inputs_digest=self.coordinator.journal.inputs.digest,
            session_id=request.session_id, upstream_route_digest=self.route.digest,
            upstream_origin=self.route.profile.origin, upstream_model=self.route.profile.effective_model,
            upstream_path="/v1/responses")
        permit_id = ledger.register(permit, self.round_number)
        held.update(ledger=ledger, permit=permit, permit_id=permit_id, captures={}, failures=[])

        def capture(payload):
            from scripts.fanout_container_certify import _capture_private_wire
            captured = _capture_private_wire(ledger, permit_id, self.round_number,
                self.coordinator.artifacts.root, request.artifact_prefix + "/codex-canary", payload)
            if captured["request_id"] in held["captures"]:
                raise BrokerRejected("canary capture replay is refused")
            held["captures"][captured["request_id"]] = captured

        try:
            source = HostCredentialSource(self.route.profile)
            credential = source.load()
            if self.initial is not None and source.fingerprint != self.initial["credential_source_fingerprint"]:
                raise ProviderRequestError("canary credential source changed")
            held["credential_source_fingerprint"] = source.fingerprint
            sender = HostUpstream(permit, self.route, max_response_bytes=permit.max_response_bytes,
                deadline_seconds=permit.response_deadline, success_sink=capture, failure_sink=held["failures"].append)
            listener = BrokerListener(Broker(permit, credential=credential, send=sender),
                ledger_binding=(ledger, permit_id, self.round_number))
            held["listener"] = listener
            with listener:
                broker_command = codex_broker_command(request, command,
                    port=listener.port, placeholder=permit.placeholder_key)
                home = Path(record["private_home"]["path"])
                broker_command = replace(broker_command,
                    argv=(record["executable"], *broker_command.argv[1:]),
                    environment={"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config")})
                held["command"] = broker_command
                yield broker_command
        finally:
            ledger.fence_epoch(permit_id, self.round_number)

    def finish(self, request, process, result):
        held = self._held
        records = held["ledger"].request_records(held["permit_id"], self.round_number)
        listener = held["listener"]
        if (not result.valid or process.status.value != "exit" or process.returncode != 0
                or not process.capture_complete or process.stdout != result.stdout or process.stderr != result.stderr
                or listener.native_failed or held["failures"] or not records
                or len(records) != len(held["captures"]) or len(records) != len(listener.native_deliveries)
                or any(row["state"] != "settled" for row in records)
                or held["ledger"].attempt_counts(held["permit_id"], self.round_number) != (len(records), 0)):
            write_evidence(self.coordinator.lifecycle_controller, _CATEGORY, {
                "schema_version": "fanout-codex-canary-failure-v1",
                "authorization_name": self.evidence_name, "authorization_digest": self.evidence_digest,
                "native": _native_record(result), "process_status": process.status.value,
                "exit_code": process.returncode, "capture_complete": process.capture_complete,
                "permit_id": held["permit_id"], "epoch": self.round_number,
                "ledger_records": list(records), "native_deliveries": list(listener.native_deliveries),
                "complete_wire_captures": list(held["captures"].values()),
                "upstream_failures": held["failures"],
                "failed_or_partial_provider_bytes": "unavailable-at-host-callback"})
            raise ProviderRequestError("Codex canary process or all-request delivery proof is incomplete")
        _expected_request(self.coordinator, self.packet, self.seat, self.round_number,
            held.get("prior"), self.peer)
        wire = []
        for row in records:
            captured = held["captures"][row["request_id"]]
            body = self.coordinator.artifacts.read_bytes(ArtifactRef(**captured["response_body"]))
            identity, model = response_model(body, captured["content_type"])
            wire.append({**captured, "response_id": identity, "response_model": model})
        auth = read_evidence(self.coordinator.lifecycle_controller, _CATEGORY, self.evidence_name, self.evidence_digest)
        proof = {"schema_version": "fanout-codex-canary-proof-v1",
            "authorization_name": self.evidence_name, "authorization_digest": self.evidence_digest,
            "native": _native_record(result), "native_observed_model": result.observed_model,
            "process": {"status": process.status.value, "exit_code": process.returncode, "capture_complete": process.capture_complete},
            "broker_command_digest": _command_digest(held["command"]),
            "broker_argv": list(held["command"].argv),
            "permit": asdict(held["permit"]), "permit_id": held["permit_id"], "epoch": self.round_number,
            "wire_records": wire, "ledger_records": list(records),
            "native_deliveries": list(listener.native_deliveries),
            "credential_source_fingerprint": held["credential_source_fingerprint"],
            "response_model": MODEL, "session_state": _session_capture(Path(auth["private_home"]["path"]), result.session_id)}
        name, digest = write_evidence(self.coordinator.lifecycle_controller, _CATEGORY, proof)
        self.coordinator.artifacts.write_json(request.artifact_prefix + "/codex-canary-proof.json",
            {"evidence_name": name, "evidence_digest": digest})
        reopen_turn(self.coordinator, self.packet, self.seat, self.round_number, result,
            prior=held.get("prior"), peer=self.peer)


def issue_turn(coordinator, packet, seat, round_number, route, *, prior=None, peer=None):
    _route_check(route)
    request = _expected_request(coordinator, packet, seat, round_number, prior, peer)
    if coordinator.journal.inputs.skill_manifests.get("codex-canary-route") != route.digest:
        raise ProviderRequestError("canary company route is not authenticated by original RunInputs")
    initial = None
    if round_number == 2:
        initial = reopen_turn(coordinator, packet, seat, 1, prior)
        old_auth = read_evidence(coordinator.lifecycle_controller, _CATEGORY,
            initial["authorization_name"], initial["authorization_digest"])
        if old_auth["route"] != route_record(route):
            raise ProviderRequestError("canary route changed before exact resume")
        home = Path(old_auth["private_home"]["path"])
    else:
        home = coordinator.lifecycle_controller.root / "codex-canary-home"
        home.mkdir(mode=0o700)
        (home / ".config").mkdir(mode=0o700)
        (home / ".codex").mkdir(mode=0o700)
    command = request.profile.adapter.build_command(request)
    executable = _executable("codex")
    authorization = {"schema_version": "fanout-codex-canary-authorization-v1",
        "binding": _binding(coordinator, packet, seat, round_number, request), "route": route_record(route),
        "command_digest": _command_digest(command), "command_argv": list(command.argv),
        "executable": str(executable), "executable_sha256": _hash_file(executable),
        "private_home": _private_home(home),
        "initial_authorization_digest": None if initial is None else initial["authorization_digest"]}
    name, digest = write_evidence(coordinator.lifecycle_controller, _CATEGORY, authorization)
    held = {} if prior is None else {"prior": prior}
    return CanaryTurn(coordinator, packet, seat, round_number, route, request, name, digest,
        initial, peer, _ISSUER, held)


def reopen_turn(coordinator, packet, seat, round_number, result, *, prior=None, peer=None):
    """Open only original signed evidence; no sender, credential, version or session lookup."""
    try:
        request = _expected_request(coordinator, packet, seat, round_number, prior, peer)
        pointer, _ = _read_owned_json(coordinator.artifacts.root / request.artifact_prefix / "codex-canary-proof.json")
        if set(pointer) != {"evidence_name", "evidence_digest"}:
            raise ValueError("canary pointer is invalid")
        controller = coordinator.lifecycle_controller
        proof = read_evidence(controller, _CATEGORY, pointer["evidence_name"], pointer["evidence_digest"])
        authorization = read_evidence(controller, _CATEGORY, proof["authorization_name"], proof["authorization_digest"])
        route = authorization["route"]
        if route["digest"] != _sha(canonical_json({key: item for key, item in route.items() if key != "digest"})):
            raise ValueError("canary original admitted route descriptor changed")
        if (proof["schema_version"] != "fanout-codex-canary-proof-v1"
                or authorization["schema_version"] != "fanout-codex-canary-authorization-v1"
                or authorization["binding"] != _binding(coordinator, packet, seat, round_number, request)
                or authorization["route"]["digest"] != coordinator.journal.inputs.skill_manifests.get("codex-canary-route")
                or proof["native"] != _native_record(result)
                or proof["native_observed_model"] != result.observed_model
                or proof["process"] != {"status": "exit", "exit_code": 0, "capture_complete": True}
                or proof["response_model"] != MODEL or proof["epoch"] != round_number):
            raise ValueError("canary original turn binding changed")
        from .diagnostic import codex_broker_command
        from .container_broker import _digest
        original = request.profile.adapter.build_command(request)
        permit = BrokerPermit(**proof["permit"])
        options = [arg for arg in proof["broker_argv"] if arg.startswith("model_providers.fanout_broker.base_url=")]
        match = re.fullmatch(r'model_providers.fanout_broker.base_url="http://127\.0\.0\.1:([0-9]+)/v1"', options[0]) if len(options) == 1 else None
        if match is None:
            raise ValueError("canary original broker command is missing")
        expected_command = codex_broker_command(request, original, port=int(match[1]), placeholder=permit.placeholder_key)
        home = Path(authorization["private_home"]["path"])
        expected_command = replace(expected_command, argv=(authorization["executable"], *expected_command.argv[1:]),
            environment={"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config")})
        if (authorization["command_digest"] != _command_digest(original)
                or authorization["command_argv"] != list(original.argv)
                or proof["broker_argv"] != list(expected_command.argv)
                or proof["broker_command_digest"] != _command_digest(expected_command)
                or permit.provider != "codex" or permit.model != MODEL
                or permit.run_id != packet.run_id or permit.task_id != packet.task_id or permit.seat_id != seat.seat_id
                or permit.inputs_digest != coordinator.journal.inputs.digest or permit.session_id != request.session_id
                or permit.route != ("resume" if request.resume else "initial")
                or permit.upstream_route_digest != route["digest"]
                or permit.upstream_origin != "https://api.openai.com" or permit.upstream_path != "/v1/responses"
                or permit.upstream_model != MODEL):
            raise ValueError("canary exact original command or permit changed")
        raw = coordinator.artifacts.read_bytes(result.stdout_ref)
        coordinator.artifacts.read_bytes(result.stderr_ref)
        answer = coordinator.artifacts.read_bytes(result.answer_ref)
        parsed = request.profile.adapter.parse(raw)
        if (not parsed.final or parsed.error is not None or parsed.session_id != result.session_id
                or parsed.answer.encode() != answer or parsed.observed_model != result.observed_model
                or parsed.observed_model is not None and parsed.observed_model != MODEL
                or sum(_json_object(line).get("type") == "turn.completed" for line in raw.splitlines()) != 1):
            raise ValueError("canary native terminal changed")
        ledger = PermitLedger(controller.root / "codex-canary-ledger", controller)
        records = ledger.request_records(proof["permit_id"], round_number)
        if (list(records) != proof["ledger_records"] or not records
                or len(records) != len(proof["wire_records"]) or len(records) != len(proof["native_deliveries"])
                or ledger.attempt_counts(proof["permit_id"], round_number) != (len(records), 0)):
            raise ValueError("canary complete ledger set changed")
        with ledger._locked() as state:
            entry = state["permits"][proof["permit_id"]]
            if (not entry["fenced"] or entry["permit_digest"] != _digest(permit)
                    or any(state["send_claims"].get(row["request_id"]) is not True for row in records)):
                raise ValueError("canary send claim or fence is missing")
        for row, capture, delivery in zip(records, proof["wire_records"], sorted(proof["native_deliveries"], key=lambda item: item["request_id"])):
            normalized, effective, response, delivered = (coordinator.artifacts.read_bytes(ArtifactRef(**capture[name]))
                for name in ("normalized_body", "effective_body", "response_body", "delivered_body"))
            identity = row["identity"]
            if (row["state"] != "settled" or row["request_id"] != capture["request_id"]
                    or delivery != {"request_id": row["request_id"], "status": capture["http_status"],
                        "content_type": capture["content_type"], "body_sha256": _sha(delivered), "delivery": "native-http-complete"}
                    or row["response_digest"] != _sha(delivered)
                    or identity["body_sha256"] != _sha(normalized) or identity["upstream_body_sha256"] != _sha(effective)
                    or normalized != effective or _json_object(normalized).get("model") != MODEL
                    or identity["wire_model"] != MODEL or identity["upstream_model"] != MODEL
                    or capture["response_model"] != MODEL
                    or identity["upstream_route_digest"] != authorization["route"]["digest"]
                    or identity["upstream_origin"] != "https://api.openai.com" or identity["upstream_path"] != "/v1/responses"
                    or not 200 <= capture["http_status"] < 300
                    or response_model(response, capture["content_type"]) != (capture["response_id"], MODEL)
                    or response_model(delivered, capture["content_type"]) != (capture["response_id"], MODEL)):
                raise ValueError("canary exact actual wire/delivery association changed")
        return proof
    except Exception as error:
        raise ValueError("authenticated same-turn Codex canary proof is unavailable") from error


def response_model(raw: bytes, mime: str) -> tuple[str, str]:
    """Read a completed Response, retaining no requested-model inference."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= 8 * 1024 * 1024:
        raise ValueError("Codex response body is missing or oversized")
    if mime == "application/json":
        responses = [(_json_object(raw), True)]
    elif mime == "text/event-stream":
        if not raw.endswith((b"\n\n", b"\r\n\r\n")):
            raise ValueError("Codex SSE response is incomplete")
        responses = []
        completed = 0
        for block in raw.decode("utf-8").replace("\r\n", "\n").split("\n\n"):
            if not block:
                continue
            lines = block.splitlines()
            data = "\n".join(line[5:].lstrip(" ") for line in lines if line.startswith("data:"))
            if not data or data == "[DONE]":
                continue
            event = _json_object(data.encode())
            kind = event.get("type")
            if (kind in {"error", "response.failed", "response.incomplete"}
                    or event.get("error") is not None):
                raise ValueError("Codex SSE response contains a failure")
            if kind == "response.completed":
                completed += 1
            if "response" in event:
                responses.append((event["response"], kind == "response.completed"))
        if completed != 1:
            raise ValueError("Codex SSE response has no unique completion")
    else:
        raise ValueError("Codex response media is unsupported")
    identity = None
    completed_identity = None
    for response, complete in responses:
        if (type(response) is not dict
                or response.get("object", "response") != "response"
                or not isinstance(response.get("id"), str) or not response["id"]
                or response.get("model") is not None and response["model"] != MODEL
                or response.get("error") is not None
                or response.get("incomplete_details") is not None
                or (complete and (response.get("status") != "completed"
                                  or response.get("object") != "response" or response.get("model") != MODEL))
                or response.get("status", "in_progress") not in {"completed", "in_progress"}):
            raise ValueError("positive completed Codex Response.model is missing")
        if identity is not None and identity != response["id"]:
            raise ValueError("Codex response identity or model conflicts")
        identity = response["id"]
        if complete:
            completed_identity = response["id"], response["model"]
    if completed_identity is None:
        raise ValueError("positive completed Codex Response.model is missing")
    return completed_identity
