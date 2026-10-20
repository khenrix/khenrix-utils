#!/usr/bin/env python3
"""Measure no-retry fanout operations through hermetic smoke runs."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

import fanout_smoke


class BenchmarkError(RuntimeError):
    """A measured operation or its evidence violated the benchmark contract."""


def _six_turn_policy() -> None:
    policy = fanout_smoke._plan().tasks[0].provider_policy
    if (
        len(policy.executor_ids) != 3 or policy.rounds != 2
        or policy.retries != 0 or policy.quality_tier != "standard"
    ):
        raise BenchmarkError("hermetic smoke no longer has a standard six-turn no-retry policy")


def _validated_sample(
    sample_id: str, receipt: dict[str, object], duration_ns: int,
) -> dict[str, object]:
    if not isinstance(receipt, dict) or receipt.get("status") != "pass":
        raise BenchmarkError(f"{sample_id}: smoke did not pass")
    for field, expected in (
        ("provider_turns", 6), ("peer_transfers", 6),
        ("replayed_provider_turns", 0), ("source_count", 3),
    ):
        if type(receipt.get(field)) is not int or receipt[field] != expected:
            raise BenchmarkError(f"{sample_id}: expected six no-retry turns and no settled replay ({field})")
    if receipt.get("caller_unchanged") is not True:
        raise BenchmarkError(f"{sample_id}: caller repository changed")
    source = receipt.get("source_sha256")
    if not isinstance(source, str) or len(source) != 64 or any(
        char not in "0123456789abcdef" for char in source
    ):
        raise BenchmarkError(f"{sample_id}: runtime source digest is missing")
    if type(duration_ns) is not int or duration_ns <= 0:
        raise BenchmarkError(f"{sample_id}: measured duration must be positive")
    return {
        "sample_id": sample_id, "candidate_ms": duration_ns / 1_000_000,
        "turns": 6, "retries": 0, "settled_replays": 0,
        "peer_transfers": 6, "caller_unchanged": True,
        "source_sha256": source,
    }


def measure_hermetic_sample(
    root: Path, sample_id: str, *,
    run_sample: Callable[[Path], dict[str, object]] | None = None,
    clock_ns: Callable[[], int] | None = None,
) -> dict[str, object]:
    """Time a fresh three-seat smoke and validate its durable six-turn receipt."""
    _six_turn_policy()
    runner = run_sample or fanout_smoke.run_hermetic
    clock = clock_ns or time.perf_counter_ns
    start = clock()
    receipt = runner(Path(root))
    elapsed = clock() - start
    return _validated_sample(sample_id, receipt, elapsed)


def _p95(values: list[float]) -> float:
    return sorted(values)[math.ceil(0.95 * len(values)) - 1]


def _digest(value: object) -> str:
    return hashlib.sha256(fanout_smoke.fanout.canonical_json(value)).hexdigest()


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _validate_sample(sample: object, *, mode: str, sample_id: str) -> None:
    base = {"sample_id", "candidate_ms", "turns", "retries", "settled_replays",
            "peer_transfers", "caller_unchanged", "source_sha256"}
    if not isinstance(sample, dict) or set(sample) != base:
        raise BenchmarkError(f"{sample_id}: operation sample schema changed")
    duration = sample["candidate_ms"]
    try:
        valid_duration = type(duration) in (int, float) and math.isfinite(duration) and duration > 0
    except OverflowError:
        valid_duration = False
    if (sample["sample_id"] != sample_id
            or not valid_duration
            or any(type(sample[key]) is not int or sample[key] != expected
                   for key, expected in (("turns", 6), ("retries", 0),
                                         ("settled_replays", 0), ("peer_transfers", 6)))
            or sample["caller_unchanged"] is not True
            or not _is_digest(sample["source_sha256"])):
        raise BenchmarkError(f"{sample_id}: operation sample semantics changed")


def _summary(samples: list[dict[str, object]], *, mode: str = "hermetic",
             driver_sha256: str | None = None) -> dict[str, object]:
    if not isinstance(mode, str) or mode != "hermetic":
        raise BenchmarkError("operation benchmark mode is invalid")
    if driver_sha256 is None:
        driver_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if not _is_digest(driver_sha256):
        raise BenchmarkError("operation benchmark driver digest is invalid")
    if len(samples) < 30:
        raise BenchmarkError("at least 30 validated operation samples are required")
    for index, sample in enumerate(samples, 1):
        _validate_sample(sample, mode=mode, sample_id=f"op-{index:04d}")
    ids = [sample["sample_id"] for sample in samples]
    sources = {sample["source_sha256"] for sample in samples}
    if len(set(ids)) != len(ids) or len(sources) != 1:
        raise BenchmarkError("operation samples reuse an ID or mix runtime source revisions")
    receipt = {
        "schema_version": "fanout-operation-benchmark-v1",
        "mode": mode, "status": "pass", "certification_ready": False,
        "measured_samples": len(samples),
        "candidate_p95_ms": _p95([sample["candidate_ms"] for sample in samples]),
        "baseline_p95_ms": None,
        "standard_no_retry_samples": len(samples),
        "total_provider_turns": sum(sample["turns"] for sample in samples),
        "settled_replays": sum(sample["settled_replays"] for sample in samples),
        "cost_usd": None,
        "runtime_source_sha256": next(iter(sources)),
        "memory_controller_sha256": None,
        "driver_sha256": driver_sha256,
        "samples_sha256": _digest(samples), "samples": samples,
    }
    receipt["receipt_sha256"] = _digest(receipt)
    return receipt


def verify_benchmark_receipt(receipt: dict[str, object]) -> None:
    """Reject altered sample data or summary fields in a stored receipt."""
    if not isinstance(receipt, dict) or not isinstance(receipt.get("samples"), list):
        raise BenchmarkError("benchmark receipt digest is unavailable")
    if receipt.get("samples_sha256") != _digest(receipt["samples"]):
        raise BenchmarkError("benchmark sample digest changed")
    body = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    if receipt.get("receipt_sha256") != _digest(body):
        raise BenchmarkError("benchmark receipt digest changed")
    if receipt != _summary(
        receipt["samples"], mode=receipt.get("mode"),
        driver_sha256=receipt.get("driver_sha256"),
    ):
        raise BenchmarkError("benchmark summary metrics changed")


def run_hermetic_benchmark(
    root: Path | str, *, sample_count: int = 30,
    run_sample: Callable[[Path], dict[str, object]] | None = None,
    clock_ns: Callable[[], int] | None = None,
) -> dict[str, object]:
    """Create a new benchmark root; a reused root can never replay settled turns."""
    if type(sample_count) is not int or sample_count < 30:
        raise BenchmarkError("at least 30 validated operation samples are required")
    root = Path(root).resolve()
    try:
        root.mkdir(mode=0o700)
    except FileExistsError as error:
        raise BenchmarkError("benchmark root already exists; duplicate resume is refused") from error
    samples = []
    for index in range(sample_count):
        sample_id = f"op-{index + 1:04d}"
        sample = measure_hermetic_sample(
            root / sample_id, sample_id, run_sample=run_sample,
            clock_ns=clock_ns,
        )
        samples.append(sample)
    receipt = _summary(samples)
    verify_benchmark_receipt(receipt)
    payload = fanout_smoke.fanout.canonical_json(receipt)
    fanout_smoke._write_new(root / "receipt.json", payload)
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermetic", action="store_true", required=True,
                        help="Measure fake providers and memory; never starts models")
    parser.add_argument("--root", type=Path, help="Fresh benchmark root")
    parser.add_argument("--samples", type=int, default=30, help="At least 30 operation samples")
    arguments = parser.parse_args(argv)
    try:
        if arguments.root is None:
            with tempfile.TemporaryDirectory(prefix="fanout-benchmark-") as temporary:
                receipt = run_hermetic_benchmark(
                    Path(temporary) / "benchmark", sample_count=arguments.samples,
                )
        else:
            receipt = run_hermetic_benchmark(arguments.root, sample_count=arguments.samples)
    except (BenchmarkError, fanout_smoke.SmokeError, OSError, ValueError) as error:
        print(f"fanout benchmark failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
