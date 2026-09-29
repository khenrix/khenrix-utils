"""Run-owned native guard for agy read-only seats."""
from __future__ import annotations

import importlib.util
import json
import shlex
import stat
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
FANOUT_ROOT = ROOT / "shared" / "lib" / "fanout"
SPEC = importlib.util.spec_from_file_location(
    "_fanout_agy_guard_contracts", FANOUT_ROOT / "__init__.py",
    submodule_search_locations=[str(FANOUT_ROOT)],
)
assert SPEC and SPEC.loader
fanout = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fanout
SPEC.loader.exec_module(fanout)


def _verified_workspace(tmp_path: Path, *, customization_root: str | None = None):
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(("git", "init", "-q", str(repository)), check=True)
    (repository / "fixture.txt").write_text("public fixture\n")
    if customization_root is not None:
        plugin = repository / customization_root / "plugins.json"
        plugin.parent.mkdir()
        plugin.write_text('{"entries":[]}\n')
    subprocess.run(("git", "-C", str(repository), "add", "-A"), check=True)
    subprocess.run((
        "git", "-C", str(repository), "-c", "user.name=Test",
        "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture",
    ), check=True)
    baseline = fanout.capture_repository_baseline(repository)
    controller = fanout.create_lifecycle_controller(tmp_path / "controller")
    workspace = fanout.create_seat_workspace(
        baseline, controller.root / "workspaces", "agy-seat",
    )
    verification = fanout.verify_seat_workspace(
        baseline, workspace, controller=controller,
    )
    return controller, verification


