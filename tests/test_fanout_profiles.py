"""Class- and tier-specific, digest-bound executor profile contracts."""
from __future__ import annotations

import importlib.util
import dataclasses
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "shared" / "lib" / "fanout"
SPEC = importlib.util.spec_from_file_location(
    "_fanout_profile_contracts", PACKAGE / "__init__.py",
    submodule_search_locations=[str(PACKAGE)],
)
assert SPEC and SPEC.loader
fanout = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fanout
SPEC.loader.exec_module(fanout)


@pytest.fixture
def _mock_clean_codex_host(monkeypatch):
    # Model/argv fixtures do not depend on the machine's managed Codex config.
    providers = sys.modules[f"{SPEC.name}.providers"]
    monkeypatch.setattr(providers, "_codex_read_only_host_config_preflight", lambda: None)


def test_registry_pins_distinct_standard_and_deep_class_profiles():
    """Collapsing profile identity to executor ID permits cross-class/model drift."""
    registry = fanout.ProviderRegistry.default()

    read = registry.select("claude", "read-only", "standard")
    write = registry.select("claude", "repo-write", "standard")
    deep = registry.select("claude", "read-only", "deep")

    assert (read.requested_model, read.requested_effort) == ("claude-opus-5-5", "max")
    assert (write.requested_model, write.requested_effort) == ("claude-opus-5-5", "xhigh")
    assert (deep.requested_model, deep.requested_effort) == ("claude-opus-5-5", "ultracode")
    assert len({read.digest, write.digest, deep.digest}) == 3
    assert read.default_timeout == read.timeout_ceiling == 900
    assert write.default_timeout == write.timeout_ceiling == 3600
    assert deep.default_timeout == 1800
    assert registry.profile_digests["claude/read-only/standard"] == read.digest
    assert registry.profile_digests["claude/repo-write/standard"] == write.digest
    assert registry.profile_digests["claude/read-only/deep"] == deep.digest


def test_builtin_profile_rejects_a_descriptor_that_would_launch_another_model():
    """A matching digest cannot redeem argv that disagrees with the declared model."""
    registry = fanout.ProviderRegistry.default()
    profiles = tuple(registry._profiles.values())
    modified = tuple(
        dataclasses.replace(profile, requested_model="other-model", profile_sha256=None)
        if profile.key == ("codex", "read-only", "standard") else profile
        for profile in profiles
    )

    with pytest.raises(fanout.ProviderRequestError, match="argv"):
        fanout.ProviderRegistry(modified)


@pytest.mark.parametrize("executor,model,effort", [
    ("codex", "gpt-6-sol", "xhigh"),
    ("agy", "gemini-3.8-flash-high", "high"),
])
def test_standard_profiles_pin_model_and_effort(executor, model, effort):
    """A registry must not silently inherit ambient model/effort defaults."""
    profile = fanout.ProviderRegistry.default().select(executor, "read-only", "standard")

    assert (profile.requested_model, profile.requested_effort) == (model, effort)
    assert profile.cli_version
    assert profile.digest == profile.profile_sha256


def test_default_codex_profiles_admit_measured_01571_and_reject_older_binary():
    """A CLI upgrade must not leave every Codex seat blocked or admit stale sessions."""
    installed = ["0.157.1"]
    registry = fanout.ProviderRegistry.default(version_probe=lambda _executor: installed[0])

    for execution_class, tier in (("read-only", "standard"),
                                  ("read-only", "deep"),
                                  ("repo-write", "standard")):
        registry.assert_installed_version(registry.select("codex", execution_class, tier))

    installed[0] = "0.156.1"
    with pytest.raises(fanout.ProviderRequestError, match="CLI version drift"):
        registry.assert_installed_version(registry.select("codex", "read-only", "deep"))


