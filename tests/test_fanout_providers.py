"""Fixture-only contracts for resumable fanout executor adapters."""
from __future__ import annotations

import ctypes
import hashlib
import importlib.util
import json
import os
import sys
import time
import tomllib
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest

ROOT = Path(__file__).resolve().parents[1]
FANOUT_ROOT = ROOT / "shared" / "lib" / "fanout"
# Council's immutable compatibility tests intentionally import their legacy
# `fanout.py` as ``fanout``. Load this package under its own test-only name so
# the aggregate deterministic gate has no collection-order dependency.
SPEC = importlib.util.spec_from_file_location(
    "_fanout_provider_contracts",
    FANOUT_ROOT / "__init__.py",
    submodule_search_locations=[str(FANOUT_ROOT)],
)
assert SPEC and SPEC.loader
fanout = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fanout
SPEC.loader.exec_module(fanout)

ArtifactStore = fanout.ArtifactStore
ArtifactQuotaError = fanout.ArtifactQuotaError
ProcessResult = fanout.ProcessResult
ProcessStatus = fanout.ProcessStatus
ProviderProtocolError = fanout.ProviderProtocolError
ProviderRequestError = fanout.ProviderRequestError
UnsupportedExecutorError = fanout.UnsupportedExecutorError
ProviderRegistry = fanout.ProviderRegistry
ProviderCapabilities = fanout.ProviderCapabilities
ProviderAdapter = fanout.ProviderAdapter
ExecutorProfile = fanout.ExecutorProfile
ProviderRequest = fanout.ProviderRequest
ProviderResult = fanout.ProviderResult
RoutingTargetRegistry = fanout.RoutingTargetRegistry
run_many = fanout.run_many
run_provider = fanout.run_provider
providers = sys.modules[f"{SPEC.name}.providers"]


FIXTURES = ROOT / "tests" / "fixtures" / "fanout_providers"


@pytest.fixture(autouse=True)
def _pin_fixture_cli_versions(monkeypatch):
    # Deterministic provider tests never invoke installed CLIs.
    versions = {"claude": "2.1.281", "codex": "0.157.1", "agy": "1.2.12"}
    monkeypatch.setattr(providers, "_installed_cli_version", lambda executor: versions[executor])


@pytest.fixture
def _mock_clean_codex_host(monkeypatch):
    # Command/protocol contracts are independent of each runner's managed host.
    # The dedicated preflight tests below exercise the real check.
    monkeypatch.setattr(providers, "_codex_read_only_host_config_preflight", lambda: None)


def _result(*, stdout: bytes, stderr: bytes = b"", code: int = 0,
            status: ProcessStatus = ProcessStatus.EXIT) -> ProcessResult:
    return ProcessResult(status=status, returncode=code, stdout=stdout, stderr=stderr)


def _request(executor_id: str, tmp_path: Path, **changes: object) -> ProviderRequest:
    execution_class = changes.get(
        "execution_class", "repo-write" if executor_id == "agy" else "read-only",
    )
    values: dict[str, object] = {
        "executor_id": executor_id,
        "prompt": "secret prompt only in stdin",
        "cwd": tmp_path,
        "execution_class": execution_class,
        "timeout": 3,
        "artifact_store": ArtifactStore(tmp_path / "artifacts"),
        "artifact_prefix": f"turns/{executor_id}",
        "profile": ProviderRegistry.default().select(
            executor_id, execution_class, "standard",
        ),
    }
    values.update(changes)
    return ProviderRequest(**values)


def _fake_provider(request: ProviderRequest, runner, *, registry=None) -> ProviderResult:
    return providers._run_provider_with_runner(request, registry=registry, runner=runner)


def _fake_many(requests, runner, *, max_workers: int) -> tuple[ProviderResult, ...]:
    return providers._run_many_with_runner(requests, max_workers=max_workers, runner=runner)


@pytest.mark.parametrize("executor_id", ("claude", "codex", "agy"))
@pytest.mark.parametrize("resume", (False, True))
def test_repo_write_requires_native_seat_boundary_before_any_subprocess(
    executor_id: str, resume: bool, tmp_path: Path, monkeypatch,
) -> None:
    launched = []
    monkeypatch.setattr(providers, "_installed_cli_version", lambda _executor: pytest.fail(
        "version probe ran before the repo-write boundary gate",
    ))
    monkeypatch.setattr(providers, "run_command", launched.append)
    request = _request(
        executor_id, tmp_path, execution_class="repo-write", resume=resume,
        session_id=("11111111-1111-4111-8111-111111111111" if executor_id == "claude"
                    else f"exact-{executor_id}-session") if resume else None,
    )

    with pytest.raises(ProviderRequestError, match="native seat boundary"):
        run_provider(request)
    assert launched == []


def test_run_many_rejects_mixed_repo_write_batch_before_any_subprocess(
    tmp_path: Path, monkeypatch,
) -> None:
    launched = []
    monkeypatch.setattr(providers, "_installed_cli_version", lambda _executor: pytest.fail(
        "version probe ran before the repo-write batch boundary gate",
    ))
    monkeypatch.setattr(providers, "run_command", launched.append)
    requests = (_request("claude", tmp_path), _request("agy", tmp_path))

    with pytest.raises(ProviderRequestError, match="native seat boundary"):
        run_many(requests, max_workers=2)
    assert launched == []


