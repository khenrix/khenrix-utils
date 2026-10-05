"""Forge quality fixtures are runnable, sealed, and blind to executor seats."""
from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import pwd
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "evals" / "llm-fanout" / "forge-fixtures"
CASEBOOK = ROOT / "evals" / "llm-fanout" / "quality-cases.json"


@pytest.fixture(scope="module")
def fixtures():
    spec = importlib.util.spec_from_file_location(
        "fanout_forge_fixtures", ROOT / "scripts" / "fanout_forge_fixtures.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fixture_probe_argv(fixtures, case_id: str, workspace: Path,
                        argv: list[str], root: Path = FIXTURES) -> list[str]:
    fixtures.verify_pin(CASEBOOK, root)
    policy = fixtures._validate_boundary(case_id, workspace, root)
    return [str(fixtures._SANDBOX_EXEC), "-p", policy, *argv]


def test_quality_archive_derivative_reopens_real_loaded_modules(tmp_path, fixtures, monkeypatch):
    archive = Path(os.environ["FANOUT_QUALITY_ARCHIVE_ROOT"])
    checks = Path(os.environ["FANOUT_QUALITY_ORIGINAL_CHECKS"])
    monkeypatch.delitem(sys.modules, "khenrix_checks", raising=False)
    files = {name: hashlib.sha256(data).hexdigest()
             for name, data in fixtures._archive_files(archive).items()}
    files["scripts/lib/checks.py"] = fixtures._ORIGINAL_CHECKS_SHA256
    stage = tmp_path / "derivative"
    receipt = fixtures.stage_quality_archive(archive, stage, files, {}, original_checks=checks)
    assert len(receipt["original"]) == 61
    assert receipt["original"] == receipt["projected"]
    modules, loaded = fixtures.load_quality_archive(stage, receipt,
                                                    ("forge.fix", "forge.verify"))
    assert modules["forge.fix"].apply.__module__.endswith("forge.fix")
    assert "shared/lib/forge/fix.py" in loaded
    assert loaded["shared/lib/forge/fix.py"] == files["shared/lib/forge/fix.py"]
    assert loaded["scripts/lib/checks.py"] == files["scripts/lib/checks.py"]
    monkeypatch.delitem(sys.modules, "khenrix_checks")
    (stage / "shared/lib/forge/fix.py").write_bytes(b"raise RuntimeError('substituted')\n")
    with pytest.raises(fixtures.FixtureError, match="archive"):
        fixtures.reopen_quality_archive(stage, receipt)
    with pytest.raises(fixtures.FixtureError, match="projection"):
        fixtures.stage_quality_archive(archive, tmp_path / "bad", files,
                                       {"shared/lib/forge/fix.py": b"pass\n"}, original_checks=checks)


def test_quality_original_checks_fifo_refuses_before_archive_or_receipt(tmp_path, fixtures):
    fifo = tmp_path / "checks.py"
    os.mkfifo(fifo, 0o600)
    child = (
        "import importlib.util,sys\n"
        "spec=importlib.util.spec_from_file_location('quality_fixture',sys.argv[1])\n"
        "module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)\n"
        "try: module._original_checks_bytes(sys.argv[2])\n"
        "except module.FixtureError: print('refused')\n"
        "else: raise AssertionError('FIFO accepted')\n"
    )
    result = subprocess.run((sys.executable, "-B", "-c", child, fixtures.__file__, str(fifo)),
                            capture_output=True, text=True, timeout=2, check=True)
    assert result.stdout == "refused\n"
    assert result.stderr == ""
    assert sorted(path.name for path in tmp_path.iterdir()) == ["checks.py"]


def test_quality_archive_executes_verified_source_not_untracked_bytecode(tmp_path, fixtures, monkeypatch):
    import importlib.machinery
    import importlib._bootstrap_external
    archive = Path(os.environ["FANOUT_QUALITY_ARCHIVE_ROOT"])
    checks = Path(os.environ["FANOUT_QUALITY_ORIGINAL_CHECKS"])
    monkeypatch.delitem(sys.modules, "khenrix_checks", raising=False)
    files = {name: hashlib.sha256(data).hexdigest()
             for name, data in fixtures._archive_files(archive).items()}
    files["scripts/lib/checks.py"] = fixtures._ORIGINAL_CHECKS_SHA256
    stage = tmp_path / "archive"
    receipt = fixtures.stage_quality_archive(archive, stage, files, {}, original_checks=checks)
    for relative in ("scripts/lib/checks.py", "shared/lib/council/engine.py", "shared/lib/forge/fix.py"):
        path = stage / relative
        original = path.read_bytes()
        loader = importlib.machinery.SourceFileLoader("untracked_cache", str(path))
        info = path.stat()
        code = compile(original + b"\nQUALITY_CACHE_MARKER = 'untracked'\n", str(path), "exec")
        bytecode = importlib._bootstrap_external._code_to_timestamp_pyc(code,
            int(info.st_mtime), info.st_size)
        loader.set_data(importlib.util.cache_from_source(str(path)), bytecode)
        assert path.read_bytes() == original
    modules, loaded = fixtures.load_quality_archive(stage, receipt, ("council.engine", "forge.fix"))
    assert not hasattr(sys.modules["khenrix_checks"], "QUALITY_CACHE_MARKER")
    assert not hasattr(modules["council.engine"], "QUALITY_CACHE_MARKER")
    assert not hasattr(modules["forge.fix"], "QUALITY_CACHE_MARKER")
    assert callable(modules["council.engine"].extract_agy_json)
    assert loaded["scripts/lib/checks.py"] == fixtures._ORIGINAL_CHECKS_SHA256
    assert loaded["shared/lib/council/engine.py"] == receipt["projected"]["shared/lib/council/engine.py"]
    monkeypatch.delitem(sys.modules, "khenrix_checks")


def test_quality_original_checks_refuses_absent_tampered_and_foreign_cache(tmp_path, fixtures, monkeypatch):
    from types import ModuleType
    archive = Path(os.environ["FANOUT_QUALITY_ARCHIVE_ROOT"])
    checks = Path(os.environ["FANOUT_QUALITY_ORIGINAL_CHECKS"])
    files = {name: hashlib.sha256(data).hexdigest() for name, data in fixtures._archive_files(archive).items()}
    files["scripts/lib/checks.py"] = fixtures._ORIGINAL_CHECKS_SHA256
    with pytest.raises(fixtures.FixtureError, match="checks dependency"):
        fixtures.stage_quality_archive(archive, tmp_path / "absent", files, {},
                                       original_checks=tmp_path / "missing")
    wrong = tmp_path / "wrong.py"
    wrong.write_bytes(checks.read_bytes() + b"\n")
    with pytest.raises(fixtures.FixtureError, match="checks dependency"):
        fixtures.stage_quality_archive(archive, tmp_path / "tampered", files, {}, original_checks=wrong)
    stage = tmp_path / "sealed"
    receipt = fixtures.stage_quality_archive(archive, stage, files, {}, original_checks=checks)
    foreign = ModuleType("khenrix_checks")
    foreign.__file__ = str(checks)
    monkeypatch.setitem(sys.modules, "khenrix_checks", foreign)
    with pytest.raises(fixtures.FixtureError, match="foreign loaded"):
        fixtures.load_quality_archive(stage, receipt, ("forge.baseline",))
    monkeypatch.delitem(sys.modules, "khenrix_checks")
    (stage / "scripts/lib/checks.py").unlink()
    with pytest.raises(fixtures.FixtureError, match="archive"):
        fixtures.reopen_quality_archive(stage, receipt)


def test_quality_fixed_projection_loads_actual_launchers_only(tmp_path, fixtures, monkeypatch):
    archive = Path(os.environ["FANOUT_QUALITY_ARCHIVE_ROOT"])
    checks = Path(os.environ["FANOUT_QUALITY_ORIGINAL_CHECKS"])
    original = fixtures._archive_files(archive)
    projection = fixtures.quality_archive_projection(original)
    assert set(projection) == fixtures._QUALITY_PROJECTION
    files = {name: hashlib.sha256(data).hexdigest() for name, data in original.items()}
    files["scripts/lib/checks.py"] = fixtures._ORIGINAL_CHECKS_SHA256
    monkeypatch.delitem(sys.modules, "khenrix_checks", raising=False)
    receipt = fixtures.stage_quality_archive(archive, tmp_path / "projected", files,
                                             projection, original_checks=checks)
    modules, loaded = fixtures.load_quality_archive(tmp_path / "projected", receipt,
        ("council.engine", "forge.cli", "forge.deepreview", "forge.review"))
    engine, cli = modules["council.engine"], modules["forge.cli"]
    assert cli._model_cfg("claude-opus-5-5", "gemini-3.8-flash-high")["codex"]["model"] == "gpt-6.1-sol"
    assert engine.extract_agy_json.__code__.co_filename == str(tmp_path / "projected/shared/lib/council/engine.py")
    with pytest.raises(RuntimeError, match="protected quality transport"):
        engine.build_real_spec("claude", "original prompt", 900, {}, tmp_path)
    for name in files.keys() - fixtures._QUALITY_PROJECTION:
        assert receipt["projected"][name] == files[name]
    assert loaded["shared/lib/forge/cli.py"] == receipt["projected"]["shared/lib/forge/cli.py"]
    monkeypatch.delitem(sys.modules, "khenrix_checks")
    altered = {**projection, "shared/lib/council/engine.py": projection["shared/lib/council/engine.py"] + b"\n# foreign\n"}
    with pytest.raises(fixtures.FixtureError, match="fixed projection"):
        fixtures.stage_quality_archive(archive, tmp_path / "foreign", files, altered, original_checks=checks)


