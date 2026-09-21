"""Hermetic coverage for portable model/effort defaults.

The defaults path must be narrow: it owns declared leaves and preserves every
other setting, MCP, plugin, secret and instruction without even inspecting the
other reconciliation surfaces.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import tomllib


ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "khenrix_reconcile_defaults", ROOT / "scripts" / "lib" / "reconcile.py")
reconcile = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(reconcile)


def caps():
    return {
        "_dir": ROOT,
        "settings": {
            "defaults": {
                "claude": {
                    "model": "best",
                    "effortLevel": "xhigh",
                    "ultracode": True,
                },
                "codex": {
                    "model": "gpt-5.6-sol",
                    "model_reasoning_effort": "xhigh",
                    "plan_mode_reasoning_effort": "ultra",
                    "agents": {"default_subagent_reasoning_effort": "xhigh"},
                },
                "agy": {"model": "Gemini 3.8 Flash (High)"},
            }
        },
    }


def test_claude_adds_missing_defaults_and_preserves_unrelated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    original = {
        "permissions": {"allow": ["Read"]},
        "hooks": {"Stop": [{"secretSentinel": "keep-me"}]},
        "plugins": {"private-plugin": True},
    }
    path.write_text(json.dumps(original))

    rows, apply = reconcile.portable_defaults_report("claude", caps())
    assert {row[1] for row in rows} == {"ADD"}
    apply(False)

    live = json.loads(path.read_text())
    assert live["model"] == "best"
    assert live["effortLevel"] == "xhigh"
    assert live["ultracode"] is True
    for key, value in original.items():
        assert live[key] == value
    backups = list(path.parent.glob("settings.json.khenrix-backup*"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text()) == original


def test_claude_drift_requires_update_drift(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "model": "custom",
        "effortLevel": "low",
        "ultracode": False,
        "unrelated": "keep",
    }))

    rows, apply = reconcile.portable_defaults_report("claude", caps())
    assert {row[1] for row in rows} == {"UPDATE"}
    actions = apply(False)
    assert json.loads(path.read_text())["model"] == "custom"
    assert not list(path.parent.glob("settings.json.khenrix-backup*"))
    assert "skipped drifted" in actions[0]

    apply(True)
    live = json.loads(path.read_text())
    assert live == {
        "model": "best",
        "effortLevel": "xhigh",
        "ultracode": True,
        "unrelated": "keep",
    }


def test_codex_updates_root_and_nested_defaults_without_touching_other_tables(
        tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / ".codex" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(
        'model = "old"\n'
        'private_token_reference = "op://Private/codex/token"\n\n'
        '[agents]\n'
        'default_subagent_reasoning_effort = "low"\n'
        'max_threads = 7\n\n'
        '[mcp_servers.private]\n'
        'command = "private-command"\n')

    _, apply = reconcile.portable_defaults_report("codex", caps())
    apply(True)
    live = tomllib.loads(path.read_text())

    assert live["model"] == "gpt-5.6-sol"
    assert live["model_reasoning_effort"] == "xhigh"
    assert live["plan_mode_reasoning_effort"] == "ultra"
    assert live["agents"]["default_subagent_reasoning_effort"] == "xhigh"
    assert live["agents"]["max_threads"] == 7
    assert live["mcp_servers"]["private"]["command"] == "private-command"
    assert live["private_token_reference"] == "op://Private/codex/token"
    assert path.read_text().count("model_reasoning_effort =") == 1
    assert path.read_text().count("default_subagent_reasoning_effort =") == 1


def test_codex_refuses_a_nested_default_when_parent_is_not_a_table(
        tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / ".codex" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text('agents = "custom-launcher"\n')

    rows, apply = reconcile.portable_defaults_report("codex", caps())
    blocked = [row for row in rows if row[0].endswith("default_subagent_reasoning_effort")]
    assert blocked[0][1] == "REFUSED"
    apply(True)

    live = tomllib.loads(path.read_text())
    assert live["agents"] == "custom-launcher"
    assert "default_subagent_reasoning_effort" not in live


def test_agy_updates_only_model(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / ".gemini" / "antigravity-cli" / "settings.json"
    path.parent.mkdir(parents=True)
    original = {
        "model": "older",
        "trustedWorkspaces": ["/private/project"],
        "statusLine": {"command": "private-status"},
        "authReference": "keychain://agy",
    }
    path.write_text(json.dumps(original))

    _, apply = reconcile.portable_defaults_report("agy", caps())
    apply(True)
    live = json.loads(path.read_text())
    assert live["model"] == "Gemini 3.8 Flash (High)"
    for key in ("trustedWorkspaces", "statusLine", "authReference"):
        assert live[key] == original[key]


def test_defaults_only_does_not_inspect_other_reconciliation_surfaces(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("defaults-only inspected an unrelated surface")

    for name in (
        "desired_mcp", "mcp_current", "classify_mcp", "statusline_asset_report",
        "hook_asset_report", "shell_aliases_report", "instructions_report",
        "claude_settings", "codex_settings", "agy_settings",
    ):
        monkeypatch.setattr(reconcile, name, forbidden)

    reconcile.reconcile(
        "claude", caps(), apply=True, update_drift=True, defaults_only=True)
    output = capsys.readouterr().out
    assert "Portable model/effort defaults" in output
    assert "MCP servers" not in output
    assert "Shell aliases" not in output


def test_full_settings_apply_keeps_baseline_and_defaults_from_the_same_file(
        tmp_path, monkeypatch):
    """The two layers must re-read between writes instead of restoring a stale snapshot."""
    monkeypatch.setenv("HOME", str(tmp_path))
    manifest = caps()
    manifest["settings"]["claude"] = {"theme": "dark-ansi"}

    _, apply = reconcile.settings_report("claude", manifest)
    apply(False)

    live = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert live["theme"] == "dark-ansi"
    assert live["model"] == "best"
    assert live["effortLevel"] == "xhigh"
    assert live["ultracode"] is True


def test_repository_declares_the_approved_defaults():
    with (ROOT / "capabilities.toml").open("rb") as f:
        defaults = tomllib.load(f)["settings"]["defaults"]
    assert defaults == caps()["settings"]["defaults"]