def test_deep_profile_does_not_silently_downgrade_uncharacterized_effort():
    """Only live initial/resume canaries authorize a deep executor profile."""
    registry = fanout.ProviderRegistry.default()

    for executor in ("claude", "codex", "agy"):
        assert registry.admit(executor, "read-only", "deep").characterized
    with pytest.raises(fanout.UnsupportedExecutorError, match="maka"):
        registry.select("maka", "read-only", "standard")


def test_uncharacterized_deep_turn_blocks_without_provider_spend(tmp_path, monkeypatch):
    """A declared deep profile is not permission to launch an unverified CLI tier."""
    providers = sys.modules[f"{SPEC.name}.providers"]
    launched = []
    monkeypatch.setattr(providers, "run_command", lambda command: launched.append(command))
    default = fanout.ProviderRegistry.default()
    profiles = tuple(
        dataclasses.replace(profile, characterized=False, profile_sha256=None)
        if profile.key == ("codex", "read-only", "deep") else profile
        for profile in default._profiles.values()
    )
    registry = fanout.ProviderRegistry(profiles, version_probe=lambda _executor: "unused")

    with pytest.raises(fanout.ProviderRequestError, match="characterization"):
        fanout.run_provider(fanout.ProviderRequest(
            "codex", "question", cwd=tmp_path, execution_class="read-only",
            profile=registry.select("codex", "read-only", "deep"),
        ), registry=registry)
    assert launched == []


@pytest.mark.parametrize("executor,session", [
    ("claude", "11111111-1111-4111-8111-111111111111"),
    ("codex", "codex-thread-1"),
    ("agy", "agy-conversation-1"),
])
def test_initial_and_resume_argv_pin_the_same_selected_model(
    tmp_path, executor, session, _mock_clean_codex_host,
):
    """A resumed CLI falling back to ambient defaults could silently switch models."""
    registry = fanout.ProviderRegistry.default()
    execution_class = "repo-write" if executor == "agy" else "read-only"
    profile = registry.admit(executor, execution_class, "standard")
    adapter = registry.require(executor)
    initial = fanout.ProviderRequest(
        executor, "secret", cwd=tmp_path, session_id=session if executor == "claude" else None,
        execution_class=execution_class, profile=profile,
    )
    resumed = fanout.ProviderRequest(
        executor, "secret", cwd=tmp_path, session_id=session, resume=True,
        execution_class=execution_class, profile=profile,
    )

    first = adapter.build_command(initial)
    next_turn = adapter.build_command(resumed)

    assert profile.requested_model in first.argv
    assert profile.requested_model in next_turn.argv
    assert first.timeout == next_turn.timeout == profile.default_timeout
    if executor == "agy":
        assert first.stdin == next_turn.stdin == b'{"event":"user","message":{"content":"secret"}}\n'
    else:
        assert first.stdin == next_turn.stdin == b"secret"
    if executor == "agy":
        assert "--dangerously-skip-permissions" in first.argv
        assert "--dangerously-skip-permissions" in next_turn.argv
        assert ("--mode", "accept-edits") == first.argv[first.argv.index("--mode"):][:2]
        assert ("--mode", "accept-edits") == next_turn.argv[next_turn.argv.index("--mode"):][:2]
    elif executor == "claude":
        assert ("--effort", "max") == first.argv[first.argv.index("--effort"):][:2]
        assert ("--effort", "max") == next_turn.argv[next_turn.argv.index("--effort"):][:2]
    else:
        assert 'model_reasoning_effort="xhigh"' in first.argv
        assert 'model_reasoning_effort="xhigh"' in next_turn.argv
        assert 'sandbox_mode="read-only"' in next_turn.argv


