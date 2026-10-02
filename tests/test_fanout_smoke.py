"""Hermetic certification smoke over the production fanout coordination APIs."""
from __future__ import annotations

import json
import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import fanout_smoke  # noqa: E402


def _calls(root: Path) -> list[dict[str, object]]:
    return [json.loads(path.read_text()) for path in sorted((root / "calls").glob("*.json"))]


def test_hermetic_smoke_cold_reopens_and_never_repeats_a_settled_turn(tmp_path: Path) -> None:
    root = tmp_path / "smoke"
    receipt = fanout_smoke.run_hermetic(root)
    calls = _calls(root)

    assert receipt["status"] == "pass"
    assert receipt["provider_turns"] == 6
    assert receipt["peer_transfers"] == 6
    assert receipt["replayed_provider_turns"] == 0
    assert receipt["source_count"] == 3
    assert receipt["caller_unchanged"] is True
    assert len(calls) == 6
    assert sorted((call["round"], call["executor_id"]) for call in calls) == [
        (1, "agy"), (1, "claude"), (1, "codex"),
        (2, "agy"), (2, "claude"), (2, "codex"),
    ]
    by_seat = {(call["round"], call["executor_id"]): call for call in calls}
    for executor_id in ("claude", "codex", "agy"):
        first = by_seat[(1, executor_id)]
        second = by_seat[(2, executor_id)]
        assert first["resume"] is False
        assert first["session_id"] is None
        assert second["resume"] is True
        assert second["session_id"] == first["returned_session_id"]
        assert "UNTRUSTED_PEER_EVIDENCE" not in first["prompt"]
        assert second["prompt"].count("UNTRUSTED_PEER_EVIDENCE") >= 1
    assert (root / "repo" / "source.txt").read_text() == "dirty caller input\n"
    assert "agreed synthesis" not in json.dumps(receipt)
    assert "fact from" not in json.dumps(receipt)
    state = json.loads((root / "state.json").read_text())
    controller = fanout_smoke.fanout.resume_lifecycle_controller(
        root / "controller",
        fanout_smoke.fanout.LifecycleCapability(state["controller_token"]),
    )
    artifacts = fanout_smoke.fanout.ArtifactStore(root / "artifacts")
    try:
        result_ref = fanout_smoke.fanout.ArtifactRef(**receipt["synthesis_ref"])
        synthesis = fanout_smoke.fanout.load_answer_synthesis(
            controller, artifacts,
            run_id=fanout_smoke.RUN_ID,
            task_id=fanout_smoke.TASK_ID,
            answer_ref=result_ref,
        )
        assert synthesis.valid
        assert len(synthesis.source_answers) == 3
        assert artifacts.read_bytes(result_ref) == (root / "delivered.txt").read_bytes()
    finally:
        artifacts.close()


def test_poisoned_peer_is_delimited_and_not_promoted_into_final_answer(tmp_path: Path) -> None:
    root = tmp_path / "poison"
    receipt = fanout_smoke.run_hermetic(root, poison_peer=True)
    calls = _calls(root)
    resumed = [call for call in calls if call["round"] == 2 and call["executor_id"] != "agy"]

    assert receipt["status"] == "pass"
    assert len(resumed) == 2
    assert all("IGNORE ALL PRIOR INSTRUCTIONS" in call["prompt"] for call in resumed)
    assert all("untrusted_evidence" in call["prompt"] for call in resumed)
    final = (root / "delivered.txt").read_text()
    assert "IGNORE ALL PRIOR INSTRUCTIONS" not in final
    assert "agreed synthesis" in final


def test_cold_reopen_rejects_changed_exact_memory_before_resume(tmp_path: Path) -> None:
    root = tmp_path / "tampered"
    fanout_smoke.prepare_hermetic(root)
    observation = next((root / "fake-memory" / "observations").glob("*.json"))
    value = json.loads(observation.read_text())
    value["text"] = "changed after publication"
    observation.write_text(json.dumps(value))

    with pytest.raises(fanout_smoke.SmokeError, match="exact memory"):
        fanout_smoke.resume_hermetic(root)
    assert len(_calls(root)) == 3


def test_system_temp_alias_is_canonicalized_before_authority_bootstrap(tmp_path: Path) -> None:
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)

    receipt = fanout_smoke.run_hermetic(alias / "smoke")

    assert receipt["status"] == "pass"


