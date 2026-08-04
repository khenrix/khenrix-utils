"""Seam tests for the caller-parameterized process-global behaviour (spec §17)."""
import ast
import inspect
import json
import re
import signal
import subprocess
import sys
import tempfile
import types
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


def _engine():
    """The council engine, imported the way production does.

    `import_fanout()` is what puts `shared/lib` on `sys.path` — `fanout.py` inserts it at
    import time and nothing else in this file does — so a plain `from council import engine`
    here would work or not depending on which test ran first.
    """
    import_fanout()
    from council import engine  # noqa: PLC0415
    return engine


def test_the_depth_guard_composes_with_a_scrubbed_environment():
    """`child_env(base)` must apply the council's guard ON TOP of what it is given. Reading
    `os.environ` regardless would hand a forge seat back every name `fleet.forge_child_env`
    removed — `gitcmd.HOSTILE_ENV`, an ambient absolute `GIT_DIR` — while looking correct."""
    eng = _engine()
    out = eng.child_env({"LLM_FORGE_DEPTH": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})
    assert out["LLM_COUNCIL_DEPTH"] == "1"
    assert out["LLM_FORGE_DEPTH"] == "1" and out["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert "PATH" not in out, \
        "a scrubbed base is the WHOLE environment, not a patch applied over os.environ"


def test_the_default_is_byte_identical_to_the_previous_behaviour(monkeypatch):
    """The change is additive: `child_env()` with no base must still be os.environ plus the
    guard, or every existing council run has quietly moved."""
    eng = _engine()
    monkeypatch.setenv("LLM_COUNCIL_DEPTH", "2")
    monkeypatch.setenv("A_MARKER", "kept")
    out = eng.child_env()
    assert out["LLM_COUNCIL_DEPTH"] == "3" and out["A_MARKER"] == "kept"


def test_run_provider_hands_its_callers_environment_to_the_child(monkeypatch):
    """The seam `forge.launch` exists for: an `env=` that never reached `run_member` would
    leave every real seat running under `os.environ` while the record read identically.

    A signature check alone cannot see that — `env` could be accepted and dropped, which is
    the exact defect one module over — so this watches what the child was actually given.
    """
    eng = _engine()
    seen = {}

    def fake_run_member(argv, stdin=None, timeout=None, env=None, cwd=None):
        seen["env"] = env
        raise FileNotFoundError("no binary is launched by this test")

    monkeypatch.setattr(eng, "run_member", fake_run_member)
    with tempfile.TemporaryDirectory() as td:
        spec = eng.ProviderSpec("claude", ["true"], None, eng.extract_raw, min_chars=0)
        eng.run_provider(spec, 0, 5, 0.0, Path(td),
                         env={"LLM_FORGE_DEPTH": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})
    assert seen["env"]["LLM_FORGE_DEPTH"] == "1" and seen["env"]["LLM_COUNCIL_DEPTH"] == "1"
    assert "PATH" not in seen["env"], "the scrubbed base reached the child, not os.environ"


def test_run_council_hands_its_callers_environment_to_every_member(monkeypatch):
    """THE SEAM ONE CONTAINER OUT. `run_provider` already took an `env`; `run_council` did
    not, so a caller convening a PANEL could not give it one and every member ran under
    `os.environ` regardless. `forge.review.run_round` is the caller that needs it — its three
    reviewers are write-capable and share the checkout they are reviewing, and the environment
    is what stops them writing bytecode into it.

    Watches what each child was actually given rather than the signature: `env` accepted and
    dropped is the defect this whole family of tests exists for, and a council record reads
    identically either way.
    """
    eng = _engine()
    seen = []

    def fake_run_member(argv, stdin=None, timeout=None, env=None, cwd=None):
        seen.append(env)
        raise FileNotFoundError("no binary is launched by this test")

    monkeypatch.setattr(eng, "run_member", fake_run_member)
    with tempfile.TemporaryDirectory() as td:
        specs = [eng.ProviderSpec(n, ["true"], None, eng.extract_raw, min_chars=0)
                 for n in ("claude", "codex", "agy")]
        eng.run_council(specs, retries=0, timeout=5, backoff=0.0, workdir=Path(td),
                        install_signal_handler=False,
                        env={"PYTHONDONTWRITEBYTECODE": "1"})
    assert len(seen) == 3
    for env in seen:
        assert env["PYTHONDONTWRITEBYTECODE"] == "1"
        assert env["LLM_COUNCIL_DEPTH"] == "1", "the council's own guard still applies on top"
        assert "PATH" not in env, "the caller's environment reached the child, not os.environ"


def test_no_second_timeout_mechanism_was_added_beside_agys_print_timeout(tmp_path):
    """The fixed 120s cap that used to live here killed every agy attempt in a 10-skill eval
    sweep, and the panel read those deaths as `agy_error` rather than `timeout`. The rule that
    replaced it is that ONE expression decides agy's window: `pt = max(5, int(timeout) - 5)`.

    Asserted over the AST rather than the file's text because the comment directly above that
    line is a history of measured durations — 120, 124, 608 — so a regex looking for a numeric
    ceiling in the source cannot tell a remembered number from a live one. `ast.unparse`
    normalizes formatting, so a rewrap is not a failure but a second operand is.
    """
    eng = _engine()
    src = inspect.getsource(eng.build_real_spec)
    fn = ast.parse(src).body[0]
    assigns = [n for n in ast.walk(fn) if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "pt" for t in n.targets)]
    assert len(assigns) == 1, "agy's window must be decided in exactly one place"
    assert ast.unparse(assigns[0].value) == "max(5, int(timeout) - 5)", \
        "a second operand joined the computation of agy's print-timeout"
    assert not [n for n in ast.walk(fn) if isinstance(n, ast.AugAssign)
                and isinstance(n.target, ast.Name) and n.target.id == "pt"], \
        "the window was narrowed after it was computed"
    assert not [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name) and n.func.id == "min"], \
        "a ceiling reappeared in agy's spec builder"
    assert re.search(r'"--print-timeout",\s*f"\{pt\}s"', src), \
        "the flag must carry `pt` itself, not a value narrowed on its way into argv"
    # ...and the value that reaches argv is the engine's, at the longest window there is.
    spec = eng.build_real_spec("agy", "p", eng.MODE_TIMEOUT["forge"], {}, tmp_path)
    assert spec.argv[spec.argv.index("--print-timeout") + 1] == \
        f"{eng.MODE_TIMEOUT['forge'] - 5}s"