@pytest.mark.parametrize("execution_class,quality_tier,mode", [
    ("read-only", "standard", "plan"),
    ("repo-write", "standard", "accept-edits"),
    ("read-only", "deep", "plan"),
])
def test_agy_profile_passes_pinned_effort_on_initial_and_resume(
    tmp_path, execution_class, quality_tier, mode,
):
    """An agy profile must pass its declared effort on both turn shapes."""
    registry = fanout.ProviderRegistry.default()
    profile = registry.select("agy", execution_class, quality_tier)
    adapter = registry.require("agy")

    assert profile.requested_effort == "high"
    for resume, session_id in ((False, None), (True, "conversation-1")):
        request = fanout.ProviderRequest(
            "agy", "question", cwd=tmp_path, execution_class=execution_class,
            session_id=session_id, resume=resume, profile=profile,
        )
        if execution_class == "read-only":
            with pytest.raises(fanout.ProviderRequestError, match="guard"):
                adapter.build_command(request)
            argv = profile.resume_argv if resume else profile.initial_argv
            assert "--dangerously-skip-permissions" not in argv
        else:
            argv = adapter.build_command(request).argv

        assert argv.count("--effort") == 1
        assert argv[argv.index("--effort") + 1] == "high"
        assert argv[argv.index("--mode") + 1] == mode


def test_agy_write_profile_approves_headless_tools_without_dropping_write_mode(tmp_path):
    registry = fanout.ProviderRegistry.default()
    profile = registry.select("agy", "repo-write", "standard")
    adapter = registry.require("agy")

    for request in (
        fanout.ProviderRequest("agy", "work", cwd=tmp_path, execution_class="repo-write",
                               profile=profile),
        fanout.ProviderRequest("agy", "continue", cwd=tmp_path, execution_class="repo-write",
                               session_id="conversation-1", resume=True, profile=profile),
    ):
        argv = adapter.build_command(request).argv
        assert "--dangerously-skip-permissions" in argv
        assert ("--mode", "accept-edits") == argv[argv.index("--mode"):][:2]


def test_result_distinguishes_requested_from_unobserved_model(
    tmp_path, monkeypatch, _mock_clean_codex_host,
):
    """A pinned request is not evidence that the provider actually ran that model."""
    fixture = (ROOT / "tests" / "fixtures" / "fanout_providers" / "codex-success.ndjson").read_bytes()
    providers = sys.modules[f"{SPEC.name}.providers"]
    monkeypatch.setattr(providers, "run_command", lambda _command: fanout.ProcessResult(
        status=fanout.ProcessStatus.EXIT, returncode=0, stdout=fixture, stderr=b"",
    ))
    registry = fanout.ProviderRegistry.default(version_probe=lambda _executor: "0.157.1")
    profile = registry.admit("codex", "read-only", "standard")

    result = fanout.run_provider(fanout.ProviderRequest(
        "codex", "question", cwd=tmp_path, profile=profile,
    ), registry=registry)

    assert result.valid
    assert result.requested_model == "gpt-6-sol"
    assert result.observed_model is None


def test_installed_cli_version_drift_blocks_before_provider_launch(tmp_path, monkeypatch):
    """A changed binary must not silently resume an earlier pinned session."""
    providers = sys.modules[f"{SPEC.name}.providers"]
    launched = []
    monkeypatch.setattr(providers, "run_command", lambda command: launched.append(command))
    registry = fanout.ProviderRegistry.default(version_probe=lambda _executor: "0.999.0")
    profile = registry.select("codex", "read-only", "standard")

    with pytest.raises(fanout.ProviderRequestError, match="CLI version drift"):
        fanout.run_provider(fanout.ProviderRequest(
            "codex", "resume", cwd=tmp_path, session_id="thread-1", resume=True,
            profile=profile,
        ), registry=registry)
    assert launched == []


@pytest.mark.parametrize("execution_class", ["read-only", "repo-write"])
def test_agy_standard_profiles_pin_installed_version_and_reject_future_drift(execution_class):
    """Version pinning remains checked after live characterization."""
    installed = ["1.2.12"]
    registry = fanout.ProviderRegistry.default(version_probe=lambda _executor: installed[0])
    profile = registry.select("agy", execution_class, "standard")
    assert registry.admit("agy", execution_class, "standard") is profile

    assert profile.cli_version == "1.2.12"
    registry.assert_installed_version(profile)
    installed[0] = "1.2.13"
    with pytest.raises(fanout.ProviderRequestError, match="CLI version drift"):
        registry.assert_installed_version(profile)


