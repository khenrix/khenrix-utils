"""File-backed scheduler and execution authority within disposable run roots."""
from __future__ import annotations

import hashlib
import importlib.util
import fcntl
import json
import multiprocessing
import os
import shutil
import stat
import sys
from dataclasses import replace
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
FANOUT_ROOT = ROOT / "shared" / "lib" / "fanout"
SPEC = importlib.util.spec_from_file_location(
    "_fanout_storage_contracts", FANOUT_ROOT / "__init__.py",
    submodule_search_locations=[str(FANOUT_ROOT)],
)
assert SPEC and SPEC.loader
fanout = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fanout
SPEC.loader.exec_module(fanout)


_OWNER_TOKEN = "storage-test-owner-capability-0123456789"


def _digest(value: bytes | str) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def _plan():
    return fanout.FanoutPlanV1.from_dict({
        "schema_version": "v1",
        "source": {
            "path": "docs/superpowers/plans/storage.md",
            "sha256": _digest("storage source"),
            "parser_version": "parser-v1",
        },
        "defaults": {
            "executor_ids": ["claude", "codex"], "rounds": 1,
            "timeout": 10, "retries": 0, "minimum_success": 2,
        },
        "source_steps": [{"id": "Task 1/Step 1", "sha256": _digest("approval step")}],
        "tasks": [{
            "id": "approval", "kind": "work", "parent_id": None,
            "title": "Approval", "objective": "Record owner approval.",
            "source_step_ids": ["Task 1/Step 1"], "depends_on": [],
            "execution_class": "orchestrator-action", "required_skills": [],
            "none_reason": "The owner performs this action.",
            "owned_paths": [], "acceptance": ["Approval is recorded."],
            "checks": [], "provider_policy": None,
        }],
    })


def _inputs(plan):
    return fanout.RunInputs(
        run_id="run-storage",
        compiled_plan_sha256=_digest(fanout.canonical_json(plan.to_dict())),
        source_sha256=plan.source.sha256,
        draft_sha256=_digest("draft"),
        compiler_sha256=_digest("compiler"),
        parser_sha256=_digest("parser"),
        provider_profiles={"claude": _digest("claude"), "codex": _digest("codex")},
        skill_manifests={},
        repo_baseline_sha256=None,
    )


def _owner():
    return fanout.OwnerCapability.from_token(_OWNER_TOKEN)


def test_file_scheduler_reopens_typed_v2_handover_dependency(tmp_path):
    import test_fanout_scheduler as scheduler_cases

    scheduler, memory_backend, journal, controller, baseline, owner = (
        scheduler_cases._v2_handover_fixture(tmp_path)
    )
    scheduler.schedule_ready(owner=owner)
    scheduler.mark_active("address", owner=owner)
    scheduler.begin_reconciliation("address", owner=owner)
    bundle = scheduler_cases.fanout.CandidateBundle(baseline.digest, (), ())
    scheduler_cases._reconcile_v2_candidate(scheduler, memory_backend, bundle, owner)
    terminal = scheduler_cases._deliver_v2_candidate(
        scheduler, memory_backend, journal, controller, baseline, owner,
    )
    scheduler.schedule_ready(owner=owner)
    file_backend = scheduler_cases.fanout.FileSchedulerBackend.create(
        journal.root, run_id=scheduler.inputs.run_id, owner=owner,
    )
    snapshot = replace(
        memory_backend.record.snapshot,
        backend_identity=file_backend.identity(), backend_key=file_backend.key(),
        backend_revision=1, previous_commit="0" * 64,
    )
    file_backend.compare_and_set(0, snapshot, owner=owner)
    file_backend.close()
    reopened = scheduler_cases.fanout.FileSchedulerBackend.resume(
        journal.root, run_id=scheduler.inputs.run_id, owner=owner,
    )
    stored = reopened.read().snapshot
    assert stored == snapshot
    booking = next(state for state in stored.tasks if state.task_id == "booking")
    assert booking.dependencies[0].kind == "handover"
    assert booking.dependencies[0].artifact == terminal.evidence
    assert booking.dependencies[0].receipt_sha256 == terminal.terminal_sha256


def _backend(kind: str):
    return getattr(fanout, "FileSchedulerBackend" if kind == "scheduler" else "FileExecutionBackend")


def _snapshot(kind: str, backend, plan, inputs, revision: int = 1):
    if kind == "scheduler":
        return fanout.SchedulerSnapshot(
            plan=plan, plan_sha256=inputs.compiled_plan_sha256,
            plan_revision=1, run_id=inputs.run_id, inputs_digest=inputs.digest,
            backend_identity=backend.identity(), backend_key=backend.key(),
            backend_revision=revision,
            previous_commit="0" * 64 if revision == 1 else _digest("previous commit"),
            tasks=(fanout.SchedulerTaskState("approval"),),
        )
    return fanout.ExecutionSnapshot(
        run_id=inputs.run_id, plan_sha256=inputs.compiled_plan_sha256,
        inputs_digest=inputs.digest, backend_identity=backend.identity(),
        backend_key=backend.key(), backend_revision=revision, plan_revision=1,
        tasks=(fanout.ExecutionTaskState(
            task_id="approval", preparation_sha256="", decision_plan_revision=1,
            decision_plan_sha256=inputs.compiled_plan_sha256,
            decision_inputs_digest=inputs.digest,
            task_sha256=_digest(fanout.canonical_json(plan.tasks[0].to_dict())),
            packet_context_sha256="",
        ),),
    )


