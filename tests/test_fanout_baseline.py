"""The immutable inputs that define the Council and Forge comparison baseline."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fanout_baseline  # noqa: E402
import fanout_smoke  # noqa: E402
from test_fanout_container_upstream import connection_factory  # noqa: F401
from test_fanout_readonly_canaries import canaries, _forbid_reopen_sends_and_probes


def test_committed_baseline_lock_matches_the_explicit_original_historical_subject():
    """The historical source gate requires an explicit, safe original snapshot."""
    assert fanout_baseline.main([
        "check-historical",
        "--root",
        str(ROOT),
        "--lock",
        "evals/llm-fanout/baseline-lock.json",
    ]) == 0


def test_live_read_only_profile_canaries_match_the_current_runtime():
    """A new profile or runtime must not inherit an old live certification."""
    evidence = json.loads((ROOT / "evals/llm-fanout/read-only-profile-canaries.json").read_text())
    _assert_current_readonly_canaries(evidence)


def _assert_current_readonly_canaries(evidence):
    assert evidence["source_sha256"] == fanout_smoke._live_source_hash()
    assert evidence["schema_version"] == "fanout-readonly-canaries-v1"
    assert evidence["status"] == "pass"
    assert evidence["release_binding"] == fanout_smoke.release_source_binding().to_dict()
    assert set(evidence["tiers"]) == {"standard", "deep"}
    for canary in evidence["tiers"].values():
        assert canary["status"] == "pass"
        for name in ("raw_receipt_sha256", "receipt_sha256"):
            _assert_sha256(canary[name])
        assert canary["raw_receipt_sha256"] != canary["receipt_sha256"]
        assert set(canary["providers"]) == set(fanout_smoke.EXECUTORS)
        for observed in canary["providers"].values():
            _assert_sha256(observed["profile_sha256"])
            assert isinstance(observed["requested_model"], str) and observed["requested_model"].strip()
            assert observed["observed_model"] == observed["requested_model"]


def _assert_sha256(value):
    assert isinstance(value, str) and len(value) == 64
    assert all(character in "0123456789abcdef" for character in value)


@pytest.mark.parametrize("canaries", ["template", "observed"], indirect=True)
def test_fresh_current_version_canary_publication_passes_the_strict_gate(canaries, monkeypatch):
    helper, receipts = canaries
    _forbid_reopen_sends_and_probes(helper, monkeypatch)
    summary = helper.summarize_readonly_canaries(*receipts)
    _assert_current_readonly_canaries(summary)


@pytest.mark.parametrize("canaries", ["template"], indirect=True)
@pytest.mark.parametrize("damage", ["source", "release", "tier", "status", "raw-hash",
                                     "unsigned-hash", "profile-hash", "provider", "requested", "observed"])
def test_strict_gate_refuses_damaged_fresh_canary_publication(canaries, damage):
    helper, receipts = canaries
    summary = helper.summarize_readonly_canaries(*receipts)
    tier = summary["tiers"]["deep"]
    codex = tier["providers"]["codex"]
    if damage == "source":
        summary["source_sha256"] = "0" * 64
    elif damage == "release":
        summary["release_binding"]["digest"] = "0" * 64
    elif damage == "tier":
        summary["tiers"].pop("standard")
    elif damage == "status":
        tier["status"] = "fail"
    elif damage == "raw-hash":
        tier["raw_receipt_sha256"] = "z" * 64
    elif damage == "unsigned-hash":
        tier["receipt_sha256"] = None
    elif damage == "profile-hash":
        codex["profile_sha256"] = "z" * 64
    elif damage == "provider":
        tier["providers"].pop("agy")
    elif damage == "requested":
        codex["requested_model"] = ""
    else:
        codex["observed_model"] = None
    with pytest.raises(AssertionError):
        _assert_current_readonly_canaries(summary)


def test_committed_baseline_lock_check_rejects_controlled_drift(tmp_path):
    """The gate's real-repository check must not accept a changed expected digest."""
    lock = json.loads((ROOT / "evals/llm-fanout/baseline-lock.json").read_text())
    lock["sources"]["shared/lib/council/engine.py"] = "0" * 64
    drifted_lock = tmp_path / "baseline-lock.json"
    drifted_lock.write_text(json.dumps(lock), encoding="utf-8")

    assert fanout_baseline.main([
        "check-historical",
        "--root",
        str(ROOT),
        "--lock",
        str(drifted_lock),
    ]) == 2


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _fixture_tree(tmp_path: Path) -> Path:
    _write(tmp_path, "shared/lib/council/engine.py", "council\n")
    _write(tmp_path, "shared/lib/forge/engine.py", "forge\n")
    _write(tmp_path, "shared/skills/llm-council/SKILL.md", "council skill\n")
    _write(tmp_path, "shared/skills/llm-forge/SKILL.md", "forge skill\n")
    _write(tmp_path, "evals/llm-council/evals.json", "{}\n")
    _write(tmp_path, "evals/llm-council/receipt.json", "{\"ok\": true}\n")
    _write(tmp_path, "evals/llm-forge/evals.json", "{\"forge\": true}\n")
    _write(tmp_path, "evals/llm-forge/fixtures/sample.txt", "fixture\n")
    _write(tmp_path, "evals/llm-forge/receipt.json", "{\"ok\": true}\n")
    return tmp_path