@pytest.mark.parametrize("executor,stdout,stderr,expected", [
    ("claude", b"2.1.281 (Claude Code)\n", b"", "2.1.281"),
    ("codex", b"codex-cli 0.156.1\n", b"launcher warning\n", "0.156.1"),
    ("agy", b"1.2.11\n", b"", "1.2.11"),
    ("codex", b"codex-cli 0.156.1-dev\n", b"", None),
    ("codex", b"notice 0.156.1\ncodex-cli 0.156.0\n", b"", None),
    ("codex", b"", b"unrelated 0.156.1 warning\n", None),
])
def test_cli_version_probe_accepts_only_an_exact_provider_version_line(
    monkeypatch, executor, stdout, stderr, expected,
):
    """A semver fragment in a banner or prerelease suffix must not satisfy a pin."""
    providers = sys.modules[f"{SPEC.name}.providers"]
    monkeypatch.setattr(providers.subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess(args[0], 0, stdout, stderr))

    if expected is None:
        with pytest.raises(ValueError, match="version"):
            providers._installed_cli_version(executor)
    else:
        assert providers._installed_cli_version(executor) == expected


def test_provider_runner_rejects_an_unpinned_direct_request(tmp_path, monkeypatch):
    """A direct caller cannot bypass run-input model/version binding by omitting a profile."""
    providers = sys.modules[f"{SPEC.name}.providers"]
    launched = []
    monkeypatch.setattr(providers, "run_command", lambda command: launched.append(command))

    with pytest.raises(fanout.ProviderRequestError, match="pinned profile"):
        fanout.run_provider(fanout.ProviderRequest("codex", "question", cwd=tmp_path))
    assert launched == []


def test_structured_observed_model_mismatch_is_invalid(
    tmp_path, monkeypatch, _mock_clean_codex_host,
):
    """A provider's explicit model evidence must outrank the model we requested."""
    providers = sys.modules[f"{SPEC.name}.providers"]
    events = [
        {"type": "thread.started", "thread_id": "thread-1"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "answer"}},
        {"type": "turn.completed", "model": "gpt-5.6-sol", "usage": {}},
    ]
    output = b"".join(json.dumps(event).encode() + b"\n" for event in events)
    monkeypatch.setattr(providers, "run_command", lambda _command: fanout.ProcessResult(
        status=fanout.ProcessStatus.EXIT, returncode=0, stdout=output, stderr=b"",
    ))
    registry = fanout.ProviderRegistry.default(version_probe=lambda _executor: "0.157.1")

    result = fanout.run_provider(fanout.ProviderRequest(
        "codex", "question", cwd=tmp_path,
        profile=registry.select("codex", "read-only", "standard"),
    ), registry=registry)

    assert not result.valid
    assert result.reason == "model-drift"
    assert result.requested_model == "gpt-6-sol"
    assert result.observed_model == "gpt-5.6-sol"


@pytest.mark.parametrize("actual,valid", [
    ("claude-opus-5-5", True),
    ("claude-sonnet-5-5", False),
])
def test_claude_model_usage_is_observed_and_checked(tmp_path, monkeypatch, actual, valid):
    """Claude's measured result envelope reports model IDs under modelUsage."""
    providers = sys.modules[f"{SPEC.name}.providers"]
    source = json.loads((ROOT / "tests" / "fixtures" / "fanout_providers" /
                         "claude-success.json").read_text())
    source["modelUsage"] = {actual: {"inputTokens": 1}}
    output = json.dumps(source).encode()
    monkeypatch.setattr(providers, "run_command", lambda _command: fanout.ProcessResult(
        status=fanout.ProcessStatus.EXIT, returncode=0, stdout=output, stderr=b"",
    ))
    registry = fanout.ProviderRegistry.default(version_probe=lambda _executor: "2.1.281")

    result = fanout.run_provider(fanout.ProviderRequest(
        "claude", "question", cwd=tmp_path,
        session_id="11111111-1111-4111-8111-111111111111",
        profile=registry.select("claude", "read-only", "standard"),
    ), registry=registry)

    assert result.valid is valid
    assert result.reason == ("ok" if valid else "model-drift")
    assert result.requested_model == "claude-opus-5-5"
    assert result.observed_model == actual


