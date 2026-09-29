"""The immutable inputs that define the Council and Forge comparison baseline."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fanout_baseline  # noqa: E402


def test_committed_baseline_lock_matches_the_real_legacy_surface():
    """Legacy source or receipt drift must fail the deterministic test gate."""
    assert fanout_baseline.main([
        "check",
        "--root",
        str(ROOT),
        "--lock",
        "evals/llm-fanout/baseline-lock.json",
    ]) == 0


def test_committed_baseline_lock_check_rejects_controlled_drift(tmp_path):
    """The gate's real-repository check must not accept a changed expected digest."""
    lock = json.loads((ROOT / "evals/llm-fanout/baseline-lock.json").read_text())
    lock["sources"]["shared/lib/council/engine.py"] = "0" * 64
    drifted_lock = tmp_path / "baseline-lock.json"
    drifted_lock.write_text(json.dumps(lock), encoding="utf-8")

    assert fanout_baseline.main([
        "check",
        "--root",
        str(ROOT),
        "--lock",
        str(drifted_lock),
    ]) == 1


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
