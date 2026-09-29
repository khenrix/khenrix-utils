"""Non-mutating collection, transactional handover, and exact-owner GC contracts."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
FANOUT_ROOT = ROOT / "shared" / "lib" / "fanout"
SPEC = importlib.util.spec_from_file_location(
    "_fanout_lifecycle_contracts",
    FANOUT_ROOT / "__init__.py",
    submodule_search_locations=[str(FANOUT_ROOT)],
)
assert SPEC and SPEC.loader
fanout = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fanout
SPEC.loader.exec_module(fanout)


GarbageCollectionError = fanout.GarbageCollectionError
HandoverError = fanout.HandoverError
LifecycleError = fanout.LifecycleError
capture_repository_baseline = fanout.capture_repository_baseline
claim_run_path = fanout.claim_run_path
collect_candidates = fanout.collect_candidates
create_candidate = fanout.create_candidate
create_seat_workspace = fanout.create_seat_workspace
garbage_collect = fanout.garbage_collect
handover_candidate = fanout.handover_candidate
verify_candidate = fanout.verify_candidate
PlanCheckV1 = fanout.PlanCheckV1


def _git_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for name in tuple(environment):
        if name.startswith("GIT_"):
            environment.pop(name)
    environment.update({
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    })
    return environment


def _git(repository: Path, *args: str) -> bytes:
    completed = subprocess.run(
        ("git", "-C", os.fspath(repository), *args),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_git_environment(),
        check=False,
    )
    if completed.returncode:
        raise AssertionError(completed.stderr.decode("utf-8", "replace"))
    return completed.stdout


def _repository(tmp_path: Path, *, directory_case: bool = False) -> Path:
    repository = tmp_path / "caller"
    repository.mkdir()
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.name", "Fixture")
    _git(repository, "config", "user.email", "fixture@example.invalid")
    (repository / "changed.txt").write_text("before\n", encoding="utf-8")
    (repository / "remove.txt").write_text("remove\n", encoding="utf-8")
    if directory_case:
        (repository / ".gitignore").write_text("dir/ignored\n", encoding="utf-8")
        (repository / "dir").mkdir()
        (repository / "dir" / "old.txt").write_text("old\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "baseline")
    return repository


def _tree(root: Path) -> dict[str, tuple[str, int, bytes]]:
    entries: dict[str, tuple[str, int, bytes]] = {}
    for path in sorted(root.rglob("*")):
        if ".git" in path.parts:
            continue
        relative = path.relative_to(root).as_posix()
        mode = stat.S_IMODE(path.lstat().st_mode)
        if path.is_symlink():
            entries[relative] = ("symlink", mode, os.fsencode(os.readlink(path)))
        elif path.is_file():
            entries[relative] = ("file", mode, path.read_bytes())
    return entries


def _git_metadata(repository: Path) -> tuple[bytes, bytes, bytes, bytes]:
    git_dir = Path(_git(repository, "rev-parse", "--absolute-git-dir").decode().strip())
    return (
        (git_dir / "index").read_bytes(),
        _git(repository, "show-ref", "--head", "--dereference"),
        (git_dir / "config").read_bytes(),
        _git(repository, "worktree", "list", "--porcelain"),
    )


def _receipt(tmp_path: Path, repository: Path, *, seat_id: str = "claude") -> tuple[object, object, object]:
    baseline = capture_repository_baseline(repository)
    seat = create_seat_workspace(baseline, tmp_path / "run", seat_id)
    (seat.root / "changed.txt").write_text("after\n", encoding="utf-8")
    (seat.root / "remove.txt").unlink()
    candidate = create_candidate(baseline, seat.root)
    controller = _controller(tmp_path)
    return baseline, verify_candidate(
        baseline,
        candidate,
        (_passing_check(),),
        controller=controller,
    ), controller


def _passing_check() -> object:
    return PlanCheckV1(
        argv=(sys.executable, "-c", "import sys; sys.exit(0)"),
        cwd="",
        env_allowlist=(),
        timeout=5,
        accepted_exit_codes=(0,),
        expected_artifacts=(),
    )


def _controller(tmp_path: Path) -> object:
    return fanout.create_lifecycle_controller(tmp_path / "controller")


def _incomplete_recovery_transaction(
    tmp_path: Path,
    repository: Path,
    baseline: object,
    controller: object,
    *,
    action: str,
    path: str,
    expected: dict[str, object],
) -> tuple[object, Path, tuple[object, ...]]:
    lifecycle = sys.modules[f"{fanout.__name__}.lifecycle"]
    transaction = controller.root / "transactions" / f"incomplete-{action}"
    transaction.mkdir()
    before = lifecycle._snapshot_tree(repository)
    backup = transaction / "backup"
    lifecycle._make_private_directory(backup)
    lifecycle._restore_tree(backup, before)
    device, inode = lifecycle._directory_identity(repository)
    journal = lifecycle._HandoverJournal(transaction / "handover.jsonl")
    journal.record(
        "transaction-opened",
        details={
            "baseline_digest": baseline.digest,
            "candidate_digest": "0" * 64,
            "controller_id": controller.controller_id,
            "destination": os.fspath(repository),
            "destination_device": device,
            "destination_inode": inode,
        },
    )
    journal.record("backup-created", details={"tree_digest": lifecycle._tree_digest(before)})
    journal.record(
        f"destination-{action}-intent",
        path,
        details={"expected": expected},
    )
    journal.close()
    return lifecycle, transaction, before


def _directory_mode_recovery_transaction(
    tmp_path: Path,
    *,
    action: str,
    target_mode: int,
) -> tuple[object, object, Path, Path, Path, str, object]:
    repository = _repository(tmp_path)
    private = repository / "private"
    private.mkdir()
    (private / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _git(repository, "add", "private/tracked.txt")
    _git(repository, "commit", "-qm", "private directory")
    private.chmod(0o755)
    baseline = capture_repository_baseline(repository)
    controller = _controller(tmp_path)
    lifecycle = sys.modules[f"{fanout.__name__}.lifecycle"]
    transaction = controller.root / "transactions" / f"recovery-{action}"
    transaction.mkdir()
    before = lifecycle._snapshot_tree(repository)
    backup = transaction / "backup"
    lifecycle._make_private_directory(backup)
    lifecycle._restore_tree(backup, before)
    device, inode = lifecycle._directory_identity(repository)
    source_quarantine = "source-" + "1" * 32
    journal = lifecycle._HandoverJournal(transaction / "handover.jsonl")
    journal.record(
        "transaction-opened",
        details={
            "baseline_digest": baseline.digest,
            "candidate_digest": "0" * 64,
            "controller_id": controller.controller_id,
            "destination": os.fspath(repository),
            "destination_device": device,
            "destination_inode": inode,
        },
    )
    journal.record(
        "backup-created",
        details={"tree_digest": lifecycle._tree_digest(before)},
    )
    journal.record(
        f"destination-{action}-intent",
        "private",
        details={
            "before": {
                "kind": "directory",
                "mode": 0o755,
                "data_sha256": hashlib.sha256(b"").hexdigest(),
            },
            "expected": {
                "kind": "directory",
                "mode": target_mode,
                "data_sha256": hashlib.sha256(b"").hexdigest(),
            },
            "source_quarantine": source_quarantine,
        },
    )
    return lifecycle, controller, transaction, repository, private, source_quarantine, journal


def test_collection_reads_seat_workspaces_without_mutating_them(tmp_path):
    """Collection that writes a peer checkout could contaminate a later candidate or round."""
    repository = _repository(tmp_path)
    baseline = capture_repository_baseline(repository)
    first = create_seat_workspace(baseline, tmp_path / "run", "claude")
    second = create_seat_workspace(baseline, tmp_path / "run", "codex")
    (first.root / "changed.txt").write_text("first\n", encoding="utf-8")
    (second.root / "changed.txt").write_text("second\n", encoding="utf-8")
    before = {
        seat.seat_id: (_tree(seat.root), (seat.root / ".git" / "index").read_bytes())
        for seat in (first, second)
    }

    collected = collect_candidates(baseline, {"codex": second, "claude": first})

    assert tuple(item.seat_id for item in collected) == ("claude", "codex")
    assert tuple(item.candidate.entries[0].data for item in collected) == (b"first\n", b"second\n")
    assert before == {
        seat.seat_id: (_tree(seat.root), (seat.root / ".git" / "index").read_bytes())
        for seat in (first, second)
    }


def test_handover_refuses_destination_baseline_drift_before_any_candidate_change(tmp_path):
    """Applying a valid candidate to a drifted caller would overwrite work made after dispatch."""
    repository = _repository(tmp_path)
    baseline, receipt, controller = _receipt(tmp_path, repository)
    (repository / "changed.txt").write_text("caller drift\n", encoding="utf-8")

    with pytest.raises(HandoverError, match="baseline"):
        handover_candidate(baseline, receipt, controller=controller)

    assert (repository / "changed.txt").read_text(encoding="utf-8") == "caller drift\n"
    assert (repository / "remove.txt").exists()


def test_handover_stages_applies_and_journals_without_changing_git_metadata(tmp_path):
    """A successful handover must change only selected working-tree bytes and leave Git control data alone."""
    repository = _repository(tmp_path)
    baseline, receipt, controller = _receipt(tmp_path, repository)
    metadata_before = _git_metadata(repository)

    result = handover_candidate(baseline, receipt, controller=controller)

    assert (repository / "changed.txt").read_text(encoding="utf-8") == "after\n"
    assert not (repository / "remove.txt").exists()
    assert _git_metadata(repository) == metadata_before
    records = [json.loads(line) for line in result.journal.read_text(encoding="utf-8").splitlines()]
    steps = [record["step"] for record in records]
    assert "baseline-verified" in steps
    assert "stage-verified" in steps
    assert "destination-applied" in steps
    assert "final-verification-passed" in steps
    assert steps[-1] == "handover-complete"
    for action in ("destination-delete", "destination-write"):
        assert steps.index(f"{action}-intent") < steps.index(action)
    final_evidence = next(record["details"] for record in records if record["step"] == "final-check-outcomes")
    assert final_evidence["failure"] is None
    assert final_evidence["outcomes"] == [
        {
            "artifacts": [],
            "argv_digest": receipt.outcomes[0].argv_digest,
            "failure": None,
            "index": 0,
            "returncode": 0,
            "status": "exit",
        }
    ]


def test_handover_refuses_a_receipt_rebound_away_from_controller_evidence(tmp_path):
    """A copied dataclass must not borrow a verifier pass after any field is changed."""
    repository = _repository(tmp_path)
    baseline, receipt, controller = _receipt(tmp_path, repository)
    rebound = replace(receipt, workspace=tmp_path.resolve())

    with pytest.raises(HandoverError, match="authenticated"):
        handover_candidate(baseline, rebound, controller=controller)

    assert (repository / "changed.txt").read_text(encoding="utf-8") == "before\n"


def test_handover_preserves_empty_and_new_directory_modes(tmp_path):
    """Directory transitions are content-addressed state, including an empty final directory."""
    repository = _repository(tmp_path)
    baseline = capture_repository_baseline(repository)
    seat = create_seat_workspace(baseline, tmp_path / "run", "claude")
    (seat.root / "changed.txt").unlink()
    (seat.root / "changed.txt").mkdir()
    (seat.root / "changed.txt").chmod(0o750)
    (seat.root / "new-parent").mkdir()
    (seat.root / "new-parent").chmod(0o751)
    (seat.root / "new-parent" / "value.txt").write_text("new\n", encoding="utf-8")
    candidate = create_candidate(baseline, seat.root)
    controller = _controller(tmp_path)
    check = PlanCheckV1(
        argv=(
            sys.executable,
            "-c",
            "from pathlib import Path; import stat; "
            "assert Path('changed.txt').is_dir(); "
            "assert stat.S_IMODE(Path('changed.txt').stat().st_mode) == 0o750; "
            "assert stat.S_IMODE(Path('new-parent').stat().st_mode) == 0o751",
        ),
        cwd="",
        env_allowlist=(),
        timeout=5,
        accepted_exit_codes=(0,),
        expected_artifacts=(),
    )
    receipt = verify_candidate(baseline, candidate, (check,), controller=controller)

    handover_candidate(baseline, receipt, controller=controller)

    assert (repository / "changed.txt").is_dir()
    assert stat.S_IMODE((repository / "changed.txt").stat().st_mode) == 0o750
    assert stat.S_IMODE((repository / "new-parent").stat().st_mode) == 0o751
    assert (repository / "new-parent" / "value.txt").read_text(encoding="utf-8") == "new\n"


def test_file_delta_does_not_change_an_unchanged_baseline_directory_mode(tmp_path):
    """A synthetic seat's default parent mode must not become a caller directory-mode update."""
    repository = _repository(tmp_path)
    private = repository / "private-dir"
    private.mkdir()
    (private / "tracked.txt").write_text("before\n", encoding="utf-8")
    _git(repository, "add", "private-dir/tracked.txt")
    _git(repository, "commit", "-qm", "private directory")
    private.chmod(0o700)
    baseline = capture_repository_baseline(repository)
    seat = create_seat_workspace(baseline, tmp_path / "run", "claude")
    (seat.root / "private-dir" / "tracked.txt").write_text("after\n", encoding="utf-8")

    candidate = create_candidate(baseline, seat.root)
    controller = _controller(tmp_path)
    receipt = verify_candidate(baseline, candidate, (_passing_check(),), controller=controller)

    assert all(entry.path != "private-dir" for entry in candidate.entries)
    handover_candidate(baseline, receipt, controller=controller)
    assert stat.S_IMODE(private.stat().st_mode) == 0o700
    assert (private / "tracked.txt").read_text(encoding="utf-8") == "after\n"


