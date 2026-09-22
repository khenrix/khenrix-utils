from __future__ import annotations

import importlib.util
from pathlib import Path


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
