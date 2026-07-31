"""Characterization of the llm-council engine's observable surface.

These tests pin behaviour that MUST survive the shared-core extraction
(spec 2026-07-30-llm-forge-design.md §17). They are written against the
pre-move fanout.py and must stay green, unmodified, through every task
of Plan A. If one goes red after a refactor, the refactor is wrong.
"""
import json
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FANOUT = ROOT / "shared" / "skills" / "llm-council" / "scripts" / "fanout.py"
STUB = ROOT / "shared" / "skills" / "llm-council" / "tests" / "stub_provider.py"


def import_fanout():
    """Import fanout exactly the way checks.model_crosscheck / eval_harness do."""
    sys.path.insert(0, str(FANOUT.parent))
    try:
        import fanout  # noqa: PLC0415
        return fanout
    finally:
        sys.path.pop(0)


def _stub_spec(fanout, tmp, mode="ok"):
    """A seat backed by tests/stub_provider.py — no network, no auth."""
    return fanout.ProviderSpec(
        "claude", [sys.executable, str(STUB), "--mode", mode, "--as", "raw"],
        None, fanout.extract_raw, min_chars=0)


# --- API surface -----------------------------------------------------------

def test_api_names_and_defaults():
    f = import_fanout()
    for name in ("MODES", "MODE_TIMEOUT", "ProviderSpec", "run_council",
                 "run_member", "build_real_spec", "make_readonly",
                 "isolate_agy_worktree", "install_cleanup_handler",
                 "extract_raw", "extract_usage", "MIN_SUBSTANTIVE_CHARS"):
        assert hasattr(f, name), name
    assert {"normal", "deep"} <= set(f.MODES)
    for m in ("normal", "deep"):
        assert {"claude", "codex", "agy"} <= set(f.MODES[m])
    spec = f.ProviderSpec("claude", ["true"], None, f.extract_raw)
    assert spec.min_chars == f.MIN_SUBSTANTIVE_CHARS
    assert spec.sentinel is None and spec.cwd is None


def test_underscore_state_reachable():
    # --self-test reads _LIVE_WORKTREES directly; a facade that drops
    # underscore names kills the receipt gate (spec §17).
    f = import_fanout()
    assert isinstance(f._LIVE_WORKTREES, set)


# --- monkeypatch-through-globals (the star-import killer) ------------------

def test_monkeypatch_run_member_is_seen_by_run_provider():
    f = import_fanout()
    calls = []
    orig = f.run_member
    f.run_member = lambda *a, **k: (calls.append(1), orig(*a, **k))[1]
    try:
        with tempfile.TemporaryDirectory() as td:
            m = f.run_council([_stub_spec(f, td)], retries=0, timeout=30,
                              backoff=0.05, workdir=Path(td), prompt="hi")
    finally:
        f.run_member = orig
    assert calls, ("patching fanout.run_member was NOT observed by "
                   "run_provider — module identity broke (spec §17)")
    assert m["providers"][0]["valid"] is True


# --- CLI surface -----------------------------------------------------------

def test_cli_help_flag_surface():
    r = subprocess.run([sys.executable, str(FANOUT), "--help"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0
    for flag in ("--prompt-file", "--mode", "--providers", "--out",
                 "--timeout", "--retries", "--allow-writes", "--self-test",
                 "--smoke"):
        assert flag in r.stdout, flag


@pytest.mark.slow
def test_self_test_exits_zero_from_repo_path():
    r = subprocess.run([sys.executable, str(FANOUT), "--self-test"],
                       capture_output=True, text=True, timeout=1200)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]


# --- sentinel is per-spec, engine never injects it (forge seam, spec §13) --

def test_engine_does_not_inject_sentinel_itself():
    f = import_fanout()
    with tempfile.TemporaryDirectory() as td:
        spec = _stub_spec(f, td)
        spec.sentinel = "SENTINEL-feedbeefcafe"   # stub output will not quote it
        spec.min_chars = 0
        m = f.run_council([spec], retries=0, timeout=30, backoff=0.05,
                          workdir=Path(td), prompt="hi")
    p = m["providers"][0]
    assert p["valid"] is False and p["reason"] == "did_not_read_input"
