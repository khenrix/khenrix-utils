"""Repository-baseline contracts for repository-writing fanout seats."""
from __future__ import annotations

import hashlib
import importlib.util
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
FANOUT_ROOT = ROOT / "shared" / "lib" / "fanout"
SPEC = importlib.util.spec_from_file_location(
    "_fanout_repo_contracts", FANOUT_ROOT / "__init__.py",
    submodule_search_locations=[str(FANOUT_ROOT)],
)
assert SPEC and SPEC.loader
fanout = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fanout
SPEC.loader.exec_module(fanout)


RepoLimits = fanout.RepoLimits
RepositoryIsolationError = fanout.RepositoryIsolationError
RepositoryQuotaError = fanout.RepositoryQuotaError
RepositorySecretError = fanout.RepositorySecretError
capture_repository_baseline = fanout.capture_repository_baseline
create_seat_workspace = fanout.create_seat_workspace


def _git_env() -> dict[str, str]:
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


def _git(repo: Path, *args: str, input: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        ("git", "-C", os.fspath(repo), *args), input=input,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_git_env(), check=False,
    )
    if result.returncode:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr.decode(errors='replace')}")
    return result


def _repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "user.email", "fixture@example.invalid")
    (repo / "tracked.txt").write_text("tracked-before\n")
    (repo / "staged.txt").write_text("staged-before\n")
    (repo / "deleted.txt").write_text("delete-me\n")
    (repo / "staged-deleted.txt").write_text("stage-delete-me\n")
    (repo / "mode.sh").write_text("#!/bin/sh\necho original\n")
    (repo / "binary.bin").write_bytes(b"\x00\x01original\xff")
    (repo / "target.txt").write_text("link target\n")
    (repo / "inside-link").symlink_to("target.txt")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "seed")
    return repo


def _target_repo(tmp_path: Path, target_id: str) -> tuple[Path, object]:
    root = _repo(tmp_path, target_id)
    _git(root, "remote", "add", "origin", f"https://github.com/example/{target_id}.git")
    return root, fanout.TargetSpec(
        target_id, f"github.com/example/{target_id}", "TASK-123",
        f"refs/heads/feat/TASK-123-{target_id}",
    )


@pytest.mark.parametrize("namespace", ("head", "index", "merge"))
@pytest.mark.parametrize("target", ("a/../outside", "b"))
def test_retained_link_namespace_cannot_borrow_safe_current_intermediates(tmp_path, namespace, target):
    repo = _repo(tmp_path)
    (repo / "dir").mkdir()
    (repo / "dir/inside").write_bytes(b"inside")
    (repo / "a").symlink_to(".")
    (repo / "b").symlink_to(target)
    _git(repo, "add", "a", "b", "dir/inside")
    if namespace == "head":
        _git(repo, "commit", "-qm", "retained links")
    elif namespace == "merge":
        a_oid = _git(repo, "hash-object", "-w", "--stdin", input=b".").stdout.strip()
        b_oid = _git(repo, "hash-object", "-w", "--stdin", input=os.fsencode(target)).stdout.strip()
        _git(repo, "update-index", "--force-remove", "a", "b")
        _git(repo, "update-index", "-z", "--index-info", input=(
            b"120000 " + a_oid + b" 1\ta\0" + b"120000 " + b_oid + b" 1\tb\0"
        ))
    (repo / "a").unlink()
    (repo / "a").symlink_to("dir")
    (repo / "b").unlink()
    (repo / "b").symlink_to("dir/inside")
    with pytest.raises(fanout.RepositoryIsolationError, match="symlink"):
        capture_repository_baseline(repo)


def test_safe_merge_link_views_replay_all_stages_and_current_bytes(tmp_path):
    repo = _repo(tmp_path)
    (repo / "dir").mkdir()
    (repo / "dir/inside").write_bytes(b"inside")
    (repo / "a").symlink_to("dir")
    (repo / "b").symlink_to("a/inside")
    _git(repo, "add", "dir/inside")
    records = []
    for stage, target in ((1, b"dir"), (2, b"."), (3, b"dir/..")):
        oid = _git(repo, "hash-object", "-w", "--stdin", input=target).stdout.strip()
        records.append(b"120000 " + oid + f" {stage}\ta\0".encode())
    b_oid = _git(repo, "hash-object", "-w", "--stdin", input=b"a/dir/inside").stdout.strip()
    records.append(b"120000 " + b_oid + b" 0\tb\0")
    _git(repo, "update-index", "-z", "--index-info", input=b"".join(records))
    baseline = capture_repository_baseline(repo)
    assert [(entry.stage, entry.data) for entry in baseline.index_entries if entry.path == "a"] == [
        (1, b"dir"), (2, b"."), (3, b"dir/.."),
    ]
    seat = create_seat_workspace(baseline, tmp_path / "seats", "merge")
    assert _git(seat.root, "ls-files", "--stage", "-z").stdout == _git(repo, "ls-files", "--stage", "-z").stdout
    assert os.readlink(seat.root / "a") == "dir"
    assert os.readlink(seat.root / "b") == "a/inside"


def test_two_equal_content_repositories_keep_distinct_target_bindings(tmp_path):
    """A shared baseline digest must not collapse different repository identities."""
    address, address_spec = _target_repo(tmp_path, "address")
    booking = tmp_path / "booking"
    _git(tmp_path, "clone", "-q", "--no-local", str(address), str(booking))
    _git(booking, "remote", "set-url", "origin", "https://github.com/example/booking.git")
    booking_spec = fanout.TargetSpec(
        "booking", "github.com/example/booking", "TASK-123",
        "refs/heads/feat/TASK-123-booking",
    )
    bindings, baselines = fanout.repo.capture_and_bind_targets(
        {"address": address_spec, "booking": booking_spec},
        {"address": address, "booking": booking},
    )
    assert bindings["address"].baseline_sha256 == bindings["booking"].baseline_sha256
    assert bindings["address"].spec.repository == "github.com/example/address"
    assert bindings["booking"].spec.repository == "github.com/example/booking"
    assert baselines["address"].digest == baselines["booking"].digest


