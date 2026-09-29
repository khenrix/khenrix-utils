#!/usr/bin/env python3
"""Measure no-retry fanout operations through hermetic or live smoke runs."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
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


def measure_live_sample(
    root: Path, sample_id: str, *, authorized: bool = False,
    run_sample: Callable[[Path], dict[str, object]] | None = None,
    clock_ns: Callable[[], int] | None = None,
) -> dict[str, object]:
    """Time one cold-reopened live smoke and bind its exact six-turn receipt."""
    if authorized is not True:
        raise BenchmarkError("live sample requires explicit authorization")
    _six_turn_policy()
    runner = run_sample or (lambda path: fanout_smoke.run_live(path, authorized=True))
    clock = clock_ns or time.perf_counter_ns
    start = clock()
    supplied = Path(root)
    if supplied.is_symlink():
        raise BenchmarkError("live benchmark root cannot be a symlink")
    root = supplied.resolve()
    receipt = runner(root)
    elapsed = clock() - start
    if (not isinstance(receipt, dict)
            or receipt.get("schema_version") != "fanout-live-smoke-receipt-v1"
            or receipt.get("receipt_sha256") != _digest({
                key: value for key, value in receipt.items() if key != "receipt_sha256"
            })
            or receipt.get("run_id") != fanout_smoke._live_run_id(root)
            or receipt.get("source_sha256") != fanout_smoke._live_source_hash()):
        raise BenchmarkError(f"{sample_id}: live smoke receipt is unbound")
    if _read_private_json(root / "receipt.json") != receipt:
        raise BenchmarkError(f"{sample_id}: live smoke receipt differs from its root")
    memory_controller = receipt.get("memory_controller_sha256")
    if not _is_digest(memory_controller):
        raise BenchmarkError(f"{sample_id}: installed memory controller digest is missing")
    return {
        **_validated_sample(sample_id, receipt, elapsed),
        "run_id": receipt["run_id"],
        "sample_root_sha256": hashlib.sha256(os.fsencode(root)).hexdigest(),
        "smoke_receipt_sha256": receipt["receipt_sha256"],
        "memory_controller_sha256": memory_controller,
    }


def _p95(values: list[float]) -> float:
    return sorted(values)[math.ceil(0.95 * len(values)) - 1]


def _digest(value: object) -> str:
    return hashlib.sha256(fanout_smoke.fanout.canonical_json(value)).hexdigest()


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _read_private_json(path: Path) -> dict[str, object]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_size > 64 * 1024):
                raise BenchmarkError("benchmark evidence file is unsafe")
            raw = os.read(descriptor, 64 * 1024 + 1)
        finally:
            os.close(descriptor)
        value = json.loads(raw)
    except (OSError, ValueError, UnicodeError) as error:
        raise BenchmarkError("benchmark evidence file is unavailable") from error
    if not isinstance(value, dict):
        raise BenchmarkError("benchmark evidence file is not an object")
    return value


def _require_live_source_pin(manifest: dict[str, object]) -> None:
    if (fanout_smoke._live_source_hash() != manifest["source_sha256"]
            or hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != manifest["driver_sha256"]):
        raise BenchmarkError("live benchmark source changed before paid work or delivery")


def _validate_sample(sample: object, *, mode: str, sample_id: str) -> None:
    base = {"sample_id", "candidate_ms", "turns", "retries", "settled_replays",
            "peer_transfers", "caller_unchanged", "source_sha256"}
    live = {"run_id", "sample_root_sha256", "smoke_receipt_sha256",
            "memory_controller_sha256"}
    if not isinstance(sample, dict) or set(sample) != base | (live if mode == "live" else set()):
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
    if mode == "live" and (
        not _is_digest(sample["sample_root_sha256"])
        or not _is_digest(sample["smoke_receipt_sha256"])
        or not _is_digest(sample["memory_controller_sha256"])
        or sample["run_id"] != f"fanout-smoke-{sample['sample_root_sha256'][:24]}"
    ):
        raise BenchmarkError(f"{sample_id}: live operation sample identity changed")


def _summary(samples: list[dict[str, object]], *, mode: str = "hermetic",
             driver_sha256: str | None = None) -> dict[str, object]:
    if not isinstance(mode, str) or mode not in {"hermetic", "live"}:
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
    roots = {sample["sample_root_sha256"] for sample in samples} if mode == "live" else set()
    controllers = {sample["memory_controller_sha256"] for sample in samples} if mode == "live" else set()
    if len(set(ids)) != len(ids) or len(sources) != 1 or (mode == "live" and len(roots) != len(samples)):
        raise BenchmarkError("operation samples reuse an ID or mix runtime source revisions")
    if mode == "live" and len(controllers) != 1:
        raise BenchmarkError("operation samples mix installed memory controller revisions")
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
        "memory_controller_sha256": next(iter(controllers)) if mode == "live" else None,
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


def run_live_benchmark(
    root: Path | str, *, authorized: bool = False, sample_count: int = 30,
    resume: bool = False,
    run_sample: Callable[[Path], dict[str, object]] | None = None,
    clock_ns: Callable[[], int] | None = None,
) -> dict[str, object]:
    """Measure thirty paid six-turn runs; quality and cost remain separate gates."""
    if authorized is not True:
        raise BenchmarkError("live benchmark requires explicit authorization")
    if type(sample_count) is not int or sample_count < 30:
        raise BenchmarkError("at least 30 validated operation samples are required")
    supplied = Path(root)
    if supplied.is_symlink():
        raise BenchmarkError("live benchmark root cannot be a symlink")
    root = supplied.resolve()
    fanout_smoke._live_root(root / ".eligibility", new=True)
    manifest = {
        "schema_version": "fanout-live-benchmark-manifest-v1",
        "sample_count": sample_count,
        "source_sha256": fanout_smoke._live_source_hash(),
        "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    records = root / "samples"
    if resume:
        if (not root.is_dir() or root.is_symlink()
                or stat.S_IMODE(root.stat().st_mode) != 0o700
                or root.stat().st_uid != os.getuid()
                or _read_private_json(root / "manifest.json") != manifest
                or not records.is_dir() or records.is_symlink()
                or stat.S_IMODE(records.stat().st_mode) != 0o700
                or records.stat().st_uid != os.getuid()):
            raise BenchmarkError("live benchmark resume manifest is unavailable or changed")
    else:
        try:
            root.mkdir(mode=0o700)
        except FileExistsError as error:
            raise BenchmarkError("benchmark root already exists; explicit resume is required") from error
        records.mkdir(mode=0o700)
        fanout_smoke._write_new(root / "manifest.json", fanout_smoke.fanout.canonical_json(manifest))
    samples = []
    for index in range(sample_count):
        sample_id = f"op-{index + 1:04d}"
        sample_root = root / sample_id
        record = records / f"{sample_id}.json"
        if record.exists() or record.is_symlink():
            sample = _read_private_json(record)
            _validate_sample(sample, mode="live", sample_id=sample_id)
            if sample["source_sha256"] != manifest["source_sha256"]:
                raise BenchmarkError(f"{sample_id}: resumed sample source changed")
            if (sample_root.is_symlink() or not sample_root.is_dir()
                    or sample["sample_root_sha256"] != hashlib.sha256(
                        os.fsencode(sample_root.resolve(strict=True))).hexdigest()
                    or sample["run_id"] != fanout_smoke._live_run_id(sample_root.resolve(strict=True))):
                raise BenchmarkError(f"{sample_id}: resumed sample root differs from its evidence")
            smoke = _read_private_json(sample_root / "receipt.json")
            if (smoke.get("receipt_sha256") != sample["smoke_receipt_sha256"]
                    or smoke.get("run_id") != sample["run_id"]
                    or smoke.get("source_sha256") != sample["source_sha256"]
                    or smoke.get("schema_version") != "fanout-live-smoke-receipt-v1"
                    or smoke.get("memory_controller_sha256") != sample["memory_controller_sha256"]
                    or smoke.get("receipt_sha256") != _digest({
                        key: value for key, value in smoke.items() if key != "receipt_sha256"
                    })):
                raise BenchmarkError(f"{sample_id}: resumed live smoke receipt changed")
            _validated_sample(sample_id, smoke, 1)
        else:
            if sample_root.exists() or sample_root.is_symlink():
                raise BenchmarkError(f"{sample_id}: uncertain paid sample; never replay automatically")
            _require_live_source_pin(manifest)
            sample = measure_live_sample(
                sample_root, sample_id, authorized=True,
                run_sample=run_sample, clock_ns=clock_ns,
            )
            fanout_smoke._write_new(record, fanout_smoke.fanout.canonical_json(sample))
        if samples and sample["memory_controller_sha256"] != samples[0]["memory_controller_sha256"]:
            raise BenchmarkError(f"{sample_id}: installed memory controller changed")
        samples.append(sample)
    _require_live_source_pin(manifest)
    receipt = _summary(samples, mode="live")
    verify_benchmark_receipt(receipt)
    fanout_smoke._write_or_verify_exact(
        root / "receipt.json", fanout_smoke.fanout.canonical_json(receipt), "benchmark receipt",
    )
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--hermetic", action="store_true", help="Measure fake providers and memory")
    mode.add_argument("--live", action="store_true", help="Measure paid live profiles")
    parser.add_argument("--root", type=Path,
                        help="Fresh benchmark root; live mode also accepts FANOUT_BENCHMARK_ROOT")
    parser.add_argument("--samples", type=int, default=30, help="At least 30 operation samples")
    parser.add_argument("--allow-paid", action="store_true",
                        help="Authorize live provider spend; or set FANOUT_LIVE_ALLOW_PAID=1")
    parser.add_argument("--resume", action="store_true",
                        help="Continue only from verified completed live samples")
    arguments = parser.parse_args(argv)
    try:
        if arguments.live:
            root = arguments.root or os.environ.get("FANOUT_BENCHMARK_ROOT")
            if root is None:
                raise BenchmarkError("live benchmark requires --root and --allow-paid")
            receipt = run_live_benchmark(
                Path(root),
                authorized=arguments.allow_paid or os.environ.get("FANOUT_LIVE_ALLOW_PAID") == "1",
                sample_count=arguments.samples, resume=arguments.resume,
            )
        elif arguments.allow_paid or arguments.resume:
            raise BenchmarkError("--allow-paid and --resume apply only to --live")
        elif arguments.root is None:
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
