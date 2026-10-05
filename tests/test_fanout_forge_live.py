"""The Forge boundary must cover each profiled live provider launch."""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest


VERSIONS = {"claude": "2.1.284", "codex": "0.160.0", "agy": "1.2.13"}
CLAUDE_SESSION = "11111111-1111-4111-8111-111111111111"
ROOT = Path(__file__).resolve().parents[1]


def test_quality_agy_selection_feeds_only_genuine_native_value_to_unchanged_extractor(tmp_path, monkeypatch):
    live = _live()
    session = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    result = {"status": "SUCCESS", "conversation_id": session,
              "model": "gemini-3.8-flash-high", "response": "inert actual child answer"}
    stream = b"\n".join(json.dumps(event).encode() for event in (
        {"event": "init", "conversation_id": session, "init": {}},
        {"event": "result", "result": result})) + b"\n"
    child = subprocess.run((sys.executable, "-c", "import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())"),
                           input=stream, check=True, capture_output=True)
    with live.fanout.ArtifactStore(tmp_path / "raw") as store:
        selected, proof = live.select_quality_agy_result(child.stdout, store, "selected",
                                                        expected_session=session)
        assert json.loads(selected) == result
        assert store.read_bytes(live.fanout.ArtifactRef(**proof["stream"])) == child.stdout
        archive = Path(os.environ["FANOUT_QUALITY_ARCHIVE_ROOT"])
        checks = Path(os.environ["FANOUT_QUALITY_ORIGINAL_CHECKS"])
        monkeypatch.delitem(sys.modules, "khenrix_checks", raising=False)
        files = {name: live.fixtures.hashlib.sha256(data).hexdigest()
                 for name, data in live.fixtures._archive_files(archive).items()}
        files["scripts/lib/checks.py"] = live.fixtures._ORIGINAL_CHECKS_SHA256
        stage = tmp_path / "archive"
        receipt = live.fixtures.stage_quality_archive(archive, stage, files, {}, original_checks=checks)
        modules, _ = live.fixtures.load_quality_archive(stage, receipt, ("council.engine",))
        original = modules["council.engine"]
        assert original.extract_agy_json(selected.decode()) == (result["response"], None)
        monkeypatch.delitem(sys.modules, "khenrix_checks")
        for index, invalid in enumerate((child.stdout + child.stdout.splitlines()[-1] + b"\n",
                child.stdout.replace(session.encode(), b"foreign", 1), child.stdout[:-10],
                child.stdout.replace(b'"SUCCESS"', b'"ERROR"'))):
            with pytest.raises(live.fanout.ProviderProtocolError):
                live.select_quality_agy_result(invalid, store, f"bad-{index}", expected_session=session)


def _live():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    try:
        return importlib.import_module("scripts.fanout_forge_live")
    except ImportError as error:
        pytest.fail(f"Forge live provider bridge is missing: {error}")


def _request(live, registry, executor_id: str, workspace: Path, *,
             resume: bool = False, retries: int = 0, execution_class: str = "repo-write"):
    session_id = (
        CLAUDE_SESSION if executor_id == "claude" else f"exact-{executor_id}-session"
    ) if resume else None
    return live.fanout.ProviderRequest(
        executor_id, "private prompt", cwd=workspace,
        execution_class=execution_class,
        profile=registry.select(executor_id, execution_class, "standard"),
        session_id=session_id, resume=resume, retries=retries,
    )


def _unstarted(live):
    return live.fanout.ProcessResult(
        status=live.fanout.ProcessStatus.SPAWN_ERROR, returncode=None,
        stdout=b"", stderr=b"", error="synthetic unstarted child",
    )


def _fake_provider(live, request, runner, *, registry):
    return live.fanout.providers._run_provider_with_runner(
        request, registry=registry, runner=runner,
    )


def _fake_boundary(live, root: Path):
    return live.fixtures.ForgeBoundaryDescriptor(
        root, root / "baseline", root / "consumer", (root,),
        tuple((name, Path(f"/usr/bin/{name}")) for name in ("claude", "codex", "agy")),
        "0" * 64, "0" * 64,
    )