def _synthetic_claude_adc(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    config = home / ".claude"
    config.mkdir(mode=0o700)
    settings = config / "settings.json"
    settings.write_text(json.dumps({"env": {
        "CLAUDE_CODE_USE_VERTEX": "1",
        "ANTHROPIC_VERTEX_PROJECT_ID": "disposable-test-project",
        "CLOUD_ML_REGION": "eu",
    }}))
    settings.chmod(0o600)
    adc = home / ".config" / "gcloud" / "application_default_credentials.json"
    adc.parent.mkdir(parents=True)
    adc.write_text("synthetic credential")
    adc.chmod(0o600)
    monkeypatch.setenv("HOME", str(home))
    return adc


class _Adapter(ProviderAdapter):
    """Minimal compatible adapter used only to exercise registry admission."""

    def __init__(self, executor_id: str, capabilities: ProviderCapabilities) -> None:
        self.executor_id = executor_id
        self.capabilities = capabilities

    def build_command(self, request):  # pragma: no cover - registry-only test double.
        raise AssertionError(request)

    def parse(self, stdout):  # pragma: no cover - registry-only test double.
        raise AssertionError(stdout)


class _IncompleteAdapter(ProviderAdapter):
    executor_id = "custom"

    def build_command(self, request):  # pragma: no cover - registry-only test double.
        raise AssertionError(request)

    def parse(self, stdout):  # pragma: no cover - registry-only test double.
        raise AssertionError(stdout)


def _profile(executor_id, adapter, capabilities):
    return ExecutorProfile(
        executor_id, adapter, capabilities, "read-only", "standard", "test-model",
        "high", "test-cli-1", ("custom", "--model", "test-model"),
        ("custom", "--resume", "{session_id}", "--model", "test-model"),
        900, 900,
    )


def test_registry_has_only_three_default_executors_and_maka_is_routing_only():
    """Registering Maka as an executor would let an evaluation host spend a seat."""
    registry = ProviderRegistry.default()

    assert registry.executor_ids == ("claude", "codex", "agy")
    assert RoutingTargetRegistry.default().target_ids == ("maka",)
    with pytest.raises(UnsupportedExecutorError, match="maka"):
        registry.require("maka")


@pytest.mark.parametrize("executor_id", ["", 1, None])
def test_registry_rejects_non_string_or_empty_profile_ids(executor_id):
    """A non-string registry key defeats the open-ended string executor contract."""
    adapter = _Adapter("custom", ProviderCapabilities(False, True, True))

    with pytest.raises(ProviderRequestError):
        ProviderRegistry((_profile(executor_id, adapter, adapter.capabilities),))


def test_registry_rejects_incompatible_profiles_and_maka_adapter_bypasses():
    """Profile aliases must not smuggle Maka or a different capability surface into seats."""
    capabilities = ProviderCapabilities(False, True, True)
    adapter = _Adapter("custom", capabilities)
    with pytest.raises(ProviderRequestError, match="ProviderAdapter"):
        ProviderRegistry((_profile("custom", object(), capabilities),))
    with pytest.raises(ProviderRequestError, match="capabilities"):
        ProviderRegistry((_profile("custom", _IncompleteAdapter(), capabilities),))
    with pytest.raises(ProviderRequestError, match="match adapter"):
        ProviderRegistry((_profile("other", adapter, capabilities),))
    with pytest.raises(ProviderRequestError, match="capabilities"):
        ProviderRegistry((_profile("custom", adapter, ProviderCapabilities(True, True, True)),))
    maka = _Adapter("maka", capabilities)
    with pytest.raises(UnsupportedExecutorError, match="maka"):
        ProviderRegistry((_profile("maka", maka, capabilities),))
    with pytest.raises(ProviderRequestError, match="match adapter"):
        ProviderRegistry((_profile("not-maka", maka, capabilities),))


@pytest.mark.parametrize(
    ("executor_id", "execution_class", "session_id", "resume", "expected"),
    [
        ("claude", "read-only", "11111111-1111-4111-8111-111111111111", False, ("claude", "--print", "--session-id", "11111111-1111-4111-8111-111111111111", "--output-format", "json", "--restricted", "--strict-mcp-config", "--tools", "Read,Glob,Grep", "--permission-mode", "plan", "--permission-prompts", "none", "--disallowedTools", "ExitPlanMode")),
        ("claude", "repo-write", "11111111-1111-4111-8111-111111111111", False, ("claude", "--print", "--session-id", "11111111-1111-4111-8111-111111111111", "--output-format", "json", "--dangerously-skip-permissions")),
        ("claude", "read-only", "11111111-1111-4111-8111-111111111111", True, ("claude", "--print", "--resume", "11111111-1111-4111-8111-111111111111", "--output-format", "json", "--restricted", "--strict-mcp-config", "--tools", "Read,Glob,Grep", "--permission-mode", "plan", "--permission-prompts", "none", "--disallowedTools", "ExitPlanMode")),
        ("claude", "repo-write", "11111111-1111-4111-8111-111111111111", True, ("claude", "--print", "--resume", "11111111-1111-4111-8111-111111111111", "--output-format", "json", "--dangerously-skip-permissions")),
        ("codex", "read-only", None, False, ("codex", "exec", "-", "--json", "--sandbox", "read-only")),
        ("codex", "repo-write", None, False, ("codex", "exec", "-", "--json", "--sandbox", "workspace-write")),
        ("codex", "read-only", "codex-thread-1", True, ("codex", "exec", "resume", "-c", 'sandbox_mode="read-only"', "--json", "codex-thread-1", "-")),
        ("codex", "repo-write", "codex-thread-1", True, ("codex", "exec", "resume", "-c", 'sandbox_mode="workspace-write"', "--json", "codex-thread-1", "-")),
        ("agy", "read-only", None, False, ("agy", "--mode", "plan", "--input-format", "stream-json", "--output-format", "stream-json")),
        ("agy", "repo-write", None, False, ("agy", "--mode", "accept-edits", "--input-format", "stream-json", "--output-format", "stream-json")),
        ("agy", "read-only", "agy-conversation-1", True, ("agy", "--conversation", "agy-conversation-1", "--mode", "plan", "--input-format", "stream-json", "--output-format", "stream-json")),
        ("agy", "repo-write", "agy-conversation-1", True, ("agy", "--conversation", "agy-conversation-1", "--mode", "accept-edits", "--input-format", "stream-json", "--output-format", "stream-json")),
    ],
)
def test_adapter_commands_pin_explicit_sessions_posture_and_stdin_only(
    executor_id, execution_class, session_id, resume, expected, tmp_path,
    _mock_clean_codex_host,
):
    """Moving prompts to argv or implicit continuation would leak data or resume the wrong turn."""
    request = _request(
        executor_id, tmp_path, execution_class=execution_class, session_id=session_id, resume=resume,
        profile=None,
    )

    if executor_id == "agy" and execution_class == "read-only":
        with pytest.raises(ProviderRequestError, match="guard"):
            ProviderRegistry.default().require(executor_id).build_command(request)
        return
    command = ProviderRegistry.default().require(executor_id).build_command(request)

    if executor_id == "codex" and execution_class == "read-only":
        isolation = (
            "--ignore-user-config", "--disable", "apps", "--disable", "plugins",
            "--disable", "remote_plugin", "--disable", "hooks",
            "-c", 'cli_auth_credentials_store="keyring"',
            "-c", f'projects={{{json.dumps(str(tmp_path))}={{trust_level="untrusted"}}}}',
        )
        offset = 3 if resume else 2
        expected = expected[:offset] + isolation + expected[offset:]
    assert command.argv == expected
    if executor_id == "agy":
        assert json.loads(command.stdin) == {
            "event": "user", "message": {"content": "secret prompt only in stdin"},
        }
        assert command.stdin.endswith(b"\n")
    else:
        assert command.stdin == b"secret prompt only in stdin"
    assert "secret prompt only in stdin" not in repr(command)
    assert not {"--continue", "--last"}.intersection(command.argv)


@pytest.mark.parametrize("tier,effort", [("standard", "xhigh"), ("deep", "ultra")])
@pytest.mark.parametrize("resume", [False, True])
def test_codex_read_only_profile_isolates_ambient_tools_on_both_turn_shapes(
    tmp_path, tier, effort, resume, _mock_clean_codex_host,
):
    workspace = tmp_path / 'seat.with.dot"quote'
    workspace.mkdir()
    registry = ProviderRegistry.default()
    profile = registry.select("codex", "read-only", tier)
    request = ProviderRequest(
        "codex", "read fixture", cwd=workspace, profile=profile,
        session_id="exact-codex-thread" if resume else None, resume=resume,
    )

    argv = registry.require("codex").build_command(request).argv
    prefix = ("codex", "exec", "resume") if resume else ("codex", "exec")
    assert argv[:len(prefix)] == prefix
    assert argv.count("--ignore-user-config") == 1
    assert [argv[index + 1] for index, arg in enumerate(argv[:-1])
            if arg == "--disable"] == ["apps", "plugins", "remote_plugin", "hooks"]
    assert 'cli_auth_credentials_store="keyring"' in argv
    trust = next(arg.removeprefix("projects=") for arg in argv
                 if arg.startswith("projects="))
    assert tomllib.loads(f"override = {trust}") == {
        "override": {str(workspace): {"trust_level": "untrusted"}},
    }
    assert argv[argv.index("-m") + 1] == "gpt-6-sol"
    assert f'model_reasoning_effort="{effort}"' in argv
    if resume:
        assert argv[-3:] == ("--json", "exact-codex-thread", "-")
        assert 'sandbox_mode="read-only"' in argv
    else:
        assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv


@pytest.mark.parametrize("resume", [False, True])
def test_codex_read_only_trust_override_accepts_unicode_workspace(
    tmp_path, resume, _mock_clean_codex_host,
):
    workspace = tmp_path / "seat-🧭"
    workspace.mkdir()
    registry = ProviderRegistry.default()
    request = ProviderRequest(
        "codex", "read fixture", cwd=workspace,
        profile=registry.select("codex", "read-only", "standard"),
        session_id="exact-codex-thread" if resume else None, resume=resume,
    )

    argv = registry.require("codex").build_command(request).argv
    trust = next(arg.removeprefix("projects=") for arg in argv
                 if arg.startswith("projects="))
    assert tomllib.loads(f"override = {trust}") == {
        "override": {str(workspace): {"trust_level": "untrusted"}},
    }


def _codex_system_paths(tmp_path, monkeypatch):
    system = tmp_path / "codex-system"
    system.mkdir()
    paths = tuple(system / name for name in (
        "config.toml", "managed_config.toml", "requirements.toml",
    ))
    monkeypatch.setattr(providers, "_CODEX_SYSTEM_CONFIG_PATHS", paths, raising=False)
    monkeypatch.setattr(providers, "_CODEX_HOST_PLATFORM", "linux", raising=False)
    return paths


@pytest.fixture
def _mock_admin_owned_codex_system_location(monkeypatch):
    # A tmp_path is owned by the test user; these tests need a protected
    # synthetic location so they reach the later config and preference checks.
    monkeypatch.setattr(providers.os, "geteuid", lambda: -1)
    monkeypatch.setattr(providers.os, "access", lambda *_args, **_kwargs: False)


@pytest.mark.parametrize("mode", [0o700, 0o500])
@pytest.mark.parametrize("resume", [False, True])
def test_codex_read_only_rejects_owner_mutable_system_config_directory(
    tmp_path, monkeypatch, resume, mode,
):
    _codex_system_paths(tmp_path, monkeypatch)[0].parent.chmod(mode)
    registry = ProviderRegistry.default()

    with pytest.raises(ProviderRequestError, match="writable|mutable"):
        registry.require("codex").build_command(ProviderRequest(
            "codex", "read", cwd=tmp_path,
            profile=registry.select("codex", "read-only", "standard"),
            session_id="exact-thread" if resume else None, resume=resume,
        ))


@pytest.mark.parametrize("through_symlink", [False, True])
@pytest.mark.parametrize("resume", [False, True])
def test_codex_read_only_rejects_mutable_system_config_parent_when_directory_absent(
    tmp_path, monkeypatch, through_symlink, resume,
):
    parent = tmp_path
    if through_symlink:
        target = tmp_path / "private" / "etc"
        target.mkdir(parents=True)
        parent = tmp_path / "etc"
        parent.symlink_to(target, target_is_directory=True)
    system = parent / "codex"
    monkeypatch.setattr(providers, "_CODEX_SYSTEM_CONFIG_PATHS", tuple(
        system / name for name in (
            "config.toml", "managed_config.toml", "requirements.toml",
        )
    ))
    monkeypatch.setattr(providers, "_CODEX_HOST_PLATFORM", "linux")
    registry = ProviderRegistry.default()

    with pytest.raises(ProviderRequestError, match="writable|mutable"):
        registry.require("codex").build_command(ProviderRequest(
            "codex", "read", cwd=tmp_path,
            profile=registry.select("codex", "read-only", "standard"),
            session_id="exact-thread" if resume else None, resume=resume,
        ))


@pytest.mark.parametrize("name", [
    "config.toml", "managed_config.toml", "requirements.toml",
])
@pytest.mark.parametrize("resume", [False, True])
def test_codex_read_only_rejects_present_system_configuration(
    tmp_path, monkeypatch, name, resume,
):
    paths = _codex_system_paths(tmp_path, monkeypatch)
    next(path for path in paths if path.name == name).write_text("[mcp_servers.extra]\n")
    registry = ProviderRegistry.default()

    with pytest.raises(ProviderRequestError, match="system|managed|host config"):
        registry.require("codex").build_command(ProviderRequest(
            "codex", "read", cwd=tmp_path,
            profile=registry.select("codex", "read-only", "standard"),
            session_id="exact-thread" if resume else None, resume=resume,
        ))


@pytest.mark.parametrize("kind", ["directory", "symlink"])
def test_codex_read_only_rejects_nonregular_system_configuration(
    tmp_path, monkeypatch, kind,
):
    path = _codex_system_paths(tmp_path, monkeypatch)[0]
    if kind == "directory":
        path.mkdir()
    else:
        path.symlink_to(tmp_path / "missing-target")
    registry = ProviderRegistry.default()

    with pytest.raises(ProviderRequestError, match="system|managed|host config"):
        registry.require("codex").build_command(ProviderRequest(
            "codex", "read", cwd=tmp_path,
            profile=registry.select("codex", "read-only", "standard"),
        ))


def test_codex_read_only_rejects_symlinked_system_config_directory(tmp_path, monkeypatch):
    paths = _codex_system_paths(tmp_path, monkeypatch)
    system = paths[0].parent
    system.rmdir()
    system.symlink_to(tmp_path / "untrusted-config", target_is_directory=True)
    registry = ProviderRegistry.default()

    with pytest.raises(ProviderRequestError, match="system|managed|host config"):
        registry.require("codex").build_command(ProviderRequest(
            "codex", "read", cwd=tmp_path,
            profile=registry.select("codex", "read-only", "standard"),
        ))


def test_codex_read_only_rejects_unreadable_system_configuration(tmp_path, monkeypatch):
    path = _codex_system_paths(tmp_path, monkeypatch)[0]
    original_lstat = Path.lstat

    def denied_lstat(candidate):
        if candidate == path:
            raise PermissionError("synthetic inaccessible system configuration")
        return original_lstat(candidate)

    monkeypatch.setattr(Path, "lstat", denied_lstat)
    registry = ProviderRegistry.default()
    with pytest.raises(ProviderRequestError, match="system|managed|host config"):
        registry.require("codex").build_command(ProviderRequest(
            "codex", "read", cwd=tmp_path,
            profile=registry.select("codex", "read-only", "standard"),
        ))


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("outcome", ["forced", "probe-error"])
def test_codex_read_only_rejects_forced_macos_preferences_or_probe_errors(
    tmp_path, monkeypatch, resume, outcome, _mock_admin_owned_codex_system_location,
):
    _codex_system_paths(tmp_path, monkeypatch)
    monkeypatch.setattr(providers, "_CODEX_HOST_PLATFORM", "darwin", raising=False)

    def probe():
        if outcome == "probe-error":
            raise OSError("synthetic CoreFoundation failure")
        return True

    monkeypatch.setattr(providers, "_codex_macos_forced_preferences_present", probe,
                        raising=False)
    registry = ProviderRegistry.default()
    with pytest.raises(ProviderRequestError, match="managed preferences|host config"):
        registry.require("codex").build_command(ProviderRequest(
            "codex", "read", cwd=tmp_path,
            profile=registry.select("codex", "read-only", "standard"),
            session_id="exact-thread" if resume else None, resume=resume,
        ))


def test_codex_read_only_host_preflight_rechecks_after_config_insertion(
    tmp_path, monkeypatch, _mock_admin_owned_codex_system_location,
):
    path = _codex_system_paths(tmp_path, monkeypatch)[1]
    registry = ProviderRegistry.default()
    adapter = registry.require("codex")
    profile = registry.select("codex", "read-only", "standard")
    initial = ProviderRequest("codex", "read", cwd=tmp_path, profile=profile)
    resumed = ProviderRequest(
        "codex", "read again", cwd=tmp_path, profile=profile,
        session_id="exact-thread", resume=True,
    )

    assert adapter.build_command(initial).argv[0] == "codex"
    path.write_text("[mcp_servers.extra]\n")
    with pytest.raises(ProviderRequestError, match="system|managed|host config"):
        adapter.build_command(resumed)


def test_codex_repo_write_does_not_apply_read_only_host_preflight(tmp_path, monkeypatch):
    path = _codex_system_paths(tmp_path, monkeypatch)[0]
    path.write_text("[mcp_servers.extra]\n")
    monkeypatch.setattr(providers, "_CODEX_HOST_PLATFORM", "darwin", raising=False)
    monkeypatch.setattr(
        providers, "_codex_macos_forced_preferences_present",
        lambda: (_ for _ in ()).throw(OSError("should not be probed")), raising=False,
    )
    registry = ProviderRegistry.default()
    profile = registry.select("codex", "repo-write", "standard")
    adapter = registry.require("codex")

    for resume in (False, True):
        command = adapter.build_command(ProviderRequest(
            "codex", "write", cwd=tmp_path, execution_class="repo-write", profile=profile,
            session_id="exact-thread" if resume else None, resume=resume,
        ))
        assert command.argv[0] == "codex"


def test_codex_macos_probe_queries_only_forced_status_without_values(monkeypatch):
    class Function:
        def __init__(self, operation):
            self.operation = operation

        def __call__(self, *args):
            return self.operation(*args)

    class FakeCoreFoundation:
        def __init__(self):
            self.names = {}
            self.queries = []
            self.released = []
            self.CFStringCreateWithCString = Function(self.create_string)
            self.CFPreferencesAppSynchronize = Function(lambda _domain: 1)
            self.CFPreferencesAppValueIsForced = Function(self.is_forced)
            self.CFRelease = Function(self.release)

        def create_string(self, _allocator, raw, _encoding):
            pointer = len(self.names) + 1
            self.names[pointer] = raw.decode("utf-8")
            return pointer

        def is_forced(self, key, domain):
            self.queries.append((self.names[key], self.names[domain]))
            return self.names[key] == "requirements_toml_base64"

        def release(self, pointer):
            self.released.append(pointer)

    fake = FakeCoreFoundation()
    monkeypatch.setattr(providers, "ctypes", ctypes, raising=False)
    monkeypatch.setattr(ctypes, "CDLL", lambda _path: fake)

    assert providers._codex_macos_forced_preferences_present() is True
    assert fake.queries == [
        ("config_toml_base64", "com.openai.codex"),
        ("requirements_toml_base64", "com.openai.codex"),
    ]
    assert len(fake.released) == 3


def test_codex_read_only_rechecks_exact_workspace_and_rejects_aliases(
    tmp_path, monkeypatch, _mock_clean_codex_host,
):
    workspace = tmp_path / "seat"
    workspace.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(workspace, target_is_directory=True)
    registry = ProviderRegistry.default()
    profile = registry.select("codex", "read-only", "standard")
    adapter = registry.require("codex")

    for cwd in (alias, workspace / ".." / "seat"):
        with pytest.raises(ProviderRequestError, match="canonical|absolute|workspace"):
            adapter.build_command(ProviderRequest("codex", "read", cwd=cwd, profile=profile))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ProviderRequestError, match="canonical|absolute|workspace"):
        adapter.build_command(ProviderRequest("codex", "read", cwd="seat", profile=profile))


