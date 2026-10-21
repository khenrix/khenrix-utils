"""Settings plans bind to their read bytes; publication never truncates live files."""
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
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


@pytest.mark.parametrize("contents", [
    b'\xef\xbb\xbf{"local":true}',
    b'{"local":"\xff"}',
    '{"local":true}'.encode("utf-16"),
], ids=["utf8-bom", "invalid-utf8", "utf16"])
def test_common_json_paths_refuse_unsupported_encoding_without_publication(tmp_path, contents):
    path = tmp_path / "settings.json"
    path.write_bytes(contents)
    with pytest.raises(reconcile.ReconcileReadError, match=str(path)):
        reconcile.read_json_object(path)
    with pytest.raises(reconcile.ReconcileReadError, match=str(path)):
        reconcile.read_json_object(path, preimage=contents)
    with pytest.raises(reconcile.ReconcileReadError, match=str(path)):
        reconcile.write_json_object(path, {"managed": True})
    assert path.read_bytes() == contents
    assert not list(path.parent.glob("*.khenrix-backup*"))
    assert not list(path.parent.glob(".*.tmp"))


def test_common_json_paths_preserve_valid_utf8_unicode(tmp_path):
    path = tmp_path / "settings.json"
    original = '{"local":"Blåbär 猫 🚚"}'.encode("utf-8")
    path.write_bytes(original)
    assert reconcile.read_json_object(path) == {"local": "Blåbär 猫 🚚"}
    assert reconcile.read_json_object(path, preimage=original) == {"local": "Blåbär 猫 🚚"}
    assert path.read_bytes() == original
    reconcile.write_json_object(path, {"local": "Blåbär 猫 🚚", "managed": True})
    assert json.loads(path.read_text()) == {"local": "Blåbär 猫 🚚", "managed": True}
    assert path.with_suffix(".json.khenrix-backup").read_bytes() == original


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


def test_claude_mcp_validates_review_before_native_invocation(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    path = reconcile.claude_mcp_path()
    path.write_text('{"external":true}')
    invoked = []
    monkeypatch.setattr(reconcile, "run", lambda command: (
        invoked.append(command) or subprocess.CompletedProcess(command, 0, "", "")))
    with pytest.raises(reconcile.ReconcileReadError, match="re-plan"):
        reconcile.apply_mcp("claude", "docs", {"url": "https://example.org"}, expected=b'{}')
    assert invoked == []
    assert path.read_text() == '{"external":true}'


@pytest.mark.parametrize("override", [False, True])
@pytest.mark.parametrize("legacy", ["absent", "nonempty", "empty-writable", "empty-readonly", "empty-with-modern"])
def test_claude_mcp_location_matches_licensed_resolver(tmp_path, monkeypatch, override, legacy):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CODE_CUSTOM_OAUTH_URL", raising=False)
    if override:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "override"))
    else:
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    roots = reconcile.resolve_native_roots()
    roots.claude.mkdir(parents=True)
    modern = (roots.claude if override else home) / ".claude.json"
    old = roots.claude / ".config.json"
    if legacy != "absent":
        old.write_text('{}' if legacy == "nonempty" else '')
        old.chmod(0o400 if legacy == "empty-readonly" else 0o600)
    if legacy == "empty-with-modern":
        modern.write_text('{}')
    expected = old if legacy in ("nonempty", "empty-writable") else modern
    assert reconcile.claude_mcp_path() == expected


def test_claude_custom_oauth_mcp_filename_matches_installed_production_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "override"))
    monkeypatch.setenv("CLAUDE_CODE_CUSTOM_OAUTH_URL", "https://example.org")
    assert reconcile.claude_mcp_path() == tmp_path / "override/.claude-custom-oauth.json"


@pytest.mark.parametrize("dangling", [False, True])
def test_json_publication_refuses_config_symlinks(tmp_path, dangling):
    target = tmp_path / "target.json"
    if not dangling:
        target.write_text('{"local":true}')
    path = tmp_path / "settings.json"
    path.symlink_to(target)
    with pytest.raises(reconcile.ReconcileReadError, match="symlink"):
        reconcile.write_json_object(path, {"managed": True})
    assert path.is_symlink()
    assert (target.read_text() if target.exists() else None) == (None if dangling else '{"local":true}')
    assert not list(tmp_path.glob("*.khenrix-backup*"))
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize("cli", ["codex", "agy"])
def test_sequential_mcp_writes_do_not_accept_intervening_external_edit(tmp_path, monkeypatch, cli):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = reconcile.codex_config_path() if cli == "codex" else reconcile.agy_mcp_path()
    path.parent.mkdir(parents=True)
    path.write_text('local = true\n' if cli == "codex" else '{"local":true}')
    caps = {"_dir": ROOT, "mcp_servers": {
        "one": {"url": "https://one.example.org"}, "two": {"url": "https://two.example.org"}}}
    for name in ("statusline_asset_report", "hook_asset_report", "shell_aliases_report", "instructions_report"):
        monkeypatch.setattr(reconcile, name, lambda *args: ([], lambda update: []))
    publish = reconcile.publish_config
    edited = []
    def edit_after_publication(*args, **kwargs):
        payload = publish(*args, **kwargs)
        if not edited:
            current = path.read_text()
            external = (current + '\n[mcp_servers.two]\nurl = "https://external.example.org"\n'
                        if cli == "codex" else json.dumps({**json.loads(current), "mcpServers": {
                            **json.loads(current)["mcpServers"], "two": {"httpUrl": "https://external.example.org"}}}))
            path.write_text(external)
            edited.append(path.read_bytes())
        return payload
    monkeypatch.setattr(reconcile, "publish_config", edit_after_publication)
    with pytest.raises(reconcile.ReconcileReadError, match="re-plan"):
        reconcile.reconcile(cli, caps, True, False)
    assert path.read_bytes() == edited[0]