@pytest.fixture
def _allow_hermetic_bridge(monkeypatch):
    # Existing fake-provider tests exercise command wrapping after the live gate.
    live = _live()
    monkeypatch.delenv("AGY_ADC_AUTH", raising=False)
    real_registry = live.ForgeLiveRegistry
    monkeypatch.setattr(live, "ForgeLiveRegistry", lambda case_id, workspace, **kwargs:
                        real_registry(case_id, workspace,
                                      boundary=_fake_boundary(live, Path(workspace)), **kwargs))
    monkeypatch.setattr(live.fixtures, "require_blindness_attestation",
                        lambda **_kwargs: None)
    monkeypatch.setattr(live.fixtures, "seat_environment",
                        lambda *_args: {"HOME": "/private/tmp",
                                        "XDG_CONFIG_HOME": "/private/tmp/.config",
                                        "TMPDIR": "/private/tmp"})
    monkeypatch.setattr(live, "_sealed_version_probe",
                        lambda _boundary, executor: VERSIONS[executor])


def test_direct_live_bridge_requires_blindness_attestation_before_any_spawn(
    tmp_path: Path, monkeypatch,
) -> None:
    live = _live()
    registry = live.ForgeLiveRegistry(
        "forge-01", tmp_path, boundary=_fake_boundary(live, tmp_path),
    )
    wrapped = []
    launched = []
    monkeypatch.setattr(
        live.fixtures, "executor_argv",
        lambda case_id, workspace, argv, *, boundary: (
            wrapped.append((case_id, workspace, argv))
            or ["/usr/bin/sandbox-exec", "-p", "legacy-policy", *argv]
        ),
    )

    def runner(command):
        launched.append(command)
        return _unstarted(live)

    with pytest.raises(live.fixtures.FixtureError, match="boundary|root|auth|resume"):
        _fake_provider(
            live, _request(live, registry, "claude", tmp_path), runner, registry=registry,
        )

    assert wrapped == []
    assert launched == []


@pytest.mark.usefixtures("_allow_hermetic_bridge")
def test_public_repo_write_launch_rejects_even_with_forge_wrapper(
    tmp_path: Path, monkeypatch,
) -> None:
    live = _live()
    registry = live.ForgeLiveRegistry("forge-01", tmp_path)
    wrapped = []
    launched = []
    monkeypatch.setattr(live.fixtures, "executor_argv",
                        lambda *_args, **_kwargs: wrapped.append("wrapped"))
    monkeypatch.setattr(live.fanout.providers, "run_command", launched.append)

    with pytest.raises(live.fanout.ProviderRequestError, match="native seat boundary"):
        live.fanout.run_provider(
            _request(live, registry, "claude", tmp_path), registry=registry,
        )

    assert wrapped == []
    assert launched == []


def test_registry_rejects_custom_version_probe_before_callback_can_run(
    tmp_path: Path,
) -> None:
    live = _live()
    marker = tmp_path / "unsafe-version-probe-ran"

    def unsafe_probe(_executor: str) -> str:
        marker.write_text("unsandboxed", encoding="utf-8")
        return VERSIONS["claude"]

    with pytest.raises(live.fixtures.FixtureError, match="version probe|callback"):
        live.ForgeLiveRegistry(
            "forge-01", tmp_path, boundary=_fake_boundary(live, tmp_path),
            version_probe=unsafe_probe,
        )
    assert not marker.exists()


def test_run_provider_does_not_probe_path_cli_before_sealed_preflight(
    tmp_path: Path, monkeypatch,
) -> None:
    live = _live()
    marker = tmp_path / "unsafe-path-cli-ran"

    def unsafe_probe(_executor: str) -> str:
        marker.write_text("unsandboxed", encoding="utf-8")
        return VERSIONS["claude"]

    monkeypatch.setattr(live.fanout.providers, "_installed_cli_version", unsafe_probe)
    registry = live.ForgeLiveRegistry(
        "forge-01", tmp_path, boundary=_fake_boundary(live, tmp_path),
    )
    with pytest.raises((live.fixtures.FixtureError, live.fanout.ProviderRequestError)):
        _fake_provider(
            live, _request(live, registry, "claude", tmp_path),
            lambda _command: _unstarted(live), registry=registry,
        )
    assert not marker.exists()


