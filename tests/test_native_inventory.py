"""Portable inventory observes the same native roots as launch policy."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/lib"))
SPEC = importlib.util.spec_from_file_location("native_inventory", ROOT / "scripts/lib/inventory.py")
inventory = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inventory)


@pytest.mark.parametrize("cli,variable,filename,contents", [
    ("claude", "CLAUDE_CONFIG_DIR", "settings.json", json.dumps({"model": "fixture-claude"})),
    ("codex", "CODEX_HOME", "config.toml", 'model = "fixture-codex"\n'),
])
def test_inventory_model_settings_read_overridden_native_roots(tmp_path, monkeypatch, cli, variable, filename, contents):
    monkeypatch.setenv("HOME", str(tmp_path / "unused-home"))
    config = tmp_path / "effective-root"
    config.mkdir()
    (config / filename).write_text(contents)
    monkeypatch.setenv(variable, str(config))
    assert inventory.model_settings(cli)["model"] == f"fixture-{cli}"