def test_build_lock_is_deterministic_and_hashes_all_baseline_surfaces(tmp_path):
    """Dropping a legacy input from the lock would let it change undetected."""
    root = _fixture_tree(tmp_path)

    first = fanout_baseline.build_lock(root)
    second = fanout_baseline.build_lock(root)

    assert first == second
    assert first["schema_version"] == 2
    assert first["sources"] == {
        "shared/lib/council/engine.py": hashlib.sha256(b"council\n").hexdigest(),
        "shared/lib/forge/engine.py": hashlib.sha256(b"forge\n").hexdigest(),
        "shared/skills/llm-council/SKILL.md": hashlib.sha256(b"council skill\n").hexdigest(),
        "shared/skills/llm-forge/SKILL.md": hashlib.sha256(b"forge skill\n").hexdigest(),
    }
    assert first["fixtures"] == {
        "evals/llm-council/evals.json": hashlib.sha256(b"{}\n").hexdigest(),
        "evals/llm-forge/evals.json": hashlib.sha256(b'{"forge": true}\n').hexdigest(),
        "evals/llm-forge/fixtures/sample.txt": hashlib.sha256(b"fixture\n").hexdigest(),
    }
    assert first["receipts"] == {
        "evals/llm-council/receipt.json": hashlib.sha256(b'{"ok": true}\n').hexdigest(),
        "evals/llm-forge/receipt.json": hashlib.sha256(b'{"ok": true}\n').hexdigest(),
    }
    assert "help" not in first


def test_check_lock_reports_the_changed_surface_and_never_changes_it(tmp_path):
    """A source mismatch must identify the immutable input that invalidated comparison."""
    root = _fixture_tree(tmp_path)
    expected = fanout_baseline.build_lock(root)
    path = root / "evals/llm-forge/fixtures/sample.txt"
    before = path.read_bytes()
    path.write_text("changed fixture\n", encoding="utf-8")

    mismatches = fanout_baseline.check_lock(expected, root)

    assert mismatches == ["fixtures: evals/llm-forge/fixtures/sample.txt"]
    assert path.read_bytes() == b"changed fixture\n"
    assert before != path.read_bytes()


def test_deterministic_lock_does_not_invoke_installed_clis(tmp_path, monkeypatch):
    """An installed CLI update must not make the source baseline stale."""
    root = _fixture_tree(tmp_path)
    monkeypatch.setattr(
        fanout_baseline,
        "installed_help",
        lambda _command: (_ for _ in ()).throw(AssertionError("called CLI")),
    )
    assert "help" not in fanout_baseline.build_lock(root)


