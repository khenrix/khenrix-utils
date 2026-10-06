"""Private synthetic container probe. No certificate or caller-repository authority.

The fake route exercises admission and receipt policy without provider spend.
The live route remains an explicit, private diagnostic and never opens the
public provider gate.
"""
from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import http.client
import importlib.util
import json
import os
import re
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import uuid
from dataclasses import asdict, dataclass, field, replace
from itertools import islice
from pathlib import Path
from types import SimpleNamespace
from types import CodeType
from typing import Mapping

if __package__ in (None, ""):
    bootstrap_path = Path(__file__).resolve().parents[1] / "shared/skills/llm-fanout-execute/scripts/execute.py"
    bootstrap_spec = importlib.util.spec_from_file_location("_fanout_checkout_bootstrap", bootstrap_path)
    bootstrap = importlib.util.module_from_spec(bootstrap_spec)
    sys.modules[bootstrap_spec.name] = bootstrap
    bootstrap_spec.loader.exec_module(bootstrap)
    bootstrap._runtime()

from shared.lib.fanout.artifacts import ArtifactRef, ArtifactStore, canonical_json
from shared.lib.fanout.broker import Broker, BrokerPermit, BrokerRejected, BrokerResponse
from shared.lib.fanout.container_boundary import (
    ContainerAdmission, PinnedDockerTransport, SessionStore,
    issue_container_boundary, run_container_turn,
)
from shared.lib.fanout.container_broker import (
    FrameStream, HostRelay, PermitLedger, _digest, attempt_limit, auxiliary_attempt_limit,
)
from shared.lib.fanout.container_credentials import HostCredentialSource, RouteCredentialError
from shared.lib.fanout.container_certification import (
    CertificationRejected, PrivateProbeDecision, read_private_route_evidence,
)
from shared.lib.fanout.container_source import release_source_binding
from shared.lib.fanout.container_routes import (
    MachineRoutes, PreparedOutbound, RouteAdmission, RouteProfile, RouteRejected,
    _open_parent, _read_owned_json, read_machine_routes, select_route,
)
from shared.lib.fanout.container_runtime import (
    ContainerHandle, ContainerLauncher, ContainerSpec, GuestSocketAttestation,
    _RecoveryAssociation, decode_launch_record, private_volume_names, seat_socket_path,
)
from shared.lib.fanout.container_source import (
    HostRuntimeIdentity, RuntimeIdentityError, SourceClosureError,
    _read_source, _source_paths, host_runtime_identity, runtime_source_digest,
)
from shared.lib.fanout.container_upstream import (
    HostUpstream, _error_projection, _FAILURE_PHASES, _FAILURE_CATEGORIES,
    _FAILURE_OMISSIONS,
)
from shared.lib.fanout.container_transfer import (
    TransferIdentity, snapshot_manifest,
)
from shared.lib.fanout.container_transfer_transport import issue_input_source_grant
from shared.lib.fanout.container_transfer_transport import CandidateExportReceipt
from shared.lib.fanout.container_vm import VMProbe
from shared.lib.fanout.controller import (
    LifecycleCapability, create_lifecycle_controller, read_evidence,
    resume_lifecycle_controller, write_evidence,
)
from shared.lib.fanout.errors import LifecycleError
from shared.lib.fanout.evaluation import JudgeRequest, MakaJudgeAdapter
from shared.lib.fanout.maka_transport import HarborMakaTransport
from shared.lib.fanout.process import ProcessCommand, ProcessResult, ProcessStatus
from shared.lib.fanout.providers import ProviderRegistry, ProviderRequest, ProviderRequestError
from shared.lib.fanout.repo import RepositoryBaseline, capture_repository_baseline
from shared.lib.fanout.verification import create_candidate_from_export


PROVIDERS = ("claude", "codex", "agy")
ROUTES = ("initial", "resume")
ISOLATION_TARGETS = ("caller", "sibling", "git_objects", "credentials",
                     "docker_socket", "host_home", "vm_network")
SCHEMA = "fanout-container-private-probe-v1"
_SOURCE_ROOT = Path(__file__).resolve().parents[1]
_SHA = set("0123456789abcdef")
_LEGACY_KEY_ENVS = ("FANOUT_ANTHROPIC_API_KEY", "FANOUT_OPENAI_API_KEY",
                    "FANOUT_GEMINI_API_KEY")
_SPEND_SCOPE = [f"{provider}:{route}" for provider in PROVIDERS for route in ROUTES]
_MAX_MAIN_REQUESTS = 4
_SPEND_CEILING = {"max_model_calls": 24,
                  "max_request_bytes_per_call": 1024 * 1024,
                  "max_response_bytes_per_call": 8 * 1024 * 1024,
                  "max_output_tokens_per_call": 8192}


@dataclass(frozen=True, slots=True)
class QualityAssessment:
    isolation_passed: bool
    ballot_digest: str
    ballots: tuple[dict[str, object], ...] = field(default=(), repr=False)


@dataclass(frozen=True, slots=True)
class _LiveAdmission:
    source_sha256: str
    runtime: HostRuntimeIdentity
    image_digest: str
    cli_digests: dict[str, str]
    approval_reference: str
    observation_date: str
    manifest_path: Path
    policy_path: Path
    manifest: MachineRoutes
    selected: dict[str, RouteAdmission]
    private_run_root: Path
    approval_packet_path: Path
    approval_packet_digest: str


def _owner_packet(private_root: Path, reference: str, observed: str,
                  manifest: MachineRoutes,
                  selected: Mapping[str, RouteAdmission]) -> dict[str, object]:
    return {
        "schema": "fanout-container-owner-approval-v1",
        "reference": reference, "observation_date": observed,
        "private_run_root": str(private_root),
        "source_sha256": runtime_source_digest(_SOURCE_ROOT),
        "machine_id": manifest.machine_id,
        "manifest_digest": manifest.manifest_digest,
        "policy_digest": manifest.policy_digest,
        "route_digests": {provider: selected[provider].digest for provider in PROVIDERS},
        "spend_scope": _SPEND_SCOPE, "spend_ceiling": _SPEND_CEILING,
    }


def _check_owner_packet(path: Path, expected: Mapping[str, object]) -> str:
    if (not path.is_absolute() or path == Path(expected["private_run_root"])
            or Path(expected["private_run_root"]) in path.parents
            or path == _SOURCE_ROOT or _SOURCE_ROOT in path.parents):
        raise CertificationRejected("owner approval packet must be separately held")
    try:
        packet, raw = _read_owned_json(path)
    except RouteRejected:
        raise CertificationRejected("owner approval packet is missing or unsafe") from None
    if packet != expected:
        raise CertificationRejected("owner approval packet does not cover this run")
    return hashlib.sha256(raw).hexdigest()


def _admit_route_set(manifest_path: Path | None, policy_path: Path | None,
                     requested: Mapping[str, str]) -> tuple[MachineRoutes, dict[str, RouteAdmission]]:
    if (manifest_path is None or policy_path is None
            or not isinstance(requested, Mapping)
            or set(requested) != set(PROVIDERS)):
        raise CertificationRejected("three explicit machine routes are required")
    try:
        manifest = read_machine_routes(manifest_path, policy_path)
        registry = ProviderRegistry.default(version_probe=lambda _: "unused")
        selected = {
            provider: select_route(
                manifest, route_id=requested[provider], provider=provider,
                requested_model=registry.select(provider, "repo-write", "standard").requested_model,
                execution_class="repo-write", data_classification="protected")
            for provider in PROVIDERS
        }
    except (RouteRejected, TypeError, ValueError, OSError):
        raise CertificationRejected("machine route set is unavailable or disallowed") from None
    if len({route.digest for route in selected.values()}) != len(PROVIDERS):
        raise CertificationRejected("machine route set is invalid")
    return manifest, selected


def _discard_legacy_keys() -> None:
    for name in _LEGACY_KEY_ENVS:
        os.environ.pop(name, None)


