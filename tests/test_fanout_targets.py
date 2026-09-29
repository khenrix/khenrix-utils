"""Local Git identity contracts for multi-repository fanout targets."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
FANOUT_ROOT = ROOT / "shared" / "lib" / "fanout"
SPEC = importlib.util.spec_from_file_location(
    "_fanout_target_contracts", FANOUT_ROOT / "__init__.py",
    submodule_search_locations=[str(FANOUT_ROOT)],
)
assert SPEC and SPEC.loader
fanout = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fanout
SPEC.loader.exec_module(fanout)

TargetSpec = fanout.TargetSpec
PlanValidationError = fanout.PlanValidationError
RepositoryValidationError = fanout.RepositoryValidationError


def _git(root: Path, *args: str) -> str:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
    result = subprocess.run(
        ("git", "-C", str(root), *args), capture_output=True, text=True,
        env=environment, check=True,
    )
    return result.stdout.strip()


def _repo(tmp_path: Path, name: str, repository: str = "address-service") -> Path:
    root = tmp_path / name
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Fixture")
    _git(root, "config", "user.email", "fixture@example.invalid")
    (root / "tracked.txt").write_text("fixture\n")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "-qm", "seed")
    _git(root, "remote", "add", "origin", f"https://github.com/example/{repository}.git")
    return root


def _spec(target_id: str = "address", repository: str = "address-service") -> object:
    return TargetSpec(
        target_id, f"github.com/example/{repository}", "TASK-123",
        f"refs/heads/feat/TASK-123-{target_id}",
    )


@pytest.mark.parametrize("url", [
    "git@github.com:example/address-service.git",
    "https://github.com/example/address-service.git",
])
def test_equivalent_origin_forms(url):
    assert fanout.normalize_origin(url) == "github.com/example/address-service"


@pytest.mark.parametrize("url", [
    "", "https://token@github.com/a/b.git", "file:///tmp/a",
    "https://github.com/example/../address-service.git",
    "https://github.com//example/address-service.git",
    "https://../example/address-service.git",
])
def test_unsafe_origin_fails(url):
    with pytest.raises(RepositoryValidationError):
        fanout.normalize_origin(url)


def test_same_ticket_is_valid_across_distinct_repositories(tmp_path):
    address = _repo(tmp_path, "address", "address-service")
    booking = _repo(tmp_path, "booking", "booking-service")
    bindings = {
        "address": fanout.resolve_target(_spec(), address),
        "booking": fanout.resolve_target(_spec("booking", "booking-service"), booking),
    }
    fanout.validate_target_bindings(bindings, writable_ids={"address", "booking"})
    assert bindings["address"].spec.ticket_key == bindings["booking"].spec.ticket_key


@pytest.mark.parametrize("alias", ("refs/heads/main", "refs/heads/hidden"))
def test_ticket_branch_symbolic_ref_is_not_admitted(tmp_path, alias):
    root = _repo(tmp_path, "address")
    spec = _spec()
    _git(root, "symbolic-ref", spec.branch_ref, alias)

    with pytest.raises(RepositoryValidationError, match="symbolic"):
        fanout.resolve_target(spec, root)


def test_ticket_must_be_an_exact_branch_token():
    with pytest.raises(PlanValidationError):
        TargetSpec(
            "address", "github.com/example/address-service", "TASK-123",
            "refs/heads/feat/TASK-1234-address",
        )


def test_malformed_portable_spec_raises_plan_validation_error():
    with pytest.raises(PlanValidationError):
        TargetSpec.from_dict({})


@pytest.mark.parametrize("branch", [
    "feat/TASK-123-address", "refs/tags/feat/TASK-123-address",
    "refs/heads/feat/TASK-123-address..bad",
])
def test_branch_must_be_a_valid_full_head_ref(branch):
    with pytest.raises(PlanValidationError):
        TargetSpec("address", "github.com/example/address-service", "TASK-123", branch)


def test_same_origin_in_two_clones_is_not_two_writable_targets(tmp_path):
    first = _repo(tmp_path, "first")
    second = _repo(tmp_path, "second")
    bindings = {
        "address": fanout.resolve_target(_spec(), first),
        "booking": fanout.resolve_target(_spec("booking"), second),
    }
    with pytest.raises(RepositoryValidationError, match="repository identity"):
        fanout.validate_target_bindings(bindings, writable_ids={"address", "booking"})


def test_two_worktrees_with_one_common_directory_are_not_two_writers(tmp_path):
    first = _repo(tmp_path, "first")
    second = tmp_path / "second"
    _git(first, "worktree", "add", "-q", "-b", "second", str(second))
    bindings = {
        "address": fanout.resolve_target(_spec(), first),
        "booking": fanout.resolve_target(_spec("booking"), second),
    }
    with pytest.raises(RepositoryValidationError, match="common directory"):
        fanout.validate_target_bindings(bindings, writable_ids={"address", "booking"})


def test_rewritten_origin_is_rejected(tmp_path):
    root = _repo(tmp_path, "address")
    _git(root, "config", "--local", "--add", "url.https://example.invalid/.insteadOf", "https://github.com/")
    with pytest.raises(RepositoryValidationError, match="origin"):
        fanout.resolve_target(_spec(), root)


@pytest.mark.parametrize("included_url", [
    "https://github.com/example/address-service.git",
    "https://github.com/example/booking-service.git",
])
def test_included_origin_value_is_ambiguous_even_when_raw_origin_matches(tmp_path, included_url):
    root = _repo(tmp_path, "address")
    included = tmp_path / "included.gitconfig"
    included.write_text(f'[remote "origin"]\n\turl = {included_url}\n')
    _git(root, "config", "--local", "include.path", str(included))
    effective = _git(root, "config", "--includes", "--get-all", "remote.origin.url")
    assert len(effective.splitlines()) == 2
    with pytest.raises(RepositoryValidationError, match="origin"):
        fanout.resolve_target(_spec(), root)


def test_missing_or_ambiguous_origin_is_rejected(tmp_path):
    root = _repo(tmp_path, "address")
    _git(root, "config", "--local", "--unset-all", "remote.origin.url")
    with pytest.raises(RepositoryValidationError, match="origin"):
        fanout.resolve_target(_spec(), root)
    _git(root, "config", "--local", "--add", "remote.origin.url", "https://github.com/example/address-service.git")
    _git(root, "config", "--local", "--add", "remote.origin.url", "git@github.com:example/address-service.git")
    with pytest.raises(RepositoryValidationError, match="origin"):
        fanout.resolve_target(_spec(), root)


def test_empty_second_origin_value_is_ambiguous(tmp_path):
    root = _repo(tmp_path, "address")
    _git(root, "config", "--local", "--add", "remote.origin.url", "")
    with pytest.raises(RepositoryValidationError, match="origin"):
        fanout.resolve_target(_spec(), root)


def test_symlinked_root_is_rejected(tmp_path):
    root = _repo(tmp_path, "address")
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(RepositoryValidationError, match="symlink"):
        fanout.resolve_target(_spec(), alias)


def test_binding_is_complete_only_after_matching_baseline_capture(tmp_path):
    root = _repo(tmp_path, "address")
    binding = fanout.resolve_target(_spec(), root)
    assert binding.baseline_sha256 is None
    with pytest.raises(RepositoryValidationError, match="baseline"):
        binding.to_dict()
    baseline = fanout.capture_repository_baseline(root)
    complete = fanout.bind_captured_baseline(binding, baseline)
    assert complete.baseline_sha256 == baseline.digest
    assert fanout.TargetBinding.from_dict(complete.to_dict()) == complete


def test_complete_binding_wire_format_rejects_missing_or_extra_fields(tmp_path):
    root = _repo(tmp_path, "address")
    complete = fanout.bind_captured_baseline(
        fanout.resolve_target(_spec(), root), fanout.capture_repository_baseline(root),
    )
    wire = complete.to_dict()
    with pytest.raises(RepositoryValidationError):
        fanout.TargetBinding.from_dict({**wire, "unknown": "drift"})
    with pytest.raises(RepositoryValidationError):
        fanout.TargetBinding.from_dict({key: value for key, value in wire.items() if key != "baseline_sha256"})
    with pytest.raises(RepositoryValidationError, match="baseline"):
        fanout.TargetBinding.from_dict({**wire, "baseline_sha256": None})
    with pytest.raises(RepositoryValidationError):
        fanout.TargetBinding.from_dict({key: value for key, value in wire.items() if key != "head_ref"})


def test_binding_rejects_baseline_from_another_repository(tmp_path):
    address = _repo(tmp_path, "address")
    booking = _repo(tmp_path, "booking", "booking-service")
    binding = fanout.resolve_target(_spec(), address)
    with pytest.raises(RepositoryValidationError, match="baseline"):
        fanout.bind_captured_baseline(binding, fanout.capture_repository_baseline(booking))
