"""Production local authority contracts, using only disposable explicit roots."""
from __future__ import annotations

import hashlib
import importlib.util
import multiprocessing
import os
import stat
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
FANOUT_ROOT = ROOT / "shared" / "lib" / "fanout"
SPEC = importlib.util.spec_from_file_location(
    "_fanout_local_authority_contracts", FANOUT_ROOT / "__init__.py",
    submodule_search_locations=[str(FANOUT_ROOT)],
)
assert SPEC and SPEC.loader
fanout = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fanout
SPEC.loader.exec_module(fanout)

LocalAnchorAuthority = fanout.LocalAnchorAuthority
RunStateError = fanout.RunStateError


def _roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    return tmp_path / "authority", tmp_path / "run", tmp_path / "repo"


def _authority(tmp_path: Path):
    root, run_root, repo_root = _roots(tmp_path)
    repo_root.mkdir(mode=0o700)
    return LocalAnchorAuthority.bootstrap(root, run_root=run_root, repo_root=repo_root)


def _record_file(root: Path) -> Path:
    records = list(root.glob("anchor-*.json"))
    assert len(records) == 1
    return records[0]


def _inputs() -> object:
    return fanout.RunInputs(
        run_id="run-1", compiled_plan_sha256="a" * 64, source_sha256="b" * 64,
        draft_sha256="c" * 64, compiler_sha256="d" * 64,
        parser_sha256="e" * 64, provider_profiles={"claude": "f" * 64},
        skill_manifests={}, repo_baseline_sha256="0" * 64,
    )


def _race_worker(root: str, run_root: str, repo_root: str, barrier, results,
                 operation: str) -> None:
    authority = LocalAnchorAuthority(root, run_root=run_root, repo_root=repo_root)
    barrier.wait()
    try:
        if operation == "create":
            result = authority.create("shared/key", b"initial")
        else:
            result = authority.compare_and_set("shared/key", 1, b"next")
        results.put(("ok", result.revision))
    except RunStateError:
        results.put(("conflict", None))


def test_bootstrap_is_private_separate_and_has_a_stable_health_probe(tmp_path: Path):
    root, run_root, repo_root = _roots(tmp_path)
    with pytest.raises(RunStateError, match="outside"):
        LocalAnchorAuthority.bootstrap(run_root / "authority", run_root=run_root, repo_root=repo_root)
    with pytest.raises(RunStateError, match="outside"):
        LocalAnchorAuthority.bootstrap(repo_root / "authority", run_root=run_root, repo_root=repo_root)

    authority = _authority(tmp_path)
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in root.iterdir())
    assert authority.probe() == authority.identity
    reopened = LocalAnchorAuthority(root, run_root=run_root, repo_root=repo_root)
    assert reopened.identity == authority.identity
    assert reopened.probe() == authority.identity
    with pytest.raises(RunStateError):
        LocalAnchorAuthority.bootstrap(root, run_root=run_root, repo_root=repo_root)


def test_case_alias_of_repository_cannot_host_authority(tmp_path: Path):
    repo_root = tmp_path / "repository"
    repo_root.mkdir(mode=0o700)
    alias = tmp_path / "REPOSITORY"
    if not alias.exists() or not os.path.samefile(alias, repo_root):
        pytest.skip("filesystem has no case-insensitive directory alias")

    with pytest.raises(RunStateError, match="outside"):
        LocalAnchorAuthority.bootstrap(
            alias / "authority", run_root=tmp_path / "run", repo_root=repo_root,
        )
    assert not (repo_root / "authority").exists()

    code_alias = Path(str(ROOT).upper())
    if code_alias.exists() and os.path.samefile(code_alias, ROOT):
        runtime = sys.modules[f"{SPEC.name}.runstate"]
        unrelated_repo = tmp_path / "unrelated-repo"
        unrelated_repo.mkdir(mode=0o700)
        with pytest.raises(RunStateError, match="outside"):
            runtime._local_authority_paths(
                code_alias / "authority", tmp_path / "run", unrelated_repo,
            )


def test_bootstrap_refuses_absent_repository_before_writing(tmp_path: Path):
    root, run_root, repo_root = _roots(tmp_path)
    with pytest.raises(RunStateError, match="repository root must exist"):
        LocalAnchorAuthority.bootstrap(root, run_root=run_root, repo_root=repo_root)
    assert not root.exists()
    assert not repo_root.exists()


def test_bootstrap_refuses_nondirectory_repository_before_writing(tmp_path: Path):
    root, run_root, repo_root = _roots(tmp_path)
    repo_root.write_bytes(b"not a repository directory")
    with pytest.raises(RunStateError, match="repository root must exist"):
        LocalAnchorAuthority.bootstrap(root, run_root=run_root, repo_root=repo_root)
    assert not root.exists()