def test_claude_multiple_reported_models_is_not_mistaken_for_one(tmp_path, monkeypatch):
    providers = sys.modules[f"{SPEC.name}.providers"]
    source = json.loads((ROOT / "tests" / "fixtures" / "fanout_providers" /
                         "claude-success.json").read_text())
    source["modelUsage"] = {
        "claude-opus-5-5": {"inputTokens": 1},
        "claude-sonnet-5-5": {"inputTokens": 1},
    }
    output = json.dumps(source).encode()
    monkeypatch.setattr(providers, "run_command", lambda _command: fanout.ProcessResult(
        status=fanout.ProcessStatus.EXIT, returncode=0, stdout=output, stderr=b"",
    ))
    registry = fanout.ProviderRegistry.default(version_probe=lambda _executor: "2.1.281")

    result = fanout.run_provider(fanout.ProviderRequest(
        "claude", "question", cwd=tmp_path,
        session_id="11111111-1111-4111-8111-111111111111",
        profile=registry.select("claude", "read-only", "standard"),
    ), registry=registry)

    assert not result.valid
    assert result.reason == "protocol-error"
    assert result.observed_model is None


def test_policy_inherits_selected_class_and_tier_timeout():
    """A missing timeout must not fall back to the old unprofiled 120-second value."""
    registry = fanout.ProviderRegistry.default()
    policy = fanout.ProviderPolicyV1(timeout=None)

    assert policy.effective_timeout(registry.select("claude", "read-only", "standard")) == 900
    assert policy.effective_timeout(registry.select("claude", "repo-write", "standard")) == 3600
    assert fanout.ProviderPolicyV1(quality_tier="deep", timeout=None).effective_timeout(
        registry.select("agy", "read-only", "deep"),
    ) == 1800
    round_policy = fanout.RoundPolicy.from_provider_policy(policy)
    assert round_policy.timeout is None
    assert round_policy.quality_tier == "standard"


def test_over_ceiling_timeout_requires_reviewed_reason_and_never_exceeds_process_cap():
    """A task cannot casually raise its seat budget above its class ceiling."""
    profile = fanout.ProviderRegistry.default().select("claude", "read-only", "standard")

    with pytest.raises(fanout.PlanValidationError, match="review"):
        fanout.ProviderPolicyV1(timeout=1200).effective_timeout(profile)
    with pytest.raises(fanout.PlanValidationError, match="review"):
        fanout.ProviderPolicyV1(timeout=1200, timeout_override_reason="more analysis").effective_timeout(profile)
    approved = fanout.ProviderPolicyV1(
        timeout=1200, timeout_override_reason="owner approved deeper source review",
        timeout_override_review_sha256="a" * 64,
    )
    assert approved.effective_timeout(profile) == 1200
    with pytest.raises(fanout.PlanValidationError, match="3600"):
        fanout.ProviderPolicyV1(timeout=3601)