def test_campaign_boundary_seals_exact_roots_and_rejects_disk_tampering(
    tmp_path: Path, fixtures, monkeypatch,
) -> None:
    """Changing a denied root after campaign creation must fail before a seat runs."""
    home = tmp_path / "home"
    source = tmp_path / "source"
    baseline = tmp_path / "baseline"
    consumer = tmp_path / "consumer"
    for path in (home, source, baseline, consumer):
        path.mkdir()
    monkeypatch.setattr(fixtures, "_account_home", lambda: home)
    monkeypatch.setattr(fixtures, "_protected_roots", lambda _root: (source,))
    run_root = tmp_path / "campaign"
    boundary = fixtures.open_blindness_boundary(
        run_root, baseline, consumer, resume=False,
    )
    assert boundary.run_root == run_root.resolve()
    assert boundary.baseline_root == baseline.resolve()
    assert boundary.consumer_root == consumer.resolve()
    assert source.resolve() in boundary.protected_roots
    assert home.resolve() in boundary.protected_roots
    assert len(boundary.policy_sha256) == 64
    assert fixtures.open_blindness_boundary(
        run_root, baseline, consumer, resume=True,
    ) == boundary
    with pytest.raises(fixtures.FixtureError, match="boundary|root"):
        fixtures.open_blindness_boundary(
            run_root, baseline, source, resume=True,
        )
    seal_file = run_root / ".forge-boundary" / "descriptor.json"
    sealed = json.loads(seal_file.read_text(encoding="utf-8"))
    sealed["baseline_root"] = str(source)
    seal_file.write_text(json.dumps(sealed), encoding="utf-8")
    os.chmod(seal_file, 0o600)
    with pytest.raises(fixtures.FixtureError, match="seal|boundary"):
        fixtures.open_blindness_boundary(
            run_root, baseline, consumer, resume=True,
        )


def test_root_discovery_and_seed_git_ignore_attacker_path_and_inherited_tokens(
    tmp_path: Path, fixtures, monkeypatch,
) -> None:
    marker = tmp_path / "attacker-git-ran"
    attacker = tmp_path / "git"
    attacker.write_text(
        f"#!/bin/sh\n/usr/bin/touch '{marker}'\nexit 79\n", encoding="utf-8",
    )
    attacker.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    monkeypatch.setenv("DUMMY_API_TOKEN", "must-not-reach-git")
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "spoofed-git-dir"))
    calls = []
    original_run = fixtures.subprocess.run

    def recorded_run(argv, **kwargs):
        calls.append((tuple(argv), kwargs.get("env")))
        return original_run(argv, **kwargs)

    monkeypatch.setattr(fixtures.subprocess, "run", recorded_run)
    roots = fixtures._protected_roots(FIXTURES)
    assert ROOT.resolve() in roots
    workspace = tmp_path / "seed"
    workspace.mkdir()
    fixtures._git(workspace, "init", "-q")
    assert (workspace / ".git").is_dir()
    assert not marker.exists()
    assert len(calls) == 3
    for argv, env in calls:
        assert argv[0] == "/usr/bin/git"
        assert env is not None
        assert env["PATH"] == "/usr/bin:/bin"
        assert "DUMMY_API_TOKEN" not in env
        assert "GIT_DIR" not in env


