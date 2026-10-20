"""Native evaluator transport failures retain evidence and never replay spend."""
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import eval_harness
from test_eval_native_consumers import native_cli


def run_fake(tmp_path, home, provider, prompt="Answer briefly.", timeout=3):
    effort = "high" if provider == "agy" else "medium"
    cfg = {provider: {"model": "fixture-model", "thinking": effort}}
    answer, record = eval_harness.run_text(
        provider, prompt, cfg, tmp_path / "artifacts", timeout=timeout, retries=2,
        readonly=True, exec_cwd=tmp_path / "cwd", real_home=home)
    path = home / "launches.jsonl"
    launches = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    return answer, record, launches


@pytest.mark.parametrize("provider", ["claude", "codex", "agy"])
def test_eval_accepts_short_answers_with_native_identity(tmp_path, native_cli, provider):
    answer, record, launches = run_fake(tmp_path, native_cli, provider)
    assert answer == "installed read verified"
    assert record["valid"] and record["reason"] == "ok"
    assert len(launches) == 1 and "Answer briefly." in launches[0]["prompt"]
    assert "Answer briefly." not in launches[0]["argv"]
    assert record["session_id"]
    assert Path(record["result_file"]).read_text() == answer


@pytest.mark.parametrize("provider", ["claude", "codex", "agy"])
def test_paid_provider_failure_is_terminal_and_keeps_raw_capture(tmp_path, native_cli, provider):
    answer, record, launches = run_fake(tmp_path, native_cli, provider, "provider-error")
    assert answer == "" and not record["valid"] and record["reason"] == "provider-error"
    assert len(launches) == 1 and record["attempts"] == 1
    assert "quota" in Path(record["stdout_file"]).read_text()


def test_native_unstarted_spawn_failure_is_not_automatically_retried(tmp_path, native_cli, monkeypatch):
    process = sys.modules[eval_harness.eval_runtime.__package__ + ".process"]
    calls = []

    def fail_spawn(*args, **kwargs):
        calls.append(1)
        raise FileNotFoundError("synthetic unstarted spawn")

    monkeypatch.setattr(process.subprocess, "Popen", fail_spawn)
    answer, record, launches = run_fake(tmp_path, native_cli, "codex")
    assert answer == "" and not record["valid"]
    assert len(calls) == 1 and record["attempts"] == 1 and not launches
    assert Path(record["stdout_file"]).read_bytes() == b""


@pytest.mark.parametrize("provider", ["claude", "codex", "agy"])
@pytest.mark.parametrize("prompt,timeout,reason", [
    ("malformed-case", 3, "protocol-error"), ("slow-case", 0.15, "timeout"),
])
def test_possible_paid_protocol_failure_or_timeout_is_never_replayed(
    tmp_path, native_cli, provider, prompt, timeout, reason,
):
    answer, record, launches = run_fake(tmp_path, native_cli, provider, prompt, timeout)
    assert answer == "" and not record["valid"] and record["reason"] == reason
    assert record["attempts"] == 1 and len(launches) == 1
    assert Path(record["stdout_file"]).read_bytes()


def test_native_role_rejects_unapproved_effort_before_launch(tmp_path, native_cli):
    with pytest.raises(RuntimeError, match="effort differs from its role"):
        eval_harness.run_text("codex", "Answer",
            {"codex": {"model": "fixture-model", "thinking": "ultra"}},
            tmp_path / "artifacts", timeout=3, retries=0, readonly=True,
            exec_cwd=tmp_path / "cwd", real_home=native_cli)
    assert not (native_cli / "launches.jsonl").exists()
