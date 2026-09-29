"""Status-line deployment must not inspect or rewrite other CLI settings."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("statusline_reconcile", ROOT / "scripts/lib/reconcile.py")
reconcile = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(reconcile)


def statusline_caps() -> dict:
    command = "${HOME}/.local/share/khenrix-utils/statusline/khenrix-statusline"
    return {"_dir": ROOT, "settings": {
        "claude": {"statusLine": {"type": "command", "command": command + " claude"}},
        "agy": {"statusLine": {"type": "command", "command": command + " agy"}},
        "codex": {"tui": {"status_line": ["model-with-reasoning", "context-remaining"],
                          "status_line_use_colors": True}},
    }}


def test_matching_managed_keys_ignore_local_statusline_options():
    rows, todo = reconcile.statusline_config_rows(
        {"statusLine": {"type": "command", "command": "render claude", "padding": 0}},
        {"type": "command", "command": "render claude"},
    )
    assert rows[0][1] == "MATCH"
    assert todo is None


def test_null_statusline_is_refused_without_crashing(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(reconcile, "load_caps", statusline_caps)
    path = tmp_path / ".claude/settings.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"statusLine": null, "theme": "dark"}')
    assert reconcile.main(["--cli", "claude", "--statusline-only", "--apply",
                           "--update-drift"]) == 0
    assert json.loads(path.read_text()) == {"statusLine": None, "theme": "dark"}


def test_inline_codex_tui_is_refused_without_corrupting_toml(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / ".codex/config.toml"
    path.parent.mkdir(parents=True)
    path.write_text('tui = { status_line = ["model"], other = true }\n')
    rows, apply = reconcile.statusline_settings_report("codex", statusline_caps())
    assert any(row[1] == "REFUSED" for row in rows)
    assert apply(True) == []
    assert tomllib.loads(path.read_text()) == {
        "tui": {"status_line": ["model"], "other": True}}


def test_quoted_codex_tui_key_updates_without_duplicate(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(reconcile, "load_caps", statusline_caps)
    path = tmp_path / ".codex/config.toml"
    path.parent.mkdir(parents=True)
    path.write_text('[tui]\n"status_line" = ["model"]\nother = true\n')
    assert reconcile.main(["--cli", "codex", "--statusline-only", "--apply",
                           "--update-drift"]) == 0
    tui = tomllib.loads(path.read_text())["tui"]
    assert tui == {"status_line": ["model-with-reasoning", "context-remaining"],
                   "status_line_use_colors": True, "other": True}


def test_escaped_codex_tui_key_refuses_invalid_candidate(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / ".codex/config.toml"
    path.parent.mkdir(parents=True)
    original = '[tui]\n"status\\u005fline" = ["model"]\n'
    path.write_text(original)
    rows, apply = reconcile.statusline_settings_report("codex", statusline_caps())
    assert any(row[1] == "UPDATE" for row in rows)
    assert any("refused" in action for action in apply(True))
    assert path.read_text() == original


def test_full_codex_reconcile_refuses_unsupported_tui_syntax(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / ".codex/config.toml"
    path.parent.mkdir(parents=True)
    want = {"codex": {"tui": {"status_line": ["context-remaining"]}}}
    for original in ('tui = { status_line = ["model"] }\n',
                     '[tui]\n"status\\u005fline" = ["model"]\n'):
        path.write_text(original)
        rows, apply = reconcile.codex_settings(want)
        assert any(row[1] == "UPDATE" for row in rows)
        assert any("refused" in action for action in apply(True))
        assert path.read_text() == original


def test_full_codex_reconcile_updates_regular_tui_table(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / ".codex/config.toml"
    path.parent.mkdir(parents=True)
    path.write_text('[tui]\nstatus_line = ["model"]\nother = true\n')
    _, apply = reconcile.codex_settings({
        "codex": {"tui": {"status_line": ["context-remaining"]}}})
    assert any("wrote" in action for action in apply(True))
    assert tomllib.loads(path.read_text())["tui"] == {
        "status_line": ["context-remaining"], "other": True}


def test_statusline_only_apply_preserves_other_settings_and_local_options(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(reconcile, "load_caps", statusline_caps)
    monkeypatch.setattr(reconcile, "mcp_current", lambda cli: 1 / 0)

    claude_path = tmp_path / ".claude/settings.json"
    claude_path.parent.mkdir(parents=True)
    claude_path.write_text(json.dumps({
        "statusLine": {"type": "command", "command": "custom claude", "padding": 0},
        "permissions": {"allow": ["Read"]},
    }))
    agy_path = tmp_path / ".gemini/antigravity-cli/settings.json"
    agy_path.parent.mkdir(parents=True)
    agy_path.write_text(json.dumps({
        "statusLine": {"type": "command", "command": "custom agy", "enabled": True},
        "trustedWorkspaces": ["private-project"],
    }))
    keybindings = agy_path.parent / "keybindings.json"
    keybindings.write_text("{intentionally invalid and unread}")
    codex_path = tmp_path / ".codex/config.toml"
    codex_path.parent.mkdir(parents=True)
    codex_path.write_text(
        'approval_policy = "never"\n\n[tui]\nstatus_line = ["model"]\n'
        'status_line_use_colors = false\nother = true\n\n[mcp_servers.private]\n'
        'command = "keep-me"\n')
    installed = tmp_path / ".local/share/khenrix-utils/statusline/khenrix-statusline"
    installed.parent.mkdir(parents=True)
    installed.write_text("old renderer\n")

    assert reconcile.main(["--all", "--statusline-only", "--status"]) == 0
    assert "MCP servers" not in capsys.readouterr().out
    assert installed.read_text() == "old renderer\n"
    assert reconcile.main(["--all", "--statusline-only", "--apply", "--update-drift"]) == 0

    claude = json.loads(claude_path.read_text())
    agy = json.loads(agy_path.read_text())
    codex = tomllib.loads(codex_path.read_text())
    assert claude["statusLine"] == {
        **reconcile.desired_statusline(statusline_caps()["settings"], "claude"),
        "padding": 0,
    }
    assert claude["permissions"] == {"allow": ["Read"]}
    assert agy["statusLine"] == {
        **reconcile.desired_statusline(statusline_caps()["settings"], "agy"),
        "enabled": True,
    }
    assert agy["trustedWorkspaces"] == ["private-project"]
    assert keybindings.read_text() == "{intentionally invalid and unread}"
    assert codex["approval_policy"] == "never"
    assert codex["tui"] == {"status_line": ["model-with-reasoning", "context-remaining"],
                            "status_line_use_colors": True, "other": True}
    assert codex["mcp_servers"]["private"]["command"] == "keep-me"
    assert installed.read_bytes() == (ROOT / "statusline/khenrix-statusline").read_bytes()


def test_statusline_only_apply_requires_update_drift_for_existing_values(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(reconcile, "load_caps", statusline_caps)
    path = tmp_path / ".claude/settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"statusLine": {"type": "command", "command": "custom"}}))
    assert reconcile.main(["--cli", "claude", "--statusline-only", "--apply"]) == 0
    assert json.loads(path.read_text())["statusLine"]["command"] == "custom"
