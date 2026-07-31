"""Seam tests for the caller-parameterized process-global behaviour (spec §17)."""
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from test_council_characterization import import_fanout, _stub_spec


def test_handler_state_is_a_container():
    f = import_fanout()
    assert not hasattr(f, "_HANDLER_FIRED"), "rebindable bool must be gone"
    assert f._STATE["handler_fired"] is False


def test_install_cleanup_handler_respects_foreign_handler():
    f = import_fanout()
    prev = signal.getsignal(signal.SIGTERM)
    marker = lambda s, fr: None
    signal.signal(signal.SIGTERM, marker)
    try:
        installed = f.install_cleanup_handler()
        assert installed is False
        assert signal.getsignal(signal.SIGTERM) is marker
        assert f.install_cleanup_handler(force=True) is True
        assert signal.getsignal(signal.SIGTERM) is not marker
    finally:
        signal.signal(signal.SIGTERM, prev)


def test_run_council_can_skip_handler_install():
    f = import_fanout()
    prev = signal.getsignal(signal.SIGTERM)
    marker = lambda s, fr: None
    signal.signal(signal.SIGTERM, marker)
    try:
        with tempfile.TemporaryDirectory() as td:
            f.run_council([_stub_spec(f, td)], retries=0, timeout=30,
                          backoff=0.05, workdir=Path(td), prompt="hi",
                          install_signal_handler=False)
        assert signal.getsignal(signal.SIGTERM) is marker
    finally:
        signal.signal(signal.SIGTERM, prev)


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, timeout=30)


def test_isolate_worktree_branch_and_no_prune(tmp_path):
    f = import_fanout()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "--allow-empty", "-q", "-m", "seed")
    spec = f.ProviderSpec("agy", ["true"], None, f.extract_raw)
    handle = f.isolate_agy_worktree(spec, tmp_path / "wd", repo_dir=str(repo),
                                    prune=False, branch="forge/test/seat")
    assert handle is not None
    r = _git(repo, "branch", "--list", "forge/test/seat")
    assert "forge/test/seat" in r.stdout
    f.remove_agy_worktree(handle)


def test_isolate_worktree_register_false_skips_live_set(tmp_path):
    f = import_fanout()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "--allow-empty", "-q", "-m", "seed")
    spec = f.ProviderSpec("agy", ["true"], None, f.extract_raw)
    handle = f.isolate_agy_worktree(spec, tmp_path / "wd", repo_dir=str(repo),
                                    register=False, prune=False, branch="forge/test/keep")
    assert handle is not None
    assert handle not in f._LIVE_WORKTREES
    f.remove_agy_worktree(handle)   # must not raise on an unregistered handle
    assert handle not in f._LIVE_WORKTREES


def test_injectable_validator_replaces_evaluate():
    f = import_fanout()
    with tempfile.TemporaryDirectory() as td:
        spec = _stub_spec(f, td)
        spec.min_chars = f.MIN_SUBSTANTIVE_CHARS  # would normally fail short output
        spec.validator = lambda rc, out, err, s: (True, "ok", "forced", False)
        m = f.run_council([spec], retries=0, timeout=30, backoff=0.05,
                          workdir=Path(td), prompt="hi")
    assert m["providers"][0]["valid"] is True
    assert m["providers"][0]["reason"] == "ok"
