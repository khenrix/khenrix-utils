"""Private machine-route doctor, using invented owner files and no model calls."""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_fanout_container_certify import (
    company_manifest, complete_receipt, certifier, _completed_quality_root,
    _requested_company_routes,
)
from shared.lib.fanout.container_credentials import RouteCredentialError


@pytest.fixture
def doctor(monkeypatch):
    module = importlib.import_module("scripts.fanout_route_doctor")

    def unavailable(_route):
        raise RouteCredentialError("inert test source is unavailable")

    monkeypatch.setattr(module, "HostCredentialSource", unavailable)
    return module


def test_doctor_reports_auth_without_model_proof(
    doctor, company_manifest, monkeypatch, capsys,
):
    class FakeSource:
        def __init__(self, _route):
            self.fingerprint = None

        def load(self):
            self.fingerprint = "9" * 64
            return "invented-secret-never-print"

    monkeypatch.setattr(doctor, "HostCredentialSource", FakeSource)
    path, policy, _ = company_manifest
    report = doctor.diagnose_routes(path, policy, _requested_company_routes())
    assert report["summary"] == "model_unverified"
    for cell in report["providers"].values():
        assert cell["configured_policy"] is True
        assert cell["credential_source_reachable"] is True
        assert cell["completed_model_call_proof"] is None
        assert cell["model_proof_state"] == "unassessed"
        assert len(cell["machine_route_digest"]) == 64
    assert doctor.main(["--machine-routes", str(path), "--machine-policy", str(policy),
                        *(argument for provider, route in _requested_company_routes().items()
                          for argument in ("--route-id", f"{provider}={route}"))]) == 0
    output = capsys.readouterr().out
    assert "invented-secret-never-print" not in output
    assert "invented-vertex-project" not in output
    assert "invented-openai-project" not in output
    assert "invented-account" not in output


def test_doctor_needs_auth_when_source_is_unreachable(
    doctor, company_manifest, monkeypatch,
):
    class MissingSource:
        def __init__(self, _route):
            raise RouteCredentialError("invented-secret-never-print")

    monkeypatch.setattr(doctor, "HostCredentialSource", MissingSource)
    path, policy, _ = company_manifest
    report = doctor.diagnose_routes(path, policy, _requested_company_routes())
    assert report["summary"] == "needs_auth"
    assert all(cell["configured_policy"] for cell in report["providers"].values())
    assert all(not cell["credential_source_reachable"]
               for cell in report["providers"].values())
    assert "invented-secret-never-print" not in json.dumps(report)


def test_doctor_rejects_subscription_for_protected_repo_write(
    doctor, company_manifest, monkeypatch,
):
    path, policy, _ = company_manifest
    policy_doc = json.loads(policy.read_bytes())
    subscription = {
        "route_id": "codex-subscription", "provider": "codex",
        "auth_family": "codex-subscription", "api_family": "codex-subscription",
        "origin": "https://api.openai.com", "project": None, "location": None,
        "requested_model": "gpt-6.1-sol", "wire_model": "gpt-6.1-sol",
        "effective_model": "gpt-6.1-sol", "credential_ref": "adapter:codex",
        "max_data_classification": "non-sensitive",
        "execution_classes": ["read-only"], "transform_version": 1,
    }
    policy_doc["routes"].append({k: v for k, v in subscription.items()
                                 if k != "credential_ref"})
    encoded_policy = (json.dumps(policy_doc, sort_keys=True,
                                 separators=(",", ":")) + "\n").encode()
    policy.write_bytes(encoded_policy)
    manifest_doc = json.loads(path.read_bytes())
    manifest_doc["policy_digest"] = __import__("hashlib").sha256(encoded_policy).hexdigest()
    manifest_doc["routes"].append(subscription)
    path.write_bytes((json.dumps(manifest_doc, sort_keys=True,
                           separators=(",", ":")) + "\n").encode())
    requested = _requested_company_routes()
    requested["codex"] = "codex-subscription"
    monkeypatch.setenv("CODEX_CLI_LOGGED_IN", "true")
    report = doctor.diagnose_routes(path, policy, requested)
    assert report["summary"] == "blocked_policy"
    assert report["providers"]["codex"]["configured_policy"] is True
    assert report["providers"]["codex"]["protected_repo_write_eligible"] is False
    assert report["providers"]["codex"]["adapter_available"] is False
    assert report["providers"]["codex"]["credential_state"] == "unassessed"
    assert report["providers"]["codex"]["completed_model_call_proof"] is None
    assert report["profile_coverage"] == {"scope": "selected", "selected": 3, "manifest": 5}


def test_doctor_does_not_transfer_certificate_to_alternate_approved_route(
    doctor, certifier, complete_receipt, company_manifest, tmp_path, monkeypatch,
):
    root, _, _ = _completed_quality_root(
        certifier, complete_receipt, tmp_path, monkeypatch)

    class FakeSource:
        def __init__(self, _route):
            self.fingerprint = None

        def load(self):
            self.fingerprint = "9" * 64
            return "invented-secret"

    monkeypatch.setattr(doctor, "HostCredentialSource", FakeSource)
    requested = _requested_company_routes()
    requested["codex"] = "codex-alternate"
    report = doctor.diagnose_routes(
        company_manifest[0], company_manifest[1], requested, private_root=root)
    assert report["summary"] == "model_unverified"
    assert report["providers"]["codex"]["completed_model_call_proof"] is False
    assert report["providers"]["codex"]["model_proof_state"] == "route_mismatch"
    for provider in ("claude", "agy"):
        assert report["providers"][provider]["completed_model_call_proof"] is True
        assert report["providers"][provider]["model_proof_state"] == "matching"
    assert report["route_set_proof_state"] == "route_mismatch"
    assert report["certification_state"] == "unassessed"


