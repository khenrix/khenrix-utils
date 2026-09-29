"""Durable, contained artifact contracts for the fanout runtime."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import multiprocessing
import os
import stat
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FANOUT_ROOT = ROOT / "shared" / "lib" / "fanout"
SPEC = importlib.util.spec_from_file_location(
    "_fanout_artifact_contracts",
    FANOUT_ROOT / "__init__.py",
    submodule_search_locations=[str(FANOUT_ROOT)],
)
assert SPEC and SPEC.loader
fanout = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fanout
SPEC.loader.exec_module(fanout)

ArtifactExistsError = fanout.ArtifactExistsError
ArtifactIntegrityError = fanout.ArtifactIntegrityError
ArtifactLimits = fanout.ArtifactLimits
ArtifactPathError = fanout.ArtifactPathError
ArtifactQuotaError = fanout.ArtifactQuotaError
ArtifactStore = fanout.ArtifactStore
canonical_json = fanout.canonical_json
artifacts = sys.modules[f"{SPEC.name}.artifacts"]


def _quota_writer(root: str, start: multiprocessing.synchronize.Event,
                  barrier: multiprocessing.synchronize.Barrier,
                  lock_barrier: multiprocessing.synchronize.Barrier,
                  results: multiprocessing.queues.Queue) -> None:
    """One independently constructed store participating in a quota race."""
    store = ArtifactStore(root, limits=ArtifactLimits(max_file_bytes=4, max_total_bytes=4))
    count = store._stored_bytes
    write_lock = store._write_lock

    def count_then_wait(root_fd: int) -> int:
        total = count(root_fd)
        try:
            barrier.wait(timeout=2)
        except threading.BrokenBarrierError:
            pass
        return total

    store._stored_bytes = count_then_wait

    @contextmanager
    def enter_lock_together():
        lock_barrier.wait(timeout=5)
        with write_lock() as root_fd:
            yield root_fd

    store._write_lock = enter_lock_together
    start.wait(10)
    try:
        store.write_bytes(f"{os.getpid()}.bin", b"four")
    except ArtifactQuotaError:
        results.put("quota")
    else:
        results.put("written")
    finally:
        store.close()


def test_canonical_json_is_utf8_sorted_compact_and_newline_terminated():
    """Changing serialization options would change durable artifact digests."""
    assert canonical_json({"å": "värde", "z": [2, 1], "a": True}) == (
        b'{"a":true,"z":[2,1],"\xc3\xa5":"v\xc3\xa4rde"}\n'
    )


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_canonical_json_rejects_non_finite_numbers(value):
    """Permitting non-JSON numeric values would make artifact interchange ambiguous."""
    with pytest.raises(ValueError):
        canonical_json({"value": value})


def test_write_returns_a_digest_bound_reference_and_read_verifies_it(tmp_path):
    """Returning an unbound path would let later consumers accept substituted bytes."""
    store = ArtifactStore(tmp_path)

    ref = store.write_bytes("seats/claude/result.txt", b"durable answer\n")

    assert ref.path == "seats/claude/result.txt"
    assert ref.digest == hashlib.sha256(b"durable answer\n").hexdigest()
    assert ref.size == len(b"durable answer\n")
    assert store.read_bytes(ref) == b"durable answer\n"


def test_new_artifact_root_and_nested_terminal_parents_are_fsynced(tmp_path, monkeypatch):
    """A journal terminal must not outlive a newly created artifact ancestor after a crash."""
    root = tmp_path / "run" / "artifacts"
    fsynced_directories = set()
    real_fsync = os.fsync

    def observe_fsync(fd):
        info = os.fstat(fd)
        if stat.S_ISDIR(info.st_mode):
            fsynced_directories.add((info.st_dev, info.st_ino))
        return real_fsync(fd)

    monkeypatch.setattr(artifacts.os, "fsync", observe_fsync)
    with ArtifactStore(root) as store:
        ref = store.write_bytes("branch-handovers/terminal/work/evidence.json", b"commit evidence\n")
        assert store.read_bytes(ref) == b"commit evidence\n"

    created_entry_parents = (
        tmp_path, tmp_path / "run", root,
        root / "branch-handovers", root / "branch-handovers" / "terminal",
        root / "branch-handovers" / "terminal" / "work",
    )
    missing = [
        str(parent) for parent in created_entry_parents
        if (parent.stat().st_dev, parent.stat().st_ino) not in fsynced_directories
    ]
    assert missing == []


def test_open_existing_artifact_root_never_creates_missing_components(tmp_path):
    root = tmp_path / "missing" / "artifacts"
    with pytest.raises(ArtifactPathError):
        ArtifactStore.open_existing(root)
    assert not (tmp_path / "missing").exists()

    root.mkdir(parents=True)
    with ArtifactStore(root) as writer:
        ref = writer.write_bytes("plans/replacement.json", b"{}\n")
    with ArtifactStore.open_existing(root) as reader:
        assert reader.read_bytes(ref) == b"{}\n"

    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(ArtifactPathError):
        ArtifactStore.open_existing(alias)


def test_write_json_uses_canonical_bytes(tmp_path):
    """A JSON writer that bypasses canonical serialization would give unstable digests."""
    store = ArtifactStore(tmp_path)

    ref = store.write_json("run.json", {"b": 2, "a": 1})

    assert store.read_bytes(ref) == b'{"a":1,"b":2}\n'
    assert store.read_json(ref) == {"a": 1, "b": 2}


def test_write_is_exclusive_and_leaves_original_bytes_intact(tmp_path):
    """Replacing an existing artifact would permit a settled turn to be rewritten."""
    store = ArtifactStore(tmp_path)
    ref = store.write_bytes("events/terminal.json", b"first")

    with pytest.raises(ArtifactExistsError):
        store.write_bytes("events/terminal.json", b"second")

    assert store.read_bytes(ref) == b"first"


def test_duplicate_content_at_a_different_name_is_a_distinct_reference(tmp_path):
    """Deduplicating names from content alone would lose producer provenance."""
    store = ArtifactStore(tmp_path)

    first = store.write_bytes("claude/output.txt", b"same")
    second = store.write_bytes("codex/output.txt", b"same")

    assert first.digest == second.digest
    assert first.path != second.path
    assert store.read_bytes(first) == store.read_bytes(second) == b"same"


@pytest.mark.parametrize("name", ["", ".", "../outside", "/absolute", "a/../../outside"])
def test_write_rejects_paths_outside_the_run_root(tmp_path, name):
    """A traversal spelling would let a provider artifact escape its owned run."""
    store = ArtifactStore(tmp_path / "run")

    with pytest.raises(ArtifactPathError):
        store.write_bytes(name, b"blocked")


def test_write_rejects_a_parent_symlink_that_escapes_the_run_root(tmp_path):
    """Resolving only the final path would let an internal symlink redirect writes."""
    store = ArtifactStore(tmp_path / "run")
    outside = tmp_path / "outside"
    outside.mkdir()
    (store.root / "seats").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ArtifactPathError):
        store.write_bytes("seats/claude.txt", b"blocked")

    assert not (outside / "claude.txt").exists()


def test_root_swap_cannot_redirect_the_lock_or_artifact_publication(tmp_path):
    """Reopening the root pathname after setup would let a symlink redirect a write."""
    root = tmp_path / "run"
    store = ArtifactStore(root)
    pinned_root = tmp_path / "pinned-run"
    outside = tmp_path / "outside"
    outside.mkdir()
    root.rename(pinned_root)
    root.symlink_to(outside, target_is_directory=True)
    try:
        ref = store.write_bytes("result.txt", b"pinned")

        assert (pinned_root / ref.path).read_bytes() == b"pinned"
        assert not (outside / ".artifact.lock").exists()
        assert not (outside / "result.txt").exists()
    finally:
        store.close()


def test_constructor_rejects_a_root_swapped_for_an_outside_symlink(tmp_path, monkeypatch):
    """Resolving after mkdir would pin an attacker-selected symlink target instead of the root."""
    root = tmp_path / "run"
    outside = tmp_path / "outside"
    outside.mkdir()
    original_mkdir = artifacts.os.mkdir

    def create_then_swap(name, *args, **kwargs):
        result = original_mkdir(name, *args, **kwargs)
        if name == root.name:
            root.rmdir()
            root.symlink_to(outside, target_is_directory=True)
        return result

    monkeypatch.setattr(artifacts.os, "mkdir", create_then_swap)

    with pytest.raises(ArtifactPathError):
        ArtifactStore(root)

    assert list(outside.iterdir()) == []


def test_read_rejects_a_digest_mismatch_after_artifact_tampering(tmp_path):
    """Reading a named file without hashing it would silently accept changed evidence."""
    store = ArtifactStore(tmp_path)
    ref = store.write_bytes("result.txt", b"verified")
    (store.root / ref.path).write_bytes(b"tampered")

    with pytest.raises(ArtifactIntegrityError):
        store.read_bytes(ref)


def test_interrupted_publication_never_exposes_a_partial_artifact(tmp_path, monkeypatch):
    """A failed publish must not leave a reader-visible prefix of the intended bytes."""
    store = ArtifactStore(tmp_path)

    def interrupt(*_args, **_kwargs):
        raise OSError("simulated interruption")

    monkeypatch.setattr(artifacts.os, "write", interrupt)

    with pytest.raises(OSError, match="simulated interruption"):
        store.write_bytes("result.txt", b"unpublished")

    assert not (store.root / "result.txt").exists()
    assert not list(store.root.rglob("*.tmp"))


def test_crashed_post_fsync_write_recovers_orphan_without_spending_quota(tmp_path):
    """A crash after durable staging must not retain invisible bytes against the next write."""
    root = tmp_path / "run"
    child = "\n".join([
        "import os",
        "import stat",
        "import sys",
        f"sys.path.insert(0, {str(ROOT / 'shared' / 'lib')!r})",
        "from fanout import ArtifactLimits, ArtifactStore",
        "from fanout import artifacts",
        "original_fsync = artifacts.os.fsync",
        "def crash_after_staging(fd):",
        "    original_fsync(fd)",
        "    if stat.S_ISREG(os.fstat(fd).st_mode):",
        "        os._exit(97)",
        "artifacts.os.fsync = crash_after_staging",
        f"store = ArtifactStore({str(root)!r}, limits=ArtifactLimits(max_file_bytes=4, max_total_bytes=4))",
        "store.write_bytes('crashed.bin', b'four')",
    ])

    crashed = subprocess.run([sys.executable, "-c", child], check=False)

    assert crashed.returncode == 97
    assert not (root / "crashed.bin").exists()
    assert list((root / ".fanout-artifact-tmp").glob("*.tmp"))

    store = ArtifactStore(root, limits=ArtifactLimits(max_file_bytes=4, max_total_bytes=4))
    try:
        ref = store.write_bytes("next.bin", b"four")
        assert store.read_bytes(ref) == b"four"
        assert not list((root / ".fanout-artifact-tmp").glob("*.tmp"))
    finally:
        store.close()


def test_file_quota_rejects_oversize_data_without_creating_an_artifact(tmp_path):
    """Checking a per-file cap after publication would retain disallowed bytes."""
    store = ArtifactStore(tmp_path, limits=ArtifactLimits(max_file_bytes=3, max_total_bytes=10))

    with pytest.raises(ArtifactQuotaError, match="file"):
        store.write_bytes("large.bin", b"four")

    assert not (store.root / "large.bin").exists()


def test_aggregate_quota_counts_prior_artifacts_and_rejects_overflow(tmp_path):
    """Counting only the new payload would allow many small artifacts to bypass the cap."""
    store = ArtifactStore(tmp_path, limits=ArtifactLimits(max_file_bytes=10, max_total_bytes=5))
    first = store.write_bytes("one.bin", b"abc")

    with pytest.raises(ArtifactQuotaError, match="total"):
        store.write_bytes("two.bin", b"def")

    assert store.read_bytes(first) == b"abc"
    assert not (store.root / "two.bin").exists()


def test_duplicate_name_wins_over_a_saturated_total_quota(tmp_path):
    """A duplicate is immutable-name evidence, not a capacity failure."""
    store = ArtifactStore(tmp_path, limits=ArtifactLimits(max_file_bytes=3, max_total_bytes=3))
    store.write_bytes("same.bin", b"abc")

    with pytest.raises(ArtifactExistsError):
        store.write_bytes("same.bin", b"abc")


def test_write_rejects_a_final_symlink_before_quota_accounting(tmp_path):
    """A final symlink must never be classified by following or replacing it."""
    root = tmp_path / "run"
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    store = ArtifactStore(root, limits=ArtifactLimits(max_file_bytes=3, max_total_bytes=3))
    (root / "same.bin").symlink_to(outside)

    with pytest.raises(ArtifactPathError):
        store.write_bytes("same.bin", b"abc")

    assert outside.read_bytes() == b"outside"


def test_transient_first_lock_creation_enoent_retries_without_leaving_the_root(tmp_path, monkeypatch):
    """A filesystem's first-create race must not be misclassified as an unsafe lock path."""
    store = ArtifactStore(tmp_path)
    original_open = artifacts.os.open
    injected = False

    def transient_open(path, flags, *args, **kwargs):
        nonlocal injected
        if path == ".artifact.lock" and flags & os.O_CREAT and not injected:
            injected = True
            raise FileNotFoundError("simulated first-create race")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(artifacts.os, "open", transient_open)

    ref = store.write_bytes("result.bin", b"safe")

    assert injected
    assert store.read_bytes(ref) == b"safe"


