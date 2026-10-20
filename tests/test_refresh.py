from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("khenrix_refresh", ROOT / "scripts" / "refresh.py")
assert SPEC and SPEC.loader
refresh = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(refresh)


def test_absent_agy_plugin_is_not_installed_by_metadata_refresh(monkeypatch):
    calls = []
    monkeypatch.setattr(refresh, "installed_dirs", lambda cli: [])
    monkeypatch.setattr(refresh.subprocess, "run", lambda *a, **k: calls.append((a, k)))

    assert refresh.meta_refresh("agy") is None
    assert calls == []


def test_existing_agy_plugin_remains_refreshable(monkeypatch, tmp_path):
    calls = []

    class Result:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(refresh, "installed_dirs", lambda cli: [tmp_path / "installed"])
    monkeypatch.setattr(
        refresh.subprocess,
        "run",
        lambda *a, **k: calls.append((a, k)) or Result(),
    )

    note = refresh.meta_refresh("agy")

    assert note and "ok" in note
    argv = calls[0][0][0]
    assert argv[:3] == ["agy", "plugin", "install"]


def test_sync_removes_obsolete_native_only_copies_from_plugin(monkeypatch, tmp_path):
    src = tmp_path / "source"
    dest = tmp_path / "installed"
    (src / "skills" / "other").mkdir(parents=True)
    (src / "skills" / "other" / "SKILL.md").write_text("other\n")
    for name in refresh.NATIVE_ONLY_SKILLS:
        stale = dest / "skills" / name
        stale.mkdir(parents=True)
        (stale / "SKILL.md").write_text("stale\n")

    monkeypatch.setattr(refresh, "ROOT", tmp_path)
    plugin_source = tmp_path / "marketplaces" / "agy" / "plugins" / "khenrix-utils"
    plugin_source.parent.mkdir(parents=True)
    src.rename(plugin_source)
    monkeypatch.setattr(refresh, "installed_dirs", lambda cli: [dest])

    refresh.sync("agy")

    assert (dest / "skills" / "other" / "SKILL.md").is_file()
    assert all(not (dest / "skills" / name).exists()
               for name in refresh.NATIVE_ONLY_SKILLS)


@pytest.mark.parametrize("cli", ["claude", "codex"])
def test_cached_refresh_never_rediscovers_or_changes_its_recovery_backup(monkeypatch, tmp_path, cli):
    repo = tmp_path / "repo"
    repo.mkdir()
    shutil.copy2(ROOT / "capabilities.toml", repo / "capabilities.toml")
    source = repo / "marketplaces" / cli / "plugins/khenrix-utils"
    (source / "skills/ordinary").mkdir(parents=True)
    (source / "skills/ordinary/SKILL.md").write_text("ordinary\n")
    home = tmp_path / "home"
    versions = home / f".{cli}/plugins/cache/khenrix-{cli}-marketplace/khenrix-utils"
    destination = versions / "0.1.0"
    historical = destination / "lib/forge"
    historical.mkdir(parents=True)
    (historical / "sentinel").write_bytes(b"recover these exact bytes\n")
    hidden_version = versions / ".user-owned-version"
    hidden_version.mkdir()
    monkeypatch.setattr(refresh, "ROOT", repo)
    monkeypatch.setattr(refresh, "HOME", home)

    first_notes = refresh.sync(cli)
    backups = list(home.rglob(".khenrix-retired-backup-*"))
    assert len(backups) == 1
    backup = backups[0]
    before = {p.relative_to(backup): (p.read_bytes(), p.stat().st_mode)
              for p in backup.rglob("*") if p.is_file()}
    assert before[Path("lib/forge/sentinel")][0] == b"recover these exact bytes\n"
    assert any(str(backup) in note for note in first_notes)
    assert set(refresh.installed_dirs(cli)) == {destination, hidden_version}

    refresh.sync(cli)

    assert list(home.rglob(".khenrix-retired-backup-*")) == [backup]
    assert {p.relative_to(backup): (p.read_bytes(), p.stat().st_mode)
            for p in backup.rglob("*") if p.is_file()} == before
    assert not (backup / "skills/ordinary").exists()
    assert (hidden_version / "skills/ordinary/SKILL.md").is_file()
