"""One controller-adopted installed read-only Council case; no transport callbacks."""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import pwd
import sys
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from .artifacts import ArtifactRef, ArtifactStore, canonical_json
from .broker import BrokerPermit
from .collaboration import MAX_SEATS, PeerPacket
from .container_broker import PermitLedger
from .container_credentials import HostCredentialSource
from .container_routes import read_machine_routes, select_route
from .container_source import runtime_source_digest
from .container_transfer_transport import admitted_request_digest
from .controller import read_evidence, write_evidence
from .diagnostic import (_QUALITY_REQUEST_CONTROLS, _quality_row_permit, _reopen_quality_catalog,
    _read_quality_request_artifact, _read_quality_request_json,
    quality_row_identity, reopen_quality_allocation, reopen_quality_predecessor,
    run_quality_readonly_turn, validate_quality_readonly_turn)
from .errors import ProviderRequestError
from .execute import ExecutionService
from .native_boundary import _hash_file
from .providers import ExecutorProfile, ProviderRequest
from .skills import SkillAdmission
from .quality import _validate_casebook, case_prompt
from .repo import validate_seat_workspace
from .verification import load_answer_synthesis

_CATEGORY = "quality-installed-case-attachment"
_CASEBOOK = "7e36393b56d37e9fd482adbde5169a25252be878c9f93a59b3fc64ddaf3ff535"
_PROVIDERS = frozenset({"claude", "codex", "agy"})
_FIELDS = {"schema", "case_id", "run_id", "task_id", "target_id", "condition",
    "execution_class", "inputs_digest", "allocation", "bindings", "casebook",
    "case_input", "catalog", "manifest", "policy", "manifest_digest", "policy_digest",
    "seats", "installation"}


@dataclass(frozen=True, slots=True)
class QualityCaseAttachment:
    """Only an existing controller evidence pointer, never a serialized capability."""

    name: str
    digest: str


def _ref(value, limit):
    if type(value) is not dict or set(value) != {"path", "digest", "size"}:
        raise ProviderRequestError("quality input reference fields differ")
    ref = ArtifactRef(**value)
    if type(ref.size) is not int or not 0 < ref.size <= limit:
        raise ProviderRequestError("quality input reference exceeds its bound")
    return ref


