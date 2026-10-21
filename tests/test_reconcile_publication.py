"""Settings plans bind to their read bytes; publication never truncates live files."""
import importlib.util
import json
import os
from pathlib import Path
import stat
import tomllib

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("publication_reconcile", ROOT / "scripts/lib/reconcile.py")
reconcile = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reconcile)


@pytest.mark.parametrize("cli", ["claude", "codex", "agy"])
@pytest.mark.parametrize("change", ["managed", "unrelated", "remove", "create"])
def test_defaults_refuse_changed_preimage(tmp_path, monkeypatch, cli, change):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = {"claude": reconcile.claude_settings_path,
            "codex": reconcile.codex_config_path, "agy": reconcile.agy_settings_path}[cli]()
    path.parent.mkdir(parents=True)
    initial = b'model = "old"\n' if cli == "codex" else b'{"model":"old"}'
    if change != "create":
        path.write_bytes(initial)
    caps = {"settings": {"defaults": {cli: {"model": "wanted"}}}}
    _, apply = reconcile.portable_defaults_report(cli, caps)
    if change == "remove":
        path.unlink()
    else:
        text = ('model = "external"\n' if cli == "codex" else '{"model":"external"}')
        if change == "unrelated":
            text = ('model = "old"\nlocal = true\n' if cli == "codex"
                    else '{"model":"old","local":true}')
        path.write_text(text)
    current = path.read_bytes() if path.exists() else None
    with pytest.raises(reconcile.ReconcileReadError, match="re-plan"):
        apply(True)
    assert (path.read_bytes() if path.exists() else None) == current
    assert not list(path.parent.glob("*.khenrix-backup*"))


@pytest.mark.parametrize("kind", ["codex", "claude", "agy", "keybindings", "statusline", "codex-statusline", "full-defaults"])
def test_other_report_writers_refuse_stale_inputs(tmp_path, monkeypatch, kind):
    monkeypatch.setenv("HOME", str(tmp_path))
    if kind in ("codex", "codex-statusline"):
        path = reconcile.codex_config_path()
        report = (lambda: reconcile.codex_settings({"approval_policy": "never"})) if kind == "codex" else (
            lambda: reconcile.statusline_settings_report("codex", {
                "settings": {"codex": {"tui": {"status_line": ["model"]}}}}))
    elif kind == "claude":
        path = reconcile.claude_settings_path()
        report = lambda: reconcile.claude_settings({"claude": {"model": "wanted"}})
    elif kind == "statusline":
        path = reconcile.claude_settings_path()
        report = lambda: reconcile.statusline_settings_report("claude", {
            "settings": {"claude": {"statusLine": {"command": "footer"}}}})
    elif kind == "full-defaults":
        path = reconcile.claude_settings_path()
        report = lambda: reconcile.settings_report("claude", {
            "settings": {"defaults": {"claude": {"model": "wanted"}}}})
    else:
        path = reconcile.agy_keybindings_path() if kind == "keybindings" else reconcile.agy_settings_path()
        report = lambda: reconcile.agy_settings({"trusted_paths": ["/project"],
                                               "agy": {"keybindings": {"accept": ["enter"]}}})
    _, apply = report()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('local = true\n' if kind.startswith("codex") else '{"local":true}')
    current = path.read_bytes()
    with pytest.raises(reconcile.ReconcileReadError, match="re-plan"):
        apply(True)
    assert path.read_bytes() == current


