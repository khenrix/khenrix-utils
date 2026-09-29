"""Pinned Claude read-only tools and explicit Vertex authentication route."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


FANOUT_ROOT = Path(__file__).resolve().parents[1] / "shared" / "lib" / "fanout"
SPEC = importlib.util.spec_from_file_location(
    "_fanout_claude_restricted_contracts", FANOUT_ROOT / "__init__.py",
    submodule_search_locations=[str(FANOUT_ROOT)],
)
assert SPEC and SPEC.loader
fanout = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fanout
SPEC.loader.exec_module(fanout)


def _private_vertex_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "owner"
    home.mkdir(mode=0o700)
    config = home / ".claude"
    config.mkdir(mode=0o700)
    settings = config / "settings.json"
    settings.write_text(json.dumps({"env": {
        "CLAUDE_CODE_USE_VERTEX": "1",
        "ANTHROPIC_VERTEX_PROJECT_ID": "disposable-test-project",
        "CLOUD_ML_REGION": "eu",
    }}), encoding="utf-8")
    settings.chmod(0o600)
    adc = home / ".config" / "gcloud" / "application_default_credentials.json"
    adc.parent.mkdir(parents=True)
    adc.write_text("{}", encoding="utf-8")
    adc.chmod(0o600)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGY_ADC_AUTH", "true")
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    return home


def _request(tmp_path: Path, tier: str) -> fanout.ProviderRequest:
    profile = fanout.ProviderRegistry.default().select("claude", "read-only", tier)
    return fanout.ProviderRequest(
        "claude", "Read fixture.txt", cwd=tmp_path, profile=profile,
        session_id="dc91a643-13c8-4ee3-922f-0a33f23d74a0",
    )


@pytest.mark.parametrize("tier", ["standard", "deep"])
def test_claude_read_only_profile_pins_a_closed_read_tool_surface(tier: str):
    profile = fanout.ProviderRegistry.default().select("claude", "read-only", tier)

    for argv in (profile.initial_argv, profile.resume_argv):
        assert "--restricted" in argv
        assert "--strict-mcp-config" in argv
        assert argv[argv.index("--tools") + 1] == "Read,Glob,Grep"
        assert argv[argv.index("--permission-prompts") + 1] == "none"
        assert argv[argv.index("--permission-mode") + 1] == "plan"
        assert argv[argv.index("--disallowedTools") + 1] == "ExitPlanMode"
        assert "--dangerously-skip-permissions" not in argv


def test_restricted_claude_gets_only_the_verified_vertex_route(tmp_path: Path, monkeypatch):
    home = _private_vertex_home(tmp_path, monkeypatch)
    request = _request(tmp_path, "deep")

    command = fanout.ProviderRegistry.default().require("claude").build_command(request)
    child = fanout.build_child_environment(base={"PATH": "/usr/bin"},
                                           overrides=command.environment)

    assert command.environment == {
        "GOOGLE_APPLICATION_CREDENTIALS": str(home / ".config" / "gcloud" /
                                               "application_default_credentials.json"),
        "CLAUDE_CODE_USE_VERTEX": "1",
        "ANTHROPIC_VERTEX_PROJECT_ID": "disposable-test-project",
        "CLOUD_ML_REGION": "eu",
    }
    assert all(child[key] == value for key, value in command.environment.items())
    assert "HOME" not in child


def test_restricted_claude_rejects_unsafe_route_settings(tmp_path: Path, monkeypatch):
    home = _private_vertex_home(tmp_path, monkeypatch)
    settings = home / ".claude" / "settings.json"
    settings.chmod(0o644)

    with pytest.raises(fanout.ProviderRequestError, match="Vertex|route|settings"):
        fanout.ProviderRegistry.default().require("claude").build_command(
            _request(tmp_path, "deep")
        )


def test_restricted_claude_rejects_symlinked_settings(tmp_path: Path, monkeypatch):
    home = _private_vertex_home(tmp_path, monkeypatch)
    settings = home / ".claude" / "settings.json"
    target = tmp_path / "settings.json"
    target.write_bytes(settings.read_bytes())
    target.chmod(0o600)
    settings.unlink()
    settings.symlink_to(target)

    with pytest.raises(fanout.ProviderRequestError, match="Vertex route"):
        fanout.ProviderRegistry.default().require("claude").build_command(
            _request(tmp_path, "standard")
        )


def test_restricted_claude_route_is_rechecked_at_child_boundary(tmp_path: Path, monkeypatch):
    home = _private_vertex_home(tmp_path, monkeypatch)
    command = fanout.ProviderRegistry.default().require("claude").build_command(
        _request(tmp_path, "standard")
    )
    settings = home / ".claude" / "settings.json"
    data = json.loads(settings.read_text(encoding="utf-8"))
    data["env"]["ANTHROPIC_VERTEX_PROJECT_ID"] = "different-test-project"
    settings.write_text(json.dumps(data), encoding="utf-8")
    settings.chmod(0o600)

    with pytest.raises(fanout.ProcessValidationError, match="Vertex route"):
        fanout.build_child_environment(base={"PATH": "/usr/bin"},
                                       overrides=command.environment)


def test_restricted_claude_does_not_silently_ignore_parent_adc_override(
        tmp_path: Path, monkeypatch):
    _private_vertex_home(tmp_path, monkeypatch)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/other/credentials.json")

    with pytest.raises(fanout.ProviderRequestError, match="credential|ADC|route"):
        fanout.ProviderRegistry.default().require("claude").build_command(
            _request(tmp_path, "deep")
        )