def _doctor_sources(doctor, monkeypatch, *, unavailable=()):
    calls = []

    class Source:
        def __init__(self, route):
            self.provider = route.provider
            self.fingerprint = None

        def load(self):
            calls.append(self.provider)
            if self.provider in unavailable:
                raise RouteCredentialError("invented-private-error")
            self.fingerprint = "9" * 64
            return "invented-private-value"

    monkeypatch.setattr(doctor, "HostCredentialSource", Source)
    return Source, calls


def test_matching_authenticated_matrix_is_model_proof_not_a_release_certificate(
    doctor, certifier, complete_receipt, company_manifest, tmp_path, monkeypatch,
):
    root, _, ledger = _completed_quality_root(
        certifier, complete_receipt, tmp_path, monkeypatch)
    source, calls = _doctor_sources(doctor, monkeypatch)
    monkeypatch.setattr(certifier, "HostCredentialSource", source)
    before = ledger._record.read_bytes()
    report = doctor.diagnose_routes(*company_manifest[:2], _requested_company_routes(),
                                    private_root=root)
    assert report["summary"] == "model_verified"
    assert report["route_set_proof_state"] == "matching"
    assert report["certification_state"] == "unassessed"
    assert report["profile_coverage"] == {"scope": "selected", "selected": 3, "manifest": 4}
    for cell in report["providers"].values():
        assert cell["configured_policy"] is True
        assert cell["protected_repo_write_eligible"] is True
        assert cell["adapter_available"] is True
        assert cell["credential_state"] == "reachable"
        assert cell["model_proof_state"] == "matching"
        assert cell["completed_model_call_proof"] is True
        assert cell["certification_state"] == "unassessed"
    assert calls == ["claude", "codex", "agy", "claude", "codex", "agy"]
    assert ledger._record.read_bytes() == before
    assert "invented-private" not in json.dumps(report)


def test_mixed_current_credentials_make_original_authenticated_reader_unavailable(
    doctor, certifier, complete_receipt, company_manifest, tmp_path, monkeypatch,
):
    root, _, ledger = _completed_quality_root(
        certifier, complete_receipt, tmp_path, monkeypatch)
    source, calls = _doctor_sources(doctor, monkeypatch, unavailable=("codex",))
    monkeypatch.setattr(certifier, "HostCredentialSource", source)
    before = ledger._record.read_bytes()
    report = doctor.diagnose_routes(*company_manifest[:2], _requested_company_routes(),
                                    private_root=root)
    assert report["summary"] == "needs_auth"
    assert report["route_set_proof_state"] == "unavailable"
    assert [cell["credential_source_reachable"] for cell in report["providers"].values()] == [
        True, False, True]
    assert calls == ["claude", "codex", "agy", "claude", "codex"]
    for cell in report["providers"].values():
        assert cell["model_proof_state"] == "unavailable"
        assert cell["completed_model_call_proof"] is None
        assert cell["certification_state"] == "unassessed"
    assert ledger._record.read_bytes() == before
    assert "invented-private" not in json.dumps(report)


def test_disabled_credential_checks_leave_auth_and_model_proof_unassessed(
    doctor, company_manifest, monkeypatch, tmp_path,
):
    def forbidden(*_args, **_kwargs):
        pytest.fail("disabled checks must not load credentials or authenticated proof")

    monkeypatch.setattr(doctor, "HostCredentialSource", forbidden)
    monkeypatch.setattr(doctor, "read_private_route_evidence", forbidden)
    report = doctor.diagnose_routes(*company_manifest[:2], _requested_company_routes(),
                                    private_root=tmp_path, check_credentials=False)
    assert report["summary"] == "configured"
    assert report["route_set_proof_state"] == "unassessed"
    for cell in report["providers"].values():
        assert cell["credential_source_reachable"] is None
        assert cell["credential_state"] == "unassessed"
        assert cell["completed_model_call_proof"] is None
        assert cell["model_proof_state"] == "unassessed"
        assert cell["certification_state"] == "unassessed"


def test_original_authenticated_reader_refusal_is_not_absent_model_evidence(
    doctor, certifier, complete_receipt, company_manifest, tmp_path, monkeypatch,
):
    root, _, ledger = _completed_quality_root(
        certifier, complete_receipt, tmp_path, monkeypatch)
    source, _ = _doctor_sources(doctor, monkeypatch)
    monkeypatch.setattr(certifier, "HostCredentialSource", source)
    complete_receipt["approval_reference"] = "invented-tampered-reference"
    raw = certifier.canonical_json(complete_receipt)
    (root / "receipt.json").write_bytes(raw)
    ledger_before = ledger._record.read_bytes()
    report = doctor.diagnose_routes(*company_manifest[:2], _requested_company_routes(),
                                    private_root=root)
    assert report["summary"] == "model_unverified"
    assert report["route_set_proof_state"] == "unavailable"
    assert all(cell["completed_model_call_proof"] is None
               and cell["model_proof_state"] == "unavailable"
               for cell in report["providers"].values())
    assert (root / "receipt.json").read_bytes() == raw
    assert ledger._record.read_bytes() == ledger_before
    assert "invented-tampered-reference" not in json.dumps(report)