def test_codex_repo_write_argv_is_unchanged_by_read_only_isolation(tmp_path):
    registry = ProviderRegistry.default()
    profile = registry.select("codex", "repo-write", "standard")
    adapter = registry.require("codex")

    initial = adapter.build_command(ProviderRequest(
        "codex", "write", cwd=tmp_path, execution_class="repo-write", profile=profile,
    )).argv
    resumed = adapter.build_command(ProviderRequest(
        "codex", "continue", cwd=tmp_path, execution_class="repo-write",
        profile=profile, session_id="exact-codex-thread", resume=True,
    )).argv
    assert initial == (
        "codex", "exec", "-", "--json", "--sandbox", "workspace-write",
        "-m", "gpt-6-sol", "-c", 'model_reasoning_effort="xhigh"',
    )
    assert resumed == (
        "codex", "exec", "resume", "-c", 'sandbox_mode="workspace-write"',
        "-m", "gpt-6-sol", "-c", 'model_reasoning_effort="xhigh"',
        "--json", "exact-codex-thread", "-",
    )


@pytest.mark.parametrize("execution_class,mode", [
    ("read-only", "plan"),
    ("repo-write", "accept-edits"),
])
@pytest.mark.parametrize("resume", [False, True])
def test_agy_profile_uses_stream_json_stdin_on_initial_and_exact_resume(
    tmp_path, execution_class, mode, resume,
):
    """A bare --print consumes a flag as its prompt; user text belongs in one JSON stdin event."""
    registry = ProviderRegistry.default()
    profile = registry.select("agy", execution_class, "standard")
    prompt = 'Say "héllo"\n世界'
    session_id = "agy-conversation-1" if resume else None
    request = ProviderRequest(
        "agy", prompt, cwd=tmp_path, execution_class=execution_class,
        session_id=session_id, resume=resume, profile=profile,
    )

    if execution_class == "read-only":
        with pytest.raises(ProviderRequestError, match="guard"):
            registry.require("agy").build_command(request)
        return
    command = registry.require("agy").build_command(request)

    expected = ("agy",) + (("--conversation", session_id) if resume else ()) + (
        "--dangerously-skip-permissions", "--mode", mode,
        "--input-format", "stream-json", "--output-format", "stream-json",
        "--model", "gemini-3.8-flash-high", "--effort", "high",
    )
    assert command.argv == expected
    assert "--print" not in command.argv
    assert prompt not in repr(command)
    assert command.stdin.endswith(b"\n")
    assert command.stdin.count(b"\n") == 1
    assert b"\\n" in command.stdin
    assert "héllo".encode("utf-8") in command.stdin
    assert json.loads(command.stdin) == {
        "event": "user", "message": {"content": prompt},
    }