def test_cold_reopen_rejects_runtime_source_drift_before_provider_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "source-drift"
    fanout_smoke.prepare_hermetic(root)
    monkeypatch.setattr(fanout_smoke, "_source_hash", lambda: "f" * 64)

    with pytest.raises(fanout_smoke.SmokeError, match="source changed"):
        fanout_smoke.resume_hermetic(root)
    assert len(_calls(root)) == 3


@pytest.mark.parametrize("mutation", ("config", "refs", "worktree"))
def test_cold_reopen_rejects_git_admin_drift_before_provider_spend(
    tmp_path: Path, mutation: str,
) -> None:
    root = tmp_path / "git-admin-drift"
    fanout_smoke.prepare_hermetic(root)
    repository = root / "repo"
    if mutation == "config":
        fanout_smoke._git(repository, "config", "smoke.canary", "changed")
    elif mutation == "refs":
        fanout_smoke._git(repository, "update-ref", "refs/smoke/canary", "HEAD")
    else:
        fanout_smoke._git(
            repository, "worktree", "add", "--detach", str(tmp_path / "auxiliary"), "HEAD",
        )

    with pytest.raises(fanout_smoke.SmokeError, match="Git admin"):
        fanout_smoke.resume_hermetic(root)
    assert len(_calls(root)) == 3


class _FakeLiveAdapter(fanout_smoke.fanout.ProviderAdapter):
    capabilities = fanout_smoke.fanout.ProviderCapabilities(False, True, True)

    def __init__(self, executor_id: str) -> None:
        self.executor_id = executor_id

    def build_command(self, request):
        raise AssertionError("unit smoke must not launch a provider CLI")

    def parse(self, stdout):
        raise AssertionError("unit smoke must not parse a provider CLI")


def _fake_live_registry() -> fanout_smoke.fanout.ProviderRegistry:
    profiles = []
    for executor_id in fanout_smoke.EXECUTORS:
        adapter = _FakeLiveAdapter(executor_id)
        for quality_tier in ("standard", "deep"):
            profiles.append(fanout_smoke.fanout.ExecutorProfile(
                executor_id, adapter, adapter.capabilities,
                "read-only", quality_tier, "fake-model", "fake-effort", "1.0.0",
                (executor_id, "--fake"),
                (executor_id, "--fake-resume", "{session_id}"),
                900, 900,
            ))
    return fanout_smoke.fanout.ProviderRegistry(
        profiles, version_probe=lambda _executor_id: "1.0.0",
    )


def _fake_live_dependencies(root: Path, monkeypatch: pytest.MonkeyPatch,
                            *, ignore_peer_facts: bool = False):
    registry = _fake_live_registry()
    runner = None

    def run(request, *, registry):
        nonlocal runner
        assert request.cwd.is_relative_to(root / "controller" / "workspaces")
        assert isinstance(registry.require(request.executor_id), _FakeLiveAdapter)
        if runner is None:
            runner = fanout_smoke._FakeProvider(
                root, poison_peer=False, live_markers=True,
                ignore_peer_facts=ignore_peer_facts,
            )
        return runner(request, registry=registry)

    def guard(controller, verification, profile):
        return fanout_smoke.fanout.AgyReadOnlyGuard(
            controller, verification, profile.digest,
            root / "fake-agy-home", root / "fake-agy", root / "fake-adc",
            "fake-guard.json", "a" * 64,
        )

    collaboration = sys.modules[fanout_smoke.fanout.__name__ + ".collaboration"]
    monkeypatch.setattr(fanout_smoke.fanout, "issue_agy_readonly_guard", guard)
    monkeypatch.setattr(collaboration, "validate_agy_readonly_guard", lambda *_args: None)
    return {
        "registry": registry,
        "provider_runner": run,
        "memory_factory": lambda artifacts: fanout_smoke._FakeMemory(root, artifacts),
        "preflight": lambda _registry: (root / "fake-memory_exchange.py", "b" * 64),
    }


