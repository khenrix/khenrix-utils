"""Operational benchmark evidence from the hermetic fanout smoke path."""
from __future__ import annotations

import hashlib
import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import fanout_benchmark  # noqa: E402
import fanout_smoke  # noqa: E402


SOURCE_SHA256 = hashlib.sha256(b"fanout smoke fixture").hexdigest()


def _smoke_receipt() -> dict[str, object]:
    return {
        "status": "pass", "provider_turns": 6, "peer_transfers": 6,
        "replayed_provider_turns": 0, "source_count": 3,
        "caller_unchanged": True, "source_sha256": SOURCE_SHA256,
    }


def _live_smoke_receipt(
    root: Path, *, memory_controller_sha256: str = "a" * 64,
) -> dict[str, object]:
    root.mkdir(mode=0o700)
    receipt = {
        **_smoke_receipt(), "schema_version": "fanout-live-smoke-receipt-v1",
        "source_sha256": fanout_smoke._live_source_hash(),
        "run_id": fanout_smoke._live_run_id(root.resolve()),
        "memory_controller_sha256": memory_controller_sha256,
    }
    receipt["receipt_sha256"] = hashlib.sha256(
        fanout_smoke.fanout.canonical_json(receipt)
    ).hexdigest()
    (root / "receipt.json").write_bytes(fanout_smoke.fanout.canonical_json(receipt))
    (root / "receipt.json").chmod(0o600)
    return receipt


def _ticks(durations_ms: range) -> object:
    now = 0
    values = []
    for duration in durations_ms:
        values.extend((now, now + duration * 1_000_000))
        now += duration * 1_000_000 + 1
    return iter(values).__next__


def test_benchmark_measures_thirty_fresh_runs_and_uses_nearest_rank_p95(
    tmp_path: Path,
) -> None:
    roots = []

    def runner(root: Path) -> dict[str, object]:
        roots.append(root)
        return _smoke_receipt()

    receipt = fanout_benchmark.run_hermetic_benchmark(
        tmp_path / "benchmark", sample_count=30, run_sample=runner,
        clock_ns=_ticks(range(1, 31)),
    )

    assert len(set(roots)) == 30
    assert receipt["status"] == "pass"
    assert receipt["measured_samples"] == 30
    assert receipt["candidate_p95_ms"] == 29.0
    assert receipt["total_provider_turns"] == 180
    assert receipt["standard_no_retry_samples"] == 30
    assert receipt["settled_replays"] == 0
    assert receipt["baseline_p95_ms"] is None
    assert receipt["cost_usd"] is None
    assert receipt["certification_ready"] is False
    assert len(receipt["samples"]) == 30
    assert receipt["samples"][0]["candidate_ms"] == 1.0
    assert receipt["samples"][-1]["candidate_ms"] == 30.0
    assert receipt["samples_sha256"] == hashlib.sha256(
        fanout_smoke.fanout.canonical_json(receipt["samples"])
    ).hexdigest()
    fanout_benchmark.verify_benchmark_receipt(receipt)
    altered = copy.deepcopy(receipt)
    altered["samples"][0]["candidate_ms"] = 999.0
    with pytest.raises(fanout_benchmark.BenchmarkError, match="digest"):
        fanout_benchmark.verify_benchmark_receipt(altered)


def test_duplicate_benchmark_root_refuses_resume_before_any_run(tmp_path: Path) -> None:
    root = tmp_path / "benchmark"
    root.mkdir()

    def forbidden_runner(_root: Path) -> dict[str, object]:
        pytest.fail("duplicate benchmark started a provider turn")

    with pytest.raises(fanout_benchmark.BenchmarkError, match="already exists"):
        fanout_benchmark.run_hermetic_benchmark(
            root, run_sample=forbidden_runner, clock_ns=_ticks(range(1, 31)),
        )


def test_benchmark_rejects_insufficient_or_invalid_turn_samples(tmp_path: Path) -> None:
    with pytest.raises(fanout_benchmark.BenchmarkError, match="at least 30"):
        fanout_benchmark.run_hermetic_benchmark(tmp_path / "short", sample_count=29)
    assert not (tmp_path / "short").exists()

    bad = _smoke_receipt()
    bad["provider_turns"] = 7
    with pytest.raises(fanout_benchmark.BenchmarkError, match="six.*turn"):
        fanout_benchmark.run_hermetic_benchmark(
            tmp_path / "bad", run_sample=lambda _root: bad,
            clock_ns=_ticks(range(1, 31)),
        )