@pytest.mark.parametrize("probe_name", ("_native_campaign_probe", "_sandbox_probe"))
def test_sandbox_preflight_git_probe_does_not_inherit_parent_secrets(
    tmp_path: Path, fixtures, monkeypatch, probe_name: str,
) -> None:
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    monkeypatch.setenv("DUMMY_API_TOKEN", "must-not-reach-probe")
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "spoofed-git-dir"))
    calls = []

    def recorded_run(argv, **kwargs):
        calls.append((tuple(argv), kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(fixtures.subprocess, "run", recorded_run)
    assert getattr(fixtures, probe_name)("(version 1) (allow default)", "pass") == 0
    assert len(calls) == 1
    environment = calls[0][1].get("env")
    assert environment is not None
    assert environment["PATH"] == "/usr/bin:/bin"
    assert "DUMMY_API_TOKEN" not in environment
    assert "GIT_DIR" not in environment


def test_campaign_boundary_rejects_a_symlinked_requested_root(
    tmp_path: Path, fixtures, monkeypatch,
) -> None:
    home = tmp_path / "home"
    source = tmp_path / "source"
    baseline = tmp_path / "baseline"
    consumer = tmp_path / "consumer"
    for path in (home, source, baseline, consumer):
        path.mkdir()
    alias = tmp_path / "baseline-alias"
    alias.symlink_to(baseline, target_is_directory=True)
    monkeypatch.setattr(fixtures, "_account_home", lambda: home)
    monkeypatch.setattr(fixtures, "_protected_roots", lambda _root: (source,))
    with pytest.raises(fixtures.FixtureError, match="baseline root"):
        fixtures.open_blindness_boundary(
            tmp_path / "campaign", alias, consumer, resume=False,
        )
    assert not (tmp_path / "campaign").exists()


def test_campaign_boundary_requires_a_consumer_root_before_creation(
    tmp_path: Path, fixtures, monkeypatch,
) -> None:
    home = tmp_path / "home"
    source = tmp_path / "source"
    baseline = tmp_path / "baseline"
    for path in (home, source, baseline):
        path.mkdir()
    monkeypatch.setattr(fixtures, "_account_home", lambda: home)
    monkeypatch.setattr(fixtures, "_protected_roots", lambda _root: (source,))
    with pytest.raises(fixtures.FixtureError, match="consumer root is required"):
        fixtures.open_blindness_boundary(
            tmp_path / "campaign", baseline, None, resume=False,
        )
    assert not (tmp_path / "campaign").exists()


def test_boundary_rejects_redirected_home_before_creation(
    tmp_path: Path, fixtures, monkeypatch,
) -> None:
    account_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve(strict=True)
    redirected = tmp_path / "redirected-home"
    redirected.mkdir()
    source = tmp_path / "source"
    baseline = tmp_path / "baseline"
    consumer = tmp_path / "consumer"
    for path in (source, baseline, consumer):
        path.mkdir()
    monkeypatch.setattr(fixtures, "_protected_roots", lambda _root: (source,))
    monkeypatch.setenv("HOME", str(redirected))
    with pytest.raises(fixtures.FixtureError, match="HOME|account home"):
        fixtures.open_blindness_boundary(
            tmp_path / "campaign", baseline, consumer, resume=False,
        )
    assert not (tmp_path / "campaign").exists()
    monkeypatch.delenv("HOME")
    assert fixtures._account_home() == account_home
    boundary = fixtures.open_blindness_boundary(
        tmp_path / "campaign", baseline, consumer, resume=False,
    )
    monkeypatch.setenv("HOME", str(redirected))
    with pytest.raises(fixtures.FixtureError, match="HOME|account home"):
        fixtures.require_blindness_attestation(boundary=boundary)


def test_boundary_rejects_relocated_memory_before_creation(
    tmp_path: Path, fixtures, monkeypatch,
) -> None:
    home = tmp_path / "home"
    source = tmp_path / "source"
    baseline = tmp_path / "baseline"
    consumer = tmp_path / "consumer"
    for path in (home, source, baseline, consumer):
        path.mkdir()
    monkeypatch.setattr(fixtures, "_account_home", lambda: home)
    monkeypatch.setattr(fixtures, "_protected_roots", lambda _root: (source,))
    monkeypatch.setenv("AGENTIC_MEMORY_HOME", str(tmp_path / "relocated-memory"))
    with pytest.raises(fixtures.FixtureError, match="AGENTIC_MEMORY_HOME|memory"):
        fixtures.open_blindness_boundary(
            tmp_path / "campaign", baseline, consumer, resume=False,
        )
    assert not (tmp_path / "campaign").exists()
    monkeypatch.delenv("AGENTIC_MEMORY_HOME")
    boundary = fixtures.open_blindness_boundary(
        tmp_path / "campaign", baseline, consumer, resume=False,
    )
    monkeypatch.setenv("AGENTIC_MEMORY_HOME", "")
    with pytest.raises(fixtures.FixtureError, match="AGENTIC_MEMORY_HOME|memory"):
        fixtures.require_blindness_attestation(boundary=boundary)


def test_boundary_digest_binds_the_exact_native_policy_template(
    tmp_path: Path, fixtures, monkeypatch,
) -> None:
    home = tmp_path / "home"
    source = tmp_path / "source"
    baseline = tmp_path / "baseline"
    consumer = tmp_path / "consumer"
    for path in (home, source, baseline, consumer):
        path.mkdir()
    monkeypatch.setattr(fixtures, "_account_home", lambda: home)
    monkeypatch.setattr(fixtures, "_protected_roots", lambda _root: (source,))
    boundary = fixtures.open_blindness_boundary(
        tmp_path / "campaign", baseline, consumer, resume=False,
    )
    template = fixtures._native_campaign_policy(
        boundary, boundary.run_root / "__SEAT__", boundary.run_root / "__STATE__",
    )
    assert boundary.policy_sha256 == hashlib.sha256(template.encode()).hexdigest()


def test_private_seat_state_has_private_ancestors_and_stable_resume_home(
    tmp_path: Path, fixtures, monkeypatch,
) -> None:
    home = tmp_path / "home"
    source = tmp_path / "source"
    baseline = tmp_path / "baseline"
    consumer = tmp_path / "consumer"
    for path in (home, source, baseline, consumer):
        path.mkdir()
    monkeypatch.setattr(fixtures, "_account_home", lambda: home)
    monkeypatch.setattr(fixtures, "_protected_roots", lambda _root: (source,))
    boundary = fixtures.open_blindness_boundary(
        tmp_path / "campaign", baseline, consumer, resume=False,
    )
    seat = boundary.run_root / "cases" / "forge-01" / "seats" / "codex"
    fixtures.materialize("forge-01", seat, FIXTURES)
    first = fixtures.seat_environment(boundary, seat, "codex")
    second = fixtures.seat_environment(boundary, seat, "codex")
    assert first == second
    private = boundary.run_root / ".forge-seat-state"
    for path in (private, private / "forge-01", Path(first["HOME"]),
                 Path(first["XDG_CONFIG_HOME"]), Path(first["TMPDIR"])):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700, str(path)


def test_native_probe_refuses_a_dangling_session_state_alias_without_writing_through_it(
    tmp_path: Path, fixtures, monkeypatch,
) -> None:
    home = tmp_path / "home"
    source = tmp_path / "source"
    baseline = tmp_path / "baseline"
    consumer = tmp_path / "consumer"
    for path in (home, source, baseline, consumer):
        path.mkdir()
    monkeypatch.setattr(fixtures, "_account_home", lambda: home)
    monkeypatch.setattr(fixtures, "_protected_roots", lambda _root: (source,))
    boundary = fixtures.open_blindness_boundary(
        tmp_path / "campaign", baseline, consumer, resume=False,
    )
    seat = boundary.run_root / "cases" / "forge-01" / "seats" / "codex"
    seat.mkdir(parents=True)
    (seat / "sentinel").write_bytes(b"seat\n")
    state = fixtures._seat_state(boundary, seat)
    victim = baseline / "created-by-probe"
    (state / ".boundary-probe").symlink_to(victim)
    monkeypatch.setattr(fixtures, "_native_campaign_probe", lambda *_args: 0)
    with pytest.raises(fixtures.FixtureError, match="session probe path is unsafe"):
        fixtures._attest_native_policy(boundary, seat, state)
    assert not victim.exists()


@pytest.mark.parametrize("which", ("baseline", "memory"))
def test_campaign_policy_denies_a_preexisting_hardlink_alias(
    tmp_path: Path, fixtures, monkeypatch, which: str,
) -> None:
    """A file in a denied tree cannot be reachable through an allowed alias."""
    if sys.platform != "darwin":
        pytest.skip("native Forge boundary requires macOS sandbox-exec")
    home = tmp_path / "home"
    source = tmp_path / "source"
    baseline = tmp_path / "baseline"
    consumer = tmp_path / "consumer"
    for path in (home, source, baseline, consumer):
        path.mkdir()
    memory = home / ".config" / "agentic-memory"
    memory.mkdir(parents=True)
    protected = (baseline if which == "baseline" else memory) / "sentinel"
    protected.write_text("private", encoding="utf-8")
    (tmp_path / "alias").hardlink_to(protected)
    monkeypatch.setattr(fixtures, "_account_home", lambda: home)
    monkeypatch.setattr(fixtures, "_protected_roots", lambda _root: (source,))
    boundary = fixtures.open_blindness_boundary(
        tmp_path / "campaign", baseline, consumer, resume=False,
    )
    policy = fixtures._native_campaign_policy(
        boundary, boundary.run_root / "__SEAT__", boundary.run_root / "__STATE__",
    )
    denied = ("import sys\ntry: open(sys.argv[1],'rb').read(1)\n"
              "except PermissionError: sys.exit(17)\nsys.exit(0)")
    try:
        actual = fixtures._native_campaign_probe(policy, denied, str(tmp_path / "alias"))
    except fixtures.FixtureError as error:
        if str(error) == "sandbox-exec cannot apply in this process":
            pytest.skip("nested tool sandbox cannot apply macOS sandbox-exec")
        raise
    assert actual == 17


def test_native_boundary_refuses_a_preexisting_hardlink_inside_the_allowed_seat(
    tmp_path: Path, fixtures, monkeypatch,
) -> None:
    home = tmp_path / "home"
    source = tmp_path / "source"
    baseline = tmp_path / "baseline"
    consumer = tmp_path / "consumer"
    for path in (home, source, baseline, consumer):
        path.mkdir()
    oracle = source / "held-out" / "forge-01.py"
    oracle.parent.mkdir()
    oracle.write_text("private oracle", encoding="utf-8")
    monkeypatch.setattr(fixtures, "_account_home", lambda: home)
    monkeypatch.setattr(fixtures, "_protected_roots", lambda _root: (source,))
    boundary = fixtures.open_blindness_boundary(
        tmp_path / "campaign", baseline, consumer, resume=False,
    )
    seat = boundary.run_root / "cases" / "forge-01" / "seats" / "claude"
    fixtures.materialize("forge-01", seat, FIXTURES)
    (seat / "oracle-alias.py").hardlink_to(oracle)

    def no_native_probe(*_args):
        raise AssertionError("hard-link preflight must run before sandbox launch")

    monkeypatch.setattr(fixtures, "_native_campaign_probe", no_native_probe)
    with pytest.raises(fixtures.FixtureError, match="hard.link.*seat"):
        fixtures._attest_native_boundary(boundary, seat)


@pytest.mark.parametrize("location", ("home", "peer", "controller"))
def test_native_boundary_denies_hardlink_aliases_from_every_protected_root(
    tmp_path: Path, fixtures, monkeypatch, location: str,
) -> None:
    if sys.platform != "darwin":
        pytest.skip("native Forge boundary requires macOS sandbox-exec")
    home = tmp_path / "home"
    source = tmp_path / "source"
    baseline = tmp_path / "baseline"
    consumer = tmp_path / "consumer"
    for path in (home, source, baseline, consumer):
        path.mkdir()
    monkeypatch.setattr(fixtures, "_account_home", lambda: home)
    monkeypatch.setattr(fixtures, "_protected_roots", lambda _root: (source,))
    boundary = fixtures.open_blindness_boundary(
        tmp_path / "campaign", baseline, consumer, resume=False,
    )
    seat = boundary.run_root / "cases" / "forge-01" / "seats" / "claude"
    fixtures.materialize("forge-01", seat, FIXTURES)
    if location == "home":
        protected = home / "Documents" / "private.txt"
    elif location == "peer":
        protected = seat.parent / "codex" / "private.txt"
    else:
        protected = seat.parent.parent / "turns" / "terminal.json"
    protected.parent.mkdir(parents=True)
    protected.write_text("private", encoding="utf-8")
    (tmp_path / "outside-alias").hardlink_to(protected)

    try:
        policy = fixtures._attest_native_boundary(boundary, seat)
    except fixtures.FixtureError as error:
        if str(error) == "sandbox-exec cannot apply in this process":
            pytest.skip("nested tool sandbox cannot apply macOS sandbox-exec")
        raise
    denied = ("import sys\ntry: open(sys.argv[1],'rb').read(1)\n"
              "except PermissionError: sys.exit(17)\nsys.exit(0)")
    assert fixtures._native_campaign_probe(
        policy, denied, str(tmp_path / "outside-alias"),
    ) == 17


def test_native_boundary_does_not_scan_unrelated_protected_hardlinks(
    tmp_path: Path, fixtures, monkeypatch,
) -> None:
    if sys.platform != "darwin":
        pytest.skip("native Forge boundary requires macOS sandbox-exec")
    home = tmp_path / "home"
    source = tmp_path / "source"
    baseline = tmp_path / "baseline"
    consumer = tmp_path / "consumer"
    for path in (home, source, baseline, consumer):
        path.mkdir()
    notes = home / "Notes"
    notes.mkdir()
    note = notes / "private"
    note.write_text("private", encoding="utf-8")
    (notes / "another-name").hardlink_to(note)
    monkeypatch.setattr(fixtures, "_account_home", lambda: home)
    monkeypatch.setattr(fixtures, "_protected_roots", lambda _root: (source,))
    boundary = fixtures.open_blindness_boundary(
        tmp_path / "campaign", baseline, consumer, resume=False,
    )
    seat = boundary.run_root / "cases" / "forge-01" / "seats" / "claude"
    fixtures.materialize("forge-01", seat, FIXTURES)
    try:
        fixtures._attest_native_boundary(boundary, seat)
    except fixtures.FixtureError as error:
        if str(error) == "sandbox-exec cannot apply in this process":
            pytest.skip("nested tool sandbox cannot apply macOS sandbox-exec")
        raise


@pytest.mark.parametrize("private_name", ("caller", "turns", "synthesis", "reviewer"))
def test_native_attestation_probes_existing_caller_and_authority_roots(
    tmp_path: Path, fixtures, monkeypatch, private_name: str,
) -> None:
    home = tmp_path / "home"
    source = tmp_path / "source"
    baseline = tmp_path / "baseline"
    consumer = tmp_path / "consumer"
    for path in (home, source, baseline, consumer):
        path.mkdir()
    monkeypatch.setattr(fixtures, "_account_home", lambda: home)
    monkeypatch.setattr(fixtures, "_protected_roots", lambda _root: (source,))
    boundary = fixtures.open_blindness_boundary(
        tmp_path / "campaign", baseline, consumer, resume=False,
    )
    case = boundary.run_root / "cases" / "forge-01"
    seat = case / "seats" / "claude"
    fixtures.materialize("forge-01", seat, FIXTURES)
    for name in ("caller", "turns", "synthesis", "reviewer"):
        (case / name).mkdir()
    leaked = case / private_name

    def leaking_probe(_policy, source_code, *args):
        if args == (str(leaked),):
            return 0
        if ("TemporaryDirectory" in source_code
                or source_code == "import sys;open(sys.argv[1],'rb').read(1)"):
            return 0
        return 17

    monkeypatch.setattr(fixtures, "_native_campaign_probe", leaking_probe)
    with pytest.raises(fixtures.FixtureError, match="caller|authority|private"):
        fixtures._attest_native_boundary(boundary, seat)


def test_native_attestation_probes_a_peer_session_state_root(
    tmp_path: Path, fixtures, monkeypatch,
) -> None:
    home = tmp_path / "home"
    source = tmp_path / "source"
    baseline = tmp_path / "baseline"
    consumer = tmp_path / "consumer"
    for path in (home, source, baseline, consumer):
        path.mkdir()
    monkeypatch.setattr(fixtures, "_account_home", lambda: home)
    monkeypatch.setattr(fixtures, "_protected_roots", lambda _root: (source,))
    boundary = fixtures.open_blindness_boundary(
        tmp_path / "campaign", baseline, consumer, resume=False,
    )
    seat = boundary.run_root / "cases" / "forge-01" / "seats" / "claude"
    fixtures.materialize("forge-01", seat, FIXTURES)
    own_state = fixtures._seat_state(boundary, seat)
    peer_state = own_state.parent / "codex"
    peer_state.mkdir(mode=0o700)

    def leaking_probe(_policy, source_code, *args):
        if args == (str(peer_state),):
            return 0
        if ("TemporaryDirectory" in source_code
                or source_code == "import sys;open(sys.argv[1],'rb').read(1)"):
            return 0
        return 17

    monkeypatch.setattr(fixtures, "_native_campaign_probe", leaking_probe)
    with pytest.raises(fixtures.FixtureError, match="peer.*session|session.*peer"):
        fixtures._attest_native_boundary(boundary, seat)


@pytest.mark.parametrize("role", ("synthesis", "reviewer"))
def test_native_attestation_probes_source_seats_for_synthesis_and_review(
    tmp_path: Path, fixtures, monkeypatch, role: str,
) -> None:
    home = tmp_path / "home"
    source = tmp_path / "source"
    baseline = tmp_path / "baseline"
    consumer = tmp_path / "consumer"
    for path in (home, source, baseline, consumer):
        path.mkdir()
    monkeypatch.setattr(fixtures, "_account_home", lambda: home)
    monkeypatch.setattr(fixtures, "_protected_roots", lambda _root: (source,))
    boundary = fixtures.open_blindness_boundary(
        tmp_path / "campaign", baseline, consumer, resume=False,
    )
    case = boundary.run_root / "cases" / "forge-01"
    workspace = case / role
    fixtures.materialize("forge-01", workspace, FIXTURES)
    peer_seats = case / "seats"
    (peer_seats / "claude").mkdir(parents=True)

    def leaking_probe(_policy, source_code, *args):
        if args == (str(peer_seats),):
            return 0
        if ("TemporaryDirectory" in source_code
                or source_code == "import sys;open(sys.argv[1],'rb').read(1)"):
            return 0
        return 17

    monkeypatch.setattr(fixtures, "_native_campaign_probe", leaking_probe)
    with pytest.raises(fixtures.FixtureError, match="peer|authority|private"):
        fixtures._attest_native_boundary(boundary, workspace)


def test_native_campaign_policy_denies_external_roots_aliases_and_loopback(
    tmp_path: Path, fixtures, monkeypatch,
) -> None:
    """A real sandbox, not a string assertion, must enforce the campaign boundary."""
    if sys.platform != "darwin":
        pytest.skip("native Forge boundary requires macOS sandbox-exec")
    home = tmp_path / "home"
    source = tmp_path / "source"
    baseline = tmp_path / "baseline"
    consumer = tmp_path / "consumer"
    for path in (home, source, baseline, consumer):
        path.mkdir()
    sentinels = []
    for root in (home, source, baseline, consumer):
        sentinel = root / "sentinel"
        sentinel.write_text("private", encoding="utf-8")
        sentinels.append(sentinel)
    monkeypatch.setattr(fixtures, "_account_home", lambda: home)
    monkeypatch.setattr(fixtures, "_protected_roots", lambda _root: (source,))
    run_root = tmp_path / "campaign"
    boundary = fixtures.open_blindness_boundary(run_root, baseline, consumer, resume=False)
    seat = run_root / "cases" / "forge-01" / "seats" / "claude"
    fixtures.materialize("forge-01", seat, FIXTURES)
    sibling = seat.parent / "codex"
    sibling.mkdir()
    sibling_sentinel = sibling / "sentinel"
    sibling_sentinel.write_text("private", encoding="utf-8")
    case = seat.parent.parent
    campaign_private = []
    for name in ("caller", "turns", "synthesis", "reviewer"):
        private = case / name
        private.mkdir()
        sentinel = private / "sentinel"
        sentinel.write_text("private", encoding="utf-8")
        campaign_private.append(sentinel)
    peer_state = fixtures._seat_state(boundary, seat).parent / "codex"
    peer_state.mkdir(mode=0o700)
    peer_state_sentinel = peer_state / "sentinel"
    peer_state_sentinel.write_text("private", encoding="utf-8")
    aliases = []
    for index, target in enumerate(sentinels):
        alias = tmp_path / f"alias-{index}"
        alias.symlink_to(target)
        aliases.append(alias)
    seat_alias = seat / "alias-to-source"
    seat_alias.symlink_to(source / "sentinel")
    aliases.append(seat_alias)
    try:
        policy = fixtures._attest_native_boundary(boundary, seat)
    except fixtures.FixtureError as error:
        if str(error) == "sandbox-exec cannot apply in this process":
            pytest.skip("nested tool sandbox cannot apply macOS sandbox-exec")
        raise
    wrapped = fixtures.executor_argv(
        "forge-01", seat, ["/usr/bin/python3", "-c", "pass"], FIXTURES,
        boundary=boundary,
    )
    assert wrapped[:3] == ["/usr/bin/sandbox-exec", "-p", policy]
    denied = "import sys;open(sys.argv[1],'rb').read(1)"
    for path in (*sentinels, *aliases, sibling_sentinel,
                 *campaign_private, peer_state_sentinel,
                 run_root / ".forge-boundary" / "descriptor.json",
                 run_root / ".forge-boundary" / "key"):
        run = subprocess.run(
            ["/usr/bin/sandbox-exec", "-p", policy, "/usr/bin/python3",
             "-c", denied, str(path)], capture_output=True, text=True,
        )
        assert run.returncode != 0 and "PermissionError" in run.stderr, str(path)
    own = seat / "src" / "transfer.py"
    run = subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", policy, "/usr/bin/python3",
         "-c", denied, str(own)], capture_output=True, text=True,
    )
    assert run.returncode == 0, run.stderr
    outside = tmp_path / "outside.txt"
    outside.write_text("unchanged", encoding="utf-8")
    denied_write = (
        "import sys\ntry: open(sys.argv[1],'wb').write(b'changed')\n"
        "except PermissionError: sys.exit(17)\nsys.exit(0)"
    )
    run = subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", policy, "/usr/bin/python3",
         "-c", denied_write, str(outside)], capture_output=True, text=True,
    )
    assert run.returncode == 17, run.stderr
    assert outside.read_text(encoding="utf-8") == "unchanged"