def test_absent_repository_case_alias_is_refused_before_writing(tmp_path: Path):
    probe = tmp_path / "case-probe"
    probe.mkdir(mode=0o700)
    alias_probe = tmp_path / "CASE-PROBE"
    if not alias_probe.exists() or not os.path.samefile(probe, alias_probe):
        pytest.skip("filesystem has no case-insensitive directory alias")

    repo_root = tmp_path / "future-repo"
    alias_root = tmp_path / "FUTURE-REPO"
    authority_root = alias_root / "authority"
    with pytest.raises(RunStateError):
        LocalAnchorAuthority.bootstrap(
            authority_root, run_root=tmp_path / "run", repo_root=repo_root,
        )
    assert not alias_root.exists()
    assert not repo_root.exists()
    assert not authority_root.exists()


def test_exclusive_create_and_contiguous_cas_survive_reopen(tmp_path: Path):
    authority = _authority(tmp_path)
    assert authority.create("run/key", b"first") == fanout.AnchorRevision(1, b"first")
    with pytest.raises(RunStateError):
        authority.create("run/key", b"other")
    with pytest.raises(RunStateError):
        authority.compare_and_set("run/key", 0, b"skipped")
    assert authority.compare_and_set("run/key", 1, b"second") == fanout.AnchorRevision(2, b"second")
    with pytest.raises(RunStateError):
        authority.compare_and_set("run/key", 1, b"stale")
    root, run_root, repo_root = _roots(tmp_path)
    assert LocalAnchorAuthority(root, run_root=run_root, repo_root=repo_root).read("run/key") == (
        fanout.AnchorRevision(2, b"second")
    )
    with pytest.raises(RunStateError, match="different run root"):
        fanout.RunJournal.create(tmp_path / "other-run", _inputs(), anchor_store=authority)


@pytest.mark.parametrize("operation", ["create", "cas"])
def test_processes_cannot_both_win_the_same_revision(tmp_path: Path, operation: str):
    authority = _authority(tmp_path)
    if operation == "cas":
        authority.create("shared/key", b"initial")
    root, run_root, repo_root = _roots(tmp_path)
    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(2)
    results = context.Queue()
    children = [context.Process(target=_race_worker, args=(
        str(root), str(run_root), str(repo_root), barrier, results, operation,
    )) for _ in range(2)]
    for child in children:
        child.start()
    try:
        outcomes = [results.get(timeout=10) for _ in children]
    finally:
        for child in children:
            child.join(timeout=10)
            if child.is_alive():
                child.terminate()
                child.join(timeout=10)
    assert sorted(outcomes) == [("conflict", None), ("ok", 1 if operation == "create" else 2)]
    assert all(child.exitcode == 0 for child in children)
    assert authority.read("shared/key").revision == (1 if operation == "create" else 2)


def test_same_process_controllers_cannot_both_win_cas(tmp_path: Path):
    authority = _authority(tmp_path)
    authority.create("shared/key", b"initial")
    root, run_root, repo_root = _roots(tmp_path)
    barrier = threading.Barrier(2)

    def attempt() -> bool:
        controller = LocalAnchorAuthority(root, run_root=run_root, repo_root=repo_root)
        barrier.wait()
        try:
            controller.compare_and_set("shared/key", 1, b"next")
            return True
        except RunStateError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: attempt(), range(2)))
    assert sorted(outcomes) == [False, True]
    assert authority.read("shared/key") == fanout.AnchorRevision(2, b"next")


@pytest.mark.parametrize("damage", ["truncated", "symlink", "hardlink", "fifo", "missing"])
def test_damaged_or_aliased_record_refuses_read_and_cas(tmp_path: Path, damage: str):
    authority = _authority(tmp_path)
    authority.create("run/key", b"original")
    root, _, _ = _roots(tmp_path)
    record = _record_file(root)
    if damage == "truncated":
        record.write_bytes(b'{"revision":')
    elif damage == "symlink":
        record.unlink()
        record.symlink_to(tmp_path / "outside")
    elif damage == "hardlink":
        os.link(record, tmp_path / "alias")
    elif damage == "fifo":
        record.unlink()
        os.mkfifo(record)
    else:
        record.unlink()
    with pytest.raises(RunStateError):
        authority.read("run/key")
    with pytest.raises(RunStateError):
        authority.compare_and_set("run/key", 1, b"second")


@pytest.mark.parametrize("entry", ["authority.json", ".authority.lock"])
@pytest.mark.parametrize("damage", ["symlink", "hardlink", "fifo"])
def test_damaged_bootstrap_files_refuse_reopen_and_probe(tmp_path: Path, entry: str, damage: str):
    authority = _authority(tmp_path)
    root, run_root, repo_root = _roots(tmp_path)
    target = root / entry
    if damage == "symlink":
        target.unlink()
        target.symlink_to(tmp_path / "outside")
    elif damage == "hardlink":
        os.link(target, tmp_path / "alias")
    else:
        target.unlink()
        os.mkfifo(target, 0o600)
    with pytest.raises(RunStateError):
        LocalAnchorAuthority(root, run_root=run_root, repo_root=repo_root)
    with pytest.raises(RunStateError):
        authority.probe()


