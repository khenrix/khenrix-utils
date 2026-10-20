"""Operational benchmark evidence from the hermetic fanout smoke path."""
from __future__ import annotations

import hashlib
import copy
import json
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