def test_live_fake_adapters_share_peers_after_exact_reopen_and_emit_safe_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "live"
    dependencies = _fake_live_dependencies(root, monkeypatch)

    fanout_smoke.prepare_live(root, authorized=True, **dependencies)
    assert len(_calls(root)) == 3
    inputs = json.loads((root / "journal" / "inputs.json").read_bytes())
    assert inputs["skill_manifests"]["release-source"] == fanout_smoke.release_source_binding().digest
    receipt = fanout_smoke.resume_live(root, authorized=True, **dependencies)
    calls = _calls(root)

    assert receipt["status"] == "pass"
    assert receipt["provider_turns"] == 6
    assert receipt["peer_transfers"] == 6
    assert receipt["peer_facts_used"] == 6
    assert receipt["scope"] == "transport-and-peer-use"
    assert receipt["replayed_provider_turns"] == 0
    assert receipt["caller_unchanged"] is True
    assert receipt["source_count"] == 3
    assert len(calls) == 6
    by_seat = {(call["round"], call["executor_id"]): call for call in calls}
    for executor_id in fanout_smoke.EXECUTORS:
        first = by_seat[(1, executor_id)]
        second = by_seat[(2, executor_id)]
        assert first["resume"] is False
        assert second["resume"] is True
        assert second["session_id"] == first["returned_session_id"]
        assert "UNTRUSTED_PEER_EVIDENCE" not in first["prompt"]
        assert second["prompt"].count("UNTRUSTED_PEER_EVIDENCE") >= 1
        for peer in set(fanout_smoke.EXECUTORS) - {executor_id}:
            assert f'"executor_id":"{peer}"' in second["prompt"]
    synthesis = json.loads((root / "delivered.txt").read_text())
    assert synthesis["schema_version"] == "fanout-live-smoke-synthesis-v1"
    assert set(synthesis["peer_fact_acknowledgments"]) == set(fanout_smoke.EXECUTORS)
    for executor_id, peers in synthesis["peer_fact_acknowledgments"].items():
        assert set(peers) == set(fanout_smoke.EXECUTORS) - {executor_id}
    assert (root / "repo" / "source.txt").read_text() == "dirty caller input\n"
    saved_receipt = (root / "receipt.json").read_text()
    assert json.loads(saved_receipt) == receipt
    assert "dirty caller input" not in saved_receipt
    assert "fact from" not in saved_receipt
    assert "UNTRUSTED_PEER_EVIDENCE" not in saved_receipt
    assert len(receipt["source_sha256"]) == 64
    assert len(receipt["run_inputs_sha256"]) == 64
    assert len(receipt["memory_controller_sha256"]) == 64
    assert receipt["release_binding"] == fanout_smoke.release_source_binding().to_dict()


def test_live_deep_tier_survives_exact_cold_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "deep"
    dependencies = _fake_live_dependencies(root, monkeypatch)
    original = dependencies["provider_runner"]
    seen = []

    def record(request, *, registry):
        seen.append((2 if request.resume else 1, request.executor_id, request.profile.quality_tier))
        return original(request, registry=registry)

    dependencies["provider_runner"] = record
    fanout_smoke.prepare_live(root, authorized=True, quality_tier="deep", **dependencies)
    state = json.loads((root / "state.json").read_text())
    assert state["quality_tier"] == "deep"
    receipt = fanout_smoke.resume_live(root, authorized=True, **dependencies)

    assert receipt["status"] == "pass"
    assert receipt["quality_tier"] == "deep"
    assert sorted(seen) == sorted(
        (round_number, executor_id, "deep")
        for round_number in (1, 2)
        for executor_id in fanout_smoke.EXECUTORS
    )


