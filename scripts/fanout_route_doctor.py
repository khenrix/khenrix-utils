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
from shared.lib.fanout.container_routes import RouteRejected, read_machine_routes, select_route
from shared.lib.fanout.providers import ProviderRegistry
from shared.lib.fanout.container_certification import CertificationRejected, read_private_route_evidence


PROVIDERS = ("claude", "codex", "agy")


def diagnose_routes(manifest_path: Path, policy_path: Path,
                    requested: Mapping[str, str], *,
                    private_root: Path | None = None,
                    check_credentials: bool = True) -> dict[str, object]:
    """Return only route digests and separate policy, auth, and model proof states."""
    report: dict[str, object] = {"summary": "blocked_policy", "providers": {
        provider: {"configured_policy": False,
                   "credential_source_reachable": False,
                   "completed_model_call_proof": False,
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
    registry = ProviderRegistry.default(version_probe=lambda _: "unused")
    selected = {}
    for provider in PROVIDERS:
        cell = report["providers"][provider]
        try:
            route = select_route(
                manifest, route_id=requested[provider], provider=provider,
                requested_model=registry.select(provider, "repo-write", "standard").requested_model,
                execution_class="repo-write", data_classification="protected")
            if route.profile.auth_family not in {
                    "vertex-adc", "openai-keychain", "openai-1password"}:
                raise RouteRejected("repository write adapter is unavailable")
        except (RouteRejected, TypeError, ValueError):
            continue
        selected[provider] = route
        cell["configured_policy"] = True
        cell["machine_route_digest"] = route.digest
        if not check_credentials:
            continue
        try:
            source = HostCredentialSource(route.profile)
            source.load()
            cell["credential_source_reachable"] = (
                isinstance(source.fingerprint, str) and len(source.fingerprint) == 64)
        except RouteCredentialError:
            pass
    if len(selected) != len(PROVIDERS):
        return report
    if not check_credentials:
        report["summary"] = "configured"
        return report
    if not all(cell["credential_source_reachable"]
               for cell in report["providers"].values()):
        report["summary"] = "needs_auth"
        return report
    report["summary"] = "model_unverified"
    if private_root is not None:
        try:
            proof = read_private_route_evidence(private_root, manifest)
            same_routes = dict(proof.route_digests) == {
                provider: selected[provider].digest for provider in PROVIDERS}
        except (CertificationRejected, ValueError, TypeError, KeyError, OSError):
            same_routes = False
        if same_routes:
            report["summary"] = "certified"
            for cell in report["providers"].values():
                cell["completed_model_call_proof"] = True
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