def test_record_mutations_fsync_file_and_directory(tmp_path: Path, monkeypatch):
    authority = _authority(tmp_path)
    runtime = sys.modules[f"{SPEC.name}.runstate"]
    original_fsync = os.fsync
    synced_modes: list[int] = []

    def record_fsync(fd: int) -> None:
        synced_modes.append(os.fstat(fd).st_mode)
        original_fsync(fd)

    monkeypatch.setattr(runtime.os, "fsync", record_fsync)
    authority.create("run/key", b"first")
    assert any(stat.S_ISREG(mode) for mode in synced_modes)
    assert any(stat.S_ISDIR(mode) for mode in synced_modes)
    synced_modes.clear()
    authority.compare_and_set("run/key", 1, b"second")
    assert any(stat.S_ISREG(mode) for mode in synced_modes)
    assert any(stat.S_ISDIR(mode) for mode in synced_modes)


@pytest.mark.parametrize("operation", ["create", "cas"])
def test_read_refuses_visible_revision_while_directory_fsync_fails(
        tmp_path: Path, monkeypatch, operation: str):
    authority = _authority(tmp_path)
    if operation == "cas":
        authority.create("run/key", b"initial")
    runtime = sys.modules[f"{SPEC.name}.runstate"]
    original_fsync = os.fsync
    original_replace = os.replace
    replaced = False

    def mark_replace(*args, **kwargs) -> None:
        nonlocal replaced
        original_replace(*args, **kwargs)
        replaced = True

    def fail_directory_fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode) and (operation == "create" or replaced):
            raise OSError("injected directory fsync failure")
        original_fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(runtime.os, "fsync", fail_directory_fsync)
        if operation == "cas":
            patch.setattr(runtime.os, "replace", mark_replace)
        with pytest.raises(RunStateError):
            if operation == "create":
                authority.create("run/key", b"visible")
            else:
                authority.compare_and_set("run/key", 1, b"visible")
        assert _record_file(_roots(tmp_path)[0]).exists()
        if operation == "cas":
            assert replaced
        with pytest.raises(RunStateError, match="durab|fsync"):
            authority.read("run/key")

    assert authority.read("run/key") == fanout.AnchorRevision(
        1 if operation == "create" else 2, b"visible",
    )


def test_read_syncs_file_and_directory_after_interrupted_replace(tmp_path: Path, monkeypatch):
    authority = _authority(tmp_path)
    authority.create("run/key", b"initial")
    runtime = sys.modules[f"{SPEC.name}.runstate"]
    original_replace = os.replace
    original_fsync = os.fsync

    class InterruptedWrite(BaseException):
        pass

    def replace_then_interrupt(*args, **kwargs) -> None:
        original_replace(*args, **kwargs)
        raise InterruptedWrite()

    with monkeypatch.context() as patch:
        patch.setattr(runtime.os, "replace", replace_then_interrupt)
        with pytest.raises(InterruptedWrite):
            authority.compare_and_set("run/key", 1, b"visible")

    synced_modes: list[int] = []

    def record_fsync(fd: int) -> None:
        synced_modes.append(os.fstat(fd).st_mode)
        original_fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(runtime.os, "fsync", record_fsync)
        assert authority.read("run/key") == fanout.AnchorRevision(2, b"visible")
    assert any(stat.S_ISREG(mode) for mode in synced_modes)
    assert any(stat.S_ISDIR(mode) for mode in synced_modes)
    assert next(i for i, mode in enumerate(synced_modes) if stat.S_ISREG(mode)) < next(
        i for i, mode in enumerate(synced_modes) if stat.S_ISDIR(mode)
    )


def test_interrupted_pre_replace_temp_does_not_advance_revision(tmp_path: Path):
    authority = _authority(tmp_path)
    authority.create("run/key", b"committed")
    root, _, _ = _roots(tmp_path)
    orphan = root / (".anchor-tmp-" + "a" * 32)
    orphan.write_bytes(b'{"revision":2')
    os.chmod(orphan, 0o600)

    assert authority.probe() == authority.identity
    assert authority.read("run/key") == fanout.AnchorRevision(1, b"committed")
    assert authority.compare_and_set("run/key", 1, b"next") == fanout.AnchorRevision(2, b"next")