def _guard_test_environment(tmp_path: Path, monkeypatch, *, version: str = "1.2.12") -> None:
    home = tmp_path / "auth-home"
    adc = home / ".config/gcloud/application_default_credentials.json"
    adc.parent.mkdir(parents=True)
    adc.write_text("synthetic test credential")
    adc.chmod(0o600)
    binary = tmp_path / "agy-bin"
    binary.write_text(f"#!/bin/sh\nprintf '%s\\n' '{version}'\n")
    binary.chmod(0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGY_ADC_AUTH", "true")
    guard_module = sys.modules[f"{SPEC.name}.agy_guard"]
    monkeypatch.setattr(guard_module, "_BINARY", binary)


def test_read_only_agy_profile_never_auto_approves_tools():
    """Restoring --dangerously-skip-permissions would bypass the native deny rules."""
    registry = fanout.ProviderRegistry.default()

    for tier in ("standard", "deep"):
        profile = registry.select("agy", "read-only", tier)
        assert "--dangerously-skip-permissions" not in profile.initial_argv
        assert "--dangerously-skip-permissions" not in profile.resume_argv
    assert "--dangerously-skip-permissions" in registry.select(
        "agy", "repo-write", "standard",
    ).initial_argv


def test_guarded_agy_read_only_profiles_are_live_characterized():
    """Both guarded tiers passed initial/resume read and write-denial canaries."""
    registry = fanout.ProviderRegistry.default()

    assert registry.admit("agy", "read-only", "standard").characterized
    assert registry.admit("agy", "read-only", "deep").characterized


def test_guard_receipt_binding_survives_auth_and_binary_drift_for_status(
    tmp_path, monkeypatch,
):
    """No-spend status authenticates the original guard binding, not current auth."""
    controller, verification = _verified_workspace(tmp_path)
    _guard_test_environment(tmp_path, monkeypatch)
    profile = fanout.ProviderRegistry.default().select("agy", "read-only", "standard")
    guard = fanout.issue_agy_readonly_guard(controller, verification, profile)

    monkeypatch.delenv("AGY_ADC_AUTH")
    guard.binary.write_text("#!/bin/sh\nprintf '%s\\n' '1.2.13'\n")

    assert fanout.agy_readonly_guard_receipt_sha256(
        controller, verification, profile,
    ) == guard.receipt_sha256
    with pytest.raises(fanout.ProviderRequestError, match="validation failed"):
        fanout.validate_agy_readonly_guard(
            guard, verification.workspace.root, profile,
        )


@pytest.mark.parametrize("root_name", (".agents", ".agent", "_agents", "_agent"))
def test_guard_issue_rejects_documented_project_customizations(tmp_path, monkeypatch, root_name):
    """Tracked workspace plugins can execute before the private HOME hook is active."""
    controller, verification = _verified_workspace(
        tmp_path, customization_root=root_name,
    )
    _guard_test_environment(tmp_path, monkeypatch)
    profile = fanout.ProviderRegistry.default().select("agy", "read-only", "standard")

    with pytest.raises(fanout.ProviderRequestError, match="guard"):
        fanout.issue_agy_readonly_guard(controller, verification, profile)


def test_guard_issue_rejects_symlinked_project_customization(tmp_path, monkeypatch):
    """A symlinked customization root is still discovered and must fail closed."""
    controller, verification = _verified_workspace(tmp_path)
    _guard_test_environment(tmp_path, monkeypatch)
    outside = tmp_path / "outside-customization"
    outside.mkdir()
    (verification.workspace.root / ".agents").symlink_to(outside)
    profile = fanout.ProviderRegistry.default().select("agy", "read-only", "standard")

    with pytest.raises(fanout.ProviderRequestError, match="guard"):
        fanout.issue_agy_readonly_guard(controller, verification, profile)


@pytest.mark.parametrize("root_name", (".agents", ".agent", "_agents", "_agent"))
@pytest.mark.parametrize("resume", (False, True))
def test_inserted_customization_blocks_guarded_turn_before_model_spend(
    tmp_path, monkeypatch, root_name, resume,
):
    """Repeated validation catches a plugin inserted after the receipt was issued."""
    controller, verification = _verified_workspace(tmp_path)
    _guard_test_environment(tmp_path, monkeypatch)
    default = fanout.ProviderRegistry.default()
    candidate = replace(
        default.select("agy", "read-only", "standard"),
        characterized=True, profile_sha256=None,
    )
    registry = fanout.ProviderRegistry(
        (candidate,), version_probe=lambda _executor: "1.2.12",
    )
    guard = fanout.issue_agy_readonly_guard(controller, verification, candidate)
    request = fanout.ProviderRequest(
        "agy", "read fixture", cwd=verification.workspace.root,
        execution_class="read-only", profile=candidate, agy_guard=guard,
        session_id="exact-conversation" if resume else None, resume=resume,
    )
    inserted = verification.workspace.root / root_name
    inserted.mkdir()
    (inserted / "plugins.json").write_text('{"entries":[]}\n')
    providers = sys.modules[f"{SPEC.name}.providers"]
    launched = []
    monkeypatch.setattr(providers, "run_command", lambda command: (
        launched.append(command) or fanout.ProcessResult(
            status=fanout.ProcessStatus.SPAWN_ERROR, returncode=None,
            stdout=b"", stderr=b"", error="synthetic unstarted child",
        )
    ))

    with pytest.raises(fanout.ProviderRequestError, match="guard"):
        fanout.run_provider(request, registry=registry)
    assert launched == []


def test_guard_rejects_symlink_alias_for_provider_cwd(tmp_path, monkeypatch):
    """A cwd alias must not widen agy's upward customization discovery path."""
    controller, verification = _verified_workspace(tmp_path)
    _guard_test_environment(tmp_path, monkeypatch)
    profile = fanout.ProviderRegistry.default().select("agy", "read-only", "standard")
    guard = fanout.issue_agy_readonly_guard(controller, verification, profile)
    alias = tmp_path / "aliased-workspace"
    alias.symlink_to(verification.workspace.root)
    request = fanout.ProviderRequest(
        "agy", "read fixture", cwd=alias, profile=profile, agy_guard=guard,
    )

    with pytest.raises(fanout.ProviderRequestError, match="guard"):
        fanout.ProviderRegistry.default().require("agy").build_command(request)


def test_unstarted_retry_rechecks_workspace_customizations(tmp_path, monkeypatch):
    """A plugin inserted after an unstarted spawn may not reach the retry child."""
    controller, verification = _verified_workspace(tmp_path)
    _guard_test_environment(tmp_path, monkeypatch)
    default = fanout.ProviderRegistry.default()
    candidate = replace(
        default.select("agy", "read-only", "standard"),
        characterized=True, profile_sha256=None,
    )
    registry = fanout.ProviderRegistry(
        (candidate,), version_probe=lambda _executor: "1.2.12",
    )
    guard = fanout.issue_agy_readonly_guard(controller, verification, candidate)
    calls = []

    def unstarted(command):
        calls.append(command)
        (verification.workspace.root / ".agents").mkdir()
        return fanout.ProcessResult(
            status=fanout.ProcessStatus.SPAWN_ERROR, returncode=None,
            stdout=b"", stderr=b"", error="synthetic unstarted child",
        )

    providers = sys.modules[f"{SPEC.name}.providers"]
    monkeypatch.setattr(providers, "run_command", unstarted)
    request = fanout.ProviderRequest(
        "agy", "read fixture", cwd=verification.workspace.root,
        profile=candidate, agy_guard=guard, retries=1,
    )

    with pytest.raises(fanout.ProviderRequestError, match="guard"):
        fanout.run_provider(request, registry=registry)
    assert len(calls) == 1


def test_read_only_agy_rejects_a_missing_guard_before_process_start(tmp_path):
    """An unguarded request must not fall back to instruction-only plan mode."""
    registry = fanout.ProviderRegistry.default()
    request = fanout.ProviderRequest(
        "agy", "read fixture", cwd=tmp_path,
        profile=registry.select("agy", "read-only", "standard"),
    )

    with pytest.raises(fanout.ProviderRequestError, match="guard"):
        registry.require("agy").build_command(request)


def test_guard_issues_private_home_and_exact_read_only_command(tmp_path, monkeypatch):
    """A seat launch must use the controller-owned policy, not ambient agy config."""
    controller, verification = _verified_workspace(tmp_path)
    registry = fanout.ProviderRegistry.default()
    profile = registry.select("agy", "read-only", "standard")
    _guard_test_environment(tmp_path, monkeypatch)
    before = fanout.capture_repository_baseline(verification.workspace.root)
    guard = fanout.issue_agy_readonly_guard(
        controller, verification, profile,
    )
    request = fanout.ProviderRequest(
        "agy", "read fixture", cwd=verification.workspace.root,
        profile=profile, agy_guard=guard,
    )

    command = registry.require("agy").build_command(request)

    assert command.argv[0] == str(guard.binary)
    assert command.argv[1] == "--new-project"
    assert "--dangerously-skip-permissions" not in command.argv
    assert command.environment["HOME"] == str(guard.home)
    assert command.environment["XDG_CONFIG_HOME"] == str(guard.home / ".config")
    assert command.environment["GOOGLE_CLOUD_LOCATION"] == "eu"
    assert command.environment["GOOGLE_CLOUD_REGION"] == "eu"
    assert command.environment["AGY_ADC_AUTH"] == "true"
    assert command.environment["GOOGLE_APPLICATION_CREDENTIALS"].endswith(
        "/.config/gcloud/application_default_credentials.json"
    )
    assert (guard.home / ".gemini/config/hooks.json").is_file()
    assert (guard.home / ".gemini/antigravity-cli/settings.json").is_file()
    native_settings = guard.home / ".gemini/antigravity-cli/settings.json"
    assert stat.S_IMODE(native_settings.stat().st_mode) == 0o600
    assert native_settings.read_text() == (
        json.dumps({"permissions": {
            "allow": [f"read_file({verification.workspace.root})"],
            "deny": ["write_file(*)", "command(*)", "unsandboxed(*)", "mcp(*)", "execute_url(*)"],
        }}, indent=2) + "\n"
    )
    assert (guard.home / "fanout_guard.py").is_file()
    assert not (verification.workspace.root / ".gemini").exists()
    assert fanout.capture_repository_baseline(verification.workspace.root).digest == before.digest
    resumed = replace(request, session_id="exact-conversation", resume=True)
    next_command = registry.require("agy").build_command(resumed)
    assert next_command.environment["HOME"] == command.environment["HOME"]
    assert next_command.argv[0] == command.argv[0]
    assert next_command.argv[1:3] == ("--conversation", "exact-conversation")
    assert "--new-project" not in next_command.argv


def test_guard_cold_reopen_reuses_authenticated_hook_from_original_interpreter(
    tmp_path, monkeypatch,
):
    controller, verification = _verified_workspace(tmp_path)
    _guard_test_environment(tmp_path, monkeypatch)
    profile = fanout.ProviderRegistry.default().select("agy", "read-only", "standard")
    issued = fanout.issue_agy_readonly_guard(controller, verification, profile)
    hooks = issued.home / ".gemini/config/hooks.json"
    original_hook = hooks.read_bytes()
    command = json.loads(original_hook)["fanout-readonly-guard"]["PreToolUse"][0]["hooks"][0]["command"]
    assert shlex.split(command) == [sys.executable, str(issued.home / "fanout_guard.py")]
    guard_module = sys.modules[f"{SPEC.name}.agy_guard"]
    monkeypatch.setattr(guard_module, "sys", SimpleNamespace(executable="/opt/alternate/bin/python3"))
    resumed_controller = fanout.resume_lifecycle_controller(controller.root, controller.capability)
    resumed_verification = fanout.resume_seat_workspace(
        verification.workspace, controller=resumed_controller,
        evidence_digest=verification.evidence_digest,
    )

    reopened = fanout.issue_agy_readonly_guard(
        resumed_controller, resumed_verification, profile,
    )

    assert reopened.receipt_name == issued.receipt_name
    assert reopened.receipt_sha256 == issued.receipt_sha256
    assert hooks.read_bytes() == original_hook
    fanout.validate_agy_readonly_guard(reopened, verification.workspace.root, profile)


def test_guard_cold_reopen_refuses_to_sign_changed_hook_bytes(tmp_path, monkeypatch):
    controller, verification = _verified_workspace(tmp_path)
    _guard_test_environment(tmp_path, monkeypatch)
    profile = fanout.ProviderRegistry.default().select("agy", "read-only", "standard")
    issued = fanout.issue_agy_readonly_guard(controller, verification, profile)
    receipts = controller.root / "receipts"
    original_receipts = {path.name for path in receipts.iterdir()}
    alternate_python = "/opt/alternate/bin/python3"
    hooks = issued.home / ".gemini/config/hooks.json"
    altered = json.loads(hooks.read_bytes())
    altered["fanout-readonly-guard"]["PreToolUse"][0]["hooks"][0]["command"] = (
        f"{shlex.quote(alternate_python)} {shlex.quote(str(issued.home / 'fanout_guard.py'))}"
    )
    hooks.chmod(0o600)
    hooks.write_bytes(fanout.canonical_json(altered))
    hooks.chmod(0o400)
    guard_module = sys.modules[f"{SPEC.name}.agy_guard"]
    monkeypatch.setattr(guard_module, "sys", SimpleNamespace(executable=alternate_python))

    with pytest.raises(fanout.ProviderRequestError, match="guard"):
        fanout.issue_agy_readonly_guard(controller, verification, profile)

    assert {path.name for path in receipts.iterdir()} == original_receipts


def test_guard_issue_refuses_to_sign_changed_hook_source(tmp_path, monkeypatch):
    controller, verification = _verified_workspace(tmp_path)
    _guard_test_environment(tmp_path, monkeypatch)
    profile = fanout.ProviderRegistry.default().select("agy", "read-only", "standard")
    receipts = controller.root / "receipts"
    original_receipts = {path.name for path in receipts.iterdir()}
    changed_source = tmp_path / "changed-hook.py"
    changed_source.write_text("print('unguarded')\n")
    guard_module = sys.modules[f"{SPEC.name}.agy_guard"]
    monkeypatch.setattr(guard_module, "_HOOK_SOURCE", changed_source)

    with pytest.raises(fanout.ProviderRequestError, match="guard"):
        fanout.issue_agy_readonly_guard(controller, verification, profile)

    assert {path.name for path in receipts.iterdir()} == original_receipts


def test_guard_cold_reopen_refuses_missing_home_even_with_signed_receipt(
    tmp_path, monkeypatch,
):
    controller, verification = _verified_workspace(tmp_path)
    _guard_test_environment(tmp_path, monkeypatch)
    profile = fanout.ProviderRegistry.default().select("agy", "read-only", "standard")
    issued = fanout.issue_agy_readonly_guard(controller, verification, profile)
    receipts = controller.root / "receipts"
    original_receipts = {path.name for path in receipts.iterdir()}
    issued.home.rename(issued.home.with_name(f"{issued.home.name}-moved"))
    guard_module = sys.modules[f"{SPEC.name}.agy_guard"]
    monkeypatch.setattr(guard_module, "sys", SimpleNamespace(executable="/opt/alternate/bin/python3"))

    with pytest.raises(fanout.ProviderRequestError, match="guard"):
        fanout.issue_agy_readonly_guard(controller, verification, profile)

    assert {path.name for path in receipts.iterdir()} == original_receipts
    assert not issued.home.exists()


def test_guard_rejects_policy_tampering_before_resume(tmp_path, monkeypatch):
    """An edited hook must not be used for a later turn of the same session."""
    controller, verification = _verified_workspace(tmp_path)
    registry = fanout.ProviderRegistry.default()
    profile = registry.select("agy", "read-only", "standard")
    _guard_test_environment(tmp_path, monkeypatch)
    guard = fanout.issue_agy_readonly_guard(controller, verification, profile)
    request = fanout.ProviderRequest(
        "agy", "continue", cwd=verification.workspace.root, profile=profile,
        session_id="agy-session-1", resume=True, agy_guard=guard,
    )
    hooks = guard.home / ".gemini/config/hooks.json"
    hooks.chmod(0o600)
    hooks.write_text(json.dumps({"disabled": True}))

    with pytest.raises(fanout.ProviderRequestError, match="guard"):
        registry.require("agy").build_command(request)


def test_seat_assignment_preserves_the_controller_guard_receipt(tmp_path, monkeypatch):
    """Dropping the guard while handing a staged seat to dispatch would bypass preflight."""
    controller, verification = _verified_workspace(tmp_path)
    profile = fanout.ProviderRegistry.default().select("agy", "read-only", "standard")
    _guard_test_environment(tmp_path, monkeypatch)
    guard = fanout.issue_agy_readonly_guard(controller, verification, profile)
    source = tmp_path / "skills"
    source.mkdir()
    resolver = fanout.SkillResolver((fanout.SkillRoot("test", source, 0),))
    admission = resolver.admit(
        (), task_id="task", seat_id="agy-seat", provider="agy", session_id="session-agy",
    )
    admission = resolver.stage(admission, tmp_path / "staged")
    admission = resolver.verify_engine_delivery(admission, ())
    bundle = fanout.canonical_json({"schema_version": "fanout-seat-skill-bundle-v1", "skills": []})

    seat = fanout.SeatAssignment.from_admission(
        admission, skill_bundle=bundle, artifacts=fanout.ArtifactStore(tmp_path / "artifacts"),
        workspace_verification=verification, agy_guard=guard,
    )

    assert seat.agy_guard is guard
    assert seat.agy_guard.receipt_sha256 == guard.receipt_sha256


def test_guard_rejects_wrong_direct_binary_version(tmp_path, monkeypatch):
    """The wrapper's pin alone cannot authorize a different licensed binary."""
    controller, verification = _verified_workspace(tmp_path)
    _guard_test_environment(tmp_path, monkeypatch, version="1.2.9")
    profile = fanout.ProviderRegistry.default().select("agy", "read-only", "standard")

    with pytest.raises(fanout.ProviderRequestError, match="guard"):
        fanout.issue_agy_readonly_guard(controller, verification, profile)


def test_guard_hook_confines_reads_and_denies_tool_writes(tmp_path, monkeypatch):
    """A path traversal, symlink, or write tool must receive an explicit deny."""
    controller, verification = _verified_workspace(tmp_path)
    _guard_test_environment(tmp_path, monkeypatch)
    profile = fanout.ProviderRegistry.default().select("agy", "read-only", "standard")
    guard = fanout.issue_agy_readonly_guard(controller, verification, profile)
    workspace = verification.workspace.root
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n")
    (workspace / "escape").symlink_to(outside)

    def decision(name: str, args: dict[str, object]) -> str:
        payload = {
            "workspacePaths": [str(workspace)],
            "toolCall": {"name": name, "args": args},
        }
        result = subprocess.run(
            (sys.executable, str(guard.home / "fanout_guard.py")),
            input=json.dumps(payload).encode(), capture_output=True, check=True,
        )
        return json.loads(result.stdout)["decision"]

    assert decision("view_file", {"AbsolutePath": str(workspace / "fixture.txt")}) == "allow"
    assert decision("view_file", {"AbsolutePath": "fixture.txt"}) == "allow"
    assert decision("find_by_name", {"SearchDirectory": ".", "Pattern": "*"}) == "allow"
    assert decision("list_dir", {"DirectoryPath": "."}) == "allow"
    assert decision("grep_search", {"SearchPath": "fixture.txt"}) == "allow"
    assert decision("find_by_name", {"SearchDirectory": str(workspace), "Pattern": "*"}) == "allow"
    assert decision("view_file", {"AbsolutePath": str(outside)}) == "deny"
    assert decision("view_file", {"AbsolutePath": "../outside.txt"}) == "deny"
    assert decision("view_file", {"AbsolutePath": "escape"}) == "deny"
    assert decision("view_file", {"AbsolutePath": "missing.txt"}) == "deny"
    assert decision("view_file", {"AbsolutePath": str(workspace / "../outside.txt")}) == "deny"
    assert decision("view_file", {"AbsolutePath": str(workspace / "escape")}) == "deny"
    assert decision("view_file", {"AbsolutePath": str(guard.home / "fanout_guard.py")}) == "deny"
    assert decision("write_to_file", {"TargetFile": str(workspace / "canary.txt")}) == "deny"
    assert decision("write_to_file", {"TargetFile": "canary.txt"}) == "deny"
    assert decision("run_command", {"CommandLine": "touch canary.txt"}) == "deny"
    assert decision("read_url_content", {"Url": "file:///fixture.txt"}) == "deny"


def test_guarded_agy_child_receives_only_private_auth_paths_and_eu_route(tmp_path, monkeypatch):
    """The isolated HOME must not reintroduce ambient credentials or memory tokens."""
    controller, verification = _verified_workspace(tmp_path)
    _guard_test_environment(tmp_path, monkeypatch)
    registry = fanout.ProviderRegistry.default()
    profile = registry.select("agy", "read-only", "standard")
    guard = fanout.issue_agy_readonly_guard(controller, verification, profile)
    request = fanout.ProviderRequest(
        "agy", "read fixture", cwd=verification.workspace.root,
        profile=profile, agy_guard=guard,
    )
    command = registry.require("agy").build_command(request)
    probe = (
        "import json, os; "
        "keys=('HOME','XDG_CONFIG_HOME','GOOGLE_APPLICATION_CREDENTIALS',"
        "'AGY_ADC_AUTH','GOOGLE_CLOUD_LOCATION','GOOGLE_CLOUD_REGION',"
        "'GEMINI_API_KEY','CLAUDE_MEM_TOKEN','GOOGLE_CLOUD_QUOTA_PROJECT'); "
        "print(json.dumps({k:os.environ[k] for k in keys if k in os.environ}))"
    )
    child = replace(command, argv=(sys.executable, "-c", probe), stdin=b"",
                    slot_root=tmp_path / "slots")
    ambient = {
        "PATH": "/usr/bin:/bin", "GEMINI_API_KEY": "ambient-sentinel",
        "CLAUDE_MEM_TOKEN": "ambient-memory", "GOOGLE_CLOUD_QUOTA_PROJECT": "other-project",
    }

    result = fanout.run_command(child, base_environment=ambient)

    assert result.status is fanout.ProcessStatus.EXIT
    assert result.returncode == 0
    assert json.loads(result.stdout) == dict(guard.environment)