@pytest.mark.parametrize("tier", ("standard", "deep"))
def test_live_fresh_installed_profiles_are_durable_before_dispatch_and_bound_on_resume(
    tmp_path, monkeypatch, tier,
):
    versions = {"claude": "2.2.0", "codex": "0.160.0", "agy": "1.3.0"}
    monkeypatch.setattr(fanout_smoke.fanout.providers, "_installed_cli_version",
                        lambda executor: versions[executor])
    for index, version in enumerate(("0.160.0", "0.161.0")):
        versions["codex"] = version
        root = tmp_path / str(index)
        dependencies = _fake_live_dependencies(root, monkeypatch)
        dependencies.pop("registry")
        fake = None
        sent = []

        def sender(request, *, registry):
            nonlocal fake
            profile = request.profile
            assert profile.cli_version == versions[request.executor_id]
            data = (root / "artifacts" / "amendments/executor-profiles" /
                    f"{profile.digest}.json").read_bytes()
            assert hashlib.sha256(data).hexdigest() == profile.digest
            assert json.loads(data) == profile.to_dict()
            sent.append(profile.cli_version)
            if fake is None:
                fake = fanout_smoke._FakeProvider(root, poison_peer=False, live_markers=True)
            return fake(request, registry=registry)

        def preflight(registry):
            for executor in fanout_smoke.EXECUTORS:
                registry.assert_installed_version(registry.admit(executor, "read-only", tier))
            return root / "fake-memory_exchange.py", "b" * 64

        dependencies.update(provider_runner=sender, preflight=preflight)
        fanout_smoke.prepare_live(root, authorized=True, quality_tier=tier, **dependencies)
        saved = {path: path.read_bytes() for path in
                 (root / "state.json", root / "journal/inputs.json")}
        if index == 0:
            versions["codex"] = "0.161.0"
            with pytest.raises(fanout_smoke.fanout.ProviderRequestError, match="drift"):
                fanout_smoke.resume_live(root, authorized=True, **dependencies)
            assert len(sent) == 3
            assert all(path.read_bytes() == raw for path, raw in saved.items())
            versions["codex"] = version
        assert fanout_smoke.resume_live(root, authorized=True, **dependencies)["status"] == "pass"
        assert len(sent) == 6


def test_live_recorded_descriptor_tamper_refuses_before_preflight(tmp_path, monkeypatch):
    root = tmp_path / "tampered-profile"
    dependencies = _fake_live_dependencies(root, monkeypatch)
    fanout_smoke.prepare_live(root, authorized=True, **dependencies)
    state_path = root / "state.json"
    state = json.loads(state_path.read_bytes())
    state["provider_profile_descriptors"][0]["cli_version"] = "9.9.9"
    state_path.write_bytes(fanout_smoke.fanout.canonical_json(state))
    dependencies["preflight"] = lambda _registry: pytest.fail("tampered descriptor reached preflight")
    with pytest.raises(fanout_smoke.SmokeError, match="profiles"):
        fanout_smoke.resume_live(root, authorized=True, **dependencies)
    assert len(_calls(root)) == 3


def test_live_peer_synthesis_accepts_capitalized_executor_names() -> None:
    facts = {"agy": "agy-fact", "claude": "claude-fact", "codex": "codex-fact"}
    answers = {
        executor: {
            "executor_id": executor, "source_line": "dirty caller input",
            "fact": facts[executor],
            "peer_facts": {peer: fact for peer, fact in facts.items() if peer != executor},
            "synthesis": " and ".join(peer.title() for peer in facts if peer != executor),
        }
        for executor in facts
    }

    class Artifacts:
        def read_bytes(self, ref):
            return json.dumps(answers[ref]).encode()

    terminals = tuple(
        SimpleNamespace(seat_id=f"seat-{executor}", answer_ref=executor)
        for executor in facts
    )
    assert fanout_smoke._live_second_answers(Artifacts(), terminals, facts) == answers


def test_live_requires_explicit_authorization_before_preflight_or_root_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "unauthorized"
    dependencies = _fake_live_dependencies(root, monkeypatch)
    dependencies["preflight"] = lambda _registry: pytest.fail("preflight ran")

    with pytest.raises(fanout_smoke.SmokeError, match="authorization"):
        fanout_smoke.prepare_live(root, **dependencies)
    assert not root.exists()


def test_live_root_cannot_be_created_inside_source_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setattr(fanout_smoke, "ROOT", source)
    with pytest.raises(fanout_smoke.SmokeError, match="checkout"):
        fanout_smoke._live_root(source / "smoke-output", new=True)


def test_live_cli_accepts_exact_environment_opt_in_for_existing_make_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "env-live"
    monkeypatch.setenv("FANOUT_LIVE_ALLOW_PAID", "1")
    monkeypatch.setenv("FANOUT_LIVE_ROOT", str(root))
    called = []
    monkeypatch.setattr(
        fanout_smoke, "run_live",
        lambda selected_root, *, authorized, quality_tier, machine_routes, machine_policy, codex_route: called.append(
            (selected_root, authorized, quality_tier, machine_routes, machine_policy, codex_route)
        )
        or {"status": "pass"},
    )

    assert fanout_smoke.main(["--live"]) == 0
    assert [call[:3] for call in called] == [(root, True, "standard")]
    assert [call[3:] for call in called] == [(None, None, None)]
    assert json.loads(capsys.readouterr().out) == {"status": "pass"}