def test_public_journal_detects_run_root_rollback(tmp_path: Path):
    authority = _authority(tmp_path)
    root, run_root, repo_root = _roots(tmp_path)
    journal, capability = fanout.RunJournal.create(run_root, _inputs(), anchor_store=authority)
    token = capability.export_token().encode("ascii")
    assert list(repo_root.iterdir()) == []
    assert all(token not in path.read_bytes() for path in root.iterdir())
    old_events = (run_root / "events.jsonl").read_bytes()
    journal.append("dispatch-intent", task_id="work", seat_id="claude", attempt=1, round=1)
    journal.close()
    with (run_root / "events.jsonl").open("wb") as stream:
        stream.write(old_events)
        stream.flush()
        os.fsync(stream.fileno())
    with pytest.raises(RunStateError, match="authority|journal"):
        fanout.RunJournal.resume(run_root, _inputs(), capability, anchor_store=authority)


def test_unavailable_authority_blocks_dispatch_intent(tmp_path: Path):
    authority = _authority(tmp_path)
    root, run_root, repo_root = _roots(tmp_path)
    journal, capability = fanout.RunJournal.create(run_root, _inputs(), anchor_store=authority)
    (root / "authority.json").write_bytes(b"torn")
    try:
        with pytest.raises(RunStateError, match="authority"):
            journal.append("dispatch-intent", task_id="work", seat_id="claude", attempt=1, round=1)
        assert journal.state.seq == 0
    finally:
        journal.close()
    with pytest.raises(RunStateError):
        LocalAnchorAuthority(root, run_root=run_root, repo_root=repo_root)
    with pytest.raises(RunStateError, match="authority"):
        fanout.RunJournal.resume(run_root, _inputs(), capability, anchor_store=authority)


def test_public_scheduler_accepts_local_authority(tmp_path: Path):
    scheduler_module = sys.modules[f"{SPEC.name}.scheduler_authority"]
    plan_data = {
        "schema_version": "v1",
        "source": {"path": "plan.md", "sha256": "a" * 64, "parser_version": "parser-v1"},
        "defaults": {"executor_ids": ["claude", "codex"], "rounds": 1, "timeout": 120,
                     "retries": 0, "minimum_success": 2},
        "source_steps": [{"id": "Task 1/Step 1", "sha256": "b" * 64}],
        "tasks": [{"id": "work", "kind": "work", "parent_id": None, "title": "Work",
                   "objective": "Complete work.", "source_step_ids": ["Task 1/Step 1"],
                   "depends_on": [], "execution_class": "read-only", "required_skills": [],
                   "none_reason": "No specialist skill is needed.", "owned_paths": [],
                   "acceptance": ["Work is complete."], "checks": [], "provider_policy": None}],
    }
    plan = fanout.FanoutPlanV1.from_dict(plan_data)
    inputs = fanout.RunInputs(
        run_id="run-1", compiled_plan_sha256=hashlib.sha256(fanout.canonical_json(plan.to_dict())).hexdigest(),
        source_sha256="a" * 64, draft_sha256="c" * 64,
        compiler_sha256="d" * 64, parser_sha256="e" * 64,
        provider_profiles={"claude": "f" * 64, "codex": "2" * 64},
        skill_manifests={"work": "0" * 64},
        repo_baseline_sha256="1" * 64,
    )
    owner = fanout.OwnerCapability.from_token("o" * 43)
    authority = _authority(tmp_path)
    artifacts = fanout.ArtifactStore(tmp_path / "artifacts")

    @dataclass
    class Backend:
        record: object = None

        def identity(self): return "backend/v1"
        def key(self): return "scheduler/run-1"
        def read(self): return self.record
        def compare_and_set(self, expected_revision, snapshot, *, owner):
            actual = 0 if self.record is None else self.record.revision
            if actual != expected_revision:
                raise fanout.SchedulerConflictError("stale backend")
            self.record = scheduler_module.BackendRecord(actual + 1, snapshot)
            return self.record

    backend = Backend()
    scheduler = scheduler_module.Scheduler.create(
        plan, inputs, backend, artifacts, owner=owner, anchor_store=authority,
    )
    root, run_root, repo_root = _roots(tmp_path)
    metadata = root / "authority.json"
    intact = metadata.read_bytes()
    metadata.write_bytes(b"torn")
    with pytest.raises(fanout.SchedulerConflictError, match="authority"):
        scheduler.schedule_ready(owner=owner)
    assert backend.record.snapshot.tasks[0].phase == "unscheduled"
    metadata.write_bytes(intact)
    assert scheduler.schedule_ready(owner=owner)[0].task_id == "work"
    reopened = LocalAnchorAuthority(root, run_root=run_root, repo_root=repo_root)
    resumed = scheduler_module.Scheduler.resume(
        plan, inputs, backend, artifacts, owner=owner, anchor_store=reopened,
    )
    assert resumed.schedule_ready(owner=owner) == ()