def test_only_agy_commands_enable_the_literal_adc_mode(
    tmp_path, monkeypatch, _mock_clean_codex_host,
):
    """The parent auth-mode switch must reach agy without reaching other executors."""
    monkeypatch.setenv("AGY_ADC_AUTH", "true")
    registry = ProviderRegistry.default()

    for resume in (False, True):
        command = registry.require("agy").build_command(ProviderRequest(
            "agy", "question", cwd=tmp_path, execution_class="repo-write",
            session_id="agy-conversation-1" if resume else None, resume=resume,
            profile=registry.select("agy", "repo-write", "standard"),
        ))
        assert command.environment == {"AGY_ADC_AUTH": "true"}
        assert "AGY_ADC_AUTH" not in command.argv
    for executor, session_id in (
        ("claude", "11111111-1111-4111-8111-111111111111"),
        ("codex", None),
    ):
        command = registry.require(executor).build_command(ProviderRequest(
            executor, "question", cwd=tmp_path, session_id=session_id,
            execution_class="repo-write" if executor == "agy" else "read-only",
            profile=registry.select(
                executor, "repo-write" if executor == "agy" else "read-only", "standard",
            ),
        ))
        assert "AGY_ADC_AUTH" not in command.environment


def test_adapter_auth_mode_reaches_only_agy_child_without_secrets(
    tmp_path, monkeypatch, _mock_clean_codex_host,
):
    """Exercise adapter and process isolation with a local child, not a model CLI."""
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    monkeypatch.setenv("AGY_ADC_AUTH", "true")
    registry = ProviderRegistry.default()
    forbidden = (
        "CLAUDE_MEM_TOKEN", "GEMINI_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CLOUD_QUOTA_PROJECT", "HOME",
    )
    base = {name: "sentinel" for name in forbidden}
    base.update({"AGY_ADC_AUTH": "true", "PATH": "/usr/bin:/bin"})
    probe = (
        "import json, os; "
        f"names = {forbidden!r}; "
        "print(json.dumps({'adc': os.getenv('AGY_ADC_AUTH'), "
        "'forbidden': [name for name in names if name in os.environ]}))"
    )

    for executor, session_id, expected_adc in (
        ("agy", None, "true"),
        ("codex", None, None),
    ):
        command = registry.require(executor).build_command(ProviderRequest(
            executor, "question", cwd=tmp_path, session_id=session_id,
            execution_class="repo-write" if executor == "agy" else "read-only",
            profile=registry.select(
                executor, "repo-write" if executor == "agy" else "read-only", "standard",
            ),
        ))
        child = replace(command, argv=(sys.executable, "-c", probe), stdin=b"",
                        slot_root=tmp_path / "slots")

        result = fanout.run_command(child, base_environment=base)

        assert result.status is ProcessStatus.EXIT
        assert result.returncode == 0
        assert json.loads(result.stdout) == {"adc": expected_adc, "forbidden": []}

    with pytest.raises(ProviderRequestError, match="Vertex route"):
        registry.require("claude").build_command(ProviderRequest(
            "claude", "question", cwd=tmp_path,
            session_id="11111111-1111-4111-8111-111111111111",
            profile=registry.select("claude", "read-only", "standard"),
        ))