def test_lock_rejects_a_nonprivate_regular_file(tmp_path):
    """Accepting a user-visible lock inode would let another process interfere with exclusion."""
    root = tmp_path / "run"
    ArtifactStore(root).close()
    lock = root / ".artifact.lock"
    lock.write_bytes(b"")
    lock.chmod(0o644)
    store = ArtifactStore(root)

    with pytest.raises(ArtifactPathError, match="private regular"):
        store.write_bytes("result.bin", b"safe")


@pytest.mark.parametrize("attempt", range(5))
def test_concurrent_stores_admit_only_one_write_that_fits_alone(tmp_path, attempt):
    """Dropping the shared lock would let two writers both pass aggregate accounting."""
    root = tmp_path / "run"
    ArtifactStore(root).close()
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    barrier = context.Barrier(2)
    lock_barrier = context.Barrier(2)
    results = context.Queue()
    processes = [
        context.Process(target=_quota_writer, args=(str(root), start, barrier, lock_barrier, results))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(15)
        assert process.exitcode == 0

    outcomes = sorted(results.get(timeout=5) for _ in processes)
    files = [path for path in root.iterdir() if path.name.endswith(".bin")]

    assert outcomes == ["quota", "written"]
    assert sum(path.stat().st_size for path in files) == 4


def test_read_rejects_a_final_symlink_even_when_its_textual_path_is_contained(tmp_path):
    """Following a swapped final symlink would make verification read attacker-controlled bytes."""
    store = ArtifactStore(tmp_path / "run")
    ref = store.write_bytes("result.txt", b"verified")
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")
    (store.root / ref.path).unlink()
    (store.root / ref.path).symlink_to(outside)

    with pytest.raises(ArtifactPathError):
        store.read_bytes(ref)


def test_read_json_rejects_invalid_stored_json_after_digest_verification(tmp_path):
    """A future consumer must not receive a parsing exception unrelated to artifact identity."""
    store = ArtifactStore(tmp_path)
    payload = b"not-json"
    ref = store.write_bytes("bad.json", payload)

    with pytest.raises(json.JSONDecodeError):
        store.read_json(ref)
