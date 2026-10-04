"""Private container certification admission and no-spend receipt checks."""
from __future__ import annotations

import importlib
import importlib.util
import datetime
import hashlib
import json
import os
import stat
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.lib.fanout.container_source import host_runtime_identity
from shared.lib.fanout.broker import Broker, BrokerPermit, BrokerRejected
from shared.lib.fanout.container_broker import (
    OutboundRequestIdentity, PermitLedger, RelayRequest, dispatch_once,
    parse_request_frame,
)
from shared.lib.fanout.container_transfer import TransferIdentity, snapshot_manifest
from shared.lib.fanout.container_transfer_transport import CandidateExportReceipt
from shared.lib.fanout.controller import (
    LifecycleCapability, create_lifecycle_controller, read_evidence,
    resume_lifecycle_controller, write_evidence,
)
from shared.lib.fanout.repo import capture_repository_baseline
from shared.lib.fanout.container_routes import read_machine_routes


@pytest.fixture
def company_manifest(tmp_path):
    profiles = [
        {"route_id": "claude-company", "provider": "claude",
         "auth_family": "vertex-adc", "api_family": "anthropic-vertex-messages",
         "origin": "https://europe-west1-aiplatform.googleapis.com",
         "project": "invented-vertex-project", "location": "europe-west1",
         "requested_model": "claude-opus-5-5", "wire_model": "claude-opus-5-5",
         "effective_model": "claude-opus-5-5", "credential_ref": "gcloud-adc",
         "max_data_classification": "protected",
         "execution_classes": ["repo-write"], "transform_version": 1},
        {"route_id": "codex-company", "provider": "codex",
         "auth_family": "openai-keychain", "api_family": "openai-responses",
         "origin": "https://api.openai.com", "project": "invented-openai-project",
         "location": None, "requested_model": "gpt-6.1-sol", "wire_model": "gpt-6.1-sol",
         "effective_model": "gpt-6.1-sol", "credential_ref": "keychain:invented-account",
         "max_data_classification": "protected",
         "execution_classes": ["repo-write"], "transform_version": 1},
        {"route_id": "codex-alternate", "provider": "codex",
         "auth_family": "openai-keychain", "api_family": "openai-responses",
         "origin": "https://api.openai.com", "project": "invented-openai-project",
         "location": None, "requested_model": "gpt-6.1-sol", "wire_model": "gpt-6.1-sol",
         "effective_model": "gpt-6.1-sol", "credential_ref": "keychain:alternate-account",
         "max_data_classification": "protected",
         "execution_classes": ["repo-write"], "transform_version": 1},
        {"route_id": "agy-company", "provider": "agy",
         "auth_family": "vertex-adc", "api_family": "google-vertex-generate-content",
         "origin": "https://europe-west1-aiplatform.googleapis.com",
         "project": "invented-vertex-project", "location": "europe-west1",
         "requested_model": "gemini-3.8-flash-high", "wire_model": "gemini-3.8-flash",
         "effective_model": "gemini-3.8-flash", "credential_ref": "gcloud-adc",
         "max_data_classification": "protected",
         "execution_classes": ["repo-write"], "transform_version": 1},
    ]
    policy = {"schema": "fanout-machine-policy-v1", "machine_id": "invented-machine",
              "routes": [{k: v for k, v in route.items() if k != "credential_ref"}
                         for route in profiles]}
    policy_path = tmp_path / "policy.json"
    policy_path.write_bytes((json.dumps(policy, sort_keys=True,
                                        separators=(",", ":")) + "\n").encode())
    policy_path.chmod(0o600)
    manifest = {"schema": "fanout-machine-routes-v1", "machine_id": "invented-machine",
                "policy_digest": hashlib.sha256(policy_path.read_bytes()).hexdigest(),
                "routes": profiles}
    path = tmp_path / "routes.json"
    path.write_bytes((json.dumps(manifest, sort_keys=True,
                                 separators=(",", ":")) + "\n").encode())
    path.chmod(0o600)
    return path, policy_path, read_machine_routes(path, policy_path)


def _requested_company_routes():
    return {"claude": "claude-company", "codex": "codex-company",
            "agy": "agy-company"}


def test_missing_manifest_never_falls_back_to_direct_key_envs(certifier, monkeypatch):
    for name in ("FANOUT_OPENAI_API_KEY", "FANOUT_ANTHROPIC_API_KEY",
                 "FANOUT_GEMINI_API_KEY"):
        monkeypatch.setenv(name, "ignored-secret")
    with pytest.raises(certifier.CertificationRejected, match="machine route"):
        certifier._admit_route_set(None, None, _requested_company_routes())


def test_direct_key_envs_cannot_override_company_routes(
    certifier, company_manifest, monkeypatch,
):
    monkeypatch.setenv("FANOUT_ANTHROPIC_API_KEY", "ignored-secret")
    path, policy_path, manifest = company_manifest
    actual, selected = certifier._admit_route_set(
        path, policy_path, _requested_company_routes())
    assert actual == manifest
    assert selected["claude"].profile.auth_family == "vertex-adc"
    assert selected["claude"].profile.credential_ref == "gcloud-adc"


def test_route_selection_rejects_swapped_or_disallowed_profile(
    certifier, company_manifest,
):
    path, policy_path, _ = company_manifest
    requested = _requested_company_routes()
    requested["claude"] = requested["codex"]
    with pytest.raises(certifier.CertificationRejected, match="machine route"):
        certifier._admit_route_set(path, policy_path, requested)


@pytest.fixture
def certifier(monkeypatch):
    if importlib.util.find_spec("scripts.fanout_container_certify") is None:
        class MissingCertifier:
            def __getattr__(self, _name):
                pytest.fail("private container certifier API is missing")
        return MissingCertifier()
    module = importlib.import_module("scripts.fanout_container_certify")

    class ObservationDate(datetime.date):
        @classmethod
        def today(cls):
            return cls(2026, 9, 30)

    monkeypatch.setattr(module, "datetime", SimpleNamespace(date=ObservationDate))
    return module


@pytest.fixture
def complete_receipt(certifier, company_manifest):
    models = {"claude": "claude-opus-5-5", "codex": "gpt-6.1-sol",
              "agy": "gemini-3.8-flash-high"}
    source = certifier.runtime_source_digest(certifier._SOURCE_ROOT)
    runtime = asdict(host_runtime_identity())
    runtime_digest = hashlib.sha256(certifier.canonical_json(runtime)).hexdigest()
    image = "fanout-seat@sha256:" + "3" * 64
    manifest_path, policy_path, manifest = company_manifest
    _, selected = certifier._admit_route_set(
        manifest_path, policy_path, _requested_company_routes())
    machine_routes = {}
    providers = {}
    for provider, model in models.items():
        profile = certifier.ProviderRegistry.default(version_probe=lambda _: "unused").select(
            provider, "repo-write", "standard")
        selected_route = selected[provider]
        upstream = selected_route.profile
        effective_path = (
            f"/v1/projects/{upstream.project}/locations/{upstream.location}/"
            f"publishers/anthropic/models/{upstream.effective_model}:streamRawPredict"
            if provider == "claude" else
            f"/v1/projects/{upstream.project}/locations/{upstream.location}/"
            f"publishers/google/models/{upstream.effective_model}:streamGenerateContent?alt=sse"
            if provider == "agy" else "/v1/responses")
        machine_routes[provider] = {
            "route_id": upstream.route_id, "digest": selected_route.digest,
            "origin": upstream.origin, "path": effective_path,
            "model": upstream.effective_model,
            "auth_family": upstream.auth_family}
        cells = {}
        for route, epoch in (("initial", 1), ("resume", 2)):
            permit_id = hashlib.sha256(f"permit-{provider}-{route}".encode()).hexdigest()
            container_id = hashlib.sha256(f"container-{provider}-{route}".encode()).hexdigest()
            initial_container_id = hashlib.sha256(f"container-{provider}-initial".encode()).hexdigest()
            request_id = f"{provider}-{route}"
            wire_model = "gemini-3.8-flash" if provider == "agy" else model
            path = (f"/v1beta/models/{wire_model}:streamGenerateContent?alt=sse"
                    if provider == "agy" else
                    "/v1/messages" if provider == "claude" else "/v1/responses")
            identity = {"method": "POST", "path": path, "body_sha256": "e" * 64,
                        "effective_output_tokens": 32, "wire_model": wire_model,
                        "upstream_route_digest": selected_route.digest,
                        "upstream_origin": upstream.origin,
                        "upstream_path": effective_path,
                        "upstream_body_sha256": "a" * 64,
                        "upstream_model": upstream.effective_model}
            cells[route] = {
                "passed": True, "route": route, "source_sha256": source,
                "route_id": upstream.route_id,
                "machine_route_digest": selected_route.digest,
                "manifest_digest": manifest.manifest_digest,
                "policy_digest": manifest.policy_digest,
                "credential_source_fingerprint": "9" * 64,
                "effective_origin": upstream.origin, "effective_path": effective_path,
                "effective_model": upstream.effective_model,
                "host_runtime_sha256": runtime_digest,
                "vm_probe_sha256": "2" * 64,
                "image_digest": image,
                "cli_digest": "4" * 64, "profile_digest": profile.digest,
                "container_id": container_id, "container_inspect_sha256": "7" * 64,
                "session_volume": f"fanout-session-{provider}",
                "session_id": f"session-{provider}",
                "requested_model": model, "observed_model": model,
                "outbound_wire_model": wire_model,
                "broker_request_count": 1,
                "broker_attempt_count": 1, "broker_auxiliary_attempt_count": 0,
                "broker_auxiliary_refusal_count": 0,
                "ledger_requests": [{"request_id": request_id, "permit_id": permit_id,
                                     "epoch": epoch, "identity": identity,
                                     "state": "settled", "response_digest": "8" * 64}],
                "broker_dispositions": [{"request_id": request_id,
                                         "state": "settled", "response_sha256": "8" * 64}],
                "permit_id": permit_id,
                "permit_epoch": epoch, "permit_fenced": True,
                "approval_evidence_digest": "6" * 64,
                "prior_seat_stopped": True, "seat_stopped": True,
                "cold_controller_reopened": route == "resume",
                "recovered_prior_container_id": initial_container_id if route == "resume" else None,
                "socket_attestation_sha256": "f" * 64,
                "input_receipt_digest": "1" * 64,
                "input_manifest_sha256": "2" * 64,
                "candidate_digest": "3" * 64,
                "candidate_edited_paths": ["README.md"],
                "candidate_export_manifest_sha256": "4" * 64,
                "candidate_export_evidence_digest": "5" * 64,
                "isolation": {
                    name: {"read_denied": True, "write_denied": True}
                    for name in ("caller", "sibling", "git_objects", "credentials",
                                 "docker_socket", "host_home", "vm_network")
                },
                "own_workspace_write": True, "answer_sha256": "b" * 64,
                "stdout_sha256": "c" * 64, "stderr_sha256": "d" * 64,
                "elapsed_seconds": 0.1,
            }
        providers[provider] = cells
    return {"schema_version": "fanout-container-private-probe-v1",
            "mode": "live", "source_sha256": source,
            "manifest_path": str(manifest_path), "policy_path": str(policy_path),
            "manifest_digest": manifest.manifest_digest,
            "policy_digest": manifest.policy_digest,
            "machine_routes": machine_routes,
            "host_runtime": runtime, "image_digest": image,
            "cli_digests": {provider: "4" * 64 for provider in models},
            "approval_evidence_digest": "6" * 64,
            "approval_reference": "owner-ticket-123",
            "observation_date": "2026-09-30",
            "spend_scope": [f"{provider}:{route}" for provider in models
                            for route in ("initial", "resume")],
            "spend_ceiling": {"max_model_calls": 24,
                              "max_request_bytes_per_call": 1048576,
                              "max_response_bytes_per_call": 8388608,
                              "max_output_tokens_per_call": 8192},
            "target_ids": ["repo-alpha", "repo-beta"], "providers": providers}


def test_caller_root_refused_without_writing_it(certifier, tmp_path):
    caller = tmp_path / "caller"
    caller.mkdir()
    sentinel = caller / "keep.txt"
    sentinel.write_text("unchanged")
    private = tmp_path / "private-run"

    with pytest.raises(certifier.CertificationRejected, match="synthetic target"):
        certifier.certification_run(caller, private)

    assert sentinel.read_text() == "unchanged"
    assert not private.exists()


def test_missing_exact_resume_blocks_six_cell_decision(certifier, complete_receipt):
    complete_receipt["providers"]["agy"].pop("resume")
    decision = certifier.evaluate_private_probe(complete_receipt)
    assert not decision.passed
    assert any("agy resume" in reason for reason in decision.reasons)


@pytest.mark.parametrize("change", (
    "route_digest", "policy_digest", "credential_source", "effective_origin",
    "effective_model", "effective_body", "uncertain"))
def test_mixed_or_missing_route_evidence_is_not_a_certificate(
    certifier, complete_receipt, change,
):
    cell = complete_receipt["providers"]["agy"]["resume"]
    if change == "route_digest":
        cell["machine_route_digest"] = "0" * 64
    elif change == "policy_digest":
        cell["policy_digest"] = "0" * 64
    elif change == "credential_source":
        cell.pop("credential_source_fingerprint")
    elif change == "effective_origin":
        cell["effective_origin"] = "https://other.invalid"
    elif change == "effective_model":
        cell["effective_model"] = "other-model"
    elif change == "effective_body":
        cell["ledger_requests"][0]["identity"]["upstream_body_sha256"] = "invalid"
    else:
        cell["ledger_requests"][0]["state"] = "uncertain"
    assert not certifier.evaluate_private_probe(complete_receipt).passed


def test_complete_live_matrix_allows_missing_terminal_model_with_wire_proof(
    certifier, complete_receipt,
):
    complete_receipt["providers"]["codex"]["resume"]["observed_model"] = None
    complete_receipt["providers"]["agy"]["initial"]["observed_model"] = None
    assert certifier.evaluate_private_probe(complete_receipt).passed
    complete_receipt["providers"]["codex"]["resume"]["observed_model"] = "other-model"
    assert not certifier.evaluate_private_probe(complete_receipt).passed


@pytest.mark.parametrize("change", ("missing", "swapped", "wrong_wire", "extra_attempt",
                                    "auxiliary_marker", "uncertain", "malformed_permit"))
def test_ledger_identity_and_finite_attempts_bind_every_main_send(
    certifier, complete_receipt, change,
):
    cell = complete_receipt["providers"]["agy"]["initial"]
    if change == "missing":
        cell["ledger_requests"][0]["identity"].pop("body_sha256")
    elif change == "swapped":
        cell["ledger_requests"][0]["request_id"] = "other-request"
    elif change == "wrong_wire":
        cell["ledger_requests"][0]["identity"]["wire_model"] = "gemini-3.8-flash-high"
    elif change == "extra_attempt":
        cell["broker_attempt_count"] = 2
    elif change == "auxiliary_marker":
        cell["broker_attempt_count"] = 3
        cell["broker_auxiliary_attempt_count"] = 2
        cell["broker_auxiliary_refusal_count"] = 2
    elif change == "malformed_permit":
        cell["permit_id"] = []
    else:
        cell["ledger_requests"][0]["state"] = "uncertain"
    assert not certifier.evaluate_private_probe(complete_receipt).passed


def test_stale_source_or_host_runtime_refuses_receipt(certifier, complete_receipt):
    complete_receipt["source_sha256"] = "0" * 64
    for cells in complete_receipt["providers"].values():
        for cell in cells.values():
            cell["source_sha256"] = "0" * 64
    assert not certifier.evaluate_private_probe(complete_receipt).passed
    complete_receipt["source_sha256"] = certifier.runtime_source_digest(certifier._SOURCE_ROOT)
    for cells in complete_receipt["providers"].values():
        for cell in cells.values():
            cell["source_sha256"] = complete_receipt["source_sha256"]
    complete_receipt["host_runtime"]["openssl_version"] = "stale"
    assert not certifier.evaluate_private_probe(complete_receipt).passed