def test_direct_provider_request_cannot_bypass_timeout_review(tmp_path):
    """The transport boundary must enforce the review recorded by the plan."""
    profile = fanout.ProviderRegistry.default().select("claude", "read-only", "standard")

    with pytest.raises(fanout.ProviderRequestError, match="review"):
        fanout.ProviderRequest("claude", "question", cwd=tmp_path, timeout=1200,
                               profile=profile)
    request = fanout.ProviderRequest(
        "claude", "question", cwd=tmp_path, timeout=1200, profile=profile,
        timeout_override_reason="reviewed larger task", timeout_override_review_sha256="a" * 64,
    )
    assert request.timeout == 1200
    with pytest.raises(fanout.ProviderRequestError, match="3600"):
        fanout.ProviderRequest(
            "claude", "question", cwd=tmp_path, timeout=3601, profile=profile,
            timeout_override_reason="reviewed larger task",
            timeout_override_review_sha256="a" * 64,
        )


def _two_class_plan():
    source = fanout.SourceInfoV1(
        "plans/approved.md", hashlib.sha256(b"source").hexdigest(), "parser-v1",
    )
    steps = (
        fanout.SourceStepV1("Task 1/Step 1", "b" * 64),
        fanout.SourceStepV1("Task 2/Step 1", "c" * 64),
    )
    tasks = tuple(fanout.PlanTaskV1(
        id=name, kind="work", title=name, objective=f"Complete {name}.",
        source_step_ids=(steps[index].id,), execution_class=execution_class,
        none_reason="No specialist skill is needed.", acceptance=(f"{name} is complete.",),
    ) for index, (name, execution_class) in enumerate((
        ("read", "read-only"), ("write", "repo-write"),
    )))
    return fanout.FanoutPlanV1(source=source, source_steps=steps, tasks=tasks)


def _run_inputs(plan, profiles, *, profile_shape="class-tier"):
    return fanout.RunInputs(
        run_id="run-profile-test",
        compiled_plan_sha256=hashlib.sha256(fanout.canonical_json(plan.to_dict())).hexdigest(),
        source_sha256=plan.source.sha256,
        draft_sha256="d" * 64, compiler_sha256="e" * 64, parser_sha256="f" * 64,
        provider_profiles=profiles, profile_shape=profile_shape,
        skill_manifests={"read": "1" * 64, "write": "2" * 64},
    )


def test_run_input_profile_shape_disambiguates_custom_executor_and_rejects_mixed_map():
    """A valid flat custom ID can end with the exact suffix of a composite key."""
    plan = dataclasses.replace(
        _two_class_plan(),
        defaults=fanout.ProviderPolicyV1(executor_ids=("partner/read-only/standard", "codex")),
    )
    scheduler = sys.modules[f"{SPEC.name}.scheduler_authority"]
    flat = _run_inputs(
        plan, {"partner/read-only/standard": "1" * 64, "codex": "2" * 64},
        profile_shape="flat",
    )
    scheduler._validate_inputs(plan, flat)
    assert flat.to_dict()["schema_version"] == "fanout-run-inputs-v1"
    assert "profile_shape" not in flat.to_dict()
    assert fanout.RunInputs.from_dict(flat.to_dict()).digest == flat.digest

    composite = _run_inputs(plan, dict(fanout.ProviderRegistry.default().profile_digests))
    assert composite.to_dict()["schema_version"] == "fanout-run-inputs-v2"
    assert composite.to_dict()["profile_shape"] == "class-tier"
    assert fanout.RunInputs.from_dict(composite.to_dict()).digest == composite.digest
    with pytest.raises(fanout.RunStateError, match="profile"):
        dataclasses.replace(composite, provider_profiles={**composite.provider_profiles,
                                                         "codex": "3" * 64})
    with pytest.raises(fanout.RunStateError, match="profile shape"):
        dataclasses.replace(composite, profile_shape="unknown")
    with pytest.raises(fanout.RunStateError, match="fields"):
        fanout.RunInputs.from_dict({**composite.to_dict(), "profile_shape": "flat"})
    with pytest.raises(fanout.RunStateError, match="fields"):
        fanout.RunInputs.from_dict({**flat.to_dict(), "profile_shape": "class-tier"})