@pytest.mark.parametrize("executor_id", ("claude", "codex", "agy"))
def test_pinned_version_probe_uses_the_same_sealed_seat_and_private_environment(
    tmp_path: Path, monkeypatch, executor_id: str,
) -> None:
    live = _live()
    seat = tmp_path / "seat"
    seat.mkdir()
    home = tmp_path / "private-home"
    home.mkdir(mode=0o700)
    config = home / ".config"
    config.mkdir(mode=0o700)
    temporary = home / "tmp"
    temporary.mkdir(mode=0o700)
    boundary = _fake_boundary(live, tmp_path)
    observed = []
    monkeypatch.setenv("UNRELATED_API_TOKEN", "dummy-do-not-inherit")
    monkeypatch.setenv("AGY_ADC_AUTH", "true")
    monkeypatch.setattr(live.fixtures, "require_blindness_attestation",
                        lambda **kwargs: observed.append(("attest", kwargs)))
    monkeypatch.setattr(live.fixtures, "seat_environment",
                        lambda *_args: {"HOME": str(home), "XDG_CONFIG_HOME": str(config),
                                        "TMPDIR": str(temporary)})

    def wrapper(case_id, workspace, argv, *, boundary):
        observed.append(("wrap", case_id, workspace, tuple(argv), boundary))
        return ["/usr/bin/sandbox-exec", "-p", "sealed-policy", *argv]

    def runner(argv, **kwargs):
        observed.append(("run", tuple(argv), kwargs))
        lines = {
            "claude": b"2.1.284 (Claude Code)\n",
            "codex": b"codex-cli 0.160.0\n",
            "agy": b"1.2.13\n",
        }
        return subprocess.CompletedProcess(argv, 0, stdout=lines[executor_id], stderr=b"")

    monkeypatch.setattr(live.fixtures, "executor_argv", wrapper)
    monkeypatch.setattr(subprocess, "run", runner)
    registry = live.ForgeLiveRegistry("forge-01", seat, boundary=boundary)
    registry.assert_installed_version(registry.select(executor_id, "repo-write", "standard"))
    assert [item[0] for item in observed] == ["attest", "wrap", "run"]
    assert observed[0][1] == {"boundary": boundary, "seat_workspace": seat}
    assert observed[1][3] == (
        str(dict(boundary.executable_paths)[executor_id]), "--version",
    )
    assert observed[2][1] == (
        "/usr/bin/sandbox-exec", "-p", "sealed-policy",
        str(dict(boundary.executable_paths)[executor_id]), "--version",
    )
    assert observed[2][2]["cwd"] == seat
    assert observed[2][2]["env"]["HOME"] == str(home)
    assert "UNRELATED_API_TOKEN" not in observed[2][2]["env"]
    assert "AGY_ADC_AUTH" not in observed[2][2]["env"]


def test_forge_bridge_rejects_base_adapter_credential_overrides_before_spawn(
    tmp_path: Path, monkeypatch,
) -> None:
    live = _live()
    boundary = _fake_boundary(live, tmp_path)
    guard = live._ForgeBoundary("forge-01", tmp_path, boundary)
    profile = live.fanout.ProviderRegistry.default().select(
        "claude", "repo-write", "standard",
    )
    request = live.fanout.ProviderRequest(
        "claude", "private prompt", cwd=tmp_path, execution_class="repo-write",
        profile=profile,
    )
    command = live.fanout.ProcessCommand(
        argv=("claude", "--print"), stdin=b"private prompt", cwd=tmp_path,
        environment={"GOOGLE_APPLICATION_CREDENTIALS": "/private/tmp/dummy-adc"},
    )
    spawned = []
    monkeypatch.setattr(live.fixtures, "require_blindness_attestation",
                        lambda **_kwargs: None)
    monkeypatch.setattr(live.fixtures, "executor_argv", lambda *_args, **_kwargs:
                        spawned.append("wrapped") or ["/usr/bin/sandbox-exec", "-p", "x"])
    with pytest.raises(live.fixtures.FixtureError, match="credential|environment"):
        guard.wrap(request, command)
    assert spawned == []


@pytest.mark.usefixtures("_allow_hermetic_bridge")
def test_base_claude_adapter_cannot_route_ambient_adc_into_a_forge_seat(
    tmp_path: Path, monkeypatch,
) -> None:
    live = _live()
    monkeypatch.setenv("AGY_ADC_AUTH", "true")
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setattr(live.fanout.providers, "default_claude_adc_path",
                        lambda: tmp_path / "dummy-adc")
    registry = live.ForgeLiveRegistry("forge-01", tmp_path)
    spawned = []
    monkeypatch.setattr(live.fixtures, "executor_argv",
                        lambda *_args, **_kwargs: spawned.append("wrapped"))
    with pytest.raises(live.fixtures.FixtureError, match="credential environment"):
        _fake_provider(
            live, _request(live, registry, "claude", tmp_path),
            spawned.append, registry=registry,
        )
    assert spawned == []