def test_one_measured_sample_executes_fake_providers_and_memory(tmp_path: Path) -> None:
    sample = fanout_benchmark.measure_hermetic_sample(tmp_path / "sample", "op-0001")

    assert sample["candidate_ms"] > 0
    assert sample["turns"] == 6
    assert sample["retries"] == 0
    assert sample["settled_replays"] == 0
    assert sample["peer_transfers"] == 6
    assert len(list((tmp_path / "sample" / "calls").glob("*.json"))) == 6
    assert len(list((tmp_path / "sample" / "fake-memory" / "observations").glob("*.json"))) == 6


def test_live_benchmark_requires_explicit_authorization_before_creation(tmp_path: Path) -> None:
    root = tmp_path / "live"

    with pytest.raises(fanout_benchmark.BenchmarkError, match="authorization"):
        fanout_benchmark.run_live_benchmark(
            root, run_sample=lambda _root: pytest.fail("unauthorized provider spend"),
        )

    assert not root.exists()


def test_direct_live_sample_requires_authorization_before_provider_call(tmp_path: Path) -> None:
    root = tmp_path / "sample"

    with pytest.raises(fanout_benchmark.BenchmarkError, match="authorization"):
        fanout_benchmark.measure_live_sample(
            root, "op-0001",
            run_sample=lambda _root: pytest.fail("unauthorized provider spend"),
        )

    assert not root.exists()


def test_live_benchmark_measures_thirty_real_receipt_shapes_without_certifying_quality(
    tmp_path: Path,
) -> None:
    roots = []

    def runner(root: Path) -> dict[str, object]:
        roots.append(root)
        return _live_smoke_receipt(root)

    receipt = fanout_benchmark.run_live_benchmark(
        tmp_path / "live", authorized=True, sample_count=30,
        run_sample=runner, clock_ns=_ticks(range(1, 31)),
    )

    assert len(set(roots)) == 30
    assert receipt["mode"] == "live"
    assert receipt["measured_samples"] == 30
    assert receipt["candidate_p95_ms"] == 29.0
    assert receipt["total_provider_turns"] == 180
    assert receipt["certification_ready"] is False
    assert receipt["baseline_p95_ms"] is None
    assert receipt["cost_usd"] is None
    fanout_benchmark.verify_benchmark_receipt(receipt)


def test_live_benchmark_rejects_unbound_receipt_before_recording_sample(tmp_path: Path) -> None:
    def invalid(root: Path) -> dict[str, object]:
        receipt = _live_smoke_receipt(root)
        receipt["receipt_sha256"] = "0" * 64
        return receipt

    with pytest.raises(fanout_benchmark.BenchmarkError, match="live smoke receipt"):
        fanout_benchmark.run_live_benchmark(
            tmp_path / "live", authorized=True,
            run_sample=invalid,
            clock_ns=_ticks(range(1, 31)),
        )


def test_live_benchmark_rejects_mixed_installed_memory_controllers(tmp_path: Path) -> None:
    calls = 0

    def runner(root: Path) -> dict[str, object]:
        nonlocal calls
        calls += 1
        digest = "a" * 64 if calls == 1 else "b" * 64
        return _live_smoke_receipt(root, memory_controller_sha256=digest)

    with pytest.raises(fanout_benchmark.BenchmarkError, match="memory controller"):
        fanout_benchmark.run_live_benchmark(
            tmp_path / "live", authorized=True,
            run_sample=runner, clock_ns=_ticks(range(1, 31)),
        )


def test_live_benchmark_resume_skips_completed_paid_samples(tmp_path: Path) -> None:
    root = tmp_path / "live"
    calls = []

    def interrupt(sample_root: Path) -> dict[str, object]:
        calls.append(sample_root.name)
        if sample_root.name == "op-0002":
            raise RuntimeError("simulated interruption before sample two")
        return _live_smoke_receipt(sample_root)

    with pytest.raises(RuntimeError, match="interruption"):
        fanout_benchmark.run_live_benchmark(
            root, authorized=True, run_sample=interrupt,
            clock_ns=_ticks(range(1, 31)),
        )

    assert calls == ["op-0001", "op-0002"]
    assert (root / "samples" / "op-0001.json").is_file()
    resumed = []
    receipt = fanout_benchmark.run_live_benchmark(
        root, authorized=True, resume=True,
        run_sample=lambda sample_root: (
            resumed.append(sample_root.name), _live_smoke_receipt(sample_root)
        )[1],
        clock_ns=_ticks(range(2, 31)),
    )

    assert resumed == [f"op-{index:04d}" for index in range(2, 31)]
    assert receipt["measured_samples"] == 30
    assert receipt["status"] == "pass"


def test_live_benchmark_resume_refuses_uncertain_paid_sample(tmp_path: Path) -> None:
    root = tmp_path / "live"

    def interrupt(sample_root: Path) -> dict[str, object]:
        _live_smoke_receipt(sample_root)
        raise RuntimeError("simulated crash after live receipt before benchmark row")

    with pytest.raises(RuntimeError, match="simulated crash"):
        fanout_benchmark.run_live_benchmark(root, authorized=True, run_sample=interrupt)

    with pytest.raises(fanout_benchmark.BenchmarkError, match="uncertain"):
        fanout_benchmark.run_live_benchmark(
            root, authorized=True, resume=True,
            run_sample=lambda _root: pytest.fail("settled sample was replayed"),
        )