def require_quality_installation(value, helper, runtime_digest):
    """Reuse the complete existing delivery comparison, not a receipt-header check."""
    if type(value) is not dict or set(value) != {"repo_root", "receipt_digest",
            "helper_path", "helper_digest", "runtime_digest"}:
        raise ProviderRequestError("quality installed binding fields differ")
    helper = Path(helper)
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    targets = (home / ".agents/skills", home / ".claude/skills", home / ".gemini/config/skills")
    if (helper not in tuple(root / "llm-fanout-execute/scripts/execute.py" for root in targets)
            or value["helper_path"] != str(helper)
            or value["runtime_digest"] != runtime_digest
            or _hash_file(helper) != value["helper_digest"]
            or not any(isinstance(getattr(module, "__file__", None), str) and Path(module.__file__) == helper
                       for module in tuple(sys.modules.values()))):
        raise ProviderRequestError("quality entry is not the exact loaded installed helper")
    root = Path(__file__).resolve().parents[3]
    path = root / "components/skills/skillctl.py"
    if path.is_symlink() or not path.is_file():
        raise ProviderRequestError("quality installed receipt validator is not bundled")
    if _hash_file(path) != _hash_file(Path(value["repo_root"]) / "components/skills/skillctl.py"):
        raise ProviderRequestError("quality bundled installed validator differs from the admitted source")
    spec = importlib.util.spec_from_file_location("_fanout_quality_skillctl", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    historical = sys.modules.get("historical_only")
    if (historical is None or Path(historical.__file__).resolve() != root / "shared/lib/fanout/historical_only.py"):
        raise ProviderRequestError("quality installed validator imported foreign source")
    config = module.load_configuration(Path(value["repo_root"]), home, None)
    receipt = config.state_dir / module.RECEIPT_NAME
    if _hash_file(receipt) != value["receipt_digest"]:
        raise ProviderRequestError("quality actual installed receipt changed")
    with contextlib.redirect_stdout(io.StringIO()):
        module.doctor(config, as_json=True)
    if _hash_file(receipt) != value["receipt_digest"] or _hash_file(helper) != value["helper_digest"]:
        raise ProviderRequestError("quality installed evidence changed during comparison")


def _candidate_work_rows(allocation):
    rows = [row for row in allocation["rows"] if row["condition"] == "candidate"
            and row["role"] in {"candidate-seat", "owner-synthesis"}]
    if (sum(row["role"] == "candidate-seat" for row in rows) != 6
            or sum(row["role"] == "owner-synthesis" for row in rows) != 1 or len(rows) != 7):
        raise ProviderRequestError("quality candidate requires six seat rows and one synthesis row")
    return rows


def _case(service, value, *, helper, runtime_digest):
    if (type(service) is not ExecutionService or type(service.artifacts) is not ArtifactStore
            or type(value) is not dict or set(value) != _FIELDS
            or len(canonical_json(value)) > 64 * 1024
            or value["schema"] != "fanout-quality-installed-case-attachment-v1"
            or value["condition"] != "candidate" or value["execution_class"] != "read-only"
            or value["run_id"] != service.inputs.run_id
            or value["inputs_digest"] != service.inputs.digest
            or value["target_id"] != "quality-" + value["case_id"]):
        raise ProviderRequestError("quality attachment exact service binding differs")
    work = [task for task in service.plan.tasks if task.kind == "work"]
    if (service.plan.schema_version != "v1" or len(work) != 1
            or work[0].id != value["task_id"] or work[0].execution_class != "read-only"
            or work[0].reconciliation_policy != "synthesis-required" or work[0].depends_on):
        raise ProviderRequestError("quality attachment requires one v1 read-only synthesis task")
    task = work[0]
    preparation = service.preparations[task.id]
    policy = preparation.policy
    if (frozenset(policy.executor_ids) != _PROVIDERS or len(policy.executor_ids) != 3
            or policy.rounds != 2 or policy.retries != 0
            or len(preparation.seats) != 3 or set(value["seats"]) != _PROVIDERS):
        raise ProviderRequestError("quality attachment requires exactly three seats and two zero-retry rounds")
    require_quality_installation(value["installation"], helper, runtime_digest)
    store, controller = service.artifacts, service.lifecycle_controller
    allocation = reopen_quality_allocation(controller, store, tuple(value["allocation"]), bindings=value["bindings"])
    bindings = value["bindings"]
    if (allocation["case_id"] != value["case_id"] or bindings["inputs"] != service.inputs.digest
            or bindings["source"] != runtime_source_digest()
            or bindings["profile"] != hashlib.sha256(canonical_json(dict(service.inputs.provider_profiles))).hexdigest()):
        raise ProviderRequestError("quality allocation source, profiles or inputs differ")
    book_ref = _ref(value["casebook"], 2 * 1024 * 1024)
    if book_ref.digest != _CASEBOOK or bindings["casebook"] != _CASEBOOK:
        raise ProviderRequestError("quality casebook is not the approved original")
    book = _validate_casebook(json.loads(_read_quality_request_artifact(store, book_ref, 2 * 1024 * 1024)))
    selected = next((case for case in book["cases"] if case["id"] == value["case_id"]), None)
    if selected is None or selected["kind"] != "council":
        raise ProviderRequestError("quality attachment case is not Council")
    prompt = case_prompt(book, value["case_id"])
    input_ref = _ref(value["case_input"], 1024 * 1024)
    if _read_quality_request_artifact(store, input_ref, 1024 * 1024) != canonical_json({"schema": "fanout-quality-case-input-v1",
            "casebook": _CASEBOOK, "case_id": value["case_id"], "prompt": prompt}):
        raise ProviderRequestError("quality compiled question differs from the admitted original")
    require_quality_question(service, preparation, prompt, selected["mode"])
    _reopen_quality_catalog(store, value["catalog"])
    projection = {"case_input": value["case_input"], "catalog": value["catalog"]}
    if bindings["projection"] != hashlib.sha256(canonical_json(projection)).hexdigest():
        raise ProviderRequestError("quality accepted input/catalog projection differs")
    manifest = read_machine_routes(Path(value["manifest"]), Path(value["policy"]))
    if (manifest.manifest_digest != value["manifest_digest"] or manifest.policy_digest != value["policy_digest"]):
        raise ProviderRequestError("quality machine parser digests differ")
    routes = {}
    for seat in preparation.seats:
        selected = value["seats"][seat.executor_id]
        profile, timeout = service.coordinator._selected_profile(preparation.packet, seat, policy, service.inputs)
        if (type(selected) is not dict or set(selected) != {"seat_id", "workspace_digest", "profile_digest", "route_id"}
                or selected["seat_id"] != seat.seat_id or selected["profile_digest"] != profile.digest
                or selected["workspace_digest"] != seat.workspace_verification.evidence_digest):
            raise ProviderRequestError("quality seat profile or workspace differs")
        validate_seat_workspace(controller, seat.workspace_verification)
        routes[seat.executor_id] = select_route(manifest, route_id=selected["route_id"],
            provider=seat.executor_id, requested_model=profile.requested_model,
            execution_class="read-only", data_classification="protected")
    rows = _candidate_work_rows(allocation)
    seats = [row for row in rows if row["role"] == "candidate-seat"]
    owners = [row for row in rows if row["role"] == "owner-synthesis"]
    for seat in preparation.seats:
        own = [row for row in seats if row["permit"]["seat_id"] == seat.seat_id]
        originals = [row for row in own if row["phase"] == "initial" and row["initial"] is None]
        resumes = [row for row in own if row["phase"] == "resume"]
        if (len(own) != 2 or len(originals) != 1 or len(resumes) != 1
                or resumes[0]["initial"] != quality_row_identity(originals[0])
                or (seat.executor_id == "claude" and not originals[0]["permit"]["session_id"])):
            raise ProviderRequestError("quality frozen initial/resume allocation differs")
    for row in rows:
        permit = row["permit"]
        provider = permit["provider"]
        seat = next((seat for seat in preparation.seats if seat.seat_id == permit["seat_id"]), None)
        route = routes.get(provider)
        if seat is None or seat.executor_id != provider or route is None:
            raise ProviderRequestError("quality allocated row has no admitted workspace")
        profile, timeout = service.coordinator._selected_profile(preparation.packet, seat, policy, service.inputs)
        p = route.profile
        path = ("/v1/responses" if provider == "codex" else
            f"/v1/projects/{p.project}/locations/{p.location}/publishers/" +
            (f"anthropic/models/{p.effective_model}:streamRawPredict" if provider == "claude" else
             f"google/models/{p.effective_model}:streamGenerateContent?alt=sse"))
        if (any(permit[key] != value[key] for key in ("run_id", "task_id", "target_id"))
                or permit["inputs_digest"] != service.inputs.digest or row["profile"] != profile.digest
                or permit["model"] != profile.requested_model or row["process_seconds"] != timeout
                or permit["upstream_route_digest"] != route.digest or permit["upstream_origin"] != p.origin
                or permit["upstream_model"] != p.effective_model or permit["upstream_path"] != path
                or row["catalog"] != (value["catalog"]["projected"]["digest"] if provider == "codex" else None)
                or row["blocker"] is not None):
            raise ProviderRequestError("quality frozen row route, model, profile or identity differs")
        if provider == "claude" and row["initial"] is None:
            try:
                if str(uuid.UUID(permit["session_id"])) != permit["session_id"]:
                    raise ValueError("canonical UUID")
            except (TypeError, ValueError, AttributeError) as error:
                raise ProviderRequestError("quality Claude initial requires its frozen assigned UUID") from error
    if owners[0]["initial"] is not None or owners[0]["phase"] != "initial":
        raise ProviderRequestError("quality owner synthesis must be a separate initial row")
    return task, preparation, allocation, routes


def require_quality_question(service, preparation, question, mode):
    """Join the original prompt to the existing fixed planner source and task template."""
    root = Path(__file__).resolve().parents[3]
    path = root / "shared/skills/llm-fanout-plan/scripts/plan.py"
    if path.is_symlink() or not path.is_file():
        raise ProviderRequestError("quality fixed question planner source differs")
    spec = importlib.util.spec_from_file_location("_fanout_quality_question_planner", path)
    planner = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = planner
    spec.loader.exec_module(planner)
    source = planner._question_source(question, mode).encode()
    task = next(task for task in service.plan.tasks if task.id == preparation.packet.task_id)
    expected = planner._question_draft(source, tuple(task.required_skills), task.none_reason,
        tuple(preparation.policy.executor_ids), mode)["tasks"][0]
    expected["reconciliation_policy"] = "synthesis-required"
    if (preparation.packet.source_markdown != source
            or canonical_json(task.to_dict()) != canonical_json(expected)):
        raise ProviderRequestError("quality compiled question source or task template differs")


def adopt_quality_case(service, owner, value, *, helper, runtime_digest):
    if type(service) is not ExecutionService:
        raise ProviderRequestError("quality adoption requires the actual execution service")
    service._authorize_command(owner)
    _case(service, value, helper=helper, runtime_digest=runtime_digest)
    if (service.journal.state.seat_phases or service._execution_state(value["task_id"]).barriers
            or service.scheduler.task_phase(value["task_id"]) != "scheduled"):
        raise ProviderRequestError("quality attachment must precede every turn and barrier")
    name, digest = write_evidence(service.lifecycle_controller, _CATEGORY, value)
    return QualityCaseAttachment(name, digest)


def reopen_quality_case(service, attachment, *, helper, runtime_digest):
    if type(attachment) is not QualityCaseAttachment:
        raise ProviderRequestError("quality attachment requires its typed authenticated pointer")
    value = read_evidence(service.lifecycle_controller, _CATEGORY, attachment.name, attachment.digest)
    return value, _case(service, value, helper=helper, runtime_digest=runtime_digest)


def _quality_case_request(service, value, preparation, seat, row, *, peer=None, sources=None):
    packet, coordinator = preparation.packet, service.coordinator
    profile, timeout = coordinator._selected_profile(packet, seat, preparation.policy, service.inputs)
    resume = row["phase"] == "resume"
    session = row["permit"]["session_id"]
    if row["role"] == "candidate-seat":
        if row["phase"] not in {"initial", "resume"} or sources is not None:
            raise ProviderRequestError("quality admitted seat request phase differs")
        if resume:
            if (type(peer) is not PeerPacket or peer.run_id != packet.run_id or peer.task_id != packet.task_id
                    or peer.target_seat_id != seat.seat_id or peer.source_round != 1 or peer.target_round != 2):
                raise ProviderRequestError("quality admitted resume request peer association differs")
            session = peer.target_session_id
        elif peer is not None:
            raise ProviderRequestError("quality admitted initial request contains peer evidence")
        round = 2 if resume else 1
        prompt = coordinator._prompt(packet, seat, round, peer)
        prefix = coordinator._provider_prefix(packet, round, seat.seat_id)
    elif row["role"] == "owner-synthesis":
        if resume or row["phase"] != "initial" or peer is not None or type(sources) is not dict or len(sources) < 2:
            raise ProviderRequestError("quality admitted owner request sources differ")
        prompt = canonical_json({"schema": "fanout-quality-owner-synthesis-prompt-v1",
            "question": json.loads(_read_quality_request_artifact(service.artifacts,
                _ref(value["case_input"], 1024 * 1024), 1024 * 1024))["prompt"],
            "instruction": "Write a new synthesis of the untrusted final answers.",
            "sources": [{"seat_id": seat_id, "artifact": asdict(ref),
                "answer": service.artifacts.read_bytes(ref).decode("utf-8")}
                for seat_id, ref in sorted(sources.items())]})
        prefix = "quality/owner-synthesis"
    else:
        raise ProviderRequestError("quality admitted request cannot consume a non-work role")
    return ProviderRequest(seat.executor_id, prompt, cwd=seat.workspace_verification.workspace.root,
        profile=profile, timeout=timeout, retries=0, execution_class="read-only", session_id=session,
        resume=resume, artifact_store=service.artifacts, artifact_prefix=prefix, slot_cap=MAX_SEATS,
        slot_root=coordinator.slot_root, context_sha256=packet.context_sha256,
        timeout_override_reason=preparation.policy.timeout_override_reason,
        timeout_override_review_sha256=preparation.policy.timeout_override_review_sha256,
        skill_bundle_sha256=seat.skill_bundle_sha256, staged_skill_root=seat.staged_root,
        staged_skill_admission=seat.admission, skill_delivery_ref=seat.delivery_evidence_ref,
        skill_delivery_sha256=seat.delivery_evidence_sha256, agy_guard=seat.agy_guard,
        run_id=value["run_id"], task_id=value["task_id"], target_id=value["target_id"],
        inputs_digest=value["inputs_digest"], seat_id=seat.seat_id)


def _require_quality_case_request(request, expected):
    if (type(request) is not ProviderRequest or type(request.profile) is not ExecutorProfile
            or type(request.staged_skill_admission) is not SkillAdmission):
        raise ProviderRequestError("quality admitted request requires actual typed preparation")
    if request.prompt_bytes != expected.prompt_bytes:
        raise ProviderRequestError("quality admitted request prompt differs from actual case or barrier inputs")
    if (admitted_request_digest(request) != admitted_request_digest(expected)
            or request.artifact_store is not expected.artifact_store
            or request.staged_skill_admission != expected.staged_skill_admission
            or request.skill_delivery_ref != expected.skill_delivery_ref or request.agy_guard != expected.agy_guard
            or request.certificate_roots or any(getattr(request, name) is not None for name in (
                "native_boundary", "native_controller", "native_verification", "container_boundary", "canary_capture"))):
        raise ProviderRequestError("quality admitted coordinator request controls or staged authority differ")


def _reopen_quality_case_request(service, value, preparation, seat, row, native, *, sources=None):
    from .providers import ProviderRegistry
    descriptor = _read_quality_request_json(service.artifacts, _ref(native["request"], 64 * 1024))
    if (type(descriptor) is not dict or set(descriptor) != {"schema", "controls", "prompt", "profile"}
            or descriptor["schema"] != "fanout-quality-request-v1"
            or type(descriptor["controls"]) is not dict
            or set(descriptor["controls"]) != _QUALITY_REQUEST_CONTROLS):
        raise ProviderRequestError("quality delivered retained request fields differ")
    profile = ExecutorProfile.from_dict(_read_quality_request_json(service.artifacts,
        _ref(descriptor["profile"], 64 * 1024)), adapter=ProviderRegistry.default().require(seat.executor_id))
    request = ProviderRequest(**descriptor["controls"], prompt=_read_quality_request_artifact(service.artifacts,
        _ref(descriptor["prompt"], 1024 * 1024), 1024 * 1024), profile=profile,
        staged_skill_admission=seat.admission, skill_delivery_ref=seat.delivery_evidence_ref,
        artifact_store=service.artifacts, agy_guard=seat.agy_guard)
    peer = None
    if row["role"] == "candidate-seat" and row["phase"] == "resume":
        state = service._execution_state(value["task_id"])
        if len(state.barriers) != 2:
            raise ProviderRequestError("quality delivered resume lacks the exact first barrier")
        barrier = service._restore_barrier(state.barriers[0], preparation.packet)
        peer = service.coordinator.build_peer_packet(preparation.packet, barrier, target_seat_id=seat.seat_id)
    expected = _quality_case_request(service, value, preparation, seat, row, peer=peer, sources=sources)
    _require_quality_case_request(request, expected)
    return request


class QualityCaseRunner:
    """The only provider dependency of the closed entry; no caller-supplied producer."""

    def __init__(self, attachment, *, helper, runtime_digest):
        if type(attachment) is not QualityCaseAttachment:
            raise ProviderRequestError("quality runner requires the exact attachment type")
        self._attachment, self._helper, self._runtime_digest = attachment, helper, runtime_digest
        self._service = None

    def bind(self, service):
        if self._service is not None or type(service) is not ExecutionService:
            raise ProviderRequestError("quality runner service binding differs")
        reopen_quality_case(service, self._attachment, helper=self._helper, runtime_digest=self._runtime_digest)
        self._service = service

    def _context(self):
        return reopen_quality_case(self._service, self._attachment,
            helper=self._helper, runtime_digest=self._runtime_digest)

    def __call__(self, request, *, registry):
        value, (task, preparation, allocation, routes) = self._context()
        if registry is not self._service.coordinator.registry:
            raise ProviderRequestError("quality runner registry differs from the admitted service")
        if (type(request) is not ProviderRequest or type(request.profile) is not ExecutorProfile
                or type(request.staged_skill_admission) is not SkillAdmission):
            raise ProviderRequestError("quality runner requires the actual coordinator request")
        seat = next((seat for seat in preparation.seats if seat.executor_id == request.executor_id), None)
        if seat is None:
            raise ProviderRequestError("quality runner executor differs")
        phase = "resume" if request.resume else "initial"
        row = next(row for row in allocation["rows"] if row["role"] == "candidate-seat"
            and row["condition"] == "candidate" and row["phase"] == phase
            and row["permit"]["seat_id"] == seat.seat_id)
        packet, coordinator = preparation.packet, self._service.coordinator
        peer = None
        if request.resume:
            state = self._service._execution_state(task.id)
            if len(state.barriers) != 1:
                raise ProviderRequestError("quality resume lacks the exact first barrier")
            barrier = self._service._restore_barrier(state.barriers[0], packet)
            peer = coordinator.build_peer_packet(packet, barrier, target_seat_id=seat.seat_id)
        expected = _quality_case_request(self._service, value, preparation, seat, row, peer=peer)
        if not request.resume and request.session_id not in (None, expected.session_id):
            raise ProviderRequestError("quality coordinator initial session substitution")
        identities = {key: value[key] for key in ("run_id", "task_id", "target_id", "inputs_digest")}
        identities["seat_id"] = seat.seat_id
        if any(getattr(request, key) not in (None, expected) for key, expected in identities.items()):
            raise ProviderRequestError("quality coordinator identity substitution")
        session = request.session_id if request.resume else row["permit"]["session_id"]
        request = replace(request, **identities, session_id=session)
        _require_quality_case_request(request, expected)
        initial, proof = None, None
        if request.resume:
            pointer = read_evidence(self._service.lifecycle_controller, "quality-installed-seat-turn",
                *self._turn_pointer(seat.seat_id, "initial"))
            if pointer.keys() != {"attachment", "seat_id", "phase", "proof"} or pointer != {
                    "attachment": [self._attachment.name, self._attachment.digest],
                    "seat_id": seat.seat_id, "phase": "initial", "proof": pointer["proof"]}:
                raise ProviderRequestError("quality predecessor turn attachment differs")
            proof = tuple(pointer["proof"])
            initial = reopen_quality_predecessor(self._service.lifecycle_controller, self._service.artifacts,
                request, proof, verification=seat.workspace_verification,
                authority=tuple(value["allocation"]), bindings=value["bindings"], initial_digest=row["initial"])
        terminal, proof = self._turn(request, value, row, routes, seat, initial, proof)
        pointer = write_evidence(self._service.lifecycle_controller, "quality-installed-seat-turn", {
            "attachment": [self._attachment.name, self._attachment.digest], "seat_id": seat.seat_id,
            "phase": phase, "proof": list(proof)})
        self._service.artifacts.write_json(self._pointer_path(seat.seat_id, phase), {"pointer": list(pointer)})
        return terminal.result

    def _pointer_path(self, seat, phase):
        digest = hashlib.sha256(canonical_json((self._attachment.digest, seat, phase))).hexdigest()
        return f"quality/installed-turns/{digest}.json"

    def _turn_pointer(self, seat, phase):
        from .container_certification import _read_owned_json
        store = self._service.artifacts
        path = self._pointer_path(seat, phase)
        value, raw = _read_owned_json(store.root / path)
        _read_quality_request_artifact(store, ArtifactRef(path, hashlib.sha256(raw).hexdigest(), len(raw)), 1024)
        if type(value) is not dict or set(value) != {"pointer"} or type(value["pointer"]) is not list or len(value["pointer"]) != 2:
            raise ProviderRequestError("quality turn pointer differs")
        return tuple(value["pointer"])

    def _turn(self, request, value, row, routes, seat, initial=None, proof=None):
        service = self._service
        route = routes[request.executor_id]
        permit = _quality_row_permit(row, request.session_id if request.resume else None)
        claim = {"allocation": value["allocation"][1], "row": row}
        path = service.lifecycle_controller.root / "locks" / ("quality-row-" + hashlib.sha256(canonical_json(claim)).hexdigest())
        if path.exists() or path.is_symlink():
            raise ProviderRequestError("quality row already consumed; no credential load or replay")
        # The existing native command validator repeats all request/row checks before launch.
        service.coordinator.registry.assert_installed_version(request.profile)
        credential = HostCredentialSource(route.profile).load()
        return run_quality_readonly_turn(request, controller=service.lifecycle_controller,
            store=service.artifacts, authority=tuple(value["allocation"]),
            ledger=PermitLedger(service.lifecycle_controller.root / "locks/spend", service.lifecycle_controller),
            permit=permit, epoch=row["epoch"], bindings=value["bindings"], role=row["role"],
            phase=row["phase"], condition="candidate", route=route, credential=credential,
            verification=seat.workspace_verification, other_target_roots=(),
            denied_owner_roots=(service.baseline.repository,),
            catalog=value["catalog"] if request.executor_id == "codex" else None,
            initial=initial, initial_digest=row["initial"], initial_proof=proof)

    def synthesize_and_submit(self, owner):
        """Use the reserved row and exact final sources, then ordinary fresh verification."""
        service = self._service
        service._authorize_command(owner)
        value, (task, preparation, allocation, routes) = self._context()
        if service.scheduler.task_phase(task.id) != "reconciliation-pending":
            raise ProviderRequestError("quality synthesis requires ordinary pending reconciliation")
        state = service._execution_state(task.id)
        if len(state.barriers) != 2:
            raise ProviderRequestError("quality synthesis requires two complete barriers")
        barrier = service._restore_barrier(state.barriers[-1], preparation.packet)
        sources = {terminal.seat_id: terminal.answer_ref for terminal in barrier.valid_terminals}
        if len(sources) < 2 or any(ref is None for ref in sources.values()):
            raise ProviderRequestError("quality synthesis final sources are incomplete")
        row = next(row for row in allocation["rows"] if row["condition"] == "candidate" and row["role"] == "owner-synthesis")
        seat = next(seat for seat in preparation.seats if seat.seat_id == row["permit"]["seat_id"])
        request = _quality_case_request(service, value, preparation, seat, row, sources=sources)
        terminal, proof = self._turn(request, value, row, routes, seat)
        verification = service.verify_read_only_synthesis(task.id, source_seat_ids=tuple(sorted(sources)),
            synthesizer_id="quality-owner", answer=terminal.result.answer.encode("utf-8"), owner=owner)
        completion = service.submit(task.id, verification, owner=owner)
        turns = []
        for phase in ("initial", "resume"):
            for assigned in preparation.seats:
                pointer = read_evidence(service.lifecycle_controller, "quality-installed-seat-turn",
                    *self._turn_pointer(assigned.seat_id, phase))
                turns.append(pointer)
        delivery = write_evidence(service.lifecycle_controller, "quality-installed-case-delivery", {
            "schema": "fanout-quality-installed-case-delivery-v1",
            "attachment": [self._attachment.name, self._attachment.digest],
            "run_id": value["run_id"], "task_id": task.id, "inputs_digest": value["inputs_digest"],
            "plan_digest": state.decision_plan_sha256, "plan_revision": state.decision_plan_revision,
            "barriers": [asdict(ref) for ref in state.barriers],
            "sources": {seat_id: asdict(ref) for seat_id, ref in sources.items()},
            "turns": turns, "synthesis_turn": list(proof), "final": asdict(completion.artifact),
            "verification": [verification.evidence_name, verification.evidence_digest]})
        reopen_quality_delivery(service, self._attachment, delivery,
            helper=self._helper, runtime_digest=self._runtime_digest)
        return delivery


def reopen_quality_delivery(service, attachment, proof, *, helper, runtime_digest):
    value, (task, preparation, allocation, _) = reopen_quality_case(service, attachment,
        helper=helper, runtime_digest=runtime_digest)
    record = read_evidence(service.lifecycle_controller, "quality-installed-case-delivery", *proof)
    if record.keys() != {"schema", "attachment", "run_id", "task_id", "inputs_digest",
            "plan_digest", "plan_revision", "barriers", "sources", "turns", "synthesis_turn", "final", "verification"}:
        raise ProviderRequestError("quality delivery fields differ")
    service._validate_completed_results()
    state = service._execution_state(task.id)
    completed = service.scheduler.result_for(task.id)
    if (record["schema"] != "fanout-quality-installed-case-delivery-v1"
            or record["attachment"] != [attachment.name, attachment.digest]
            or any(record[key] != value[key] for key in ("run_id", "task_id", "inputs_digest"))
            or service.scheduler.task_phase(task.id) != "completed" or completed is None
            or record["final"] != asdict(completed.artifact)
            or record["plan_digest"] != state.decision_plan_sha256
            or record["plan_revision"] != state.decision_plan_revision
            or len(state.barriers) != 2 or record["barriers"] != [asdict(ref) for ref in state.barriers]):
        raise ProviderRequestError("quality delivery lacks exact ordinary service completion")
    barrier = service._restore_barrier(state.barriers[-1], preparation.packet)
    sources = {terminal.seat_id: terminal.answer_ref for terminal in barrier.valid_terminals}
    if record["sources"] != {seat: asdict(ref) for seat, ref in sources.items()}:
        raise ProviderRequestError("quality delivered sources differ from the actual final barrier")
    verification = load_answer_synthesis(service.lifecycle_controller, service.artifacts,
        run_id=value["run_id"], task_id=task.id, answer_ref=completed.artifact)
    if (record["verification"] != [verification.evidence_name, verification.evidence_digest]
            or dict(verification.source_answers) != sources):
        raise ProviderRequestError("quality delivered synthesis verification differs")
    if type(record["turns"]) is not list or len(record["turns"]) != 6:
        raise ProviderRequestError("quality delivery does not account for six actual seat turns")
    work_rows = {quality_row_identity(row): row for row in _candidate_work_rows(allocation)}
    turns = record["turns"] + [{"proof": record["synthesis_turn"]}]
    seen = set()
    for index, turn in enumerate(turns):
        native = read_evidence(service.lifecycle_controller, "quality-readonly-turn", *turn["proof"])
        command = read_evidence(service.lifecycle_controller, "quality-native-command", *native["command"])
        row = command["registration"]["row"]
        identity = quality_row_identity(row)
        if (command["allocation"] != value["allocation"] or command["bindings"] != value["bindings"]
                or work_rows.get(identity) != row or identity in seen
                or row["role"] != ("candidate-seat" if index < 6 else "owner-synthesis")):
            raise ProviderRequestError("quality delivered native turn is unrelated or repeated")
        if index < 6 and turn != {"attachment": [attachment.name, attachment.digest],
                "seat_id": row["permit"]["seat_id"], "phase": row["phase"], "proof": turn["proof"]}:
            raise ProviderRequestError("quality delivered seat turn attachment differs")
        seen.add(identity)
        seat = next((seat for seat in preparation.seats if seat.seat_id == row["permit"]["seat_id"]), None)
        if seat is None:
            raise ProviderRequestError("quality delivered request has no admitted seat")
        request = _reopen_quality_case_request(service, value, preparation, seat, row, native,
            sources=sources if row["role"] == "owner-synthesis" else None)
        validate_quality_readonly_turn(service.lifecycle_controller, service.artifacts, request, tuple(turn["proof"]))
        _require_turn_chain(service, value, request, native, command, seat)
        terminal = read_evidence(service.lifecycle_controller, "brokered-provider-diagnostic", *native["terminal"])
        if row["role"] == "candidate-seat":
            round = 1 if row["phase"] == "initial" else 2
            actual_barrier = service._restore_barrier(state.barriers[round - 1], preparation.packet)
            actual_terminal = next((item for item in actual_barrier.valid_terminals if item.seat_id == request.seat_id), None)
            if (actual_terminal is None or actual_terminal.executor_id != request.executor_id
                    or actual_terminal.session_id != terminal["session_id"]
                    or actual_terminal.answer_ref.digest != native["final"]["digest"]
                    or actual_terminal.stdout_ref.digest != native["stdout"]["digest"]
                    or actual_terminal.stderr_ref.digest != native["stderr"]["digest"]
                    or service.artifacts.read_bytes(actual_terminal.answer_ref) != service.artifacts.read_bytes(ArtifactRef(**native["final"]))):
                raise ProviderRequestError("quality actual service barrier does not join its native raw answer")
        elif row["role"] != "owner-synthesis" or completed.artifact.digest != native["final"]["digest"]:
            raise ProviderRequestError("quality delivered owner answer differs from the native synthesis")
    if seen != work_rows.keys():
        raise ProviderRequestError("quality delivery does not consume exactly the admitted work rows")
    return record


def _require_turn_chain(service, value, request, native, command, seat):
    from . import native_boundary as boundary_api
    from .diagnostic import codex_broker_command
    from .eval_provider import quality_session_command
    controller = service.lifecycle_controller
    terminal = read_evidence(controller, "brokered-provider-diagnostic", *native["terminal"])
    registration = command["registration"]
    initial_ref = None
    if not request.resume:
        boundary_api._require_broker_initial(controller, seat.workspace_verification,
            replace(request, resume=True, session_id=terminal["session_id"]), tuple(native["terminal"]))
    else:
        prior = reopen_quality_predecessor(controller, service.artifacts, request,
            tuple(registration["initial_receipt"]), verification=seat.workspace_verification,
            authority=tuple(value["allocation"]), bindings=value["bindings"],
            initial_digest=registration["row"]["initial"])
        initial_ref = (prior.receipt_name, prior.receipt_digest)
    boundary = read_evidence(controller, "native-seat-boundary",
        terminal["boundary_receipt_digest"] + ".json", terminal["boundary_receipt_digest"])
    workspace = seat.workspace_verification.workspace.root
    info = workspace.stat()
    state = controller.root / "native-seat-state" / hashlib.sha256(canonical_json((
        request.run_id, request.task_id, request.target_id, request.seat_id))).hexdigest()
    executable_name = request.profile.initial_argv[0]
    if request.executor_id == "agy":
        executable_name = str(boundary_api._AGY_DIRECT_BINARY)
    executable = boundary_api._executable(executable_name)
    denied = (service.baseline.repository,)
    grant = boundary_api._staged_skill_grant(request, denied, workspace, controller.root)
    policy = boundary_api._policy(workspace, state, executable, denied,
        None if grant is None else Path(grant["root"]), execution_class="read-only", broker_port=boundary["broker_port"])
    policy_digest = hashlib.sha256(policy).hexdigest()
    profile_path = controller.root / "native-profiles" / (policy_digest + ".sb")
    actual = quality_session_command(request, catalog_path=str(state / "home/.codex/quality-catalog.json")
        if request.executor_id == "codex" else None)
    actual = replace(actual, max_output_bytes=8 * 1024 * 1024)
    if request.executor_id == "codex":
        actual = codex_broker_command(request, actual, port=boundary["broker_port"],
            placeholder=registration["permit"]["placeholder"])
    if request.executor_id == "agy":
        actual = replace(actual, argv=(str(boundary_api._AGY_DIRECT_BINARY), *actual.argv[1:]))
    expected = {"controller_id": controller.controller_id, "workspace": str(workspace),
        "workspace_device": info.st_dev, "workspace_inode": info.st_ino,
        "workspace_receipt": seat.workspace_verification.evidence_digest, "cwd": str(workspace),
        "profile_digest": request.profile.digest, "route": boundary_api._route(request),
        "state_root": str(state), "executable": str(executable), "executable_digest": _hash_file(executable),
        "resolved_argv": [str(executable), *actual.argv[1:]], "command_digest": boundary_api._command_digest(actual),
        "profile_path": str(profile_path), "profile_sha256": policy_digest,
        "denied_paths": [str(service.baseline.repository)]}
    expected.update({key: getattr(request, key) for key in ("run_id", "task_id", "target_id", "seat_id", "inputs_digest")})
    if (any(boundary.get(key) != expected_value for key, expected_value in expected.items())
            or boundary.get("staged_skill_grant") != grant or _hash_file(profile_path, mode=0o400) != policy_digest
            or boundary["broker_permit_sha256"] != hashlib.sha256(canonical_json(registration["permit"])).hexdigest()):
        raise ProviderRequestError("quality delivered native boundary chain differs")
    admission = read_evidence(controller, "native-broker-diagnostic-admission",
        boundary["broker_admission_name"], boundary["broker_admission_digest"])
    binding = (boundary["broker_port"], boundary["broker_binding_id"], boundary["broker_permit_sha256"], "")
    if admission != boundary_api._broker_admission_payload(request, seat.workspace_verification.evidence_digest,
            expected["command_digest"], binding, initial_ref, quality_admission=tuple(native["command"])):
        raise ProviderRequestError("quality delivered native broker admission differs")