def test_native_campaign_policy_denies_ambient_hardlink_alias(
    tmp_path: Path, fixtures, monkeypatch,
) -> None:
    """A denied home file cannot escape through an alias in an ambient path."""
    if sys.platform != "darwin":
        pytest.skip("native Forge boundary requires macOS sandbox-exec")
    home = tmp_path / "home"
    source = tmp_path / "source"
    baseline = tmp_path / "baseline"
    consumer = tmp_path / "consumer"
    for path in (home, source, baseline, consumer):
        path.mkdir()
    secret = home / "private.txt"
    secret.write_text("private", encoding="utf-8")
    alias = tmp_path / "ambient-alias"
    alias.hardlink_to(secret)
    monkeypatch.setattr(fixtures, "_account_home", lambda: home)
    monkeypatch.setattr(fixtures, "_protected_roots", lambda _root: (source,))
    boundary = fixtures.open_blindness_boundary(
        tmp_path / "campaign", baseline, consumer, resume=False,
    )
    seat = boundary.run_root / "cases" / "forge-01" / "seats" / "claude"
    fixtures.materialize("forge-01", seat, FIXTURES)
    state = fixtures._seat_state(boundary, seat)
    policy = fixtures._native_campaign_policy(boundary, seat, state)
    denied = ("import sys\ntry: open(sys.argv[1],'rb').read(1)\n"
              "except PermissionError: sys.exit(17)\nsys.exit(0)")
    try:
        actual = fixtures._native_campaign_probe(policy, denied, str(alias))
    except fixtures.FixtureError as error:
        if str(error) == "sandbox-exec cannot apply in this process":
            pytest.skip("nested tool sandbox cannot apply macOS sandbox-exec")
        raise
    assert actual == 17