def _created_backend(tmp_path: Path, kind: str):
    root = tmp_path / "run"
    root.mkdir(mode=0o700)
    plan = _plan()
    inputs = _inputs(plan)
    owner = _owner()
    backend = _backend(kind).create(root, run_id=inputs.run_id, owner=owner)
    first = backend.compare_and_set(0, _snapshot(kind, backend, plan, inputs), owner=owner)
    return root, plan, inputs, owner, backend, first


def _process_read(kind: str, root: str, run_id: str, queue) -> None:
    backend = _backend(kind).resume(root, run_id=run_id, owner=_owner())
    record = backend.read()
    queue.put((record.revision, record.snapshot.run_id, record.snapshot.backend_revision))


def _process_cas(kind: str, root: str, run_id: str, snapshot, barrier, queue) -> None:
    try:
        backend = _backend(kind).resume(root, run_id=run_id, owner=_owner())
        barrier.wait(timeout=10)
        result = backend.compare_and_set(1, snapshot, owner=_owner())
        queue.put(("committed", result.revision))
    except fanout.FanoutError:
        queue.put(("conflict", None))


def _process_create(kind: str, root: str, run_id: str, barrier, queue) -> None:
    try:
        barrier.wait(timeout=10)
        backend = _backend(kind).create(root, run_id=run_id, owner=_owner())
        queue.put(("created", backend.read()))
    except fanout.FanoutError:
        queue.put(("conflict", None))


def _process_killed_at_publication(
        kind: str, root: str, run_id: str, expected_revision: int, snapshot) -> None:
    backend = _backend(kind).resume(root, run_id=run_id, owner=_owner())
    storage = sys.modules[f"{SPEC.name}.storage"]
    operation = "link" if expected_revision == 0 else "replace"
    original = getattr(storage.os, operation)

    def publish_then_exit(*args, **kwargs):
        original(*args, **kwargs)
        os._exit(77)

    setattr(storage.os, operation, publish_then_exit)
    backend.compare_and_set(expected_revision, snapshot, owner=_owner())


_BOOTSTRAP_EXITS = {
    "lock-write": 71,
    "lock-linked": 72,
    "lock-cleaned": 73,
    "lock-durable": 74,
    "metadata-write": 75,
    "metadata-linked": 76,
    "metadata-cleaned": 77,
    "metadata-durable": 78,
}


def _process_killed_during_bootstrap(kind: str, root: str, run_id: str,
                                     boundary: str) -> None:
    storage = sys.modules[f"{SPEC.name}.storage"]
    run_root = Path(root)
    lock_name = f".fanout-{kind}.lock"
    meta_name = f".fanout-{kind}.meta.json"
    original_write = os.write
    original_link = os.link
    original_unlink = os.unlink
    original_fsync = os.fsync

    def write_then_exit(fd: int, data: bytes):
        if ((boundary == "lock-write" and b'"schema_version":"fanout-file-backend-lock-v1"' in data)
                or (boundary == "metadata-write" and b'"lock_device":' in data
                    and b'"owner_mac":' in data)):
            original_write(fd, data[:19])
            original_fsync(fd)
            os._exit(_BOOTSTRAP_EXITS[boundary])
        return original_write(fd, data)

    def link_then_exit(source, destination, **kwargs):
        result = original_link(source, destination, **kwargs)
        if ((boundary == "lock-linked" and destination == lock_name)
                or (boundary == "metadata-linked" and destination == meta_name)):
            os._exit(_BOOTSTRAP_EXITS[boundary])
        return result

    def unlink_then_exit(name, **kwargs):
        final = lock_name if boundary == "lock-cleaned" else meta_name
        linked = False
        if boundary in {"lock-cleaned", "metadata-cleaned"}:
            try:
                source_info = os.stat(name, dir_fd=kwargs.get("dir_fd"),
                                      follow_symlinks=False)
                final_info = os.stat(final, dir_fd=kwargs.get("dir_fd"),
                                     follow_symlinks=False)
                linked = ((source_info.st_dev, source_info.st_ino)
                          == (final_info.st_dev, final_info.st_ino)
                          and source_info.st_nlink == 2)
            except FileNotFoundError:
                pass
        result = original_unlink(name, **kwargs)
        if linked:
            os._exit(_BOOTSTRAP_EXITS[boundary])
        return result

    def fsync_then_exit(fd: int):
        result = original_fsync(fd)
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            if (boundary == "lock-durable" and (run_root / lock_name).exists()
                    and not (run_root / meta_name).exists()
                    and (run_root / lock_name).stat().st_nlink == 1):
                os._exit(_BOOTSTRAP_EXITS[boundary])
            if (boundary == "metadata-durable" and (run_root / meta_name).exists()
                    and (run_root / meta_name).stat().st_nlink == 1):
                os._exit(_BOOTSTRAP_EXITS[boundary])
        return result

    storage.os.write = write_then_exit
    storage.os.link = link_then_exit
    storage.os.unlink = unlink_then_exit
    storage.os.fsync = fsync_then_exit
    _backend(kind).create(root, run_id=run_id, owner=_owner())


def _entries(root: Path) -> tuple[tuple[str, int, int, bytes], ...]:
    return tuple(sorted(
        (path.name, path.lstat().st_ino, path.lstat().st_nlink, path.read_bytes())
        for path in root.iterdir()
    ))