def test_baseline_change_between_git_binding_and_capture_blocks(tmp_path, monkeypatch):
    """A checkout edit between identity resolution and capture must abort the group."""
    address, address_spec = _target_repo(tmp_path, "address")
    booking, booking_spec = _target_repo(tmp_path, "booking")
    original = fanout.repo.capture_repository_baseline

    def changed_capture(root):
        if root == booking:
            (booking / "tracked.txt").write_text("changed during capture\n")
        return original(root)

    monkeypatch.setattr(fanout.repo, "capture_repository_baseline", changed_capture)
    with pytest.raises(fanout.RepositoryValidationError, match="changed during capture"):
        fanout.repo.capture_and_bind_targets(
            {"address": address_spec, "booking": booking_spec},
            {"address": address, "booking": booking},
        )


def test_origin_change_between_capture_and_final_recheck_blocks(tmp_path, monkeypatch):
    """A captured tree cannot authorize spending against a changed origin."""
    address, address_spec = _target_repo(tmp_path, "address")
    booking, booking_spec = _target_repo(tmp_path, "booking")
    original = fanout.repo.capture_repository_baseline

    def changed_capture(root):
        baseline = original(root)
        if root == booking:
            _git(booking, "remote", "set-url", "origin", "https://github.com/example/wrong.git")
        return baseline

    monkeypatch.setattr(fanout.repo, "capture_repository_baseline", changed_capture)
    with pytest.raises(fanout.RepositoryValidationError, match="origin"):
        fanout.repo.capture_and_bind_targets(
            {"address": address_spec, "booking": booking_spec},
            {"address": address, "booking": booking},
        )


def test_saved_write_binding_rechecks_symbolic_head_before_mutation(tmp_path):
    """An existing ticket ref at the right OID is unsafe when HEAD is another branch."""
    root, spec = _target_repo(tmp_path, "address")
    _git(root, "branch", "feat/TASK-123-address")
    bindings, _ = fanout.repo.capture_and_bind_targets({"address": spec}, {"address": root})
    restored = {"address": fanout.TargetBinding.from_dict(bindings["address"].to_dict())}
    with pytest.raises(fanout.RepositoryValidationError, match="current HEAD"):
        fanout.repo.revalidate_target_bindings(restored, writable_ids={"address"})


def test_saved_write_binding_rejects_direct_head_alias_to_ticket(tmp_path):
    root, spec = _target_repo(tmp_path, "address")
    _git(root, "switch", "-q", "-c", "feat/TASK-123-address")
    bindings, _ = fanout.repo.capture_and_bind_targets({"address": spec}, {"address": root})
    restored = {"address": fanout.TargetBinding.from_dict(bindings["address"].to_dict())}
    _git(root, "symbolic-ref", "refs/heads/alias", spec.branch_ref)
    _git(root, "symbolic-ref", "HEAD", "refs/heads/alias")
    assert _git(root, "symbolic-ref", "--no-recurse", "HEAD").stdout.strip() == b"refs/heads/alias"
    assert _git(root, "rev-parse", "HEAD").stdout.strip().decode() == restored["address"].base_oid

    with pytest.raises(fanout.RepositoryValidationError, match="current HEAD"):
        fanout.repo.revalidate_target_bindings(restored, writable_ids={"address"})


def test_absent_branch_binding_rejects_same_oid_head_switch(tmp_path):
    root, spec = _target_repo(tmp_path, "address")
    bindings, _ = fanout.repo.capture_and_bind_targets({"address": spec}, {"address": root})
    restored = {"address": fanout.TargetBinding.from_dict(bindings["address"].to_dict())}
    assert restored["address"].branch_oid is None
    assert _git(root, "symbolic-ref", "--no-recurse", "HEAD").stdout.strip() == b"refs/heads/main"
    _git(root, "switch", "-q", "-c", "other")
    assert _git(root, "rev-parse", "HEAD").stdout.strip().decode() == restored["address"].base_oid

    with pytest.raises(fanout.RepositoryValidationError, match="binding|HEAD"):
        fanout.repo.revalidate_target_bindings(restored, writable_ids={"address"})


def _non_git_tree(root: Path) -> dict[str, tuple[str, int, bytes]]:
    result: dict[str, tuple[str, int, bytes]] = {}
    for path in sorted(root.rglob("*")):
        if ".git" in path.parts:
            continue
        relative = path.relative_to(root).as_posix()
        mode = stat.S_IMODE(path.lstat().st_mode)
        if path.is_symlink():
            result[relative] = ("symlink", mode, os.fsencode(os.readlink(path)))
        elif path.is_file():
            result[relative] = ("file", mode, path.read_bytes())
    return result


def _caller_state(repo: Path) -> tuple[dict[str, tuple[str, int, bytes]], bytes, bytes, bytes, bytes]:
    git_dir = Path(_git(repo, "rev-parse", "--absolute-git-dir").stdout.decode().strip())
    return (
        _non_git_tree(repo),
        (git_dir / "index").read_bytes(),
        _git(repo, "show-ref", "--head", "--dereference").stdout,
        (git_dir / "config").read_bytes(),
        _git(repo, "worktree", "list", "--porcelain").stdout,
    )


def test_baseline_replays_tracked_index_and_worktree_state_into_each_distinct_seat(tmp_path):
    """Dropping any worktree/index state would make a repository-writing seat start elsewhere."""
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("unstaged bytes\n")
    (repo / "staged.txt").write_text("staged bytes\n")
    _git(repo, "add", "staged.txt")
    (repo / "deleted.txt").unlink()
    (repo / "staged-deleted.txt").unlink()
    _git(repo, "add", "-u", "staged-deleted.txt")
    (repo / "binary.bin").write_bytes(b"\x00\x01changed\xff")
    (repo / "mode.sh").chmod(0o755)
    (repo / "untracked.bin").write_bytes(b"\x00untracked\xfe")
    (repo / "ignored.tmp").write_text("ignored\n")
    (repo / ".gitignore").write_text("ignored.tmp\n")

    expected_status = _git(repo, "status", "--porcelain=v1", "-z").stdout
    expected_tree = _non_git_tree(repo)
    expected_tree.pop("ignored.tmp")
    baseline = capture_repository_baseline(repo)
    (repo / "tracked.txt").write_text("later caller bytes must not enter a captured baseline\n")
    _git(repo, "add", "tracked.txt")
    first = create_seat_workspace(baseline, tmp_path / "run", "claude")
    second = create_seat_workspace(baseline, tmp_path / "run", "codex")

    assert first.root != second.root
    assert first.root.is_dir() and second.root.is_dir()
    assert _non_git_tree(first.root) == expected_tree
    assert _non_git_tree(second.root) == expected_tree
    assert _git(first.root, "status", "--porcelain=v1", "-z").stdout == expected_status
    assert _git(second.root, "status", "--porcelain=v1", "-z").stdout == expected_status
    assert "deleted.txt" in baseline.deleted_paths
    assert "staged-deleted.txt" in baseline.deleted_paths
    assert "ignored.tmp" not in {entry.path for entry in baseline.entries}
    assert (first.root / "inside-link").is_symlink()
    assert stat.S_IMODE((first.root / "mode.sh").lstat().st_mode) == 0o755