@pytest.mark.parametrize("cli", ["codex", "claude", "agy"])
def test_baseline_defaults_sequence_rejects_external_edit_after_own_write(tmp_path, monkeypatch, cli):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = {"codex": reconcile.codex_config_path, "claude": reconcile.claude_settings_path,
            "agy": reconcile.agy_settings_path}[cli]()
    caps = {"settings": {"approval_policy": "never", "trusted_paths": ["/project"],
                         "claude": {"theme": "managed"},
                         "defaults": {cli: {"model": "wanted"}}}}
    _, apply = reconcile.settings_report(cli, caps)
    publish = reconcile.publish_config
    edited = []
    def edit_after_publication(*args, **kwargs):
        payload = publish(*args, **kwargs)
        if not edited:
            path.write_text(path.read_text() + '\nexternal = true\n' if cli == "codex" else
                            json.dumps({**json.loads(path.read_text()), "external": True}))
            edited.append(path.read_bytes())
        return payload
    monkeypatch.setattr(reconcile, "publish_config", edit_after_publication)
    with pytest.raises(reconcile.ReconcileReadError, match="re-plan"):
        apply(True)
    assert path.read_bytes() == edited[0]


def test_claude_native_mcp_reclassifies_before_a_second_add(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    path = reconcile.claude_mcp_path()
    path.write_text('{}')
    caps = {"_dir": ROOT, "mcp_servers": {
        "one": {"url": "https://one.example.org"}, "two": {"url": "https://two.example.org"}}}
    for name in ("statusline_asset_report", "hook_asset_report", "shell_aliases_report", "instructions_report"):
        monkeypatch.setattr(reconcile, name, lambda *args: ([], lambda update: []))
    added = []
    def native(command):
        if command[2] == "add":
            name = command[-2]
            added.append(name)
            path.write_text(json.dumps({"mcpServers": {
                "one": {"url": "https://one.example.org"},
                "two": {"url": "https://external.example.org"}}}))
        return subprocess.CompletedProcess(command, 0, "", "")
    monkeypatch.setattr(reconcile, "run", native)
    reconcile.reconcile("claude", caps, True, False)
    assert added == ["one"]
    assert json.loads(path.read_text())["mcpServers"]["two"]["url"] == "https://external.example.org"


def test_claude_native_mcp_refuses_symlinks_before_invocation(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    target = tmp_path / "target.json"
    target.write_text('{}')
    reconcile.claude_mcp_path().symlink_to(target)
    def unexpected(command):
        pytest.fail("native invocation must not occur for a symlink target")
    monkeypatch.setattr(reconcile, "run", unexpected)
    with pytest.raises(reconcile.ReconcileReadError, match="symlink"):
        reconcile.apply_mcp("claude", "docs", {"url": "https://example.org"})
    assert target.read_text() == '{}'


def test_claude_mcp_location_change_is_rejected_before_native_invocation(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    modern = reconcile.claude_mcp_path()
    modern.write_text('{}')
    legacy = reconcile.resolve_native_roots().claude / ".config.json"
    caps = {"_dir": ROOT, "mcp_servers": {"docs": {"url": "https://example.org"}}}
    monkeypatch.setattr(reconcile, "claude_mcp_listed", lambda: {})
    for name in ("statusline_asset_report", "hook_asset_report", "shell_aliases_report"):
        monkeypatch.setattr(reconcile, name, lambda *args: ([], lambda update: []))
    def move_effective_location(*args):
        legacy.parent.mkdir(parents=True)
        legacy.write_text('{}')
        return [], lambda update: []
    monkeypatch.setattr(reconcile, "instructions_report", move_effective_location)
    invoked = []
    def native(command):
        invoked.append(command)
        legacy.write_text('{"native":"wrote"}')
        return subprocess.CompletedProcess(command, 0, "", "")
    monkeypatch.setattr(reconcile, "run", native)
    with pytest.raises(reconcile.ReconcileReadError, match="location changed.*re-plan"):
        reconcile.reconcile("claude", caps, True, False)
    assert invoked == []
    assert modern.read_text() == '{}'
    assert legacy.read_text() == '{}'