@pytest.mark.parametrize("kind", ["scheduler", "execution"])
@pytest.mark.parametrize("boundary,reopen", [
    ("lock-write", "create"),
    ("lock-linked", "resume"), ("lock-linked", "create"),
    ("lock-cleaned", "resume"), ("lock-cleaned", "create"),
    ("lock-durable", "resume"), ("lock-durable", "create"),
    ("metadata-write", "resume"), ("metadata-write", "create"),
    ("metadata-linked", "resume"),
    ("metadata-cleaned", "resume"),
    ("metadata-durable", "resume"),
])
def test_killed_bootstrap_recovers_only_authenticated_incomplete_state(
        tmp_path: Path, kind: str, boundary: str, reopen: str):
    root = tmp_path / "run"
    root.mkdir(mode=0o700)
    inputs = _inputs(_plan())
    backend_type = _backend(kind)
    context = multiprocessing.get_context("fork")
    child = context.Process(target=_process_killed_during_bootstrap,
                            args=(kind, str(root), inputs.run_id, boundary))
    child.start()
    child.join(timeout=10)
    if child.is_alive():
        child.terminate()
        child.join(timeout=10)
    assert child.exitcode == _BOOTSTRAP_EXITS[boundary]
    unpublished = {
        path.name: path.read_bytes() for path in root.iterdir()
        if boundary in {"lock-write", "metadata-write"} and "-tmp-" in path.name
    }
    if boundary in {"lock-write", "metadata-write"}:
        assert unpublished

    if boundary != "lock-write":
        before = _entries(root)
        wrong_owner = fanout.OwnerCapability.from_token(
            "different-storage-owner-capability-0123456789"
        )
        for method in (backend_type.create, backend_type.resume):
            with pytest.raises(fanout.FanoutError):
                method(root, run_id=inputs.run_id, owner=wrong_owner)
            with pytest.raises(fanout.FanoutError):
                method(root, run_id="another-run", owner=_owner())
        assert _entries(root) == before
    else:
        with pytest.raises(fanout.FanoutError):
            backend_type.resume(root, run_id=inputs.run_id, owner=_owner())

    reopened = getattr(backend_type, reopen)(root, run_id=inputs.run_id, owner=_owner())
    assert reopened.read() is None
    assert (root / reopened._lock_name).stat().st_nlink == 1
    assert (root / reopened._meta_name).stat().st_nlink == 1
    assert all((root / name).read_bytes() == data for name, data in unpublished.items())
    with pytest.raises(fanout.FanoutError):
        backend_type.create(root, run_id=inputs.run_id, owner=_owner())
    assert backend_type.resume(root, run_id=inputs.run_id, owner=_owner()).read() is None


@pytest.mark.parametrize("kind", ["scheduler", "execution"])
@pytest.mark.parametrize("entry", ["lock", "meta"])
def test_bootstrap_refuses_unrelated_existing_entry_without_removing_it(
        tmp_path: Path, kind: str, entry: str):
    root = tmp_path / "run"
    root.mkdir(mode=0o700)
    name = f".fanout-{kind}.{'lock' if entry == 'lock' else 'meta.json'}"
    path = root / name
    path.write_bytes(b"unrelated owner-private entry")
    path.chmod(0o600)
    before = _entries(root)
    with pytest.raises(fanout.FanoutError):
        _backend(kind).create(root, run_id=_inputs(_plan()).run_id, owner=_owner())
    assert _entries(root) == before


@pytest.mark.parametrize("kind", ["scheduler", "execution"])
def test_malformed_final_metadata_is_never_overwritten_during_recovery(
        tmp_path: Path, kind: str):
    root = tmp_path / "run"
    root.mkdir(mode=0o700)
    inputs = _inputs(_plan())
    context = multiprocessing.get_context("fork")
    child = context.Process(target=_process_killed_during_bootstrap,
                            args=(kind, str(root), inputs.run_id, "lock-durable"))
    child.start()
    child.join(timeout=10)
    if child.is_alive():
        child.terminate()
        child.join(timeout=10)
    assert child.exitcode == _BOOTSTRAP_EXITS["lock-durable"]
    metadata = root / f".fanout-{kind}.meta.json"
    metadata.write_bytes(b'{"schema_version":')
    metadata.chmod(0o600)
    before = _entries(root)
    for method in (_backend(kind).create, _backend(kind).resume):
        with pytest.raises(fanout.FanoutError):
            method(root, run_id=inputs.run_id, owner=_owner())
    assert _entries(root) == before


@pytest.mark.parametrize("kind", ["scheduler", "execution"])
@pytest.mark.parametrize("boundary,entry", [
    ("lock-linked", "lock"), ("metadata-linked", "meta.json"),
])
def test_tampered_linked_bootstrap_entry_is_not_unlinked_or_recovered(
        tmp_path: Path, kind: str, boundary: str, entry: str):
    root = tmp_path / "run"
    root.mkdir(mode=0o700)
    inputs = _inputs(_plan())
    context = multiprocessing.get_context("fork")
    child = context.Process(target=_process_killed_during_bootstrap,
                            args=(kind, str(root), inputs.run_id, boundary))
    child.start()
    child.join(timeout=10)
    if child.is_alive():
        child.terminate()
        child.join(timeout=10)
    assert child.exitcode == _BOOTSTRAP_EXITS[boundary]
    (root / f".fanout-{kind}.{entry}").write_bytes(b'{"incomplete":')
    before = _entries(root)
    for method in (_backend(kind).create, _backend(kind).resume):
        with pytest.raises(fanout.FanoutError):
            method(root, run_id=inputs.run_id, owner=_owner())
    assert _entries(root) == before