@pytest.mark.parametrize(
    "index_flag",
    ("intent-to-add", "intent-to-add-special", "skip-worktree", "assume-unchanged", "skip-and-assume"),
)
def test_seat_replays_index_flags_that_change_dirty_worktree_status(tmp_path, index_flag):
    """Dropping any of these flags changes what the provider sees as staged or dirty."""
    repo = _repo(tmp_path)
    if index_flag.startswith("intent-to-add"):
        name = "intent:\nodd.txt" if index_flag == "intent-to-add-special" else "intent.txt"
        (repo / name).write_text("not staged\n")
        _git(repo, "add", "-N", "--", name)
    else:
        if index_flag in {"skip-worktree", "skip-and-assume"}:
            _git(repo, "update-index", "--skip-worktree", "tracked.txt")
        if index_flag in {"assume-unchanged", "skip-and-assume"}:
            _git(repo, "update-index", "--assume-unchanged", "tracked.txt")
        (repo / "tracked.txt").write_text("dirty but intentionally hidden\n")
    expected_status = _git(repo, "status", "--porcelain=v1", "-z").stdout
    expected_tags = _git(repo, "ls-files", "-v", "-z").stdout
    caller_before = _caller_state(repo)

    baseline = capture_repository_baseline(repo)
    seat = create_seat_workspace(baseline, tmp_path / "run", "claude")

    assert _git(seat.root, "status", "--porcelain=v1", "-z").stdout == expected_status
    assert _git(seat.root, "ls-files", "-v", "-z").stdout == expected_tags
    assert _caller_state(repo) == caller_before


@pytest.mark.parametrize("executable", (False, True))
def test_deleted_intent_to_add_replays_as_a_missing_worktree_path(tmp_path, executable):
    repo = _repo(tmp_path)
    path = repo / "intent-deleted.txt"
    path.write_text("never staged\n")
    if executable:
        path.chmod(0o755)
    _git(repo, "add", "-N", "--", path.name)
    path.unlink()
    assert _git(repo, "status", "--porcelain=v1", "-z").stdout == b" D intent-deleted.txt\0"
    caller_before = _caller_state(repo)

    baseline = capture_repository_baseline(repo)
    assert any(
        entry.path == path.name and entry.mode == (0o100755 if executable else 0o100644)
        for entry in baseline.index_entries
    )
    controller = fanout.create_lifecycle_controller(tmp_path / "controller")
    seat = create_seat_workspace(baseline, controller.root / "workspaces", "claude")
    fanout.verify_seat_workspace(baseline, seat, controller=controller)

    assert not (seat.root / path.name).exists()
    assert _git(seat.root, "status", "--porcelain=v1", "-z").stdout == b" D intent-deleted.txt\0"
    assert _caller_state(repo) == caller_before


def test_deleted_intent_replay_restores_read_only_directory_and_present_intent(tmp_path):
    repo = _repo(tmp_path)
    private = repo / "private"
    private.mkdir()
    deleted_name = "gone:\nodd.txt"
    (private / deleted_name).write_text("never staged\n")
    (repo / "present.txt").write_text("still here\n")
    _git(repo, "add", "-N", "--", f"private/{deleted_name}", "present.txt")
    (private / deleted_name).unlink()
    private.chmod(0o500)
    expected_status = _git(repo, "status", "--porcelain=v1", "-z").stdout
    assert b" D private/gone:\nodd.txt\0" in expected_status
    assert b" A present.txt\0" in expected_status
    caller_before = _caller_state(repo)

    seat = None
    try:
        baseline = capture_repository_baseline(repo)
        controller = fanout.create_lifecycle_controller(tmp_path / "controller")
        seat = create_seat_workspace(baseline, controller.root / "workspaces", "claude")
        fanout.verify_seat_workspace(baseline, seat, controller=controller)

        assert not (seat.root / "private" / deleted_name).exists()
        assert stat.S_IMODE((seat.root / "private").stat().st_mode) == 0o500
        assert _git(seat.root, "status", "--porcelain=v1", "-z").stdout == expected_status
        assert _caller_state(repo) == caller_before
    finally:
        private.chmod(0o700)
        if seat is not None:
            (seat.root / "private").chmod(0o700)


def test_deleted_intent_replay_removes_temporary_parent_directories(tmp_path):
    repo = _repo(tmp_path)
    missing = repo / "gone-dir" / "nested" / "intent.txt"
    missing.parent.mkdir(parents=True)
    missing.write_text("never staged\n")
    _git(repo, "add", "-N", "--", "gone-dir/nested/intent.txt")
    missing.unlink()
    missing.parent.rmdir()
    missing.parent.parent.rmdir()
    caller_before = _caller_state(repo)

    baseline = capture_repository_baseline(repo)
    controller = fanout.create_lifecycle_controller(tmp_path / "controller")
    seat = create_seat_workspace(baseline, controller.root / "workspaces", "claude")
    fanout.verify_seat_workspace(baseline, seat, controller=controller)

    assert not (seat.root / "gone-dir").exists()
    assert _git(seat.root, "status", "--porcelain=v1", "-z").stdout == b" D gone-dir/nested/intent.txt\0"
    assert _caller_state(repo) == caller_before


