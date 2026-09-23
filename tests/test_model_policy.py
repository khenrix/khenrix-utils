"""Model registry and price guards for adjacent version IDs."""
from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys
import tomllib
import types


ROOT = pathlib.Path(__file__).resolve().parent.parent


def load_module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


checks = load_module("model_policy_checks", ROOT / "scripts" / "lib" / "checks.py")
tuneup = load_module(
    "model_policy_tuneup", ROOT / "shared" / "skills" / "skill-tuneup" / "scripts" / "tuneup.py")
session_stats = load_module("model_policy_stats", ROOT / "scripts" / "claude_session_stats.py")


def test_price_coverage_requires_exact_registered_model(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "capabilities.toml").write_text(
        '[models]\nclaude = ["claude-opus-5-5"]\n')
    (tmp_path / "scripts" / "pricing.toml").write_text(
        '[claude-opus-5]\ninput = 5.0\noutput = 25.0\n'
        'cache_read = 0.5\ncache_write = 6.25\n')
    assert any("claude-opus-5-5" in issue for issue in checks.pricing_coverage(tmp_path))


def test_crosscheck_includes_portable_defaults(tmp_path, monkeypatch):
    (tmp_path / "capabilities.toml").write_text(
        '[models]\nclaude = ["claude-opus-5-5"]\n'
        'codex = ["gpt-6-sol"]\nagy = ["Gemini 3.8 Flash (High)"]\n'
        '[settings.defaults.claude]\nmodel = "best"\n'
        '[settings.defaults.codex]\nmodel = "unknown-model"\n'
        '[settings.defaults.agy]\nmodel = "Gemini 3.8 Flash (High)"\n')
    monkeypatch.setitem(sys.modules, "fanout", types.SimpleNamespace(MODES={}))
    issues = checks.model_crosscheck(tmp_path)
    assert any("unknown-model" in issue and "codex" in issue for issue in issues)
    assert not any("best" in issue for issue in issues)


def test_tuneup_does_not_call_a_new_minor_version_current_by_prefix():
    assert tuneup.tag_model("claude-opus-5-5", {"claude-opus-5"}) == "stale-candidate"
    assert tuneup.tag_model("claude-opus-5-20260901", {"claude-opus-5"}) == "current"


def test_current_claude_prices_are_exactly_registered():
    with (ROOT / "scripts" / "pricing.toml").open("rb") as f:
        prices = tomllib.load(f)
    assert prices["claude-opus-5-5"] == {
        "input": 4.0, "output": 20.0, "cache_read": 0.2, "cache_write": 5.0}
    assert prices["claude-fable-5-1"] == {
        "input": 10.0, "output": 50.0, "cache_read": 0.25, "cache_write": 12.5}
    assert prices["claude-sonnet-5"] == {
        "input": 2.0, "output": 10.0, "cache_read": 0.2, "cache_write": 2.5}


def test_statusline_labels_claude_cost_as_estimate():
    result = subprocess.run(
        [sys.executable, str(ROOT / "statusline" / "khenrix-statusline"), "claude"],
        input='{"cost":{"total_cost_usd":1.23},"terminal_width":120}',
        text=True, capture_output=True, check=True)
    assert "est $1.23" in result.stdout


def test_session_price_matches_dates_but_not_a_new_model_version():
    prices = {"claude-opus-5": {
        "input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25}}
    event = {"model": "claude-opus-5-5", "input": 1_000_000, "output": 0,
             "cache_read": 0, "cache_creation": 0}
    assert session_stats.price(event, prices) == 0.0
    assert session_stats.price({**event, "model": "claude-opus-5-20260901"}, prices) == 5.0