@pytest.mark.parametrize("kind", ["scheduler", "execution"])
@pytest.mark.parametrize("failed_sync", ["lock-file", "lock-directory",
                                         "metadata-file", "metadata-directory"])
def test_bootstrap_fsync_failure_never_reports_success(
        tmp_path: Path, monkeypatch, kind: str, failed_sync: str):
    root = tmp_path / "run"
    root.mkdir(mode=0o700)
    inputs = _inputs(_plan())
    backend_type = _backend(kind)
    lock = root / f".fanout-{kind}.lock"
    metadata = root / f".fanout-{kind}.meta.json"
    storage = sys.modules[f"{SPEC.name}.storage"]
    original_fsync = os.fsync
    failed = False

    def fail_selected_fsync(fd: int):
        nonlocal failed
        info = os.fstat(fd)
        access = fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE
        selected = (
            (failed_sync == "lock-file" and stat.S_ISREG(info.st_mode)
             and access == os.O_WRONLY and not lock.exists())
            or (failed_sync == "lock-directory" and stat.S_ISDIR(info.st_mode)
                and lock.exists() and not metadata.exists())
            or (failed_sync == "metadata-file" and stat.S_ISREG(info.st_mode)
                and access == os.O_WRONLY and lock.exists() and not metadata.exists())
            or (failed_sync == "metadata-directory" and stat.S_ISDIR(info.st_mode)
                and metadata.exists())
        )
        if selected and not failed:
            failed = True
            raise OSError("injected bootstrap fsync failure")
        return original_fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(storage.os, "fsync", fail_selected_fsync)
        with pytest.raises(fanout.FanoutError):
            backend_type.create(root, run_id=inputs.run_id, owner=_owner())
    assert failed
    recovery = backend_type.create if failed_sync == "lock-file" else backend_type.resume
    reopened = recovery(root, run_id=inputs.run_id, owner=_owner())
    assert reopened.read() is None
    with pytest.raises(fanout.FanoutError):
        backend_type.create(root, run_id=inputs.run_id, owner=_owner())


@pytest.mark.parametrize("kind", ["scheduler", "execution"])
def test_exclusive_create_and_cas_survive_fresh_object_reopen(tmp_path: Path, kind: str):
    run_root = tmp_path / "run"
    run_root.mkdir(mode=0o700)
    plan = _plan()
    inputs = _inputs(plan)
    owner = _owner()
    backend_type = _backend(kind)
    backend = backend_type.create(run_root, run_id=inputs.run_id, owner=owner)
    assert backend.read() is None

    first = _snapshot(kind, backend, plan, inputs)
    created = backend.compare_and_set(0, first, owner=owner)
    assert created.revision == 1
    assert created.snapshot == first
    with pytest.raises(fanout.FanoutError):
        backend_type.create(run_root, run_id=inputs.run_id, owner=owner)

    reopened = backend_type.resume(run_root, run_id=inputs.run_id, owner=_owner())
    assert reopened.identity() == backend.identity()
    assert reopened.key() == backend.key()
    assert reopened.read() == created
    second = _snapshot(kind, backend, plan, inputs, revision=2)
    committed = reopened.compare_and_set(1, second, owner=_owner())
    assert committed.revision == 2
    assert backend_type.resume(run_root, run_id=inputs.run_id, owner=_owner()).read() == committed
    with pytest.raises(fanout.FanoutError):
        backend.compare_and_set(1, second, owner=owner)


@pytest.mark.parametrize("kind", ["scheduler", "execution"])
def test_two_processes_cannot_both_exclusively_create_one_backend(
        tmp_path: Path, kind: str):
    root = tmp_path / "run"
    root.mkdir(mode=0o700)
    inputs = _inputs(_plan())
    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(2)
    queue = context.Queue()
    children = [context.Process(target=_process_create,
                                args=(kind, str(root), inputs.run_id, barrier, queue))
                for _ in range(2)]
    for child in children:
        child.start()
    try:
        outcomes = [queue.get(timeout=12) for _ in children]
    finally:
        for child in children:
            child.join(timeout=10)
            if child.is_alive():
                child.terminate()
                child.join(timeout=10)
    assert sorted(outcomes) == [("conflict", None), ("created", None)]
    assert all(child.exitcode == 0 for child in children)
    assert _backend(kind).resume(root, run_id=inputs.run_id, owner=_owner()).read() is None


@pytest.mark.parametrize("kind", ["scheduler", "execution"])
def test_fresh_process_reads_the_exact_committed_revision(tmp_path: Path, kind: str):
    root, _, inputs, _, _, first = _created_backend(tmp_path, kind)
    context = multiprocessing.get_context("fork")
    queue = context.Queue()
    child = context.Process(target=_process_read, args=(kind, str(root), inputs.run_id, queue))
    child.start()
    try:
        observed = queue.get(timeout=10)
    finally:
        child.join(timeout=10)
        if child.is_alive():
            child.terminate()
            child.join(timeout=10)
    assert child.exitcode == 0
    assert observed == (first.revision, inputs.run_id, first.snapshot.backend_revision)