def test_campaign_precreation_checks_native_boundary_before_any_intent(
    tmp_path: Path, fixtures, monkeypatch,
) -> None:
    """A controller cannot create a campaign on a descriptor that was never probed."""
    home = tmp_path / "home"
    source = tmp_path / "source"
    baseline = tmp_path / "baseline"
    consumer = tmp_path / "consumer"
    for path in (home, source, baseline, consumer):
        path.mkdir()
    monkeypatch.setattr(fixtures, "_account_home", lambda: home)
    monkeypatch.setattr(fixtures, "_protected_roots", lambda _root: (source,))
    boundary = fixtures.open_blindness_boundary(
        tmp_path / "campaign", baseline, consumer, resume=False,
    )
    checked = []
    monkeypatch.setattr(fixtures, "_attest_native_policy",
                        lambda descriptor, workspace, state: checked.append(
                            (descriptor, workspace, state)))
    with pytest.raises(fixtures.FixtureError, match="auth|resume"):
        fixtures.require_blindness_attestation(boundary=boundary)
    assert len(checked) == 1
    assert checked[0][0] == boundary
    assert checked[0][1].parent == checked[0][2].parent


def test_live_executor_wrapper_refuses_a_missing_sealed_boundary(
    tmp_path: Path, fixtures, monkeypatch,
) -> None:
    seat = tmp_path / "forge-01"
    fixtures.materialize("forge-01", seat, FIXTURES)
    monkeypatch.setattr(fixtures, "_validate_boundary", lambda *_args: "(version 1) (allow default)")
    with pytest.raises(fixtures.FixtureError, match="sealed.*boundary"):
        fixtures.executor_argv("forge-01", seat, [sys.executable, "-c", "pass"], FIXTURES)


@pytest.fixture
def native_boundary(tmp_path: Path, fixtures):
    if sys.platform != "darwin":
        pytest.skip("macOS sandbox-exec is required for runnable Forge fixtures")
    workspace = tmp_path / "boundary-preflight"
    fixtures.materialize("forge-01", workspace, FIXTURES)
    try:
        _fixture_probe_argv(fixtures, "forge-01", workspace, [sys.executable, "-c", "pass"])
    except fixtures.FixtureError as error:
        if str(error) == "sandbox-exec cannot apply in this process":
            pytest.skip("nested tool sandbox cannot apply macOS sandbox-exec")
        raise


@pytest.mark.usefixtures("native_boundary")
def test_twelve_runnable_fixtures_match_the_committed_casebook(tmp_path: Path, fixtures):
    book = json.loads(CASEBOOK.read_text(encoding="utf-8"))
    descriptors = fixtures.verify_pin(CASEBOOK, FIXTURES)
    forge = {case["id"]: case["mode"] for case in book["cases"] if case["kind"] == "forge"}

    assert set(descriptors) == set(forge)
    assert book["forge_fixture_manifest_sha256"] == fixtures.manifest_sha256(FIXTURES)
    for case_id in sorted(forge):
        seat = tmp_path / case_id
        fixtures.materialize(case_id, seat, FIXTURES)
        assert not any("held-out" in path.parts for path in seat.rglob("*"))
        assert fixtures.run_declared_check(case_id, seat, FIXTURES).returncode == 0
        held_out = fixtures.run_held_out(case_id, seat, FIXTURES)
        assert held_out.returncode != 0 and "AssertionError" in held_out.stderr, case_id