def test_live_registry_rejects_missing_campaign_boundary_before_provider_launch(
    tmp_path: Path,
) -> None:
    live = _live()
    with pytest.raises(live.fixtures.FixtureError, match="boundary|descriptor"):
        live.ForgeLiveRegistry(
            "forge-01", tmp_path,
        )


def test_live_bridge_uses_seat_home_and_resolved_binary_on_initial_and_resume(
    tmp_path: Path, monkeypatch,
) -> None:
    """A resume must remain in one private HOME and avoid the ambient agy wrapper."""
    live = _live()
    monkeypatch.delenv("AGY_ADC_AUTH", raising=False)
    seat = tmp_path / "seat"
    seat.mkdir()
    boundary = _fake_boundary(live, tmp_path)
    home = tmp_path / "private-home"
    home.mkdir(mode=0o700)
    config = home / ".config"
    config.mkdir(mode=0o700)
    sandboxed = []
    monkeypatch.setattr(live.fixtures, "require_blindness_attestation",
                        lambda **_kwargs: None)
    monkeypatch.setattr(live.fixtures, "seat_environment",
                        lambda *_args: {"HOME": str(home), "XDG_CONFIG_HOME": str(config),
                                        "TMPDIR": str(home)})

    def wrapper(_case_id, _workspace, argv, *, boundary):
        sandboxed.append((tuple(argv), boundary))
        return ["/usr/bin/sandbox-exec", "-p", "validated", *argv]

    monkeypatch.setattr(live.fixtures, "executor_argv", wrapper)
    registry = live.ForgeLiveRegistry(
        "forge-01", seat, boundary=boundary,
    )
    for resume in (False, True):
        command = registry.require("agy").build_command(
            _request(live, registry, "agy", seat, resume=resume),
        )
        assert command.environment["HOME"] == str(home)
        assert command.argv[3] == str(dict(boundary.executable_paths)["agy"])
        assert ("--conversation" in command.argv) is resume
    assert [item[1] for item in sandboxed] == [boundary, boundary]


@pytest.mark.usefixtures("_allow_hermetic_bridge")
@pytest.mark.parametrize("executor_id", ("claude", "codex", "agy"))
@pytest.mark.parametrize("resume", (False, True))
def test_each_initial_and_resume_launch_uses_boundary_around_actual_profile_argv(
    tmp_path: Path, monkeypatch, executor_id: str, resume: bool,
) -> None:
    """Omitting the wrapper on any provider turn would expose private Forge files."""
    live = _live()
    registry = live.ForgeLiveRegistry(
        "forge-01", tmp_path,
    )
    wrapped: list[tuple[str, Path, list[str]]] = []
    launched = []

    def boundary(case_id, workspace, argv, *, boundary):
        wrapped.append((case_id, workspace, argv))
        return ["/usr/bin/sandbox-exec", "-p", "validated-policy", *argv]

    def runner(command):
        launched.append(command)
        return _unstarted(live)

    monkeypatch.setattr(live.fixtures, "executor_argv", boundary)
    result = _fake_provider(
        live, _request(live, registry, executor_id, tmp_path, resume=resume),
        runner, registry=registry,
    )

    assert result.reason == "spawn-error"
    defaults = live.fanout.ProviderRegistry.default()
    for base_profile in defaults._profiles.values():
        bridged = registry.select(*base_profile.key)
        expected = (replace(base_profile, characterized=True, profile_sha256=None)
                    if base_profile.execution_class == "repo-write" else base_profile)
        assert bridged.digest == expected.digest
    assert len(wrapped) == len(launched) == 1
    assert wrapped[0][:2] == ("forge-01", tmp_path)
    assert launched[0].argv == (
        "/usr/bin/sandbox-exec", "-p", "validated-policy", *wrapped[0][2],
    )
    assert launched[0].stdin and b"private prompt" in launched[0].stdin
    assert launched[0].cwd == tmp_path
    assert wrapped[0][2][0] == str(dict(_fake_boundary(live, tmp_path).executable_paths)[executor_id])
    assert ("--resume" in wrapped[0][2] if executor_id == "claude" else
            "resume" in wrapped[0][2] if executor_id == "codex" else
            "--conversation" in wrapped[0][2]) is resume