def test_legacy_flat_run_input_bytes_and_digest_are_unchanged():
    old_v1 = {
        "schema_version": "fanout-run-inputs-v1",
        "run_id": "legacy-run",
        "compiled_plan_sha256": "a" * 64,
        "source_sha256": "b" * 64,
        "draft_sha256": "c" * 64,
        "compiler_sha256": "d" * 64,
        "parser_sha256": "e" * 64,
        "provider_profiles": {"claude": "f" * 64},
        "skill_manifests": {"work": "1" * 64},
        "repo_baseline_sha256": None,
    }
    old_bytes = fanout.canonical_json(old_v1)

    restored = fanout.RunInputs.from_dict(json.loads(old_bytes))

    assert restored.profile_shape == "flat"
    assert fanout.canonical_json(restored.to_dict()) == old_bytes
    assert restored.digest == hashlib.sha256(old_bytes).hexdigest()


def test_read_only_profile_change_only_affects_read_only_work():
    """A read-only profile update must not force unrelated write seats to be unscheduled."""
    plan = _two_class_plan()
    profiles = dict(fanout.ProviderRegistry.default().profile_digests)
    old = _run_inputs(plan, profiles)
    changed = dict(profiles)
    changed["claude/read-only/standard"] = "9" * 64
    new = _run_inputs(plan, changed)
    scheduler = sys.modules[f"{SPEC.name}.scheduler_authority"]
    execute = sys.modules[f"{SPEC.name}.execute"]

    assert scheduler._input_affected_work(plan, plan, old, new) == {"read"}
    assert execute._affected_amendment(plan, plan, old, new, 2).affected_task_ids == ("read",)


def test_profile_transition_registry_binding_admits_each_executor_once():
    """Profile keys must not be mistaken for CLI executable IDs during CAS recovery."""
    registry = fanout.ProviderRegistry.default(version_probe=lambda _executor: "unused")
    execute = sys.modules[f"{SPEC.name}.execute"]

    _require_identity, adapters = execute._registry_binding(
        registry, dict(registry.profile_digests),
    )

    assert tuple(executor for executor, _adapter in adapters) == ("agy", "claude", "codex")


def test_new_run_inputs_must_cover_each_task_class_and_tier():
    """One executor digest cannot stand in for both read-only and write profiles."""
    plan = _two_class_plan()
    profiles = dict(fanout.ProviderRegistry.default().profile_digests)
    scheduler = sys.modules[f"{SPEC.name}.scheduler_authority"]

    scheduler._validate_inputs(plan, _run_inputs(plan, profiles))
    profiles.pop("claude/repo-write/standard")

    with pytest.raises(fanout.SchedulerStateError, match="provider profiles"):
        scheduler._validate_inputs(plan, _run_inputs(plan, profiles))


def test_deep_tier_is_read_only_until_a_write_profile_is_characterized():
    """A deep write plan must not inherit the read-only deep profile's permissions."""
    plan = _two_class_plan()

    with pytest.raises(fanout.PlanValidationError, match="deep"):
        dataclasses.replace(plan, defaults=fanout.ProviderPolicyV1(quality_tier="deep"))


def _read_preparation(tmp_path, plan, inputs, registry):
    artifacts = fanout.ArtifactStore(tmp_path / "artifacts")
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    resolver = fanout.SkillResolver((fanout.SkillRoot("test", skill_root, 0),))
    skill_bundle = fanout.canonical_json({"schema_version": "fanout-seat-skill-bundle-v1", "skills": []})
    seats = []
    for executor in plan.defaults.executor_ids:
        admission = resolver.admit((), task_id="read", seat_id=executor, provider=executor,
                                   session_id=f"session-{executor}")
        admission = resolver.stage(admission, tmp_path / f"staged-{executor}")
        admission = resolver.verify_engine_delivery(admission, ())
        seats.append(fanout.SeatAssignment.from_admission(
            admission, skill_bundle=skill_bundle, artifacts=artifacts,
        ))
    task = plan.tasks[0]
    encoded_task = fanout.canonical_json(task.to_dict())
    packet = fanout.TaskPacket(
        run_id=inputs.run_id, task_id="read", attempt=1,
        compiled_plan=fanout.canonical_json(plan.to_dict()),
        compiled_plan_sha256=inputs.compiled_plan_sha256,
        source_markdown=b"source",
        task=encoded_task, task_sha256=hashlib.sha256(encoded_task).hexdigest(),
        skill_bundle=skill_bundle, skill_manifest_sha256=inputs.skill_manifests["read"],
        dependency_artifacts=(), execution_class="read-only", cwd=tmp_path,
    )
    return fanout.TaskPreparation(
        packet, tuple(seats), fanout.RoundPolicy.from_provider_policy(plan.defaults),
        inputs, 1, registry=registry,
    )