def test_handover_restores_the_full_pre_handover_tree_when_application_cannot_finish(tmp_path):
    """Deleting an old path before a later conflict must not leave the caller partially handed over."""
    repository = _repository(tmp_path, directory_case=True)
    baseline = capture_repository_baseline(repository)
    seat = create_seat_workspace(baseline, tmp_path / "run", "claude")
    (seat.root / "dir" / "old.txt").unlink()
    (seat.root / "dir").rmdir()
    (seat.root / "dir").write_text("candidate file\n", encoding="utf-8")
    candidate = create_candidate(baseline, seat.root)
    controller = _controller(tmp_path)
    receipt = verify_candidate(
        baseline,
        candidate,
        (_passing_check(),),
        controller=controller,
    )
    (repository / "dir" / "ignored").write_text("keep this ignored file\n", encoding="utf-8")
    before = _tree(repository)

    with pytest.raises(HandoverError):
        handover_candidate(baseline, receipt, controller=controller)

    assert _tree(repository) == before
    assert (repository / "dir" / "old.txt").read_text(encoding="utf-8") == "old\n"
    assert (repository / "dir" / "ignored").read_text(encoding="utf-8") == "keep this ignored file\n"


def test_handover_refuses_an_ignored_escaping_link_before_backup_or_partial_application(tmp_path):
    """A backup that follows or cannot restore an ignored escaping link would strand a partial handover."""
    repository = _repository(tmp_path, directory_case=True)
    baseline = capture_repository_baseline(repository)
    seat = create_seat_workspace(baseline, tmp_path / "run", "claude")
    (seat.root / "dir" / "old.txt").unlink()
    (seat.root / "dir").rmdir()
    (seat.root / "dir").write_text("candidate file\n", encoding="utf-8")
    candidate = create_candidate(baseline, seat.root)
    controller = _controller(tmp_path)
    receipt = verify_candidate(
        baseline,
        candidate,
        (_passing_check(),),
        controller=controller,
    )
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (repository / "dir" / "ignored").symlink_to(outside)

    with pytest.raises(HandoverError, match="symlink"):
        handover_candidate(baseline, receipt, controller=controller)

    assert (repository / "dir" / "old.txt").read_text(encoding="utf-8") == "old\n"
    assert (repository / "dir" / "ignored").is_symlink()
    assert outside.read_text(encoding="utf-8") == "outside\n"


def test_handover_rolls_back_when_the_completion_journal_write_fails(tmp_path, monkeypatch):
    """A durable journal failure after backup must restore rather than strand caller changes."""
    repository = _repository(tmp_path)
    baseline, receipt, controller = _receipt(tmp_path, repository)
    before = _tree(repository)
    lifecycle = sys.modules[f"{fanout.__name__}.lifecycle"]
    original = lifecycle._HandoverJournal.record

    def fail_completion(self, step, path=None, **kwargs):
        if step == "handover-complete":
            raise OSError("injected completion fsync failure")
        return original(self, step, path)

    monkeypatch.setattr(lifecycle._HandoverJournal, "record", fail_completion)

    with pytest.raises(HandoverError):
        handover_candidate(baseline, receipt, controller=controller)

    assert _tree(repository) == before


def test_torn_completion_record_is_never_followed_by_an_interior_journal_append_and_is_recoverable(tmp_path, monkeypatch):
    """A failed append must leave at most one torn tail, never poison rollback recovery."""
    repository = _repository(tmp_path)
    baseline, receipt, controller = _receipt(tmp_path, repository)
    before = _tree(repository)
    lifecycle = sys.modules[f"{fanout.__name__}.lifecycle"]
    original = lifecycle._HandoverJournal.record

    def tear_completion(self, step, path=None, **kwargs):
        if step == "handover-complete":
            self._handle.write(b'{"sequence":')
            self._handle.flush()
            raise OSError("injected torn completion append")
        return original(self, step, path, **kwargs)

    monkeypatch.setattr(lifecycle._HandoverJournal, "record", tear_completion)

    with pytest.raises(HandoverError):
        handover_candidate(baseline, receipt, controller=controller)

    transaction = next((controller.root / "transactions").iterdir())
    assert _tree(repository) == before
    assert not fanout.recover_handover(controller, transaction)
    assert all(
        json.loads(line)["step"] != "handover-complete"
        for line in (transaction / "handover.jsonl").read_text(encoding="utf-8").splitlines()
    )


def test_close_failure_after_durable_completion_is_a_committed_handover_not_a_false_rollback(tmp_path, monkeypatch):
    """Once completion is durable, a close error cannot report failure after restoring old bytes."""
    repository = _repository(tmp_path)
    baseline, receipt, controller = _receipt(tmp_path, repository)
    lifecycle = sys.modules[f"{fanout.__name__}.lifecycle"]
    original = lifecycle._HandoverJournal.close
    failed = False

    def fail_after_close(self):
        nonlocal failed
        original(self)
        if not failed:
            failed = True
            raise OSError("injected close failure after completion")

    monkeypatch.setattr(lifecycle._HandoverJournal, "close", fail_after_close)

    result = handover_candidate(baseline, receipt, controller=controller)

    assert (repository / "changed.txt").read_text(encoding="utf-8") == "after\n"
    assert not (repository / "remove.txt").exists()
    assert json.loads(result.journal.read_text(encoding="utf-8").splitlines()[-1])["step"] == "handover-complete"