def test_source_and_held_out_oracle_tampering_both_break_the_pin(tmp_path: Path, fixtures):
    fixture_root = tmp_path / "fixtures"
    shutil.copytree(FIXTURES, fixture_root)
    source = fixture_root / "public" / "forge-01" / "src" / "transfer.py"
    source.write_text(source.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")
    with pytest.raises(fixtures.FixtureError, match="pin"):
        fixtures.verify_pin(CASEBOOK, fixture_root)

    shutil.copytree(FIXTURES, tmp_path / "oracle-fixtures")
    oracle_root = tmp_path / "oracle-fixtures"
    oracle = oracle_root / "held-out" / "forge-01.py"
    oracle.write_text(oracle.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")
    with pytest.raises(fixtures.FixtureError, match="pin"):
        fixtures.verify_pin(CASEBOOK, oracle_root)

    book = json.loads(CASEBOOK.read_text(encoding="utf-8"))
    book["cases"][-1]["prompt"] += " Changed after registration."
    stale_book = tmp_path / "stale-casebook.json"
    stale_book.write_text(json.dumps(book), encoding="utf-8")
    with pytest.raises(fixtures.FixtureError, match="casebook seal"):
        fixtures.verify_pin(stale_book, FIXTURES)


@pytest.mark.parametrize("path_kind", (
    "dot", "empty", "workspace", "seat", "seat-alias", "binary-alias",
))
def test_declared_check_never_executes_a_candidate_owned_path_binary(
    tmp_path: Path, sealed_verifier, fixtures, monkeypatch, path_kind: str,
) -> None:
    boundary, workspace = sealed_verifier
    run_root = boundary.run_root
    seat_bin = run_root / "cases" / "forge-01" / "seats" / "claude" / "bin"
    if path_kind in {"seat", "seat-alias"}:
        search_dir = seat_bin
        search_dir.mkdir(parents=True)
    else:
        search_dir = workspace
    marker = tmp_path / "candidate-binary-ran"
    fake_python = search_dir / "python3"
    fake_python.write_text(
        f"#!/bin/sh\n/usr/bin/touch '{marker}'\n", encoding="utf-8",
    )
    fake_python.chmod(0o755)
    if path_kind == "dot":
        component = "."
    elif path_kind == "empty":
        component = ""
    elif path_kind == "seat-alias":
        alias = tmp_path / "seat-bin-alias"
        alias.symlink_to(seat_bin, target_is_directory=True)
        component = str(alias)
    elif path_kind == "binary-alias":
        alias_bin = tmp_path / "external-bin"
        alias_bin.mkdir()
        (alias_bin / "python3").symlink_to(fake_python)
        component = str(alias_bin)
    else:
        component = str(search_dir)
    monkeypatch.setenv("PATH", f"{component}:/usr/bin:/bin")
    with pytest.raises(fixtures.FixtureError, match="PATH|candidate|executable"):
        fixtures.run_declared_check("forge-01", workspace, FIXTURES, boundary=boundary)
    assert not marker.exists()


@pytest.fixture
def sealed_verifier(tmp_path: Path, fixtures, monkeypatch):
    home = tmp_path / "home"
    source = tmp_path / "source"
    baseline = tmp_path / "baseline"
    consumer = tmp_path / "consumer"
    for path in (home, source, baseline, consumer):
        path.mkdir()
    monkeypatch.setattr(fixtures, "_account_home", lambda: home)
    monkeypatch.setattr(fixtures, "_protected_roots", lambda _root: (source,))
    monkeypatch.setattr(fixtures, "_installed_executables", lambda: (
        ("claude", Path("/usr/bin/true")),
        ("codex", Path("/usr/bin/true")),
        ("agy", Path("/usr/bin/true")),
    ))
    boundary = fixtures.open_blindness_boundary(
        tmp_path / "campaign", baseline, consumer, resume=False,
    )
    workspace = boundary.run_root / "verifiers" / "forge-fresh-verifier-test" / "workspace"
    fixtures.materialize("forge-01", workspace, FIXTURES)
    workspace.parent.chmod(0o700)
    return boundary, workspace


@pytest.mark.parametrize("check", ("declared", "held-out", "probe"))
def test_campaign_verifier_checks_refuse_missing_boundary_before_candidate_launch(
    sealed_verifier, fixtures, monkeypatch, check: str,
) -> None:
    _boundary, workspace = sealed_verifier
    launched = []
    monkeypatch.setattr(fixtures, "_validate_boundary", lambda *_args: "standalone-policy")

    def recorded_run(argv, **_kwargs):
        launched.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

    monkeypatch.setattr(fixtures.subprocess, "run", recorded_run)
    operation = {
        "declared": fixtures.run_declared_check,
        "held-out": fixtures.run_held_out,
        "probe": fixtures.probe_observation,
    }[check]
    with pytest.raises(fixtures.FixtureError, match="sealed.*boundary"):
        operation("forge-01", workspace, FIXTURES)
    assert launched == []


def test_campaign_declared_check_launches_under_its_sealed_protected_roots(
    sealed_verifier, fixtures, monkeypatch,
) -> None:
    boundary, workspace = sealed_verifier
    monkeypatch.setattr(fixtures, "_attest_native_policy",
                        lambda descriptor, work, state:
                        fixtures._native_campaign_policy(descriptor, work, state))
    launched = []

    def recorded_run(argv, **kwargs):
        launched.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    monkeypatch.setattr(fixtures.subprocess, "run", recorded_run)
    result = fixtures.run_declared_check("forge-01", workspace, FIXTURES,
                                         boundary=boundary)
    assert result.returncode == 0
    assert len(launched) == 1
    argv, kwargs = launched[0]
    assert argv[:2] == ["/usr/bin/sandbox-exec", "-p"]
    assert "(deny file-read* (require-not (require-any " in argv[2]
    for root in boundary.protected_roots:
        assert f'(allow file-read* (subpath {json.dumps(str(root))}))' not in argv[2]
    assert kwargs["cwd"] == workspace
    assert kwargs["env"]["HOME"].startswith(str(workspace.parent))
    assert kwargs["env"]["TMPDIR"].startswith(str(workspace.parent))


def test_campaign_held_out_probe_uses_sealed_policy_without_oracle_in_candidate_process(
    sealed_verifier, fixtures, monkeypatch,
) -> None:
    boundary, workspace = sealed_verifier
    monkeypatch.setattr(fixtures, "_attest_native_policy",
                        lambda descriptor, work, state:
                        fixtures._native_campaign_policy(descriptor, work, state))
    launched = []

    def recorded_run(argv, **kwargs):
        launched.append((argv, kwargs))
        stdout = '{}' if argv[0] == sys.executable else '{"orders": []}'
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(fixtures.subprocess, "run", recorded_run)
    fixtures.run_held_out("forge-01", workspace, FIXTURES, boundary=boundary)
    assert len(launched) == 1
    oracle_argv, oracle_kwargs = launched.pop()
    assert oracle_argv[:2] == [sys.executable, str(FIXTURES / "held-out" / "forge-01.py")]
    assert oracle_argv[2] == str(workspace)
    for key, value in oracle_kwargs["env"].items():
        monkeypatch.setenv(key, value)
    fixtures.probe_observation("forge-01", workspace, FIXTURES)
    assert len(launched) == 1
    probe_argv, probe_kwargs = launched[0]
    assert probe_argv[:2] == ["/usr/bin/sandbox-exec", "-p"]
    assert "(deny file-read* (require-not (require-any " in probe_argv[2]
    for root in boundary.protected_roots:
        assert f'(allow file-read* (subpath {json.dumps(str(root))}))' not in probe_argv[2]
    assert str(FIXTURES / "held-out" / "forge-01.py") not in probe_argv
    assert probe_kwargs["cwd"] == workspace


def test_campaign_held_out_rejects_protected_python_or_denies_roots(
    tmp_path: Path, fixtures,
) -> None:
    baseline = tmp_path / "baseline"
    consumer = tmp_path / "consumer"
    baseline.mkdir()
    consumer.mkdir()
    boundary = fixtures.open_blindness_boundary(
        tmp_path / "campaign", baseline, consumer, resume=False,
    )
    workspace = boundary.run_root / "verifiers" / "forge-fresh-verifier-test" / "workspace"
    fixtures.materialize("forge-01", workspace, FIXTURES)
    workspace.parent.chmod(0o700)
    source = workspace / "src" / "transfer.py"
    roots = [str(root) for root in boundary.protected_roots]
    source.write_text(
        "import os\nfrom pathlib import Path\n"
        f"for root in {roots!r}:\n"
        "    try: os.listdir(root)\n"
        "    except PermissionError: pass\n"
        "    else: raise RuntimeError('protected root readable')\n"
        "Path(__file__).resolve().parents[1].joinpath('denied-probe').write_text('denied')\n"
        + source.read_text(encoding="utf-8"), encoding="utf-8",
    )
    protected_python = Path(sys.executable).resolve()
    if any(protected_python == root or root in protected_python.parents
           for root in boundary.protected_roots):
        with pytest.raises(fixtures.FixtureError, match="Python runtime.*protected root"):
            fixtures.run_held_out("forge-01", workspace, FIXTURES,
                                  boundary=boundary)
        assert not (workspace / "denied-probe").exists()
        return
    try:
        result = fixtures.run_held_out("forge-01", workspace, FIXTURES,
                                       boundary=boundary)
    except fixtures.FixtureError as error:
        if str(error) == "sandbox-exec cannot apply in this process":
            pytest.skip("nested tool sandbox cannot apply macOS sandbox-exec")
        raise
    assert (workspace / "denied-probe").is_file(), result.stderr
    assert (workspace / "denied-probe").read_text() == "denied"
    assert result.returncode != 0 and "AssertionError" in result.stderr
    assert "protected root readable" not in result.stderr


def test_campaign_verifier_native_policy_denies_protected_roots(
    tmp_path: Path, fixtures, monkeypatch,
) -> None:
    home = tmp_path / "home"
    baseline = tmp_path / "baseline"
    consumer = tmp_path / "consumer"
    home.mkdir()
    baseline.mkdir()
    consumer.mkdir()
    monkeypatch.setattr(fixtures, "_account_home", lambda: home)
    boundary = fixtures.open_blindness_boundary(
        tmp_path / "campaign", baseline, consumer, resume=False,
    )
    workspace = boundary.run_root / "verifiers" / "forge-fresh-verifier-test" / "workspace"
    fixtures.materialize("forge-01", workspace, FIXTURES)
    workspace.parent.chmod(0o700)
    try:
        policy, _state = fixtures._verifier_policy(boundary, workspace)
    except fixtures.FixtureError as error:
        if str(error) == "sandbox-exec cannot apply in this process":
            pytest.skip("nested tool sandbox cannot apply macOS sandbox-exec")
        raise
    denied = ("import os,sys\ntry: os.listdir(sys.argv[1])\n"
              "except PermissionError: sys.exit(17)\nsys.exit(0)")
    for root in boundary.protected_roots:
        result = subprocess.run(
            ["/usr/bin/sandbox-exec", "-p", policy, "/usr/bin/python3", "-c",
             denied, str(root)], capture_output=True, text=True,
        )
        assert result.returncode == 17, (root, result.stderr)
    own = subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", policy, "/usr/bin/python3", "-c",
         "import sys;open(sys.argv[1], 'rb').read(1)",
         str(workspace / "src" / "transfer.py")], capture_output=True, text=True,
    )
    assert own.returncode == 0, own.stderr


def test_materialization_keeps_dirty_binary_and_symlink_out_of_the_oracle_tree(tmp_path: Path, fixtures):
    seat_10 = tmp_path / "forge-10"
    fixtures.materialize("forge-10", seat_10, FIXTURES)
    assert b"\x00" in (seat_10 / "assets" / "staged.bin").read_bytes()
    assert (seat_10 / "notes" / "local.txt").exists()

    seat_12 = tmp_path / "forge-12"
    fixtures.materialize("forge-12", seat_12, FIXTURES)
    assert b"\x00" in (seat_12 / "assets" / "staged.bin").read_bytes()
    assert (seat_12 / "assets" / "current.bin").is_symlink()
    assert (seat_12 / "assets" / "current.bin").readlink() == Path("staged.bin")
    assert not (seat_12 / "held-out").exists()


def test_manifest_hash_is_stable_across_repeated_reads(fixtures):
    first = fixtures.descriptors(FIXTURES)
    second = fixtures.descriptors(FIXTURES)
    assert first == second
    assert fixtures.manifest_sha256(FIXTURES) == fixtures.manifest_sha256(FIXTURES)


@pytest.mark.usefixtures("native_boundary")
def test_consistent_reverse_lock_order_passes_held_out_check(tmp_path: Path, fixtures):
    seat = tmp_path / "forge-01"
    fixtures.materialize("forge-01", seat, FIXTURES)
    source = seat / "src" / "transfer.py"
    source.write_text(source.read_text(encoding="utf-8").replace(
        "with ledger_lock:\n        with account_lock:",
        "with account_lock:\n        with ledger_lock:",
    ), encoding="utf-8")
    assert fixtures.run_held_out("forge-01", seat, FIXTURES).returncode == 0

    source.write_text(source.read_text(encoding="utf-8").replace(
        "with account_lock:\n        with ledger_lock:",
        "with ledger_lock:\n        with account_lock:",
    ), encoding="utf-8")
    assert fixtures.run_held_out("forge-01", seat, FIXTURES).returncode == 0