def test_opt_in_characterization_records_versions_and_help_hashes():
    """A changed CLI help or version must stale only the opt-in receipt."""
    help_bytes = lambda command: f"{command} --help\n".encode("utf-8")
    version_bytes = lambda command: f"{command} 1.2.3\n".encode("utf-8")
    expected = fanout_baseline.build_characterization(
        help_runner=help_bytes, version_runner=version_bytes
    )

    assert expected == {
        "schema_version": 1,
        "clis": {
            command: {
                "version": f"{command} 1.2.3",
                "help_sha256": hashlib.sha256(help_bytes(command)).hexdigest(),
            }
            for command in ("agy", "claude", "codex")
        },
    }
    assert fanout_baseline.check_characterization(
        expected, help_runner=help_bytes, version_runner=version_bytes
    ) == []
    assert fanout_baseline.check_characterization(
        expected,
        help_runner=lambda command: b"new codex help\n" if command == "codex" else help_bytes(command),
        version_runner=version_bytes,
    ) == ["clis: codex.help_sha256"]
    assert fanout_baseline.check_characterization(
        expected,
        help_runner=help_bytes,
        version_runner=lambda command: b"claude 2.0\n" if command == "claude" else version_bytes(command),
    ) == ["clis: claude.version"]


def test_installed_cli_stderr_warning_does_not_change_characterization(monkeypatch):
    """A Codex alias warning must not become part of its version or help digest."""
    def run(_command, **options):
        assert options["stderr"] == subprocess.PIPE
        return subprocess.CompletedProcess(
            ["codex", "--version"], 0, b"codex-cli 0.156.1\n", b"alias warning\n"
        )

    monkeypatch.setattr(
        fanout_baseline.subprocess,
        "run",
        run,
    )
    assert fanout_baseline.installed_version("codex") == b"codex-cli 0.156.1\n"


def test_stderr_only_cli_help_is_characterized(monkeypatch):
    """Agy prints help to stderr even when it exits successfully."""
    monkeypatch.setattr(
        fanout_baseline.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["agy", "--help"], 0, b"", b"agy usage\n"
        ),
    )
    assert fanout_baseline.installed_help("agy") == b"agy usage\n"


@pytest.mark.parametrize("ambient_guard", [None, "false"])
@pytest.mark.parametrize("flag,expected", [("--version", b"1.3.1\n"),
                                          ("--help", b"agy usage\n")])
def test_real_agy_observation_disables_updates_without_changing_output(
    tmp_path, monkeypatch, ambient_guard, flag, expected
):
    binary = tmp_path / "agy"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "root = pathlib.Path(os.environ['HOME'])\n"
        "guard = os.environ.get('AGY_CLI_DISABLE_AUTO_UPDATE')\n"
        "(root / 'observation.json').write_text(json.dumps({'guard': guard, 'argv': sys.argv[1:]}))\n"
        "if guard != 'true': (root / 'updated').touch()\n"
        "if sys.argv[1:] == ['--version']: print('1.3.1')\n"
        "elif sys.argv[1:] == ['--help']: print('agy usage', file=sys.stderr)\n"
        "else: sys.exit(9)\n"
    )
    binary.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("AGY_CLI_DISABLE_AUTO_UPDATE", raising=False)
    if ambient_guard is not None:
        monkeypatch.setenv("AGY_CLI_DISABLE_AUTO_UPDATE", ambient_guard)

    observe = fanout_baseline.installed_version if flag == "--version" else fanout_baseline.installed_help
    assert observe("agy") == expected
    assert json.loads((tmp_path / "observation.json").read_text()) == {
        "guard": "true", "argv": [flag],
    }
    assert not (tmp_path / "updated").exists()
    assert os.environ.get("AGY_CLI_DISABLE_AUTO_UPDATE") == ambient_guard