@pytest.mark.usefixtures("_allow_hermetic_bridge")
@pytest.mark.parametrize("executor_id", ("claude", "codex", "agy"))
def test_unstarted_retry_rebuilds_and_rewraps_every_spawn(
    tmp_path: Path, monkeypatch, executor_id: str,
) -> None:
    """A retry may not reuse an unwrapped command or skip the boundary check."""
    live = _live()
    registry = live.ForgeLiveRegistry(
        "forge-01", tmp_path,
    )
    wrapped = []
    launched = []

    def boundary(_case_id, _workspace, argv, *, boundary):
        wrapped.append(tuple(argv))
        return ["/usr/bin/sandbox-exec", "-p", f"validated-{len(wrapped)}", *argv]

    def runner(command):
        launched.append(command)
        return _unstarted(live)

    monkeypatch.setattr(live.fixtures, "executor_argv", boundary)
    result = _fake_provider(
        live, _request(live, registry, executor_id, tmp_path, retries=1),
        runner, registry=registry,
    )

    assert result.attempt_count == 2
    assert len(wrapped) == len(launched) == 2
    assert [command.argv[:3] for command in launched] == [
        ("/usr/bin/sandbox-exec", "-p", "validated-1"),
        ("/usr/bin/sandbox-exec", "-p", "validated-2"),
    ]
    assert all(command.argv[3:] == argv for command, argv in zip(launched, wrapped))
    if executor_id == "claude":
        assert wrapped[0] == wrapped[1]


@pytest.mark.usefixtures("_allow_hermetic_bridge")
@pytest.mark.parametrize("executor_id", ("claude", "codex", "agy"))
def test_boundary_denial_prevents_spawn_even_with_retry_budget(
    tmp_path: Path, monkeypatch, executor_id: str,
) -> None:
    """A failed sandbox preflight must abort before any paid process starts."""
    live = _live()
    registry = live.ForgeLiveRegistry(
        "forge-01", tmp_path,
    )
    launched = []

    def deny(_case_id, _workspace, _argv, *, boundary):
        raise live.fixtures.FixtureError("sandbox preflight denied")

    monkeypatch.setattr(live.fixtures, "executor_argv", deny)
    with pytest.raises(live.fixtures.FixtureError, match="sandbox preflight denied"):
        _fake_provider(
            live, _request(live, registry, executor_id, tmp_path, retries=2),
            launched.append, registry=registry,
        )
    assert launched == []


@pytest.mark.usefixtures("_allow_hermetic_bridge")
@pytest.mark.parametrize("unsafe_prefix", (
    (), ("/usr/bin/env",), ("/usr/bin/sandbox-exec", "-p", ""),
))
def test_boundary_rejects_a_missing_or_invalid_sandbox_prefix(
    tmp_path: Path, monkeypatch, unsafe_prefix: tuple[str, ...],
) -> None:
    """An unexpected fixture wrapper must not reach the provider runner."""
    live = _live()
    registry = live.ForgeLiveRegistry(
        "forge-01", tmp_path,
    )
    launched = []
    monkeypatch.setattr(
        live.fixtures, "executor_argv",
        lambda _case_id, _workspace, argv, *, boundary: [*unsafe_prefix, *argv],
    )
    with pytest.raises(live.fanout.ProviderRequestError, match="sandbox"):
        _fake_provider(
            live, _request(live, registry, "claude", tmp_path),
            launched.append, registry=registry,
        )
    assert launched == []


@pytest.mark.usefixtures("_allow_hermetic_bridge")
def test_retry_stops_when_the_second_boundary_preflight_denies(
    tmp_path: Path, monkeypatch,
) -> None:
    """A first unstarted spawn cannot let a later boundary failure launch bare."""
    live = _live()
    registry = live.ForgeLiveRegistry(
        "forge-01", tmp_path,
    )
    calls = 0
    launched = []

    def boundary(_case_id, _workspace, argv, *, boundary):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise live.fixtures.FixtureError("sandbox changed before retry")
        return ["/usr/bin/sandbox-exec", "-p", "validated-policy", *argv]

    def runner(command):
        launched.append(command)
        return _unstarted(live)

    monkeypatch.setattr(live.fixtures, "executor_argv", boundary)
    with pytest.raises(live.fixtures.FixtureError, match="sandbox changed"):
        _fake_provider(
            live, _request(live, registry, "claude", tmp_path, retries=1),
            runner, registry=registry,
        )
    assert calls == 2
    assert len(launched) == 1
    assert launched[0].argv[:3] == (
        "/usr/bin/sandbox-exec", "-p", "validated-policy",
    )