@pytest.mark.usefixtures("native_boundary")
@pytest.mark.parametrize("peer_messages", (
    "[{'role': 'user', 'content': peer}]",
    "[{'role': 'user', 'content': '<untrusted-peer-evidence>' + peer + '</untrusted-peer-evidence>'}, "
    "{'role': 'user', 'content': peer}]",
), ids=("raw-peer", "raw-peer-beside-wrapper"))
def test_raw_peer_instruction_fails_held_out_check(tmp_path: Path, fixtures, peer_messages: str):
    seat = tmp_path / "forge-08"
    fixtures.materialize("forge-08", seat, FIXTURES)
    (seat / "src" / "prompt.py").write_text(
        "def compose_messages(peer, task):\n"
        f"    return {peer_messages} + [{{'role': 'user', 'content': task}}]\n",
        encoding="utf-8",
    )
    result = fixtures.run_held_out("forge-08", seat, FIXTURES)
    assert result.returncode != 0 and "AssertionError" in result.stderr


@pytest.mark.usefixtures("native_boundary")
def test_wrapped_peer_evidence_preserves_owner_task_and_passes_held_out_check(tmp_path: Path, fixtures):
    seat = tmp_path / "forge-08"
    fixtures.materialize("forge-08", seat, FIXTURES)
    (seat / "src" / "prompt.py").write_text(
        "def compose_messages(peer, task):\n"
        "    return [{'role': 'user', 'content': '<untrusted-peer-evidence>\\n' + peer + "
        "'\\n</untrusted-peer-evidence>'}, {'role': 'user', 'content': task}]\n",
        encoding="utf-8",
    )
    result = fixtures.run_held_out("forge-08", seat, FIXTURES)
    assert result.returncode == 0, result.stderr


@pytest.mark.usefixtures("native_boundary")
@pytest.mark.parametrize("body", ("return", "raise ValueError('rejected before the race')"),
                         ids=("no-op", "early-refusal"))
def test_publisher_bypassing_race_fails_held_out_check(tmp_path: Path, fixtures, body: str):
    seat = tmp_path / "forge-05"
    fixtures.materialize("forge-05", seat, FIXTURES)
    (seat / "src" / "publisher.py").write_text(
        f"def publish(root, relative, data, before_commit):\n    {body}\n",
        encoding="utf-8",
    )
    result = fixtures.run_held_out("forge-05", seat, FIXTURES)
    assert result.returncode != 0 and "AssertionError" in result.stderr


@pytest.mark.usefixtures("native_boundary")
def test_publisher_exercising_safe_race_passes_held_out_check(tmp_path: Path, fixtures):
    seat = tmp_path / "forge-05"
    fixtures.materialize("forge-05", seat, FIXTURES)
    (seat / "src" / "publisher.py").write_text(
        "from pathlib import Path\n"
        "def publish(root, relative, data, before_commit):\n"
        "    root = Path(root).resolve()\n"
        "    destination = root / relative\n"
        "    before_commit()\n"
        "    if not destination.parent.resolve().is_relative_to(root):\n"
        "        raise ValueError('parent replaced outside the allowed root')\n"
        "    destination.write_bytes(data)\n",
        encoding="utf-8",
    )
    result = fixtures.run_held_out("forge-05", seat, FIXTURES)
    assert result.returncode == 0, result.stderr


@pytest.mark.usefixtures("native_boundary")
def test_safe_shell_target_with_combined_flags_passes_held_out_check(tmp_path: Path, fixtures):
    seat = tmp_path / "forge-04"
    fixtures.materialize("forge-04", seat, FIXTURES)
    (seat / "deploy.sh").write_text(
        '#!/bin/sh\nroot=$1\ntarget=$2\n'
        'root_real=$(cd -P "$root" && pwd -P) || exit 2\n'
        'target_real=$(cd -P "$target" && pwd -P) || exit 2\n'
        'case "$target_real" in "$root_real"/*) ;; *) exit 2 ;; esac\n'
        'rm -rf -- "$target"\n', encoding="utf-8",
    )
    assert fixtures.run_held_out("forge-04", seat, FIXTURES).returncode == 0


@pytest.mark.usefixtures("native_boundary")
def test_lexical_prefix_guard_fails_traversal_held_out_case(tmp_path: Path, fixtures):
    seat = tmp_path / "forge-04"
    fixtures.materialize("forge-04", seat, FIXTURES)
    (seat / "deploy.sh").write_text(
        '#!/bin/sh\nroot=$1\ntarget=$2\ncase "$target" in "$root"/*) ;; *) exit 2 ;; esac\n'
        'rm -rf -- "$target"\n', encoding="utf-8",
    )
    assert fixtures.run_held_out("forge-04", seat, FIXTURES).returncode != 0


@pytest.mark.usefixtures("native_boundary")
def test_held_out_checker_does_not_pass_ambient_secrets_to_candidate(
    tmp_path: Path, fixtures, monkeypatch,
):
    seat = tmp_path / "forge-01"
    fixtures.materialize("forge-01", seat, FIXTURES)
    source = seat / "src" / "transfer.py"
    fixed = source.read_text(encoding="utf-8").replace(
        "with ledger_lock:\n        with account_lock:",
        "with account_lock:\n        with ledger_lock:",
    )
    source.write_text(
        "import os\nif os.getenv('FANOUT_FIXTURE_SENTINEL'): "
        "raise RuntimeError('ambient secret leaked')\n" + fixed,
        encoding="utf-8",
    )
    monkeypatch.setenv("FANOUT_FIXTURE_SENTINEL", "private-test-value")
    assert fixtures.run_held_out("forge-01", seat, FIXTURES).returncode == 0