def test_agys_structured_timeout_keeps_its_retries_but_its_quota_wall_does_not(tmp_path,
                                                                               monkeypatch):
    """`"timeout" not in STRUCTURED_TERMINAL_REASONS` is a proxy; this is the behaviour it
    stands for. Terminality is SPENT in `run_provider`'s loop, which `evaluate` — the only
    thing the engine's own self-test reaches for this — never runs.

    Both halves are here on purpose: the timeout must cost every attempt and the wall must
    cost one. A change that made both terminal, or neither, still passes either half alone.
    """
    eng = _engine()
    payload, seen = {}, []

    def fake_run_member(argv, stdin=None, timeout=None, env=None, cwd=None):
        seen.append(argv)
        return types.SimpleNamespace(stdout=payload["json"], stderr="", returncode=1)

    monkeypatch.setattr(eng, "run_member", fake_run_member)
    spec = eng.ProviderSpec("agy", ["true"], None, eng.extract_agy_json)

    payload["json"] = json.dumps({"status": "ERROR",
                                  "error": "timeout waiting for response from the model"})
    wd = tmp_path / "timeout"
    wd.mkdir()
    rec = eng.run_provider(spec, 2, 5, 0.0, wd)
    assert (rec["reason"], rec["structured"]) == ("timeout", True)
    assert len(seen) == 3, "a structured timeout is not terminal — it keeps its retries"

    seen.clear()
    payload["json"] = json.dumps({"status": "ERROR",
                                  "error": "timeout waiting for response: RESOURCE_EXHAUSTED, "
                                           "Individual quota reached"})
    wall = tmp_path / "wall"
    wall.mkdir()
    rec = eng.run_provider(spec, 2, 5, 0.0, wall)
    assert rec["reason"] == "auth_or_quota", "a wall reported as a timeout is still a wall"
    assert len(seen) == 1, "and a wall is terminal — it must not spend three attempts on one"
