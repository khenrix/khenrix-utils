"""Controller-issued native file boundary for disposable provider seats."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
FANOUT_ROOT = ROOT / "shared/lib/fanout"
SPEC = importlib.util.spec_from_file_location(
    "_fanout_native_boundary_contracts", FANOUT_ROOT / "__init__.py",
    submodule_search_locations=[str(FANOUT_ROOT)],
)
assert SPEC and SPEC.loader
fanout = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fanout
SPEC.loader.exec_module(fanout)


@pytest.fixture
def seat(request):
    disposable = tempfile.TemporaryDirectory(prefix="fanout-native-seat-", dir="/private/tmp")
    request.addfinalizer(disposable.cleanup)
    tmp_path = Path(disposable.name)
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(("git", "init", "-q", str(repository)), check=True)
    (repository / "fixture.txt").write_text("disposable fixture\n")
    subprocess.run(("git", "-C", str(repository), "add", "-A"), check=True)
    subprocess.run((
        "git", "-C", str(repository), "-c", "user.name=Test",
        "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture",
    ), check=True)
    baseline = fanout.capture_repository_baseline(repository)
    controller = fanout.create_lifecycle_controller(tmp_path / "controller")
    workspace = fanout.create_seat_workspace(
        baseline, controller.root / "workspaces", "codex-seat",
    )
    verification = fanout.verify_seat_workspace(
        baseline, workspace, controller=controller,
    )
    sibling = controller.root / "workspaces" / "seats" / "sibling"
    sibling.mkdir(mode=0o700)
    (sibling / "secret.txt").write_text("sibling seat\n")
    other = tmp_path / "other-target"
    other.mkdir(mode=0o700)
    (other / "secret.txt").write_text("other target\n")
    owner = tmp_path / "owner"
    owner.mkdir(mode=0o700)
    (owner / "run.json").write_text("owner state\n")
    adapter = fanout.CodexAdapter()
    profile = fanout.ExecutorProfile(
        "codex", adapter, adapter.capabilities, "repo-write", "standard",
        "diagnostic-model", "low", "diagnostic-cli", ("/bin/sh",),
        ("/bin/sh", "--resume", "{session_id}"), 10, 10,
    )
    request = fanout.ProviderRequest(
        "codex", "diagnostic only", cwd=workspace.root,
        execution_class="repo-write", profile=profile, run_id="run-1",
        task_id="task-1", target_id="target-1", inputs_digest="a" * 64,
        seat_id="codex-seat",
    )
    command = fanout.ProcessCommand(
        argv=("/bin/sh", "-c", "printf diagnostic"),
        stdin=b"", cwd=workspace.root,
    )
    return controller, verification, request, command, other, owner


def _boundary(seat, command=None):
    controller, verification, request, original, other, owner = seat
    return fanout.issue_native_boundary(
        controller, verification, run_id="run-1", task_id="task-1",
        target_id="target-1", inputs_digest="a" * 64, request=request,
        command=original if command is None else command,
        other_target_roots=(other, controller.root / "workspaces/seats/sibling"),
        denied_owner_roots=(owner,),
    )


def test_descriptor_authenticates_exact_request_command_and_controller(seat):
    controller, verification, request, command, _, _ = seat
    boundary = _boundary(seat)
    fanout.validate_native_boundary(boundary, request, command, controller, verification)
    wrapped = fanout.wrap_native_command(
        command, boundary, request=request, controller=controller,
        verification=verification,
    )
    assert wrapped.argv[:3] == ("/usr/bin/sandbox-exec", "-f", str(boundary.profile_path))
    assert wrapped.argv[3] == str(Path("/bin/sh").resolve())


def test_descriptor_rejects_cwd_route_profile_and_workspace_swaps(seat):
    controller, verification, request, command, _, _ = seat
    boundary = _boundary(seat)
    other_cwd = request.cwd.parent
    with pytest.raises(fanout.ProviderRequestError, match="boundary"):
        fanout.validate_native_boundary(boundary, replace(request, cwd=other_cwd), command,
                                        controller, verification)
    with pytest.raises(fanout.ProviderRequestError, match="boundary"):
        fanout.validate_native_boundary(boundary, replace(request, resume=True,
            session_id="exact-session"), command, controller, verification)
    with pytest.raises(fanout.ProviderRequestError, match="boundary"):
        fanout.validate_native_boundary(replace(boundary, profile_sha256="0" * 64),
                                        request, command, controller, verification)
    replacement = request.cwd.parent / "old-workspace"
    request.cwd.rename(replacement)
    request.cwd.mkdir()
    with pytest.raises(fanout.ProviderRequestError, match="boundary"):
        fanout.validate_native_boundary(boundary, request, command, controller, verification)


def test_descriptor_binds_initial_assigned_session_id(seat):
    controller, verification, request, command, other, owner = seat
    assigned = replace(request, session_id="initial-session-a")
    boundary = fanout.issue_native_boundary(
        controller, verification, run_id="run-1", task_id="task-1",
        target_id="target-1", inputs_digest="a" * 64,
        request=assigned, command=command, other_target_roots=(other,),
        denied_owner_roots=(owner,),
    )
    fanout.validate_native_boundary(boundary, assigned, command, controller, verification)
    for changed in (replace(assigned, session_id="initial-session-b"),
                    replace(assigned, session_id=None)):
        with pytest.raises(fanout.ProviderRequestError, match="boundary"):
            fanout.validate_native_boundary(boundary, changed, command,
                                            controller, verification)


def test_descriptor_rejects_changed_argv_stdin_and_route_command(seat):
    controller, verification, request, command, _, _ = seat
    boundary = _boundary(seat)
    for changed in (
        replace(command, argv=("/bin/sh", "-c", "printf different")),
        replace(command, stdin=b"different private prompt"),
        replace(command, argv=("/bin/sh", "--resume", "foreign-session")),
    ):
        with pytest.raises(fanout.ProviderRequestError, match="boundary"):
            fanout.validate_native_boundary(boundary, request, changed,
                                            controller, verification)


def test_descriptor_rejects_unadmitted_staged_skill_read_grant(seat):
    controller, verification, request, command, other, owner = seat
    staged = controller.root / "receipts"
    changed = replace(request, staged_skill_root=staged)
    with pytest.raises(fanout.ProviderRequestError, match="staged skill"):
        fanout.issue_native_boundary(
            controller, verification, run_id="run-1", task_id="task-1",
            target_id="target-1", inputs_digest="a" * 64, request=changed,
            command=command, other_target_roots=(other,), denied_owner_roots=(owner,),
        )


def test_descriptor_rejects_symlink_and_hardlink_aliases(seat):
    _, _, request, _, other, _ = seat
    (request.cwd / "alias").symlink_to(other, target_is_directory=True)
    with pytest.raises(fanout.ProviderRequestError, match="alias"):
        _boundary(seat)
    (request.cwd / "alias").unlink()
    (request.cwd / "alias").symlink_to(seat[0].root / "receipts", target_is_directory=True)
    with pytest.raises(fanout.ProviderRequestError, match="alias"):
        _boundary(seat)
    (request.cwd / "alias").unlink()
    os.link(other / "secret.txt", request.cwd / "hardlink")
    with pytest.raises(fanout.ProviderRequestError, match="alias"):
        _boundary(seat)


def test_descriptor_rejects_staged_skill_root_with_symlink_ancestor(seat):
    controller, verification, request, _, other, owner = seat
    parent = owner / "real"
    parent.mkdir()
    staged = parent / "staged"
    staged.mkdir()
    alias = owner / "alias"
    alias.symlink_to(parent, target_is_directory=True)
    changed = replace(request, staged_skill_root=alias / "staged")
    with pytest.raises(fanout.ProviderRequestError, match="staged skill"):
        fanout.issue_native_boundary(
            controller, verification, run_id="run-1", task_id="task-1",
            target_id="target-1", inputs_digest="a" * 64, request=changed,
            command=seat[3],
            other_target_roots=(other,), denied_owner_roots=(owner,),
        )


def test_descriptor_rejects_tampered_profile_and_foreign_controller(seat, tmp_path):
    controller, verification, request, command, _, _ = seat
    boundary = _boundary(seat)
    boundary.profile_path.chmod(0o600)
    boundary.profile_path.write_text("(version 1) (allow default)")
    with pytest.raises(fanout.ProviderRequestError, match="boundary"):
        fanout.validate_native_boundary(boundary, request, command, controller, verification)
    foreign = fanout.create_lifecycle_controller(tmp_path / "foreign")
    with pytest.raises(fanout.ProviderRequestError, match="boundary"):
        fanout.validate_native_boundary(boundary, request, command, foreign, verification)


def test_native_command_rejects_credential_environment(seat):
    controller, verification, request, command, _, _ = seat
    boundary = _boundary(seat)
    credential_command = replace(command, environment={
        "GOOGLE_APPLICATION_CREDENTIALS": str(request.cwd / "fake-adc.json"),
    })
    with pytest.raises(fanout.ProviderRequestError, match="boundary"):
        fanout.wrap_native_command(
            credential_command, boundary, request=request,
            controller=controller, verification=verification,
        )


def test_process_revalidates_profile_immediately_before_spawn(seat, monkeypatch):
    controller, verification, request, command, _, _ = seat
    boundary = _boundary(seat)
    wrapped = fanout.wrap_native_command(
        command, boundary, request=request, controller=controller,
        verification=verification,
    )
    boundary.profile_path.chmod(0o600)
    boundary.profile_path.write_text("(version 1) (allow default)")
    process = sys.modules[f"{SPEC.name}.process"]
    real_popen = process.subprocess.Popen
    def guarded_popen(argv, **kwargs):
        if argv[0] == "/usr/bin/sandbox-exec":
            pytest.fail("spawned after native profile changed")
        return real_popen(argv, **kwargs)
    monkeypatch.setattr(process.subprocess, "Popen", guarded_popen)
    with pytest.raises(fanout.ProviderRequestError, match="boundary"):
        fanout.run_command(wrapped)


def test_process_rejects_changed_wrapped_stdin_before_spawn(seat, monkeypatch):
    controller, verification, request, command, _, _ = seat
    boundary = _boundary(seat)
    wrapped = fanout.wrap_native_command(
        command, boundary, request=request, controller=controller,
        verification=verification,
    )
    process = sys.modules[f"{SPEC.name}.process"]
    real_popen = process.subprocess.Popen
    def guarded_popen(argv, **kwargs):
        if argv[0] == "/usr/bin/sandbox-exec":
            pytest.fail("spawned with changed command")
        return real_popen(argv, **kwargs)
    monkeypatch.setattr(process.subprocess, "Popen", guarded_popen)
    with pytest.raises(fanout.ProcessValidationError, match="wrapper changed"):
        fanout.run_command(replace(wrapped, stdin=b"different private prompt"))


def test_public_provider_launch_stays_closed_with_a_valid_descriptor(seat, monkeypatch):
    controller, verification, request, _, _, _ = seat
    boundary = _boundary(seat)
    bound = replace(request, native_boundary=boundary, native_controller=controller,
                    native_verification=verification)
    providers = sys.modules[f"{SPEC.name}.providers"]
    monkeypatch.setattr(fanout.CodexAdapter, "build_command", lambda *_args:
                        pytest.fail("adapter ran before live route gate"))
    monkeypatch.setattr(providers, "_installed_cli_version", lambda _executor:
                        pytest.fail("version probe preceded live route gate"))
    monkeypatch.setattr(providers, "run_command", lambda _command:
                        pytest.fail("provider spent before live route certificate"))
    with pytest.raises(fanout.ProviderRequestError, match="live-certified"):
        fanout.run_provider(bound)
    with pytest.raises(fanout.ProviderRequestError, match="live-certified"):
        fanout.run_many((bound,), max_workers=1)


def test_v2_read_only_provider_request_needs_native_boundary_before_spend(seat, monkeypatch):
    _, _, request, _, _, _ = seat
    read_only = replace(
        request, execution_class="read-only",
        profile=fanout.ProviderRegistry.default().select("codex", "read-only", "standard"),
    )
    providers = sys.modules[f"{SPEC.name}.providers"]
    monkeypatch.setattr(providers, "_installed_cli_version", lambda _executor:
                        pytest.fail("v2 version probe preceded native boundary gate"))
    with pytest.raises(fanout.ProviderRequestError, match="native seat boundary"):
        fanout.run_provider(read_only)
    with pytest.raises(fanout.ProviderRequestError, match="native seat boundary"):
        fanout.run_many((read_only,), max_workers=1)


def test_native_probe_receipt_records_observed_denials_and_workspace_write(seat):
    controller, verification, request, command, other, owner = seat
    boundary = _boundary(seat)
    request = replace(request, artifact_store=fanout.ArtifactStore(
        controller.root / "probe-artifacts"), artifact_prefix="native-probe")
    oracles = {"other": other / "secret.txt", "owner": owner / "run.json",
               "profile": boundary.profile_path}
    receipt = fanout.run_native_boundary_probe(
        boundary, request, command, controller=controller,
        verification=verification, oracle_paths=oracles,
    )
    assert receipt.exit_code == 0
    assert receipt.denied_paths == tuple(str(path) for path in oracles.values())
    assert receipt.allowed_workspace_write is True
    assert receipt.credential_blind is False
    assert request.artifact_store.read_bytes(receipt.transcript)


def test_native_probe_rejects_reachable_oracle_without_a_receipt(seat):
    controller, verification, request, command, _, _ = seat
    boundary = _boundary(seat)
    request = replace(request, artifact_store=fanout.ArtifactStore(
        controller.root / "probe-artifacts"), artifact_prefix="native-probe")
    with pytest.raises(fanout.ProviderRequestError, match="oracle"):
        fanout.run_native_boundary_probe(
            boundary, request, command, controller=controller,
            verification=verification,
            oracle_paths={"seat-file": request.cwd / "fixture.txt"},
        )
    assert not tuple((controller.root / "probe-artifacts").rglob("native-probe-*.json"))


def test_native_probe_fail_open_policy_preserves_protected_bytes(seat, monkeypatch):
    controller, verification, request, command, other, _ = seat
    module = sys.modules[f"{SPEC.name}.native_boundary"]
    monkeypatch.setattr(module, "_policy", lambda *_args:
                        b"(version 1) (allow default)\n")
    boundary = _boundary(seat)
    request = replace(request, artifact_store=fanout.ArtifactStore(
        controller.root / "probe-artifacts"), artifact_prefix="native-probe")
    before = (other / "secret.txt").read_bytes()
    with pytest.raises(fanout.ProviderRequestError, match="oracle"):
        fanout.run_native_boundary_probe(
            boundary, request, command, controller=controller,
            verification=verification,
            oracle_paths={"other": other / "secret.txt"},
        )
    assert (other / "secret.txt").read_bytes() == before
    assert not tuple((controller.root / "probe-artifacts").rglob("native-probe-*.json"))


def test_real_native_denial_and_workspace_write(seat):
    if sys.platform != "darwin":
        pytest.skip("macOS Seatbelt required")
    controller, verification, request, _, other, owner = seat
    boundary = _boundary(seat)
    probe = 'if [ "$1" = read ]; then IFS= read -r x < "$2"; else printf probe > "$2"; fi'
    for operation, path in (
        ("read", other / "secret.txt"),
        ("write", other / "secret.txt"),
        ("read", controller.root / "workspaces/seats/sibling/secret.txt"),
        ("write", controller.root / "workspaces/seats/sibling/secret.txt"),
        ("read", owner / "run.json"),
        ("read", controller.root / "controller.json"),
        ("read", boundary.profile_path),
        ("write", boundary.profile_path),
        ("read", Path("/tmp") / (other / "secret.txt").relative_to("/private/tmp")),
        ("read", Path("/tmp") / boundary.profile_path.relative_to("/private/tmp")),
    ):
        command = replace(seat[3], argv=("/bin/sh", "-c", probe, "sh",
                                         operation, str(path)))
        exact = _boundary(seat, command)
        wrapped = fanout.wrap_native_command(
            command, exact, request=request, controller=controller,
            verification=verification,
        )
        result = fanout.run_command(wrapped)
        if result.returncode == 71 and b"sandbox_apply: Operation not permitted" in result.stderr:
            pytest.skip("enclosing sandbox denied Seatbelt application; host probe required")
        assert result.returncode == 1, (operation, path, result)
        assert b"Operation not permitted" in result.stderr
    allowed = request.cwd / "allowed.txt"
    command = replace(seat[3], argv=("/bin/sh", "-c", probe, "sh",
                                     "write", str(allowed)))
    result = fanout.run_command(fanout.wrap_native_command(
        command, _boundary(seat, command), request=request, controller=controller,
        verification=verification,
    ))
    assert result.returncode == 0, result.stderr
    assert allowed.read_text() == "probe"
    listing = replace(seat[3], argv=(
        "/bin/sh", "-c", 'root=$1; set -- "$root"/*; [ "$1" = "$root/*" ]',
        "sh", str(controller.root),
    ))
    result = fanout.run_command(fanout.wrap_native_command(
        listing, _boundary(seat, listing), request=request, controller=controller,
        verification=verification,
    ))
    assert result.returncode == 0, "seat listed controller authority directory"
    child = replace(seat[3], argv=(
        "/bin/sh", "-c", '/bin/sh -c \'IFS= read -r x < "$1"\' sh "$1"',
        "sh", str(other / "secret.txt"),
    ))
    result = fanout.run_command(fanout.wrap_native_command(
        child, _boundary(seat, child), request=request, controller=controller,
        verification=verification,
    ))
    assert result.returncode == 1, result.stderr
    assert b"Operation not permitted" in result.stderr
