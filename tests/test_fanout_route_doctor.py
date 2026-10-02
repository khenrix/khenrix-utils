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
def doctor():
    return importlib.import_module("scripts.fanout_route_doctor")


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
        assert cell["completed_model_call_proof"] is False
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
    assert report["providers"]["codex"]["configured_policy"] is False
    assert report["providers"]["codex"]["completed_model_call_proof"] is False


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