@pytest.mark.parametrize("failure", ["write", "fsync", "replace"])
@pytest.mark.parametrize("cli", ["common", "codex-mcp", "agy-mcp"])
def test_publication_failure_preserves_original(tmp_path, monkeypatch, failure, cli):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = (reconcile.codex_config_path() if cli == "codex-mcp" else
            reconcile.agy_mcp_path() if cli == "agy-mcp" else tmp_path / "settings.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    original = b'local = true\n' if cli == "codex-mcp" else b'{"local":true}'
    path.write_bytes(original)
    path.chmod(0o640)
    def fail(*args, **kwargs):
        raise OSError("injected publication failure")
    if failure == "write":
        original_fdopen = os.fdopen
        class BrokenWriter:
            def __init__(self, *args, **kwargs):
                self.stream = original_fdopen(*args, **kwargs)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.stream.close()
            def fileno(self):
                return self.stream.fileno()
            write = fail
        monkeypatch.setattr(reconcile.os, "fdopen", BrokenWriter)
    else:
        monkeypatch.setattr(reconcile.os, failure, fail)
    with pytest.raises(OSError, match="injected"):
        if cli == "common":
            reconcile.write_json_object(path, {"local": True, "managed": True})
        else:
            reconcile.apply_mcp(cli.split("-")[0], "docs", {"url": "https://example.org"})
    assert path.read_bytes() == original
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert not list(path.parent.glob(".*.tmp"))


def test_common_writer_preserves_mode_before_atomic_publication_and_numbered_backups(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text('{"local":true}')
    path.chmod(0o640)
    replace = os.replace
    observed = []
    def observe(source, destination):
        assert Path(source).parent == path.parent
        assert stat.S_IMODE(Path(source).stat().st_mode) == 0o640
        observed.append(path.read_bytes())
        replace(source, destination)
    monkeypatch.setattr(reconcile.os, "replace", observe)
    reconcile.write_json_object(path, {"local": True, "model": "one"})
    reconcile.write_json_object(path, {"local": True, "model": "two"})
    assert len(observed) == 2
    assert path.with_suffix(".json.khenrix-backup").read_bytes() == observed[0]
    assert path.with_suffix(".json.khenrix-backup.1").read_bytes() == observed[1]


@pytest.mark.parametrize("contents", ["broken{", "[]", "null"])
def test_common_writer_refuses_invalid_json(tmp_path, contents):
    path = tmp_path / "settings.json"
    path.write_text(contents)
    with pytest.raises(reconcile.ReconcileReadError):
        reconcile.write_json_object(path, {"model": "wanted"})
    assert path.read_text() == contents


@pytest.mark.parametrize("cli", ["codex", "agy"])
def test_mcp_refuses_explicit_stale_preimage(tmp_path, monkeypatch, cli):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = reconcile.codex_config_path() if cli == "codex" else reconcile.agy_mcp_path()
    path.parent.mkdir(parents=True)
    path.write_text('local = true\n' if cli == "codex" else '{"local":true}')
    current = path.read_bytes()
    with pytest.raises(reconcile.ReconcileReadError, match="re-plan"):
        reconcile.apply_mcp(cli, "docs", {"url": "https://example.org"}, expected=None)
    assert path.read_bytes() == current


@pytest.mark.parametrize("cli", ["codex", "agy"])
def test_mcp_atomic_success_preserves_mode_and_unrelated_data(tmp_path, monkeypatch, cli):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = reconcile.codex_config_path() if cli == "codex" else reconcile.agy_mcp_path()
    path.parent.mkdir(parents=True)
    path.write_text('local = true\n' if cli == "codex" else '{"local":true}')
    original = path.read_bytes()
    path.chmod(0o640)
    reconcile.apply_mcp(cli, "docs", {"url": "https://example.org"})
    live = tomllib.loads(path.read_text()) if cli == "codex" else json.loads(path.read_text())
    assert live["local"] is True
    assert "docs" in live["mcp_servers" if cli == "codex" else "mcpServers"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert path.with_suffix(path.suffix + ".khenrix-backup").read_bytes() == original


@pytest.mark.parametrize("cli, contents", [("codex", "model = ["), ("agy", "broken{"),
                                           ("agy", "[]"), ("agy", "null")])
def test_mcp_invalid_input_is_retained_without_backup(tmp_path, monkeypatch, cli, contents):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = reconcile.codex_config_path() if cli == "codex" else reconcile.agy_mcp_path()
    path.parent.mkdir(parents=True)
    path.write_text(contents)
    with pytest.raises((reconcile.ReconcileReadError, tomllib.TOMLDecodeError)):
        reconcile.apply_mcp(cli, "docs", {"url": "https://example.org"})
    assert path.read_text() == contents
    assert not list(path.parent.glob("*.khenrix-backup*"))


def test_change_during_staging_refuses_publication(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text('{}')
    fsync = os.fsync
    def edit_during_fsync(descriptor):
        path.write_text('{"external":true}')
        fsync(descriptor)
    monkeypatch.setattr(reconcile.os, "fsync", edit_during_fsync)
    with pytest.raises(reconcile.ReconcileReadError, match="re-plan"):
        reconcile.write_json_object(path, {"managed": True})
    assert path.read_text() == '{"external":true}'
    assert not list(path.parent.glob("*.khenrix-backup*"))
    assert not list(path.parent.glob(".*.tmp"))


def test_full_reconcile_rebuilds_settings_after_own_mcp_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = reconcile.codex_config_path()
    path.parent.mkdir()
    path.write_text('local = true\n')
    caps = {"_dir": ROOT, "mcp_servers": {
        "one": {"url": "https://one.example.org"}, "two": {"url": "https://two.example.org"}},
        "settings": {"approval_policy": "never", "defaults": {"codex": {"model": "wanted"}}}}
    for name in ("statusline_asset_report", "hook_asset_report", "shell_aliases_report", "instructions_report"):
        monkeypatch.setattr(reconcile, name, lambda *args: ([], lambda update: []))
    reconcile.reconcile("codex", caps, True, True)
    live = tomllib.loads(path.read_text())
    assert live["local"] is True
    assert live["approval_policy"] == "never"
    assert live["model"] == "wanted"
    assert set(live["mcp_servers"]) == {"one", "two"}


def test_full_reconcile_refuses_external_edit_before_first_mcp(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = reconcile.codex_config_path()
    path.parent.mkdir()
    path.write_text('local = true\n')
    caps = {"_dir": ROOT, "mcp_servers": {"docs": {"url": "https://example.org"}}}
    for name in ("statusline_asset_report", "hook_asset_report", "shell_aliases_report"):
        monkeypatch.setattr(reconcile, name, lambda *args: ([], lambda update: []))
    def edit_after_review(*args):
        path.write_text('local = false\n')
        return [], lambda update: []
    monkeypatch.setattr(reconcile, "instructions_report", edit_after_review)
    with pytest.raises(reconcile.ReconcileReadError, match="re-plan"):
        reconcile.reconcile("codex", caps, True, True)
    assert path.read_text() == 'local = false\n'
    assert not list(path.parent.glob("*.khenrix-backup*"))


def test_native_root_overrides_bind_reports_and_hooks(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    for cli in ("codex", "claude"):
        _, apply = reconcile.portable_defaults_report(cli, {
            "settings": {"defaults": {cli: {"model": "wanted"}}}})
        apply(True)
    assert (tmp_path / "codex/config.toml").is_file()
    assert (tmp_path / "claude/settings.json").is_file()
    assert reconcile.claude_hook_command("hooks/check.py") == str(tmp_path / "claude/hooks/check.py")
    assert not (tmp_path / "home").exists()