def test_completion_append_error_after_writing_bytes_is_not_recovered_as_a_false_success(tmp_path, monkeypatch):
    """A completion call that raises is rollback evidence even if its final bytes were emitted."""
    repository = _repository(tmp_path)
    baseline, receipt, controller = _receipt(tmp_path, repository)
    before = _tree(repository)
    lifecycle = sys.modules[f"{fanout.__name__}.lifecycle"]
    original = lifecycle._HandoverJournal.record

    def write_then_fail_completion(self, step, path=None, **kwargs):
        result = original(self, step, path, **kwargs)
        if step == "handover-complete":
            raise OSError("injected post-write completion failure")
        return result

    monkeypatch.setattr(lifecycle._HandoverJournal, "record", write_then_fail_completion)

    with pytest.raises(HandoverError):
        handover_candidate(baseline, receipt, controller=controller)

    transaction = next((controller.root / "transactions").iterdir())
    records = [json.loads(line) for line in (transaction / "handover.jsonl").read_text(encoding="utf-8").splitlines()]
    assert _tree(repository) == before
    assert records[-1]["step"] == "rollback-complete"
    assert all(record["step"] != "handover-complete" for record in records)
    assert not fanout.recover_handover(controller, transaction)


def test_handover_revalidates_the_caller_after_stage_before_direct_writes(tmp_path, monkeypatch):
    """A caller edit after stage verification must not be overwritten by the direct apply."""
    repository = _repository(tmp_path)
    baseline, receipt, controller = _receipt(tmp_path, repository)
    lifecycle = sys.modules[f"{fanout.__name__}.lifecycle"]
    original = lifecycle._run_checks
    injected = False

    def edit_after_stage(workspace, checks, environment):
        nonlocal injected
        outcomes, failure = original(workspace, checks, environment)
        if not injected:
            injected = True
            (repository / "changed.txt").write_text("late caller edit\n", encoding="utf-8")
        return outcomes, failure

    monkeypatch.setattr(lifecycle, "_run_checks", edit_after_stage)

    with pytest.raises(HandoverError, match="destination"):
        handover_candidate(baseline, receipt, controller=controller)

    assert (repository / "changed.txt").read_text(encoding="utf-8") == "late caller edit\n"
    assert (repository / "remove.txt").exists()


def test_handover_preserves_a_caller_edit_detected_before_the_first_direct_mutation(tmp_path, monkeypatch):
    """A post-backup race must be refused without restoring over the newly edited caller bytes."""
    repository = _repository(tmp_path)
    baseline, receipt, controller = _receipt(tmp_path, repository)
    lifecycle = sys.modules[f"{fanout.__name__}.lifecycle"]
    original = lifecycle._HandoverJournal.record

    def edit_after_backup(self, step, path=None, **kwargs):
        result = original(self, step, path, **kwargs)
        if step == "backup-created":
            (repository / "changed.txt").write_text("late backup-window edit\n", encoding="utf-8")
        return result

    monkeypatch.setattr(lifecycle._HandoverJournal, "record", edit_after_backup)

    with pytest.raises(HandoverError, match="destination"):
        handover_candidate(baseline, receipt, controller=controller)

    assert (repository / "changed.txt").read_text(encoding="utf-8") == "late backup-window edit\n"
    assert (repository / "remove.txt").exists()
    transaction = next((controller.root / "transactions").iterdir())
    assert not fanout.recover_handover(controller, transaction)
    assert (repository / "changed.txt").read_text(encoding="utf-8") == "late backup-window edit\n"


def test_handover_rechecks_target_identity_inside_the_write_after_its_journal_intent(tmp_path, monkeypatch):
    """An edit between write intent and the actual unlink/create must be refused, not overwritten."""
    repository = _repository(tmp_path)
    baseline, receipt, controller = _receipt(tmp_path, repository)
    lifecycle = sys.modules[f"{fanout.__name__}.lifecycle"]
    original = lifecycle._HandoverJournal.record
    injected = False

    def edit_after_write_intent(self, step, path=None, **kwargs):
        nonlocal injected
        result = original(self, step, path, **kwargs)
        if step == "destination-write-intent" and path == "changed.txt" and not injected:
            injected = True
            (repository / "changed.txt").write_text("late write-window edit\n", encoding="utf-8")
        return result

    monkeypatch.setattr(lifecycle._HandoverJournal, "record", edit_after_write_intent)

    with pytest.raises(HandoverError, match="destination"):
        handover_candidate(baseline, receipt, controller=controller)

    assert (repository / "changed.txt").read_text(encoding="utf-8") == "late write-window edit\n"
    assert (repository / "remove.txt").exists()


def test_handover_rechecks_inside_the_descriptor_write_before_replacing_a_file(tmp_path, monkeypatch):
    """A pathname edit immediately before the descriptor operation must not be overwritten."""
    repository = _repository(tmp_path)
    baseline, receipt, controller = _receipt(tmp_path, repository)
    verification = sys.modules[f"{fanout.__name__}.verification"]
    original = verification._write_entry
    injected = False

    def edit_immediately_before_descriptor_write(root, entry, *, guard=None, quarantine=None, created=None):
        nonlocal injected
        if (
            isinstance(root, int)
            and os.fstat(root).st_ino == repository.stat().st_ino
            and entry.path == "changed.txt"
            and not injected
        ):
            injected = True
            (repository / "changed.txt").write_text("late descriptor-window edit\n", encoding="utf-8")
        return original(root, entry, guard=guard, quarantine=quarantine, created=created)

    monkeypatch.setattr(verification, "_write_entry", edit_immediately_before_descriptor_write)

    with pytest.raises(HandoverError, match="destination"):
        handover_candidate(baseline, receipt, controller=controller)

    assert injected
    assert (repository / "changed.txt").read_text(encoding="utf-8") == "late descriptor-window edit\n"
    assert (repository / "remove.txt").exists()


def test_handover_never_deletes_a_peer_swapped_after_its_final_mutation_guard(tmp_path, monkeypatch):
    """The final guard must bind the identity that is actually removed, not only its earlier spelling."""
    repository = _repository(tmp_path)
    baseline, receipt, controller = _receipt(tmp_path, repository)
    verification = sys.modules[f"{fanout.__name__}.verification"]
    original = verification._mutation_guard
    parked = tmp_path / "parked-baseline-remove.txt"
    injected = False

    def swap_after_guard(guard, action, path):
        inner = original(guard, action, path)
        if inner is None or action != "delete" or path != "remove.txt":
            return inner

        def guarded():
            nonlocal injected
            inner()
            if not injected:
                injected = True
                os.rename(repository / "remove.txt", parked)
                (repository / "remove.txt").write_text("late peer bytes\n", encoding="utf-8")

        return guarded

    monkeypatch.setattr(verification, "_mutation_guard", swap_after_guard)

    with pytest.raises(HandoverError):
        handover_candidate(baseline, receipt, controller=controller)

    assert injected
    assert (repository / "remove.txt").read_text(encoding="utf-8") == "late peer bytes\n"


def test_handover_restores_a_quarantined_source_when_replacement_creation_fails(tmp_path, monkeypatch):
    """A write intent that loses its source before creation must still roll back from the backup."""
    repository = _repository(tmp_path)
    baseline, receipt, controller = _receipt(tmp_path, repository)
    verification = sys.modules[f"{fanout.__name__}.verification"]
    original = verification.os.open
    injected = False

    def fail_direct_replacement(name, flags, *args, **kwargs):
        nonlocal injected
        descriptor = kwargs.get("dir_fd")
        if (
            name == "changed.txt"
            and flags & os.O_CREAT
            and descriptor is not None
            and os.fstat(descriptor).st_ino == repository.stat().st_ino
            and not injected
        ):
            injected = True
            raise OSError("injected replacement creation failure")
        return original(name, flags, *args, **kwargs)

    monkeypatch.setattr(verification.os, "open", fail_direct_replacement)

    with pytest.raises(HandoverError):
        handover_candidate(baseline, receipt, controller=controller)

    assert injected
    assert (repository / "changed.txt").read_text(encoding="utf-8") == "before\n"
    assert (repository / "remove.txt").read_text(encoding="utf-8") == "remove\n"


def test_handover_refuses_destination_path_rebinding_and_never_mutates_the_replacement(tmp_path, monkeypatch):
    """A lock on one inode cannot authorize writes through a later replacement pathname."""
    repository = _repository(tmp_path)
    baseline, receipt, controller = _receipt(tmp_path, repository)
    lifecycle = sys.modules[f"{fanout.__name__}.lifecycle"]
    original = lifecycle._HandoverJournal.record
    displaced = tmp_path / "locked-original"
    replacement = tmp_path / "replacement"
    injected = False

    def rebind_after_backup(self, step, path=None, **kwargs):
        nonlocal injected
        result = original(self, step, path, **kwargs)
        if step == "backup-created" and not injected:
            injected = True
            shutil.copytree(repository, replacement, symlinks=True)
            os.rename(repository, displaced)
            os.rename(replacement, repository)
        return result

    monkeypatch.setattr(lifecycle._HandoverJournal, "record", rebind_after_backup)

    with pytest.raises(HandoverError, match="identity"):
        handover_candidate(baseline, receipt, controller=controller)

    for root in (repository, displaced):
        assert (root / "changed.txt").read_text(encoding="utf-8") == "before\n"
        assert (root / "remove.txt").read_text(encoding="utf-8") == "remove\n"