def test_materializer_refuses_copy_drift_before_executor_dispatch(
    tmp_path: Path, fixtures, monkeypatch,
):
    real_copy = fixtures.shutil.copytree

    def drifted_copy(source, destination, *args, **kwargs):
        copied = real_copy(source, destination, *args, **kwargs)
        if Path(destination).name == "seat":
            path = Path(destination) / "src" / "transfer.py"
            path.write_text(path.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
        return copied

    monkeypatch.setattr(fixtures.shutil, "copytree", drifted_copy)
    with pytest.raises(fixtures.FixtureError, match="materialized source"):
        fixtures.materialize("forge-01", tmp_path / "seat", FIXTURES)


@pytest.mark.usefixtures("native_boundary")
def test_held_out_check_runs_from_a_fresh_verifier_copy(tmp_path: Path, fixtures):
    seat = tmp_path / "forge-01"
    fixtures.materialize("forge-01", seat, FIXTURES)
    source = seat / "src" / "transfer.py"
    source.write_text(
        "from pathlib import Path\nPath(__file__).resolve().parents[1].joinpath('checker-created').write_text('x')\n"
        + source.read_text(encoding="utf-8"), encoding="utf-8",
    )
    fixtures.run_held_out("forge-01", seat, FIXTURES)
    assert not (seat / "checker-created").exists()


@pytest.mark.usefixtures("native_boundary")
def test_candidate_cannot_inspect_oracle_main_file_or_read_checkout(
    tmp_path: Path, fixtures,
):
    seat = tmp_path / "forge-01"
    fixtures.materialize("forge-01", seat, FIXTURES)
    source = seat / "src" / "transfer.py"
    fixed = source.read_text(encoding="utf-8").replace(
        "with ledger_lock:\n        with account_lock:",
        "with account_lock:\n        with ledger_lock:",
    )
    oracle = FIXTURES / "held-out" / "forge-01.py"
    source.write_text(
        "import sys\n"
        "if str(getattr(sys.modules['__main__'], '__file__', '')).endswith('forge-01.py'): "
        "raise RuntimeError('oracle file disclosed')\n"
        f"try: open({str(oracle)!r}, 'rb').read(1)\n"
        "except PermissionError: pass\n"
        "else: raise RuntimeError('private oracle readable')\n" + fixed,
        encoding="utf-8",
    )
    assert fixtures.run_held_out("forge-01", seat, FIXTURES).returncode == 0


@pytest.mark.usefixtures("native_boundary")
def test_executor_boundary_denies_private_checkout_even_through_symlink(
    tmp_path: Path, fixtures,
):
    if sys.platform != "darwin":
        pytest.skip("macOS sandbox-exec is required for live Forge seats")
    seat = tmp_path / "forge-01"
    fixtures.materialize("forge-01", seat, FIXTURES)
    secret = FIXTURES / "held-out" / "forge-01.py"
    alias = tmp_path / "oracle-alias.py"
    alias.symlink_to(secret)
    probe = "import sys;open(sys.argv[1],'rb').read(1)"
    command = _fixture_probe_argv(fixtures, "forge-01", seat,
                                  [sys.executable, "-c", probe, str(alias)])
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode != 0
    assert "PermissionError" in result.stderr


@pytest.mark.usefixtures("native_boundary")
def test_executor_boundary_denies_git_objects_and_other_worktrees(
    tmp_path: Path, fixtures,
):
    if sys.platform != "darwin":
        pytest.skip("macOS sandbox-exec is required for live Forge seats")
    seat = tmp_path / "forge-01"
    fixtures.materialize("forge-01", seat, FIXTURES)
    common = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.strip()
    main = Path(common).parent / "Makefile"
    for path in (Path(common) / "HEAD", main):
        command = _fixture_probe_argv(
            fixtures, "forge-01", seat,
            [sys.executable, "-c", "import sys;open(sys.argv[1],'rb').read(1)", str(path)],
        )
        result = subprocess.run(command, capture_output=True, text=True)
        assert result.returncode != 0 and "PermissionError" in result.stderr, str(path)


@pytest.mark.usefixtures("native_boundary")
def test_native_executor_cannot_read_a_sibling_seat(
    tmp_path: Path, fixtures,
) -> None:
    case = tmp_path / "case"
    seat = case / "seats" / "claude"
    sibling = case / "seats" / "codex"
    fixtures.materialize("forge-01", seat, FIXTURES)
    fixtures.materialize("forge-01", sibling, FIXTURES)
    sibling_source = sibling / "src" / "transfer.py"
    alias = tmp_path / "sibling-alias.py"
    alias.symlink_to(sibling_source)
    for path in (sibling_source, alias):
        command = _fixture_probe_argv(
            fixtures, "forge-01", seat,
            [sys.executable, "-c", "import sys;open(sys.argv[1],'rb').read(1)",
             str(path)],
        )
        result = subprocess.run(command, capture_output=True, text=True)
        assert result.returncode != 0, str(path)
        assert "PermissionError" in result.stderr, str(path)


def test_executor_preflight_refuses_a_policy_that_exposes_sibling_source(
    tmp_path: Path, fixtures, monkeypatch,
) -> None:
    case = tmp_path / "case"
    seat = case / "seats" / "claude"
    sibling = case / "seats" / "codex"
    fixtures.materialize("forge-01", seat, FIXTURES)
    fixtures.materialize("forge-01", sibling, FIXTURES)
    sibling_source = sibling / "src" / "transfer.py"
    monkeypatch.setattr(fixtures, "_SANDBOX_EXEC", Path(sys.executable))

    def probe(_policy, source, *args):
        if source == "import sys;open(sys.argv[1],'rb').read(1)":
            return 0
        if "TemporaryDirectory" in source:
            return 0
        if args == (str(sibling_source),):
            return 0
        return 17

    monkeypatch.setattr(fixtures, "_sandbox_probe", probe)
    with pytest.raises(fixtures.FixtureError, match="sibling"):
        _fixture_probe_argv(fixtures, "forge-01", seat, [sys.executable, "-c", "pass"])


def test_executor_preflight_refuses_a_hardlink_alias_to_sibling_source(
    tmp_path: Path, fixtures, monkeypatch,
) -> None:
    case = tmp_path / "case"
    seat = case / "seats" / "claude"
    sibling = case / "seats" / "codex"
    fixtures.materialize("forge-01", seat, FIXTURES)
    fixtures.materialize("forge-01", sibling, FIXTURES)
    (tmp_path / "sibling-hardlink.py").hardlink_to(sibling / "src" / "transfer.py")
    monkeypatch.setattr(fixtures, "_SANDBOX_EXEC", Path(sys.executable))

    def probe(_policy, source, *_args):
        if source == "import sys;open(sys.argv[1],'rb').read(1)":
            return 0
        if "TemporaryDirectory" in source:
            return 0
        return 17

    monkeypatch.setattr(fixtures, "_sandbox_probe", probe)
    with pytest.raises(fixtures.FixtureError, match="hard-link.*sibling"):
        _fixture_probe_argv(fixtures, "forge-01", seat, [sys.executable, "-c", "pass"])


def test_executor_boundary_refuses_missing_sandbox(tmp_path: Path, fixtures, monkeypatch):
    seat = tmp_path / "forge-01"
    fixtures.materialize("forge-01", seat, FIXTURES)
    monkeypatch.setattr(fixtures, "_SANDBOX_EXEC", tmp_path / "absent-sandbox")
    with pytest.raises(fixtures.FixtureError, match="sandbox"):
        _fixture_probe_argv(fixtures, "forge-01", seat, [sys.executable, "-c", "pass"])


def test_executor_boundary_refuses_a_seat_that_cannot_write_or_stage(
    tmp_path: Path, fixtures, monkeypatch,
):
    seat = tmp_path / "forge-01"
    fixtures.materialize("forge-01", seat, FIXTURES)
    monkeypatch.setattr(fixtures, "_SANDBOX_EXEC", Path(sys.executable))

    def probe(_policy, source, *_args):
        if "TemporaryDirectory" in source:
            return 1
        if source == "import sys;open(sys.argv[1],'rb').read(1)":
            return 0
        return 17

    monkeypatch.setattr(fixtures, "_sandbox_probe", probe)
    with pytest.raises(fixtures.FixtureError, match="write|stage"):
        _fixture_probe_argv(fixtures, "forge-01", seat, [sys.executable, "-c", "pass"])


@pytest.mark.parametrize("store", ["objects", ".git/objects"])
def test_executor_boundary_refuses_hardlinked_git_objects(
    tmp_path: Path, fixtures, monkeypatch, store: str,
):
    seat = tmp_path / "forge-01"
    fixtures.materialize("forge-01", seat, FIXTURES)
    common = tmp_path / "git-common"
    worktree = tmp_path / "worktree"
    common.mkdir()
    worktree.mkdir()
    object_root = common if store == "objects" else worktree
    git_object = object_root / store / "ab" / ("1" * 38)
    git_object.parent.mkdir(parents=True)
    git_object.write_bytes(b"synthetic object")
    (tmp_path / "outside-alias").hardlink_to(git_object)
    assert git_object.stat().st_nlink == 2

    monkeypatch.setattr(fixtures, "_SANDBOX_EXEC", Path(sys.executable))
    monkeypatch.setattr(fixtures, "_protected_roots", lambda _root: (common, worktree))

    def probe(_policy, source, *_args):
        if source == "import sys;open(sys.argv[1],'rb').read(1)":
            return 0
        if "TemporaryDirectory" in source:
            return 0
        return 17

    monkeypatch.setattr(fixtures, "_sandbox_probe", probe)
    with pytest.raises(fixtures.FixtureError, match="hard-link.*Git object"):
        _fixture_probe_argv(fixtures, "forge-01", seat, [sys.executable, "-c", "pass"])


def test_executor_boundary_does_not_treat_worktree_content_as_git_objects(
    tmp_path: Path, fixtures, monkeypatch,
):
    seat = tmp_path / "forge-01"
    fixtures.materialize("forge-01", seat, FIXTURES)
    common = tmp_path / "git-common"
    (common / "objects").mkdir(parents=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: shared worktree metadata\n", encoding="utf-8")
    content = worktree / "objects" / "data"
    content.parent.mkdir()
    content.write_bytes(b"public content")
    (tmp_path / "public-alias").hardlink_to(content)

    monkeypatch.setattr(fixtures, "_SANDBOX_EXEC", Path(sys.executable))
    monkeypatch.setattr(fixtures, "_protected_roots", lambda _root: (common, worktree))

    def probe(_policy, source, *_args):
        if source == "import sys;open(sys.argv[1],'rb').read(1)":
            return 0
        if "TemporaryDirectory" in source:
            return 0
        return 17

    monkeypatch.setattr(fixtures, "_sandbox_probe", probe)
    command = _fixture_probe_argv(fixtures, "forge-01", seat, [sys.executable, "-c", "pass"])
    assert command[-3:] == [sys.executable, "-c", "pass"]


@pytest.mark.parametrize("case_id,owned_path", [
    ("forge-09", "src/parser.py"),
    ("forge-10", "src/formatter.py"),
])
def test_ingress_packet_preserves_owned_path_check_and_skill_route(
    tmp_path: Path, fixtures, case_id: str, owned_path: str,
):
    packet = fixtures.ingress_packet(case_id, FIXTURES)
    assert packet["schema_version"] == "fanout-bundle-ingress-v1"
    task = packet["draft"]["tasks"][0]
    assert task["owned_paths"] == [owned_path]
    assert task["required_skills"] == ["khenrix-quality"]
    assert task["checks"][0]["argv"] == ["python3", "-c", (
        f"compile(open('{owned_path}', encoding='utf-8').read(), '{owned_path}', 'exec')"
    )]
    bundle = tmp_path / "bundle.json"
    bundle.write_text(json.dumps(packet), encoding="utf-8")
    binding = hashlib.sha256((json.dumps(packet, sort_keys=True, separators=(",", ":"),
                                         ensure_ascii=False) + "\n").encode()).hexdigest()
    review = tmp_path / "review.json"
    review.write_text(json.dumps({"schema_version": "fanout-owner-review-v1",
                                  "reviewer": "fixture-owner", "binding_sha256": binding}),
                      encoding="utf-8")
    skills = tmp_path / "skills" / "khenrix-quality"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("# Quality\n", encoding="utf-8")
    cli = ROOT / "shared" / "skills" / "llm-fanout-plan" / "scripts" / "plan.py"
    result = subprocess.run([sys.executable, str(cli), "from-bundle", "--bundle", str(bundle),
                             "--owner-review-file", str(review), "--skill-root", str(skills.parent)],
                            cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    admitted = json.loads(result.stdout)["compiled"]["plan"]["tasks"][0]
    assert admitted["owned_paths"] == task["owned_paths"]
    assert admitted["checks"] == task["checks"]