@pytest.mark.parametrize("changed", ("approval_reference", "observation_date",
                                      "spend_scope", "cell_digest"))
def test_live_receipt_binds_owner_approval_scope(certifier, complete_receipt, changed):
    if changed == "cell_digest":
        complete_receipt["providers"]["codex"]["resume"]["approval_evidence_digest"] = "7" * 64
    elif changed == "spend_scope":
        complete_receipt[changed].pop()
    else:
        complete_receipt[changed] = None
    assert not certifier.evaluate_private_probe(complete_receipt).passed


def test_forged_candidate_export_refuses_receipt(certifier, complete_receipt):
    complete_receipt["providers"]["claude"]["initial"]["candidate_edited_paths"] = []
    assert not certifier.evaluate_private_probe(complete_receipt).passed


def test_live_admission_needs_paid_gate_before_root_or_child(certifier, tmp_path, monkeypatch):
    private = tmp_path / "private"
    called = []
    monkeypatch.setattr(certifier.subprocess, "run", lambda *a, **kw: called.append(a))
    with pytest.raises(certifier.CertificationRejected, match="allow-paid"):
        certifier.certification_run(private / "targets" / "repo-alpha", private)
    assert not called and not private.exists()


def test_live_admission_rejects_direct_keys_without_machine_routes(
    certifier, tmp_path, monkeypatch,
):
    private = tmp_path / "private"
    for name in ("FANOUT_OPENAI_API_KEY", "FANOUT_ANTHROPIC_API_KEY",
                 "FANOUT_GEMINI_API_KEY"):
        monkeypatch.setenv(name, "synthetic-private-value")
    with pytest.raises(certifier.CertificationRejected, match="machine route"):
        certifier.certification_run(
            private / "targets" / "repo-alpha", private, allow_paid=True,
            approval_reference="owner-ticket-123", observation_date="2026-09-30",
            image_digest="fanout-seat@sha256:" + "3" * 64,
            cli_digests={name: "4" * 64 for name in ("claude", "codex", "agy")},
        )
    assert not private.exists()
    assert all(name not in os.environ for name in (
        "FANOUT_OPENAI_API_KEY", "FANOUT_ANTHROPIC_API_KEY", "FANOUT_GEMINI_API_KEY"))


def test_legacy_key_envs_are_removed_before_child_boundary(certifier, monkeypatch):
    names = ("FANOUT_OPENAI_API_KEY", "FANOUT_ANTHROPIC_API_KEY",
             "FANOUT_GEMINI_API_KEY")
    for name in names:
        monkeypatch.setenv(name, "synthetic-private-value")
    certifier._discard_legacy_keys()
    assert all(name not in os.environ for name in names)
    child = subprocess.run((sys.executable, "-c",
                            "import json,os,sys;print(json.dumps([k for k in sys.argv[1:] if k in os.environ]))",
                            *names),
                           capture_output=True, check=True, text=True)
    assert __import__("json").loads(child.stdout) == []


def test_live_cell_reopens_authenticated_ledger_requests(certifier, tmp_path):
    controller = create_lifecycle_controller(tmp_path / "controller")
    ledger = PermitLedger(controller.root / "locks" / "spend", controller)
    permit = BrokerPermit("codex", "run", "task", "repo-alpha", "seat", "initial",
                          "gpt-6.1-sol", "disposable-placeholder", 1, 100_000,
                          100_000, 1024)
    permit_id = ledger.register(permit, 1)
    identity = OutboundRequestIdentity("POST", "/v1/responses", "1" * 64, 32,
                                       "gpt-6.1-sol")
    ledger.record_attempt(permit_id, 1)
    ledger.reserve(permit_id, 1, "request-1", identity)
    ledger.settle("request-1", "2" * 64)
    ledger.fence_epoch(permit_id, 1)

    records, attempts, auxiliary, fenced = certifier._ledger_snapshot(
        controller, permit_id, 1)
    assert records == ({"request_id": "request-1", "permit_id": permit_id,
                        "epoch": 1, "identity": asdict(identity), "state": "settled",
                        "response_digest": "2" * 64},)
    assert (attempts, auxiliary, fenced) == (1, 0, True)