@pytest.mark.usefixtures("_allow_hermetic_bridge")
def test_version_drift_blocks_before_boundary_or_launch(tmp_path: Path, monkeypatch) -> None:
    """The Forge path must keep the registry's installed CLI version gate."""
    live = _live()
    registry = live.ForgeLiveRegistry(
        "forge-01", tmp_path,
    )
    monkeypatch.setattr(live, "_sealed_version_probe", lambda _boundary, _executor: "0.0.0")
    called = []
    monkeypatch.setattr(live.fixtures, "executor_argv",
                        lambda *_args, **_kwargs: called.append("wrapped"))
    with pytest.raises(live.fanout.ProviderRequestError, match="version drifted"):
        _fake_provider(
            live, _request(live, registry, "codex", tmp_path),
            called.append, registry=registry,
        )
    assert called == []


@pytest.mark.usefixtures("_allow_hermetic_bridge")
def test_agy_read_only_still_requires_native_guard(tmp_path: Path, monkeypatch) -> None:
    """The outer Forge sandbox must not bypass agy's inner read-only guard."""
    live = _live()
    registry = live.ForgeLiveRegistry(
        "forge-01", tmp_path,
    )
    called = []
    monkeypatch.setattr(live.fixtures, "executor_argv", called.append)
    monkeypatch.setattr(live.fanout.providers, "run_command", called.append)
    with pytest.raises(live.fanout.ProviderRequestError, match="native guard"):
        live.fanout.run_provider(
            _request(live, registry, "agy", tmp_path, execution_class="read-only"),
            registry=registry,
        )
    assert called == []


@pytest.mark.usefixtures("_allow_hermetic_bridge")
def test_request_cwd_must_match_the_bound_seat(tmp_path: Path, monkeypatch) -> None:
    """A registry bound to one seat must not launch from a different cwd."""
    live = _live()
    seat = tmp_path / "seat"
    other = tmp_path / "other"
    seat.mkdir()
    other.mkdir()
    registry = live.ForgeLiveRegistry(
        "forge-01", seat,
    )
    called = []
    monkeypatch.setattr(live.fixtures, "executor_argv", called.append)
    with pytest.raises(live.fanout.ProviderRequestError, match="seat workspace"):
        _fake_provider(
            live, _request(live, registry, "claude", other),
            called.append, registry=registry,
        )
    assert called == []


def test_real_fixture_preflight_builds_native_sandbox_command_without_provider_call(
    tmp_path: Path, monkeypatch,
) -> None:
    """The production bridge reaches fixture pin and native sandbox probes."""
    if sys.platform != "darwin":
        pytest.skip("native Forge boundary requires macOS sandbox-exec")
    live = _live()
    monkeypatch.delenv("AGY_ADC_AUTH", raising=False)
    baseline = tmp_path / "baseline"
    consumer = tmp_path / "consumer"
    baseline.mkdir()
    consumer.mkdir()
    boundary = live.fixtures.open_blindness_boundary(
        tmp_path / "campaign", baseline, consumer, resume=False,
    )
    seat = boundary.run_root / "cases" / "forge-01" / "seats" / "claude"
    live.fixtures.materialize("forge-01", seat)
    registry = live.ForgeLiveRegistry("forge-01", seat, boundary=boundary)
    request = _request(live, registry, "claude", seat)
    monkeypatch.setattr(live.fixtures, "require_blindness_attestation",
                        lambda **_kwargs: None)
    try:
        command = registry.require("claude").build_command(request)
    except live.fixtures.FixtureError as error:
        if str(error) == "sandbox-exec cannot apply in this process":
            pytest.skip("nested tool sandbox cannot apply macOS sandbox-exec")
        raise
    assert command.argv[:2] == ("/usr/bin/sandbox-exec", "-p")
    assert command.argv[3:5] == (
        str(dict(boundary.executable_paths)["claude"]), "--print",
    )
    for name, binary in boundary.executable_paths:
        sealed_version = live._sealed_version_probe(registry._seat_boundary, name)
        env = live.fanout.process.build_child_environment(
            overrides=live.fixtures.seat_environment(boundary, seat, name),
        )
        probe = live.fixtures.executor_argv(
            "forge-01", seat, [str(binary), "--version"], boundary=boundary,
        )
        version = subprocess.run(
            probe, cwd=seat, env=env, capture_output=True, text=True, timeout=15,
        )
        assert version.returncode == 0, (name, version.stderr)
        expected_output = {
            "claude": f"{sealed_version} (Claude Code)",
            "codex": f"codex-cli {sealed_version}",
            "agy": sealed_version,
        }
        assert version.stdout.strip() == expected_output[name]


