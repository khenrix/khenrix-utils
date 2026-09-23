"""Current Council model/effort profile and measured model provenance."""
from __future__ import annotations

import json
import pathlib
import sys
from types import SimpleNamespace


ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "skills" / "llm-council" / "scripts"))
import fanout as council  # noqa: E402


def test_council_profiles_are_explicit_and_separate():
    assert council.MODES["normal"]["claude"] == {
        "model": "claude-opus-5-5", "thinking": "max"}
    assert council.MODES["normal"]["codex"] == {
        "model": "gpt-6-sol", "thinking": "xhigh"}
    assert council.MODES["deep"]["claude"] == {
        "model": "claude-opus-5-5", "thinking": "ultracode"}
    assert council.MODES["deep"]["codex"] == {
        "model": "gpt-6-sol", "thinking": "ultra"}
    assert council.MODES["normal"]["agy"] == council.MODES["deep"]["agy"]


def test_claude_result_records_requested_and_observed_model(tmp_path, monkeypatch):
    payload = json.dumps({"type": "result", "subtype": "success", "result": "ok",
                          "modelUsage": {"claude-opus-5-5": {"inputTokens": 2}}})
    monkeypatch.setattr(council, "run_member", lambda *_args, **_kwargs:
                        SimpleNamespace(stdout=payload, stderr="", returncode=0,
                                        slot_wait_sec=0))
    spec = council.ProviderSpec(
        "claude", ["claude", "--model", "claude-opus-5-5"], None,
        council.extract_claude_json, model="claude-opus-5-5", min_chars=0)
    record = council.run_provider(spec, retries=0, timeout=5, backoff=0,
                                  workdir=tmp_path)
    assert record["valid"]
    assert record["requested_model"] == "claude-opus-5-5"
    assert record["observed_models"] == ["claude-opus-5-5"]


def test_claude_result_without_model_usage_is_unverified(tmp_path, monkeypatch):
    payload = json.dumps({"type": "result", "subtype": "success", "result": "ok"})
    monkeypatch.setattr(council, "run_member", lambda *_args, **_kwargs:
                        SimpleNamespace(stdout=payload, stderr="", returncode=0,
                                        slot_wait_sec=0))
    spec = council.ProviderSpec(
        "claude", ["claude", "--model", "claude-opus-5-5"], None,
        council.extract_claude_json, model="claude-opus-5-5", min_chars=0)
    record = council.run_provider(spec, retries=0, timeout=5, backoff=0,
                                  workdir=tmp_path)
    assert record["valid"]
    assert record["observed_models"] == []


def test_fallback_requires_a_model_specific_failure():
    assert council.fallback_target(
        "claude", "claude-opus-5-5", "claude_error",
        "Model claude-opus-5-5 is not available", structured=True) == "claude-opus-5"
    assert council.fallback_target(
        "claude", "claude-opus-5-5", "auth_or_quota",
        "Authentication failed", structured=True) is None
    assert council.fallback_target(
        "claude", "claude-fable-5-1", "auth_or_quota",
        "Out of usage credits for this model", structured=True) == "claude-opus-5-5"
    assert council.fallback_target(
        "codex", "gpt-6-sol", "auth_or_quota",
        "Quota for this model", structured=True) is None


def test_header_discloses_a_reported_model_mismatch():
    manifest = {"summary": {"valid": 1, "requested": 1}, "providers": [{
        "name": "claude", "valid": True, "model": "claude-opus-5-5",
        "observed_models": ["claude-opus-5"], "model_match": False}]}
    header = council.council_header(manifest)
    assert "model mismatch" in header.lower()
    assert "claude-opus-5-5" in header
    assert "claude-opus-5" in header
