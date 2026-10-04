"""Private machine-route status without model calls or account identifiers."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Mapping

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.lib.fanout.container_credentials import HostCredentialSource, RouteCredentialError
from shared.lib.fanout.container_routes import (
    RouteAdmission, RouteRejected, read_machine_routes, select_route,
)
from shared.lib.fanout.providers import ProviderRegistry
from shared.lib.fanout.container_certification import CertificationRejected, read_private_route_evidence


PROVIDERS = ("claude", "codex", "agy")


def diagnose_routes(manifest_path: Path, policy_path: Path,
                    requested: Mapping[str, str], *,
                    private_root: Path | None = None,
                    check_credentials: bool = True) -> dict[str, object]:
    """Return only route digests and separate policy, auth, and model proof states."""
    report: dict[str, object] = {"summary": "blocked_policy",
        "profile_coverage": {"scope": "selected", "selected": 0, "manifest": None},
        "route_set_proof_state": "unassessed", "certification_state": "unassessed",
        "providers": {
        provider: {"configured_policy": False,
                   "protected_repo_write_eligible": False, "adapter_available": False,
                   "credential_source_reachable": None, "credential_state": "unassessed",
                   "completed_model_call_proof": None, "model_proof_state": "unassessed",
                   "certification_state": "unassessed",
                   "machine_route_digest": None}
        for provider in PROVIDERS}}
    if not isinstance(requested, Mapping) or set(requested) != set(PROVIDERS):
        return report
    try:
        manifest = read_machine_routes(manifest_path, policy_path)
    except (RouteRejected, TypeError, ValueError, OSError):
        return report
    report["manifest_digest"] = manifest.manifest_digest
    report["policy_digest"] = manifest.policy_digest
    report["profile_coverage"]["manifest"] = len(manifest.routes)
    registry = ProviderRegistry.default(version_probe=lambda _: "unused")
    selected = {}
    for provider in PROVIDERS:
        cell = report["providers"][provider]
        matches = [profile for profile in manifest.routes
                   if profile.route_id == requested[provider] and profile.provider == provider
                   and profile.requested_model == registry.select(
                       provider, "repo-write", "standard").requested_model]
        if len(matches) != 1:
            continue
        route = RouteAdmission(manifest.machine_id, manifest.policy_digest,
                               manifest.manifest_digest, matches[0])
        selected[provider] = route
        cell["configured_policy"] = True
        cell["machine_route_digest"] = route.digest
        cell["adapter_available"] = route.profile.auth_family in {
            "vertex-adc", "openai-keychain", "openai-1password"}
        try:
            select_route(
                manifest, route_id=requested[provider], provider=provider,
                requested_model=registry.select(provider, "repo-write", "standard").requested_model,
                execution_class="repo-write", data_classification="protected")
        except (RouteRejected, TypeError, ValueError):
            continue
        cell["protected_repo_write_eligible"] = True
        if not check_credentials or not cell["adapter_available"]:
            continue
        cell["credential_source_reachable"] = False
        cell["credential_state"] = "unavailable"
        try:
            source = HostCredentialSource(route.profile)
            source.load()
            cell["credential_source_reachable"] = (
                isinstance(source.fingerprint, str) and len(source.fingerprint) == 64)
            if cell["credential_source_reachable"]:
                cell["credential_state"] = "reachable"
        except RouteCredentialError:
            pass
    report["profile_coverage"]["selected"] = len(selected)
    eligible = all(cell["protected_repo_write_eligible"] and cell["adapter_available"]
                   for cell in report["providers"].values())
    reachable = all(cell["credential_source_reachable"] is True
                    for cell in report["providers"].values())
    if eligible:
        report["summary"] = ("configured" if not check_credentials else
                             "model_unverified" if reachable else "needs_auth")
    if private_root is not None and check_credentials:
        try:
            proof = read_private_route_evidence(private_root, manifest)
            proved = dict(proof.route_digests)
        except (CertificationRejected, ValueError, TypeError, KeyError, OSError):
            report["route_set_proof_state"] = "unavailable"
            for provider in selected:
                report["providers"][provider]["model_proof_state"] = "unavailable"
        else:
            same_routes = proved == {provider: route.digest for provider, route in selected.items()}
            report["route_set_proof_state"] = "matching" if same_routes else "route_mismatch"
            for provider, route in selected.items():
                cell = report["providers"][provider]
                matching = proved.get(provider) == route.digest
                cell["completed_model_call_proof"] = matching
                cell["model_proof_state"] = "matching" if matching else "route_mismatch"
            if same_routes and eligible and reachable:
                report["summary"] = "model_verified"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Private machine-route status")
    parser.add_argument("--machine-routes", type=Path, required=True)
    parser.add_argument("--machine-policy", type=Path, required=True)
    parser.add_argument("--route-id", action="append", required=True,
                        metavar="PROVIDER=ROUTE-ID")
    parser.add_argument("--private-run-root", type=Path)
    args = parser.parse_args(argv)
    requested = {}
    for item in args.route_id:
        provider, separator, route_id = item.partition("=")
        if (separator != "=" or provider not in PROVIDERS or not route_id
                or provider in requested):
            parser.error("three unique provider route IDs are required")
        requested[provider] = route_id
    if set(requested) != set(PROVIDERS):
        parser.error("three unique provider route IDs are required")
    report = diagnose_routes(args.machine_routes, args.machine_policy, requested,
                             private_root=args.private_run_root)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