@pytest.mark.parametrize("value", [None, "false", "TRUE", "key-sentinel"])
def test_agy_uses_default_auth_when_adc_mode_is_not_literal_true(tmp_path, monkeypatch, value):
    """Unknown parent values cannot become child auth settings."""
    if value is None:
        monkeypatch.delenv("AGY_ADC_AUTH", raising=False)
    else:
        monkeypatch.setenv("AGY_ADC_AUTH", value)
    registry = ProviderRegistry.default()

    command = registry.require("agy").build_command(ProviderRequest(
        "agy", "question", cwd=tmp_path, execution_class="repo-write",
        profile=registry.select("agy", "repo-write", "standard"),
    ))

    assert command.environment == {}


@pytest.mark.parametrize("resume", [False, True])
def test_claude_command_passes_only_private_default_vertex_route(tmp_path, monkeypatch, resume):
    """A literal opt-in passes only the local ADC path and pinned EU route."""
    adc = _synthetic_claude_adc(tmp_path, monkeypatch)
    monkeypatch.setenv("AGY_ADC_AUTH", "true")
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_QUOTA_PROJECT", "ambient-quota")
    monkeypatch.setenv("CLAUDE_MEM_TOKEN", "ambient-memory-token")
    registry = ProviderRegistry.default()
    request = ProviderRequest(
        "claude", "question", cwd=tmp_path,
        session_id="11111111-1111-4111-8111-111111111111", resume=resume,
        profile=registry.select("claude", "read-only", "standard"),
    )

    command = registry.require("claude").build_command(request)

    assert command.environment == {
        "GOOGLE_APPLICATION_CREDENTIALS": str(adc),
        "CLAUDE_CODE_USE_VERTEX": "1",
        "ANTHROPIC_VERTEX_PROJECT_ID": "disposable-test-project",
        "CLOUD_ML_REGION": "eu",
    }
    assert "synthetic credential" not in repr(command)


@pytest.mark.parametrize("state", ["missing", "public", "symlink", "directory"])
def test_claude_command_rejects_ineligible_adc(tmp_path, monkeypatch, state):
    """An opted-in restricted seat fails before spend if ADC is ineligible."""
    adc = _synthetic_claude_adc(tmp_path, monkeypatch)
    monkeypatch.setenv("AGY_ADC_AUTH", "true")
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    if state == "missing":
        adc.unlink()
    elif state == "public":
        adc.chmod(0o644)
    elif state == "symlink":
        target = tmp_path / "target.json"
        target.write_text("synthetic credential")
        target.chmod(0o600)
        adc.unlink()
        adc.symlink_to(target)
    else:
        adc.unlink()
        adc.mkdir()
    registry = ProviderRegistry.default()
    request = ProviderRequest(
        "claude", "question", cwd=tmp_path,
        profile=registry.select("claude", "read-only", "standard"),
    )

    with pytest.raises(ProviderRequestError, match="Vertex route"):
        registry.require("claude").build_command(request)


@pytest.mark.parametrize("auth_mode", [None, "false", "TRUE"])
@pytest.mark.parametrize("resume", [False, True])
def test_claude_default_adc_requires_literal_opt_in(tmp_path, monkeypatch, auth_mode, resume):
    """A private default file alone does not authorize forwarding its path."""
    _synthetic_claude_adc(tmp_path, monkeypatch)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    if auth_mode is None:
        monkeypatch.delenv("AGY_ADC_AUTH", raising=False)
    else:
        monkeypatch.setenv("AGY_ADC_AUTH", auth_mode)
    registry = ProviderRegistry.default()
    request = ProviderRequest(
        "claude", "question", cwd=tmp_path,
        session_id="11111111-1111-4111-8111-111111111111", resume=resume,
        profile=registry.select("claude", "read-only", "standard"),
    )

    assert registry.require("claude").build_command(request).environment == {}


@pytest.mark.parametrize("explicit", ["", "/explicit/credentials.json"])
@pytest.mark.parametrize("resume", [False, True])
def test_claude_restricted_route_rejects_explicit_parent_choice(
    tmp_path, monkeypatch, explicit, resume,
):
    """An explicit parent path cannot be silently replaced or inherited."""
    _synthetic_claude_adc(tmp_path, monkeypatch)
    monkeypatch.setenv("AGY_ADC_AUTH", "true")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", explicit)
    registry = ProviderRegistry.default()
    request = ProviderRequest(
        "claude", "question", cwd=tmp_path,
        session_id="11111111-1111-4111-8111-111111111111", resume=resume,
        profile=registry.select("claude", "read-only", "standard"),
    )

    with pytest.raises(ProviderRequestError, match="parent override"):
        registry.require("claude").build_command(request)


def test_private_claude_adc_path_reaches_only_claude_child(
    tmp_path, monkeypatch, _mock_clean_codex_host,
):
    """Probe the actual child environment without invoking a provider CLI."""
    adc = _synthetic_claude_adc(tmp_path, monkeypatch)
    monkeypatch.setenv("AGY_ADC_AUTH", "true")
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    registry = ProviderRegistry.default()
    names = (
        "GOOGLE_APPLICATION_CREDENTIALS", "HOME", "GOOGLE_CLOUD_QUOTA_PROJECT",
        "CLAUDE_MEM_TOKEN", "AGY_ADC_AUTH", "CLAUDE_CODE_USE_VERTEX",
        "ANTHROPIC_VERTEX_PROJECT_ID", "CLOUD_ML_REGION",
    )
    base = {name: "ambient-sentinel" for name in names}
    base["PATH"] = os.environ.get("PATH", "/usr/bin:/bin")
    probe = (
        "import json, os; "
        f"names = {names!r}; "
        "print(json.dumps({name: os.getenv(name) for name in names if name in os.environ}))"
    )
    for executor, session_id, resume, expected in (
        ("claude", None, False, {"GOOGLE_APPLICATION_CREDENTIALS": str(adc),
                                 "CLAUDE_CODE_USE_VERTEX": "1",
                                 "ANTHROPIC_VERTEX_PROJECT_ID": "disposable-test-project",
                                 "CLOUD_ML_REGION": "eu"}),
        ("claude", "11111111-1111-4111-8111-111111111111", True,
         {"GOOGLE_APPLICATION_CREDENTIALS": str(adc),
          "CLAUDE_CODE_USE_VERTEX": "1",
          "ANTHROPIC_VERTEX_PROJECT_ID": "disposable-test-project",
          "CLOUD_ML_REGION": "eu"}),
        ("agy", None, False, {"AGY_ADC_AUTH": "true"}),
        ("codex", None, False, {}),
    ):
        request = ProviderRequest(
            executor, "question", cwd=tmp_path, session_id=session_id, resume=resume,
            execution_class="repo-write" if executor == "agy" else "read-only",
            profile=registry.select(
                executor, "repo-write" if executor == "agy" else "read-only", "standard",
            ),
        )
        command = registry.require(executor).build_command(request)
        child = replace(command, argv=(sys.executable, "-c", probe), stdin=b"",
                        slot_root=tmp_path / "slots")

        result = fanout.run_command(child, base_environment=base)

        assert result.status is ProcessStatus.EXIT
        assert result.returncode == 0
        assert json.loads(result.stdout) == expected