@pytest.mark.parametrize("kind", ["scheduler", "execution"])
def test_two_processes_cannot_win_one_cas_revision(tmp_path: Path, kind: str):
    root, plan, inputs, _, backend, _ = _created_backend(tmp_path, kind)
    next_snapshot = _snapshot(kind, backend, plan, inputs, revision=2)
    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(2)
    queue = context.Queue()
    children = [context.Process(
        target=_process_cas,
        args=(kind, str(root), inputs.run_id, next_snapshot, barrier, queue),
    ) for _ in range(2)]
    for child in children:
        child.start()
    try:
        outcomes = [queue.get(timeout=12) for _ in children]
    finally:
        for child in children:
            child.join(timeout=10)
            if child.is_alive():
                child.terminate()
                child.join(timeout=10)
    assert sorted(outcomes) == [("committed", 2), ("conflict", None)]
    assert all(child.exitcode == 0 for child in children)
    assert _backend(kind).resume(root, run_id=inputs.run_id, owner=_owner()).read().revision == 2


@pytest.mark.parametrize("kind", ["scheduler", "execution"])
def test_backend_is_bound_to_the_exact_run_root_and_owner(tmp_path: Path, kind: str):
    root, plan, inputs, owner, backend, _ = _created_backend(tmp_path, kind)
    copied = tmp_path / "copied-run"
    shutil.copytree(root, copied)
    wrong_owner = fanout.OwnerCapability.from_token("different-storage-owner-capability-0123456789")
    with pytest.raises(fanout.FanoutError):
        _backend(kind).resume(copied, run_id=inputs.run_id, owner=owner)
    with pytest.raises(fanout.FanoutError):
        _backend(kind).resume(root, run_id="other-run", owner=owner)
    with pytest.raises(fanout.RunAuthorizationError):
        _backend(kind).resume(root, run_id=inputs.run_id, owner=wrong_owner)
    with pytest.raises(fanout.RunAuthorizationError):
        backend.compare_and_set(1, _snapshot(kind, backend, plan, inputs, 2), owner=wrong_owner)
    assert not any(
        _OWNER_TOKEN.encode() in entry.read_bytes()
        for entry in root.iterdir() if entry.is_file()
    )