def test_live_cli_environment_opt_in_must_be_literal_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "env-not-authorized"
    monkeypatch.setenv("FANOUT_LIVE_ALLOW_PAID", "yes")
    monkeypatch.setenv("FANOUT_LIVE_ROOT", str(root))

    assert fanout_smoke.main(["--live"]) == 1
    assert not root.exists()


def test_live_rejects_missing_preflight_before_provider_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "no-memory"
    dependencies = _fake_live_dependencies(root, monkeypatch)
    dependencies["preflight"] = lambda _registry: (_ for _ in ()).throw(
        fanout_smoke.SmokeError("authenticated memory preflight failed")
    )

    with pytest.raises(fanout_smoke.SmokeError, match="memory preflight"):
        fanout_smoke.prepare_live(root, authorized=True, **dependencies)
    assert not root.exists()


def test_live_cold_reopen_rejects_source_drift_before_provider_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "drift"
    dependencies = _fake_live_dependencies(root, monkeypatch)
    fanout_smoke.prepare_live(root, authorized=True, **dependencies)
    monkeypatch.setattr(fanout_smoke, "_live_source_hash", lambda: "f" * 64)

    with pytest.raises(fanout_smoke.SmokeError, match="source changed"):
        fanout_smoke.resume_live(root, authorized=True, **dependencies)
    assert len(_calls(root)) == 3


def test_live_cold_reopen_rejects_memory_component_drift_with_legacy_hash_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.lib.fanout.container_source import ReleaseSourceBinding, release_source_binding
    root = tmp_path / "release-drift"
    dependencies = _fake_live_dependencies(root, monkeypatch)
    before = release_source_binding()
    monkeypatch.setattr(fanout_smoke, "release_source_binding", lambda: before, raising=False)
    fanout_smoke.prepare_live(root, authorized=True, **dependencies)
    monkeypatch.setattr(fanout_smoke, "release_source_binding",
                        lambda: ReleaseSourceBinding(before.runtime_sha256, "e" * 64))
    with pytest.raises(fanout_smoke.SmokeError, match="release|component"):
        fanout_smoke.resume_live(root, authorized=True, **dependencies)
    assert len(_calls(root)) == 3


@pytest.mark.parametrize("relabel_inputs", [False, True])
def test_live_cold_reopen_rejects_relabelled_release_before_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relabel_inputs: bool,
) -> None:
    from shared.lib.fanout.container_source import ReleaseSourceBinding, release_source_binding

    root = tmp_path / "relabelled-release"
    dependencies = _fake_live_dependencies(root, monkeypatch)
    before = release_source_binding()
    monkeypatch.setattr(fanout_smoke, "release_source_binding", lambda: before)
    fanout_smoke.prepare_live(root, authorized=True, **dependencies)
    assert len(_calls(root)) == 3
    source_sha256 = fanout_smoke._live_source_hash()
    authenticated_inputs = (root / "journal" / "inputs.json").read_bytes()
    journal_events = (root / "journal" / "events.jsonl").read_bytes()
    state_path = root / "state.json"
    state = json.loads(state_path.read_bytes())
    original_inputs = state["inputs"]
    changed = ReleaseSourceBinding(before.runtime_sha256, "e" * 64)
    state["release_binding"] = changed.to_dict()
    if relabel_inputs:
        state["inputs"]["skill_manifests"]["release-source"] = changed.digest
    state_path.write_text(json.dumps(state))
    monkeypatch.setattr(fanout_smoke, "release_source_binding", lambda: changed)
    preflights = []
    original_preflight = dependencies["preflight"]

    def preflight(registry):
        preflights.append(registry)
        return original_preflight(registry)

    dependencies["preflight"] = preflight
    with pytest.raises(
        (fanout_smoke.SmokeError, fanout_smoke.fanout.RunStateError),
        match="release|binding|immutable preflight",
    ):
        fanout_smoke.resume_live(root, authorized=True, **dependencies)
    assert preflights == []
    assert len(_calls(root)) == 3
    assert fanout_smoke._live_source_hash() == source_sha256
    if not relabel_inputs:
        assert json.loads(state_path.read_bytes())["inputs"] == original_inputs
    assert (root / "journal" / "inputs.json").read_bytes() == authenticated_inputs
    assert (root / "journal" / "events.jsonl").read_bytes() == journal_events