def test_agy_stream_result_preserves_exact_identity_usage_and_unobserved_model(tmp_path):
    """The init model repeats the requested override; it is not independent model evidence."""
    stdout = (
        b'{"event":"init","conversation_id":"agy-conversation-1","init":{"cwd":"/tmp",'
        b'"tools":[],"permission_mode":"always-proceed","model":"gemini-3.8-flash-high"}}\n'
        b'{"event":"step_update","step_update":{"conversation_id":"agy-conversation-1",'
        b'"step_index":0,"state":"DONE","step_type":"agent_response","text_delta":"READY"}}\n'
        b'{"event":"result","result":{"conversation_id":"agy-conversation-1",'
        b'"status":"SUCCESS","response":"READY","duration_seconds":1.0,"num_turns":1,'
        b'"usage":{"input_tokens":17,"output_tokens":9,"thinking_tokens":2,'
        b'"cache_read_tokens":4,"total_tokens":32}}}\n'
    )
    registry = ProviderRegistry.default()

    for resume in (False, True):
        result = _fake_provider(ProviderRequest(
            "agy", "reply READY", cwd=tmp_path, execution_class="repo-write",
            session_id="agy-conversation-1" if resume else None, resume=resume,
            profile=registry.select("agy", "repo-write", "standard"),
        ), lambda _command: _result(stdout=stdout), registry=registry)

        assert result.valid
        assert result.session_id == "agy-conversation-1"
        assert result.answer == "READY"
        assert result.usage == {
            "input": 17, "output": 9, "thinking": 2, "cache_read": 4, "total": 32,
        }
        assert result.requested_model == "gemini-3.8-flash-high"
        assert result.observed_model is None
        assert result.stdout == stdout


def test_codex_stream_uses_final_agent_message_not_intermediate_commentary():
    stdout = (
        b'{"type":"thread.started","thread_id":"codex-thread-1"}\n'
        b'{"type":"item.completed","item":{"type":"agent_message",'
        b'"text":"I will read the source and return JSON."}}\n'
        b'{"type":"item.completed","item":{"type":"agent_message",'
        b'"text":"{\\"fact\\":\\"verified\\"}"}}\n'
        b'{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":2}}\n'
    )

    parsed = ProviderRegistry.default().require("codex").parse(stdout)

    assert parsed.answer == '{"fact":"verified"}'


def test_codex_empty_final_message_does_not_promote_intermediate_commentary():
    stdout = (
        b'{"type":"thread.started","thread_id":"codex-thread-1"}\n'
        b'{"type":"item.completed","item":{"type":"agent_message",'
        b'"text":"I will answer shortly."}}\n'
        b'{"type":"item.completed","item":{"type":"agent_message","text":""}}\n'
        b'{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":2}}\n'
    )

    parsed = ProviderRegistry.default().require("codex").parse(stdout)

    assert parsed.answer == ""


@pytest.mark.parametrize("stdout", [
    b'{"conversation_id":"agy-conversation-1","status":"SUCCESS","response":"legacy"}',
    b'{"event":"init","conversation_id":"agy-conversation-1","init":{}}\n',
    (b'{"event":"init","conversation_id":"agy-conversation-1","init":{}}\n'
     b'{"event":"step_update","step_update":{"conversation_id":"agy-conversation-2",'
     b'"step_index":0,"state":"DONE","step_type":"agent_response"}}\n'
     b'{"event":"result","result":{"conversation_id":"agy-conversation-1",'
     b'"status":"SUCCESS","response":"wrong step"}}\n'),
    (b'{"event":"init","conversation_id":"agy-conversation-1","init":{}}\n'
     b'{"event":"result","result":{"conversation_id":"agy-conversation-2",'
     b'"status":"SUCCESS","response":"wrong session"}}\n'),
    (b'{"event":"init","conversation_id":"agy-conversation-1","init":{}}\n'
     b'{"event":"result","result":{"conversation_id":"agy-conversation-1",'
     b'"status":"RUNNING","response":"unfinished"}}\n'),
    (b'{"event":"init","conversation_id":"agy-conversation-1","init":{}}\n'
     b'{"event":"result","result":{"conversation_id":"agy-conversation-1",'
     b'"status":"SUCCESS","response":"first"}}\n'
     b'{"event":"result","result":{"conversation_id":"agy-conversation-1",'
     b'"status":"SUCCESS","response":"second"}}\n'),
    (b'{"event":"init","conversation_id":"agy-conversation-1","init":{}}\n'
     b'not json\n'
     b'{"event":"result","result":{"conversation_id":"agy-conversation-1",'
     b'"status":"SUCCESS","response":"ignored"}}\n'),
    (b'{"event":"init","conversation_id":"agy-conversation-1","init":{}}\n\n'
     b'{"event":"result","result":{"conversation_id":"agy-conversation-1",'
     b'"status":"SUCCESS","response":"ignored"}}\n'),
])
def test_agy_stream_rejects_incomplete_mismatched_or_malformed_terminal_events(stdout):
    """A partial or contradictory stream cannot establish a resumable result."""
    with pytest.raises(ProviderProtocolError):
        ProviderRegistry.default().require("agy").parse(stdout)


def test_agy_result_only_error_keeps_provider_diagnostic(tmp_path):
    """The CLI can fail before init (for example, a rejected model override)."""
    stdout = (b'{"event":"result","result":{"conversation_id":"",'
              b'"status":"ERROR","response":"","error":"invalid model selection"}}\n')
    result = _fake_provider(_request("agy", tmp_path),
                            lambda _command: _result(stdout=stdout, code=1))

    assert not result.valid
    assert result.reason == "provider-error"
    assert result.hint == "invalid model selection"


def test_agy_success_with_error_field_fails_closed(tmp_path):
    """A contradictory terminal result is not evidence of a successful turn."""
    stdout = (
        b'{"event":"init","conversation_id":"agy-conversation-1","init":{}}\n'
        b'{"event":"result","result":{"conversation_id":"agy-conversation-1",'
        b'"status":"SUCCESS","response":"READY","error":"provider failed"}}\n'
    )
    result = _fake_provider(_request("agy", tmp_path), lambda _command: _result(stdout=stdout))

    assert not result.valid
    assert result.reason == "protocol-error"
    assert result.stdout == stdout