@pytest.mark.parametrize("kind", ["scheduler", "execution"])
def test_initial_cas_cannot_overwrite_an_entry_created_at_publication(
        tmp_path: Path, monkeypatch, kind: str):
    root = tmp_path / "run"
    root.mkdir(mode=0o700)
    plan = _plan()
    inputs = _inputs(plan)
    owner = _owner()
    backend = _backend(kind).create(root, run_id=inputs.run_id, owner=owner)
    record_name = backend._record_name
    storage = sys.modules[f"{SPEC.name}.storage"]
    original_replace = os.replace
    original_link = os.link

    def insert_foreign_entry(source, destination, **kwargs):
        assert destination == record_name
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
                     dir_fd=kwargs["dst_dir_fd"])
        try:
            os.write(fd, b"foreign entry")
        finally:
            os.close(fd)
        if "follow_symlinks" in kwargs:
            return original_link(source, destination, **kwargs)
        return original_replace(source, destination, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(storage.os, "replace", insert_foreign_entry)
        patch.setattr(storage.os, "link", insert_foreign_entry)
        with pytest.raises(fanout.FanoutError):
            backend.compare_and_set(0, _snapshot(kind, backend, plan, inputs), owner=owner)
    assert (root / record_name).read_bytes() == b"foreign entry"


@pytest.mark.parametrize("kind", ["scheduler", "execution"])
def test_same_revision_in_place_record_substitution_is_refused(tmp_path: Path, kind: str):
    root, _, _, _, backend, _ = _created_backend(tmp_path, kind)
    path = root / backend._record_name
    document = json.loads(path.read_text())
    if kind == "scheduler":
        document["snapshot"]["previous_commit"] = _digest("substituted predecessor")
    else:
        document["snapshot"]["tasks"][0]["task_sha256"] = _digest("substituted task")
    document["snapshot_sha256"] = _digest(fanout.canonical_json(document["snapshot"]))
    path.write_bytes(fanout.canonical_json(document))
    with pytest.raises(fanout.FanoutError, match="replaced|changed"):
        backend.read()


@pytest.mark.parametrize("kind", ["scheduler", "execution"])
@pytest.mark.parametrize("revision", [1, 2])
def test_killed_process_leaves_exact_old_or_new_record_discoverable(
        tmp_path: Path, kind: str, revision: int):
    root = tmp_path / "run"
    root.mkdir(mode=0o700)
    plan = _plan()
    inputs = _inputs(plan)
    backend = _backend(kind).create(root, run_id=inputs.run_id, owner=_owner())
    if revision == 2:
        backend.compare_and_set(0, _snapshot(kind, backend, plan, inputs), owner=_owner())
    candidate = _snapshot(kind, backend, plan, inputs, revision=revision)
    context = multiprocessing.get_context("fork")
    child = context.Process(target=_process_killed_at_publication, args=(
        kind, str(root), inputs.run_id, revision - 1, candidate,
    ))
    child.start()
    child.join(timeout=10)
    if child.is_alive():
        child.terminate()
        child.join(timeout=10)
    assert child.exitcode == 77
    recovered = _backend(kind).resume(root, run_id=inputs.run_id, owner=_owner()).read()
    assert recovered.revision == revision
    assert recovered.snapshot == candidate
    assert (root / backend._record_name).stat().st_nlink == 1


@pytest.mark.parametrize("kind", ["scheduler", "execution"])
@pytest.mark.parametrize(
    "damage",
    ["malformed", "noncanonical", "oversized", "unknown-record", "unknown-snapshot",
     "wrong-digest", "bool-revision", "invalid-nested-ref"],
)
def test_malformed_noncanonical_or_oversized_record_is_refused(
        tmp_path: Path, kind: str, damage: str):
    root, _, inputs, _, backend, _ = _created_backend(tmp_path, kind)
    path = root / backend._record_name
    document = json.loads(path.read_text())
    if damage == "malformed":
        payload = b'{"snapshot":'
    elif damage == "noncanonical":
        payload = (json.dumps(document, indent=2) + "\n").encode()
    elif damage == "oversized":
        payload = b"x" * (8 * 1024 * 1024 + 1)
    else:
        if damage == "unknown-record":
            document["unexpected"] = True
        elif damage == "unknown-snapshot":
            document["snapshot"]["unexpected"] = True
        elif damage == "wrong-digest":
            document["snapshot_sha256"] = "0" * 64
        elif damage == "bool-revision":
            document["revision"] = True
        else:
            state = document["snapshot"]["tasks"][0]
            if kind == "scheduler":
                state["dependencies"] = [{"unexpected": True}]
            else:
                state["barriers"] = [{"path": "../escape", "digest": "0" * 64, "size": 1}]
        if damage in {"unknown-snapshot", "invalid-nested-ref"}:
            document["snapshot_sha256"] = _digest(
                fanout.canonical_json(document["snapshot"])
            )
        payload = fanout.canonical_json(document)
    path.write_bytes(payload)
    with pytest.raises(fanout.FanoutError):
        backend.read()
    with pytest.raises(fanout.FanoutError):
        _backend(kind).resume(root, run_id=inputs.run_id, owner=_owner())


@pytest.mark.parametrize("kind", ["scheduler", "execution"])
@pytest.mark.parametrize("entry", ["record", "meta", "lock"])
@pytest.mark.parametrize("damage", ["symlink", "fifo", "hardlink", "replacement"])
def test_unsafe_or_replaced_backend_entry_is_refused(
        tmp_path: Path, kind: str, entry: str, damage: str):
    root, _, inputs, _, backend, _ = _created_backend(tmp_path, kind)
    name = getattr(backend, f"_{entry}_name")
    path = root / name
    if damage == "symlink":
        path.unlink()
        path.symlink_to(tmp_path / "outside")
    elif damage == "fifo":
        path.unlink()
        os.mkfifo(path, 0o600)
    elif damage == "hardlink":
        os.link(path, tmp_path / "alias")
    else:
        replacement = tmp_path / "replacement"
        replacement.write_bytes(path.read_bytes())
        replacement.chmod(0o600)
        os.replace(replacement, path)
    with pytest.raises(fanout.FanoutError):
        backend.read()
    if damage != "replacement" or entry == "lock":
        with pytest.raises(fanout.FanoutError):
            _backend(kind).resume(root, run_id=inputs.run_id, owner=_owner())


@pytest.mark.parametrize("kind", ["scheduler", "execution"])
def test_replaced_run_root_inode_is_refused_even_on_fresh_reopen(tmp_path: Path, kind: str):
    root, _, inputs, _, backend, _ = _created_backend(tmp_path, kind)
    moved = tmp_path / "moved-run"
    root.rename(moved)
    shutil.copytree(moved, root)
    with pytest.raises(fanout.FanoutError):
        backend.read()
    with pytest.raises(fanout.FanoutError):
        _backend(kind).resume(root, run_id=inputs.run_id, owner=_owner())


@pytest.mark.parametrize("kind", ["scheduler", "execution"])
@pytest.mark.parametrize("revision", [1, 2])
def test_record_publication_fsyncs_file_before_link_or_replace_and_directory_after(
        tmp_path: Path, monkeypatch, kind: str, revision: int):
    root = tmp_path / "run"
    root.mkdir(mode=0o700)
    plan = _plan()
    inputs = _inputs(plan)
    backend = _backend(kind).create(root, run_id=inputs.run_id, owner=_owner())
    if revision == 2:
        backend.compare_and_set(0, _snapshot(kind, backend, plan, inputs), owner=_owner())
    storage = sys.modules[f"{SPEC.name}.storage"]
    original_fsync = os.fsync
    original_link = os.link
    original_replace = os.replace
    events = []

    def record_fsync(fd: int):
        mode = os.fstat(fd).st_mode
        access = fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE
        if stat.S_ISDIR(mode):
            events.append("directory-fsync")
        elif stat.S_ISREG(mode) and access == os.O_WRONLY:
            events.append("written-file-fsync")
        return original_fsync(fd)

    def record_link(*args, **kwargs):
        events.append("publish")
        return original_link(*args, **kwargs)

    def record_replace(*args, **kwargs):
        events.append("publish")
        return original_replace(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(storage.os, "fsync", record_fsync)
        patch.setattr(storage.os, "link", record_link)
        patch.setattr(storage.os, "replace", record_replace)
        backend.compare_and_set(
            revision - 1, _snapshot(kind, backend, plan, inputs, revision), owner=_owner(),
        )
    publish_index = events.index("publish")
    assert "written-file-fsync" in events[:publish_index]
    assert "directory-fsync" in events[publish_index + 1:]


@pytest.mark.parametrize("kind", ["scheduler", "execution"])
@pytest.mark.parametrize("revision", [1, 2])
@pytest.mark.parametrize("failed_sync", ["written-file", "directory-after-publication"])
def test_fsync_failure_never_reports_success_and_reopen_sees_exact_boundary(
        tmp_path: Path, monkeypatch, kind: str, revision: int, failed_sync: str):
    root = tmp_path / "run"
    root.mkdir(mode=0o700)
    plan = _plan()
    inputs = _inputs(plan)
    backend = _backend(kind).create(root, run_id=inputs.run_id, owner=_owner())
    old = None
    if revision == 2:
        old = backend.compare_and_set(0, _snapshot(kind, backend, plan, inputs), owner=_owner())
    candidate = _snapshot(kind, backend, plan, inputs, revision)
    storage = sys.modules[f"{SPEC.name}.storage"]
    original_fsync = os.fsync
    original_link = os.link
    original_replace = os.replace
    published = False

    def mark_link(*args, **kwargs):
        nonlocal published
        result = original_link(*args, **kwargs)
        published = True
        return result

    def mark_replace(*args, **kwargs):
        nonlocal published
        result = original_replace(*args, **kwargs)
        published = True
        return result

    def fail_selected_fsync(fd: int):
        mode = os.fstat(fd).st_mode
        access = fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE
        if failed_sync == "written-file" and stat.S_ISREG(mode) and access == os.O_WRONLY:
            raise OSError("injected written-file fsync failure")
        if failed_sync == "directory-after-publication" and published and stat.S_ISDIR(mode):
            raise OSError("injected directory fsync failure")
        return original_fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(storage.os, "fsync", fail_selected_fsync)
        patch.setattr(storage.os, "link", mark_link)
        patch.setattr(storage.os, "replace", mark_replace)
        with pytest.raises(fanout.FanoutError):
            backend.compare_and_set(revision - 1, candidate, owner=_owner())
        if failed_sync == "directory-after-publication":
            with pytest.raises(fanout.FanoutError):
                backend.read()
        else:
            assert backend.read() == old
    recovered = _backend(kind).resume(root, run_id=inputs.run_id, owner=_owner()).read()
    if failed_sync == "written-file":
        assert recovered == old
    else:
        assert recovered.revision == revision
        assert recovered.snapshot == candidate


class _ProviderBoundary:
    def __init__(self, artifacts, journal, owner, profiles):
        self.artifacts = artifacts
        self.journal = journal
        self.owner = owner
        self.memory = _MemoryBoundary(artifacts)
        self.registry = _RegistryBoundary(profiles)
        self.lifecycle_controller = None
        self.repository_baseline = None
        self.launches = 0

    def preflight_round(self, *_args, **_kwargs):
        raise AssertionError("action-only plans cannot preflight a provider round")

    def execute_round(self, *_args, **_kwargs):
        self.launches += 1
        raise AssertionError("action-only plans cannot launch a provider")

    def restore_barrier(self, *_args, **_kwargs):
        raise AssertionError("action-only plans have no provider barrier")


class _MemoryBoundary:
    def __init__(self, artifacts):
        self.artifacts = artifacts

    def preflight(self):
        return True


class _RegistryBoundary:
    def __init__(self, profiles):
        self.profile_digests = dict(profiles)


def _production_run(tmp_path: Path):
    plan = _plan()
    inputs = _inputs(plan)
    repo_root = tmp_path / "repo"
    repo_root.mkdir(mode=0o700)
    run_root = tmp_path / "run"
    authority_root = tmp_path / "authority"
    authority = fanout.LocalAnchorAuthority.bootstrap(
        authority_root, run_root=run_root, repo_root=repo_root,
    )
    journal, owner = fanout.RunJournal.create(run_root, inputs, anchor_store=authority)
    artifacts = fanout.ArtifactStore(run_root / "artifacts")
    scheduler_backend = fanout.FileSchedulerBackend.create(
        run_root, run_id=inputs.run_id, owner=owner,
    )
    execution_backend = fanout.FileExecutionBackend.create(
        run_root, run_id=inputs.run_id, owner=owner,
    )
    scheduler = fanout.Scheduler.create(
        plan, inputs, scheduler_backend, artifacts, owner=owner, anchor_store=authority,
    )
    return (
        plan, inputs, repo_root, run_root, authority_root, owner,
        journal, artifacts, scheduler_backend, execution_backend, scheduler,
    )


def _reopen_production_run(plan, inputs, repo_root, run_root, authority_root, owner):
    authority = fanout.LocalAnchorAuthority(
        authority_root, run_root=run_root, repo_root=repo_root,
    )
    journal = fanout.RunJournal.resume(run_root, inputs, owner, anchor_store=authority)
    artifacts = fanout.ArtifactStore(run_root / "artifacts")
    scheduler_backend = fanout.FileSchedulerBackend.resume(
        run_root, run_id=inputs.run_id, owner=owner,
    )
    execution_backend = fanout.FileExecutionBackend.resume(
        run_root, run_id=inputs.run_id, owner=owner,
    )
    scheduler = fanout.Scheduler.resume(
        plan, inputs, scheduler_backend, artifacts, owner=owner, anchor_store=authority,
    )
    return journal, artifacts, scheduler_backend, execution_backend, scheduler


class _LostResponse(BaseException):
    pass


@pytest.mark.parametrize("boundary", ["before-backend-cas", "after-backend-cas"])
def test_scheduler_resolves_pending_local_authority_after_lost_backend_response(
        tmp_path: Path, monkeypatch, boundary: str):
    (plan, inputs, repo_root, run_root, authority_root, owner,
     journal, artifacts, scheduler_backend, execution_backend, scheduler) = _production_run(tmp_path)
    original_cas = scheduler_backend.compare_and_set

    def interrupted_cas(expected_revision, snapshot, *, owner):
        if boundary == "after-backend-cas":
            original_cas(expected_revision, snapshot, owner=owner)
        raise _LostResponse()

    with monkeypatch.context() as patch:
        patch.setattr(scheduler_backend, "compare_and_set", interrupted_cas)
        with pytest.raises(_LostResponse):
            scheduler.schedule_ready(owner=owner)
    journal.close()
    artifacts.close()
    scheduler_backend.close()
    execution_backend.close()

    (reopened_journal, reopened_artifacts, reopened_scheduler_backend,
     reopened_execution_backend, recovered) = _reopen_production_run(
        plan, inputs, repo_root, run_root, authority_root, owner,
    )
    try:
        expected_phase = (
            "unscheduled" if boundary == "before-backend-cas" else "blocked-action"
        )
        assert recovered.task_phase("approval") == expected_phase
        if boundary == "before-backend-cas":
            assert len(recovered.schedule_ready(owner=owner)) == 1
            assert recovered.task_phase("approval") == "blocked-action"
        assert reopened_scheduler_backend.read().revision == recovered.revision
    finally:
        reopened_journal.close()
        reopened_artifacts.close()
        reopened_scheduler_backend.close()
        reopened_execution_backend.close()


def test_real_service_reopens_all_stores_and_completes_action_without_provider_launch(
        tmp_path: Path):
    (plan, inputs, repo_root, run_root, authority_root, owner,
     journal, artifacts, scheduler_backend, execution_backend, scheduler) = _production_run(tmp_path)
    first_boundary = _ProviderBoundary(artifacts, journal, owner, inputs.provider_profiles)
    service = fanout.ExecutionService(
        plan=plan, inputs=inputs, preparations={},
        provider_profile_digests=inputs.provider_profiles,
        budget=fanout.ExecutionBudget(1),
        scheduler=scheduler, journal=journal, coordinator=first_boundary,
        artifacts=artifacts, backend=execution_backend,
        memory_preflight=first_boundary.memory.preflight,
    )
    started = service.start(owner=owner)
    assert [(item.task_id, item.phase) for item in started.tasks] == [
        ("approval", "blocked-action")
    ]
    assert first_boundary.launches == 0
    journal.close()
    artifacts.close()
    scheduler_backend.close()
    execution_backend.close()

    (reopened_journal, reopened_artifacts, reopened_scheduler_backend,
     reopened_execution_backend, reopened_scheduler) = _reopen_production_run(
        plan, inputs, repo_root, run_root, authority_root, owner,
    )
    second_boundary = _ProviderBoundary(
        reopened_artifacts, reopened_journal, owner, inputs.provider_profiles,
    )
    resumed_service = fanout.ExecutionService(
        plan=plan, inputs=inputs, preparations={},
        provider_profile_digests=inputs.provider_profiles,
        budget=fanout.ExecutionBudget(1),
        scheduler=reopened_scheduler, journal=reopened_journal,
        coordinator=second_boundary, artifacts=reopened_artifacts,
        backend=reopened_execution_backend,
        memory_preflight=second_boundary.memory.preflight,
    )
    resumed = resumed_service.resume(owner=owner)
    assert [(item.task_id, item.phase) for item in resumed.tasks] == [
        ("approval", "blocked-action")
    ]
    approval = reopened_artifacts.write_bytes("actions/approval.txt", b"approved\n")
    receipt = resumed_service.action_complete("approval", approval, owner=owner)
    assert receipt.artifact == approval
    assert resumed_service.status().tasks[0].phase == "completed"
    assert first_boundary.launches == second_boundary.launches == 0
    reopened_journal.close()
    reopened_artifacts.close()
    reopened_scheduler_backend.close()
    reopened_execution_backend.close()

    (final_journal, final_artifacts, final_scheduler_backend,
     final_execution_backend, final_scheduler) = _reopen_production_run(
        plan, inputs, repo_root, run_root, authority_root, owner,
    )
    final_boundary = _ProviderBoundary(
        final_artifacts, final_journal, owner, inputs.provider_profiles,
    )
    final_service = fanout.ExecutionService(
        plan=plan, inputs=inputs, preparations={},
        provider_profile_digests=inputs.provider_profiles,
        budget=fanout.ExecutionBudget(1),
        scheduler=final_scheduler, journal=final_journal, coordinator=final_boundary,
        artifacts=final_artifacts, backend=final_execution_backend,
        memory_preflight=final_boundary.memory.preflight,
    )
    assert final_service.resume(owner=owner).tasks[0].phase == "completed"
    assert final_boundary.launches == 0
    final_journal.close()
    final_artifacts.close()
    final_scheduler_backend.close()
    final_execution_backend.close()