def test_live_benchmark_resume_rechecks_smoke_outcome_and_turns(tmp_path: Path) -> None:
    root = tmp_path / "live"

    def interrupt(sample_root: Path) -> dict[str, object]:
        if sample_root.name == "op-0002":
            raise RuntimeError("stop before sample two")
        return _live_smoke_receipt(sample_root)

    with pytest.raises(RuntimeError, match="stop before"):
        fanout_benchmark.run_live_benchmark(
            root, authorized=True, run_sample=interrupt,
            clock_ns=_ticks(range(1, 31)),
        )
    smoke_path = root / "op-0001" / "receipt.json"
    row_path = root / "samples" / "op-0001.json"
    smoke = json.loads(smoke_path.read_bytes())
    smoke.update(status="fail", provider_turns=0)
    smoke["receipt_sha256"] = fanout_benchmark._digest({
        key: value for key, value in smoke.items() if key != "receipt_sha256"
    })
    smoke_path.write_bytes(fanout_smoke.fanout.canonical_json(smoke))
    row = json.loads(row_path.read_bytes())
    row["smoke_receipt_sha256"] = smoke["receipt_sha256"]
    row_path.write_bytes(fanout_smoke.fanout.canonical_json(row))

    with pytest.raises(fanout_benchmark.BenchmarkError, match="six|smoke|turn"):
        fanout_benchmark.run_live_benchmark(
            root, authorized=True, resume=True,
            run_sample=lambda _root: pytest.fail("invalid resumed sample started paid work"),
        )


def test_live_benchmark_fifo_evidence_fails_without_blocking(tmp_path: Path) -> None:
    path = tmp_path / "evidence.json"
    os.mkfifo(path, 0o600)
    program = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "import fanout_benchmark; "
        "fanout_benchmark._read_private_json(__import__('pathlib').Path(sys.argv[2]))"
    )
    result = subprocess.run(
        [sys.executable, "-c", program, str(ROOT / "scripts"), str(path)],
        text=True, capture_output=True, timeout=2, check=False,
    )
    assert result.returncode != 0
    assert "benchmark evidence file is unsafe" in result.stderr


def test_live_benchmark_resume_refuses_symlinked_root(tmp_path: Path) -> None:
    root = tmp_path / "live"
    fanout_benchmark.run_live_benchmark(
        root, authorized=True, run_sample=_live_smoke_receipt,
        clock_ns=_ticks(range(1, 31)),
    )
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)

    with pytest.raises(fanout_benchmark.BenchmarkError, match="symlink"):
        fanout_benchmark.run_live_benchmark(
            alias, authorized=True, resume=True,
            run_sample=lambda _root: pytest.fail("symlinked resume launched provider"),
        )


def test_live_benchmark_resume_rejects_swapped_sample_evidence(tmp_path: Path) -> None:
    root = tmp_path / "live"
    fanout_benchmark.run_live_benchmark(
        root, authorized=True, run_sample=_live_smoke_receipt,
        clock_ns=_ticks(range(1, 31)),
    )
    first_row = root / "samples" / "op-0001.json"
    second_row = root / "samples" / "op-0002.json"
    first = json.loads(first_row.read_bytes())
    second = json.loads(second_row.read_bytes())
    first_row.write_bytes(fanout_smoke.fanout.canonical_json({**second, "sample_id": "op-0001"}))
    second_row.write_bytes(fanout_smoke.fanout.canonical_json({**first, "sample_id": "op-0002"}))
    first_receipt = root / "op-0001" / "receipt.json"
    second_receipt = root / "op-0002" / "receipt.json"
    first_bytes, second_bytes = first_receipt.read_bytes(), second_receipt.read_bytes()
    first_receipt.write_bytes(second_bytes)
    second_receipt.write_bytes(first_bytes)
    (root / "receipt.json").unlink()

    with pytest.raises(fanout_benchmark.BenchmarkError, match="sample root|sample path"):
        fanout_benchmark.run_live_benchmark(
            root, authorized=True, resume=True,
            run_sample=lambda _root: pytest.fail("swapped evidence launched provider"),
        )


def test_live_benchmark_checks_source_pin_before_each_paid_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = []
    source = iter(("a" * 64, "b" * 64))
    monkeypatch.setattr(fanout_smoke, "_live_source_hash", lambda: next(source, "b" * 64))

    with pytest.raises(fanout_benchmark.BenchmarkError, match="source changed"):
        fanout_benchmark.run_live_benchmark(
            tmp_path / "live", authorized=True,
            run_sample=lambda _root: observed.append("spent"),
        )

    assert observed == []