def test_handover_never_reports_success_when_the_destination_rebinds_at_completion(tmp_path, monkeypatch):
    """The durable completion boundary must be followed by a pathname-identity disposition."""
    repository = _repository(tmp_path)
    baseline, receipt, controller = _receipt(tmp_path, repository)
    lifecycle = sys.modules[f"{fanout.__name__}.lifecycle"]
    original = lifecycle._HandoverJournal.record
    displaced = tmp_path / "committed-displaced-caller"
    replacement = tmp_path / "completion-replacement"
    shutil.copytree(repository, replacement, symlinks=True)
    injected = False

    def rebind_at_completion(self, step, path=None, **kwargs):
        nonlocal injected
        if step == "handover-complete" and not injected:
            injected = True
            os.rename(repository, displaced)
            os.rename(replacement, repository)
        return original(self, step, path, **kwargs)

    monkeypatch.setattr(lifecycle._HandoverJournal, "record", rebind_at_completion)

    with pytest.raises(HandoverError):
        handover_candidate(baseline, receipt, controller=controller)

    assert injected
    assert (repository / "changed.txt").read_text(encoding="utf-8") == "before\n"
    assert (repository / "remove.txt").read_text(encoding="utf-8") == "remove\n"
    assert (displaced / "changed.txt").read_text(encoding="utf-8") == "after\n"
    assert not (displaced / "remove.txt").exists()


def test_handover_rollback_restores_only_transaction_owned_paths_and_keeps_a_late_caller_file(tmp_path, monkeypatch):
    """A failed transaction may restore its deletion but must not clear a later unrelated caller edit."""
    repository = _repository(tmp_path)
    baseline, receipt, controller = _receipt(tmp_path, repository)
    lifecycle = sys.modules[f"{fanout.__name__}.lifecycle"]
    original = lifecycle._HandoverJournal.record
    injected = False

    def add_late_file_after_delete(self, step, path=None, **kwargs):
        nonlocal injected
        result = original(self, step, path, **kwargs)
        if step == "destination-delete" and path == "remove.txt" and not injected:
            injected = True
            (repository / "late-caller-edit.txt").write_text("preserve me\n", encoding="utf-8")
        return result

    monkeypatch.setattr(lifecycle._HandoverJournal, "record", add_late_file_after_delete)

    with pytest.raises(HandoverError, match="destination"):
        handover_candidate(baseline, receipt, controller=controller)

    assert (repository / "remove.txt").read_text(encoding="utf-8") == "remove\n"
    assert (repository / "late-caller-edit.txt").read_text(encoding="utf-8") == "preserve me\n"


def test_handover_rollback_preserves_a_late_replacement_after_ownership_is_observed(tmp_path, monkeypatch):
    """Rollback must bind candidate ownership to the restoration it performs, not only a prior read."""
    repository = _repository(tmp_path)
    baseline, receipt, controller = _receipt(tmp_path, repository)
    lifecycle = sys.modules[f"{fanout.__name__}.lifecycle"]
    original_checks = lifecycle._run_checks
    original_restore = lifecycle._restore_owned_path
    checks = 0
    injected = False

    def fail_only_final(workspace, check_set, environment):
        nonlocal checks
        checks += 1
        outcomes, failure = original_checks(workspace, check_set, environment)
        if checks == 2:
            return outcomes, "injected final verification failure"
        return outcomes, failure

    def replace_after_observation(destination, path, original_entry, expected_outputs, *, mutation, quarantine):
        nonlocal injected
        if path == "changed.txt" and not injected:
            injected = True
            (repository / "changed.txt").write_text("late caller replacement\n", encoding="utf-8")
        return original_restore(
            destination,
            path,
            original_entry,
            expected_outputs,
            mutation=mutation,
            quarantine=quarantine,
        )

    monkeypatch.setattr(lifecycle, "_run_checks", fail_only_final)
    monkeypatch.setattr(lifecycle, "_restore_owned_path", replace_after_observation)

    with pytest.raises(HandoverError, match="final"):
        handover_candidate(baseline, receipt, controller=controller)

    assert injected
    assert (repository / "changed.txt").read_text(encoding="utf-8") == "late caller replacement\n"


def test_handover_rejects_an_ignored_path_collision_before_it_can_be_replaced(tmp_path):
    """A candidate-created path must be absent, even when Git hides an existing caller file."""
    repository = _repository(tmp_path)
    baseline = capture_repository_baseline(repository)
    seat = create_seat_workspace(baseline, tmp_path / "run", "claude")
    (seat.root / "hidden.txt").write_text("candidate bytes\n", encoding="utf-8")
    candidate = create_candidate(baseline, seat.root)
    controller = _controller(tmp_path)
    receipt = verify_candidate(baseline, candidate, (_passing_check(),), controller=controller)
    (repository / ".git" / "info" / "exclude").write_text("hidden.txt\n", encoding="utf-8")
    (repository / "hidden.txt").write_text("caller private bytes\n", encoding="utf-8")

    with pytest.raises(HandoverError, match="destination"):
        handover_candidate(baseline, receipt, controller=controller)

    assert (repository / "hidden.txt").read_text(encoding="utf-8") == "caller private bytes\n"


def test_handover_runs_final_immutable_checks_on_the_caller_and_rolls_back_on_failure(tmp_path, monkeypatch):
    """The final caller destination needs its own immutable check evidence inside rollback."""
    repository = _repository(tmp_path)
    baseline, receipt, controller = _receipt(tmp_path, repository)
    before = _tree(repository)
    lifecycle = sys.modules[f"{fanout.__name__}.lifecycle"]
    original = lifecycle._run_checks
    calls = 0

    def fail_only_final(workspace, checks, environment):
        nonlocal calls
        calls += 1
        outcomes, failure = original(workspace, checks, environment)
        if calls == 2:
            return outcomes, "injected caller-only final check failure"
        return outcomes, failure

    monkeypatch.setattr(lifecycle, "_run_checks", fail_only_final)

    with pytest.raises(HandoverError, match="final"):
        handover_candidate(baseline, receipt, controller=controller)

    assert calls == 2
    assert _tree(repository) == before


def test_final_check_git_mutation_is_isolated_from_the_caller_git_metadata(tmp_path):
    """A valid argv check may mutate its own verifier Git state but never the caller index."""
    repository = _repository(tmp_path)
    baseline = capture_repository_baseline(repository)
    seat = create_seat_workspace(baseline, tmp_path / "run", "claude")
    (seat.root / "changed.txt").write_text("after\n", encoding="utf-8")
    candidate = create_candidate(baseline, seat.root)
    controller = _controller(tmp_path)
    receipt = verify_candidate(
        baseline,
        candidate,
        (
            PlanCheckV1(
                argv=(shutil.which("git") or "git", "add", "changed.txt"),
                cwd="",
                env_allowlist=(),
                timeout=5,
                accepted_exit_codes=(0,),
                expected_artifacts=(),
            ),
        ),
        controller=controller,
    )
    metadata_before = _git_metadata(repository)

    handover_candidate(baseline, receipt, controller=controller)

    assert (repository / "changed.txt").read_text(encoding="utf-8") == "after\n"
    assert _git_metadata(repository) == metadata_before


def test_handover_rolls_back_a_nonwritable_candidate_directory_after_final_check_failure(tmp_path, monkeypatch):
    """A nonwritable candidate directory must never prevent exact caller restoration."""
    repository = _repository(tmp_path)
    baseline = capture_repository_baseline(repository)
    seat = create_seat_workspace(baseline, tmp_path / "run", "claude")
    (seat.root / "changed.txt").unlink()
    (seat.root / "changed.txt").mkdir()
    (seat.root / "changed.txt" / "value.txt").write_text("candidate\n", encoding="utf-8")
    (seat.root / "changed.txt").chmod(0o500)
    candidate = create_candidate(baseline, seat.root)
    controller = _controller(tmp_path)
    receipt = verify_candidate(baseline, candidate, (_passing_check(),), controller=controller)
    before = _tree(repository)
    lifecycle = sys.modules[f"{fanout.__name__}.lifecycle"]
    original = lifecycle._run_checks
    calls = 0

    def fail_only_final(workspace, checks, environment):
        nonlocal calls
        calls += 1
        outcomes, failure = original(workspace, checks, environment)
        if calls == 2:
            return outcomes, "injected final failure after read-only directory materialization"
        return outcomes, failure

    monkeypatch.setattr(lifecycle, "_run_checks", fail_only_final)

    with pytest.raises(HandoverError):
        handover_candidate(baseline, receipt, controller=controller)

    assert _tree(repository) == before
    assert (repository / "changed.txt").read_text(encoding="utf-8") == "before\n"