def test_preparation_binds_each_selected_digest_and_effective_timeout(tmp_path):
    """A task packet must not be reused with a different profile/timeout after planning."""
    plan = _two_class_plan()
    registry = fanout.ProviderRegistry.default(version_probe=lambda _executor: "unused")
    inputs = _run_inputs(plan, dict(registry.profile_digests))

    preparation = _read_preparation(tmp_path, plan, inputs, registry)

    assert preparation.profile_bindings == {
        executor: (registry.select(executor, "read-only", "standard").digest, 900)
        for executor in plan.defaults.executor_ids
    }
    assert preparation.digest


def test_preparation_binds_agy_guard_receipt_when_seat_is_guarded(tmp_path, monkeypatch):
    """Changing the seat's native guard must change the durable preparation identity."""
    plan = _two_class_plan()
    registry = fanout.ProviderRegistry.default(version_probe=lambda _executor: "unused")
    inputs = _run_inputs(plan, dict(registry.profile_digests))
    preparation = _read_preparation(tmp_path, plan, inputs, registry)
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(("git", "init", "-q", str(repository)), check=True)
    (repository / "fixture.txt").write_text("fixture\n")
    subprocess.run(("git", "-C", str(repository), "add", "fixture.txt"), check=True)
    subprocess.run(("git", "-C", str(repository), "-c", "user.name=Test",
                    "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"), check=True)
    baseline = fanout.capture_repository_baseline(repository)
    controller = fanout.create_lifecycle_controller(tmp_path / "controller")
    workspace = fanout.create_seat_workspace(
        baseline, controller.root / "workspaces", "agy",
    )
    verification = fanout.verify_seat_workspace(baseline, workspace, controller=controller)
    monkeypatch.setenv("AGY_ADC_AUTH", "true")
    profile = registry.select("agy", "read-only", "standard")
    guard = fanout.issue_agy_readonly_guard(controller, verification, profile)
    seats_without_guard = tuple(
        dataclasses.replace(seat, workspace_verification=verification)
        if seat.executor_id == "agy" else seat
        for seat in preparation.seats
    )
    seats_with_guard = tuple(
        dataclasses.replace(seat, agy_guard=guard)
        if seat.executor_id == "agy" else seat
        for seat in seats_without_guard
    )

    unguarded = dataclasses.replace(preparation, seats=seats_without_guard)
    guarded = dataclasses.replace(preparation, seats=seats_with_guard)

    assert guarded.digest != unguarded.digest
    assert next(seat for seat in guarded.seats if seat.executor_id == "agy").agy_guard.receipt_sha256 == guard.receipt_sha256


def test_real_registry_preparation_rejects_legacy_flat_shape(tmp_path):
    plan = _two_class_plan()
    registry = fanout.ProviderRegistry.default(version_probe=lambda _executor: "unused")
    inputs = _run_inputs(plan, {name: "9" * 64 for name in plan.defaults.executor_ids},
                         profile_shape="flat")

    with pytest.raises(fanout.ExecutionValidationError, match="profile shape"):
        _read_preparation(tmp_path, plan, inputs, registry)