def test_candidate_proof_reconstructs_edited_export_bytes(certifier, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# repo-alpha\n")
    subprocess.run(("git", "init", "-q", str(repo)), check=True, capture_output=True)
    subprocess.run(("git", "-C", str(repo), "add", "README.md"), check=True,
                   capture_output=True)
    subprocess.run(("git", "-C", str(repo), "-c", "user.name=Probe",
                    "-c", "user.email=probe@example.invalid", "-c", "commit.gpgsign=false",
                    "commit", "-q", "-m", "baseline"), check=True,
                   capture_output=True)
    baseline = capture_repository_baseline(repo)
    controller = create_lifecycle_controller(tmp_path / "controller")
    export = tmp_path / "candidate-export"
    export.mkdir(mode=0o700)
    (export / "README.md").write_text("# repo-alpha\ncertified synthetic edit\n")
    identity = TransferIdentity("run", "task", "repo-alpha", "seat", "1" * 64)
    manifest = snapshot_manifest(export, identity)
    manifest_digest = hashlib.sha256(certifier.canonical_json(asdict(manifest))).hexdigest()
    proof = {"identity": asdict(identity), "turn_digest": "2" * 64,
             "manifest_sha256": manifest_digest, "root": str(export),
             "container_id": "3" * 64, "container_inspect_sha256": "4" * 64,
             "workspace_volume": "fanout-workspace-synthetic",
             "session_volume": "fanout-session-synthetic"}
    name, digest = write_evidence(controller, "container-candidate-export", proof)
    receipt = CandidateExportReceipt(identity, "2" * 64, manifest_digest, export,
                                     "3" * 64, "4" * 64,
                                     "fanout-workspace-synthetic", "fanout-session-synthetic",
                                     name, digest)
    candidate = certifier._checked_candidate(
        controller, baseline, receipt, b"certified synthetic edit\n")
    assert [entry.path for entry in candidate.entries] == ["README.md"]
    assert candidate.entries[0].data == b"# repo-alpha\ncertified synthetic edit\n"
    (export / "README.md").write_text("forged after receipt\n")
    with pytest.raises(certifier.CertificationRejected, match="candidate"):
        certifier._checked_candidate(
            controller, baseline, receipt, b"certified synthetic edit\n")


def _synthetic_live_admission(certifier, company_manifest):
    path, policy_path, _ = company_manifest
    private = path.parent / "private"
    packet_path = path.parent / "owner-approval.json"
    _invented_approval_packet(certifier, company_manifest, private, packet_path)
    return certifier._admit_live(
        allow_paid=True, approval_reference="owner-ticket-123",
        observation_date="2026-09-30",
        image_digest="fanout-seat@sha256:" + "3" * 64,
        cli_digests={provider: "4" * 64 for provider in certifier.PROVIDERS},
        manifest_path=path, policy_path=policy_path,
        route_ids=_requested_company_routes(), private_run_root=private,
        approval_packet_path=packet_path)


def _invented_approval_packet(certifier, company_manifest, private, packet_path):
    path, policy_path, manifest = company_manifest
    _, selected = certifier._admit_route_set(
        path, policy_path, _requested_company_routes())
    payload = {
        "schema": "fanout-container-owner-approval-v1",
        "reference": "owner-ticket-123",
        "observation_date": "2026-09-30",
        "private_run_root": str(private),
        "source_sha256": certifier.runtime_source_digest(certifier._SOURCE_ROOT),
        "machine_id": manifest.machine_id,
        "manifest_digest": manifest.manifest_digest,
        "policy_digest": manifest.policy_digest,
        "route_digests": {provider: selected[provider].digest
                          for provider in ("claude", "codex", "agy")},
        "spend_scope": [f"{provider}:{route}"
                        for provider in ("claude", "codex", "agy")
                        for route in ("initial", "resume")],
        "spend_ceiling": {"max_model_calls": 24,
                          "max_request_bytes_per_call": 1048576,
                          "max_response_bytes_per_call": 8388608,
                          "max_output_tokens_per_call": 8192},
    }
    certifier._write_private(packet_path, certifier.canonical_json(payload))
    return payload


@pytest.mark.parametrize("change", ("not_supplied", "missing", "scope", "route", "reused_root",
                                    "stale", "reference", "machine", "manifest", "policy",
                                    "ceiling", "source", "old_six_call_packet",
                                    "world_readable", "symlink", "noncanonical"))
def test_live_admission_requires_current_owner_packet_for_exact_run(
    certifier, company_manifest, tmp_path, change,
):
    path, policy_path, _ = company_manifest
    private = tmp_path / "private"
    packet_path = tmp_path / "owner-approval.json"
    packet = _invented_approval_packet(certifier, company_manifest, private, packet_path)
    if change == "not_supplied":
        packet_path = None
    elif change == "missing":
        packet_path.unlink()
    elif change == "scope":
        packet["spend_scope"] = packet["spend_scope"][:-1]
    elif change == "route":
        packet["route_digests"]["codex"] = "0" * 64
    elif change == "reused_root":
        packet["private_run_root"] = str(tmp_path / "previous-run")
    elif change == "stale":
        packet["observation_date"] = "2026-09-29"
    elif change == "reference":
        packet["reference"] = "another-owner-ticket"
    elif change == "machine":
        packet["machine_id"] = "another-machine"
    elif change in ("manifest", "policy"):
        packet[f"{change}_digest"] = "0" * 64
    elif change == "ceiling":
        packet["spend_ceiling"]["max_model_calls"] = 7
    elif change == "source":
        packet["source_sha256"] = "0" * 64
    elif change == "old_six_call_packet":
        packet.pop("source_sha256")
        packet["spend_ceiling"]["max_model_calls"] = 6
    elif change == "world_readable":
        packet_path.chmod(0o644)
    elif change == "symlink":
        target = tmp_path / "underlying-packet.json"
        packet_path.rename(target)
        packet_path.symlink_to(target)
    elif change == "noncanonical":
        packet_path.write_bytes(json.dumps(packet).encode())
    if change not in ("not_supplied", "missing", "world_readable", "symlink",
                      "noncanonical"):
        packet_path.write_bytes(certifier.canonical_json(packet))
    with pytest.raises(certifier.CertificationRejected, match="owner approval packet"):
        certifier._admit_live(
            allow_paid=True, approval_reference="owner-ticket-123",
            observation_date="2026-09-30",
            image_digest="fanout-seat@sha256:" + "3" * 64,
            cli_digests={provider: "4" * 64 for provider in certifier.PROVIDERS},
            manifest_path=path, policy_path=policy_path,
            route_ids=_requested_company_routes(), private_run_root=private,
            approval_packet_path=packet_path,
        )
    assert not private.exists()


@pytest.mark.parametrize("observed", ("2026-09-29", "2026-10-01"))
def test_live_admission_refuses_noncurrent_observation_date(certifier, tmp_path, observed):
    with pytest.raises(certifier.CertificationRejected, match="observation date"):
        certifier._admit_live(
            allow_paid=True, approval_reference="owner-ticket-123",
            observation_date=observed, image_digest=None, cli_digests=None,
            manifest_path=None, policy_path=None, route_ids=None,
            private_run_root=tmp_path / "private", approval_packet_path=None)


def test_owner_approval_packet_must_be_separately_held(certifier, company_manifest):
    admission = _synthetic_live_admission(certifier, company_manifest)
    packet = certifier._owner_packet(
        admission.private_run_root, admission.approval_reference,
        admission.observation_date, admission.manifest, admission.selected)
    for path in (admission.private_run_root / "approval.json",
                 certifier._SOURCE_ROOT / "owner-approval.json"):
        with pytest.raises(certifier.CertificationRejected, match="separately held"):
            certifier._check_owner_packet(path, packet)


def test_live_admission_accepts_matching_invented_owner_packet(
    certifier, company_manifest, tmp_path,
):
    path, policy_path, _ = company_manifest
    private = tmp_path / "private"
    packet_path = tmp_path / "owner-approval.json"
    _invented_approval_packet(certifier, company_manifest, private, packet_path)
    admitted = certifier._admit_live(
        allow_paid=True, approval_reference="owner-ticket-123",
        observation_date="2026-09-30",
        image_digest="fanout-seat@sha256:" + "3" * 64,
        cli_digests={provider: "4" * 64 for provider in certifier.PROVIDERS},
        manifest_path=path, policy_path=policy_path,
        route_ids=_requested_company_routes(), private_run_root=private,
        approval_packet_path=packet_path,
    )
    assert admitted.approval_packet_path == packet_path
    assert admitted.approval_packet_digest == hashlib.sha256(packet_path.read_bytes()).hexdigest()
    assert not private.exists()


def _fake_host_credential_source(certifier, monkeypatch):
    class FakeCredentialSource:
        def __init__(self, _route):
            self.fingerprint = None

        def load(self):
            self.fingerprint = "9" * 64
            return "synthetic-private-value"

    monkeypatch.setattr(certifier, "HostCredentialSource", FakeCredentialSource)


def test_live_cell_run_scope_avoids_retained_foreign_inventory_without_weakening_guards(
    certifier, tmp_path, monkeypatch, company_manifest,
):
    admission = _synthetic_live_admission(certifier, company_manifest)
    _fake_host_credential_source(certifier, monkeypatch)
    controller = create_lifecycle_controller(tmp_path / "scope-controller")
    ledger = PermitLedger(controller.root / "locks" / "spend", controller)
    target = tmp_path / "target"
    target.mkdir()
    (target / "README.md").write_bytes(b"synthetic target\n")
    captured = {}
    primary = certifier.ProviderRequestError("synthetic prelaunch refusal")

    def no_seat(_controller, spec, *, broker, **_kwargs):
        certifier.ContainerLauncher._validate_spec(spec)
        permit = broker.permit
        assert (permit.run_id, permit.task_id, permit.target_id, permit.seat_id) == (
            spec.run_id, spec.task_id, spec.target_id, spec.seat_id)
        captured[spec.run_id, permit.provider] = spec
        raise primary

    monkeypatch.setattr(certifier.PinnedDockerTransport, "start_live", no_seat)
    runs = ("private-retained-prefix-a", "private-retained-prefix-b", "r" * 128)
    for run_id in runs:
        for provider in certifier.PROVIDERS:
            with pytest.raises(certifier.ProviderRequestError) as caught:
                certifier._live_cell(
                    controller, ledger, target, target, tmp_path, tmp_path, None,
                    run_id, provider, "initial", admission, None, "6" * 64)
            assert caught.value is primary

    current = captured[runs[1], "claude"]
    old_volumes = certifier.private_volume_names(
        "private-retained-prefix-old", "synthetic-task", "repo-alpha", "seat-claude")
    old = replace(current, run_id="private-retained-prefix-old", seat_id="seat-claude",
                  socket_path="/run/fanout/seat-claude.sock",
                  workspace_volume=old_volumes[0], session_volume=old_volumes[1])
    foreign = {
        "Id": "a" * 64, "Name": "/retained-exited-seat",
        "State": {"Running": False, "Status": "exited"},
        "Config": {"Labels": certifier.ContainerLauncher._labels(old)},
        "HostConfig": {"Mounts": [
            {"Type": "volume", "Source": old.workspace_volume, "Target": "/workspace"},
            {"Type": "volume", "Source": old.session_volume, "Target": "/session"},
            {"Type": "bind", "Source": old.socket_path, "Target": "/run/fanout/broker.sock"},
        ]},
        "Mounts": [
            {"Type": "volume", "Name": old.workspace_volume, "Destination": "/workspace",
             "Source": f"/var/lib/docker/volumes/{old.workspace_volume}/_data"},
            {"Type": "volume", "Name": old.session_volume, "Destination": "/session",
             "Source": f"/var/lib/docker/volumes/{old.session_volume}/_data"},
            {"Type": "bind", "Source": old.socket_path, "Destination": "/run/fanout/broker.sock"},
        ],
    }

    def docker(argv, stdin):
        assert stdin is None
        operation = argv[3:]
        if operation == ("container", "ls", "-a", "--no-trunc", "--format", "{{.ID}}"):
            payload = ("a" * 64 + "\n").encode()
        else:
            assert operation == ("inspect", "--type", "container", "a" * 64)
            payload = json.dumps([foreign]).encode()
        return subprocess.CompletedProcess(argv, 0, payload, b"")

    launcher = certifier.ContainerLauncher(docker_runner=docker)

    def inventory(spec):
        sources = tuple(f"/var/lib/docker/volumes/{name}/_data" for name in (
            spec.workspace_volume, spec.session_volume))
        launcher._inventory(spec, sources, fence_owned=False)

    generic_volumes = certifier.private_volume_names(
        current.run_id, current.task_id, current.target_id, "seat-claude")
    generic = replace(current, seat_id="seat-claude", socket_path=old.socket_path,
                      workspace_volume=generic_volumes[0], session_volume=generic_volumes[1])
    for spec in (generic, replace(generic, seat_id=current.seat_id),
                 replace(generic, socket_path=current.socket_path)):
        with pytest.raises(certifier.ProviderRequestError, match="foreign run resource conflict"):
            inventory(spec)
    inventory(current)
    for field in ("seat_id", "socket_path", "workspace_volume", "session_volume"):
        label = {"seat_id": "seat", "socket_path": "socket_path"}.get(field, field)
        foreign["Config"]["Labels"]["io.khenrix.fanout." + label] = getattr(current, field)
        with pytest.raises(certifier.ProviderRequestError, match="foreign run resource conflict"):
            inventory(current)
        foreign["Config"]["Labels"] = certifier.ContainerLauncher._labels(old)
    assert len({spec.seat_id for spec in captured.values()}) == 9
    assert len({spec.socket_path for spec in captured.values()}) == 9
    assert len({spec.workspace_volume for spec in captured.values()}) == 9
    assert len({spec.session_volume for spec in captured.values()}) == 9
    assert all(len(spec.socket_path.encode("ascii")) < 108 for spec in captured.values())


def test_live_cell_failure_after_registration_before_transport_revokes_exact_permit(
    certifier, tmp_path, monkeypatch, company_manifest,
):
    admission = _synthetic_live_admission(certifier, company_manifest)
    _fake_host_credential_source(certifier, monkeypatch)
    controller = create_lifecycle_controller(tmp_path / "pretransport-controller")
    ledger = PermitLedger(controller.root / "locks" / "spend", controller)
    target = tmp_path / "target"
    target.mkdir()
    (target / "README.md").write_bytes(b"synthetic target\n")
    primary = certifier.ProviderRequestError("synthetic broker construction failed")

    def no_upstream(*_args, **_kwargs):
        raise primary

    monkeypatch.setattr(certifier, "HostUpstream", no_upstream)
    with pytest.raises(certifier.ProviderRequestError) as caught:
        certifier._live_cell(
            controller, ledger, target, target, tmp_path, tmp_path, None,
            "private-pretransport", "claude", "initial", admission, None, "6" * 64)
    assert caught.value is primary
    cold = resume_lifecycle_controller(controller.root, controller.capability)
    cold_ledger = PermitLedger(cold.root / "locks" / "spend", cold)
    with cold_ledger._locked() as durable:
        assert len(durable["permits"]) == 1
        permit_id, entry = next(iter(durable["permits"].items()))
        assert entry["fenced"] is True
        assert durable["requests"] == {}
        assert durable["send_claims"] == {}
    with pytest.raises(BrokerRejected, match="fenced"):
        cold_ledger.record_attempt(permit_id, 1)


@pytest.mark.parametrize("provider", ("claude", "codex", "agy"))
@pytest.mark.parametrize("route", ("initial", "resume"))
def test_live_cell_normalizes_output_before_effective_identity_without_sending(
    certifier, tmp_path, monkeypatch, company_manifest, provider, route,
):
    admission = _synthetic_live_admission(certifier, company_manifest)
    _fake_host_credential_source(certifier, monkeypatch)
    controller = create_lifecycle_controller(tmp_path / "output-controller")
    ledger = PermitLedger(controller.root / "locks" / "spend", controller)
    target = tmp_path / "target"
    target.mkdir()
    (target / "README.md").write_bytes(b"synthetic target\n")
    selected = admission.selected[provider]
    run_id = "private-output-policy"
    if route == "resume":
        seat = certifier._certification_seat_id(run_id, provider)
        _, volume = certifier.private_volume_names(
            run_id, "synthetic-task", "repo-alpha", seat)
        store = certifier.SessionStore(
            controller, run_id, "synthetic-task", "repo-alpha", seat, volume)
        store.reserve(provider, "7" * 64, initial=True,
                      upstream_route_digest=selected.digest)
        store.settle(provider, "synthetic-output-session", "7" * 64, initial=True,
                     upstream_route_digest=selected.digest)
    primary = certifier.ProviderRequestError("synthetic preview boundary reached")
    identities, previews, sends = [], [], []

    def no_send(*_args, **_kwargs):
        sends.append(True)
        raise AssertionError("pure output-policy test reached a sender")

    monkeypatch.setattr(certifier.HostUpstream, "__call__", no_send)

    def inspect_request(_controller, _spec, *, broker, permit_id, epoch, recover,
                        **_kwargs):
        assert epoch == (1 if route == "initial" else 2)
        assert recover is (route == "resume")
        assert broker.permit.max_requests == 4
        assert broker.permit.max_request_bytes == 1048576
        assert broker.permit.max_response_bytes == 8388608
        assert broker.permit.max_output_tokens == 8192
        assert broker.permit.response_deadline == 300
        auth = {"claude": "x-api-key", "codex": "x-fanout-seat",
                "agy": "x-goog-api-key"}[provider]
        headers = {"content-type": "application/json",
                   auth: broker.permit.placeholder_key}
        if provider == "agy":
            path = "/v1beta/models/gemini-3.8-flash:streamGenerateContent?alt=sse"
            document = {"contents": [], "generationConfig": {
                "maxOutputTokens": 65536, "thinkingConfig": {"thinkingBudget": 4096}}}
        elif provider == "claude":
            path = "/v1/messages"
            document = {"model": "claude-opus-5-5", "max_tokens": 128000,
                        "messages": [], "stream": True,
                        "thinking": {"type": "adaptive"},
                        "output_config": {"effort": "max"}}
        else:
            path = "/v1/responses"
            document = {"model": "gpt-6.1-sol", "input": [], "stream": True,
                        "reasoning": {"effort": "xhigh"}}

        original_preview = broker.preview_upstream

        def preview(*args):
            prepared = original_preview(*args)
            previews.append(prepared)
            return prepared

        def before_reserve(_id, _epoch, _request, identity):
            identities.append(identity)
            raise primary

        monkeypatch.setattr(broker, "preview_upstream", preview)
        monkeypatch.setattr(ledger, "reserve", before_reserve)
        request = RelayRequest(permit_id, epoch, "normalized-output", "POST", path,
                               headers, certifier.canonical_json(document))
        with pytest.raises(certifier.ProviderRequestError) as caught:
            dispatch_once(request, ledger, broker)
        assert caught.value is primary
        assert len(identities) == 1
        identity = identities[0]
        assert identity.effective_output_tokens == 8192
        assert identity.upstream_route_digest == selected.digest
        assert identity.upstream_model == selected.profile.effective_model
        assert identity.upstream_origin == selected.profile.origin
        effective = json.loads(previews[0].body)
        if provider == "agy":
            assert effective["generationConfig"] == {
                "maxOutputTokens": 8192, "thinkingConfig": {"thinkingBudget": 4096}}
            assert identity.wire_model == "gemini-3.8-flash"
        elif provider == "claude":
            assert effective["max_tokens"] == 8192
            assert effective["thinking"] == {"type": "adaptive"}
            assert effective["output_config"] == {"effort": "max"}
            assert effective["anthropic_version"] == "vertex-2023-10-16"
            assert "model" not in effective
            assert identity.wire_model == "claude-opus-5-5"
        else:
            assert effective["max_output_tokens"] == 8192
            assert effective["reasoning"] == {"effort": "xhigh"}
            assert identity.wire_model == "gpt-6.1-sol"

        invalid = (None, True, False, 0, -1, 1.5, "1024")
        invalid += (8193,) if provider == "codex" else ("missing",)
        for index, limit in enumerate(invalid):
            bad = json.loads(json.dumps(document))
            generation = bad.setdefault("generationConfig", {}) if provider == "agy" else bad
            field = {"claude": "max_tokens", "codex": "max_output_tokens",
                     "agy": "maxOutputTokens"}[provider]
            if limit == "missing":
                generation.pop(field)
            else:
                generation[field] = limit
            refused = replace(request, request_id=f"invalid-output-{index}",
                              body=certifier.canonical_json(bad))
            with pytest.raises(BrokerRejected):
                dispatch_once(refused, ledger, broker)
        with pytest.raises(BrokerRejected):
            dispatch_once(replace(request, body=b"{malformed"), ledger, broker)
        assert len(identities) == len(previews) == 1
        with pytest.raises(BrokerRejected):
            dispatch_once(replace(request, body=b" " * 1048577), ledger, broker)
        assert sends == []
        raise primary

    monkeypatch.setattr(certifier.PinnedDockerTransport, "start_live", inspect_request)
    previous = {"credential_source_fingerprint": "9" * 64} if route == "resume" else None
    with pytest.raises(certifier.ProviderRequestError) as caught:
        certifier._live_cell(
            controller, ledger, target, target, tmp_path, tmp_path, None,
            run_id, provider, route, admission, previous, "6" * 64)
    assert caught.value is primary
    assert len(identities) == 1
    with ledger._locked() as durable:
        assert durable["requests"] == durable["send_claims"] == {}
        assert all(entry["fenced"] is True for entry in durable["permits"].values())


def test_private_failure_sink_live_producer_authenticates_exact_reservation_bindings(
    certifier, tmp_path, monkeypatch, company_manifest,
):
    import test_fanout_container_upstream as upstream_tests
    admission = _synthetic_live_admission(certifier, company_manifest)
    _fake_host_credential_source(certifier, monkeypatch)
    controller = create_lifecycle_controller(tmp_path / "failure-controller")
    ledger = PermitLedger(controller.root / "locks" / "spend", controller)
    target = tmp_path / "target"; target.mkdir()
    (target / "README.md").write_bytes(b"synthetic target\n")
    response = upstream_tests.FakeResponse(status=403, chunks=(b'{"error":{"code":7,"message":"denied"}}',))
    made = []
    def connection(host, port, **kwargs):
        value = upstream_tests.FakeConnection(host, port, response=response, **kwargs)
        made.append(value); return value
    monkeypatch.setattr("http.client.HTTPSConnection", connection)
    primary = certifier.ProviderRequestError("offline transport boundary reached")
    def inspect_failure(_controller, _spec, *, broker, permit_id, epoch, **kwargs):
        request = RelayRequest(permit_id, epoch, "owner-failure", "POST", "/v1/messages",
                               {"x-api-key": "disposable-placeholder", "content-type": "application/json"},
                               b'{"model":"claude-opus-5-5","max_tokens":16,"messages":[],"stream":true}')
        ledger.record_attempt(permit_id, epoch)
        with pytest.raises(BrokerRejected): dispatch_once(request, ledger, broker)
        paths = [path for path in (controller.root / "receipts").glob("*.json")
                 if json.loads(path.read_bytes())["category"] == "container-upstream-failure"]
        assert len(paths) == 1, "actual live producer lost owner-private upstream failure"
        proof = read_evidence(controller, "container-upstream-failure", paths[0].name,
                              hashlib.sha256(paths[0].read_bytes()).hexdigest())
        assert proof["source_sha256"] == admission.source_sha256
        assert proof["approval_evidence_digest"] == "6" * 64
        assert proof["permit_id"] == permit_id and proof["permit_epoch"] == 1
        assert proof["provider"] == "claude" and proof["route"] == "initial"
        assert proof["http_status"] == 403 and proof["structured_error"] == {"code": 7, "message": "denied"}
        identity = ledger.request_records(permit_id, epoch)[0]["identity"]
        assert proof["normalized_body_sha256"] == identity["body_sha256"]
        assert proof["upstream_body_sha256"] == identity["upstream_body_sha256"] == hashlib.sha256(made[0].requests[0][2]).hexdigest()
        assert proof["upstream_route_digest"] == admission.selected["claude"].digest
        assert "request_id" not in proof and "credential" not in proof and "placeholder" not in proof
        event = {key: proof[key] for key in broker._send._failure_fields}
        for field, bad in (("upstream_route_digest", "0" * 64), ("upstream_body_sha256", "0" * 64),
                           ("normalized_body_sha256", "0" * 64), ("upstream_origin", "https://other.invalid"),
                           ("source_sha256", "0" * 64), ("controller_id", "other"), ("permit_id", "0" * 64),
                           ("structured_error", {"message": "x" * 4097}),
                           ("http_status", True), ("phase", "attacker_phase")):
            tampered = {**event, field: bad}
            with pytest.raises(BrokerRejected): broker._send._failure_sink(tampered)
        writes = []
        def fail_write(*args):
            writes.append(True)
            raise OSError("PRIVATE_WRITER_CANARY")
        monkeypatch.setattr(certifier, "write_evidence", fail_write)
        response.chunks = [b'{"error":{"code":7}}']
        ledger.record_attempt(permit_id, epoch)
        with pytest.raises(BrokerRejected) as refused:
            dispatch_once(replace(request, request_id="owner-writer-failure"), ledger, broker)
        assert "PRIVATE_WRITER_CANARY" not in str(refused.value) and writes == [True]
        assert len(list((controller.root / "receipts").glob("*.json"))) == 1
        raise primary
    monkeypatch.setattr(certifier.PinnedDockerTransport, "start_live", inspect_failure)
    with pytest.raises(certifier.ProviderRequestError) as caught:
        certifier._live_cell(controller, ledger, target, target, tmp_path, tmp_path, None,
                            "private-owner-failure", "claude", "initial", admission, None, "6" * 64)
    assert caught.value is primary and len(made) == 2 and all(connection.closed for connection in made)
    assert ledger.request_records(next(iter(ledger._load()["permits"])), 1)[0]["state"] == "uncertain"


@pytest.mark.parametrize("diagnostic", (
    "authenticated", "fake", "path", "cross_controller", "other_seat",
    "other_run", "other_provider", "hostile",
    "long", "cycle", "frames", "write_failure", "write_base_failure",
    "read_failure", "auth_failure", "constant_hook", "filename_hook",
    "notes_type_hook", "dictionary_hook", "notes_key_hook",
))
@pytest.mark.parametrize("fence_fails", (False, True))
def test_live_matrix_records_approval_and_retains_partial_failure_without_spend(
    certifier, tmp_path, monkeypatch, company_manifest, capsys, diagnostic, fence_fails,
):
    private = tmp_path / "private"
    admission = _synthetic_live_admission(certifier, company_manifest)
    _fake_host_credential_source(certifier, monkeypatch)
    monkeypatch.setattr(certifier, "_attest_image_platform", lambda _: None,
                        raising=False)

    canaries = ("DIAGNOSTIC_SECRET_VALUE", "DIAGNOSTIC_PAYLOAD_VALUE",
                "DIAGNOSTIC_SECRET_CLASS", "DIAGNOSTIC_SECRET_FUNCTION",
                "/private/DIAGNOSTIC_SECRET_PATH")
    unknown = type(canaries[2], (RuntimeError,), {})
    hook_calls = []

    class HookMeta(type):
        def __eq__(cls, other):
            hook_calls.append("type-equality")
            print(canaries[0])
            return False

    class ConstantHook(metaclass=HookMeta):
        def __hash__(self):
            hook_calls.append("constant-hash")
            print(canaries[0])
            return 1

    class FilenameHook(str):
        def __hash__(self):
            hook_calls.append("filename-hash")
            print(canaries[0])
            return str.__hash__(self)

    class DictionaryHook(dict):
        def get(self, *args):
            hook_calls.append("dictionary-get")
            print(canaries[0])
            return super().get(*args)

    class NotesKeyHook(str):
        __hash__ = str.__hash__

        def __eq__(self, other):
            hook_calls.append("notes-key-equality")
            print(canaries[0])
            return str.__eq__(self, other)

    namespace = {"error": unknown(*canaries), "foreign": compile(
        "raise error", canaries[4], "exec")}
    forged_source = ("def DIAGNOSTIC_SECRET_FUNCTION():\n"
                     "    exec(foreign)\nDIAGNOSTIC_SECRET_FUNCTION()\n")
    # A matching filename alone must not authenticate injected code as source.
    forged = compile(forged_source, str(certifier._SOURCE_ROOT /
                                        "scripts/fanout_container_certify.py"), "exec")
    if diagnostic == "constant_hook":
        forged = forged.replace(co_consts=forged.co_consts + (ConstantHook(),))
    elif diagnostic == "filename_hook":
        forged = forged.replace(co_filename=FilenameHook(forged.co_filename))
    calls, fence_attempts = [], []
    cleanup_digest = None
    original_write, original_read = certifier.write_evidence, certifier.read_evidence

    def no_seat(controller, spec, *, ledger, permit_id, epoch, **_kwargs):
        nonlocal cleanup_digest
        calls.append((spec.run_id, spec.seat_id, permit_id, epoch))
        if fence_fails:
            def fail_fence(active_id, active_epoch):
                fence_attempts.append((active_id, active_epoch))
                raise OSError("SYNTHETIC_FENCE_VALUE")
            monkeypatch.setattr(ledger, "fence_epoch", fail_fence)
        cleanup = {"provider": "claude", "spec": asdict(spec), "phase": "startup"}
        if diagnostic == "other_seat":
            cleanup["spec"]["seat_id"] = "seat-codex"
        elif diagnostic == "other_run":
            cleanup["spec"]["run_id"] = "another-private-run"
        elif diagnostic == "other_provider":
            cleanup["provider"] = "codex"
        if diagnostic == "cross_controller":
            other = create_lifecycle_controller(tmp_path / "other-controller")
            name, cleanup_digest = write_evidence(
                other, "container-seat-cleanup-failure", cleanup)
            certifier._write_private(controller.root / "receipts" / name,
                                     (other.root / "receipts" / name).read_bytes())
        elif diagnostic == "fake":
            cleanup_digest = "0" * 64
        elif diagnostic == "path":
            cleanup_digest = canaries[4]
        else:
            _, cleanup_digest = write_evidence(
                controller, "container-seat-cleanup-failure", cleanup)
        try:
            exec(forged, namespace)
        except RuntimeError as cause:
            try:
                raise ValueError(canaries[1]) from cause
            except ValueError as linked:
                error = certifier.ProviderRequestError(*canaries)
                error.add_note("Container cleanup failed; seat requires quarantine; receipt="
                               + cleanup_digest + "; " + " ".join(canaries))
                error.add_note("Container cleanup failed; seat requires quarantine; receipt="
                               + canaries[4])
                if diagnostic == "notes_type_hook":
                    error.__dict__["__notes__"] = ConstantHook()
                elif diagnostic == "dictionary_hook":
                    error.__dict__ = DictionaryHook(error.__dict__)
                elif diagnostic == "notes_key_hook":
                    error.__dict__ = {NotesKeyHook("__notes__"): error.__dict__["__notes__"]}
                if diagnostic == "hostile":
                    error.__cause__ = cause
                    raise cause
                if diagnostic == "long":
                    for _ in range(40):
                        next_error = ValueError(canaries[0])
                        next_error.__cause__ = linked
                        linked = next_error
                if diagnostic == "cycle":
                    linked.__cause__ = error
                raise error from linked

    def diagnostic_write(controller, category, payload):
        if category == "container-live-cell-failure" and diagnostic == "write_failure":
            raise unknown(*canaries)
        if category == "container-live-cell-failure" and diagnostic == "write_base_failure":
            raise KeyboardInterrupt(*canaries)
        name, digest = original_write(controller, category, payload)
        if category == "container-live-cell-failure" and diagnostic == "auth_failure":
            path = controller.root / "receipts" / name
            record = json.loads(path.read_bytes())
            record["mac"] = "0" * 64
            path.write_bytes(certifier.canonical_json(record))
        return name, digest

    def diagnostic_read(controller, category, name, digest):
        if category == "container-live-cell-failure" and diagnostic == "read_failure":
            raise unknown(*canaries)
        return original_read(controller, category, name, digest)

    monkeypatch.setattr(certifier, "write_evidence", diagnostic_write)
    monkeypatch.setattr(certifier, "read_evidence", diagnostic_read)
    if diagnostic == "frames":
        original_cell = certifier._live_cell

        def repeated_frames(*args, **kwargs):
            from types import TracebackType
            try:
                return original_cell(*args, **kwargs)
            except certifier.ProviderRequestError as error:
                trusted = error.__traceback__.tb_next
                for _ in range(40):
                    error.__traceback__ = TracebackType(
                        error.__traceback__, trusted.tb_frame,
                        trusted.tb_lasti, trusted.tb_lineno)
                raise

        monkeypatch.setattr(certifier, "_live_cell", repeated_frames)

    monkeypatch.setattr(certifier.PinnedDockerTransport, "start_live", no_seat)
    receipt = certifier._live_matrix(private / "targets" / "repo-alpha", private,
                                    admission)
    assert hook_calls == []
    assert receipt["mode"] == "live"
    assert receipt["approval_reference"] == "owner-ticket-123"
    assert receipt["spend_scope"] == [f"{provider}:{route}"
                                      for provider in ("claude", "codex", "agy")
                                      for route in ("initial", "resume")]
    assert receipt["providers"] == {}
    assert not certifier.evaluate_private_probe(receipt).passed
    assert "completion_evidence_name" not in receipt
    failure = receipt["failure"]
    assert failure["type"] == ("UnknownError" if diagnostic == "hostile"
                               else "ProviderRequestError")
    assert failure["diagnostic"]["status"] == (
        "unavailable" if diagnostic in (
            "write_failure", "write_base_failure", "read_failure", "auth_failure")
        else "authenticated")
    controller = resume_lifecycle_controller(
        private / "controller", LifecycleCapability(
            private.with_name(private.name + ".controller-capability").read_text()))
    assert len(calls) == 1
    run_id, seat_id, permit_id, epoch = calls[0]
    ledger = PermitLedger(controller.root / "locks" / "spend", controller)
    assert ledger.attempt_counts(permit_id, epoch) == (0, 0)
    assert ledger.request_records(permit_id, epoch) == ()
    with ledger._locked() as durable:
        assert durable["permits"][permit_id]["fenced"] is (not fence_fails)
    if fence_fails:
        assert fence_attempts == [(permit_id, epoch)]
    else:
        with pytest.raises(BrokerRejected, match="fenced"):
            ledger.record_attempt(permit_id, epoch)
    if failure["diagnostic"]["status"] == "authenticated":
        reference = failure["diagnostic"]
        assert reference["category"] == "container-live-cell-failure"
        path = controller.root / "receipts" / reference["name"]
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.stat().st_uid == os.getuid()
        assert reference["name"] == reference["digest"] + ".json"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == reference["digest"]
        capsule = read_evidence(controller, reference["category"],
                                reference["name"], reference["digest"])
        assert capsule["source_sha256"] == certifier.runtime_source_digest(certifier._SOURCE_ROOT)
        assert capsule["controller_id"] == controller.controller_id
        assert (capsule["run_id"], capsule["provider"], capsule["route"], capsule["phase"]) == (
            run_id, "claude", "initial", "live-cell")
        assert len(certifier.canonical_json(capsule)) <= 16 * 1024
        entries = capsule["exceptions"]
        assert 1 <= len(entries) <= 8
        assert entries[0]["type"] == failure["type"]
        frames = entries[0]["frames"]
        assert {frame["filename"] for frame in frames} == {
            "scripts/fanout_container_certify.py"}
        assert len(frames) == (8 if diagnostic == "frames" else 2)
        assert entries[0]["frames_truncated"] is (diagnostic == "frames")
        assert all(type(frame["line"]) is int and frame["line"] > 0 for frame in frames)
        source_lines = (certifier._SOURCE_ROOT /
                        "scripts/fanout_container_certify.py").read_text().splitlines()
        assert {source_lines[frame["line"] - 1].strip() for frame in frames} == {
            "cell = _live_cell(", "transport = PinnedDockerTransport.start_live("}
        if diagnostic != "hostile":
            assert entries[0]["cause"] == 1
            assert entries[1]["type"] == "ValueError"
            if diagnostic != "long":
                assert entries[1]["context"] == 2
                assert entries[2]["type"] == "UnknownError"
                assert entries[2]["frames"] == []
            cleanup = entries[0]["cleanup"]
            expected_cleanup = ("unavailable" if diagnostic in (
                "notes_type_hook", "dictionary_hook", "notes_key_hook") else
                "invalid" if diagnostic in (
                "fake", "path", "cross_controller", "other_seat", "other_run",
                "other_provider") else "authenticated")
            assert cleanup["status"] == expected_cleanup
            if cleanup["status"] == "authenticated":
                assert cleanup["digest"] == cleanup_digest
                assert cleanup["category"] == "container-seat-cleanup-failure"
        assert capsule["exceptions_truncated"] is (diagnostic == "long")
        assert capsule["cycle_detected"] is (diagnostic in ("cycle", "hostile"))
        assert all(len(entry["frames"]) <= 32 for entry in entries)
    else:
        assert failure["diagnostic"] == {"status": "unavailable"}
    assert (private / "receipt.json").is_file()
    assert (private / "approval.json").is_file()
    for path in private.rglob("*"):
        if path.is_file():
            contents = path.read_bytes()
            assert b"synthetic-private-value" not in contents
            assert all(canary.encode() not in contents for canary in canaries)
    output = capsys.readouterr()
    assert all(canary not in output.out + output.err for canary in canaries)


@pytest.mark.parametrize("changed", ("manifest", "policy"))
def test_cold_route_recheck_rejects_owner_file_drift(
    certifier, company_manifest, changed,
):
    admission = _synthetic_live_admission(certifier, company_manifest)
    path = admission.manifest_path if changed == "manifest" else admission.policy_path
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(certifier.CertificationRejected, match="machine route|policy"):
        certifier._require_live_identity(admission)


def test_cold_route_recheck_rejects_changed_owner_approval_packet(
    certifier, company_manifest,
):
    admission = _synthetic_live_admission(certifier, company_manifest)
    path = admission.approval_packet_path
    packet = json.loads(path.read_bytes())
    packet["spend_scope"] = packet["spend_scope"][:-1]
    path.write_bytes(certifier.canonical_json(packet))
    with pytest.raises(certifier.CertificationRejected, match="owner approval packet"):
        certifier._require_live_identity(admission)


def test_cold_resume_refuses_changed_credential_source_fingerprint(
    certifier, company_manifest, tmp_path, monkeypatch,
):
    admission = _synthetic_live_admission(certifier, company_manifest)
    _fake_host_credential_source(certifier, monkeypatch)
    with pytest.raises(certifier.CertificationRejected, match="credential source changed"):
        certifier._live_cell(
            None, None, tmp_path, tmp_path, tmp_path, tmp_path, None,
            "private-synthetic", "claude", "resume", admission,
            {"credential_source_fingerprint": "0" * 64}, "6" * 64)


@pytest.mark.parametrize("outcome", ("complete", "exposed", "uncertain", "approval_changed",
                                    "tool_loop_two", "tool_loop_four", "agy_auxiliary",
                                    "fifth_main", "terminal_timeout", "turn_failure",
                                    "turn_cleanup_failure", "stop_failure",
                                    "turn_fence_failure", "turn_cleanup_fence_failure",
                                    "probe_cleanup_failure", "postturn_close_failure",
                                    "normal_fence_failure"))
def test_live_orchestration_uses_task_five_stage_turn_export_and_cold_resume(
    certifier, tmp_path, monkeypatch, outcome, company_manifest,
):
    from shared.lib.fanout.container_boundary import PinnedDockerTransport
    from shared.lib.fanout.container_runtime import GuestSocketAttestation
    from shared.lib.fanout.container_vm import VMProbe
    from shared.lib.fanout.process import ProcessResult, ProcessStatus
    private = tmp_path / "private"
    admission = _synthetic_live_admission(certifier, company_manifest)
    _fake_host_credential_source(certifier, monkeypatch)
    monkeypatch.setattr(certifier, "_attest_image_platform", lambda _: None)
    sends = []
    scopes, before_failure, retained, normal_fence_attempts = {}, {}, [], []
    initial_transports = {}
    primary = certifier.ProviderRequestError("SYNTHETIC_PRIMARY_VALUE")
    cleanup_error = certifier.ProviderRequestError("SYNTHETIC_CLEANUP_VALUE")
    original_retain = certifier._retain_cell_failure

    def retain_failure(controller, active_admission, run_id, provider, route, error):
        retained.append(error)
        return original_retain(controller, active_admission, run_id, provider, route, error)

    monkeypatch.setattr(certifier, "_retain_cell_failure", retain_failure)
    original_turn = certifier.run_container_turn

    def checked_turn(request, boundary):
        spec = boundary.admission.transport.spec
        identity = (spec.run_id, spec.task_id, spec.target_id, spec.seat_id)
        assert (request.run_id, request.task_id, request.target_id, request.seat_id) == identity
        assert boundary.session_store.identity == identity
        assert boundary.session_store.volume == spec.session_volume
        return original_turn(request, boundary)

    monkeypatch.setattr(certifier, "run_container_turn", checked_turn)
    main_requests = {"tool_loop_two": 2, "tool_loop_four": 4,
                     "fifth_main": 5, "terminal_timeout": 4}.get(outcome, 1)

    def fake_probe(target, sibling, disposable, provider, route, boundary,
                   transport, raw_root):
        path = raw_root / f"{provider}-{route}-isolation.json"
        path.write_text("synthetic probe")
        proofs = {name: {"read_denied": True, "write_denied": True}
                  for name in certifier.ISOLATION_TARGETS}
        if outcome == "exposed":
            proofs["caller"]["read_denied"] = False
        elif outcome == "probe_cleanup_failure":
            with transport.ledger._locked() as durable:
                before_failure.update(json.loads(json.dumps(durable)))
            raise primary
        elif outcome == "approval_changed":
            packet_path = admission.approval_packet_path
            packet = json.loads(packet_path.read_bytes())
            packet["spend_scope"] = packet["spend_scope"][:-1]
            packet_path.write_bytes(certifier.canonical_json(packet))
        return proofs, True, str(path)

    monkeypatch.setattr(certifier, "_probe_isolation", fake_probe)
    capability_path = private.with_name(private.name + ".controller-capability")
    capability_parent = private.parent.stat()
    capability_parent_synced = []
    original_fsync = os.fsync

    def track_capability_sync(fd):
        info = os.fstat(fd)
        if (capability_path.exists() and stat.S_ISDIR(info.st_mode)
                and (info.st_dev, info.st_ino) ==
                (capability_parent.st_dev, capability_parent.st_ino)):
            capability_parent_synced.append(True)
        return original_fsync(fd)

    monkeypatch.setattr(certifier.os, "fsync", track_capability_sync)

    def synthetic_start(controller, spec, *, ledger, broker, permit_id, epoch,
                        recover=False):
        assert capability_parent_synced, "capability filename was not durable before seat start"
        assert broker.permit.max_request_bytes == 1048576
        assert broker.permit.max_response_bytes == 8388608
        assert broker.permit.max_output_tokens == 8192
        assert broker.permit.response_deadline == 300
        provider = broker.permit.provider
        route = broker.permit.route
        scopes[provider, route] = (spec, broker.permit, permit_id, epoch)
        if outcome in ("turn_fence_failure", "turn_cleanup_fence_failure"):
            def fail_fence(_permit_id, _epoch):
                assert (_permit_id, _epoch) == (permit_id, epoch)
                raise OSError("SYNTHETIC_FENCE_VALUE")
            monkeypatch.setattr(ledger, "fence_epoch", fail_fence)
        elif outcome == "normal_fence_failure":
            original_fence = ledger.fence_epoch

            def fail_first_fence(active_id, active_epoch):
                normal_fence_attempts.append((active_id, active_epoch))
                if len(normal_fence_attempts) == 1:
                    raise primary
                return original_fence(active_id, active_epoch)

            monkeypatch.setattr(ledger, "fence_epoch", fail_first_fence)
        failure_outcomes = {"turn_failure", "turn_cleanup_failure", "stop_failure",
                            "turn_fence_failure", "turn_cleanup_fence_failure",
                            "probe_cleanup_failure", "postturn_close_failure",
                            "normal_fence_failure"}
        if outcome in failure_outcomes:
            ledger.register(replace(broker.permit, seat_id="seat-unrelated"), epoch)
        workspace = private / "synthetic-guest" / provider
        workspace.parent.mkdir(exist_ok=True)
        if not recover:
            workspace.mkdir()

        class Launcher(certifier._FakeLauncher):
            def _run(self, *args):
                if (outcome in ("turn_cleanup_failure", "stop_failure",
                                "turn_cleanup_fence_failure", "probe_cleanup_failure")
                        and args == ("stop", "--time", "0", self.handle.container_id)):
                    _, digest = write_evidence(controller, "container-seat-cleanup-failure", {
                        "provider": provider, "spec": asdict(spec), "phase": "synthetic-stop"})
                    cleanup_error.add_note(
                        "Container cleanup failed; seat requires quarantine; receipt=" + digest)
                    raise cleanup_error
                return super()._run(*args)

        previous = None
        if recover:
            initial = initial_transports[provider]
            _, _, prior_permit_id, prior_epoch = scopes[provider, "initial"]
            records = ledger.request_records(prior_permit_id, prior_epoch)
            assert records and all(record["state"] == "settled" for record in records)
            native = read_evidence(controller, "container-seat-stopped", *initial.stop_evidence)
            assert native["handle"] == asdict(initial.handle)
            name, digest = write_evidence(controller, "container-fake-stopped", {
                "container_id": initial.handle.container_id, "seat_id": initial.spec.seat_id,
                "session_volume": initial.spec.session_volume,
                "permit_id": prior_permit_id, "permit_epoch": prior_epoch,
                "stopped": not initial.launcher.running, "broker_state": "settled",
                "native_stop_evidence_name": initial.stop_evidence[0],
                "native_stop_evidence_digest": initial.stop_evidence[1],
            })
            previous = {"container_id": initial.handle.container_id,
                        "permit_id": prior_permit_id, "permit_epoch": prior_epoch,
                        "stop_evidence_name": name, "stop_evidence_digest": digest}
        launcher = Launcher(spec, route, controller=controller, ledger=ledger,
                            previous=previous)
        vm = VMProbe("fanout-isolated", "unix:///private/fake/docker.sock",
                     "2" * 64, "3" * 64, "4" * 64, "5" * 64)
        socket = GuestSocketAttestation(spec.socket_path, 65532,
                                        stat.S_IFSOCK | 0o600, 1, 2, 3)

        def run_process(_command):
            workdir = workspace / certifier.Path(
                transport._model_workdir).relative_to("/workspace")
            readme = workdir / "README.md"
            marker = f"certified synthetic {provider} {route}\n".encode()
            wire = "gemini-3.8-flash" if provider == "agy" else broker.permit.model
            path = (f"/v1beta/models/{wire}:streamGenerateContent?alt=sse"
                    if provider == "agy" else
                    "/v1/messages" if provider == "claude" else "/v1/responses")
            if outcome == "agy_auxiliary" and provider == "agy":
                _refuse_auxiliary(ledger, broker.permit, permit_id, epoch)
            for index in range(main_requests):
                identity = OutboundRequestIdentity(
                    "POST", path, hashlib.sha256(f"body-{index}".encode()).hexdigest(),
                    32, wire, broker.permit.upstream_route_digest,
                    broker.permit.upstream_origin, broker.permit.upstream_path,
                    hashlib.sha256(f"effective-{index}".encode()).hexdigest(),
                    broker.permit.upstream_model)
                request_id = f"request-{provider}-{route}-{index}"
                ledger.record_attempt(permit_id, epoch)
                ledger.reserve(permit_id, epoch, request_id, identity)
                ledger.claim_send(permit_id, epoch, request_id, broker.permit, identity)
                sends.append((provider, route))
                if outcome not in ("uncertain", "turn_failure", "turn_cleanup_failure",
                                   "turn_fence_failure", "turn_cleanup_fence_failure"):
                    ledger.settle(request_id, "8" * 64)
                if index == 0:
                    readme.write_bytes(readme.read_bytes() + marker)
            if outcome in failure_outcomes:
                with ledger._locked() as durable:
                    before_failure.update(json.loads(json.dumps(durable)))
            if outcome in ("turn_failure", "turn_cleanup_failure", "turn_fence_failure",
                           "turn_cleanup_fence_failure"):
                raise primary
            session_id = broker.permit.session_id or f"{provider}-synthetic-session"
            if outcome == "terminal_timeout":
                return ProcessResult(ProcessStatus.TIMEOUT, None,
                                     certifier._fake_terminal(
                                         provider, session_id, broker.permit.model), b"")
            return ProcessResult(ProcessStatus.EXIT, 0,
                                 certifier._fake_terminal(
                                     provider, session_id, broker.permit.model), b"")

        transport = PinnedDockerTransport.start(
            controller, spec, launcher=launcher, vm_attestor=lambda: vm,
            socket_attestor=lambda _: socket,
            image_attestor=lambda: spec.image_digest,
            cli_attestor=lambda _: spec.cli_digest,
            process_runner=run_process, recover=recover)
        transport._transfer_popen = certifier._fake_transfer_popen(
            workspace, transport.handle.container_id)
        transport.ledger = ledger
        if not recover:
            initial_transports[provider] = transport
        if outcome == "postturn_close_failure":
            original_close = transport.close
            close_calls = []

            def fail_second_close():
                close_calls.append(True)
                if len(close_calls) == 2:
                    raise primary
                return original_close()

            transport.close = fail_second_close
        return transport

    monkeypatch.setattr(certifier.PinnedDockerTransport, "start_live", synthetic_start)
    receipt = certifier._live_matrix(private / "targets" / "repo-alpha", private,
                                    admission)
    if outcome in ("turn_failure", "turn_cleanup_failure", "stop_failure",
                   "turn_fence_failure", "turn_cleanup_fence_failure",
                   "probe_cleanup_failure", "postturn_close_failure", "normal_fence_failure"):
        assert retained == [cleanup_error if outcome == "stop_failure" else primary]
        assert sends == ([] if outcome == "probe_cleanup_failure" else [("claude", "initial")])
        assert receipt["providers"] == {}
        assert "completion_evidence_name" not in receipt
        assert not certifier.evaluate_private_probe(receipt).passed
        capability = LifecycleCapability(capability_path.read_text())
        cold = resume_lifecycle_controller(private / "controller", capability)
        cold_ledger = PermitLedger(cold.root / "locks" / "spend", cold)
        spec, permit, permit_id, epoch = scopes["claude", "initial"]
        fence_failed = outcome in ("turn_fence_failure", "turn_cleanup_fence_failure")
        before_failure["permits"][permit_id]["fenced"] = not fence_failed
        with cold_ledger._locked() as durable:
            assert durable == before_failure
            assert durable["permits"][permit_id]["fenced"] is (not fence_failed)
            assert all(entry["fenced"] is False for key, entry in durable["permits"].items()
                       if key != permit_id)
        records = cold_ledger.request_records(permit_id, epoch)
        if outcome == "probe_cleanup_failure":
            assert records == ()
        else:
            assert records[0]["state"] == (
                "settled" if outcome in ("stop_failure", "postturn_close_failure",
                                         "normal_fence_failure") else "uncertain")
        if not fence_failed:
            with pytest.raises(BrokerRejected, match="fenced"):
                cold_ledger.require_permit(permit_id, epoch, permit)
            with pytest.raises(BrokerRejected, match="fenced"):
                cold_ledger.record_attempt(permit_id, epoch)
            identity = OutboundRequestIdentity(
                "POST", "/v1/messages", "1" * 64, 32, permit.model,
                permit.upstream_route_digest, permit.upstream_origin,
                permit.upstream_path, "2" * 64, permit.upstream_model)
            with pytest.raises(BrokerRejected):
                cold_ledger.claim_send(permit_id, epoch, "future-request", permit, identity)
        if outcome not in ("probe_cleanup_failure", "postturn_close_failure", "normal_fence_failure"):
            store = certifier.SessionStore(cold, spec.run_id, spec.task_id, spec.target_id,
                                          spec.seat_id, spec.session_volume)
            with pytest.raises(certifier.ProviderRequestError, match="uncertain"):
                store.resume("claude")
        reference = receipt["failure"]["diagnostic"]
        assert reference["status"] == "authenticated"
        capsule = read_evidence(cold, reference["category"], reference["name"], reference["digest"])
        if outcome in ("turn_cleanup_failure", "stop_failure",
                       "turn_cleanup_fence_failure", "probe_cleanup_failure"):
            assert capsule["exceptions"][0]["cleanup"]["status"] == "authenticated"
        if outcome in ("stop_failure", "probe_cleanup_failure"):
            assert not (private / "raw" / "claude-initial-stopped-inspect.json").exists()
        if fence_failed:
            assert "Exact permit fence failed: OSError" in primary.__notes__
        if outcome == "normal_fence_failure":
            assert normal_fence_attempts == [(permit_id, epoch), (permit_id, epoch)]
        for path in private.rglob("*"):
            if path.is_file():
                contents = path.read_bytes()
                assert all(value not in contents for value in (
                    b"SYNTHETIC_PRIMARY_VALUE", b"SYNTHETIC_CLEANUP_VALUE", b"SYNTHETIC_FENCE_VALUE"))
        return
    if outcome in ("exposed", "approval_changed"):
        assert sends == []
        assert "completion_evidence_name" not in receipt
        failure = dict(receipt["failure"])
        assert failure.pop("diagnostic")["status"] == "authenticated"
        assert failure == {"provider": "claude", "route": "initial",
                           "type": "CertificationRejected"}
        return
    if outcome == "uncertain":
        assert sends == [("claude", "initial")]
        assert "completion_evidence_name" not in receipt
        assert receipt["failure"] == {"provider": "claude", "route": "initial",
                                      "type": "CellFailed"}
        assert receipt["providers"]["claude"]["initial"]["ledger_requests"][0]["state"] == "uncertain"
        return
    if outcome in ("fifth_main", "terminal_timeout"):
        assert sends == [("claude", "initial")] * 4
        assert "completion_evidence_name" not in receipt
        failure = dict(receipt["failure"])
        if outcome == "fifth_main":
            assert failure.pop("diagnostic")["status"] == "authenticated"
        assert failure == {
            "provider": "claude", "route": "initial",
            "type": "BrokerRejected" if outcome == "fifth_main" else "CellFailed"}
        assert not certifier.evaluate_private_probe(receipt).passed
        return
    assert "failure" not in receipt
    capability_path = private.with_name(private.name + ".controller-capability")
    assert capability_path.stat().st_mode & 0o777 == 0o600
    reopened = resume_lifecycle_controller(
        private / "controller", LifecycleCapability(capability_path.read_text()))
    completion = read_evidence(
        reopened, "container-private-matrix-complete",
        receipt["completion_evidence_name"], receipt["completion_evidence_digest"])
    unsigned = {key: value for key, value in receipt.items()
                if key not in ("completion_evidence_name", "completion_evidence_digest")}
    assert completion == {
        "schema_version": certifier.SCHEMA,
        "receipt_sha256": hashlib.sha256(certifier.canonical_json(unsigned)).hexdigest(),
    }
    assert sends == [(provider, route) for provider in ("claude", "codex", "agy")
                     for route in ("initial", "resume") for _ in range(main_requests)]
    assert certifier.evaluate_private_probe(receipt).passed
    assert certifier.verify_private_route_evidence(private, company_manifest[2]).passed
    for provider, cells in receipt["providers"].items():
        initial_spec, initial_permit, _, _ = scopes[provider, "initial"]
        resume_spec, resume_permit, _, _ = scopes[provider, "resume"]
        assert initial_spec == resume_spec
        assert initial_permit.seat_id == resume_permit.seat_id == initial_spec.seat_id
        assert all(initial_spec.seat_id != other_spec.seat_id
                   for (other_provider, _), (other_spec, _, _, _) in scopes.items()
                   if other_provider != provider)
        assert cells["initial"]["session_id"] == cells["resume"]["session_id"]
        assert cells["initial"]["permit_id"] != cells["resume"]["permit_id"]
        for route, cell in cells.items():
            assert cell["candidate_edited_paths"] == ["README.md"]
            auxiliary = int(outcome == "agy_auxiliary" and provider == "agy")
            assert cell["broker_request_count"] == main_requests
            assert cell["broker_attempt_count"] == main_requests + auxiliary
            assert cell["broker_auxiliary_refusal_count"] == auxiliary
            assert cell["ledger_requests"][0]["identity"]["wire_model"] == (
                "gemini-3.8-flash" if provider == "agy" else cell["requested_model"])


@pytest.mark.parametrize("changed", ("session_id", "session_volume", "permit_id",
                                      "permit_epoch", "prior_seat_stopped"))
def test_resume_requires_exact_bound_session_and_fenced_prior_seat(
    certifier, complete_receipt, changed,
):
    resume = complete_receipt["providers"]["claude"]["resume"]
    resume[changed] = False if changed == "prior_seat_stopped" else "changed"
    assert not certifier.evaluate_private_probe(complete_receipt).passed


@pytest.mark.parametrize("boundary", ("caller", "sibling", "git_objects",
                                     "credentials", "docker_socket", "host_home",
                                     "vm_network"))
def test_any_exposed_boundary_blocks_probe(certifier, complete_receipt, boundary):
    complete_receipt["providers"]["agy"]["initial"]["isolation"][boundary]["read_denied"] = False
    assert not certifier.evaluate_private_probe(complete_receipt).passed


def test_uncertain_broker_send_blocks_probe(certifier, complete_receipt):
    disposition = complete_receipt["providers"]["codex"]["resume"]["broker_dispositions"][0]
    disposition["state"] = "uncertain"
    disposition["response_sha256"] = None
    assert not certifier.evaluate_private_probe(complete_receipt).passed


def test_fake_route_receipt_never_claims_live_certification(certifier, complete_receipt):
    complete_receipt["mode"] = "fake"
    assert not certifier.evaluate_private_probe(complete_receipt).passed


def test_maka_ballot_cannot_turn_failed_isolation_green(certifier, complete_receipt):
    complete_receipt["providers"]["agy"]["initial"]["isolation"]["caller"]["write_denied"] = False

    class FakeMaka:
        target_id = "maka"
        no_tools_characterization_sha256 = "e" * 64

        def judge(self, _request):
            raise AssertionError("no completed answer artifact was supplied")

    quality = certifier.assess_quality(complete_receipt, FakeMaka())
    assert not quality.isolation_passed
    assert len(quality.ballot_digest) == 64


def _refuse_auxiliary(ledger, permit, permit_id, epoch, *, path=None):
    def no_send(*_args):
        pytest.fail("no-spend auxiliary reached the upstream sender")

    broker = Broker(permit, credential="synthetic-private-value", send=no_send)
    request = RelayRequest(
        permit_id, epoch, "auxiliary", "POST",
        path or "/v1beta/models/gemini-3.1-flash-lite-preview:streamGenerateContent?alt=sse",
        {"content-type": "application/json", "x-goog-api-key": "disposable-placeholder"},
        b'{"contents":[{"role":"user","parts":[{"text":"synthetic"}]}],'
        b'"generationConfig":{"maxOutputTokens":32}}')
    ledger.record_attempt(permit_id, epoch)
    with pytest.raises(BrokerRejected):
        dispatch_once(request, ledger, broker)


def _completed_quality_root(certifier, complete_receipt, tmp_path, monkeypatch,
                            *, ledger_body_digest=None, main_requests=1,
                            auxiliary_attempts=0, auxiliary_path=None,
                            unexplained_attempts=0, unparseable_attempts=0):
    class FakeCredentialSource:
        def __init__(self, _route):
            self.fingerprint = None

        def load(self):
            self.fingerprint = "9" * 64
            return "synthetic-private-value"

    monkeypatch.setattr(certifier, "HostCredentialSource", FakeCredentialSource)
    root = tmp_path / "quality"
    root.mkdir(mode=0o700)
    capability = LifecycleCapability("synthetic-controller-capability-" + "x" * 32)
    certifier._write_private(
        root.with_name(root.name + ".controller-capability"),
        capability.export_token().encode("ascii"))
    controller = create_lifecycle_controller(root / "controller", capability=capability)
    ledger = PermitLedger(controller.root / "locks" / "spend", controller)
    complete_receipt["private_run_root"] = str(root)
    complete_receipt["answers"] = {}
    packet_path = tmp_path / "owner-approval.json"
    _invented_approval_packet(
        certifier, (Path(complete_receipt["manifest_path"]),
                    Path(complete_receipt["policy_path"]),
                    certifier.read_machine_routes(
                        Path(complete_receipt["manifest_path"]),
                        Path(complete_receipt["policy_path"]))),
        root, packet_path)
    complete_receipt["approval_packet_path"] = str(packet_path)
    complete_receipt["approval_packet_digest"] = hashlib.sha256(
        packet_path.read_bytes()).hexdigest()
    complete_receipt["spend_ceiling"] = {"max_model_calls": 24,
                                         "max_request_bytes_per_call": 1048576,
                                         "max_response_bytes_per_call": 8388608,
                                         "max_output_tokens_per_call": 8192}
    approval = {
        "reference": complete_receipt["approval_reference"],
        "observation_date": complete_receipt["observation_date"],
        "spend_scope": complete_receipt["spend_scope"],
        "source_sha256": complete_receipt["source_sha256"],
        "host_runtime": complete_receipt["host_runtime"],
        "image_digest": complete_receipt["image_digest"],
        "cli_digests": complete_receipt["cli_digests"],
        "manifest_digest": complete_receipt["manifest_digest"],
        "policy_digest": complete_receipt["policy_digest"],
        "machine_routes": complete_receipt["machine_routes"],
        "approval_packet_path": complete_receipt["approval_packet_path"],
        "approval_packet_digest": complete_receipt["approval_packet_digest"],
        "spend_ceiling": complete_receipt["spend_ceiling"],
    }
    approval_name, approval_digest = write_evidence(
        controller, "container-spend-approval", approval)
    complete_receipt["approval_evidence_name"] = approval_name
    complete_receipt["approval_evidence_digest"] = approval_digest
    for cells in complete_receipt["providers"].values():
        for cell in cells.values():
            cell["approval_evidence_digest"] = approval_digest
    for provider in certifier.PROVIDERS:
        profile = certifier.ProviderRegistry.default(
            version_probe=lambda _: "unused").select(provider, "repo-write", "standard")
        for route, epoch in (("initial", 1), ("resume", 2)):
            cell = complete_receipt["providers"][provider][route]
            routed = complete_receipt["machine_routes"][provider]
            permit = BrokerPermit(
                provider, "private-quality", "synthetic-task", "repo-alpha",
                f"seat-{provider}", route, profile.requested_model,
                "disposable-placeholder", 4, 1048576, 8388608, 8192,
                inputs_digest="9" * 64, session_id=f"session-{provider}",
                upstream_route_digest=routed["digest"],
                upstream_origin=routed["origin"],
                upstream_model=routed["model"],
                upstream_path=routed["path"])
            permit_id = ledger.register(permit, epoch)
            cell["permit_id"] = permit_id
            if provider == "agy":
                for _ in range(auxiliary_attempts):
                    _refuse_auxiliary(ledger, permit, permit_id, epoch, path=auxiliary_path)
                for _ in range(unexplained_attempts):
                    ledger.record_attempt(permit_id, epoch)
                for _ in range(unparseable_attempts):
                    ledger.record_attempt(permit_id, epoch)
                    with pytest.raises(BrokerRejected, match="unsupported fields"):
                        parse_request_frame({"type": "request"})
            seed_identity = cell["ledger_requests"][0]["identity"]
            for index in range(main_requests):
                identity = OutboundRequestIdentity(**{
                    **seed_identity,
                    "body_sha256": hashlib.sha256(f"normalized-{index}".encode()).hexdigest(),
                    "upstream_body_sha256": hashlib.sha256(f"effective-{index}".encode()).hexdigest(),
                })
                if provider == "codex" and route == "resume" and ledger_body_digest:
                    identity = OutboundRequestIdentity(**{
                        **asdict(identity), "upstream_body_sha256": ledger_body_digest})
                request_id = f"{provider}-{route}-{index}"
                ledger.record_attempt(permit_id, epoch)
                ledger.reserve(permit_id, epoch, request_id, identity)
                ledger.claim_send(permit_id, epoch, request_id, permit, identity)
                ledger.settle(request_id, hashlib.sha256(f"response-{index}".encode()).hexdigest())
            ledger.fence_epoch(permit_id, epoch)
            cell["ledger_requests"] = list(ledger.request_records(permit_id, epoch))
            if provider == "codex" and route == "resume" and ledger_body_digest:
                cell["ledger_requests"][0]["identity"]["upstream_body_sha256"] = "a" * 64
            cell["broker_request_count"] = main_requests
            cell["broker_attempt_count"], cell["broker_auxiliary_attempt_count"] = (
                ledger.attempt_counts(permit_id, epoch))
            cell["broker_auxiliary_refusal_count"] = ledger.auxiliary_refusal_count(permit_id, epoch)
            cell["broker_dispositions"] = [{
                "request_id": record["request_id"], "state": record["state"],
                "response_sha256": record["response_digest"]}
                for record in cell["ledger_requests"]]
        answer = root / f"answer-{provider}.bin"
        digest = certifier._write_private(
            answer, f"completed synthetic {provider} answer".encode())
        complete_receipt["providers"][provider]["resume"]["answer_sha256"] = digest
        complete_receipt["answers"][provider] = {
            "path": str(answer), "sha256": digest}
    name, digest = write_evidence(controller, "container-private-matrix-complete", {
        "schema_version": certifier.SCHEMA,
        "receipt_sha256": hashlib.sha256(
            certifier.canonical_json(complete_receipt)).hexdigest(),
    })
    complete_receipt["completion_evidence_name"] = name
    complete_receipt["completion_evidence_digest"] = digest
    certifier._write_private(root / "receipt.json", certifier.canonical_json(complete_receipt))
    return root, controller, ledger


def test_verifier_reopens_hmac_ledger_effective_body(
    certifier, complete_receipt, tmp_path, monkeypatch, company_manifest,
):
    root, _, _ = _completed_quality_root(
        certifier, complete_receipt, tmp_path, monkeypatch,
        ledger_body_digest="0" * 64)
    assert certifier.evaluate_private_probe(complete_receipt).passed
    assert not certifier.verify_private_route_evidence(root, company_manifest[2]).passed


@pytest.mark.parametrize("extra_record", ("permit", "request", "send"))
def test_verifier_rejects_unreported_seventh_authenticated_record(
    certifier, complete_receipt, tmp_path, monkeypatch, company_manifest, extra_record,
):
    root, _, ledger = _completed_quality_root(
        certifier, complete_receipt, tmp_path, monkeypatch)
    assert certifier.verify_private_route_evidence(root, company_manifest[2]).passed
    routed = complete_receipt["machine_routes"]["codex"]
    profile = certifier.ProviderRegistry.default(
        version_probe=lambda _: "unused").select("codex", "repo-write", "standard")
    permit = BrokerPermit(
        "codex", "private-extra", "synthetic-task", "repo-alpha",
        "seat-extra", "initial", profile.requested_model,
        "disposable-placeholder", 1, 1024, 8192, 1024,
        inputs_digest="9" * 64, session_id="session-extra",
        upstream_route_digest=routed["digest"],
        upstream_origin=routed["origin"],
        upstream_model=routed["model"], upstream_path=routed["path"])
    permit_id = ledger.register(permit, 1)
    identity = OutboundRequestIdentity(**complete_receipt["providers"]["codex"]["initial"]
                                       ["ledger_requests"][0]["identity"])
    if extra_record in ("request", "send"):
        ledger.record_attempt(permit_id, 1)
        ledger.reserve(permit_id, 1, "unreported-seventh-send", identity)
    if extra_record == "send":
        ledger.claim_send(permit_id, 1, "unreported-seventh-send", permit, identity)
        ledger.settle("unreported-seventh-send", "8" * 64)
    ledger.fence_epoch(permit_id, 1)
    assert certifier.evaluate_private_probe(complete_receipt).passed
    assert not certifier.verify_private_route_evidence(root, company_manifest[2]).passed


def test_verifier_reopens_current_owner_approval_packet(
    certifier, complete_receipt, tmp_path, monkeypatch, company_manifest,
):
    root, _, _ = _completed_quality_root(
        certifier, complete_receipt, tmp_path, monkeypatch)
    assert certifier.verify_private_route_evidence(root, company_manifest[2]).passed
    packet_path = Path(complete_receipt["approval_packet_path"])
    packet = json.loads(packet_path.read_bytes())
    packet["route_digests"]["agy"] = "0" * 64
    packet_path.write_bytes(certifier.canonical_json(packet))
    assert not certifier.verify_private_route_evidence(root, company_manifest[2]).passed


def test_private_receipt_reopen_rejects_symlinked_ancestor(certifier, tmp_path):
    actual_parent = tmp_path / "actual"
    actual_parent.mkdir(mode=0o700)
    actual_root = actual_parent / "quality"
    actual_root.mkdir(mode=0o700)
    alias_parent = tmp_path / "alias"
    alias_parent.symlink_to(actual_parent, target_is_directory=True)
    aliased_root = alias_parent / "quality"
    certifier._write_private(actual_root / "receipt.json", certifier.canonical_json({
        "private_run_root": str(aliased_root),
    }))
    with pytest.raises(certifier.CertificationRejected, match="private route"):
        certifier._read_private_receipt(aliased_root)


def test_verifier_accepts_six_authenticated_route_cells(
    certifier, complete_receipt, tmp_path, monkeypatch, company_manifest,
):
    root, _, _ = _completed_quality_root(
        certifier, complete_receipt, tmp_path, monkeypatch)
    assert certifier.verify_private_route_evidence(root, company_manifest[2]).passed


@pytest.mark.parametrize("main_requests,auxiliary_attempts", (
    (2, 0), (2, 1), (4, 0), (4, 1), (3, 3), (1, 5),
))
def test_bounded_tool_loop_reopens_every_authenticated_request(
    certifier, complete_receipt, tmp_path, monkeypatch, company_manifest,
    main_requests, auxiliary_attempts,
):
    root, controller, _ = _completed_quality_root(
        certifier, complete_receipt, tmp_path, monkeypatch,
        main_requests=main_requests, auxiliary_attempts=auxiliary_attempts)
    cold = resume_lifecycle_controller(controller.root, controller.capability)
    reopened = PermitLedger(cold.root / "locks" / "spend", cold)
    with reopened._locked() as durable:
        assert len(durable["permits"]) == 6
        assert len(durable["requests"]) == len(durable["send_claims"]) == 6 * main_requests
        assert all(entry["used"] == main_requests for entry in durable["permits"].values())
    assert certifier.evaluate_private_probe(complete_receipt).passed
    assert certifier.verify_private_route_evidence(root, company_manifest[2]).passed


@pytest.mark.parametrize("provider,main_requests,auxiliary,attempts,marker", (
    ("agy", 4, 3, 7, 1),
    ("agy", 3, 2, 6, 1),
    ("agy", 3, 3, 6, 0),
    ("agy", 3, 3, 6, 3),
    ("agy", True, 0, 1, 0),
    ("agy", -1, 0, 1, 0),
    ("agy", 1, False, 1, 0),
    ("agy", 1, 0, True, 0),
    ("agy", 1, 0, 1, False),
    ("agy", 1, -1, 0, 0),
    ("agy", 1, 0, -1, 0),
    ("agy", 1, 0, 1, -1),
    ("claude", 1, 1, 2, 1),
    ("codex", 1, 1, 2, 1),
))
def test_receipt_rejects_unbounded_or_unaccounted_refused_attempts(
    certifier, complete_receipt, provider, main_requests, auxiliary, attempts, marker,
):
    cell = complete_receipt["providers"][provider]["initial"]
    seed = cell["ledger_requests"][0]
    cell["ledger_requests"] = [{**seed, "request_id": f"main-{index}"}
                               for index in range(main_requests)]
    cell["broker_dispositions"] = [{"request_id": record["request_id"],
        "state": "settled", "response_sha256": record["response_digest"]}
        for record in cell["ledger_requests"]]
    cell.update(broker_request_count=main_requests, broker_attempt_count=attempts,
                broker_auxiliary_attempt_count=auxiliary, broker_auxiliary_refusal_count=marker)
    assert not certifier.evaluate_private_probe(complete_receipt).passed


@pytest.mark.parametrize("provider,damage", (
    ("claude", "auxiliary"), ("codex", "auxiliary"), ("agy", "refusal_disposition"),
    ("agy", "missing_refusal"), ("agy", "extra_route"),
))
def test_tool_loop_claims_require_exact_auxiliary_disposition_and_six_cells(
    certifier, complete_receipt, provider, damage,
):
    assert certifier.evaluate_private_probe(complete_receipt).passed
    cell = complete_receipt["providers"][provider]["initial"]
    if damage == "extra_route":
        complete_receipt["providers"][provider]["retry"] = {**cell, "permit_id": "0" * 64}
    else:
        cell["broker_attempt_count"] = 2
        cell["broker_auxiliary_attempt_count"] = 1
        cell["broker_auxiliary_refusal_count"] = 0 if damage == "missing_refusal" else 1
        if damage == "refusal_disposition":
            cell["broker_dispositions"].append({"request_id": "auxiliary", "state": "settled",
                                                 "response_sha256": "0" * 64})
    assert not certifier.evaluate_private_probe(complete_receipt).passed


def _reseal_completed_receipt(certifier, root, controller, receipt):
    unsigned = {key: value for key, value in receipt.items()
                if key not in ("completion_evidence_name", "completion_evidence_digest")}
    name, digest = write_evidence(controller, "container-private-matrix-complete", {
        "schema_version": certifier.SCHEMA,
        "receipt_sha256": hashlib.sha256(certifier.canonical_json(unsigned)).hexdigest(),
    })
    receipt["completion_evidence_name"] = name
    receipt["completion_evidence_digest"] = digest
    (root / "receipt.json").write_bytes(certifier.canonical_json(receipt))


@pytest.mark.parametrize("damage", (
    "zero", "fifth", "uncertain", "duplicate", "duplicate_cross_cell", "unbound",
    "epoch", "body", "effective_body", "disposition", "output_tokens", "wire_model",
    "route", "missing_send", "hidden_request", "hidden_send", "used_count",
))
def test_bounded_tool_loop_damage_stops_maka_before_construction(
    certifier, complete_receipt, tmp_path, monkeypatch, company_manifest, damage,
):
    root, controller, ledger = _completed_quality_root(
        certifier, complete_receipt, tmp_path, monkeypatch, main_requests=4)
    cell = complete_receipt["providers"]["codex"]["resume"]
    record = cell["ledger_requests"][1]
    if damage == "zero":
        cell["ledger_requests"] = []
        cell["broker_dispositions"] = []
        cell["broker_request_count"] = cell["broker_attempt_count"] = 0
    elif damage == "fifth":
        cell["ledger_requests"].append({**record, "request_id": "fifth"})
        cell["broker_dispositions"].append({"request_id": "fifth", "state": "settled",
                                            "response_sha256": record["response_digest"]})
        cell["broker_request_count"] = cell["broker_attempt_count"] = 5
    elif damage == "uncertain":
        record["state"] = "uncertain"
        record["response_digest"] = None
        cell["broker_dispositions"][1].update(state="uncertain", response_sha256=None)
    elif damage in ("duplicate", "duplicate_cross_cell"):
        duplicate = (cell["ledger_requests"][0]["request_id"] if damage == "duplicate" else
                     complete_receipt["providers"]["claude"]["initial"]["ledger_requests"][0]["request_id"])
        record["request_id"] = cell["broker_dispositions"][1]["request_id"] = duplicate
    elif damage == "unbound":
        record["permit_id"] = complete_receipt["providers"]["claude"]["initial"]["permit_id"]
    elif damage == "epoch":
        record["epoch"] = 1
    elif damage in ("body", "effective_body"):
        field = "body_sha256" if damage == "body" else "upstream_body_sha256"
        record["identity"][field] = "0" * 64
    elif damage == "disposition":
        record["response_digest"] = cell["broker_dispositions"][1]["response_sha256"] = "0" * 64
    elif damage == "output_tokens":
        record["identity"]["effective_output_tokens"] = 8193
    elif damage == "wire_model":
        record["identity"]["wire_model"] = "another-model"
    elif damage == "route":
        record["identity"]["upstream_route_digest"] = "0" * 64
    else:
        with ledger._locked() as durable:
            if damage == "missing_send":
                durable["send_claims"].pop(record["request_id"])
            elif damage == "used_count":
                durable["permits"][cell["permit_id"]]["used"] = 3
                # Preserve the MAC while deliberately violating the generic ledger's count invariant.
                ledger._record.write_bytes(certifier.canonical_json({
                    "payload": durable, "mac": ledger._mac(durable)}))
            else:
                hidden = cell["ledger_requests"].pop()
                cell["broker_dispositions"].pop()
                cell["broker_request_count"] = cell["broker_attempt_count"] = 3
                if damage == "hidden_request":
                    durable["send_claims"].pop(hidden["request_id"])
            if damage != "used_count":
                ledger._save(durable)
    _reseal_completed_receipt(certifier, root, controller, complete_receipt)
    assert not certifier.verify_private_route_evidence(root, company_manifest[2]).passed

    class NoMaka:
        def __init__(self, **_kwargs):
            pytest.fail("damaged non-first request constructed paid Maka transport")

    monkeypatch.setattr(certifier, "HarborMakaTransport", NoMaka)
    with pytest.raises(certifier.CertificationRejected, match="unproven"):
        certifier._quality_run(
            root, allow_paid=True, approval_reference="owner-quality-123",
            observation_date="2026-09-30", launcher=tmp_path / "unused-launcher",
            boundary_sha256="e" * 64)
    assert not (root / "quality-attempt.json").exists()


@pytest.mark.parametrize("damage", ("unknown", "unexplained", "unparseable"))
def test_unrecognized_or_unaccounted_auxiliary_cannot_complete_tool_loop(
    certifier, complete_receipt, tmp_path, monkeypatch, company_manifest, damage,
):
    root, _, _ = _completed_quality_root(
        certifier, complete_receipt, tmp_path, monkeypatch, main_requests=4,
        auxiliary_attempts=1 if damage == "unknown" else 0,
        auxiliary_path=("/v1beta/models/unknown-model:streamGenerateContent?alt=sse"
                        if damage == "unknown" else None),
        unexplained_attempts=int(damage == "unexplained"),
        unparseable_attempts=int(damage == "unparseable"))
    assert not certifier.verify_private_route_evidence(root, company_manifest[2]).passed


def test_verifier_rejects_duplicate_receipt_keys(
    certifier, complete_receipt, tmp_path, monkeypatch, company_manifest,
):
    root, _, _ = _completed_quality_root(
        certifier, complete_receipt, tmp_path, monkeypatch)
    receipt_file = root / "receipt.json"
    receipt_file.write_bytes(b'{"mode":"fake",' + receipt_file.read_bytes()[1:])
    assert not certifier.verify_private_route_evidence(root, company_manifest[2]).passed


def _quality_argv(root, tmp_path):
    return [
        "--private-run-root", str(root), "--quality-maka", "--allow-paid",
        "--quality-approval-ref", "owner-quality-123",
        "--observation-date", "2026-09-30",
        "--maka-launcher", str(tmp_path / "unused-launcher"),
        "--maka-boundary-sha256", "e" * 64,
    ]


@pytest.mark.parametrize("main_requests,auxiliary_attempts", ((1, 0), (4, 1)))
def test_quality_cli_requires_authentic_completed_cells_before_maka(
    certifier, complete_receipt, tmp_path, monkeypatch, main_requests, auxiliary_attempts,
):
    for name in certifier._LEGACY_KEY_ENVS:
        monkeypatch.setenv(name, "synthetic-never-forward")
    root, _, _ = _completed_quality_root(certifier, complete_receipt, tmp_path,
                                        monkeypatch, main_requests=main_requests,
                                        auxiliary_attempts=auxiliary_attempts)
    calls = []
    attempt = root / "quality-attempt.json"
    root_info = root.stat()
    attempt_parent_synced = []
    original_fsync = os.fsync

    def track_attempt_sync(fd):
        info = os.fstat(fd)
        if (attempt.exists() and stat.S_ISDIR(info.st_mode)
                and (info.st_dev, info.st_ino) == (root_info.st_dev, root_info.st_ino)):
            attempt_parent_synced.append(True)
        return original_fsync(fd)

    monkeypatch.setattr(certifier.os, "fsync", track_attempt_sync)

    class FakeMaka:
        target_id = "maka"
        no_tools_characterization_sha256 = "e" * 64

        def __init__(self, **_kwargs):
            assert attempt_parent_synced, "attempt filename was not durable before paid judge"
            assert all(name not in os.environ
                       for name in certifier._LEGACY_KEY_ENVS)

        def judge(self, _request):
            calls.append(1)
            return {"winner": "tie", "auth_route": "harbor-lab",
                    "requested_model": "gpt-6.1-sol", "observed_model": "gpt-6.1-sol",
                    "effort": "xhigh", "tool_calls": 0,
                    "tool_boundary_sha256": self.no_tools_characterization_sha256}

    monkeypatch.setattr(certifier, "HarborMakaTransport", FakeMaka)
    status = certifier.main(_quality_argv(root, tmp_path))
    assert status == 0
    assert len(calls) == 6
    assert (root / "receipt.json").read_bytes() == certifier.canonical_json(complete_receipt)
    quality = __import__("json").loads((root / "quality.json").read_bytes())
    assert quality["isolation_passed"] is True
    assert len(quality["ballot_digest"]) == 64
    assert len(quality["ballots"]) == 6
    assert {(ballot["case_id"], ballot["order"]) for ballot in quality["ballots"]} == {
        (f"container-probe-{provider}", order)
        for provider in certifier.PROVIDERS for order in ("candidate-first", "baseline-first")}
    assert quality["release_source_binding"] == certifier.release_source_binding().to_dict()
    assert certifier.read_evidence(certifier._quality_controller(root),
        "container-maka-quality-complete", quality["completion_evidence_name"],
        quality["completion_evidence_digest"])["quality_sha256"] == hashlib.sha256(
            certifier.canonical_json({key: value for key, value in quality.items()
                if key not in {"completion_evidence_name", "completion_evidence_digest"}})).hexdigest()
    assert (root / "quality-attempt.json").stat().st_mode & 0o777 == 0o600


def test_quality_attempt_parent_sync_failure_refuses_paid_judge(
    certifier, complete_receipt, tmp_path, monkeypatch,
):
    root, _, _ = _completed_quality_root(certifier, complete_receipt, tmp_path,
                                        monkeypatch)
    attempt = root / "quality-attempt.json"
    root_info = root.stat()
    original_fsync = os.fsync

    def fail_attempt_sync(fd):
        info = os.fstat(fd)
        if (attempt.exists() and stat.S_ISDIR(info.st_mode)
                and (info.st_dev, info.st_ino) == (root_info.st_dev, root_info.st_ino)):
            raise OSError("synthetic directory sync failure")
        return original_fsync(fd)

    monkeypatch.setattr(certifier.os, "fsync", fail_attempt_sync)

    class FakeMaka:
        def __init__(self, **_kwargs):
            pytest.fail("paid judge constructed after failed attempt directory sync")

    monkeypatch.setattr(certifier, "HarborMakaTransport", FakeMaka)
    with pytest.raises(certifier.CertificationRejected):
        certifier._quality_run(
            root, allow_paid=True, approval_reference="owner-quality-123",
            observation_date="2026-09-30", launcher=tmp_path / "unused-launcher",
            boundary_sha256="e" * 64)
    assert attempt.exists()
    assert not (root / "quality.json").exists()


def test_quality_cli_refuses_plain_forged_receipt_before_paid_judge(
    certifier, complete_receipt, tmp_path, monkeypatch,
):
    root = tmp_path / "forged"
    root.mkdir(mode=0o700)
    answer = root / "answer-claude.bin"
    digest = certifier._write_private(answer, b"caller-authored answer")
    complete_receipt["private_run_root"] = str(root)
    complete_receipt["answers"] = {"claude": {
        "path": str(answer), "sha256": digest}}
    certifier._write_private(root / "receipt.json", certifier.canonical_json(complete_receipt))

    class FakeMaka:
        def __init__(self, **_kwargs):
            pytest.fail("plain forged receipt constructed paid launcher")

    monkeypatch.setattr(certifier, "HarborMakaTransport", FakeMaka)
    with pytest.raises(certifier.CertificationRejected):
        certifier._quality_run(
            root, allow_paid=True, approval_reference="owner-quality-123",
            observation_date="2026-09-30", launcher=tmp_path / "unused-launcher",
            boundary_sha256="e" * 64)
    assert not (root / "quality.json").exists()


@pytest.mark.parametrize("damage", (
    "missing_completion", "altered_completion", "receipt_changed",
    "ledger_changed", "failed_isolation", "answer_changed",
    "answer_missing", "answer_ref_unbound", "missing_capability", "quality_exists",
    "quality_attempt_exists",
))
def test_quality_cli_rejects_unproven_or_failed_matrix_before_paid_judge(
    certifier, complete_receipt, tmp_path, monkeypatch, damage,
):
    root, controller, ledger = _completed_quality_root(
        certifier, complete_receipt, tmp_path, monkeypatch)
    if damage in ("missing_completion", "altered_completion"):
        evidence = controller.root / "receipts" / complete_receipt["completion_evidence_name"]
        if damage == "missing_completion":
            evidence.unlink()
        else:
            evidence.write_bytes(b"{}")
    elif damage == "receipt_changed":
        complete_receipt["approval_reference"] = "forged-owner-reference"
        (root / "receipt.json").write_bytes(certifier.canonical_json(complete_receipt))
    elif damage == "ledger_changed":
        ledger._record.write_bytes(b"{}")
    elif damage == "failed_isolation":
        complete_receipt["providers"]["agy"]["initial"]["isolation"]["caller"]["read_denied"] = False
        (root / "receipt.json").write_bytes(certifier.canonical_json(complete_receipt))
    elif damage == "answer_ref_unbound":
        complete_receipt["providers"]["codex"]["resume"]["answer_sha256"] = "f" * 64
        unsigned = {key: value for key, value in complete_receipt.items()
                    if key not in ("completion_evidence_name", "completion_evidence_digest")}
        name, digest = write_evidence(controller, "container-private-matrix-complete", {
            "schema_version": certifier.SCHEMA,
            "receipt_sha256": hashlib.sha256(
                certifier.canonical_json(unsigned)).hexdigest(),
        })
        complete_receipt["completion_evidence_name"] = name
        complete_receipt["completion_evidence_digest"] = digest
        (root / "receipt.json").write_bytes(certifier.canonical_json(complete_receipt))
    elif damage == "missing_capability":
        root.with_name(root.name + ".controller-capability").unlink()
    elif damage == "quality_exists":
        certifier._write_private(root / "quality.json", b"existing quality result")
    elif damage == "quality_attempt_exists":
        certifier._write_private(root / "quality-attempt.json", b"reserved quality attempt")
    elif damage == "answer_missing":
        (root / "answer-codex.bin").unlink()
    else:
        (root / "answer-codex.bin").write_bytes(b"forged answer")

    class FakeMaka:
        def __init__(self, **_kwargs):
            pytest.fail("unproven quality receipt constructed paid launcher")

        def judge(self, _request):
            pytest.fail("unproven quality receipt reached paid judge")

    monkeypatch.setattr(certifier, "HarborMakaTransport", FakeMaka)
    with pytest.raises(certifier.CertificationRejected):
        certifier._quality_run(
            root, allow_paid=True, approval_reference="owner-quality-123",
            observation_date="2026-09-30", launcher=tmp_path / "unused-launcher",
            boundary_sha256="e" * 64)
    if damage == "quality_exists":
        assert (root / "quality.json").read_bytes() == b"existing quality result"
    else:
        assert not (root / "quality.json").exists()


def test_quality_reads_only_completed_private_answer_artifacts(certifier, complete_receipt,
                                                               tmp_path):
    answer = tmp_path / "answer.txt"
    answer.write_text("synthetic answer")
    answer.chmod(0o600)
    complete_receipt["private_run_root"] = str(tmp_path)
    complete_receipt["answers"] = {
        "claude": {"path": str(answer),
                   "sha256": __import__("hashlib").sha256(answer.read_bytes()).hexdigest()}
    }

    class FakeMaka:
        target_id = "maka"
        no_tools_characterization_sha256 = "e" * 64

        def __init__(self):
            self.orders = []

        def judge(self, request):
            self.orders.append((request.answer_a, request.answer_b))
            return {"winner": "tie", "auth_route": "harbor-lab",
                    "requested_model": "gpt-6.1-sol", "observed_model": "gpt-6.1-sol",
                    "effort": "xhigh", "tool_calls": 0,
                    "tool_boundary_sha256": self.no_tools_characterization_sha256}

    maka = FakeMaka()
    quality = certifier.assess_quality(complete_receipt, maka)
    assert quality.isolation_passed
    assert maka.orders == [("synthetic answer", "baseline synthetic answer"),
                           ("baseline synthetic answer", "synthetic answer")]
    assert len(quality.ballot_digest) == 64


def test_quality_refuses_answer_outside_private_root(certifier, complete_receipt, tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    caller_answer = tmp_path / "caller-answer.txt"
    caller_answer.write_text("caller content")
    caller_answer.chmod(0o600)
    complete_receipt["private_run_root"] = str(private)
    complete_receipt["answers"] = {"claude": {
        "path": str(caller_answer),
        "sha256": __import__("hashlib").sha256(caller_answer.read_bytes()).hexdigest(),
    }}

    class FakeMaka:
        target_id = "maka"
        no_tools_characterization_sha256 = "e" * 64

        def judge(self, _request):
            raise AssertionError("outside answer reached Maka")

    with pytest.raises(certifier.CertificationRejected, match="outside"):
        certifier.assess_quality(complete_receipt, FakeMaka())


def test_quality_skips_an_incomplete_provider_answer(certifier, complete_receipt, tmp_path):
    answer = tmp_path / "answer.txt"
    answer.write_text("incomplete answer")
    answer.chmod(0o600)
    complete_receipt["providers"]["claude"]["resume"]["passed"] = False
    complete_receipt["private_run_root"] = str(tmp_path)
    complete_receipt["answers"] = {"claude": {
        "path": str(answer),
        "sha256": __import__("hashlib").sha256(answer.read_bytes()).hexdigest(),
    }}

    class FakeMaka:
        target_id = "maka"
        no_tools_characterization_sha256 = "e" * 64

        def judge(self, _request):
            raise AssertionError("incomplete answer reached Maka")

    quality = certifier.assess_quality(complete_receipt, FakeMaka())
    assert not quality.isolation_passed


def test_no_release_key_option_or_public_gate_side_effect(certifier, tmp_path):
    with pytest.raises(SystemExit) as error:
        certifier.main(["--private-run-root", str(tmp_path / "private"),
                        "--issue-release-key", str(tmp_path / "release.key")])
    assert error.value.code == 2
    assert not (tmp_path / "release.key").exists()


def test_fake_route_uses_shared_turn_broker_and_cold_session_without_certifying(
    certifier, tmp_path,
):
    private = tmp_path / "private-fake"
    receipt = certifier.certification_run(
        private / "targets" / "repo-alpha", private, fake_route=True,
    )
    assert receipt["mode"] == "fake"
    assert receipt["source_sha256"] == certifier.runtime_source_digest(certifier._SOURCE_ROOT)
    assert not certifier.evaluate_private_probe(receipt).passed
    assert sorted(receipt["providers"]) == ["agy", "claude", "codex"]
    for provider, cells in receipt["providers"].items():
        initial, resume = cells["initial"], cells["resume"]
        assert initial["fake_terminal_valid"] and resume["fake_terminal_valid"]
        assert initial["source_sha256"] == resume["source_sha256"] == receipt["source_sha256"]
        assert initial["session_id"] == resume["session_id"]
        assert initial["session_volume"] == resume["session_volume"]
        assert initial["permit_id"] != resume["permit_id"]
        assert (initial["permit_epoch"], resume["permit_epoch"]) == (1, 2)
        assert initial["broker_request_count"] == resume["broker_request_count"] == 1
        assert initial["broker_dispositions"][0]["state"] == "settled"
        assert resume["broker_dispositions"][0]["state"] == "settled"
        assert initial["seat_stopped"] and resume["seat_stopped"]
        assert initial["permit_fenced"] and resume["permit_fenced"]
        assert initial["fake_http"][0]["method"] == "POST"
        assert resume["fake_http"][0]["method"] == "POST"
        assert initial["fake_sse_sha256"] == resume["fake_sse_sha256"]
        for route in ("initial", "resume"):
            for artifact in cells[route]["raw_artifacts"].values():
                path = Path(artifact)
                assert path.is_file()
                assert path.parent == private / "raw"
                assert path.stat().st_mode & 0o777 == 0o600
    assert (private / "targets" / "repo-alpha" / "README.md").is_file()
    assert (private / "targets" / "repo-beta" / "README.md").is_file()
    for cells in receipt["providers"].values():
        for cell in cells.values():
            assert (Path(cell["workspace_cwd"]) / "README.md").is_file()
            assert (Path(cell["candidate_export_root"]) / "README.md").is_file()
    assert (private / "receipt.json").stat().st_mode & 0o777 == 0o600


def test_fake_http_crosses_loopback_unix_and_stdio_relays(certifier, tmp_path):
    private = tmp_path / "http-route"
    receipt = certifier.certification_run(
        private / "targets" / "repo-alpha", private, fake_route=True,
    )
    for cells in receipt["providers"].values():
        for cell in cells.values():
            assert cell["fake_http_transport"] == "loopback-unix-stdio"
            assert cell["host_relay_completed"] is True
            assert cell["fake_http_status"] == 200
            assert cell["fake_sse_sha256"] == hashlib.sha256(
                b'data: {"type":"synthetic"}\n\ndata: [DONE]\n\n'
            ).hexdigest()


def test_fake_agy_uses_measured_wire_model_with_cli_alias(certifier, tmp_path):
    private = tmp_path / "agy-wire"
    receipt = certifier.certification_run(
        private / "targets" / "repo-alpha", private, fake_route=True,
    )
    assert receipt["mode"] == "fake"
    assert not certifier.evaluate_private_probe(receipt).passed
    for cell in receipt["providers"]["agy"].values():
        assert cell["requested_model"] == "gemini-3.8-flash-high"
        assert cell["fake_http_status"] == 200
        assert cell["fake_http"] == [{
            "method": "POST",
            "path": "/v1beta/models/gemini-3.8-flash:streamGenerateContent?alt=sse",
            "model": "gemini-3.8-flash",
            "header_names": ["content-type", "x-goog-api-key"],
        }]


def test_missing_source_closure_refuses_before_creating_private_root(
    certifier, tmp_path, monkeypatch,
):
    source_root = tmp_path / "incomplete-source"
    source_root.mkdir()
    monkeypatch.setattr(certifier, "_SOURCE_ROOT", source_root)
    private = tmp_path / "missing-source"
    with pytest.raises(certifier.CertificationRejected, match="source|closure|required"):
        certifier.certification_run(
            private / "targets" / "repo-alpha", private, fake_route=True,
        )
    assert not private.exists()


def test_fake_turn_reads_published_alpha_and_exposes_sibling_limit(certifier, tmp_path):
    private = tmp_path / "transfer-route"
    receipt = certifier.certification_run(
        private / "targets" / "repo-alpha", private, fake_route=True,
    )
    expected = hashlib.sha256(
        (private / "targets" / "repo-alpha" / "README.md").read_bytes()
    ).hexdigest()
    sibling = private / "targets" / "repo-beta" / "README.md"
    for provider, cells in receipt["providers"].items():
        for cell in cells.values():
            published = Path(cell["workspace_cwd"])
            assert private / "transfer-alpha" / provider in published.parents
            assert cell["container_workdir"].startswith("/workspace/inputs/")
            assert cell["container_workdir"].endswith("/published/repo")
            assert (published / "README.md").is_file()
            assert (published.parent / "context" / "full-plan.md").is_file()
            assert (published.parent / "context" / "skills" / "manifest.json").is_file()
            assert (published.parent / "context" / "dependencies" / "peer.json").is_file()
            assert cell["workspace_input_sha256"] == expected
            assert len(cell["input_manifest_sha256"]) == 64
            assert len(cell["input_receipt_digest"]) == 64
            assert Path(cell["candidate_export_root"]).parent.name.startswith("candidate-")
            assert (Path(cell["candidate_export_root"]) / "README.md").read_bytes() == (
                published / "README.md").read_bytes()
            assert cell["sibling_probe"]["attempted"] is True
            assert cell["sibling_probe"]["read_denied"] is False
            assert cell["sibling_probe"]["write_denied"] is False
            assert cell["sibling_probe"]["observed_sha256"] == hashlib.sha256(
                sibling.read_bytes()
            ).hexdigest()
            assert cell["isolation"]["sibling"]["read_denied"] is False


def _stopped_fake_predecessor(certifier, tmp_path):
    controller = create_lifecycle_controller(tmp_path / "controller")
    ledger = PermitLedger(controller.root / "locks" / "spend", controller)
    identity = ("fake-recovery", "task", "target", "seat-claude")
    workspace, session = certifier.private_volume_names(*identity)
    spec = certifier.ContainerSpec(*identity, "sha256:" + "1" * 64,
                                   workspace, session, "/run/fanout/seat-claude.sock",
                                   "2" * 64)
    permit = BrokerPermit("claude", *identity, "initial", "claude-opus-5-5",
                          "disposable-placeholder", 1, 100_000, 100_000, 1024)
    permit_id = ledger.register(permit, 1)
    launcher = certifier._FakeLauncher(spec, "initial")
    transport = certifier.PinnedDockerTransport.start(
        controller, spec, launcher=launcher, vm_attestor=lambda: None,
        socket_attestor=lambda _: None, image_attestor=lambda: spec.image_digest,
        cli_attestor=lambda _: spec.cli_digest, process_runner=lambda _: None)
    transport.close()
    ledger.fence_epoch(permit_id, 1)
    name, digest = write_evidence(controller, "container-fake-stopped", {
        "container_id": transport.handle.container_id, "seat_id": spec.seat_id,
        "session_volume": session, "permit_id": permit_id, "permit_epoch": 1,
        "stopped": not launcher.running, "broker_state": "settled",
        "native_stop_evidence_name": transport.stop_evidence[0],
        "native_stop_evidence_digest": transport.stop_evidence[1],
    })
    previous = {"container_id": transport.handle.container_id,
                "permit_id": permit_id, "permit_epoch": 1, "stop_evidence_name": name,
                "stop_evidence_digest": digest}
    return controller, ledger, spec, previous, transport


def test_fake_recovery_binds_native_retained_predecessor(certifier, tmp_path):
    from shared.lib.fanout.container_runtime import _RecoveryAssociation, decode_launch_record
    controller, ledger, spec, previous, initial = _stopped_fake_predecessor(
        certifier, tmp_path)
    stopped = read_evidence(controller, "container-seat-stopped", *initial.stop_evidence)
    launcher = certifier._FakeLauncher(spec, "resume", controller=controller,
                                       ledger=ledger, previous=previous)
    resumed = certifier.PinnedDockerTransport.start(
        controller, spec, launcher=launcher, vm_attestor=lambda: None,
        socket_attestor=lambda _: None, image_attestor=lambda: spec.image_digest,
        cli_attestor=lambda _: spec.cli_digest, process_runner=lambda _: None,
        recover=True)
    assert resumed.handle.container_id != initial.handle.container_id
    assert type(launcher._recovery) is _RecoveryAssociation
    assert launcher._recovery.predecessors == (
        (initial.handle.container_id, stopped["raw_inspect"]["digest"]),)
    launch = read_evidence(controller, "container-seat-launch",
                           resumed.handle_evidence_name, resumed.handle_evidence_digest)
    assert decode_launch_record(launch, spec, resumed.handle, recover=True) == launcher._recovery
    resumed.close()
    launcher.start(spec)
    assert launcher._recovery is None
    assert launcher.recovered_prior_container_id is None


@pytest.mark.parametrize("changed", ("stop_auth", "launch_auth", "launch_handle",
                                      "raw_inspect", "seat", "session", "permit",
                                      "stopped", "settled", "fenced", "epoch",
                                      "other_permit"))
def test_fake_recovery_refuses_changed_retained_predecessor(
    certifier, tmp_path, changed,
):
    controller, ledger, spec, previous, initial = _stopped_fake_predecessor(
        certifier, tmp_path)
    stopped = read_evidence(controller, "container-fake-stopped",
                            previous["stop_evidence_name"], previous["stop_evidence_digest"])
    native = read_evidence(controller, "container-seat-stopped", *initial.stop_evidence)
    if changed == "stop_auth":
        stopped["native_stop_evidence_digest"] = "0" * 64
    elif changed == "launch_auth":
        native["launch_evidence_digest"] = "0" * 64
    elif changed == "launch_handle":
        native["handle"]["container_id"] = "0" * 64
    elif changed == "raw_inspect":
        path = controller.root / "quarantine" / "runtime-proofs" / native["raw_inspect"]["path"]
        path.write_bytes(path.read_bytes().replace(b'"Running": false', b'"Running": true'))
    elif changed in ("seat", "session", "permit"):
        stopped[{"seat": "seat_id", "session": "session_volume", "permit": "permit_id"}[changed]] = "other"
    elif changed == "stopped":
        stopped["stopped"] = False
    elif changed == "settled":
        stopped["broker_state"] = "uncertain"
    elif changed == "fenced":
        with ledger._locked() as durable:
            durable["permits"][previous["permit_id"]]["fenced"] = False
            ledger._save(durable)
    elif changed == "epoch":
        previous["permit_epoch"] = stopped["permit_epoch"] = 2
    elif changed == "other_permit":
        other = BrokerPermit("claude", spec.run_id, spec.task_id, spec.target_id,
                             "seat-other", "initial", "claude-opus-5-5",
                             "disposable-placeholder", 1, 100_000, 100_000, 1024)
        other_id = ledger.register(other, 1)
        ledger.fence_epoch(other_id, 1)
        previous["permit_id"] = stopped["permit_id"] = other_id
    if changed in ("launch_auth", "launch_handle"):
        stopped["native_stop_evidence_name"], stopped["native_stop_evidence_digest"] = write_evidence(
            controller, "container-seat-stopped", native)
    previous["stop_evidence_name"], previous["stop_evidence_digest"] = write_evidence(
        controller, "container-fake-stopped", stopped)
    launcher = certifier._FakeLauncher(spec, "resume", controller=controller,
                                       ledger=ledger, previous=previous)
    launcher._recovery = object()
    with pytest.raises(certifier.CertificationRejected):
        launcher.recover(spec)
    assert launcher._recovery is None
    assert launcher.recovered_prior_container_id is None


def test_fake_resume_reopens_controller_and_attests_stopped_prior_seat(certifier, tmp_path):
    private = tmp_path / "cold-route"
    receipt = certifier.certification_run(
        private / "targets" / "repo-alpha", private, fake_route=True,
    )
    for cells in receipt["providers"].values():
        initial, resume = cells["initial"], cells["resume"]
        assert initial["stop_evidence_digest"]
        assert resume["cold_controller_reopened"] is True
        assert resume["recovered_prior_container_id"] == initial["container_id"]
        assert resume["container_id"] != initial["container_id"]


def test_interrupted_response_remains_uncertain_after_cold_reopen(certifier, tmp_path):
    private = tmp_path / "interrupted-route"
    receipt = certifier.certification_run(
        private / "targets" / "repo-alpha", private, fake_route=True,
    )
    probe = receipt["interruption_probe"]
    assert probe["upstream_send_count"] == 1
    assert probe["disposition"] == "uncertain"
    assert probe["cold_controller_reopened"] is True
    assert probe["duplicate_request_refused"] is True
    assert probe["uncertain_resume_refused"] is True


def test_fake_cli_exit_cannot_be_confused_with_certification(certifier, tmp_path,
                                                              capsys):
    private = tmp_path / "fake-cli"
    exit_status = certifier.main([
        "--private-run-root", str(private), "--fake-route",
    ])
    assert exit_status != 0
    assert '"passed": false' in capsys.readouterr().out
    assert (private / "receipt.json").is_file()
