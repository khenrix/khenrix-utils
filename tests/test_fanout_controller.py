"""Controller evidence publication and held-root durability contracts."""
from __future__ import annotations

import errno
import hashlib
import importlib.util
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "shared/lib/fanout"
SPEC = importlib.util.spec_from_file_location("_fanout_controller_contracts", PACKAGE / "__init__.py",
                                            submodule_search_locations=[str(PACKAGE)])
assert SPEC and SPEC.loader
fanout = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fanout
SPEC.loader.exec_module(fanout)
controller = sys.modules[f"{SPEC.name}.controller"]


def test_partial_staged_write_never_publishes_final_record(tmp_path, monkeypatch):
    issued = fanout.create_lifecycle_controller(tmp_path / "controller")
    real_write = os.write
    calls = []
    def fail_after_prefix(fd, data):
        if not calls:
            calls.append(True)
            return real_write(fd, data[:7])
        raise OSError(errno.ENOSPC, "injected disk full")
    monkeypatch.setattr(controller.os, "write", fail_after_prefix)
    with pytest.raises((OSError, fanout.LifecycleError)):
        controller.write_evidence(issued, "test", {"value": "complete"})
    assert list((issued.root / "receipts").iterdir()) == []


def test_controller_publication_is_exclusive_and_matching_reuse_requires_durability(tmp_path, monkeypatch):
    issued = fanout.create_lifecycle_controller(tmp_path / "controller")
    reference = controller.write_evidence(issued, "test", {"value": "complete"})
    real_fsync = os.fsync
    def fail_directory(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("injected directory fsync failure")
        real_fsync(fd)
    monkeypatch.setattr(controller.os, "fsync", fail_directory)
    with pytest.raises((OSError, fanout.LifecycleError)):
        controller.write_evidence(issued, "test", {"value": "complete"})
    monkeypatch.setattr(controller.os, "fsync", real_fsync)
    assert controller.write_evidence(issued, "test", {"value": "complete"}) == reference
    assert controller.read_evidence(issued, "test", *reference) == {"value": "complete"}
    root_fd = os.open(issued.root / "receipts", os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(fanout.LifecycleError):
            controller._write_new(root_fd, reference[0], b"conflict")
    finally:
        os.close(root_fd)
    assert controller.read_evidence(issued, "test", *reference) == {"value": "complete"}


def test_postvisibility_directory_failure_stays_refused_then_exact_reuse_recovers(tmp_path, monkeypatch):
    issued = fanout.create_lifecycle_controller(tmp_path / "controller")
    real_fsync = os.fsync
    receipts_identity = (issued.root / "receipts").stat().st_ino
    def fail_receipts(fd):
        if os.fstat(fd).st_ino == receipts_identity:
            raise OSError("postvisibility directory failure")
        real_fsync(fd)
    monkeypatch.setattr(controller.os, "fsync", fail_receipts)
    with pytest.raises((OSError, fanout.LifecycleError)):
        controller.write_evidence(issued, "test", {"value": "settled"})
    files = list((issued.root / "receipts").glob("*.json"))
    assert len(files) == 1
    raw = files[0].read_bytes()
    monkeypatch.setattr(controller.os, "fsync", real_fsync)
    reference = controller.write_evidence(issued, "test", {"value": "settled"})
    assert reference == (files[0].name, hashlib.sha256(raw).hexdigest())


def test_complete_evidence_envelope_exact_limit_and_one_byte_over(tmp_path):
    issued = fanout.create_lifecycle_controller(tmp_path / "controller")
    reference = controller.write_evidence(issued, "test", {"value": ""})
    overhead = len((issued.root / "receipts" / reference[0]).read_bytes())
    exact = {"value": "x" * (65536 - overhead)}
    reference = controller.write_evidence(issued, "test", exact)
    assert (issued.root / "receipts" / reference[0]).stat().st_size == 65536
    assert controller.read_evidence(issued, "test", *reference) == exact
    before = sorted((issued.root / "receipts").iterdir())
    with pytest.raises(fanout.LifecycleError, match="limit"):
        controller.write_evidence(issued, "test", {"value": exact["value"] + "x"})
    assert sorted((issued.root / "receipts").iterdir()) == before


@pytest.mark.parametrize("name", ["controller.json", "evidence"])
def test_controller_fifo_read_refuses_within_deadline(tmp_path, name):
    issued = fanout.create_lifecycle_controller(tmp_path / "controller")
    directory = issued.root if name == "controller.json" else issued.root / "receipts"
    if name == "controller.json":
        (directory / name).unlink()
    os.mkfifo(directory / name, 0o600)
    code = (f"import os,sys; sys.path.insert(0,{str(ROOT / 'shared/lib')!r}); "
            f"from fanout import controller; fd=os.open({str(directory)!r},os.O_RDONLY|os.O_DIRECTORY); "
            + ("controller._read_record(fd)" if name == "controller.json" else "controller._read_named(fd,'evidence')"))
    try:
        result = subprocess.run((sys.executable, "-c", code), capture_output=True, timeout=1)
    except subprocess.TimeoutExpired:
        pytest.fail("FIFO open blocked before regular-file validation")
    assert result.returncode != 0 and b"LifecycleError" in result.stderr


@pytest.mark.parametrize("reading", [False, True])
def test_root_swap_at_privileged_child_open_stays_in_authenticated_root(tmp_path, monkeypatch, reading):
    issued = fanout.create_lifecycle_controller(tmp_path / "controller")
    reference = controller.write_evidence(issued, "test", {"value": "original"})
    outside = tmp_path / "outside"
    (outside / "receipts").mkdir(parents=True)
    pinned = tmp_path / "pinned"
    real_open = os.open
    swapped = False
    def swap_at_receipts(path, flags, *args, **kwargs):
        nonlocal swapped
        if not swapped and (path == "receipts" or path == issued.root / "receipts"):
            issued.root.rename(pinned)
            issued.root.symlink_to(outside, target_is_directory=True)
            swapped = True
        return real_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(controller.os, "open", swap_at_receipts)
    try:
        if reading:
            assert controller.read_evidence(issued, "test", *reference) == {"value": "original"}
        else:
            controller.write_evidence(issued, "test", {"value": "held-root"})
    except fanout.LifecycleError:
        pass
    assert swapped
    assert list((outside / "receipts").iterdir()) == []