def test_handover_rollback_restores_a_candidate_directory_mode_without_removing_a_late_child(tmp_path, monkeypatch):
    """An unrelated late child must not prevent rollback of the transaction's own mode delta."""
    repository = _repository(tmp_path)
    private = repository / "private"
    private.mkdir()
    (private / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _git(repository, "add", "private/tracked.txt")
    _git(repository, "commit", "-qm", "private directory")
    private.chmod(0o755)
    baseline = capture_repository_baseline(repository)
    seat = create_seat_workspace(baseline, tmp_path / "run", "claude")
    (seat.root / "private").chmod(0o700)
    candidate = create_candidate(baseline, seat.root)
    controller = _controller(tmp_path)
    receipt = verify_candidate(baseline, candidate, (_passing_check(),), controller=controller)
    lifecycle = sys.modules[f"{fanout.__name__}.lifecycle"]
    original = lifecycle._HandoverJournal.record
    injected = False

    def record_mode_then_add_late_child(self, step, path=None, **kwargs):
        nonlocal injected
        result = original(self, step, path, **kwargs)
        if step == "destination-directory-mode" and path == "private" and not injected:
            injected = True
            (repository / "private" / "late-user.txt").write_text("preserve me\n", encoding="utf-8")
        return result

    monkeypatch.setattr(lifecycle._HandoverJournal, "record", record_mode_then_add_late_child)

    with pytest.raises(HandoverError, match="destination"):
        handover_candidate(baseline, receipt, controller=controller)

    assert stat.S_IMODE(private.stat().st_mode) == 0o755
    assert (private / "late-user.txt").read_text(encoding="utf-8") == "preserve me\n"


def test_recovery_restores_a_durable_incomplete_handover_deterministically(tmp_path):
    """A crash after a write-ahead backup must have one authenticated rollback outcome."""
    repository = _repository(tmp_path)
    baseline = capture_repository_baseline(repository)
    controller = _controller(tmp_path)
    lifecycle = sys.modules[f"{fanout.__name__}.lifecycle"]
    transaction = controller.root / "transactions" / "recovery-test"
    transaction.mkdir()
    before = lifecycle._snapshot_tree(repository)
    backup = transaction / "backup"
    lifecycle._make_private_directory(backup)
    lifecycle._restore_tree(backup, before)
    digest = lifecycle._tree_digest(before)
    device, inode = lifecycle._directory_identity(repository)
    journal = lifecycle._HandoverJournal(transaction / "handover.jsonl")
    journal.record(
        "transaction-opened",
        details={
            "baseline_digest": baseline.digest,
            "candidate_digest": "0" * 64,
            "controller_id": controller.controller_id,
            "destination": os.fspath(repository),
            "destination_device": device,
            "destination_inode": inode,
        },
    )
    journal.record("backup-created", details={"tree_digest": digest})
    journal.record(
        "destination-write-intent",
        "changed.txt",
        details={
            "expected": {
                "kind": "file",
                "mode": 0o644,
                "data_sha256": hashlib.sha256(b"crash-window write\n").hexdigest(),
            },
        },
    )
    journal.record("destination-write", "changed.txt")
    journal.close()
    (repository / "changed.txt").write_text("crash-window write\n", encoding="utf-8")

    assert fanout.recover_handover(controller, transaction)
    assert lifecycle._snapshot_tree(repository) == before
    steps = [json.loads(line)["step"] for line in (transaction / "handover.jsonl").read_text().splitlines()]
    assert steps[-1] == "rollback-complete"
    (repository / "post-recovery-user-edit.txt").write_text("must survive\n", encoding="utf-8")

    assert not fanout.recover_handover(controller, transaction)
    assert (repository / "post-recovery-user-edit.txt").read_text(encoding="utf-8") == "must survive\n"


def test_recovery_never_reports_success_when_the_destination_rebinds_after_rollback(tmp_path, monkeypatch):
    """The durable rollback boundary must also reject a replacement caller pathname."""
    repository = _repository(tmp_path)
    baseline = capture_repository_baseline(repository)
    controller = _controller(tmp_path)
    lifecycle = sys.modules[f"{fanout.__name__}.lifecycle"]
    transaction = controller.root / "transactions" / "recovery-completion-rebind"
    transaction.mkdir()
    before = lifecycle._snapshot_tree(repository)
    backup = transaction / "backup"
    lifecycle._make_private_directory(backup)
    lifecycle._restore_tree(backup, before)
    device, inode = lifecycle._directory_identity(repository)
    journal = lifecycle._HandoverJournal(transaction / "handover.jsonl")
    journal.record(
        "transaction-opened",
        details={
            "baseline_digest": baseline.digest,
            "candidate_digest": "0" * 64,
            "controller_id": controller.controller_id,
            "destination": os.fspath(repository),
            "destination_device": device,
            "destination_inode": inode,
        },
    )
    journal.record("backup-created", details={"tree_digest": lifecycle._tree_digest(before)})
    journal.record(
        "destination-write-intent",
        "changed.txt",
        details={
            "expected": {
                "kind": "file",
                "mode": 0o644,
                "data_sha256": hashlib.sha256(b"crash-window write\n").hexdigest(),
            },
        },
    )
    journal.record("destination-write", "changed.txt")
    journal.close()
    replacement = tmp_path / "recovery-replacement"
    displaced = tmp_path / "recovery-displaced-caller"
    shutil.copytree(repository, replacement, symlinks=True)
    (repository / "changed.txt").write_text("crash-window write\n", encoding="utf-8")
    original = lifecycle._HandoverJournal.record
    injected = False

    def rebind_after_rollback_completion(self, step, path=None, **kwargs):
        nonlocal injected
        result = original(self, step, path, **kwargs)
        if step == "rollback-complete" and not injected:
            injected = True
            os.rename(repository, displaced)
            os.rename(replacement, repository)
        return result

    monkeypatch.setattr(lifecycle._HandoverJournal, "record", rebind_after_rollback_completion)

    with pytest.raises(HandoverError, match="pathname"):
        fanout.recover_handover(controller, transaction)

    assert injected
    assert (repository / "changed.txt").read_text(encoding="utf-8") == "before\n"
    assert (displaced / "changed.txt").read_text(encoding="utf-8") == "before\n"


def test_recovery_reverses_only_a_journal_owned_path_and_keeps_a_late_caller_edit(tmp_path):
    """Recovery after a crash cannot clear unrelated caller state that arrived afterward."""
    repository = _repository(tmp_path)
    baseline = capture_repository_baseline(repository)
    controller = _controller(tmp_path)
    lifecycle = sys.modules[f"{fanout.__name__}.lifecycle"]
    transaction = controller.root / "transactions" / "recovery-owned-path"
    transaction.mkdir()
    before = lifecycle._snapshot_tree(repository)
    backup = transaction / "backup"
    lifecycle._make_private_directory(backup)
    lifecycle._restore_tree(backup, before)
    digest = lifecycle._tree_digest(before)
    device, inode = lifecycle._directory_identity(repository)
    journal = lifecycle._HandoverJournal(transaction / "handover.jsonl")
    journal.record(
        "transaction-opened",
        details={
            "baseline_digest": baseline.digest,
            "candidate_digest": "0" * 64,
            "controller_id": controller.controller_id,
            "destination": os.fspath(repository),
            "destination_device": device,
            "destination_inode": inode,
        },
    )
    journal.record("backup-created", details={"tree_digest": digest})
    journal.record(
        "destination-write-intent",
        "changed.txt",
        details={
            "expected": {
                "kind": "file",
                "mode": 0o644,
                "data_sha256": hashlib.sha256(b"crash-window write\n").hexdigest(),
            },
        },
    )
    journal.record("destination-write", "changed.txt")
    journal.close()
    (repository / "changed.txt").write_text("crash-window write\n", encoding="utf-8")
    (repository / "late-caller-edit.txt").write_text("must survive\n", encoding="utf-8")

    assert fanout.recover_handover(controller, transaction)

    assert (repository / "changed.txt").read_text(encoding="utf-8") == "before\n"
    assert (repository / "late-caller-edit.txt").read_text(encoding="utf-8") == "must survive\n"


def test_recovery_preserves_a_late_replacement_after_ownership_is_observed(tmp_path, monkeypatch):
    """Crash recovery must use the same atomic ownership-to-restore boundary as live rollback."""
    repository = _repository(tmp_path)
    baseline = capture_repository_baseline(repository)
    controller = _controller(tmp_path)
    lifecycle = sys.modules[f"{fanout.__name__}.lifecycle"]
    transaction = controller.root / "transactions" / "recovery-late-replacement"
    transaction.mkdir()
    before = lifecycle._snapshot_tree(repository)
    backup = transaction / "backup"
    lifecycle._make_private_directory(backup)
    lifecycle._restore_tree(backup, before)
    device, inode = lifecycle._directory_identity(repository)
    journal = lifecycle._HandoverJournal(transaction / "handover.jsonl")
    journal.record(
        "transaction-opened",
        details={
            "baseline_digest": baseline.digest,
            "candidate_digest": "0" * 64,
            "controller_id": controller.controller_id,
            "destination": os.fspath(repository),
            "destination_device": device,
            "destination_inode": inode,
        },
    )
    journal.record("backup-created", details={"tree_digest": lifecycle._tree_digest(before)})
    journal.record(
        "destination-write-intent",
        "changed.txt",
        details={
            "expected": {
                "kind": "file",
                "mode": 0o644,
                "data_sha256": hashlib.sha256(b"crash-window write\n").hexdigest(),
            },
        },
    )
    journal.close()
    (repository / "changed.txt").write_text("crash-window write\n", encoding="utf-8")
    original_restore = lifecycle._restore_owned_path
    injected = False

    def replace_after_observation(destination, path, original_entry, expected_outputs, *, mutation, quarantine):
        nonlocal injected
        if path == "changed.txt" and not injected:
            injected = True
            (repository / "changed.txt").write_text("late recovery replacement\n", encoding="utf-8")
        return original_restore(
            destination,
            path,
            original_entry,
            expected_outputs,
            mutation=mutation,
            quarantine=quarantine,
        )

    monkeypatch.setattr(lifecycle, "_restore_owned_path", replace_after_observation)

    with pytest.raises(HandoverError, match="unresolved mutation"):
        fanout.recover_handover(controller, transaction)
    assert injected
    assert (repository / "changed.txt").read_text(encoding="utf-8") == "late recovery replacement\n"
    steps = [json.loads(line)["step"] for line in (transaction / "handover.jsonl").read_text().splitlines()]
    assert "rollback-complete" not in steps


def test_garbage_collection_removes_only_unchanged_exactly_owned_paths(tmp_path):
    """A broad cleanup or a swapped ownership path could erase another run or an attacker replacement."""
    controller = _controller(tmp_path)
    run_root = controller.root
    owned = run_root / "owned"
    peer = run_root / "peer"
    owned.mkdir()
    peer.mkdir()
    (owned / "evidence").write_text("owned\n", encoding="utf-8")
    (peer / "evidence").write_text("peer\n", encoding="utf-8")
    claim = claim_run_path(controller, "owned")

    removed = garbage_collect(controller, (claim,))

    assert removed == (owned,)
    assert not owned.exists()
    assert (peer / "evidence").read_text(encoding="utf-8") == "peer\n"

    stale = run_root / "stale"
    stale.mkdir()
    stale_claim = claim_run_path(controller, "stale")
    stale.rmdir()
    stale.mkdir()
    with pytest.raises(GarbageCollectionError, match="ownership"):
        garbage_collect(controller, (stale_claim,))
    with pytest.raises(GarbageCollectionError, match="path"):
        claim_run_path(controller, "../outside")
    with pytest.raises(GarbageCollectionError, match="path"):
        claim_run_path(controller, ".")

    claimed_directory = run_root / "claimed-directory"
    claimed_directory.mkdir()
    (claimed_directory / "owned.txt").write_text("owned\n", encoding="utf-8")
    directory_claim = claim_run_path(controller, "claimed-directory")
    late_peer = claimed_directory / "late-peer.txt"
    late_peer.write_text("must survive\n", encoding="utf-8")

    with pytest.raises(GarbageCollectionError, match="ownership"):
        garbage_collect(controller, (directory_claim,))

    assert late_peer.read_text(encoding="utf-8") == "must survive\n"


def test_gc_refuses_a_self_asserted_unrelated_root_and_requires_a_controller_record(tmp_path):
    """An API caller must not mint deletion authority for an arbitrary local path."""
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    protected = unrelated / "keep.txt"
    protected.write_text("keep\n", encoding="utf-8")

    with pytest.raises(GarbageCollectionError, match="controller"):
        claim_run_path(unrelated, "keep.txt")

    assert protected.read_text(encoding="utf-8") == "keep\n"
    controller = _controller(tmp_path)
    owned = controller.root / "owned.txt"
    owned.write_text("owned\n", encoding="utf-8")
    claim = claim_run_path(controller, "owned.txt")

    assert garbage_collect(controller, (claim,)) == (owned,)
    assert not owned.exists()


def test_gc_refuses_a_rebound_claim_without_its_controller_issued_record(tmp_path):
    """Observable inode fields cannot be repackaged as deletion authority for another path."""
    controller = _controller(tmp_path)
    owned = controller.root / "owned.txt"
    peer = controller.root / "peer.txt"
    owned.write_text("owned\n", encoding="utf-8")
    peer.write_text("peer\n", encoding="utf-8")
    claim = claim_run_path(controller, "owned.txt")

    with pytest.raises(GarbageCollectionError, match="evidence"):
        garbage_collect(controller, (replace(claim, path="peer.txt"),))

    assert peer.read_text(encoding="utf-8") == "peer\n"


def test_gc_quarantines_before_identity_verification_so_a_name_swap_never_deletes_a_peer(tmp_path, monkeypatch):
    """Cleanup must bind the inode atomically before any final unlink can observe a swapped name."""
    controller = _controller(tmp_path)
    owned = controller.root / "owned.txt"
    peer = controller.root / "peer.txt"
    parked = controller.root / "parked.txt"
    owned.write_text("owned\n", encoding="utf-8")
    peer.write_text("peer\n", encoding="utf-8")
    claim = claim_run_path(controller, "owned.txt")
    lifecycle = sys.modules[f"{fanout.__name__}.lifecycle"]
    original = lifecycle._rename_no_replace
    injected = False

    def swap_before_quarantine(source_parent_fd, source, destination_parent_fd, target):
        nonlocal injected
        if source == "owned.txt" and not injected:
            injected = True
            os.replace(owned, parked)
            os.replace(peer, owned)
            os.replace(parked, peer)
        return original(source_parent_fd, source, destination_parent_fd, target)

    monkeypatch.setattr(lifecycle, "_rename_no_replace", swap_before_quarantine)

    with pytest.raises(GarbageCollectionError):
        garbage_collect(controller, (claim,))

    assert injected
    assert owned.read_text(encoding="utf-8") == "peer\n"
    assert peer.read_text(encoding="utf-8") == "owned\n"


def test_gc_quarantines_each_claimed_descendant_before_deletion(tmp_path, monkeypatch):
    """A peer swapped under an already quarantined directory still cannot be unlinked."""
    controller = _controller(tmp_path)
    owned = controller.root / "owned"
    owned.mkdir()
    (owned / "owned.txt").write_text("owned\n", encoding="utf-8")
    peer = controller.root / "peer.txt"
    peer.write_text("peer\n", encoding="utf-8")
    claim = claim_run_path(controller, "owned")
    lifecycle = sys.modules[f"{fanout.__name__}.lifecycle"]
    original = lifecycle._rename_no_replace
    root_fd = os.open(controller.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    injected = False

    def swap_descendant_before_quarantine(source_parent_fd, source, destination_parent_fd, target):
        nonlocal injected
        if source == "owned.txt" and not injected:
            injected = True
            directory_fd = source_parent_fd
            os.rename("owned.txt", "parked.txt", src_dir_fd=directory_fd, dst_dir_fd=root_fd)
            os.rename("peer.txt", "owned.txt", src_dir_fd=root_fd, dst_dir_fd=directory_fd)
            os.rename("parked.txt", "peer.txt", src_dir_fd=root_fd, dst_dir_fd=root_fd)
        return original(source_parent_fd, source, destination_parent_fd, target)

    monkeypatch.setattr(lifecycle, "_rename_no_replace", swap_descendant_before_quarantine)
    try:
        with pytest.raises(GarbageCollectionError):
            garbage_collect(controller, (claim,))
    finally:
        os.close(root_fd)

    assert injected
    assert peer.read_text(encoding="utf-8") == "owned\n"


def test_gc_cleanup_failure_never_overwrites_a_peer_created_after_quarantine(tmp_path, monkeypatch):
    """A failed cleanup may retain its exact evidence privately but must not reclaim a peer spelling."""
    controller = _controller(tmp_path)
    owned = controller.root / "owned.txt"
    owned.write_text("owned bytes\n", encoding="utf-8")
    claim = claim_run_path(controller, "owned.txt")
    lifecycle = sys.modules[f"{fanout.__name__}.lifecycle"]
    original = lifecycle._remove_owned_claim
    injected = False

    def fail_after_root_quarantine(parent_fd, name, information, entries, prefix=(), *, quarantine_fd=None):
        nonlocal injected
        if not injected and quarantine_fd is not None and name != "owned.txt":
            injected = True
            owned.write_text("late peer bytes\n", encoding="utf-8")
            raise GarbageCollectionError("injected cleanup failure")
        return original(parent_fd, name, information, entries, prefix, quarantine_fd=quarantine_fd)

    monkeypatch.setattr(lifecycle, "_remove_owned_claim", fail_after_root_quarantine)

    with pytest.raises(GarbageCollectionError, match="injected cleanup failure") as raised:
        garbage_collect(controller, (claim,))

    assert injected
    assert "exact evidence remains quarantined" in str(raised.value)
    assert owned.read_text(encoding="utf-8") == "late peer bytes\n"
    assert any((controller.root / "quarantine").iterdir())


def test_incomplete_delete_intent_preserves_an_external_live_deletion(tmp_path, monkeypatch):
    """An intent alone cannot authorize rollback to recreate a path another actor deleted."""
    repository = _repository(tmp_path)
    baseline, receipt, controller = _receipt(tmp_path, repository)
    verification = sys.modules[f"{fanout.__name__}.verification"]
    original = verification._mutation_guard
    injected = False

    def delete_after_guard(guard, action, path):
        inner = original(guard, action, path)
        if inner is None or action != "delete" or path != "remove.txt":
            return inner

        def guarded():
            nonlocal injected
            inner()
            if not injected:
                injected = True
                (repository / "remove.txt").unlink()

        return guarded

    monkeypatch.setattr(verification, "_mutation_guard", delete_after_guard)

    with pytest.raises(HandoverError):
        handover_candidate(baseline, receipt, controller=controller)

    assert injected
    assert not (repository / "remove.txt").exists()


def test_recovery_incomplete_delete_intent_preserves_an_external_deletion(tmp_path):
    """Recovery cannot infer that an absence following an uncompleted delete intent is its own."""
    repository = _repository(tmp_path)
    baseline = capture_repository_baseline(repository)
    controller = _controller(tmp_path)
    _lifecycle, transaction, _before = _incomplete_recovery_transaction(
        tmp_path,
        repository,
        baseline,
        controller,
        action="delete",
        path="remove.txt",
        expected={"absent": True},
    )
    (repository / "remove.txt").unlink()

    try:
        fanout.recover_handover(controller, transaction)
    except HandoverError:
        pass

    assert not (repository / "remove.txt").exists()
    steps = [json.loads(line)["step"] for line in (transaction / "handover.jsonl").read_text().splitlines()]
    assert "rollback-complete" not in steps
    assert steps[-1] == "rollback-conflict"


def test_recovery_restores_the_exact_quarantined_source_after_a_pre_record_crash(tmp_path, monkeypatch):
    """A crash before source-removal evidence must leave the moved source recoverable by inode."""
    repository = _repository(tmp_path)
    baseline, receipt, controller = _receipt(tmp_path, repository)
    lifecycle = sys.modules[f"{fanout.__name__}.lifecycle"]
    original_record = lifecycle._HandoverJournal.record
    source_inode = (repository / "remove.txt").stat().st_ino
    removal_reached = False

    def crash_before_source_removal_record(self, step, path=None, **kwargs):
        nonlocal removal_reached
        if step == "destination-source-removed" and path == "remove.txt":
            removal_reached = True
            raise OSError("injected crash before source-removal record")
        return original_record(self, step, path, **kwargs)

    def strand_transaction(*args, **kwargs):
        raise HandoverError("injected process exit before live rollback")

    with monkeypatch.context() as crash:
        crash.setattr(lifecycle._HandoverJournal, "record", crash_before_source_removal_record)
        crash.setattr(lifecycle, "_rollback_from_backup", strand_transaction)
        with pytest.raises(HandoverError):
            handover_candidate(baseline, receipt, controller=controller)

    transaction = next((controller.root / "transactions").iterdir())
    assert removal_reached
    assert not (repository / "remove.txt").exists()
    assert fanout.recover_handover(controller, transaction)
    assert (repository / "remove.txt").read_bytes() == b"remove\n"
    assert (repository / "remove.txt").stat().st_ino == source_inode
    steps = [json.loads(line)["step"] for line in (transaction / "handover.jsonl").read_text().splitlines()]
    assert steps[-1] == "rollback-complete"


def test_partial_live_write_cannot_be_terminalized_as_a_completed_rollback(tmp_path, monkeypatch):
    """A partial candidate write must be restored or leave recovery nonterminal."""
    repository = _repository(tmp_path)
    baseline, receipt, controller = _receipt(tmp_path, repository)
    verification = sys.modules[f"{fanout.__name__}.verification"]
    original = verification._write_all
    candidate_writes = 0

    def fail_after_partial_caller_write(fd, data):
        nonlocal candidate_writes
        if data == b"after\n":
            candidate_writes += 1
            if candidate_writes == 2:
                os.write(fd, b"afte")
                os.fsync(fd)
                raise OSError("injected partial caller write")
        return original(fd, data)

    monkeypatch.setattr(verification, "_write_all", fail_after_partial_caller_write)

    with pytest.raises(HandoverError):
        handover_candidate(baseline, receipt, controller=controller)

    transaction = next((controller.root / "transactions").iterdir())
    steps = [json.loads(line)["step"] for line in (transaction / "handover.jsonl").read_text().splitlines()]
    actual = (repository / "changed.txt").read_bytes()
    assert candidate_writes == 2
    assert actual in {b"before\n", b"afte"}
    assert actual == b"before\n" or "rollback-complete" not in steps


def test_partial_recovered_write_cannot_be_terminalized_as_a_completed_rollback(tmp_path):
    """Recovery must not claim restoration while an unproven partial write remains."""
    repository = _repository(tmp_path)
    baseline = capture_repository_baseline(repository)
    controller = _controller(tmp_path)
    _lifecycle, transaction, _before = _incomplete_recovery_transaction(
        tmp_path,
        repository,
        baseline,
        controller,
        action="write",
        path="changed.txt",
        expected={
            "kind": "file",
            "mode": 0o644,
            "data_sha256": hashlib.sha256(b"after\n").hexdigest(),
        },
    )
    (repository / "changed.txt").write_bytes(b"afte")

    try:
        fanout.recover_handover(controller, transaction)
    except HandoverError:
        pass

    steps = [json.loads(line)["step"] for line in (transaction / "handover.jsonl").read_text().splitlines()]
    actual = (repository / "changed.txt").read_bytes()
    assert actual in {b"before\n", b"afte"}
    assert actual == b"before\n" or "rollback-complete" not in steps


def test_recovery_incomplete_write_intent_preserves_an_exact_external_replacement(tmp_path):
    """Exact post bytes do not prove ownership when no durable operation effect was recorded."""
    repository = _repository(tmp_path)
    baseline = capture_repository_baseline(repository)
    controller = _controller(tmp_path)
    _lifecycle, transaction, _before = _incomplete_recovery_transaction(
        tmp_path,
        repository,
        baseline,
        controller,
        action="write",
        path="changed.txt",
        expected={
            "kind": "file",
            "mode": 0o644,
            "data_sha256": hashlib.sha256(b"after\n").hexdigest(),
        },
    )
    (repository / "changed.txt").write_bytes(b"after\n")

    with pytest.raises(HandoverError, match="unresolved mutation"):
        fanout.recover_handover(controller, transaction)

    assert (repository / "changed.txt").read_bytes() == b"after\n"
    steps = [json.loads(line)["step"] for line in (transaction / "handover.jsonl").read_text().splitlines()]
    assert "rollback-complete" not in steps


def test_rollback_rechecks_created_file_bytes_after_atomic_detach(tmp_path, monkeypatch):
    """Created-inode evidence cannot erase an in-place edit made at the rollback detach boundary."""
    repository = _repository(tmp_path)
    baseline, receipt, controller = _receipt(tmp_path, repository)
    lifecycle = sys.modules[f"{fanout.__name__}.lifecycle"]
    original_checks = lifecycle._run_checks
    original_detach = lifecycle._TransactionQuarantine.detach_identity
    check_calls = 0
    injected = False

    def fail_only_final(workspace, checks, environment):
        nonlocal check_calls
        check_calls += 1
        outcomes, failure = original_checks(workspace, checks, environment)
        if check_calls == 2:
            return outcomes, "injected final verification failure"
        return outcomes, failure

    def edit_at_detach(self, parent_fd, name, identity, expected, path):
        nonlocal injected
        if path == "changed.txt" and not injected:
            injected = True
            (repository / "changed.txt").write_text("late detach-boundary edit\n", encoding="utf-8")
        return original_detach(self, parent_fd, name, identity, expected, path)

    monkeypatch.setattr(lifecycle, "_run_checks", fail_only_final)
    monkeypatch.setattr(lifecycle._TransactionQuarantine, "detach_identity", edit_at_detach)

    with pytest.raises(HandoverError, match="final"):
        handover_candidate(baseline, receipt, controller=controller)

    assert injected
    assert (repository / "changed.txt").read_text(encoding="utf-8") == "late detach-boundary edit\n"


def test_exact_peer_replacement_leaves_an_explicit_nonterminal_rollback_conflict(tmp_path, monkeypatch):
    """Preserving an exact-byte peer cannot terminalize rollback before the baseline is restored."""
    repository = _repository(tmp_path)
    baseline, receipt, controller = _receipt(tmp_path, repository)
    lifecycle = sys.modules[f"{fanout.__name__}.lifecycle"]
    original_checks = lifecycle._run_checks
    original_detach = lifecycle._TransactionQuarantine.detach_identity
    check_calls = 0
    injected = False

    def fail_only_final(workspace, checks, environment):
        nonlocal check_calls
        check_calls += 1
        outcomes, failure = original_checks(workspace, checks, environment)
        if check_calls == 2:
            return outcomes, "injected final verification failure"
        return outcomes, failure

    def replace_with_exact_peer(self, parent_fd, name, identity, expected, path):
        nonlocal injected
        if path == "changed.txt" and not injected:
            injected = True
            peer = repository / "exact-peer.tmp"
            peer.write_bytes(b"after\n")
            peer.chmod(0o644)
            os.replace(peer, repository / "changed.txt")
        return original_detach(self, parent_fd, name, identity, expected, path)

    monkeypatch.setattr(lifecycle, "_run_checks", fail_only_final)
    monkeypatch.setattr(lifecycle._TransactionQuarantine, "detach_identity", replace_with_exact_peer)

    with pytest.raises(HandoverError):
        handover_candidate(baseline, receipt, controller=controller)

    assert injected
    assert (repository / "changed.txt").read_bytes() == b"after\n"
    transaction = next((controller.root / "transactions").iterdir())
    steps = [json.loads(line)["step"] for line in (transaction / "handover.jsonl").read_text().splitlines()]
    assert "rollback-complete" not in steps
    assert steps[-1] == "rollback-conflict"


def test_directory_mode_update_reverts_an_inode_moved_from_its_bound_name(tmp_path, monkeypatch):
    """A mode update cannot follow a verified directory fd after its name is rebound."""
    repository = _repository(tmp_path)
    private = repository / "private"
    private.mkdir()
    (private / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _git(repository, "add", "private/tracked.txt")
    _git(repository, "commit", "-qm", "private directory")
    private.chmod(0o755)
    baseline = capture_repository_baseline(repository)
    seat = create_seat_workspace(baseline, tmp_path / "run", "claude")
    (seat.root / "private").chmod(0o700)
    candidate = create_candidate(baseline, seat.root)
    controller = _controller(tmp_path)
    receipt = verify_candidate(baseline, candidate, (_passing_check(),), controller=controller)
    verification = sys.modules[f"{fanout.__name__}.verification"]
    original = verification.os.fchmod
    original_inode = private.stat().st_ino
    displaced = tmp_path / "externally-moved-private"
    injected = False

    def rebind_before_mode_change(fd, mode):
        nonlocal injected
        if (
            os.fstat(fd).st_ino == original_inode
            and not injected
            and private.exists()
            and private.stat().st_ino == original_inode
        ):
            injected = True
            os.rename(private, displaced)
            shutil.copytree(displaced, private, symlinks=True)
            private.chmod(0o700)
        return original(fd, mode)

    monkeypatch.setattr(verification.os, "fchmod", rebind_before_mode_change)

    try:
        handover_candidate(baseline, receipt, controller=controller)
    except HandoverError:
        pass

    if injected:
        assert stat.S_IMODE(displaced.stat().st_mode) == 0o755
    else:
        assert not displaced.exists()
    assert stat.S_IMODE(private.stat().st_mode) == 0o700


def test_directory_mode_update_never_mutates_an_inode_rebound_after_its_identity_check(tmp_path, monkeypatch):
    """The named directory must be quarantined before any mode mutation can escape a later rebind."""
    repository = _repository(tmp_path)
    private = repository / "private"
    private.mkdir()
    (private / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _git(repository, "add", "private/tracked.txt")
    _git(repository, "commit", "-qm", "private directory")
    private.chmod(0o755)
    baseline = capture_repository_baseline(repository)
    seat = create_seat_workspace(baseline, tmp_path / "run", "claude")
    (seat.root / "private").chmod(0o711)
    candidate = create_candidate(baseline, seat.root)
    controller = _controller(tmp_path)
    receipt = verify_candidate(baseline, candidate, (_passing_check(),), controller=controller)
    verification = sys.modules[f"{fanout.__name__}.verification"]
    original_stat = verification.os.stat
    original_inode = private.stat().st_ino
    repository_inode = repository.stat().st_ino
    displaced = tmp_path / "post-check-displaced-private"
    injected = False

    def rebind_after_successful_mode_identity_check(name, *args, **kwargs):
        nonlocal injected
        information = original_stat(name, *args, **kwargs)
        parent_fd = kwargs.get("dir_fd")
        frame = sys._getframe(1)
        inside_bound_mode_helper = False
        while frame is not None:
            if frame.f_code.co_name == "_set_bound_directory_mode":
                inside_bound_mode_helper = True
                break
            frame = frame.f_back
        if (
            not injected
            and inside_bound_mode_helper
            and name == "private"
            and parent_fd is not None
            and os.fstat(parent_fd).st_ino == repository_inode
            and information.st_ino == original_inode
            and stat.S_IMODE(information.st_mode) == 0o700
        ):
            injected = True
            os.rename(private, displaced)
            shutil.copytree(displaced, private, symlinks=True)
            private.chmod(0o700)
        return information

    monkeypatch.setattr(verification.os, "stat", rebind_after_successful_mode_identity_check)

    try:
        handover_candidate(baseline, receipt, controller=controller)
    except HandoverError:
        pass

    if injected:
        assert stat.S_IMODE(displaced.stat().st_mode) == 0o755
        assert stat.S_IMODE(private.stat().st_mode) == 0o700
    else:
        assert not displaced.exists()
        assert stat.S_IMODE(private.stat().st_mode) == 0o711


def test_gc_directory_restore_never_reopens_a_peer_replacement_for_mode(tmp_path, monkeypatch):
    """GC restoration must preserve mode through its quarantined inode, not a rebound name."""
    controller = _controller(tmp_path)
    owned = controller.root / "owned"
    owned.mkdir(mode=0o700)
    (owned / "marker").write_text("owned\n", encoding="utf-8")
    peer = controller.root / "peer"
    peer.mkdir(mode=0o755)
    (peer / "marker").write_text("peer\n", encoding="utf-8")
    claim = claim_run_path(controller, "owned")
    lifecycle = sys.modules[f"{fanout.__name__}.lifecycle"]
    original_remove = lifecycle._remove_owned_claim
    original_rename = lifecycle._rename_no_replace
    parked = controller.root / "parked"
    cleanup_failed = False
    replacement_installed = False

    def fail_after_quarantine(parent_fd, name, information, entries, prefix=(), *, quarantine_fd=None):
        nonlocal cleanup_failed
        if not cleanup_failed and quarantine_fd is not None:
            cleanup_failed = True
            raise GarbageCollectionError("injected directory cleanup failure")
        return original_remove(parent_fd, name, information, entries, prefix, quarantine_fd=quarantine_fd)

    def replace_after_no_replace_restore(source_parent_fd, source, destination_parent_fd, target):
        nonlocal replacement_installed
        result = original_rename(source_parent_fd, source, destination_parent_fd, target)
        if result and cleanup_failed and target == "owned" and not replacement_installed:
            replacement_installed = True
            os.rename("owned", "parked", src_dir_fd=destination_parent_fd, dst_dir_fd=destination_parent_fd)
            os.rename("peer", "owned", src_dir_fd=destination_parent_fd, dst_dir_fd=destination_parent_fd)
        return result

    monkeypatch.setattr(lifecycle, "_remove_owned_claim", fail_after_quarantine)
    monkeypatch.setattr(lifecycle, "_rename_no_replace", replace_after_no_replace_restore)

    with pytest.raises(GarbageCollectionError, match="injected directory cleanup failure"):
        garbage_collect(controller, (claim,))

    assert cleanup_failed and replacement_installed
    assert (owned / "marker").read_text(encoding="utf-8") == "peer\n"
    assert stat.S_IMODE(owned.stat().st_mode) == 0o755
    assert (parked / "marker").read_text(encoding="utf-8") == "owned\n"
    assert stat.S_IMODE(parked.stat().st_mode) == 0o700


def test_recovery_reverts_a_bound_directory_mode_without_an_operation_completion(tmp_path):
    """The bound inode and before-mode are durable ownership evidence across a crash."""
    (
        lifecycle,
        controller,
        transaction,
        _repository_path,
        private,
        _source_quarantine,
        journal,
    ) = _directory_mode_recovery_transaction(
        tmp_path,
        action="directory-prepare",
        target_mode=0o700,
    )
    identity = private.stat()
    journal.record(
        "destination-source-bound",
        "private",
        details={
            "action": "directory-prepare",
            "device": identity.st_dev,
            "inode": identity.st_ino,
            "kind": "directory",
        },
    )
    journal.close()
    private.chmod(0o700)

    assert fanout.recover_handover(controller, transaction)
    assert private.stat().st_ino == identity.st_ino
    assert stat.S_IMODE(private.stat().st_mode) == 0o755
    records = [
        json.loads(line)
        for line in (transaction / "handover.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert records[-1]["step"] == "rollback-complete"


def test_directory_mode_rollback_uses_its_journaled_quarantine_name_across_interruption(
    tmp_path,
    monkeypatch,
):
    """An interrupted rollback must leave its exact directory at a discoverable durable name."""
    (
        lifecycle,
        controller,
        transaction,
        _repository_path,
        private,
        source_quarantine,
        journal,
    ) = _directory_mode_recovery_transaction(
        tmp_path,
        action="directory-mode",
        target_mode=0o711,
    )
    identity = private.stat()
    journal.record(
        "destination-source-bound",
        "private",
        details={
            "action": "directory-mode",
            "device": identity.st_dev,
            "inode": identity.st_ino,
            "kind": "directory",
        },
    )
    private.chmod(0o711)
    journal.record("destination-directory-mode", "private")
    journal.close()
    def interrupt_after_detach(self, temporary, parent_fd, name, expected, mode):
        raise OSError("injected rollback interruption after directory detach")

    with monkeypatch.context() as interrupted:
        interrupted.setattr(
            lifecycle._TransactionQuarantine,
            "restore_directory_with_mode",
            interrupt_after_detach,
        )
        with pytest.raises(HandoverError, match="directory mode"):
            fanout.recover_handover(controller, transaction)

    quarantine = transaction / "quarantine"
    assert not private.exists()
    assert tuple(path.name for path in quarantine.iterdir()) == (source_quarantine,)

    assert fanout.recover_handover(controller, transaction)
    assert private.stat().st_ino == identity.st_ino
    assert stat.S_IMODE(private.stat().st_mode) == 0o755
    assert not any(quarantine.iterdir())


def test_peer_directory_never_completes_rollback_while_the_baseline_source_is_retained(tmp_path):
    """Matching only a directory root mode cannot substitute for its exact baseline subtree."""
    (
        lifecycle,
        controller,
        transaction,
        repository,
        private,
        source_quarantine,
        journal,
    ) = _directory_mode_recovery_transaction(
        tmp_path,
        action="directory-prepare",
        target_mode=0o700,
    )
    identity = private.stat()
    parent_fd = os.open(repository, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with lifecycle._TransactionQuarantine(transaction) as quarantine:
            quarantine.retain_exact(
                parent_fd,
                "private",
                identity,
                "private",
                source_quarantine,
            )
    finally:
        os.close(parent_fd)
    journal.record(
        "destination-source-bound",
        "private",
        details={
            "action": "directory-prepare",
            "device": identity.st_dev,
            "inode": identity.st_ino,
            "kind": "directory",
        },
    )
    journal.close()
    private.mkdir(mode=0o755)
    (private / "peer.txt").write_text("peer\n", encoding="utf-8")
    private.chmod(0o755)

    with pytest.raises(HandoverError, match="unresolved mutation"):
        fanout.recover_handover(controller, transaction)

    assert (private / "peer.txt").read_text(encoding="utf-8") == "peer\n"
    assert (transaction / "quarantine" / source_quarantine / "tracked.txt").read_text(
        encoding="utf-8"
    ) == "baseline\n"
    steps = [
        json.loads(line)["step"]
        for line in (transaction / "handover.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert "rollback-complete" not in steps
    assert steps[-1] == "rollback-conflict"