@pytest.mark.parametrize("provider,tier,model,effort", (
    ("claude", "standard", "claude-opus-5-5", "medium"),
    ("codex", "standard", "gpt-6.1-sol", "medium"),
    ("agy", "standard", "gemini-3.8-flash-high", "high"),
    ("claude", "deep", "claude-opus-5-5", "ultracode"),
    ("codex", "deep", "gpt-6.1-sol", "ultra"),
    ("agy", "deep", "gemini-3.8-flash-high", "high"),
))
def test_quality_original_spec_preserves_real_profile_model_and_effort(
        tmp_path, monkeypatch, provider, tier, model, effort):
    live = _live()
    from shared.lib.fanout.broker import BrokerPermit
    from shared.lib.fanout.diagnostic import seal_quality_allocation
    monkeypatch.delitem(sys.modules, "khenrix_checks", raising=False)
    archive = Path(os.environ["FANOUT_QUALITY_ARCHIVE_ROOT"])
    checks = Path(os.environ["FANOUT_QUALITY_ORIGINAL_CHECKS"])
    original = live.fixtures._archive_files(archive)
    expected_files = {name: live.fixtures.hashlib.sha256(data).hexdigest()
                      for name, data in original.items()}
    expected_files["scripts/lib/checks.py"] = live.fixtures._ORIGINAL_CHECKS_SHA256
    stage = tmp_path / "archive"
    receipt = live.fixtures.stage_quality_archive(archive, stage, expected_files,
        live.fixtures.quality_archive_projection(original), original_checks=checks)
    profile = live.fanout.ProviderRegistry.default().select(provider, "read-only", tier)
    request = live.fanout.ProviderRequest(provider, "inert original spec fixture",
        cwd=tmp_path, execution_class="read-only", profile=profile, timeout=18, retries=0)
    controller = live.fanout.create_lifecycle_controller(tmp_path / "controller")
    bindings = {name: "b" * 64 for name in ("campaign", "casebook", "source",
                "projection", "profile", "inputs")}
    permit = BrokerPermit(provider, "run", "task", "target", "seat", "initial", model,
        "disposable-placeholder", 1, 1024, 2048, 8192, inputs_digest=bindings["inputs"])
    row = {"role": "baseline-seat", "phase": "initial", "condition": "baseline",
           "epoch": 1, "permit": live.asdict(permit), "blocker": None, "initial": None,
           "profile": profile.digest, "catalog": "c" * 64 if provider == "codex" else None,
           "process_seconds": 18, "outer_seconds": 30}
    with live.fanout.ArtifactStore(tmp_path / "artifacts") as store:
        ref = store.write_json("allocation.json", {"schema": "fanout-quality-case-allocation-v1",
            "case_id": "normal-01", "controller_id": controller.controller_id,
            "bindings": bindings, "maka_output_tokens": 8192, "rows": [row]})
        authority = seal_quality_allocation(controller, store, ref, bindings=bindings)
        transport = live.QualityOriginalTransport(stage, receipt, [{"request": request,
            "turn": {"controller": controller, "store": store, "authority": authority,
                     "bindings": bindings, "role": "baseline-seat", "phase": "initial",
                     "condition": "baseline", "epoch": 1}}])
        actual = transport.build_real_spec(provider, request.prompt, 18,
            {provider: {"model": model, "thinking": effort}}, tmp_path)
        assert type(actual) is transport.modules["council.engine"].ProviderSpec
        assert actual.model == model and actual.thinking == effort
        assert actual.stdin == request.prompt and actual.cwd == str(tmp_path)