def test_agy_stream_keeps_unicode_separators_inside_a_crlf_delimited_result():
    """U+2028 and U+2029 inside JSON strings are content, not NDJSON boundaries."""
    stdout = (
        '{"event":"init","conversation_id":"agy-conversation-1","init":{}}\r\n'
        '{"event":"result","result":{"conversation_id":"agy-conversation-1",'
        '"status":"SUCCESS","response":"north\u2028south\u2029end"}}\r\n'
    ).encode("utf-8")

    parsed = ProviderRegistry.default().require("agy").parse(stdout)

    assert parsed.final
    assert parsed.session_id == "agy-conversation-1"
    assert parsed.answer == "north\u2028south\u2029end"


def test_adapters_reject_an_unusable_explicit_session_identity(tmp_path):
    """Treating arbitrary text as a fresh Claude UUID or ignored Codex ID breaks exact resume."""
    with pytest.raises(ProviderRequestError, match="UUID"):
        ProviderRegistry.default().require("claude").build_command(
            _request("claude", tmp_path, session_id="not-a-uuid", profile=None),
        )
    with pytest.raises(ProviderRequestError, match="only valid when resuming"):
        ProviderRegistry.default().require("codex").build_command(
            _request("codex", tmp_path, session_id="thread-that-would-be-ignored", profile=None),
        )


@pytest.mark.parametrize(
    ("executor_id", "fixture", "session_id", "answer", "usage"),
    [
        ("claude", "claude-success.json", "11111111-1111-4111-8111-111111111111", "Claude answer", {"input": 11, "output": 7, "cache_read": 3, "cache_write": 2, "cost_usd": 0.12}),
        ("codex", "codex-success.ndjson", "codex-thread-1", "Codex answer", {"input": 13, "output": 8, "cache_read": 5}),
        ("agy", "agy-success.json", "agy-conversation-1", "agy answer", {"input": 17, "output": 9, "thinking": 2, "cache_read": 4, "total": 32}),
    ],
)
def test_run_provider_parses_fixture_usage_identity_and_raw_artifacts(
    executor_id, fixture, session_id, answer, usage, tmp_path,
    _mock_clean_codex_host,
):
    """Dropping final envelopes or raw bytes would make a settled turn unverifiable."""
    stdout = (FIXTURES / fixture).read_bytes()
    seen = []

    def fake_run(command):
        seen.append(command)
        return _result(stdout=stdout)

    request = _request(
        executor_id,
        tmp_path,
        session_id=session_id if executor_id == "claude" else None,
    )

    result = _fake_provider(request, fake_run)

    assert result.valid is True
    assert result.session_id == session_id
    assert result.answer == answer
    assert result.usage == usage
    assert result.attempt_count == 1
    assert result.answer_ref and result.stdout_ref and result.stderr_ref
    assert request.artifact_store.read_bytes(result.stdout_ref) == stdout
    assert result.stdout_digest == hashlib.sha256(stdout).hexdigest()
    if executor_id == "agy":
        assert json.loads(seen[0].stdin) == {
            "event": "user", "message": {"content": "secret prompt only in stdin"},
        }
    else:
        assert seen[0].stdin == b"secret prompt only in stdin"


def test_structured_error_precedes_nonzero_exit_and_preserves_provider_identity(
    tmp_path, _mock_clean_codex_host,
):
    """Replacing a provider error with a generic exit code loses the actionable cause."""
    result = _fake_provider(
        _request("codex", tmp_path),
        lambda _command: _result(
            stdout=(FIXTURES / "codex-error.ndjson").read_bytes(),
            stderr=b"generic launcher failure",
            code=1,
        ),
    )

    assert result.valid is False
    assert result.reason == "provider-error"
    assert result.session_id == "codex-thread-1"
    assert "quota exhausted" in (result.hint or "")


def test_malformed_or_missing_session_or_final_result_fails_closed(
    tmp_path, _mock_clean_codex_host,
):
    """Accepting a fragment without identity or a terminal event permits unsafe resume."""
    result = _fake_provider(
        _request("codex", tmp_path),
        lambda _command: _result(stdout=b'{"type":"item.completed","item":{"type":"agent_message","text":"partial"}}\n'),
    )

    assert result.valid is False
    assert result.reason == "protocol-error"
    assert result.session_id is None
    with pytest.raises(ProviderProtocolError):
        ProviderRegistry.default().require("claude").parse(b"not json")


@pytest.mark.parametrize("envelope", [
    {"subtype": "success"},
    {"type": "message", "subtype": "success"},
    {"type": "result", "subtype": "failure"},
])
def test_claude_requires_the_documented_terminal_success_discriminators(tmp_path, envelope):
    """A result-looking fragment without Claude's terminal success shape is not resumable evidence."""
    envelope.update({
        "session_id": "11111111-1111-4111-8111-111111111111",
        "result": "answer",
        "is_error": False,
    })
    result = _fake_provider(_request(
        "claude", tmp_path, session_id="11111111-1111-4111-8111-111111111111",
    ), lambda _command: _result(stdout=json.dumps(envelope).encode("utf-8")))

    assert result.valid is False
    assert result.reason == "protocol-error"


@pytest.mark.parametrize("executor_id, fixture", [
    ("claude", "claude-success.json"),
    ("codex", "codex-error.ndjson"),
    ("agy", "agy-error.json"),
])
def test_each_provider_structured_error_precedes_nonzero_process_exit(
    executor_id, fixture, tmp_path, _mock_clean_codex_host,
):
    """An authoritative provider envelope must outrank generic child exit classification."""
    runner = lambda _command: _result(
        stdout=(FIXTURES / fixture).read_bytes(), code=1,
    )
    request = _request(
        executor_id,
        tmp_path,
        session_id="11111111-1111-4111-8111-111111111111" if executor_id == "claude" else None,
    )
    if executor_id == "claude":
        runner = lambda _command: _result(
            stdout=b'{"type":"result","subtype":"success","is_error":true,"session_id":"11111111-1111-4111-8111-111111111111","result":"quota exhausted"}', code=1,
        )

    result = _fake_provider(request, runner)

    assert result.reason == "provider-error"


def test_provider_result_freezes_usage_and_redacts_prompt_derived_values():
    """Frozen dataclass fields alone would still expose mutable usage or answer text."""
    usage = {"input": 3}
    result = ProviderResult(
        executor_id="custom",
        valid=True,
        reason="ok",
        hint=None,
        attempt_count=1,
        duration=0,
        usage=usage,
        session_id="s",
        answer="secret prompt only in stdin",
        stdout=b"secret prompt only in stdin",
        stderr=b"secret prompt only in stdin",
    )
    usage["input"] = 9

    assert result.usage == {"input": 3}
    assert isinstance(result.usage, MappingProxyType)
    with pytest.raises(TypeError):
        result.usage["input"] = 9  # type: ignore[index]
    assert "secret prompt only in stdin" not in repr(result)
    with pytest.raises(ProviderRequestError):
        ProviderResult(
            executor_id="custom", valid=True, reason="ok", hint=None, attempt_count=1,
            duration=0, usage={"nested": {}}, session_id="s", answer="", stdout=b"", stderr=b"",
        )


def test_retry_is_limited_to_known_pre_start_spawn_failure_and_attempts_are_visible(tmp_path):
    """Retrying after a started turn can duplicate spend; spawn failure cannot."""
    calls = []
    success = (FIXTURES / "agy-success.json").read_bytes()

    def fake_run(_command):
        calls.append(1)
        if len(calls) == 1:
            return _result(status=ProcessStatus.SPAWN_ERROR, stdout=b"", stderr=b"missing")
        return _result(stdout=success)

    result = _fake_provider(_request("agy", tmp_path, retries=1), fake_run)

    assert result.valid is True
    assert result.attempt_count == 2
    assert len(calls) == 2