def test_historical_smoke_hash_functions_keep_their_distinct_encodings(tmp_path, monkeypatch):
    root = tmp_path / "legacy-source"
    modules = root / "shared/lib/fanout"
    modules.mkdir(parents=True)
    (modules / "one.py").write_bytes(b"runtime-one")
    script = root / "scripts/fanout_smoke.py"
    script.parent.mkdir()
    script.write_bytes(b"old smoke")
    memory = root / "components/memory"
    memory.mkdir(parents=True)
    for name in ("memoryctl.py", "memory_exchange.py", "memory_gateway.py"):
        (memory / name).write_bytes(name.encode())
    monkeypatch.setattr(fanout_smoke, "ROOT", root)
    monkeypatch.setattr(fanout_smoke, "__file__", str(script))
    legacy = hashlib.sha256(b"shared/lib/fanout/one.py\0runtime-one\0scripts/fanout_smoke.py\0old smoke\0")
    assert fanout_smoke._source_hash() == legacy.hexdigest()
    live = hashlib.sha256(legacy.digest())
    live.update(b"memoryctl.py\0memoryctl.py\0memory_exchange.py\0memory_exchange.py\0memory_gateway.py\0memory_gateway.py\0")
    assert fanout_smoke._live_source_hash() == live.hexdigest()


def test_live_rejects_source_drift_during_first_paid_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "first-round-source-drift"
    dependencies = _fake_live_dependencies(root, monkeypatch)
    source = {"sha256": "a" * 64}
    monkeypatch.setattr(fanout_smoke, "_live_source_hash", lambda: source["sha256"])
    original = dependencies["provider_runner"]

    def drift_after_turn(request, *, registry):
        result = original(request, registry=registry)
        source["sha256"] = "b" * 64
        return result

    dependencies["provider_runner"] = drift_after_turn
    with pytest.raises(fanout_smoke.SmokeError, match="source changed"):
        fanout_smoke.prepare_live(root, authorized=True, **dependencies)
    assert len(_calls(root)) == 3


def test_live_rejects_round_two_answers_that_ignore_peer_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "ignored-peers"
    dependencies = _fake_live_dependencies(root, monkeypatch, ignore_peer_facts=True)
    fanout_smoke.prepare_live(root, authorized=True, **dependencies)

    with pytest.raises(fanout_smoke.SmokeError, match="peer facts"):
        fanout_smoke.resume_live(root, authorized=True, **dependencies)
    assert len(_calls(root)) == 6

    with pytest.raises(fanout_smoke.SmokeError, match="peer facts"):
        fanout_smoke.resume_live(root, authorized=True, **dependencies)
    assert len(_calls(root)) == 6
    assert not (root / "delivered.txt").exists()


def test_live_rechecks_source_after_preflight_before_second_round_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "pre-second-drift"
    dependencies = _fake_live_dependencies(root, monkeypatch)
    source = {"sha256": "a" * 64}
    monkeypatch.setattr(fanout_smoke, "_live_source_hash", lambda: source["sha256"])
    fanout_smoke.prepare_live(root, authorized=True, **dependencies)
    original_preflight = dependencies["preflight"]

    def drift_after_preflight(registry):
        result = original_preflight(registry)
        source["sha256"] = "b" * 64
        return result

    dependencies["preflight"] = drift_after_preflight
    with pytest.raises(fanout_smoke.SmokeError, match="source changed"):
        fanout_smoke.resume_live(root, authorized=True, **dependencies)
    assert len(_calls(root)) == 3
    assert not (root / "delivered.txt").exists()


def test_live_drift_during_second_round_cannot_publish_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "second-round-drift"
    dependencies = _fake_live_dependencies(root, monkeypatch)
    source = {"sha256": "a" * 64}
    monkeypatch.setattr(fanout_smoke, "_live_source_hash", lambda: source["sha256"])
    fanout_smoke.prepare_live(root, authorized=True, **dependencies)
    original = dependencies["provider_runner"]

    def drift_after_second_turn(request, *, registry):
        result = original(request, registry=registry)
        if "/round-2/" in request.artifact_prefix:
            source["sha256"] = "b" * 64
        return result

    dependencies["provider_runner"] = drift_after_second_turn
    with pytest.raises(fanout_smoke.SmokeError, match="source changed"):
        fanout_smoke.resume_live(root, authorized=True, **dependencies)
    assert len(_calls(root)) == 6
    assert not (root / "delivered.txt").exists()
    assert not (root / "receipt.json").exists()