def test_live_benchmark_rejects_reused_root_receipt(tmp_path: Path) -> None:
    first: dict[str, object] | None = None

    def replay(sample_root: Path) -> dict[str, object]:
        nonlocal first
        if first is None:
            first = _live_smoke_receipt(sample_root)
            return first
        assert first is not None
        sample_root.mkdir(mode=0o700)
        (sample_root / "receipt.json").write_bytes(fanout_smoke.fanout.canonical_json(first))
        (sample_root / "receipt.json").chmod(0o600)
        return first

    with pytest.raises(fanout_benchmark.BenchmarkError, match="unbound"):
        fanout_benchmark.run_live_benchmark(
            tmp_path / "live", authorized=True, run_sample=replay,
            clock_ns=_ticks(range(1, 31)),
        )


def test_benchmark_verifier_recomputes_metrics_even_when_digest_is_resealed(tmp_path: Path) -> None:
    receipt = fanout_benchmark.run_hermetic_benchmark(
        tmp_path / "benchmark", sample_count=30,
        run_sample=lambda _root: _smoke_receipt(),
        clock_ns=_ticks(range(1, 31)),
    )
    altered = copy.deepcopy(receipt)
    altered["candidate_p95_ms"] = 0.0
    altered["receipt_sha256"] = fanout_benchmark._digest({
        key: value for key, value in altered.items() if key != "receipt_sha256"
    })

    with pytest.raises(fanout_benchmark.BenchmarkError, match="metric|summary|p95"):
        fanout_benchmark.verify_benchmark_receipt(altered)


def test_benchmark_verifier_rejects_resealed_nonstring_mode(tmp_path: Path) -> None:
    receipt = fanout_benchmark.run_hermetic_benchmark(
        tmp_path / "benchmark", sample_count=30,
        run_sample=lambda _root: _smoke_receipt(),
        clock_ns=_ticks(range(1, 31)),
    )
    altered = copy.deepcopy(receipt)
    altered["mode"] = []
    altered["receipt_sha256"] = fanout_benchmark._digest({
        key: value for key, value in altered.items() if key != "receipt_sha256"
    })

    with pytest.raises(fanout_benchmark.BenchmarkError, match="mode"):
        fanout_benchmark.verify_benchmark_receipt(altered)


def test_benchmark_verifier_rejects_resealed_huge_duration(tmp_path: Path) -> None:
    receipt = fanout_benchmark.run_hermetic_benchmark(
        tmp_path / "benchmark", sample_count=30,
        run_sample=lambda _root: _smoke_receipt(),
        clock_ns=_ticks(range(1, 31)),
    )
    altered = copy.deepcopy(receipt)
    altered["samples"][0]["candidate_ms"] = 10 ** 400
    altered["samples_sha256"] = fanout_benchmark._digest(altered["samples"])
    altered["receipt_sha256"] = fanout_benchmark._digest({
        key: value for key, value in altered.items() if key != "receipt_sha256"
    })

    with pytest.raises(fanout_benchmark.BenchmarkError, match="duration|sample"):
        fanout_benchmark.verify_benchmark_receipt(altered)


def test_archived_benchmark_metrics_remain_verifiable_after_driver_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = fanout_benchmark.run_hermetic_benchmark(
        tmp_path / "benchmark", sample_count=30,
        run_sample=lambda _root: _smoke_receipt(),
        clock_ns=_ticks(range(1, 31)),
    )
    later_driver = tmp_path / "later-driver.py"
    later_driver.write_text("# changed driver\n", encoding="utf-8")
    monkeypatch.setattr(fanout_benchmark, "__file__", str(later_driver))

    fanout_benchmark.verify_benchmark_receipt(receipt)


def test_live_mode_fails_closed_without_authorization(capsys: pytest.CaptureFixture[str]) -> None:
    assert fanout_benchmark.main(["--live"]) == 1
    assert "--root" in capsys.readouterr().err


def test_make_target_can_opt_in_with_explicit_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "live"
    monkeypatch.setenv("FANOUT_BENCHMARK_ROOT", str(root))
    monkeypatch.setenv("FANOUT_LIVE_ALLOW_PAID", "1")
    seen = []

    def run(target: Path, *, authorized: bool, sample_count: int,
            resume: bool) -> dict[str, object]:
        seen.append((target, authorized, sample_count, resume))
        return {"mode": "live", "status": "pass"}

    monkeypatch.setattr(fanout_benchmark, "run_live_benchmark", run)

    assert fanout_benchmark.main(["--live"]) == 0
    assert seen == [(root, True, 30, False)]
    assert '"mode": "live"' in capsys.readouterr().out