def test_deleted_symlink_intent_fails_closed_before_workspace_handover(tmp_path):
    repo = _repo(tmp_path)
    link = repo / "intent-link"
    link.symlink_to("target.txt")
    _git(repo, "add", "-N", "--", link.name)
    link.unlink()
    caller_before = _caller_state(repo)
    baseline = capture_repository_baseline(repo)
    assert any(entry.path == link.name and entry.mode == 0o120000 for entry in baseline.index_entries)

    with pytest.raises(RepositoryIsolationError, match="unsupported object type"):
        create_seat_workspace(baseline, tmp_path / "run", "claude")

    assert _caller_state(repo) == caller_before


def test_baseline_digest_binds_index_flags_without_any_worktree_change(tmp_path):
    repo = _repo(tmp_path)
    before = capture_repository_baseline(repo)
    _git(repo, "update-index", "--assume-unchanged", "tracked.txt")
    after = capture_repository_baseline(repo)

    assert before.head_entries == after.head_entries
    assert before.entries == after.entries
    assert before.deleted_paths == after.deleted_paths
    assert before.index_entries != after.index_entries
    assert before.digest != after.digest


def test_old_unflagged_baseline_digest_cannot_verify_a_new_seat(tmp_path):
    """Pre-flag receipts must not silently claim that their workspace was verified under V2."""
    repo = _repo(tmp_path)
    baseline = capture_repository_baseline(repo)
    old = hashlib.sha256()
    for label, entries in (
        (b"head", baseline.head_entries), (b"index", baseline.index_entries),
        (b"worktree", baseline.entries),
    ):
        old.update(label + b"\0")
        for entry in entries:
            old.update(entry.path.encode("utf-8", "surrogateescape") + b"\0")
            old.update(f"{entry.mode:o}".encode() + b"\0")
            if isinstance(entry, fanout.RepositoryEntry):
                old.update(entry.kind.encode() + b"\0")
            else:
                old.update(str(entry.stage).encode() + b"\0")
            old.update(hashlib.sha256(entry.data).digest())
    old.update(b"head-oid\0" + baseline.head.encode("ascii") + b"\0")
    for path in baseline.deleted_paths:
        old.update(b"deleted\0" + path.encode("utf-8", "surrogateescape") + b"\0")
    for directory in baseline.directories:
        old.update(b"directory\0" + directory.path.encode("utf-8", "surrogateescape") + b"\0")
        old.update(f"{directory.mode:o}".encode() + b"\0")
    legacy_digest = hashlib.sha256(old.digest()).hexdigest()

    controller = fanout.create_lifecycle_controller(tmp_path / "controller")
    seat = create_seat_workspace(baseline, controller.root / "workspaces", "claude")
    claimed_old_seat = fanout.SeatWorkspace(seat.root, seat.seat_id, legacy_digest)

    assert baseline.digest != legacy_digest
    with pytest.raises(fanout.RepositoryValidationError, match="another immutable baseline"):
        fanout.verify_seat_workspace(baseline, claimed_old_seat, controller=controller)


def test_workspace_baseline_validation_detects_skip_worktree_flag_drift(tmp_path):
    """A workspace that loses its index flag cannot pass the immutable-baseline guard."""
    repo = _repo(tmp_path)
    _git(repo, "update-index", "--skip-worktree", "tracked.txt")
    baseline = capture_repository_baseline(repo)
    controller = fanout.create_lifecycle_controller(tmp_path / "controller")
    seat = create_seat_workspace(baseline, controller.root / "workspaces", "claude")
    verification = fanout.verify_seat_workspace(baseline, seat, controller=controller)
    resumed = fanout.resume_seat_workspace(
        seat, controller=controller, evidence_digest=verification.evidence_digest,
    )
    _git(seat.root, "update-index", "--no-skip-worktree", "tracked.txt")

    with pytest.raises(fanout.RepositoryValidationError, match="baseline|replay"):
        fanout.validate_seat_workspace_baseline(baseline, controller, resumed)


def test_initial_workspace_verification_detects_index_flag_drift(tmp_path):
    repo = _repo(tmp_path)
    _git(repo, "update-index", "--assume-unchanged", "tracked.txt")
    baseline = capture_repository_baseline(repo)
    controller = fanout.create_lifecycle_controller(tmp_path / "controller")
    seat = create_seat_workspace(baseline, controller.root / "workspaces", "claude")
    _git(seat.root, "update-index", "--no-assume-unchanged", "tracked.txt")

    with pytest.raises(fanout.RepositoryValidationError, match="baseline|replay"):
        fanout.verify_seat_workspace(baseline, seat, controller=controller)


@pytest.mark.parametrize("corruption", (b"\tflagz: 0\n", b"\tflags: 10000\n"))
def test_capture_rejects_unknown_git_index_debug_layout_or_flag(tmp_path, monkeypatch, corruption):
    repo = _repo(tmp_path)
    implementation = sys.modules[f"{SPEC.name}.repo"]
    original_git = implementation._git

    def altered_git(cwd, *args, **kwargs):
        output = original_git(cwd, *args, **kwargs)
        if args == ("ls-files", "--stage", "--debug", "-z"):
            altered = output.replace(b"\tflags: 0\n", corruption, 1)
            assert altered != output
            return altered
        return output

    monkeypatch.setattr(implementation, "_git", altered_git)
    with pytest.raises(fanout.RepositoryValidationError, match="index|flag|debug"):
        capture_repository_baseline(repo)
    assert not (tmp_path / "run").exists()