def test_live_cold_reopen_recovers_delivery_before_receipt_without_repeating_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "interrupted-receipt"
    dependencies = _fake_live_dependencies(root, monkeypatch)
    fanout_smoke.prepare_live(root, authorized=True, **dependencies)
    original_write = fanout_smoke._write_new

    def interrupt_receipt(path: Path, data: bytes) -> None:
        if path.name == "receipt.json":
            raise OSError("simulated process interruption after delivery")
        original_write(path, data)

    monkeypatch.setattr(fanout_smoke, "_write_new", interrupt_receipt)
    with pytest.raises(OSError, match="simulated process interruption"):
        fanout_smoke.resume_live(root, authorized=True, **dependencies)
    assert (root / "delivered.txt").exists()
    assert not (root / "receipt.json").exists()
    assert len(_calls(root)) == 6

    monkeypatch.setattr(fanout_smoke, "_write_new", original_write)
    receipt = fanout_smoke.resume_live(root, authorized=True, **dependencies)
    assert receipt["status"] == "pass"
    assert len(_calls(root)) == 6
    assert fanout_smoke.resume_live(root, authorized=True, **dependencies) == receipt
    assert len(_calls(root)) == 6


def test_live_cold_reopen_rejects_changed_delivery_before_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "changed-delivery"
    dependencies = _fake_live_dependencies(root, monkeypatch)
    fanout_smoke.prepare_live(root, authorized=True, **dependencies)
    original_write = fanout_smoke._write_new

    def interrupt_receipt(path: Path, data: bytes) -> None:
        if path.name == "receipt.json":
            raise OSError("simulated process interruption after delivery")
        original_write(path, data)

    monkeypatch.setattr(fanout_smoke, "_write_new", interrupt_receipt)
    with pytest.raises(OSError, match="simulated process interruption"):
        fanout_smoke.resume_live(root, authorized=True, **dependencies)
    (root / "delivered.txt").write_bytes(b"changed after delivery")
    monkeypatch.setattr(fanout_smoke, "_write_new", original_write)

    with pytest.raises(fanout_smoke.SmokeError, match="delivery changed"):
        fanout_smoke.resume_live(root, authorized=True, **dependencies)
    assert len(_calls(root)) == 6
    assert not (root / "receipt.json").exists()


@pytest.mark.parametrize("initialized", (False, True))
def test_live_preflight_requires_initialized_authenticated_memory_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, initialized: bool,
) -> None:
    controller_root = tmp_path / "controller"
    controller_root.mkdir(mode=0o700)
    controller = controller_root / "memory_exchange.py"
    controller.write_bytes(b"#!/usr/bin/env python3\n")
    controller.chmod(0o700)
    monkeypatch.setattr(
        fanout_smoke.memoryctl, "health_document",
        lambda *, require_running: {"ok": True, "worker": "running", "gateway": "running"},
    )
    monkeypatch.setattr(
        fanout_smoke.memoryctl, "installed_controller_root", lambda: controller_root,
    )
    monkeypatch.setattr(
        fanout_smoke.memory_exchange, "_read_gateway_token", lambda _path: "a" * 43,
    )

    class Response:
        status = 200

        def getheader(self, _name):
            return "application/json"

        def read(self, _limit):
            return json.dumps({
                "status": "ok", "initialized": initialized,
                "dependencies": {"details": "x" * 5000},
            }).encode()[:_limit]

    class Connection:
        def __init__(self, *_args, **_kwargs):
            pass

        def request(self, _method, _path, *, headers):
            assert headers["Authorization"] == "Bearer " + "a" * 43

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(fanout_smoke.http.client, "HTTPConnection", Connection)

    if initialized:
        path, digest = fanout_smoke._live_preflight(_fake_live_registry())
        assert path == controller
        assert len(digest) == 64
    else:
        with pytest.raises(fanout_smoke.SmokeError, match="authenticated memory preflight"):
            fanout_smoke._live_preflight(_fake_live_registry())