def test_known_started_failure_is_not_retried(
    tmp_path, _mock_clean_codex_host,
):
    """A thread-start event is evidence that repeating the request could charge twice."""
    calls = []

    def fake_run(_command):
        calls.append(1)
        return _result(stdout=(FIXTURES / "codex-error.ndjson").read_bytes(), code=1)

    result = _fake_provider(_request("codex", tmp_path, retries=3), fake_run)

    assert result.reason == "provider-error"
    assert result.attempt_count == 1
    assert len(calls) == 1


def test_claude_assigns_one_session_id_before_a_safe_pre_start_retry(tmp_path):
    """Changing Claude's caller-assigned UUID between attempts would lose exact provenance."""
    seen = []

    def fake_run(command):
        seen.append(command)
        if len(seen) == 1:
            return _result(status=ProcessStatus.SPAWN_ERROR, stdout=b"", stderr=b"missing")
        return _result(stdout=(FIXTURES / "claude-success.json").read_bytes())

    _fake_provider(_request("claude", tmp_path, retries=1), fake_run)

    assert seen[0].argv[seen[0].argv.index("--session-id") + 1] == seen[1].argv[
        seen[1].argv.index("--session-id") + 1
    ]


def test_structured_error_precedes_a_timeout_when_stdout_contains_provider_evidence(tmp_path):
    """A timeout category cannot erase an authoritative provider error it already emitted."""
    result = _fake_provider(
        _request("agy", tmp_path),
        lambda _command: _result(
            stdout=(FIXTURES / "agy-error.json").read_bytes(), status=ProcessStatus.TIMEOUT,
        ),
    )

    assert result.reason == "provider-error"
    assert "quota exhausted" in (result.hint or "")


def test_run_many_is_input_ordered_bounded_and_retains_partial_failures(
    tmp_path, _mock_clean_codex_host,
):
    """Completion order must not change seat order or erase a failed provider result."""
    active = 0
    peak = 0

    def fake_run(command):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            time.sleep(0.02 if command.argv[0] == "claude" else 0)
            if command.argv[0] == "codex":
                return _result(stdout=(FIXTURES / "codex-error.ndjson").read_bytes(), code=1)
            if command.argv[0] == "claude":
                return _result(stdout=(FIXTURES / "claude-success.json").read_bytes())
            return _result(stdout=(FIXTURES / "agy-success.json").read_bytes())
        finally:
            active -= 1

    requests = [
        _request(
            name,
            tmp_path,
            artifact_prefix=f"batch/{name}",
            session_id="11111111-1111-4111-8111-111111111111" if name == "claude" else None,
        )
        for name in ("claude", "codex", "agy")
    ]

    results = _fake_many(requests, fake_run, max_workers=2)

    assert [result.executor_id for result in results] == ["claude", "codex", "agy"]
    assert [result.valid for result in results] == [True, False, True]
    assert peak <= 2


def test_artifact_storage_failure_returns_terminal_result_with_partial_refs(tmp_path, monkeypatch):
    """A durable-store failure must retain the process evidence instead of raising it away."""
    store = ArtifactStore(tmp_path / "store")
    original_write = store.write_bytes
    calls = 0

    def fail_after_answer(path, data):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ArtifactQuotaError("simulated quota")
        return original_write(path, data)

    monkeypatch.setattr(store, "write_bytes", fail_after_answer)
    result = _fake_provider(
        _request("agy", tmp_path, artifact_store=store),
        lambda _command: _result(stdout=(FIXTURES / "agy-success.json").read_bytes()),
    )

    assert result.valid is False
    assert result.reason == "artifact-storage-error"
    assert result.answer_ref is not None
    assert result.stdout_ref is result.stderr_ref is None
    assert result.session_id == "agy-conversation-1"
    assert result.usage == {"input": 17, "output": 9, "thinking": 2, "cache_read": 4, "total": 32}
    assert result.stdout_digest == hashlib.sha256((FIXTURES / "agy-success.json").read_bytes()).hexdigest()
    assert "simulated quota" not in (result.hint or "")


def test_closed_artifact_store_becomes_terminal_evidence_not_an_exception(tmp_path, monkeypatch):
    """A store closed after dispatch cannot erase an otherwise settled provider turn."""
    store = ArtifactStore(tmp_path / "closed-store")
    store.close()
    stdout = (FIXTURES / "agy-success.json").read_bytes()
    result = _fake_provider(_request("agy", tmp_path, artifact_store=store),
                            lambda _command: _result(stdout=stdout))

    assert result.valid is False
    assert result.reason == "artifact-storage-error"
    assert result.answer_ref is result.stdout_ref is result.stderr_ref is None
    assert result.attempt_count == 1
    assert result.session_id == "agy-conversation-1"
    assert result.usage == {"input": 17, "output": 9, "thinking": 2, "cache_read": 4, "total": 32}
    assert result.stdout == stdout
    assert result.stdout_digest == hashlib.sha256(stdout).hexdigest()
    assert "artifact store is closed" not in (result.hint or "")


def test_raw_artifact_io_failure_returns_partial_refs_and_raw_digests(tmp_path, monkeypatch):
    """A raw fsync/write error after one durable artifact must retain the partial evidence."""
    store = ArtifactStore(tmp_path / "store")
    original_write = store.write_bytes
    calls = 0

    def fail_after_answer(path, data):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated fsync failure")
        return original_write(path, data)

    monkeypatch.setattr(store, "write_bytes", fail_after_answer)
    stdout = (FIXTURES / "agy-success.json").read_bytes()
    result = _fake_provider(_request("agy", tmp_path, artifact_store=store),
                            lambda _command: _result(stdout=stdout))

    assert result.reason == "artifact-storage-error"
    assert result.answer_ref is not None
    assert result.stdout_ref is result.stderr_ref is None
    assert result.stdout_digest == hashlib.sha256(stdout).hexdigest()
    assert "simulated fsync failure" not in (result.hint or "")


def test_run_many_keeps_duplicate_artifact_prefix_as_an_ordered_storage_failure(tmp_path):
    """One colliding seat must not make the batch discard other completed evidence."""
    store = ArtifactStore(tmp_path / "store")
    requests = [
        _request("agy", tmp_path, artifact_store=store, artifact_prefix="same")
        for _ in range(2)
    ]

    results = _fake_many(requests,
                         lambda _command: _result(stdout=(FIXTURES / "agy-success.json").read_bytes()),
                         max_workers=1)

    assert [result.reason for result in results] == ["ok", "artifact-storage-error"]
    assert results[1].stdout_digest == hashlib.sha256((FIXTURES / "agy-success.json").read_bytes()).hexdigest()


def test_run_many_preserves_order_when_a_closed_store_fails_after_dispatch(tmp_path):
    """One closed output store must not prevent a later healthy seat from being reported."""
    closed = ArtifactStore(tmp_path / "closed-store")
    closed.close()
    healthy = ArtifactStore(tmp_path / "healthy-store")
    requests = (
        _request("agy", tmp_path, artifact_store=closed, artifact_prefix="closed"),
        _request("agy", tmp_path, artifact_store=healthy, artifact_prefix="healthy"),
    )

    results = _fake_many(requests,
                         lambda _command: _result(stdout=(FIXTURES / "agy-success.json").read_bytes()),
                         max_workers=1)

    assert [result.reason for result in results] == ["artifact-storage-error", "ok"]
    assert results[0].session_id == results[1].session_id == "agy-conversation-1"
