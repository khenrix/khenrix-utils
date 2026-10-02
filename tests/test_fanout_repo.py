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