def test_baseline_binds_directory_modes_and_replays_them_into_seats(tmp_path):
    """A directory mode is immutable baseline state, not an ambient mode from seat creation."""
    repo = _repo(tmp_path)
    private = repo / "private-dir"
    private.mkdir()
    (private / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _git(repo, "add", "private-dir/tracked.txt")
    _git(repo, "commit", "-qm", "private directory")
    private.chmod(0o700)

    baseline = capture_repository_baseline(repo)
    seat = create_seat_workspace(baseline, tmp_path / "run", "claude")

    assert [(entry.path, entry.mode) for entry in baseline.directories if entry.path == "private-dir"] == [
        ("private-dir", 0o700),
    ]
    assert stat.S_IMODE((seat.root / "private-dir").stat().st_mode) == 0o700


def test_baseline_binds_empty_directory_modes_and_replays_them_into_seats(tmp_path):
    """An existing empty directory is immutable worktree state rather than an ambient omission."""
    repo = _repo(tmp_path)
    empty = repo / "empty-private"
    empty.mkdir()
    empty.chmod(0o711)

    baseline = capture_repository_baseline(repo)
    seat = create_seat_workspace(baseline, tmp_path / "run", "claude")

    assert [(entry.path, entry.mode) for entry in baseline.directories if entry.path == "empty-private"] == [
        ("empty-private", 0o711),
    ]
    assert stat.S_IMODE((seat.root / "empty-private").stat().st_mode) == 0o711


def test_capture_refuses_secrets_escaping_links_and_size_breaches_before_workspace_creation(tmp_path):
    """Unsafe bytes must fail before a seat can receive them."""
    repo = _repo(tmp_path)
    (repo / ".env").write_text("TOKEN=never-share-this\n")
    with pytest.raises(RepositorySecretError):
        capture_repository_baseline(repo)
    assert not (tmp_path / "run").exists()

    (repo / ".env").unlink()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n")
    (repo / "escape").symlink_to(outside)
    with pytest.raises(RepositoryIsolationError, match="symlink"):
        capture_repository_baseline(repo)

    (repo / "escape").unlink()
    (repo / "large.bin").write_bytes(b"12345")
    with pytest.raises(RepositoryQuotaError, match="file"):
        capture_repository_baseline(repo, limits=RepoLimits(max_file_bytes=4, max_total_bytes=20))
    with pytest.raises(RepositoryQuotaError, match="total"):
        capture_repository_baseline(repo, limits=RepoLimits(max_file_bytes=100, max_total_bytes=4))


def test_capture_refuses_a_secret_that_only_survives_in_the_immutable_head_snapshot(tmp_path):
    """Scanning only present worktree files would leak a staged deletion into the seat history."""
    repo = _repo(tmp_path)
    (repo / "ordinary-name.txt").write_text("API_KEY=ABCDEFGHIJKLMNOP\n")
    _git(repo, "add", "ordinary-name.txt")
    _git(repo, "commit", "-qm", "secret fixture")
    (repo / "ordinary-name.txt").unlink()
    _git(repo, "add", "-u", "ordinary-name.txt")

    with pytest.raises(RepositorySecretError, match="ordinary-name.txt"):
        capture_repository_baseline(repo)


def test_capture_refuses_a_known_secret_signature_embedded_in_binary_bytes(tmp_path):
    """A NUL byte must not turn a known credential signature into a safe baseline entry."""
    repo = _repo(tmp_path)
    payload = b"\0" + b"AK" + b"IA" + b"1234567890" + b"ABCDEF"
    (repo / "payload.bin").write_bytes(payload)
    _git(repo, "add", "payload.bin")
    _git(repo, "commit", "-qm", "binary secret fixture")

    with pytest.raises(RepositorySecretError, match="payload.bin"):
        capture_repository_baseline(repo)


def _baseline_candidate(tmp_path, payload, placement):
    repo = _repo(tmp_path)
    name = "candidate.data" if placement == "untracked" else "tracked.txt"
    path = repo / name
    path.write_bytes(payload)
    if placement in {"head", "index"}:
        _git(repo, "add", "--", name)
        if placement == "head":
            _git(repo, "commit", "-qm", "candidate bytes")
            path.unlink()
            _git(repo, "add", "-u", "--", name)
        else:
            path.write_bytes(b"harmless worktree bytes\n")
    return repo, name


@pytest.mark.parametrize("placement", ("head", "index", "worktree", "untracked"))
@pytest.mark.parametrize("payload", (
    pytest.param(
        b"volumes:\n  secret:\n    external: true\n    name: ${FIXTURE_VOLUME:?}\n",
        id="compose-mapping",
    ),
    pytest.param(
        b"settings:\n  token:\n    subscription: false\n    lookup: op://Automation/example/credential\n",
        id="different-long-child-key",
    ),
    pytest.param(b"PASSWORD:\n  enabled_flag: true\n", id="case-insensitive-mapping"),
    pytest.param(b"secret:\n  external: bounded\n", id="short-child-scalar"),
    pytest.param(
        b'message: "short"\nsecret:\n  external: true\n', id="closed-inline-quote-before-mapping",
    ),
    pytest.param(
        b"volumes:\r\n  secret:\r\n    external: true\r\n    name: ${FIXTURE_VOLUME:?}\r\n",
        id="crlf-mapping",
    ),
))
def test_baseline_admits_plain_sensitive_mapping_keys_in_each_namespace(tmp_path, payload, placement):
    repo, name = _baseline_candidate(tmp_path, payload, placement)
    before = _caller_state(repo)

    baseline = capture_repository_baseline(repo)

    entries = {
        "head": baseline.head_entries, "index": baseline.index_entries,
        "worktree": baseline.entries, "untracked": baseline.entries,
    }[placement]
    assert next(entry.data for entry in entries if entry.path == name) == payload
    assert _caller_state(repo) == before


@pytest.mark.parametrize("placement", ("head", "index", "worktree", "untracked"))
@pytest.mark.parametrize("payload", (
    pytest.param(b"password: ABCDEFGHIJKL\n", id="same-line-colon"),
    pytest.param(b"api-key = ABCDEFGHIJKL\n", id="same-line-equals"),
    pytest.param(b"token = 'ABCDEFGHIJKL'\n", id="single-quoted"),
    pytest.param(b'password: "ABCDEFGHIJKL"\n', id="double-quoted"),
    pytest.param(b"password:\n  ABCDEFGHIJKL\n", id="wrapped-literal"),
    pytest.param(b"password:\n  'ABCDEFGHIJKL'\n", id="wrapped-single-quoted"),
    pytest.param(b'password:\n  "ABCDEFGHIJKL"\n', id="wrapped-double-quoted"),
    pytest.param(b"secret:\n  external\n", id="field-looking-scalar"),
    pytest.param(b'secret:\n  "external: true"\n', id="quoted-colon-scalar"),
    pytest.param(b"secret: external: true\n", id="same-line-colon-scalar"),
    pytest.param(b"secret=\n  external: true\n", id="equals-not-mapping"),
    pytest.param(b"secret:\n  password: ABCDEFGHIJKL\n", id="sensitive-child-literal"),
    pytest.param(b"secret:\n  external: ABCDEFGHIJKL\n", id="nonsensitive-child-literal"),
    pytest.param(b"secret:\n  external: external\n", id="eight-byte-child-literal"),
    pytest.param(
        b"secret:\n  external: true\n  label: ABCDEFGHIJKL\n", id="later-child-literal",
    ),
    pytest.param(b"secret:\n  external: 'ABCDEFGHIJKL'\n", id="quoted-child-literal"),
    pytest.param(
        b"secret:\n  external: true\npassword: ABCDEFGHIJKL\n", id="later-independent-literal",
    ),
    pytest.param(b"secret:\n  external:\n    ABCDEFGHIJKL\n", id="child-wrapped-literal"),
    pytest.param(b"secret:\nexternal: true\n", id="equal-indentation"),
    pytest.param(
        b"secret:\n  external: true\n   enabled_flag: false\n", id="inconsistent-child-indentation",
    ),
    pytest.param(b"\tsecret:\n\t  external: true\n", id="tab-indentation"),
    pytest.param(b"secret:\n  external:true\n", id="uncertain-colon-delimiter"),
    pytest.param(b"secret:\n  external: 'short\n", id="unclosed-child-quote"),
    pytest.param(b"secret:\n  external:\n    localvalue: true\n", id="nested-container"),
    pytest.param(b"secret:\n  external: |\n    ABCDEFGHIJKL\n", id="block-scalar"),
    pytest.param(b"secret:\n  external: {field: true}\n", id="flow-container"),
    pytest.param(b"secret:\n  external: &fixture\n", id="anchor"),
    pytest.param(b"message: |\n  secret:\n    external: true\n", id="enclosing-block-scalar"),
    pytest.param(b"message: '\n  secret:\n    external: true\n'\n", id="enclosing-quoted-scalar"),
    pytest.param(
        b'password = """\nsecret:\n  external: true\n"""\n', id="python-triple-quoted-literal",
    ),
    pytest.param(
        b"password = '''\nsecret:\n  external: true\n'''\n", id="python-triple-single-quoted-literal",
    ),
    pytest.param(
        b'message: "\nsecret:\n  external: true\n"\n', id="root-enclosing-double-quote",
    ),
    pytest.param(
        b"message: '\nsecret:\n  external: true\n'\n", id="root-enclosing-single-quote",
    ),
    pytest.param(
        b'message: "\nsettings:\n  secret:\n    external: true\n"\n',
        id="quote-before-intervening-ancestor",
    ),
    pytest.param(
        b"message: |\nsecret:\n  external: true\n", id="uncertain-root-block-context",
    ),
    pytest.param(
        b"message: {\nsecret:\n  external: true\n}\n", id="uncertain-root-flow-context",
    ),
    pytest.param(
        b'unknown "prefix\nsettings:\n  token:\n    subscription: false\n',
        id="unknown-quoted-prefix",
    ),
    pytest.param(
        b"secret:\n  external: true\n  name: ${" + b"AK" + b"IA" + b"1234567890ABCDEF}\n",
        id="aws-signature-in-mapping-reference",
    ),
    pytest.param(
        b"secret:\n  external: true\n  lookup: op://" + b"gh" + b"p_" + b"ABCDEFGHIJKLMNOPQRST\n",
        id="github-signature-in-mapping-reference",
    ),
))
def test_baseline_refuses_literals_and_ambiguous_mapping_syntax_in_each_namespace(
    tmp_path, payload, placement,
):
    repo, name = _baseline_candidate(tmp_path, payload, placement)
    before = _caller_state(repo)

    with pytest.raises(RepositorySecretError, match="possible secret") as caught:
        baseline = capture_repository_baseline(repo)
        create_seat_workspace(baseline, tmp_path / "run", "claude")

    assert name in str(caught.value)
    assert not (tmp_path / "run").exists()
    assert _caller_state(repo) == before


@pytest.mark.parametrize("placement", ("head", "index", "worktree", "untracked"))
@pytest.mark.parametrize("payload", (
    pytest.param(b"password: ABCDEFGHIJKL\n", id="binary-same-line"),
    pytest.param(b"token: 'ABCDEFGHIJKL'\n", id="binary-quoted"),
    pytest.param(b"password:\n  ABCDEFGHIJKL\n", id="binary-wrapped"),
    pytest.param(b"AK" + b"IA" + b"1234567890ABCDEF", id="binary-aws"),
    pytest.param(b"gh" + b"p_" + b"ABCDEFGHIJKLMNOPQRST", id="binary-github"),
    pytest.param(
        b"secret:\n  external: true\n  name: ${" + b"AK" + b"IA" + b"1234567890ABCDEF}\n",
        id="signature-in-mapping-reference",
    ),
))
def test_baseline_refuses_secret_matches_inside_binary_bytes_in_each_namespace(
    tmp_path, payload, placement,
):
    repo, name = _baseline_candidate(tmp_path, b"\x00\xff" + payload + b"\x00\xfe", placement)

    with pytest.raises(RepositorySecretError, match="possible secret") as caught:
        baseline = capture_repository_baseline(repo)
        create_seat_workspace(baseline, tmp_path / "run", "claude")

    assert name in str(caught.value)
    assert not (tmp_path / "run").exists()


def test_repeated_safe_mappings_have_proportional_scanner_work():
    """Doubling safe input must not quadruple Python scanner work."""
    counts = []
    previous_trace = sys.gettrace()
    for mappings in (100, 200):
        events = 0

        def trace(frame, event, argument):
            nonlocal events
            if event == "line" and frame.f_code.co_filename == str(FANOUT_ROOT / "repo.py"):
                events += 1
            return trace

        try:
            sys.settrace(trace)
            fanout.repo._reject_secret("candidate.data", b"secret:\n  external: true\n" * mappings)
        finally:
            sys.settrace(previous_trace)
        counts.append(events)

    assert counts[0] > 0
    assert counts[1] <= 3 * counts[0], counts


@pytest.mark.parametrize("placement", ("root", "descendant", "alias-descendant"))
def test_workspace_rejects_a_run_root_at_or_below_the_caller_repository(tmp_path, placement):
    """A run root under the caller would create untracked seat files before handover."""
    repo = _repo(tmp_path)
    baseline = capture_repository_baseline(repo)
    alias = tmp_path / "repo-alias"
    alias.symlink_to(repo, target_is_directory=True)
    run_root = {
        "root": repo,
        "descendant": repo / "fanout-run",
        "alias-descendant": alias / "fanout-run",
    }[placement]
    before = _caller_state(repo)

    with pytest.raises(RepositoryIsolationError, match="run root"):
        create_seat_workspace(baseline, run_root, "claude")

    assert _caller_state(repo) == before


def test_capture_neutralizes_hostile_git_environment_and_never_changes_caller_state(tmp_path, monkeypatch):
    """Ambient selectors/config and workspace setup must not touch the caller's repository."""
    repo = _repo(tmp_path)
    rogue = _repo(tmp_path, "rogue")
    marker = tmp_path / "hostile-hook-ran"
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "post-index-change").write_text(f"#!/bin/sh\ntouch {marker}\n")
    (hooks / "post-index-change").chmod(0o755)
    monkeypatch.setenv("GIT_DIR", os.fspath(rogue / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", os.fspath(rogue))
    monkeypatch.setenv("GIT_INDEX_FILE", os.fspath(rogue / ".git" / "index"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", os.fspath(hooks))
    before = _caller_state(repo)

    baseline = capture_repository_baseline(repo)
    seat = create_seat_workspace(baseline, tmp_path / "run", "claude")

    assert baseline.repository == repo.resolve()
    assert seat.root.is_dir()
    assert _caller_state(repo) == before
    assert not marker.exists()


@pytest.mark.parametrize("target", ("../outside", "/outside"))
def test_staged_escaping_link_is_refused_even_when_current_link_is_safe(tmp_path, target):
    repo = _repo(tmp_path)
    link = repo / "inside-link"
    link.unlink()
    link.symlink_to(target)
    _git(repo, "add", "inside-link")
    link.unlink()
    link.symlink_to("target.txt")
    before = _caller_state(repo)
    with pytest.raises(RepositoryIsolationError, match="symlink"):
        capture_repository_baseline(repo)
    assert _caller_state(repo) == before


def test_safe_divergent_staged_and_current_links_replay_exactly(tmp_path):
    repo = _repo(tmp_path)
    link = repo / "inside-link"
    link.unlink()
    link.symlink_to("tracked.txt")
    _git(repo, "add", "inside-link")
    link.unlink()
    link.symlink_to("target.txt")
    baseline = capture_repository_baseline(repo)
    seat = create_seat_workspace(baseline, tmp_path / "run", "claude")
    assert _git(seat.root, "show", ":inside-link").stdout == b"tracked.txt"
    assert os.readlink(seat.root / "inside-link") == "target.txt"


@pytest.mark.parametrize("redirect", ("caller", "other", "file"))
def test_seats_parent_redirect_is_refused_before_any_destination_write(tmp_path, redirect):
    repo = _repo(tmp_path)
    other = _repo(tmp_path, "other")
    baseline = capture_repository_baseline(repo)
    run = tmp_path / "run"
    run.mkdir()
    seats = run / "seats"
    if redirect == "file":
        seats.write_bytes(b"keep me")
    else:
        seats.symlink_to(repo if redirect == "caller" else other, target_is_directory=True)
    before = (_caller_state(repo), _caller_state(other), seats.lstat())
    with pytest.raises(RepositoryIsolationError, match="seats"):
        create_seat_workspace(baseline, run, "claude")
    assert (_caller_state(repo), _caller_state(other), seats.lstat()) == before


@pytest.mark.parametrize("join", ("initial", "warm", "cold", "first-turn", "native"))
def test_seat_git_worktree_redirect_is_refused_at_workspace_authority_joins(tmp_path, join):
    repo = _repo(tmp_path)
    if join == "native":
        (repo / "inside-link").unlink()
    baseline = capture_repository_baseline(repo)
    controller = fanout.create_lifecycle_controller(tmp_path / "run")
    seat = create_seat_workspace(baseline, controller.root, "claude")
    receipt = fanout.verify_seat_workspace(baseline, seat, controller=controller)
    if join == "native":
        adapter = fanout.CodexAdapter()
        profile = fanout.ExecutorProfile("codex", adapter, adapter.capabilities, "repo-write", "standard",
                                         "diagnostic-model", "low", "diagnostic-cli", ("/bin/sh",),
                                         ("/bin/sh", "--resume", "{session_id}"), 10, 10)
        request = fanout.ProviderRequest("codex", "diagnostic only", cwd=seat.root,
            execution_class="repo-write", profile=profile, run_id="run-1", task_id="task-1",
            target_id="target-1", inputs_digest="a" * 64, seat_id="claude")
        command = fanout.ProcessCommand(argv=("/bin/sh", "-c", "printf diagnostic"), stdin=b"", cwd=seat.root)
        owner_root = tmp_path / "native-owner"
        owner_root.mkdir(mode=0o700)
        boundary = fanout.issue_native_boundary(controller, receipt, run_id="run-1", task_id="task-1",
            target_id="target-1", inputs_digest="a" * 64, request=request, command=command,
            other_target_roots=(repo,), denied_owner_roots=(owner_root,))
    identities = (seat.root.stat().st_ino, (seat.root / ".git").stat().st_ino)
    _git(seat.root, "config", "core.worktree", str(repo))
    (seat.root / "tracked.txt").write_bytes(b"seat drift\n")
    assert (seat.root.stat().st_ino, (seat.root / ".git").stat().st_ino) == identities
    with pytest.raises(fanout.ProviderRequestError if join == "native" else RepositoryIsolationError) as refused:
        if join == "initial":
            fanout.verify_seat_workspace(baseline, seat, controller=controller)
        elif join == "warm":
            fanout.validate_seat_workspace(controller, receipt)
        elif join == "cold":
            fanout.resume_seat_workspace(seat, controller=controller, evidence_digest=receipt.evidence_digest)
        elif join == "first-turn":
            fanout.validate_seat_workspace_baseline(baseline, controller, receipt)
        else:
            fanout.validate_native_boundary(boundary, request, command, controller, receipt)
    if join == "native":
        assert isinstance(refused.value.__cause__, RepositoryIsolationError)
    else:
        assert "Git" in str(refused.value) or "worktree" in str(refused.value)


def _tiny_repo(tmp_path):
    repo = tmp_path / "tiny"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "user.email", "fixture@example.invalid")
    (repo / "one").write_bytes(b"1234")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "seed")
    return repo


@pytest.mark.parametrize("namespace", ("head", "index", "worktree"))
def test_repository_quota_refuses_content_before_reading_oversized_namespace(tmp_path, monkeypatch, namespace):
    repo = _tiny_repo(tmp_path)
    if namespace != "head":
        (repo / "one").write_bytes(b"12345")
        if namespace == "index":
            _git(repo, "add", "one")
            (repo / "one").write_bytes(b"1234")
    module = sys.modules[capture_repository_baseline.__module__]
    real_git = module._git
    reads = []
    def observe_git(root, *args, **kwargs):
        if args[:2] == ("cat-file", "blob"):
            reads.append(args)
        return real_git(root, *args, **kwargs)
    monkeypatch.setattr(module, "_git", observe_git)
    with pytest.raises(RepositoryQuotaError):
        capture_repository_baseline(repo, limits=RepoLimits(3 if namespace == "head" else 4, 100))
    assert len(reads) == {"head": 0, "index": 1, "worktree": 2}[namespace]


def test_repository_quotas_are_additive_and_exact_limits_are_accepted(tmp_path, monkeypatch):
    repo = _tiny_repo(tmp_path)
    module = sys.modules[capture_repository_baseline.__module__]
    real_read = module.os.read
    requests = []
    def observe_read(fd, size):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            requests.append(size)
        return real_read(fd, size)
    monkeypatch.setattr(module.os, "read", observe_read)
    baseline = capture_repository_baseline(repo, limits=RepoLimits(4, 12))
    assert [entry.data for entry in baseline.entries] == [b"1234"]
    assert requests and max(requests) <= 4
    with pytest.raises(RepositoryQuotaError):
        capture_repository_baseline(repo, limits=RepoLimits(4, 11))


@pytest.mark.parametrize("direction", ("file-to-directory", "directory-to-file"))
def test_capture_and_replay_preserve_historical_paths_under_structural_replacement(tmp_path, direction):
    repo = _tiny_repo(tmp_path)
    if direction == "directory-to-file":
        (repo / "one").unlink()
        (repo / "one").mkdir()
        (repo / "one" / "child").write_bytes(b"old")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "directory")
        (repo / "one" / "child").unlink()
        (repo / "one").rmdir()
        (repo / "one").write_bytes(b"new")
        deleted = ("one/child",)
    else:
        (repo / "one").unlink()
        (repo / "one").mkdir()
        (repo / "one" / "child").write_bytes(b"new")
        deleted = ("one",)
    before = _caller_state(repo)
    baseline = capture_repository_baseline(repo)
    seat = create_seat_workspace(baseline, tmp_path / "run", "claude")
    assert baseline.deleted_paths == deleted
    assert _non_git_tree(seat.root) == _non_git_tree(repo)
    assert _caller_state(repo) == before


@pytest.mark.parametrize("flag", ("--assume-unchanged", "--skip-worktree"))
def test_writable_target_refuses_status_hidden_dirty_content_before_dispatch(tmp_path, flag):
    repo, spec = _target_repo(tmp_path, "address")
    _git(repo, "update-index", flag, "tracked.txt")
    (repo / "tracked.txt").write_bytes(b"hidden dirty bytes\n")
    assert _git(repo, "status", "--porcelain=v1").stdout == b""
    module = sys.modules[capture_repository_baseline.__module__]
    bindings, baselines = module.capture_and_bind_targets({"address": spec}, {"address": repo})
    seat = create_seat_workspace(baselines["address"], tmp_path / "read-only-run", "claude")
    assert (seat.root / "tracked.txt").read_bytes() == b"hidden dirty bytes\n"
    with pytest.raises(fanout.RepositoryValidationError, match="clean"):
        module.revalidate_target_bindings(bindings, writable_ids={"address"})


def test_repository_capture_refuses_regular_file_growth_during_bounded_read(tmp_path, monkeypatch):
    repo = _tiny_repo(tmp_path)
    real_read = os.read
    grew = False
    def grow_during_read(fd, size):
        nonlocal grew
        payload = real_read(fd, size)
        if stat.S_ISREG(os.fstat(fd).st_mode) and not grew:
            grew = True
            with (repo / "one").open("ab") as handle:
                handle.write(b"5")
        return payload
    monkeypatch.setattr(os, "read", grow_during_read)
    with pytest.raises(RepositoryIsolationError, match="grew|changed"):
        capture_repository_baseline(repo, limits=RepoLimits(4, 12))
    assert grew and (repo / "one").read_bytes() == b"12345"


def test_repository_capture_refuses_fifo_without_reading_it(tmp_path, monkeypatch):
    repo = _tiny_repo(tmp_path)
    (repo / "one").unlink()
    os.mkfifo(repo / "one")
    real_open = os.open
    def refuse_fifo_open(path, flags, *args, **kwargs):
        if path == "one":
            pytest.fail("capture attempted to open a known FIFO")
        return real_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", refuse_fifo_open)
    with pytest.raises(RepositoryIsolationError, match="regular file"):
        capture_repository_baseline(repo)


def test_workspace_identity_allows_legitimate_resumed_content_changes(tmp_path):
    repo = _repo(tmp_path)
    baseline = capture_repository_baseline(repo)
    controller = fanout.create_lifecycle_controller(tmp_path / "run")
    seat = create_seat_workspace(baseline, controller.root, "claude")
    receipt = fanout.verify_seat_workspace(baseline, seat, controller=controller)
    (seat.root / "tracked.txt").write_bytes(b"legitimate turn result\n")
    fanout.validate_seat_workspace(controller, receipt)
    resumed = fanout.resume_seat_workspace(seat, controller=controller, evidence_digest=receipt.evidence_digest)
    assert resumed.evidence_digest == receipt.evidence_digest
    assert (seat.root / "tracked.txt").read_bytes() == b"legitimate turn result\n"
