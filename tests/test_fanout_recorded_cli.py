"""Recorded runs bind executable versions independently of fresh compatibility."""
import pytest

import test_fanout_collaboration as cases


@pytest.mark.parametrize("changed", ["claude", "codex", "agy"])
def test_recorded_dispatch_refuses_cli_drift_before_intent(tmp_path, changed):
    fanout = cases.fanout
    versions = {"claude": "2.1.302", "codex": "0.163.0", "agy": "1.4.0"}
    executors = tuple(versions)
    registry = fanout.ProviderRegistry.for_installed(executors,
        version_probe=lambda name: versions[name], contract_probe=lambda _profile: None)
    with fanout.ArtifactStore(tmp_path / "artifacts") as store:
        packet = cases._packet(store, tmp_path, executors=executors, rounds=2, timeout=None)
        seats = tuple(cases._seat(tmp_path, name) for name in executors)
        coordinator, journal, provider, _memory = cases._coordinator(
            tmp_path, packet, executors, registry=registry,
            profile_digests=dict(registry.profile_digests))
        try:
            policy = fanout.RoundPolicy.from_provider_policy(packet.provider_policy)
            coordinator.preflight_round(packet, seats, policy)
            versions[changed] = "9.9.9"
            with pytest.raises(fanout.CollaborationValidationError, match="recorded CLI version"):
                coordinator.preflight_round(packet, seats, policy)
            assert provider.requests == []
            assert not journal.state.seat_phases
        finally:
            journal.close()

def test_recorded_request_rechecks_version_at_process_boundary(tmp_path):
    fanout = cases.fanout
    versions = {"codex": "0.163.0"}
    registry = fanout.ProviderRegistry.for_installed(("codex",),
        version_probe=lambda name: versions[name], contract_probe=lambda _profile: None)
    profile = registry.select("codex", "read-only", "standard")
    request = fanout.ProviderRequest("codex", "synthetic read", cwd=tmp_path,
        profile=profile, run_id="recorded-run", task_id="work", seat_id="seat-codex",
        inputs_digest="a" * 64)
    versions["codex"] = "0.164.0"
    calls = []

    def runner(_command):
        calls.append(1)
        raise AssertionError("recorded executable drift reached process launch")

    with pytest.raises(fanout.ProviderRequestError, match="recorded CLI version"):
        cases.sys.modules[fanout.__name__ + ".providers"]._run_provider_with_runner(
            request, registry=registry, runner=runner)
    assert calls == []


def test_v1_coordinator_carries_recorded_identity_to_provider(tmp_path):
    fanout = cases.fanout
    versions = {"claude": "2.1.302", "codex": "0.163.0"}
    executors = tuple(versions)
    registry = fanout.ProviderRegistry.for_installed(executors,
        version_probe=lambda name: versions[name], contract_probe=lambda _profile: None)
    with fanout.ArtifactStore(tmp_path / "artifacts") as store:
        packet = cases._packet(store, tmp_path, executors=executors, rounds=1, timeout=None)
        seats = tuple(cases._seat(tmp_path, name) for name in executors)
        coordinator, journal, provider, _memory = cases._coordinator(
            tmp_path, packet, executors, registry=registry,
            profile_digests=dict(registry.profile_digests))
        try:
            result = coordinator.execute_round(packet, seats,
                fanout.RoundPolicy.from_provider_policy(packet.provider_policy), round=1)
            assert result.status == "round-complete"
            assert len(provider.requests) == 2
            assert all(request.run_id == packet.run_id and
                       request.inputs_digest == journal.inputs.digest and
                       request.task_id == packet.task_id and request.seat_id
                       for request in provider.requests)
        finally:
            journal.close()