def _admit_live(*, allow_paid: bool, approval_reference: str | None,
                observation_date: str | None, image_digest: str | None,
                cli_digests: Mapping[str, str] | None,
                manifest_path: Path | None, policy_path: Path | None,
                route_ids: Mapping[str, str] | None,
                private_run_root: Path, approval_packet_path: Path | None) -> _LiveAdmission:
    if allow_paid is not True:
        raise CertificationRejected("private live route requires --allow-paid")
    if (not isinstance(approval_reference, str) or
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{2,127}", approval_reference) is None):
        raise CertificationRejected("owner approval reference is required")
    try:
        observed = datetime.date.fromisoformat(observation_date)
    except (TypeError, ValueError):
        raise CertificationRejected("owner approval observation date is invalid") from None
    if observed.isoformat() != observation_date or observed != datetime.date.today():
        raise CertificationRejected("owner approval observation date is invalid")
    if (not isinstance(image_digest, str)
            or not image_digest.startswith("fanout-seat@sha256:")
            or not _sha(image_digest.removeprefix("fanout-seat@sha256:"))):
        raise CertificationRejected("new engine-inspectable image RepoDigest is required")
    if (not isinstance(cli_digests, Mapping) or set(cli_digests) != set(PROVIDERS)
            or any(not _sha(value) for value in cli_digests.values())):
        raise CertificationRejected("three pinned Linux CLI byte digests are required")
    try:
        source = runtime_source_digest(_SOURCE_ROOT)
        runtime = host_runtime_identity()
    except (SourceClosureError, RuntimeIdentityError) as error:
        raise CertificationRejected("source or host runtime closure is invalid") from error
    _discard_legacy_keys()
    manifest, selected = _admit_route_set(manifest_path, policy_path,
                                          route_ids or {})
    if approval_packet_path is None:
        raise CertificationRejected("owner approval packet is required")
    packet_path = Path(approval_packet_path)
    packet_digest = _check_owner_packet(
        packet_path, _owner_packet(private_run_root, approval_reference,
                                   observation_date, manifest, selected))
    return _LiveAdmission(source, runtime, image_digest, dict(cli_digests),
                          approval_reference, observation_date,
                          Path(manifest_path), Path(policy_path), manifest, selected,
                          private_run_root, packet_path, packet_digest)


def _sha(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _SHA


def _broker_evidence_valid(provider: str, cell: Mapping[str, object]) -> bool:
    records = cell.get("ledger_requests")
    if not isinstance(records, list) or not 1 <= len(records) <= _MAX_MAIN_REQUESTS:
        return False
    wire_model = ("gemini-3.8-flash" if provider == "agy"
                  else cell.get("requested_model"))
    expected_path = (f"/v1beta/models/{wire_model}:streamGenerateContent?alt=sse"
                     if provider == "agy" else
                     "/v1/messages" if provider == "claude" else "/v1/responses")
    request_ids = set()
    dispositions = []
    for record in records:
        if not isinstance(record, Mapping) or set(record) != {
                "request_id", "permit_id", "epoch", "identity", "state", "response_digest"}:
            return False
        request_id = record["request_id"]
        identity = record["identity"]
        if (not isinstance(request_id, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", request_id) is None
                or request_id in request_ids
                or record["permit_id"] != cell.get("permit_id")
                or type(record["epoch"]) is not int
                or record["epoch"] != cell.get("permit_epoch")
                or record["state"] != "settled" or not _sha(record["response_digest"])
                or not isinstance(identity, Mapping) or set(identity) != {
                    "method", "path", "body_sha256", "effective_output_tokens", "wire_model",
                    "upstream_route_digest", "upstream_origin", "upstream_path",
                    "upstream_body_sha256", "upstream_model"}
                or identity["method"] != "POST" or identity["path"] != expected_path
                or not _sha(identity["body_sha256"])
                or not _sha(identity["upstream_body_sha256"])
                or type(identity["effective_output_tokens"]) is not int
                or not 1 <= identity["effective_output_tokens"] <= _SPEND_CEILING["max_output_tokens_per_call"]
                or identity["wire_model"] != wire_model
                or identity["upstream_route_digest"] != cell.get("machine_route_digest")
                or identity["upstream_origin"] != cell.get("effective_origin")
                or identity["upstream_path"] != cell.get("effective_path")
                or identity["upstream_model"] != cell.get("effective_model")):
            return False
        request_ids.add(request_id)
        dispositions.append({"request_id": request_id, "state": "settled",
                             "response_sha256": record["response_digest"]})
    auxiliary = cell.get("broker_auxiliary_attempt_count")
    return (type(cell.get("broker_request_count")) is int
            and cell["broker_request_count"] == len(records)
            and type(auxiliary) is int and auxiliary >= 0
            and auxiliary <= auxiliary_attempt_limit({
                "provider": provider, "model": cell.get("requested_model")})
            and (provider == "agy" or auxiliary == 0)
            and type(cell.get("broker_auxiliary_refusal_count")) is int
            and cell["broker_auxiliary_refusal_count"] == min(1, auxiliary)
            and type(cell.get("broker_attempt_count")) is int
            and cell["broker_attempt_count"] == len(records) + auxiliary
            and cell["broker_attempt_count"] <= attempt_limit({
                "provider": provider, "model": cell.get("requested_model"),
                "max_requests": _MAX_MAIN_REQUESTS})
            and cell.get("broker_dispositions") == dispositions)


def _cell_reasons(provider: str, route: str, cell: object,
                  source: str, runtime_sha256: str, image_digest: object,
                  cli_digests: object, approval_digest: object,
                  route_info: object, manifest_digest: object,
                  policy_digest: object) -> list[str]:
    label = f"{provider} {route}"
    if not isinstance(cell, Mapping):
        return [f"{label}: missing cell"]
    reasons = []
    if cell.get("passed") is not True or cell.get("route") != route:
        reasons.append(f"{label}: terminal did not pass")
    for name in ("source_sha256", "host_runtime_sha256", "vm_probe_sha256",
                 "socket_attestation_sha256", "cli_digest",
                 "profile_digest", "container_id", "container_inspect_sha256",
                 "permit_id", "answer_sha256", "stdout_sha256", "stderr_sha256",
                 "input_receipt_digest", "input_manifest_sha256",
                 "candidate_digest", "candidate_export_manifest_sha256",
                 "candidate_export_evidence_digest"):
        if not _sha(cell.get(name)):
            reasons.append(f"{label}: {name} missing or invalid")
    if cell.get("source_sha256") != source:
        reasons.append(f"{label}: source changed")
    if cell.get("host_runtime_sha256") != runtime_sha256:
        reasons.append(f"{label}: host runtime changed")
    image = cell.get("image_digest")
    if (not isinstance(image, str) or not image.startswith("fanout-seat@sha256:")
            or not _sha(image.removeprefix("fanout-seat@sha256:"))):
        reasons.append(f"{label}: image is not pinned")
    if image != image_digest:
        reasons.append(f"{label}: image differs from matrix")
    if (not isinstance(cli_digests, Mapping)
            or cell.get("cli_digest") != cli_digests.get(provider)):
        reasons.append(f"{label}: Linux CLI differs from matrix")
    if cell.get("approval_evidence_digest") != approval_digest:
        reasons.append(f"{label}: owner approval evidence differs")
    if (not isinstance(route_info, Mapping)
            or set(route_info) != {"route_id", "digest", "origin", "path",
                                       "model", "auth_family"}
            or not _sha(route_info.get("digest"))
            or route_info.get("auth_family") not in {
                "vertex-adc", "openai-keychain", "openai-1password"}
            or not isinstance(route_info.get("route_id"), str)
            or not route_info.get("route_id")):
        reasons.append(f"{label}: machine route is invalid")
        route_info = {}
    for cell_field, expected in (
        ("route_id", route_info.get("route_id")),
        ("machine_route_digest", route_info.get("digest")),
        ("manifest_digest", manifest_digest),
        ("policy_digest", policy_digest),
        ("effective_origin", route_info.get("origin")),
        ("effective_path", route_info.get("path")),
        ("effective_model", route_info.get("model")),
    ):
        if not expected or cell.get(cell_field) != expected:
            reasons.append(f"{label}: {cell_field} differs from machine route")
    if not _sha(cell.get("credential_source_fingerprint")):
        reasons.append(f"{label}: credential_source_fingerprint is missing")
    if not isinstance(cell.get("session_volume"), str) or not cell["session_volume"].startswith(
            "fanout-session-"):
        reasons.append(f"{label}: session volume missing")
    if not isinstance(cell.get("session_id"), str) or not cell["session_id"]:
        reasons.append(f"{label}: observed session missing")
    profile = ProviderRegistry.default(version_probe=lambda _: "unused").select(
        provider, "repo-write", "standard")
    alias = profile.requested_model
    wire_model = "gemini-3.8-flash" if provider == "agy" else alias
    observed = cell.get("observed_model")
    if cell.get("requested_model") != alias or cell.get("profile_digest") != profile.digest:
        reasons.append(f"{label}: pinned profile differs")
    if observed is not None and (not isinstance(observed, str)
                                 or observed not in {alias, wire_model}):
        reasons.append(f"{label}: observed model conflicts")
    if cell.get("outbound_wire_model") != wire_model:
        reasons.append(f"{label}: outbound wire model differs")
    if (type(cell.get("permit_epoch")) is not int
            or cell.get("permit_epoch") != (1 if route == "initial" else 2)):
        reasons.append(f"{label}: permit epoch differs")
    if (cell.get("seat_stopped") is not True or cell.get("permit_fenced") is not True
            or cell.get("prior_seat_stopped") is not True):
        reasons.append(f"{label}: prior or current seat was not fenced")
    if route == "resume" and cell.get("cold_controller_reopened") is not True:
        reasons.append(f"{label}: cold controller was not reopened")
    if cell.get("own_workspace_write") is not True:
        reasons.append(f"{label}: own workspace write was not proved")
    isolation = cell.get("isolation")
    for name in ISOLATION_TARGETS:
        proof = isolation.get(name) if isinstance(isolation, Mapping) else None
        if (not isinstance(proof, Mapping) or proof.get("read_denied") is not True
                or proof.get("write_denied") is not True):
            reasons.append(f"{label}: {name} isolation failed")
    if not _broker_evidence_valid(provider, cell):
        reasons.append(f"{label}: broker spend disposition is incomplete")
    if cell.get("candidate_edited_paths") != ["README.md"]:
        reasons.append(f"{label}: exactly one edited candidate was not exported")
    elapsed = cell.get("elapsed_seconds")
    if type(elapsed) not in (int, float) or not 0 <= elapsed <= 3600:
        reasons.append(f"{label}: elapsed time is invalid")
    return reasons


def evaluate_private_probe(receipt: Mapping[str, object]) -> PrivateProbeDecision:
    """Check claimed live evidence; certificate issuance must verify provenance."""
    if not isinstance(receipt, Mapping):
        return PrivateProbeDecision(False, ("private probe receipt is malformed",))
    reasons = []
    if receipt.get("schema_version") != SCHEMA or receipt.get("mode") != "live":
        reasons.append("private live route was not observed")
    source = receipt.get("source_sha256")
    if not _sha(source):
        reasons.append("source digest is invalid")
    try:
        if source != runtime_source_digest(_SOURCE_ROOT):
            reasons.append("source closure is stale")
        runtime = asdict(host_runtime_identity())
    except (SourceClosureError, RuntimeIdentityError):
        reasons.append("source or host runtime cannot be attested")
        runtime = {}
    if receipt.get("host_runtime") != runtime:
        reasons.append("host runtime differs")
    runtime_sha256 = hashlib.sha256(canonical_json(runtime)).hexdigest()
    image_digest = receipt.get("image_digest")
    if not isinstance(image_digest, str) or not image_digest.startswith("fanout-seat@sha256:") or not _sha(image_digest.removeprefix("fanout-seat@sha256:")):
        reasons.append("matrix image digest is invalid")
    if not _sha(receipt.get("approval_evidence_digest")):
        reasons.append("owner approval evidence is missing")
    if (not isinstance(receipt.get("approval_reference"), str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{2,127}",
                            receipt["approval_reference"]) is None
            or receipt.get("spend_scope") != _SPEND_SCOPE
            or receipt.get("spend_ceiling") != _SPEND_CEILING):
        reasons.append("owner approval scope differs")
    try:
        observed = datetime.date.fromisoformat(receipt.get("observation_date"))
        if (observed.isoformat() != receipt["observation_date"]
                or observed > datetime.date.today()):
            raise ValueError("date differs")
    except (TypeError, ValueError):
        reasons.append("owner approval observation date is invalid")
    cli_digests = receipt.get("cli_digests")
    if (not isinstance(cli_digests, Mapping)
            or set(cli_digests) != set(PROVIDERS)
            or any(not _sha(value) for value in cli_digests.values())):
        reasons.append("matrix Linux CLI digests are invalid")
    if receipt.get("target_ids") != ["repo-alpha", "repo-beta"]:
        reasons.append("synthetic target set differs")
    manifest_digest = receipt.get("manifest_digest")
    policy_digest = receipt.get("policy_digest")
    if not _sha(manifest_digest) or not _sha(policy_digest):
        reasons.append("machine route manifest or policy digest is invalid")
    machine_routes = receipt.get("machine_routes")
    if not isinstance(machine_routes, Mapping) or set(machine_routes) != set(PROVIDERS):
        reasons.append("machine route matrix is incomplete")
        machine_routes = {}
    providers = receipt.get("providers")
    if not isinstance(providers, Mapping) or set(providers) != set(PROVIDERS):
        reasons.append("three-executor matrix is incomplete")
    for provider in PROVIDERS:
        cells = providers.get(provider) if isinstance(providers, Mapping) else None
        if not isinstance(cells, Mapping) or set(cells) != set(ROUTES):
            reasons.append(f"{provider}: exact initial and resume cells are required")
        initial = cells.get("initial") if isinstance(cells, Mapping) else None
        resume = cells.get("resume") if isinstance(cells, Mapping) else None
        for route, cell in (("initial", initial), ("resume", resume)):
            reasons.extend(_cell_reasons(provider, route, cell, source,
                                         runtime_sha256, image_digest,
                                         cli_digests,
                                         receipt.get("approval_evidence_digest"),
                                         machine_routes.get(provider),
                                         manifest_digest, policy_digest))
        if isinstance(initial, Mapping) and isinstance(resume, Mapping):
            for field in ("session_id", "session_volume", "image_digest",
                          "cli_digest", "profile_digest", "requested_model",
                          "route_id", "machine_route_digest", "manifest_digest",
                          "policy_digest", "credential_source_fingerprint",
                          "effective_origin", "effective_path", "effective_model"):
                if initial.get(field) != resume.get(field):
                    reasons.append(f"{provider} resume: {field} differs from initial")
            if initial.get("permit_id") == resume.get("permit_id"):
                reasons.append(f"{provider} resume: permit was reused")
            if resume.get("recovered_prior_container_id") != initial.get("container_id"):
                reasons.append(f"{provider} resume: prior seat identity differs")
            if initial.get("container_id") == resume.get("container_id"):
                reasons.append(f"{provider} resume: container was reused")
    if isinstance(providers, Mapping):
        permits = [cell.get("permit_id") for cells in providers.values()
                   if isinstance(cells, Mapping) for cell in cells.values()
                   if isinstance(cell, Mapping)]
        if any(not isinstance(permit, str) for permit in permits):
            reasons.append("matrix permit identity is malformed")
        elif len(permits) != len(set(permits)):
            reasons.append("matrix permit was reused")
        request_ids = [record.get("request_id") for cells in providers.values()
                       if isinstance(cells, Mapping) for cell in cells.values()
                       if isinstance(cell, Mapping)
                       and isinstance(cell.get("ledger_requests"), list)
                       for record in cell["ledger_requests"] if isinstance(record, Mapping)]
        if (any(not isinstance(request_id, str) for request_id in request_ids)
                or len(request_ids) > _SPEND_CEILING["max_model_calls"]):
            reasons.append("matrix request inventory is invalid")
        elif len(request_ids) != len(set(request_ids)):
            reasons.append("matrix request was reused")
    return PrivateProbeDecision(not reasons, tuple(reasons))


def _read_private_answer(root: Path, item: Mapping[str, object]) -> str:
    if not isinstance(item, Mapping) or not isinstance(item.get("path"), str):
        raise CertificationRejected("quality answer reference is invalid")
    path = Path(item.get("path", ""))
    if (not path.is_absolute() or path.is_symlink()
            or path.resolve().parent != root.resolve()):
        raise CertificationRejected("quality answer is outside the private artifact root")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_uid != os.getuid() or info.st_size > 131072):
                raise CertificationRejected("quality answer artifact is unsafe")
            data = os.read(fd, 131073)
            if len(data) != info.st_size:
                raise CertificationRejected("quality answer artifact changed")
        finally:
            os.close(fd)
    except OSError:
        raise CertificationRejected("quality answer artifact is unavailable") from None
    if not _sha(item.get("sha256")) or hashlib.sha256(data).hexdigest() != item["sha256"]:
        raise CertificationRejected("quality answer digest differs")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise CertificationRejected("quality answer is not UTF-8") from None


def assess_quality(receipt: Mapping[str, object],
                   maka: HarborMakaTransport) -> QualityAssessment:
    """Judge completed synthetic answers; ballots never affect isolation."""
    answers = receipt.get("answers", {})
    if not isinstance(answers, Mapping):
        raise CertificationRejected("quality answer inventory is invalid")
    root = Path(receipt.get("private_run_root", ""))
    completed = {}
    for provider in PROVIDERS:
        item = answers.get(provider)
        if item is None:
            continue
        cells = receipt.get("providers", {}).get(provider, {})
        if (not isinstance(cells, Mapping)
                or any(not isinstance(cells.get(route), Mapping)
                       or cells[route].get("passed") is not True
                       for route in ROUTES)):
            continue
        if not isinstance(item, Mapping):
            raise CertificationRejected("quality answer reference is invalid")
        completed[provider] = _read_private_answer(root, item)
    return _judge_quality(receipt, maka, completed)


def _quality_judge_request(provider: str, answer: str, order: str) -> JudgeRequest:
    return JudgeRequest(
        case_id=f"container-probe-{provider}", panel_id="harbor",
        criterion="Which answer follows the synthetic task accurately?",
        candidate_answer=answer, baseline_answer="baseline synthetic answer",
        requested_model="gpt-6.1-sol", effort="xhigh", auth_route="harbor-lab", order=order)


def _judge_quality(receipt: Mapping[str, object], maka: HarborMakaTransport,
                   answers: Mapping[str, str]) -> QualityAssessment:
    adapter = MakaJudgeAdapter(maka)
    ballots = []
    for provider in PROVIDERS:
        item = answers.get(provider)
        if item is None:
            continue
        for order in ("candidate-first", "baseline-first"):
            ballot = adapter.judge(_quality_judge_request(provider, item, order))
            ballots.append(ballot)
    return QualityAssessment(evaluate_private_probe(receipt).passed,
                             hashlib.sha256(canonical_json(ballots)).hexdigest(), tuple(ballots))


def _quality_controller(private: Path):
    capability_path = private.with_name(private.name + ".controller-capability")
    try:
        fd = os.open(capability_path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_uid != os.getuid() or not 32 <= info.st_size <= 128):
                raise CertificationRejected("quality controller capability is unsafe")
            token = os.read(fd, 129).decode("ascii")
        finally:
            os.close(fd)
        return resume_lifecycle_controller(
            private / "controller", LifecycleCapability(token))
    except (OSError, UnicodeError, LifecycleError):
        raise CertificationRejected("quality controller capability is unavailable") from None


def _read_private_receipt(private: Path) -> Mapping[str, object]:
    try:
        allowed = (Path(tempfile.gettempdir()).resolve(),
                   Path("/private/tmp").resolve())
        parent = private.parent.resolve(strict=True)
        if (not private.is_absolute()
                or not any(parent == base or base in parent.parents
                           for base in allowed)):
            raise CertificationRejected("private route root is unsafe")
        parent_fd = _open_parent(private)
        try:
            info = os.stat(private.name, dir_fd=parent_fd, follow_symlinks=False)
            if (not stat.S_ISDIR(info.st_mode)
                    or stat.S_IMODE(info.st_mode) != 0o700
                    or info.st_uid != os.getuid()):
                raise CertificationRejected("private route root is unsafe")
            root_fd = os.open(private.name,
                              os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=parent_fd)
            try:
                opened = os.fstat(root_fd)
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    raise CertificationRejected("private route root changed")
                fd = os.open("receipt.json", os.O_RDONLY | os.O_NOFOLLOW,
                             dir_fd=root_fd)
                try:
                    receipt_info = os.fstat(fd)
                    if (not stat.S_ISREG(receipt_info.st_mode)
                            or stat.S_IMODE(receipt_info.st_mode) != 0o600
                            or receipt_info.st_uid != os.getuid()
                            or not 0 < receipt_info.st_size <= 1024 * 1024):
                        raise CertificationRejected("private route receipt is unsafe")
                    raw = os.read(fd, 1024 * 1024 + 1)
                    receipt = json.loads(raw)
                    if canonical_json(receipt) != raw:
                        raise CertificationRejected("private route receipt is noncanonical")
                finally:
                    os.close(fd)
            finally:
                os.close(root_fd)
        finally:
            os.close(parent_fd)
    except (OSError, RouteRejected, ValueError, UnicodeError, TypeError):
        raise CertificationRejected("private route receipt is unavailable") from None
    if not isinstance(receipt, Mapping) or receipt.get("private_run_root") != str(private):
        raise CertificationRejected("private route receipt differs from root")
    return receipt


def _open_private_route_authority(private_root: Path, manifest: MachineRoutes):
    """Reopen owner policy, controller authority and all six HMAC spend records."""
    try:
        private = Path(private_root)
        receipt = _read_private_receipt(private)
        decision = evaluate_private_probe(receipt)
        if not decision.passed:
            raise CertificationRejected("; ".join(decision.reasons))
        if not isinstance(manifest, MachineRoutes):
            raise CertificationRejected("machine route authority is invalid")
        manifest_path = receipt.get("manifest_path")
        policy_path = receipt.get("policy_path")
        if not isinstance(manifest_path, str) or not isinstance(policy_path, str):
            raise CertificationRejected("machine route authority is missing")
        current = read_machine_routes(Path(manifest_path), Path(policy_path))
        if (current != manifest or current.manifest_digest != receipt.get("manifest_digest")
                or current.policy_digest != receipt.get("policy_digest")):
            raise CertificationRejected("machine route or policy changed")
        routes = receipt["machine_routes"]
        _, selected = _admit_route_set(Path(manifest_path), Path(policy_path), {
            provider: routes[provider]["route_id"] for provider in PROVIDERS})
        packet_path = receipt.get("approval_packet_path")
        if not isinstance(packet_path, str):
            raise CertificationRejected("owner approval packet is absent")
        packet_digest = _check_owner_packet(
            Path(packet_path), _owner_packet(
                private, receipt["approval_reference"], receipt["observation_date"],
                current, selected))
        if packet_digest != receipt.get("approval_packet_digest"):
            raise CertificationRejected("owner approval packet changed")
        controller = _quality_controller(private)
        unsigned = dict(receipt)
        name = unsigned.pop("completion_evidence_name", None)
        digest = unsigned.pop("completion_evidence_digest", None)
        completion = read_evidence(controller, "container-private-matrix-complete",
                                   name, digest)
        if completion != {"schema_version": SCHEMA,
                           "receipt_sha256": hashlib.sha256(
                               canonical_json(unsigned)).hexdigest()}:
            raise CertificationRejected("completed route matrix differs")
        approval = read_evidence(controller, "container-spend-approval",
                                 receipt.get("approval_evidence_name"),
                                 receipt.get("approval_evidence_digest"))
        expected_approval = {
            "reference": receipt["approval_reference"],
            "observation_date": receipt["observation_date"],
            "spend_scope": receipt["spend_scope"],
            "source_sha256": receipt["source_sha256"],
            "host_runtime": receipt["host_runtime"],
            "image_digest": receipt["image_digest"],
            "cli_digests": receipt["cli_digests"],
            "manifest_digest": current.manifest_digest,
            "policy_digest": current.policy_digest,
            "machine_routes": receipt["machine_routes"],
            "approval_packet_path": packet_path,
            "approval_packet_digest": packet_digest,
            "spend_ceiling": _SPEND_CEILING,
        }
        if approval != expected_approval:
            raise CertificationRejected("owner route approval differs")
        ledger = PermitLedger(controller.root / "locks" / "spend", controller)
        with ledger._locked() as durable:
            cells = [receipt["providers"][provider][route]
                     for provider in PROVIDERS for route in ROUTES]
            permit_ids = {cell["permit_id"] for cell in cells}
            reported_ids = [record["request_id"] for cell in cells
                            for record in cell["ledger_requests"]]
            request_ids = set(reported_ids)
            if (len(permit_ids) != 6 or len(request_ids) != len(reported_ids)
                    or not 6 <= len(request_ids) <= _SPEND_CEILING["max_model_calls"]
                    or set(durable["permits"]) != permit_ids
                    or set(durable["requests"]) != request_ids
                    or set(durable["send_claims"]) != request_ids):
                raise CertificationRejected("route ledger has unreported spend records")
            for provider in PROVIDERS:
                selected_route = selected[provider]
                if routes[provider] != _route_receipt(selected_route):
                    raise CertificationRejected("selected machine route differs")
                source = HostCredentialSource(selected_route.profile)
                source.load()
                if not _sha(source.fingerprint):
                    raise CertificationRejected("host credential source proof is absent")
                for route, epoch in (("initial", 1), ("resume", 2)):
                    cell = receipt["providers"][provider][route]
                    if cell.get("credential_source_fingerprint") != source.fingerprint:
                        raise CertificationRejected("host credential source changed")
                    permit_id = cell["permit_id"]
                    entry = durable["permits"].get(permit_id)
                    profile = selected_route.profile
                    if (not isinstance(entry, Mapping)
                            or entry.get("provider") != provider
                            or entry.get("model") != profile.requested_model
                            or entry.get("epoch") != epoch
                            or entry.get("max_requests") != _MAX_MAIN_REQUESTS
                            or entry.get("max_response_bytes") != _SPEND_CEILING["max_response_bytes_per_call"]
                            or entry.get("max_output_tokens") != _SPEND_CEILING["max_output_tokens_per_call"]
                            or entry.get("used") != len(cell["ledger_requests"])
                            or entry.get("fenced") is not True
                            or entry.get("upstream_route_digest") != selected_route.digest
                            or entry.get("upstream_origin") != profile.origin
                            or entry.get("upstream_path") != routes[provider]["path"]
                            or entry.get("upstream_model") != profile.effective_model):
                        raise CertificationRejected("route permit provenance differs")
                    records = tuple(
                        {"request_id": request_id, **request}
                        for request_id, request in sorted(durable["requests"].items())
                        if request["permit_id"] == permit_id)
                    attempts, auxiliary = entry["attempts"], entry["auxiliary_attempts"]
                    if (list(records) != cell.get("ledger_requests")
                            or attempts != cell.get("broker_attempt_count")
                            or auxiliary != cell.get("broker_auxiliary_attempt_count")
                            or entry["auxiliary_refusals"] != cell.get("broker_auxiliary_refusal_count")
                            or not 1 <= len(records) <= _MAX_MAIN_REQUESTS
                            or attempts != len(records) + auxiliary
                            or any(record.get("state") != "settled"
                                   or record.get("epoch") != epoch
                                   or durable["send_claims"].get(record["request_id"]) is not True
                                   for record in records)):
                        raise CertificationRejected("route ledger provenance differs")
                    for record in records:
                        identity = record["identity"]
                        if (identity["upstream_route_digest"] != selected_route.digest
                                or identity["upstream_origin"] != cell["effective_origin"]
                                or identity["upstream_path"] != cell["effective_path"]
                                or identity["upstream_model"] != cell["effective_model"]
                                or not 1 <= identity["effective_output_tokens"] <= _SPEND_CEILING["max_output_tokens_per_call"]):
                            raise CertificationRejected("effective outbound request differs")
        return receipt, controller
    except (CertificationRejected, RouteRejected, RouteCredentialError,
            LifecycleError, BrokerRejected, KeyError, TypeError, ValueError) as error:
        raise CertificationRejected(str(error) if isinstance(error, CertificationRejected)
                                    else "private route evidence is unavailable") from None


def verify_private_route_evidence(private_root: Path,
                                  manifest: MachineRoutes) -> PrivateProbeDecision:
    """Compatibility decision API backed by the shared authenticated reader."""
    try:
        read_private_route_evidence(private_root, manifest)
        return PrivateProbeDecision(True, ())
    except (CertificationRejected, ValueError, KeyError, TypeError, OSError):
        return PrivateProbeDecision(False, ("private route evidence is unavailable",))


def _authenticated_quality_answers(private: Path, receipt: Mapping[str, object]
                                   ) -> dict[str, str]:
    try:
        current = read_machine_routes(Path(receipt["manifest_path"]),
                                      Path(receipt["policy_path"]))
    except (KeyError, TypeError, RouteRejected):
        raise CertificationRejected("quality machine route is unavailable") from None
    try:
        proof = read_private_route_evidence(private, current)
    except (CertificationRejected, ValueError, TypeError, KeyError, OSError):
        raise CertificationRejected("quality route evidence is unproven")
    if receipt != proof.receipt:
        raise CertificationRejected("quality route receipt changed")
    answer_refs = receipt.get("answers")
    if (not isinstance(answer_refs, Mapping) or set(answer_refs) != set(PROVIDERS)
            or any(not isinstance(answer_refs[provider], Mapping)
                   for provider in PROVIDERS)):
        raise CertificationRejected("quality needs three completed answer artifacts")
    for provider in PROVIDERS:
        item = answer_refs[provider]
        if (item.get("path") != str(private / f"answer-{provider}.bin")
                or item.get("sha256") !=
                receipt["providers"][provider]["resume"]["answer_sha256"]):
            raise CertificationRejected("quality answer reference differs from completed cell")
    return {provider: _read_private_answer(private, answer_refs[provider])
            for provider in PROVIDERS}


def _quality_run(root: Path, *, allow_paid: bool,
                 approval_reference: str | None, observation_date: str | None,
                 launcher: Path | None, boundary_sha256: str | None
                 ) -> QualityAssessment:
    if allow_paid is not True:
        raise CertificationRejected("quality route requires --allow-paid")
    if (not isinstance(approval_reference, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{2,127}",
                            approval_reference) is None):
        raise CertificationRejected("quality owner approval reference is required")
    try:
        observed = datetime.date.fromisoformat(observation_date)
    except (TypeError, ValueError):
        raise CertificationRejected("quality observation date is invalid") from None
    if (observed.isoformat() != observation_date
            or observed > datetime.date.today() or launcher is None
            or not _sha(boundary_sha256)):
        raise CertificationRejected("quality route is not fully pinned")
    private = Path(root)
    source_binding = release_source_binding()
    receipt = _read_private_receipt(private)
    providers = receipt.get("providers") if isinstance(receipt, Mapping) else None
    if (not isinstance(receipt, Mapping)
            or receipt.get("mode") != "live"
            or receipt.get("private_run_root") != str(private)
            or not isinstance(providers, Mapping)
            or any(not isinstance(providers.get(provider), Mapping)
                   or any(not isinstance(providers[provider].get(route), Mapping)
                          or providers[provider][route].get("passed") is not True
                          for route in ROUTES)
                   for provider in PROVIDERS)):
        raise CertificationRejected("quality needs six completed private synthetic cells")
    answers = _authenticated_quality_answers(private, receipt)
    result_path = private / "quality.json"
    if result_path.exists() or result_path.is_symlink():
        raise CertificationRejected("quality result already exists")
    try:
        _write_private(private / "quality-attempt.json", canonical_json({
            "approval_reference": approval_reference,
            "observation_date": observation_date,
            "source_receipt_sha256": hashlib.sha256(canonical_json(receipt)).hexdigest(),
            "boundary_sha256": boundary_sha256,
        }))
    except OSError:
        raise CertificationRejected("quality attempt is already reserved") from None
    for name in _LEGACY_KEY_ENVS:
        os.environ.pop(name, None)
    try:
        maka = HarborMakaTransport(
            launcher=launcher, no_tools_characterization_sha256=boundary_sha256,
            paid_authorized=True, quality_run_root=private)
        quality = _judge_quality(receipt, maka, answers)
        captures_sha256 = maka.finish_captures()
    except Exception:
        raise CertificationRejected('Maka original complete capture is unproven; no retry') from None
    if release_source_binding().to_dict() != source_binding.to_dict():
        raise CertificationRejected("release source changed during Maka quality judgment")
    result = {
        "schema_version": "fanout-maka-quality-v1", "private_run_root": str(private),
        "release_source_binding": source_binding.to_dict(),
        "approval_reference": approval_reference,
        "observation_date": observation_date,
        "boundary_sha256": boundary_sha256,
        "source_receipt_sha256": hashlib.sha256(canonical_json(receipt)).hexdigest(),
        "isolation_passed": quality.isolation_passed,
        "ballot_digest": quality.ballot_digest,
        "ballots": list(quality.ballots),
        "capture_inventory_sha256": captures_sha256,
    }
    result["completion_evidence_name"], result["completion_evidence_digest"] = write_evidence(
        _quality_controller(private), "container-maka-quality-complete", {
            "quality_sha256": hashlib.sha256(canonical_json(result)).hexdigest()})
    _write_private(result_path, canonical_json(result))
    return quality


def _validated_roots(target_root: Path, disposable_root: Path) -> tuple[Path, Path]:
    target = Path(target_root)
    disposable = Path(disposable_root)
    if (not target.is_absolute() or not disposable.is_absolute()
            or target != disposable / "targets" / "repo-alpha"
            or disposable.exists() or disposable.is_symlink()
            or _SOURCE_ROOT == disposable or _SOURCE_ROOT in disposable.parents):
        raise CertificationRejected("certification requires a fresh synthetic target")
    allowed = (Path(tempfile.gettempdir()).resolve(), Path("/private/tmp").resolve())
    parent = disposable.parent.resolve(strict=True)
    if not any(parent == base or base in parent.parents for base in allowed):
        raise CertificationRejected("private run root must be under a temporary root")
    return target, disposable


class _FakeLauncher(ContainerLauncher):
    """No-Docker test edge; Task 5 still owns the turn and stop sequence."""

    def __init__(self, spec: ContainerSpec, route: str, *, controller=None,
                 ledger: PermitLedger | None = None,
                 previous: Mapping[str, object] | None = None):
        self.spec = spec
        self.running = True
        self.controller = controller
        self.ledger = ledger
        self.previous = previous
        self._recovery = None
        self._volume_expectations = None
        self._volume_handle = None
        self._observed_volumes = {}
        self.volume_state = (controller.root / "quarantine" / f"fake-volumes-{spec.seat_id}.json"
                             if controller is not None else None)
        self.volumes = {}
        self.recovered_prior_container_id = None
        self.handle = ContainerHandle(
            hashlib.sha256(f"{spec.seat_id}:{route}".encode()).hexdigest(),
            hashlib.sha256(canonical_json(asdict(spec))).hexdigest(),
            spec.workspace_volume, spec.session_volume,
        )

    def start(self, spec: ContainerSpec) -> ContainerHandle:
        self._recovery = None
        self.recovered_prior_container_id = None
        if spec != self.spec:
            raise CertificationRejected("fake launcher target differs")
        self._volume_expectations = None
        for kind in ("workspace", "session"):
            self._volume(spec, kind, create=True)
        self._volume_expectations = (spec, {kind: self._observed_volumes[getattr(spec, kind + "_volume")]
                                           for kind in ("workspace", "session")})
        self._volume_handle = (spec, self.handle)
        return self.handle

    def recover(self, spec: ContainerSpec) -> ContainerHandle:
        self._recovery = None
        self.recovered_prior_container_id = None
        if spec != self.spec:
            raise CertificationRejected("fake launcher target differs")
        if (self.controller is None or self.ledger is None
                or not isinstance(self.previous, Mapping)):
            raise CertificationRejected("fake cold recovery has no prior seat")
        prior = self.previous
        try:
            stopped = read_evidence(
                self.controller, "container-fake-stopped",
                prior["stop_evidence_name"], prior["stop_evidence_digest"],
            )
        except Exception as error:
            raise CertificationRejected("fake prior seat proof is unauthenticated") from error
        if (stopped.get("container_id") != prior.get("container_id")
                or stopped.get("session_volume") != spec.session_volume
                or stopped.get("seat_id") != spec.seat_id
                or stopped.get("permit_id") != prior.get("permit_id")
                or stopped.get("permit_epoch") != prior.get("permit_epoch")
                or stopped.get("stopped") is not True
                or stopped.get("broker_state") != "settled"):
            raise CertificationRejected("fake prior seat was not safely stopped")
        try:
            native = read_evidence(
                self.controller, "container-seat-stopped",
                stopped["native_stop_evidence_name"], stopped["native_stop_evidence_digest"])
            prior_spec = ContainerSpec(**native["spec"])
            prior_handle = ContainerHandle(**native["handle"])
            launch = read_evidence(
                self.controller, "container-seat-launch",
                native["launch_evidence_name"], native["launch_evidence_digest"])
            decode_launch_record(launch, prior_spec, prior_handle,
                                 recover=launch.get("route") == "recover")
            ref = ArtifactRef(**native["raw_inspect"])
            with ArtifactStore.open_existing(
                    self.controller.root / "quarantine" / "runtime-proofs") as store:
                raw = store.read_bytes(ref)
            if (prior_spec != spec or prior_handle.container_id != prior["container_id"]
                    or raw != self._fake_inspect(prior_spec, prior_handle, False)):
                raise CertificationRejected("fake retained predecessor differs")
        except Exception as error:
            raise CertificationRejected("fake native predecessor proof is invalid") from error
        with self.ledger._locked() as durable:
            permit = durable["permits"].get(prior["permit_id"])
            if (not isinstance(permit, dict) or permit.get("fenced") is not True
                    or permit.get("epoch") != prior.get("permit_epoch")
                    or permit.get("seat_scope") != hashlib.sha256(canonical_json({
                        key: getattr(prior_spec, key) for key in (
                            "run_id", "task_id", "target_id", "seat_id")
                    })).hexdigest()):
                raise CertificationRejected("fake prior broker epoch was not fenced")
        recovery = _RecoveryAssociation(
            spec, self.handle, ((prior_handle.container_id, ref.digest),))
        if self._volume_expectations is None or self._volume_expectations[0] != spec:
            raise CertificationRejected("fake recovery lacks original volume authority")
        for kind in ("workspace", "session"):
            self._volume(spec, kind, create=False)
        self._recovery = recovery
        self._volume_handle = (spec, self.handle)
        self.recovered_prior_container_id = stopped["container_id"]
        return self.handle

    def attest_running(self, spec: ContainerSpec,
                       handle: ContainerHandle) -> ContainerHandle:
        if spec != self.spec or handle != self.handle or not self.running:
            raise CertificationRejected("fake launcher seat is not running")
        if self._volume_handle != (spec, handle):
            raise CertificationRejected("fake volume launch association differs")
        for kind in ("workspace", "session"):
            self._volume(spec, kind, create=False)
        return handle

    @staticmethod
    def _fake_inspect(spec: ContainerSpec, handle: ContainerHandle, running: bool) -> bytes:
        item = {
            "Id": handle.container_id,
            "Config": {"Labels": ContainerLauncher._labels(spec), "Image": spec.image_digest},
            "HostConfig": {"RestartPolicy": {"Name": "no"}},
            "Mounts": [
                {"Destination": "/workspace", "Type": "volume",
                 "Name": spec.workspace_volume},
                {"Destination": "/session", "Type": "volume",
                 "Name": spec.session_volume},
                {"Destination": "/run/fanout/broker.sock", "Type": "bind",
                 "Source": spec.socket_path},
            ],
            "State": {"Running": running},
        }
        return json.dumps([item]).encode()

    def _run(self, *args: str) -> bytes:
        if args[:2] in (("volume", "create"), ("volume", "inspect")):
            volumes = (json.loads(self.volume_state.read_bytes())
                       if self.volume_state is not None and self.volume_state.exists() else self.volumes)
            if args[1] == "create":
                name = args[-1]
                kind = next(kind for kind in ("workspace", "session")
                            if getattr(self.spec, kind + "_volume") == name)
                if name not in volumes:
                    volumes[name] = {"Name": name, "Driver": "local", "Scope": "local", "Options": {},
                        "Labels": self._volume_labels(self.spec, kind),
                        "CreatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        "Mountpoint": f"/var/lib/docker/volumes/{name}/_data"}
                    if self.volume_state is not None:
                        self.volume_state.write_bytes(canonical_json(volumes))
                        self.volume_state.chmod(0o600)
                self.volumes = volumes
                return name.encode()
            return canonical_json([volumes[args[2]]] if args[2] in volumes else [])
        if args == ("inspect", "--type", "container", self.handle.container_id):
            return self._fake_inspect(self.spec, self.handle, self.running)
        if args == ("update", "--restart", "no", self.handle.container_id):
            return b""
        if args == ("stop", "--time", "0", self.handle.container_id):
            self.running = False
            return b""
        raise CertificationRejected("fake launcher operation differs")


def _fake_terminal(provider: str, session_id: str, model: str) -> bytes:
    if provider == "claude":
        return canonical_json({"type": "result", "subtype": "success",
                               "is_error": False, "session_id": session_id,
                               "result": "synthetic Claude answer",
                               "usage": {"input_tokens": 1, "output_tokens": 1}})
    if provider == "codex":
        rows = ({"type": "thread.started", "thread_id": session_id},
                {"type": "item.completed", "item": {
                    "type": "agent_message", "text": "synthetic Codex answer"}},
                {"type": "turn.completed", "usage": {
                    "input_tokens": 1, "output_tokens": 1}})
    else:
        rows = ({"event": "init", "conversation_id": session_id,
                 "init": {"cwd": "/workspace", "tools": [],
                          "permission_mode": "always-proceed", "model": model}},
                {"event": "step_update", "step_update": {
                    "conversation_id": session_id, "step_index": 0,
                    "state": "DONE", "step_type": "agent_response",
                    "text_delta": "synthetic agy answer"}},
                {"event": "result", "result": {
                    "conversation_id": session_id, "status": "SUCCESS",
                    "response": "synthetic agy answer", "duration_seconds": 0.1,
                    "num_turns": 1, "usage": {"input_tokens": 1,
                                              "output_tokens": 1}}})
    return b"".join(canonical_json(row) for row in rows)


def _write_private(path: Path, data: bytes) -> str:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    return hashlib.sha256(data).hexdigest()


def _ledger_snapshot(controller, permit_id: str, epoch: int
                     ) -> tuple[tuple[dict[str, object], ...], int, int, bool]:
    """Read every request and attempt through a freshly authenticated controller."""
    cold = resume_lifecycle_controller(controller.root, controller.capability)
    ledger = PermitLedger(cold.root / "locks" / "spend", cold)
    records = ledger.request_records(permit_id, epoch)
    attempts, auxiliary = ledger.attempt_counts(permit_id, epoch)
    with ledger._locked() as durable:
        fenced = durable["permits"][permit_id]["fenced"] is True
    return records, attempts, auxiliary, fenced


def _checked_candidate(controller, baseline: RepositoryBaseline,
                       receipt: CandidateExportReceipt,
                       marker: bytes):
    """Authenticate the export record and reconstruct its exact edited bytes."""
    if not isinstance(receipt, CandidateExportReceipt):
        raise CertificationRejected("candidate export receipt is missing")
    expected = {
        "identity": asdict(receipt.identity), "turn_digest": receipt.turn_digest,
        "manifest_sha256": receipt.manifest_sha256, "root": str(receipt.root),
        "container_id": receipt.container_id,
        "container_inspect_sha256": receipt.container_inspect_sha256,
        "workspace_volume": receipt.workspace_volume,
        "session_volume": receipt.session_volume,
    }
    try:
        proof = read_evidence(controller, "container-candidate-export",
                              receipt.evidence_name, receipt.evidence_digest)
        if proof != expected:
            raise CertificationRejected("candidate export evidence differs")
        candidate = create_candidate_from_export(
            baseline, receipt.root, receipt.identity, receipt.manifest_sha256)
    except CertificationRejected:
        raise
    except Exception:
        raise CertificationRejected("candidate export cannot be authenticated") from None
    original = next((entry for entry in baseline.entries
                     if entry.path == "README.md"), None)
    if (original is None or len(candidate.entries) != 1
            or candidate.deleted_paths or candidate.source_candidate_digests
            or candidate.entries[0].path != "README.md"
            or candidate.entries[0].kind != "file"
            or candidate.entries[0].mode != original.mode
            or candidate.entries[0].data != original.data + marker):
        raise CertificationRejected("candidate export is not the exact edited file")
    return candidate


def _guest_relay_module():
    path = _SOURCE_ROOT / "components" / "fanout-container" / "relay.py"
    spec = importlib.util.spec_from_file_location("fanout_private_guest_relay", path)
    if spec is None or spec.loader is None:
        raise CertificationRejected("fake guest relay source is unavailable")
    guest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guest)
    return guest


def _fake_http_exchange(ledger: PermitLedger, broker: Broker, permit_id: str,
                        epoch: int, path: str, headers: Mapping[str, str],
                        body: bytes) -> tuple[int, bytes, bool]:
    """Run the real bridge, guest socket and framed host relay with fake upstream."""
    guest = _guest_relay_module()
    failures: list[BaseException] = []

    def run(operation):
        try:
            operation()
        except BaseException as error:
            failures.append(error)

    with tempfile.TemporaryDirectory(prefix="fanout-task6-relay-", dir="/private/tmp") as root:
        guest.GUEST_SOCKET_ROOT = root
        host_socket, guest_socket = socket.socketpair()
        host_frames = FrameStream(host_socket.makefile("rb", buffering=0),
                                  host_socket.makefile("wb", buffering=0), timeout=5)
        guest_frames = guest.FrameStream(guest_socket.makefile("rb", buffering=0),
                                         guest_socket.makefile("wb", buffering=0), timeout=5)
        relay = guest.GuestRelay(
            broker.permit.seat_id, permit_id, epoch, guest_frames,
            max_requests=1, max_request_bytes=broker.permit.max_request_bytes,
            max_response_bytes=broker.permit.max_response_bytes,
        )

        def connector(mounted_path: str) -> socket.socket:
            if mounted_path != guest.BROKER_SOCKET:
                raise CertificationRejected("fake bridge destination differs")
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.settimeout(5)
            connection.connect(guest.seat_socket_path(broker.permit.seat_id))
            return connection

        bridge = guest.LoopbackBridge(port=0, connector=connector)
        host = threading.Thread(target=run, args=(
            lambda: HostRelay(ledger, broker).serve(host_frames),), daemon=True)
        remote = threading.Thread(target=run, args=(relay.serve,), daemon=True)
        listener = threading.Thread(target=run, args=(
            lambda: bridge.serve_forever(poll_interval=0.01),), daemon=True)
        host.start()
        remote.start()
        listener.start()
        client = http.client.HTTPConnection("127.0.0.1", bridge.server_address[1],
                                            timeout=5)
        try:
            client.request("POST", path, body=body, headers=dict(headers))
            response = client.getresponse()
            status = response.status
            payload = response.read()
            with connector(guest.BROKER_SOCKET) as drain:
                drain.sendall(b"POST http://invalid.invalid/ HTTP/1.1\r\n"
                              b"Host: 127.0.0.1:43187\r\nContent-Length: 0\r\n\r\n")
                refusal = http.client.HTTPResponse(drain)
                refusal.begin()
                if refusal.status != 403 or refusal.read() != b"":
                    raise CertificationRejected("fake HTTP relay drain was not refused")
            remote.join(timeout=5)
            host.join(timeout=5)
        finally:
            client.close()
            bridge.shutdown()
            bridge.server_close()
            for connection in (host_socket, guest_socket):
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                connection.close()
            for thread in (listener, remote, host):
                thread.join(timeout=5)
        if failures or any(thread.is_alive() for thread in (listener, remote, host)):
            raise CertificationRejected("fake HTTP relay did not close cleanly")
        return status, payload, True


def _fake_transfer_popen(workspace: Path, container_id: str):
    """Run the guest transfer CLI locally behind the fixed Docker command edge."""
    def popen(argv, **kwargs):
        prefix = ("docker", "--context", "colima-fanout-isolated", "exec", "-i",
                  "--user", "65532:65532", "--workdir", "/workspace",
                  container_id, "python", "/opt/fanout/container_transfer.py")
        if (len(argv) != len(prefix) + 1 or tuple(argv[:-1]) != prefix
                or argv[-1] not in {"import", "export"}):
            raise CertificationRejected("fake transfer Docker command differs")
        script = (
            "import sys;from pathlib import Path;"
            "from shared.lib.fanout.container_transfer import main;"
            "raise SystemExit(main([sys.argv[2]],workspace=Path(sys.argv[1])))"
        )
        kwargs["cwd"] = _SOURCE_ROOT
        return subprocess.Popen(
            [sys.executable, "-c", script, str(workspace), argv[-1]], **kwargs,
        )
    return popen


def _fake_upstream_fields(provider: str, model: str) -> dict[str, str]:
    wire = "gemini-3.8-flash" if provider == "agy" else model
    path = (f"/v1beta/models/{wire}:streamGenerateContent?alt=sse"
            if provider == "agy" else
            "/v1/messages" if provider == "claude" else "/v1/responses")
    return {"upstream_route_digest": hashlib.sha256(
                canonical_json({"fake": provider, "model": model})).hexdigest(),
            "upstream_origin": "https://fake.invalid",
            "upstream_model": wire, "upstream_path": path}


def _fake_preview(permit: BrokerPermit, _method: str, _path: str,
                  _headers: Mapping[str, str], body: bytes) -> PreparedOutbound:
    return PreparedOutbound(permit.upstream_origin, permit.upstream_path,
                            body, permit.upstream_model, permit.upstream_route_digest)


def _fake_cell(controller, ledger: PermitLedger, target: Path, sibling: Path,
               raw_root: Path,
               provider: str,
               route: str, source_sha256: str, inputs_digest: str,
               previous: Mapping[str, object] | None, *,
               cold_controller_reopened: bool = False) -> dict[str, object]:
    profile = ProviderRegistry.default(version_probe=lambda _: "unused").select(
        provider, "repo-write", "standard")
    run_id, task_id, target_id, seat_id = (
        "private-fake", "synthetic-task", "repo-alpha", f"seat-{provider}")
    guest_workspace = raw_root.parent / "transfer-alpha" / provider
    source = controller.root / "quarantine" / f"source-{provider}"
    repo_bytes = (target / "README.md").read_bytes()
    plan_bytes = b"# Synthetic full plan\nExercise the private fake route.\n"
    skills_bytes = canonical_json({"skills": ["llm-fanout-execute"]})
    dependencies_bytes = canonical_json({"peer": "verified synthetic context"})
    context_sha256 = hashlib.sha256(plan_bytes).hexdigest()
    skills_sha256 = hashlib.sha256(skills_bytes).hexdigest()
    workspace, session_volume = private_volume_names(
        run_id, task_id, target_id, seat_id)
    store = SessionStore(controller, run_id, task_id, target_id, seat_id,
                         session_volume)
    session_id = (str(uuid.uuid4()) if provider == "claude"
                  and route == "initial" else None)
    if route == "resume":
        session_id = store.resume(provider).session_id
    request = ProviderRequest(
        executor_id=provider, prompt=f"{provider} synthetic {route}", cwd=target,
        execution_class="repo-write", profile=profile, run_id=run_id,
        task_id=task_id, target_id=target_id, seat_id=seat_id,
        inputs_digest=inputs_digest, session_id=session_id,
        resume=route == "resume", context_sha256=context_sha256,
        skill_bundle_sha256=skills_sha256,
        skill_delivery_sha256=skills_sha256,
    )
    permit = BrokerPermit(
        provider, run_id, task_id, target_id, seat_id, route,
        profile.requested_model, "disposable-placeholder", 1, 100_000,
        100_000, 1024, inputs_digest=inputs_digest, session_id=session_id,
        **_fake_upstream_fields(provider, profile.requested_model),
    )
    epoch = 1 if route == "initial" else 2
    permit_id = ledger.register(permit, epoch)
    fake_credential = "fake-upstream-credential-not-real"
    upstream_requests = []

    def send(method, path, headers, body):
        upstream_requests.append({"method": method, "path": path,
                                  "model": json.loads(body).get("model"),
                                  "header_names": sorted(headers)})
        return BrokerResponse(200, {"content-type": "text/event-stream"},
                              [b'data: {"type":"synthetic"}\n\n',
                               b"data: [DONE]\n\n"])

    send.preview = lambda method, path, headers, body: _fake_preview(
        permit, method, path, headers, body)

    broker = Broker(permit, credential=fake_credential, send=send)
    sse_digest = None
    fake_http_status = None
    host_relay_completed = False
    workspace_input_sha256 = None
    sibling_probe = None

    def process_runner(_command):
        nonlocal sse_digest, fake_http_status, host_relay_completed
        nonlocal workspace_input_sha256, sibling_probe
        if Path(_command.cwd) != target:
            raise CertificationRejected("fake turn target differs")
        container_workdir = Path(transport._model_workdir)
        if not container_workdir.is_absolute() or not str(container_workdir).startswith(
                "/workspace/inputs/"):
            raise CertificationRejected("fake turn workdir is not the staged transfer")
        workspace = guest_workspace / container_workdir.relative_to("/workspace")
        workspace_input_sha256 = hashlib.sha256(
            (workspace / "README.md").read_bytes()).hexdigest()
        if workspace_input_sha256 != inputs_digest:
            raise CertificationRejected("fake turn input differs from published transfer")
        observed_sibling = None
        try:
            observed_sibling = hashlib.sha256((sibling / "README.md").read_bytes()).hexdigest()
            read_denied = False
        except OSError:
            read_denied = True
        try:
            probe_path = sibling / f"probe-{provider}-{route}.txt"
            fd = os.open(probe_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         os.O_NOFOLLOW, 0o600)
        except OSError:
            write_denied = True
        else:
            os.close(fd)
            write_denied = False
        sibling_probe = {"attempted": True, "read_denied": read_denied,
                         "write_denied": write_denied,
                         "observed_sha256": observed_sibling}
        seat_header = {"claude": "x-api-key", "codex": "x-fanout-seat",
                       "agy": "x-goog-api-key"}[provider]
        wire_model = "gemini-3.8-flash" if provider == "agy" else profile.requested_model
        path = {"claude": "/v1/messages?beta=true",
                "codex": "/v1/responses",
                "agy": f"/v1beta/models/{wire_model}:streamGenerateContent?alt=sse"}[provider]
        body = {"model": wire_model}
        if provider == "agy":
            body["generationConfig"] = {"maxOutputTokens": 32}
        else:
            body["max_tokens" if provider == "claude" else "max_output_tokens"] = 32
        fake_http_status, payload, host_relay_completed = _fake_http_exchange(
            ledger, broker, permit_id, epoch, path,
            {"content-type": "application/json", seat_header: "disposable-placeholder"},
            canonical_json(body),
        )
        sse_digest = hashlib.sha256(payload).hexdigest()
        if fake_http_status != 200 or not payload:
            raise CertificationRejected("fake HTTP/SSE route did not return a body")
        observed_id = session_id or f"{provider}-synthetic-session"
        return ProcessResult(ProcessStatus.EXIT, 0,
                             _fake_terminal(provider, observed_id,
                                            profile.requested_model), b"")

    image = "fanout-seat@sha256:" + "3" * 64
    cli = hashlib.sha256(provider.encode()).hexdigest()
    spec = ContainerSpec(run_id, task_id, target_id, seat_id, image,
                         workspace, session_volume,
                         f"/run/fanout/{seat_id}.sock", cli)
    launcher = _FakeLauncher(spec, route, controller=controller, ledger=ledger,
                             previous=previous)
    vm = VMProbe("fanout-isolated", "unix:///private/fake/docker.sock",
                 "2" * 64, "3" * 64, "4" * 64, "5" * 64)
    socket = GuestSocketAttestation(spec.socket_path, 65532,
                                    stat.S_IFSOCK | 0o600, 1, 2, 3)
    transport = PinnedDockerTransport.start(
        controller, spec, launcher=launcher, vm_attestor=lambda: vm,
        socket_attestor=lambda _: socket, image_attestor=lambda: image,
        cli_attestor=lambda _: cli, process_runner=process_runner,
        recover=route == "resume", request=request,
        session_control=SimpleNamespace(broker=broker, ledger=ledger,
            permit_id=permit_id, epoch=epoch),
    )
    transport._transfer_popen = _fake_transfer_popen(
        guest_workspace, transport.handle.container_id)
    admission = ContainerAdmission(
        permit, permit_id, epoch, ledger, image, cli, socket, transport, store,
        transport.handle_evidence_name, transport.handle_evidence_digest,
    )
    if route == "initial":
        os.mkdir(guest_workspace, 0o700)
        admission = _stage_live_inputs(
            controller, request, transport, admission, source,
            repo_bytes, plan_bytes, skills_bytes, dependencies_bytes)
    boundary = issue_container_boundary(controller, request, vm,
                                        transport.handle, admission)
    result = run_container_turn(request, boundary)
    if not launcher.running:
        ledger.fence_epoch(permit_id, epoch)
    with ledger._locked() as durable:
        requests = [request_id for request_id, entry in durable["requests"].items()
                    if entry["permit_id"] == permit_id]
    if len(requests) != 1:
        raise CertificationRejected("fake relay request inventory differs")
    request_id = requests[0]
    state, response_digest = ledger.disposition(request_id)
    stop_evidence_name, stop_evidence_digest = write_evidence(
        controller, "container-fake-stopped", {
            "container_id": transport.handle.container_id,
            "seat_id": seat_id, "session_volume": session_volume,
            "permit_id": permit_id, "permit_epoch": epoch,
            "stopped": not launcher.running,
            "broker_state": state,
            "native_stop_evidence_name": transport.stop_evidence[0],
            "native_stop_evidence_digest": transport.stop_evidence[1],
        },
    )
    raw_artifacts = {}
    for name, payload in (("answer", result.answer.encode()),
                          ("stdout", result.stdout), ("stderr", result.stderr)):
        path = raw_root / f"{provider}-{route}-{name}.bin"
        _write_private(path, payload)
        raw_artifacts[name] = str(path)
    return {
        "passed": False, "fake_terminal_valid": result.valid, "route": route,
        "source_sha256": source_sha256, "vm_probe_sha256": vm.digest,
        "image_digest": image, "cli_digest": cli,
        "profile_digest": profile.digest,
        "container_id": transport.handle.container_id,
        "container_inspect_sha256": transport.handle.inspect_sha256,
        "session_volume": session_volume, "session_id": result.session_id,
        "requested_model": profile.requested_model,
        "observed_model": result.observed_model,
        "broker_request_count": len(upstream_requests),
        "broker_dispositions": [{"request_id": request_id, "state": state,
                                 "response_sha256": response_digest}],
        "permit_id": permit_id, "permit_epoch": epoch,
        "permit_fenced": not launcher.running,
        "prior_seat_stopped": previous is None or previous.get("seat_stopped") is True,
        "seat_stopped": not launcher.running,
        "stop_evidence_name": stop_evidence_name,
        "stop_evidence_digest": stop_evidence_digest,
        "cold_controller_reopened": cold_controller_reopened,
        "recovered_prior_container_id": launcher.recovered_prior_container_id,
        "isolation": {"sibling": {"read_denied": sibling_probe["read_denied"],
                                    "write_denied": sibling_probe["write_denied"]}},
        "own_workspace_write": False,
        "answer_sha256": hashlib.sha256(result.answer.encode()).hexdigest(),
        "stdout_sha256": hashlib.sha256(result.stdout).hexdigest(),
        "stderr_sha256": hashlib.sha256(result.stderr).hexdigest(),
        "elapsed_seconds": result.duration,
        "fake_http": upstream_requests, "fake_sse_sha256": sse_digest,
        "fake_http_transport": "loopback-unix-stdio",
        "fake_http_status": fake_http_status,
        "host_relay_completed": host_relay_completed,
        "raw_artifacts": raw_artifacts,
        "workspace_cwd": str(guest_workspace / Path(
            boundary.input_workdir).relative_to("/workspace")),
        "container_workdir": boundary.input_workdir,
        "input_manifest_sha256": boundary.input_manifest_sha256,
        "input_receipt_digest": boundary.input_receipt_digest,
        "candidate_export_root": str(result.candidate_export.root),
        "workspace_input_sha256": workspace_input_sha256,
        "sibling_probe": sibling_probe,
    }


def _interrupted_send_probe(controller) -> dict[str, object]:
    """Lose the guest ACK after an upstream send, then retry from cold state."""
    run_id, task_id, target_id, seat_id = (
        "private-fake", "synthetic-task", "repo-alpha", "seat-interrupted")
    _, session_volume = private_volume_names(run_id, task_id, target_id, seat_id)
    store = SessionStore(controller, run_id, task_id, target_id, seat_id,
                         session_volume)
    session_id = str(uuid.uuid4())
    initial_digest = hashlib.sha256(b"interruption-initial").hexdigest()
    interrupted_digest = hashlib.sha256(b"interruption-resume").hexdigest()
    fake_route_digest = _fake_upstream_fields("claude", "claude-opus-5-5")[
        "upstream_route_digest"]
    store.reserve("claude", initial_digest, initial=True,
                  initial_session_id=session_id,
                  upstream_route_digest=fake_route_digest)
    store.settle("claude", session_id, initial_digest, initial=True,
                 upstream_route_digest=fake_route_digest)
    store.reserve("claude", interrupted_digest, initial=False,
                  upstream_route_digest=fake_route_digest)
    ledger = PermitLedger(controller.root / "locks" / "spend", controller)
    initial_permit = BrokerPermit(
        "claude", run_id, task_id, target_id, seat_id, "initial",
        "claude-opus-5-5", "disposable-placeholder", 1, 100_000,
        100_000, 1024,
        **_fake_upstream_fields("claude", "claude-opus-5-5"),
    )
    initial_id = ledger.register(initial_permit, 1)
    ledger.fence_epoch(initial_id, 1)
    permit = BrokerPermit(
        "claude", run_id, task_id, target_id, seat_id, "resume",
        "claude-opus-5-5", "disposable-placeholder", 1, 100_000,
        100_000, 1024, session_id=session_id,
        **_fake_upstream_fields("claude", "claude-opus-5-5"),
    )
    permit_id = ledger.register(permit, 2)
    request_id = "fake-interrupted-response"
    request = {
        "type": "request", "permit_id": permit_id, "epoch": 2,
        "request_id": request_id, "method": "POST", "provider_path": "/v1/messages",
        "headers": {"content-type": "application/json",
                    "x-api-key": "disposable-placeholder"},
        "body_b64": base64.b64encode(canonical_json({
            "model": permit.model, "max_tokens": 32,
        })).decode(),
    }
    ready = {"type": "ready", "seat_id": seat_id,
             "permit_id": permit_id, "epoch": 2}

    class Frames:
        def __init__(self, incoming):
            self.incoming = iter(incoming)
            self.sent = []

        def recv(self):
            frame = next(self.incoming)
            if isinstance(frame, BaseException):
                raise frame
            return frame

        def send(self, frame):
            self.sent.append(frame)

    sends = []

    def send(method, path, headers, body):
        sends.append((method, path))
        return BrokerResponse(200, {"content-type": "text/event-stream"},
                              [b'data: {"type":"synthetic"}\n\n',
                               b"data: [DONE]\n\n"])

    send.preview = lambda method, path, headers, body: _fake_preview(
        permit, method, path, headers, body)

    frames = Frames((ready, request,
                     BrokerRejected("injected loss before guest acknowledgment")))
    try:
        HostRelay(ledger, Broker(permit, credential="fake-upstream-credential-not-real",
                                 send=send)).serve(frames)
    except BrokerRejected as error:
        if str(error) != "injected loss before guest acknowledgment":
            raise
    else:
        raise CertificationRejected("fake interrupted response unexpectedly settled")
    if not any(frame.get("type") == "response_end" for frame in frames.sent):
        raise CertificationRejected("fake interruption happened before upstream delivery")
    cold = resume_lifecycle_controller(controller.root, controller.capability)
    cold_ledger = PermitLedger(controller.root / "locks" / "spend", cold)
    state, _ = cold_ledger.disposition(request_id)
    retry = Frames((ready, request, {"type": "close"}))
    try:
        HostRelay(cold_ledger, Broker(permit,
                                     credential="fake-upstream-credential-not-real",
                                     send=send)).serve(retry)
    except BrokerRejected as error:
        if str(error) != "relay request attempt budget is exhausted":
            raise
        duplicate_request_refused = True
    else:
        duplicate_request_refused = {"type": "reject", "status": 403} in retry.sent
    cold_store = SessionStore(cold, run_id, task_id, target_id, seat_id,
                              session_volume)
    try:
        cold_store.resume("claude")
    except ProviderRequestError as error:
        uncertain_resume_refused = "uncertain" in str(error)
    else:
        uncertain_resume_refused = False
    return {
        "upstream_send_count": len(sends), "disposition": state,
        "cold_controller_reopened": cold is not controller,
        "duplicate_request_refused": duplicate_request_refused,
        "uncertain_resume_refused": uncertain_resume_refused,
    }


def _attest_image_platform(image_digest: str) -> None:
    """Check the newly supplied RepoDigest on the isolated Docker engine."""
    try:
        result = subprocess.run(
            ("docker", "--context", "colima-fanout-isolated", "image", "inspect",
             image_digest), capture_output=True, timeout=30, check=False)
        if result.returncode != 0 or len(result.stdout) > 1024 * 1024:
            raise ValueError("image inspect failed")
        images = json.loads(result.stdout)
        digests = images[0].get("RepoDigests") if isinstance(images, list) and len(images) == 1 and isinstance(images[0], dict) else None
        if (not isinstance(images, list) or len(images) != 1
                or not isinstance(images[0], dict)
                or images[0].get("Os") != "linux"
                or images[0].get("Architecture") != "amd64"
                or not isinstance(digests, list)
                or image_digest not in digests):
            raise ValueError("image platform or RepoDigest differs")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise CertificationRejected("isolated engine Linux image attestation failed") from None


def _require_live_identity(admission: _LiveAdmission) -> None:
    try:
        if admission.observation_date != datetime.date.today().isoformat():
            raise CertificationRejected("owner approval packet is stale during matrix")
        if (runtime_source_digest(_SOURCE_ROOT) != admission.source_sha256
                or host_runtime_identity() != admission.runtime):
            raise CertificationRejected("source or host runtime changed during matrix")
        current = read_machine_routes(admission.manifest_path, admission.policy_path)
        if current != admission.manifest:
            raise CertificationRejected("machine route or policy changed during matrix")
        digest = _check_owner_packet(
            admission.approval_packet_path,
            _owner_packet(admission.private_run_root,
                          admission.approval_reference, admission.observation_date,
                          current, admission.selected))
        if digest != admission.approval_packet_digest:
            raise CertificationRejected("owner approval packet changed during matrix")
    except (SourceClosureError, RuntimeIdentityError):
        raise CertificationRejected("source or host runtime cannot be reattested") from None
    except RouteRejected:
        raise CertificationRejected("machine route or policy cannot be reattested") from None


def _effective_route_path(profile: RouteProfile, *, stream: bool = False) -> str:
    if profile.provider == "claude":
        action = "streamRawPredict" if stream else "rawPredict"
        return (f"/v1/projects/{profile.project}/locations/{profile.location}"
                f"/publishers/anthropic/models/{profile.effective_model}:{action}")
    if profile.provider == "agy":
        return (f"/v1/projects/{profile.project}/locations/{profile.location}"
                f"/publishers/google/models/{profile.effective_model}"
                ":streamGenerateContent?alt=sse")
    return "/v1/responses"


def _route_receipt(route: RouteAdmission) -> dict[str, str]:
    profile = route.profile
    return {"route_id": profile.route_id, "digest": route.digest,
            "origin": profile.origin, "path": _effective_route_path(profile, stream=True),
            "model": profile.effective_model, "auth_family": profile.auth_family}


def _git_synthetic(repository: Path) -> None:
    """Create only a disposable Git baseline; child sees no direct credentials."""
    commands = (
        ("git", "init", "-q", str(repository)),
        ("git", "-C", str(repository), "add", "README.md"),
        ("git", "-C", str(repository), "-c", "user.name=Fanout Probe",
         "-c", "user.email=probe@example.invalid", "-c", "commit.gpgsign=false",
         "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "synthetic baseline"),
    )
    child_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                 "HOME": str(repository.parent), "GIT_CONFIG_NOSYSTEM": "1",
                 "GIT_CONFIG_GLOBAL": os.devnull}
    for argv in commands:
        try:
            result = subprocess.run(argv, cwd=repository, env=child_env,
                                    capture_output=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise CertificationRejected("synthetic Git baseline could not be created") from None
        if result.returncode != 0:
            raise CertificationRejected("synthetic Git baseline could not be created")


_ISOLATION_SCRIPT = '''import errno,json,os,socket,sys
config=json.loads(sys.stdin.buffer.read())
proof={}
for name,path in config["sentinels"].items():
 try:
  fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
 except OSError:
  read_denied=True
 else:
  os.close(fd); read_denied=False
 parent=path if name in ("git_objects","host_home") else os.path.dirname(path)
 write_path=os.path.join(parent,".fanout-probe-"+config["nonce"])
 try:
  fd=os.open(write_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
 except OSError:
  write_denied=True
 else:
  os.close(fd); os.unlink(write_path); write_denied=False
 proof[name]={"read_denied":read_denied,"write_denied":write_denied}
socket_path="/var/run/docker.sock"
try:
 s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); s.settimeout(1)
 s.connect(socket_path)
except OSError:
 docker_denied=True
else:
 docker_denied=False
finally:
 s.close()
proof["docker_socket"]={"read_denied":docker_denied,"write_denied":docker_denied}
credentials_absent=all(name not in os.environ for name in config["credential_names"])
proof["credentials"]["read_denied"] &= credentials_absent
proof["credentials"]["write_denied"] &= credentials_absent
try:
 s=socket.socket(socket.AF_INET,socket.SOCK_STREAM); s.settimeout(1)
 s.connect(("198.51.100.1",443))
except OSError as error:
 network_denied=error.errno in (errno.ENETUNREACH,errno.EHOSTUNREACH,errno.EACCES,errno.EPERM)
else:
 network_denied=False
finally:
 s.close()
proof["vm_network"]={"read_denied":network_denied,"write_denied":network_denied}
own=os.path.join(config["workdir"],".fanout-own-"+config["nonce"])
try:
 fd=os.open(own,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
 os.write(fd,b"own-workspace-write"); os.close(fd)
 with open(own,"rb") as source: own_write=source.read()==b"own-workspace-write"
 os.unlink(own)
except OSError:
 own_write=False
print(json.dumps({"isolation":proof,"own_workspace_write":own_write},sort_keys=True))
'''


def _probe_isolation(target: Path, sibling: Path, disposable: Path,
                     provider: str, route: str, boundary,
                     transport: PinnedDockerTransport, raw_root: Path
                     ) -> tuple[dict[str, object], bool, str]:
    sentinels = {
        "caller": str(target / "README.md"),
        "sibling": str(sibling / "README.md"),
        "git_objects": str(target / ".git" / "objects"),
        "credentials": str(disposable / "credential-selectors" / "marker"),
        "host_home": str(Path.home()),
    }
    payload = canonical_json({"sentinels": sentinels,
                              "credential_names": list(_LEGACY_KEY_ENVS),
                              "nonce": uuid.uuid4().hex,
                              "workdir": boundary.input_workdir})
    command = ProcessCommand(("python", "-c", _ISOLATION_SCRIPT), payload,
                             cwd=target, timeout=30)
    process = PinnedDockerTransport._docker_exec(
        transport.handle, command, boundary.input_workdir)
    output = raw_root / f"{provider}-{route}-isolation.json"
    _write_private(output, process.stdout)
    _write_private(raw_root / f"{provider}-{route}-isolation-stderr.bin",
                   process.stderr)
    try:
        if (process.status != ProcessStatus.EXIT or process.returncode != 0
                or len(process.stdout) > 32768):
            raise ValueError("probe status")
        observed = json.loads(process.stdout)
        if not isinstance(observed, dict):
            raise ValueError("probe shape")
        isolation = observed["isolation"]
        own_write = observed["own_workspace_write"] is True
    except (ValueError, KeyError, TypeError):
        raise CertificationRejected("container isolation probe is invalid") from None
    return isolation, own_write, str(output)


def _stage_live_inputs(controller, request: ProviderRequest,
                       transport: PinnedDockerTransport,
                       admission: ContainerAdmission, source: Path,
                       repo_bytes: bytes, plan_bytes: bytes,
                       skills_bytes: bytes, dependencies_bytes: bytes
                       ) -> ContainerAdmission:
    for directory in (source, source / "repo", source / "context",
                      source / "context" / "skills",
                      source / "context" / "dependencies"):
        os.mkdir(directory, 0o700)
    _write_private(source / "repo" / "README.md", repo_bytes)
    _write_private(source / "context" / "full-plan.md", plan_bytes)
    _write_private(source / "context" / "skills" / "manifest.json", skills_bytes)
    _write_private(source / "context" / "dependencies" / "peer.json",
                   dependencies_bytes)
    identity = TransferIdentity(request.run_id, request.task_id,
                                request.target_id, request.seat_id,
                                request.inputs_digest)
    manifest = snapshot_manifest(source, identity)
    with ArtifactStore(controller.root / "quarantine" /
                       "input-material-store") as artifacts:
        manifest_ref = artifacts.write_json(
            f"expected/{request.executor_id}.json", asdict(manifest))
    source_info = source.stat()
    material_ref = write_evidence(controller, "container-input-material", {
        "schema_version": "InputMaterialV1", "identity": asdict(identity),
        "source_device": source_info.st_dev, "source_inode": source_info.st_ino,
        "original_handle": asdict(transport.handle),
        "workspace_volume": transport.spec.workspace_volume,
        "run_inputs_sha256": request.inputs_digest,
        "task_packet_sha256": hashlib.sha256(plan_bytes).hexdigest(),
        "target_baseline_sha256": hashlib.sha256(repo_bytes).hexdigest(),
        "workspace_sha256": hashlib.sha256(canonical_json(asdict(manifest))).hexdigest(),
        "skill_delivery_sha256": hashlib.sha256(skills_bytes).hexdigest(),
        "dependency_context_sha256": hashlib.sha256(dependencies_bytes).hexdigest(),
        "provider_context_sha256": hashlib.sha256(plan_bytes).hexdigest(),
        "skill_bundle_sha256": hashlib.sha256(skills_bytes).hexdigest(),
        "manifest_ref": asdict(manifest_ref),
    })
    grant = issue_input_source_grant(
        controller, request, transport, admission, source, *material_ref)
    return replace(admission,
                   input_receipt=transport.stage_inputs(request, grant))


def _certification_seat_id(run_id: str, provider: str) -> str:
    # Full run identity avoids retained-run aliases while bounding guest socket paths.
    digest = hashlib.sha256(canonical_json({"run_id": run_id, "provider": provider})).hexdigest()
    return f"seat-{provider}-{digest}"


def _add_cell_failure_note(error: BaseException, note: str) -> None:
    attributes = BaseException.__dict__["__dict__"].__get__(error)
    if (type(attributes) is not dict or len(attributes) > 8
            or any(type(key) is not str for key in attributes)):
        return
    notes = attributes.get("__notes__")
    if notes is None:
        attributes["__notes__"] = [note]
    elif type(notes) is list:
        notes.append(note)


def _capture_private_wire(ledger: PermitLedger, permit_id: str, epoch: int,
                          raw_root: Path, prefix: str, payload: dict) -> dict:
    from shared.lib.fanout.canary_proof import served_response_identity
    fields = {"method", "provider_path", "normalized_body", "effective_body", "upstream_origin",
              "upstream_path", "upstream_model", "upstream_route_digest", "http_status", "content_type", "response_body", "delivered_body"}
    if (type(payload) is not dict or set(payload) != fields
            or any(type(payload[name]) is not bytes or len(payload[name]) > bound for name, bound in (
                ("normalized_body", 1024 * 1024), ("effective_body", 2 * 1024 * 1024),
                ("response_body", 8 * 1024 * 1024), ("delivered_body", 8 * 1024 * 1024)))
            or type(payload["http_status"]) is not int or not 200 <= payload["http_status"] < 300
            or payload["content_type"] not in {"application/json", "text/event-stream"}):
        raise BrokerRejected("successful private wire proof is unavailable")
    expected = {"method": payload["method"], "path": payload["provider_path"],
        "body_sha256": hashlib.sha256(payload["normalized_body"]).hexdigest(),
        "upstream_body_sha256": hashlib.sha256(payload["effective_body"]).hexdigest(),
        "upstream_origin": payload["upstream_origin"], "upstream_path": payload["upstream_path"],
        "upstream_model": payload["upstream_model"], "upstream_route_digest": payload["upstream_route_digest"]}
    matches = [record for record in ledger.request_records(permit_id, epoch)
               if record["state"] == "uncertain" and all(record["identity"].get(name) == value for name, value in expected.items())]
    if len(matches) != 1:
        raise BrokerRejected("successful private wire reservation differs")
    request_id = matches[0]["request_id"]
    with ledger._locked() as state:
        if state["send_claims"].get(request_id) is not True:
            raise BrokerRejected("successful private wire send claim differs")
        permit = state["permits"][permit_id]
        if (permit["epoch"] != epoch or any(permit[name] != payload[name] for name in (
                "upstream_origin", "upstream_path", "upstream_model", "upstream_route_digest"))
                or any(len(payload[name]) > permit["max_response_bytes"] for name in ("response_body", "delivered_body"))):
            raise BrokerRejected("successful private wire original permit differs")
    captured = {"request_id": request_id, "http_status": payload["http_status"], "content_type": payload["content_type"]}
    with ArtifactStore.open_existing(raw_root) as store:
        for name in ("normalized_body", "effective_body", "response_body"):
            captured[name] = asdict(store.write_bytes(f"{prefix}-wire/{request_id}/{name}.bin", payload[name]))
        captured["delivered_body"] = (captured["response_body"] if payload["delivered_body"] == payload["response_body"] else
            asdict(store.write_bytes(f"{prefix}-wire/{request_id}/delivered_body.bin", payload["delivered_body"])))
    try:
        original = served_response_identity(permit["provider"], permit["model"], permit["upstream_model"],
                                            payload["response_body"], payload["content_type"])
        delivered = served_response_identity(permit["provider"], permit["model"], permit["upstream_model"],
                                             payload["delivered_body"], payload["content_type"])
        if original != delivered:
            raise ValueError("original/delivered identity differs")
    except (ValueError, BrokerRejected, UnicodeError, TypeError, AttributeError):
        raise BrokerRejected("private wire served completion refused; diagnostic bytes retained") from None
    return captured


def _live_cell(controller, ledger: PermitLedger, target: Path, sibling: Path,
               disposable: Path, raw_root: Path, baseline: RepositoryBaseline,
               run_id: str, provider: str, route: str,
               admission: _LiveAdmission, previous: Mapping[str, object] | None,
               approval_evidence_digest: str) -> dict[str, object]:
    _require_live_identity(admission)
    selected_route = admission.selected[provider]
    upstream = selected_route.profile
    try:
        source = HostCredentialSource(upstream)
        credential = source.load()
        fingerprint = source.fingerprint
    except RouteCredentialError:
        raise CertificationRejected("host credential source is unavailable") from None
    if not _sha(fingerprint):
        raise CertificationRejected("host credential source proof is unavailable")
    if previous is not None and previous.get("credential_source_fingerprint") != fingerprint:
        raise CertificationRejected("host credential source changed before cold resume")
    _require_live_identity(admission)
    profile = ProviderRegistry.default(version_probe=lambda _: "unused").select(
        provider, "repo-write", "standard")
    task_id, target_id = "synthetic-task", "repo-alpha"
    seat_id = _certification_seat_id(run_id, provider)
    seat_socket_path(seat_id)
    workspace, session_volume = private_volume_names(
        run_id, task_id, target_id, seat_id)
    store = SessionStore(controller, run_id, task_id, target_id, seat_id,
                         session_volume)
    session_id = (str(uuid.uuid4()) if provider == "claude" and route == "initial"
                  else None)
    if route == "resume":
        session_id = store.resume(provider).session_id
    marker = f"certified synthetic {provider} {route}\n".encode()
    plan_bytes = (b"# Complete synthetic repo-write plan\n"
                  b"Target: repo-alpha. Context: private bounded certification.\n"
                  b"Edit only README.md, preserving all existing bytes; append exactly this line:\n" + marker +
                  b"Include exactly one trailing LF newline byte (0x0A) in the appended bytes.\n"
                  b"Do not modify any other path. Report the completed edit.\n")
    if provider == "agy":
        plan_bytes += (b"The current directory is already the target repository; README.md exists.\n"
                       b"Read it once with view_file, then use a native file-editing tool to append the line, "
                       b"preserving all existing bytes. Report completion.\n"
                       b"Do not use shell commands, directory listings, or byte-inspection commands "
                       b"for this single-file edit.\n")
    skills_bytes = canonical_json({"skills": ["llm-fanout-execute"],
                                   "delivery": "bounded synthetic context"})
    dependencies_bytes = canonical_json({"repo-beta": "synthetic sibling only"})
    repo_bytes = (target / "README.md").read_bytes()
    inputs_digest = hashlib.sha256(repo_bytes).hexdigest()
    request = ProviderRequest(
        executor_id=provider, prompt=plan_bytes.decode(), cwd=target,
        execution_class="repo-write", profile=profile, run_id=run_id,
        task_id=task_id, target_id=target_id, seat_id=seat_id,
        inputs_digest=inputs_digest, session_id=session_id,
        resume=route == "resume",
        context_sha256=hashlib.sha256(plan_bytes).hexdigest(),
        skill_bundle_sha256=hashlib.sha256(skills_bytes).hexdigest(),
        skill_delivery_sha256=hashlib.sha256(skills_bytes).hexdigest(),
    )
    permit = BrokerPermit(
        provider, run_id, task_id, target_id, seat_id, route,
        profile.requested_model, "disposable-placeholder", _MAX_MAIN_REQUESTS,
        1024 * 1024, 8 * 1024 * 1024, _SPEND_CEILING["max_output_tokens_per_call"],
        response_deadline=300, inputs_digest=inputs_digest,
        session_id=session_id,
        clamp_output_tokens=provider in {"claude", "agy"},
        upstream_route_digest=selected_route.digest,
        upstream_origin=upstream.origin,
        upstream_model=upstream.effective_model,
        upstream_path=_effective_route_path(upstream, stream=True))
    epoch = 1 if route == "initial" else 2
    permit_id = ledger.register(permit, epoch)
    transport = None
    raw_artifacts = {}
    wire_captures = {}
    process_proof = None

    def capture_upstream_failure(payload):
        if (type(payload) is not dict or any(type(key) is not str for key in payload)
                or payload.keys() != HostUpstream._failure_fields):
            raise BrokerRejected("upstream diagnostic shape differs")
        strings = HostUpstream._failure_fields - {"http_status", "structured_error", "omission"}
        if any(type(payload[key]) is not str for key in strings):
            raise BrokerRejected("upstream diagnostic fields differ")
        if (payload["phase"] not in _FAILURE_PHASES or payload["category"] not in _FAILURE_CATEGORIES
                or payload["method"] != "POST"
                or not all(_sha(payload[key]) for key in ("normalized_body_sha256", "upstream_body_sha256", "upstream_route_digest"))
                or payload["http_status"] is not None and type(payload["http_status"]) is not int
                or payload["omission"] is not None and (type(payload["omission"]) is not str or payload["omission"] not in _FAILURE_OMISSIONS)):
            raise BrokerRejected("upstream diagnostic fields differ")
        expected = {"requested_model": permit.model, "upstream_model": permit.upstream_model,
                    "upstream_origin": permit.upstream_origin, "upstream_path": permit.upstream_path,
                    "upstream_route_digest": selected_route.digest}
        if any(payload[key] != value for key, value in expected.items()):
            raise BrokerRejected("upstream diagnostic route differs")
        error = payload["structured_error"]
        if error is not None:
            if (type(error) is not dict or any(type(key) is not str or key not in {"code", "type", "status", "message"}
                                               for key in error)
                    or any(type(value) not in (str, int) for value in error.values())):
                raise BrokerRejected("upstream diagnostic error differs")
            if any(type(value) is str and len(value) > (4096 if key == "message" else 128)
                   for key, value in error.items()):
                raise BrokerRejected("upstream diagnostic error exceeds bound")
            projected, omission = _error_projection(canonical_json({"error": error}), credential)
            if omission is not None or projected != error:
                raise BrokerRejected("upstream diagnostic error differs")
        identity = {"method": payload["method"], "path": payload["provider_path"],
                    "body_sha256": payload["normalized_body_sha256"],
                    "upstream_body_sha256": payload["upstream_body_sha256"],
                    **{key: payload[key] for key in ("upstream_route_digest", "upstream_origin", "upstream_path", "upstream_model")}}
        records = ledger.request_records(permit_id, epoch)
        if (ledger.controller.controller_id != controller.controller_id
                or not any(all(record["identity"].get(key) == value for key, value in identity.items()) for record in records)
                or not _sha(admission.source_sha256) or not _sha(approval_evidence_digest)):
            raise BrokerRejected("upstream diagnostic reservation differs")
        write_evidence(controller, "container-upstream-failure", {
            **payload, "schema_version": 1, "controller_id": controller.controller_id,
            "source_sha256": admission.source_sha256, "approval_evidence_digest": approval_evidence_digest,
            "permit_id": permit_id, "permit_epoch": epoch, "permit_digest": _digest(permit),
            **{key: getattr(permit, key) for key in ("run_id", "task_id", "target_id", "seat_id", "provider", "route")},
        })

    try:
        def capture_upstream_success(payload):
            captured = _capture_private_wire(ledger, permit_id, epoch, raw_root, f"{provider}-{route}", payload)
            wire_captures[captured["request_id"]] = captured

        broker = Broker(permit, credential=credential,
                        send=HostUpstream(permit, selected_route,
                                          max_response_bytes=permit.max_response_bytes,
                                          deadline_seconds=permit.response_deadline,
                                          failure_sink=capture_upstream_failure,
                                          success_sink=capture_upstream_success))
        spec = ContainerSpec(run_id, task_id, target_id, seat_id,
                             admission.image_digest, workspace, session_volume,
                             f"/run/fanout/{seat_id}.sock",
                             admission.cli_digests[provider])
        transport = PinnedDockerTransport.start_live(
            controller, spec, ledger=ledger, broker=broker,
            permit_id=permit_id, epoch=epoch, recover=route == "resume", request=request)
        vm = transport.attest_vm()
        control = getattr(transport, "_control", None)
        vm_observation = getattr(control, "vm_observation", None)
        socket_proof = transport.attest_socket()
        admission_record = ContainerAdmission(
            permit, permit_id, epoch, ledger, spec.image_digest,
            spec.cli_digest, socket_proof, transport, store,
            transport.handle_evidence_name, transport.handle_evidence_digest)
        if route == "initial":
            source = controller.root / "quarantine" / f"source-{provider}"
            admission_record = _stage_live_inputs(
                controller, request, transport, admission_record, source,
                repo_bytes, plan_bytes, skills_bytes, dependencies_bytes)
        boundary = issue_container_boundary(
            controller, request, vm, transport.handle, admission_record)
        isolation, own_write, probe_path = _probe_isolation(
            target, sibling, disposable, provider, route, boundary,
            transport, raw_root)
        raw_artifacts["isolation"] = probe_path
        if (not own_write or not isinstance(isolation, Mapping)
                or any(not isinstance(isolation.get(name), Mapping)
                       or isolation[name].get("read_denied") is not True
                       or isolation[name].get("write_denied") is not True
                       for name in ISOLATION_TARGETS)):
            raise CertificationRejected("container isolation probe failed before send")
        _require_live_identity(admission)
        try:
            result = run_container_turn(request, boundary)
        finally:
            process = getattr(transport, "process_result", None)
            if isinstance(process, ProcessResult):
                for name, data in (("stdout", process.stdout),
                                   ("stderr", process.stderr)):
                    path = raw_root / f"{provider}-{route}-{name}.bin"
                    _write_private(path, data)
                    raw_artifacts[name] = str(path)
                process_proof = {"status": process.status.value, "returncode": process.returncode,
                    "capture_complete": process.capture_complete,
                    "stdout": asdict(ArtifactRef(f"{provider}-{route}-stdout.bin", hashlib.sha256(process.stdout).hexdigest(), len(process.stdout))),
                    "stderr": asdict(ArtifactRef(f"{provider}-{route}-stderr.bin", hashlib.sha256(process.stderr).hexdigest(), len(process.stderr)))}
        transport.close()
        stopped_wire = transport.launcher._run("inspect", "--type", "container",
                                               transport.handle.container_id)
        stop_path = raw_root / f"{provider}-{route}-stopped-inspect.json"
        _write_private(stop_path, stopped_wire)
        stopped_items = json.loads(stopped_wire)
        seat_stopped = (isinstance(stopped_items, list) and len(stopped_items) == 1
                        and stopped_items[0].get("Id") == transport.handle.container_id
                        and stopped_items[0].get("State", {}).get("Running") is False)
        stop_name, stop_digest = write_evidence(controller, "container-live-stopped", {
            "container_id": transport.handle.container_id,
            "seat_id": seat_id, "session_volume": session_volume,
            "permit_id": permit_id, "stopped": seat_stopped,
        })
        if not seat_stopped:
            raise CertificationRejected("exact live seat stop was not observed")
        ledger.fence_epoch(permit_id, epoch)
        records, attempts, auxiliary, fenced = _ledger_snapshot(
            controller, permit_id, epoch)
        auxiliary_refusals = ledger.auxiliary_refusal_count(permit_id, epoch)
        _require_live_identity(admission)
        answer_path = raw_root / f"{provider}-{route}-answer.bin"
        _write_private(answer_path, result.answer.encode())
        raw_artifacts["answer"] = str(answer_path)
        raw_artifacts["stopped_inspect"] = str(stop_path)
        candidate = None
        export = result.candidate_export
        if result.valid:
            expected_delta = ((f"certified synthetic {provider} initial\n".encode()
                               if route == "resume" else b"") + marker)
            candidate = _checked_candidate(controller, baseline, export,
                                           expected_delta)
        dispositions = [{"request_id": record["request_id"],
                         "state": record["state"],
                         "response_sha256": record["response_digest"]}
                        for record in records]
        outbound_wire_model = records[0]["identity"]["wire_model"] if records else None
        effective = records[0]["identity"] if records else {}
        all_isolated = (isinstance(isolation, Mapping)
                        and all(isinstance(isolation.get(name), Mapping)
                                and isolation[name].get("read_denied") is True
                                and isolation[name].get("write_denied") is True
                                for name in ISOLATION_TARGETS))
        passed = (result.valid and candidate is not None and all_isolated and own_write
                  and fenced)
        cell = {
            "passed": passed, "route": route,
            "route_id": upstream.route_id,
            "machine_route_digest": effective.get("upstream_route_digest"),
            "manifest_digest": admission.manifest.manifest_digest,
            "policy_digest": admission.manifest.policy_digest,
            "credential_source_fingerprint": fingerprint,
            "effective_origin": effective.get("upstream_origin"),
            "effective_path": effective.get("upstream_path"),
            "effective_model": effective.get("upstream_model"),
            "source_sha256": admission.source_sha256,
            "host_runtime_sha256": hashlib.sha256(canonical_json(
                asdict(admission.runtime))).hexdigest(),
            "vm_probe_sha256": vm.digest,
            "socket_attestation_sha256": hashlib.sha256(canonical_json(
                asdict(socket_proof))).hexdigest(),
            "image_digest": spec.image_digest, "cli_digest": spec.cli_digest,
            "profile_digest": profile.digest,
            "container_id": transport.handle.container_id,
            "container_inspect_sha256": transport.handle.inspect_sha256,
            "session_volume": session_volume, "session_id": result.session_id,
            "requested_model": profile.requested_model,
            "observed_model": result.observed_model,
            "outbound_wire_model": outbound_wire_model,
            "broker_request_count": len(records),
            "broker_attempt_count": attempts,
            "broker_auxiliary_attempt_count": auxiliary,
            "broker_auxiliary_refusal_count": auxiliary_refusals,
            "ledger_requests": list(records), "broker_dispositions": dispositions,
            "permit_id": permit_id, "permit_epoch": epoch,
            "permit_fenced": fenced,
            "prior_seat_stopped": previous is None or previous.get("seat_stopped") is True,
            "seat_stopped": seat_stopped,
            "cold_controller_reopened": route == "resume",
            "recovered_prior_container_id": (previous.get("container_id")
                                             if previous is not None else None),
            "stop_evidence_name": stop_name,
            "stop_evidence_digest": stop_digest,
            "approval_evidence_digest": approval_evidence_digest,
            "input_receipt_digest": boundary.input_receipt_digest,
            "input_manifest_sha256": boundary.input_manifest_sha256,
            "candidate_digest": candidate.digest if candidate is not None else None,
            "candidate_edited_paths": ([entry.path for entry in candidate.entries]
                                       if candidate is not None else []),
            "candidate_export_manifest_sha256": (export.manifest_sha256
                                                 if export is not None else None),
            "candidate_export_evidence_digest": (export.evidence_digest
                                                 if export is not None else None),
            "candidate_export_root": str(export.root) if export is not None else None,
            "isolation": isolation, "own_workspace_write": own_write,
            "answer_sha256": hashlib.sha256(result.answer.encode()).hexdigest(),
            "stdout_sha256": hashlib.sha256(result.stdout).hexdigest(),
            "stderr_sha256": hashlib.sha256(result.stderr).hexdigest(),
            "elapsed_seconds": result.duration,
            "raw_artifacts": raw_artifacts,
        }
        cell["passed"] = passed and _broker_evidence_valid(provider, cell)
        if cell["passed"] and getattr(transport, "_production_edge", False):
            if (vm_observation is None or process_proof is None or transport.stop_evidence is None
                    or set(wire_captures) != {record["request_id"] for record in records}
                    or not transport.runtime_observation):
                raise CertificationRejected("complete successful cell proof is unavailable")
            with ArtifactStore.open_existing(raw_root) as raw:
                vm_ref = raw.write_json(f"{provider}-{route}-vm.json", vm_observation)
                runtime_ref = raw.write_json(f"{provider}-{route}-runtime.json", transport.runtime_observation)
            isolation_raw = Path(probe_path).read_bytes()
            attestation = {"schema_version": "fanout-private-cell-attestation-v1",
                "release_source_binding": release_source_binding().to_dict(), "host_runtime": asdict(admission.runtime),
                "request": {name: str(getattr(request, name)) if name == "cwd" else getattr(request, name) for name in (
                    "executor_id", "prompt", "cwd", "execution_class", "session_id", "resume", "run_id", "task_id",
                    "target_id", "seat_id", "inputs_digest", "context_sha256", "skill_bundle_sha256", "skill_delivery_sha256")},
                "permit": asdict(permit), "boundary_evidence_name": boundary.evidence_name,
                "boundary_evidence_digest": boundary.evidence_digest, "vm": asdict(vm_ref), "runtime": asdict(runtime_ref),
                "process": process_proof,
                "answer": asdict(ArtifactRef(answer_path.name, hashlib.sha256(result.answer.encode()).hexdigest(), len(result.answer.encode()))),
                "isolation": asdict(ArtifactRef(Path(probe_path).name, hashlib.sha256(isolation_raw).hexdigest(), len(isolation_raw))),
                "stop_evidence_name": transport.stop_evidence[0], "stop_evidence_digest": transport.stop_evidence[1],
                "baseline_root": str(target), "baseline_digest": baseline.digest,
                "wire": [wire_captures[record["request_id"]] for record in records]}
            name, digest = write_evidence(controller, "container-private-cell-attestation", attestation)
            cell.update(attestation_evidence_name=name, attestation_evidence_digest=digest)
        return cell
    except BaseException as primary:
        try:
            ledger.fence_epoch(permit_id, epoch)
        except BaseException as fencing:
            _add_cell_failure_note(primary, f"Exact permit fence failed: {_failure_type(fencing)}")
        if transport is not None:
            try:
                transport.close()
            except BaseException as cleanup:
                _add_cell_failure_note(primary, f"Exact seat stop failed: {_failure_type(cleanup)}")
                try:
                    reference = _failure_cleanup(controller, cleanup, run_id, provider)
                except BaseException:
                    pass
                else:
                    if reference["status"] == "authenticated":
                        _add_cell_failure_note(
                            primary, "Container cleanup failed; seat requires quarantine; receipt="
                            + reference["digest"])
        raise


def _failure_type(error: BaseException) -> str:
    # Identity checks keep hostile subclass names and attribute hooks out of evidence.
    known = (
        (CertificationRejected, "CertificationRejected"),
        (ProviderRequestError, "ProviderRequestError"),
        (BrokerRejected, "BrokerRejected"), (RouteRejected, "RouteRejected"),
        (RouteCredentialError, "RouteCredentialError"),
        (SourceClosureError, "SourceClosureError"),
        (RuntimeIdentityError, "RuntimeIdentityError"), (LifecycleError, "LifecycleError"),
        (ValueError, "ValueError"), (RuntimeError, "RuntimeError"),
        (TypeError, "TypeError"), (OSError, "OSError"),
        (FileNotFoundError, "FileNotFoundError"), (PermissionError, "PermissionError"),
        (TimeoutError, "TimeoutError"), (ConnectionError, "ConnectionError"),
        (BrokenPipeError, "BrokenPipeError"), (KeyError, "KeyError"),
        (IndexError, "IndexError"), (AttributeError, "AttributeError"),
        (AssertionError, "AssertionError"), (Exception, "Exception"),
    )
    return next((label for cls, label in known if type(error) is cls), "UnknownError")


def _failure_source_codes() -> dict[str, tuple[str, set[CodeType]]]:
    trusted = {}
    for name in _source_paths(_SOURCE_ROOT):
        if not (name == "scripts/fanout_container_certify.py"
                or name.startswith("shared/lib/fanout/") and name.endswith(".py")):
            continue
        filename = str(_SOURCE_ROOT / name)
        pending = [compile(_read_source(_SOURCE_ROOT, name, 1024 * 1024),
                           filename, "exec", dont_inherit=True)]
        codes = set()
        while pending:
            code = pending.pop()
            codes.add(code)
            pending.extend(value for value in code.co_consts if type(value) is CodeType)
        trusted[filename] = name, codes
    return trusted


def _failure_code_safe(code: CodeType) -> bool:
    # Code hashing/equality dispatch into constants; admit only inert exact types first.
    pending = [(code, 0)]
    remaining, byte_budget = 4096, 1024 * 1024
    while pending:
        value, depth = pending.pop()
        remaining -= 1
        if remaining < 0 or depth > 32:
            return False
        kind = type(value)
        if kind is CodeType:
            children = (value.co_consts, value.co_names, value.co_name,
                        value.co_filename, value.co_varnames, value.co_freevars,
                        value.co_cellvars, value.co_code, value.co_linetable,
                        value.co_exceptiontable)
        elif kind is tuple or kind is frozenset:
            if len(value) > remaining - len(pending):
                return False
            children = value
        elif kind is str or kind is bytes:
            byte_budget -= len(value) * (4 if kind is str else 1)
            children = ()
        elif kind is int:
            byte_budget -= (value.bit_length() + 7) // 8
            children = ()
        elif (kind is bool or kind is float or kind is complex
              or value is None or value is Ellipsis):
            children = ()
        else:
            return False
        if byte_budget < 0:
            return False
        pending.extend((child, depth + 1) for child in children)
    return True


def _failure_cleanup(controller, error: BaseException, run_id: str,
                     provider: str) -> dict[str, str]:
    prefix = "Container cleanup failed; seat requires quarantine; receipt="
    attributes = BaseException.__dict__["__dict__"].__get__(error)
    if type(attributes) is not dict:
        return {"status": "unavailable"}
    notes = ()
    for key, value in islice(attributes.items(), 8):
        if type(key) is str and key == "__notes__":
            notes = value
            break
    if type(notes) is not list and type(notes) is not tuple:
        return {"status": "unavailable"}
    for note in notes[:8]:
        if type(note) is not str or not note.startswith(prefix):
            continue
        candidate = note[len(prefix):len(prefix) + 65]
        digest = candidate[:64]
        if not _sha(digest) or len(candidate) > 64 and candidate[64] != ";":
            return {"status": "invalid"}
        name, category = digest + ".json", "container-seat-cleanup-failure"
        try:
            cleanup = read_evidence(controller, category, name, digest)
        except BaseException:
            return {"status": "invalid"}
        spec = cleanup.get("spec")
        identities = [cleanup]
        if isinstance(spec, dict):
            identities.append(spec)
        expected = {"run_id": run_id, "provider": provider,
                    "seat_id": _certification_seat_id(run_id, provider)}
        if any(record.get(key) is not None and record[key] != value
               for record in identities for key, value in expected.items()):
            return {"status": "invalid"}
        return {"status": "authenticated", "name": name, "digest": digest,
                "category": category}
    return {"status": "unavailable"}


def _retain_cell_failure(controller, admission: _LiveAdmission, run_id: str,
                         provider: str, route: str, error: BaseException) -> dict[str, str]:
    try:
        if runtime_source_digest(_SOURCE_ROOT) != admission.source_sha256:
            return {"status": "unavailable"}
        trusted = _failure_source_codes()
        entries, seen = [], {}
        truncated, cycle = False, False

        def visit(current: BaseException | None, ancestors: set[int]) -> int | None:
            nonlocal truncated, cycle
            if current is None:
                return None
            identity = id(current)
            if identity in seen:
                cycle |= identity in ancestors
                return seen[identity]
            if len(entries) == 8:
                truncated = True
                return None
            index = len(entries)
            seen[identity] = index
            frames = []
            trace = BaseException.__traceback__.__get__(current)
            inspected, frames_truncated = 0, False
            while trace is not None and inspected < 64:
                code = trace.tb_frame.f_code
                source = (trusted.get(code.co_filename) if _failure_code_safe(code)
                          else None)
                if source is not None and code in source[1]:
                    if len(frames) < 8:
                        frames.append({"filename": source[0], "line": trace.tb_lineno})
                    else:
                        frames_truncated = True
                inspected += 1
                trace = trace.tb_next
            entry = {"type": _failure_type(current), "frames": frames,
                     "frames_truncated": frames_truncated or trace is not None,
                     "cleanup": _failure_cleanup(controller, current, run_id, provider),
                     "suppress_context": BaseException.__suppress_context__.__get__(current)}
            entries.append(entry)
            linked_ancestors = ancestors | {identity}
            entry["cause"] = visit(BaseException.__cause__.__get__(current), linked_ancestors)
            entry["context"] = visit(BaseException.__context__.__get__(current), linked_ancestors)
            return index

        visit(error, set())
        capsule = {"schema_version": "fanout-live-cell-failure-v1",
                   "source_sha256": admission.source_sha256,
                   "controller_id": controller.controller_id, "run_id": run_id,
                   "provider": provider, "route": route, "phase": "live-cell",
                   "exceptions": entries, "exceptions_truncated": truncated,
                   "cycle_detected": cycle}
        if (len(canonical_json(capsule)) > 16 * 1024
                or runtime_source_digest(_SOURCE_ROOT) != admission.source_sha256):
            return {"status": "unavailable"}
        category = "container-live-cell-failure"
        name, digest = write_evidence(controller, category, capsule)
        if read_evidence(controller, category, name, digest) != capsule:
            return {"status": "unavailable"}
        return {"status": "authenticated", "name": name, "digest": digest,
                "category": category}
    except BaseException:
        return {"status": "unavailable"}


def _live_matrix(target: Path, disposable: Path,
                 admission: _LiveAdmission) -> dict[str, object]:
    if disposable != admission.private_run_root:
        raise CertificationRejected("owner approval packet covers another private run")
    _require_live_identity(admission)
    _attest_image_platform(admission.image_digest)
    os.mkdir(disposable, 0o700)
    os.mkdir(disposable / "targets", 0o700)
    for name in ("repo-alpha", "repo-beta"):
        repository = disposable / "targets" / name
        os.mkdir(repository, 0o700)
        _write_private(repository / "README.md",
                       f"# {name}\nDisposable synthetic target.\n".encode())
        _git_synthetic(repository)
    for name in ("credential-selectors",):
        root = disposable / name
        os.mkdir(root, 0o700)
        _write_private(root / "marker", b"synthetic isolation sentinel\n")
    capability = LifecycleCapability(secrets.token_urlsafe(32))
    _write_private(
        disposable.with_name(disposable.name + ".controller-capability"),
        capability.export_token().encode("ascii"))
    controller = create_lifecycle_controller(
        disposable / "controller", capability=capability)
    ledger = PermitLedger(controller.root / "locks" / "spend", controller)
    os.mkdir(disposable / "raw", 0o700)
    approval = {"reference": admission.approval_reference,
                "observation_date": admission.observation_date,
                "spend_scope": _SPEND_SCOPE,
                "source_sha256": admission.source_sha256,
                "host_runtime": asdict(admission.runtime),
                "image_digest": admission.image_digest,
                "cli_digests": admission.cli_digests,
                "manifest_digest": admission.manifest.manifest_digest,
                "policy_digest": admission.manifest.policy_digest,
                "approval_packet_path": str(admission.approval_packet_path),
                "approval_packet_digest": admission.approval_packet_digest,
                "spend_ceiling": _SPEND_CEILING,
                "machine_routes": {provider: _route_receipt(admission.selected[provider])
                                   for provider in PROVIDERS}}
    _write_private(disposable / "approval.json", canonical_json(approval))
    approval_name, approval_digest = write_evidence(
        controller, "container-spend-approval", approval)
    baseline = capture_repository_baseline(target)
    receipt: dict[str, object] = {
        "schema_version": SCHEMA, "mode": "live",
        "source_sha256": admission.source_sha256,
        "release_source_binding": release_source_binding().to_dict(),
        "host_runtime": asdict(admission.runtime),
        "image_digest": admission.image_digest,
        "cli_digests": admission.cli_digests,
        "manifest_path": str(admission.manifest_path),
        "policy_path": str(admission.policy_path),
        "manifest_digest": admission.manifest.manifest_digest,
        "policy_digest": admission.manifest.policy_digest,
        "machine_routes": approval["machine_routes"],
        "approval_packet_path": str(admission.approval_packet_path),
        "approval_packet_digest": admission.approval_packet_digest,
        "spend_ceiling": _SPEND_CEILING,
        "approval_reference": admission.approval_reference,
        "observation_date": admission.observation_date,
        "spend_scope": _SPEND_SCOPE,
        "approval_evidence_name": approval_name,
        "approval_evidence_digest": approval_digest,
        "target_ids": ["repo-alpha", "repo-beta"],
        "private_run_root": str(disposable), "providers": {}, "answers": {},
    }
    run_id = "private-" + uuid.uuid4().hex[:24]
    for provider in PROVIDERS:
        cells = {}
        for route in ROUTES:
            previous = cells.get("initial") if route == "resume" else None
            if route == "resume":
                cold = resume_lifecycle_controller(
                    controller.root, controller.capability)
                active_ledger = PermitLedger(cold.root / "locks" / "spend", cold)
            else:
                cold, active_ledger = controller, ledger
            try:
                cell = _live_cell(
                    cold, active_ledger, target,
                    disposable / "targets" / "repo-beta", disposable,
                    disposable / "raw", baseline, run_id, provider, route,
                    admission, previous, approval_digest)
            except Exception as error:
                receipt["failure"] = {"provider": provider, "route": route,
                                      "type": _failure_type(error),
                                      "diagnostic": _retain_cell_failure(
                                          cold, admission, run_id, provider, route, error)}
                _write_private(disposable / "receipt.json", canonical_json(receipt))
                return receipt
            cells[route] = cell
            receipt["providers"][provider] = cells
            if cell.get("passed") is not True:
                receipt["failure"] = {"provider": provider, "route": route,
                                      "type": "CellFailed"}
                _write_private(disposable / "receipt.json", canonical_json(receipt))
                return receipt
            if route == "resume":
                answer_path = disposable / f"answer-{provider}.bin"
                answer_data = Path(cell["raw_artifacts"]["answer"]).read_bytes()
                answer_digest = _write_private(answer_path, answer_data)
                receipt["answers"][provider] = {
                    "path": str(answer_path), "sha256": answer_digest}
    if not evaluate_private_probe(receipt).passed:
        receipt["failure"] = {"type": "EvaluationFailed"}
        _write_private(disposable / "receipt.json", canonical_json(receipt))
        return receipt
    completion_name, completion_digest = write_evidence(
        controller, "container-private-matrix-complete", {
            "schema_version": SCHEMA,
            "receipt_sha256": hashlib.sha256(canonical_json(receipt)).hexdigest(),
        })
    receipt["completion_evidence_name"] = completion_name
    receipt["completion_evidence_digest"] = completion_digest
    _write_private(disposable / "receipt.json", canonical_json(receipt))
    return receipt


def certification_run(target_root: Path, disposable_root: Path, *,
                      fake_route: bool = False, allow_paid: bool = False,
                      approval_reference: str | None = None,
                      observation_date: str | None = None,
                      image_digest: str | None = None,
                      cli_digests: Mapping[str, str] | None = None,
                      manifest_path: Path | None = None,
                      policy_path: Path | None = None,
                      route_ids: Mapping[str, str] | None = None,
                      approval_packet_path: Path | None = None) -> dict:
    """Run one fresh private six-cell matrix against synthetic targets only."""
    target, disposable = _validated_roots(target_root, disposable_root)
    if fake_route and allow_paid:
        raise CertificationRejected("fake route cannot use paid admission")
    if not fake_route:
        admitted = _admit_live(
            allow_paid=allow_paid, approval_reference=approval_reference,
            observation_date=observation_date, image_digest=image_digest,
            cli_digests=cli_digests, manifest_path=manifest_path,
            policy_path=policy_path, route_ids=route_ids,
            private_run_root=disposable,
            approval_packet_path=approval_packet_path,
        )
        return _live_matrix(target, disposable, admitted)
    try:
        source_sha256 = runtime_source_digest(_SOURCE_ROOT)
    except SourceClosureError as error:
        raise CertificationRejected(f"source closure is invalid: {error}") from error
    os.mkdir(disposable, 0o700)
    os.mkdir(disposable / "targets", 0o700)
    for name in ("repo-alpha", "repo-beta"):
        repository = disposable / "targets" / name
        os.mkdir(repository, 0o700)
        _write_private(repository / "README.md",
                       f"# {name}\nDisposable synthetic target.\n".encode())
    controller = create_lifecycle_controller(disposable / "controller")
    ledger = PermitLedger(controller.root / "locks" / "spend", controller)
    os.mkdir(disposable / "raw", 0o700)
    inputs_digest = hashlib.sha256((target / "README.md").read_bytes()).hexdigest()
    os.mkdir(disposable / "transfer-alpha", 0o700)
    providers = {}
    for provider in PROVIDERS:
        initial = _fake_cell(controller, ledger, target,
                             disposable / "targets" / "repo-beta", disposable / "raw",
                             provider, "initial",
                             source_sha256, inputs_digest, None)
        cold_controller = resume_lifecycle_controller(
            controller.root, controller.capability)
        cold_ledger = PermitLedger(controller.root / "locks" / "spend",
                                   cold_controller)
        resume = _fake_cell(cold_controller, cold_ledger, target,
                            disposable / "targets" / "repo-beta", disposable / "raw",
                            provider, "resume",
                            source_sha256, inputs_digest, initial,
                            cold_controller_reopened=(cold_controller is not controller
                                                      and cold_controller.controller_id
                                                      == controller.controller_id))
        providers[provider] = {"initial": initial, "resume": resume}
    receipt = {"schema_version": SCHEMA, "mode": "fake",
               "source_sha256": source_sha256,
               "target_ids": ["repo-alpha", "repo-beta"],
               "private_run_root": str(disposable), "providers": providers,
               "interruption_probe": _interrupted_send_probe(controller)}
    _write_private(disposable / "receipt.json", canonical_json(receipt))
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Private synthetic container certification")
    parser.add_argument("--private-run-root", type=Path, required=True)
    parser.add_argument("--profiles", choices=("claude,codex,agy",),
                        default="claude,codex,agy")
    parser.add_argument("--routes", choices=("initial,resume",),
                        default="initial,resume")
    parser.add_argument("--fake-route", action="store_true")
    parser.add_argument("--allow-paid", action="store_true")
    parser.add_argument("--quality-maka", action="store_true")
    parser.add_argument("--prepare-synthetic-targets", action="store_true")
    parser.add_argument("--synthetic-trial", action="store_true")
    parser.add_argument("--admission-packet", type=Path)
    parser.add_argument("--skill-root", type=Path, action="append", default=[])
    parser.add_argument("--quality-approval-ref")
    parser.add_argument("--maka-launcher", type=Path)
    parser.add_argument("--maka-boundary-sha256")
    parser.add_argument("--owner-approval-ref")
    parser.add_argument("--owner-approval-packet", type=Path)
    parser.add_argument("--observation-date")
    parser.add_argument("--image-digest")
    parser.add_argument("--machine-routes", type=Path)
    parser.add_argument("--machine-policy", type=Path)
    parser.add_argument("--route-id", action="append", default=[],
                        metavar="PROVIDER=ROUTE-ID")
    for provider in PROVIDERS:
        parser.add_argument(f"--{provider}-cli-sha256")
    args = parser.parse_args(argv)
    if args.prepare_synthetic_targets or args.synthetic_trial:
        from shared.lib.fanout.container_coordinator import prepare_container_trial_targets, produce_container_trial
        from shared.lib.fanout.container_certification import _certificate
        try:
            if (args.prepare_synthetic_targets and args.synthetic_trial
                    or args.fake_route or args.quality_maka):
                raise CertificationRejected("synthetic trial operation is ambiguous")
            if args.prepare_synthetic_targets:
                if args.allow_paid or args.admission_packet or args.skill_root:
                    raise CertificationRejected("target preparation does not accept provider authority")
                roots = prepare_container_trial_targets(args.private_run_root)
                print(json.dumps({"roots": [str(root) for root in roots], "provider_turns": 0}, sort_keys=True))
            else:
                if not args.allow_paid or args.admission_packet is None or not args.skill_root:
                    raise CertificationRejected("synthetic trial requires paid approval and reviewed admission")
                value, raw = _read_owned_json(args.private_run_root / "trial-certificate.json")
                if canonical_json(value) != raw:
                    raise CertificationRejected("trial certificate file is noncanonical")
                certificate = _certificate(value)
                packet, _ = _read_owned_json(args.admission_packet)
                produce_container_trial(args.private_run_root, certificate, packet, tuple(args.skill_root), allow_paid=True)
                print(json.dumps({"passed": True, "core_checks": 5}, sort_keys=True))
            return 0
        except Exception:
            parser.exit(1, "synthetic trial authority or execution is unavailable\n")
    if args.quality_maka:
        try:
            quality = _quality_run(
                args.private_run_root, allow_paid=args.allow_paid,
                approval_reference=args.quality_approval_ref,
                observation_date=args.observation_date,
                launcher=args.maka_launcher,
                boundary_sha256=args.maka_boundary_sha256)
        except CertificationRejected as error:
            parser.exit(1, f"{error}\n")
        print(json.dumps({"passed": quality.isolation_passed,
                          "quality_digest": quality.ballot_digest}, sort_keys=True))
        return 0 if quality.isolation_passed else 1
    target = args.private_run_root / "targets" / "repo-alpha"
    try:
        selected_ids = {}
        for item in args.route_id:
            provider, separator, route_id = item.partition("=")
            if (separator != "=" or provider not in PROVIDERS or not route_id
                    or provider in selected_ids):
                raise CertificationRejected("machine route selection is invalid")
            selected_ids[provider] = route_id
        receipt = certification_run(target, args.private_run_root,
                                    fake_route=args.fake_route,
                                    allow_paid=args.allow_paid,
                                    approval_reference=args.owner_approval_ref,
                                    observation_date=args.observation_date,
                                    image_digest=args.image_digest,
                                    cli_digests={provider: getattr(args, f"{provider}_cli_sha256")
                                                 for provider in PROVIDERS},
                                    manifest_path=args.machine_routes,
                                    policy_path=args.machine_policy,
                                    route_ids=selected_ids,
                                    approval_packet_path=args.owner_approval_packet)
    except CertificationRejected as error:
        parser.exit(1, f"{error}\n")
    decision = evaluate_private_probe(receipt)
    print(json.dumps({"passed": decision.passed, "reasons": decision.reasons},
                     sort_keys=True))
    return 0 if decision.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
