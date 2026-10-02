"""The legacy read-only agy seat must be guarded before a council spends a call."""
from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from test_council_characterization import import_fanout


def _modules():
    engine = import_fanout()
    from council import agy_guard
    return engine, agy_guard


def _configured_guard(tmp_path, monkeypatch):
    engine, guard_module = _modules()
    auth_home = tmp_path / "auth-home"
    adc = auth_home / ".config/gcloud/application_default_credentials.json"
    adc.parent.mkdir(parents=True)
    adc.write_text("synthetic credential")
    adc.chmod(0o600)
    binary = auth_home / ".local/libexec/agy-bin"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\nprintf '1.2.11\\n'\n")
    binary.chmod(0o700)
    monkeypatch.setenv("HOME", str(auth_home))
    monkeypatch.setenv("AGY_ADC_AUTH", "true")
    monkeypatch.setattr(guard_module, "_BINARY", binary)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "input.txt").write_text("readable input\n")
    return engine, guard_module, workspace, adc, binary


def _decision(hook: Path, workspace: Path, name: str, args: dict) -> str:
    payload = {
        "workspacePaths": [str(workspace)],
        "toolCall": {"name": name, "args": args},
    }
    result = subprocess.run(
        (sys.executable, str(hook)), input=json.dumps(payload).encode(),
        capture_output=True, check=True,
    )
    return json.loads(result.stdout)["decision"]


def test_private_guard_preserves_workspace_reads_and_denies_writes(tmp_path, monkeypatch):
    """Removing the read allowance or letting a write tool through breaks this boundary."""
    _engine, guard_module, workspace, adc, binary = _configured_guard(tmp_path, monkeypatch)
    guard = guard_module.issue(tmp_path / "run", workspace, {"PATH": os.defpath})
    try:
        assert guard.home.stat().st_mode & 0o777 == 0o700
        assert guard.environment["HOME"] == str(guard.home)
        assert guard.environment["GOOGLE_APPLICATION_CREDENTIALS"] == str(adc)
        assert guard.environment["GOOGLE_CLOUD_LOCATION"] == "eu"
        assert guard.environment["GOOGLE_CLOUD_REGION"] == "eu"
        assert guard.binary == binary
        native_settings = guard.home / ".gemini/antigravity-cli/settings.json"
        assert native_settings.stat().st_mode & 0o777 == 0o600
        assert native_settings.read_text() == (
            json.dumps({"permissions": {
                "allow": [f"read_file({workspace})"],
                "deny": ["write_file(*)", "command(*)", "unsandboxed(*)", "mcp(*)", "execute_url(*)"],
            }}, indent=2) + "\n"
        )
        settings = json.loads(native_settings.read_text())
        assert settings["permissions"]["allow"] == [f"read_file({workspace})"]
        assert settings["permissions"]["deny"] == [
            "write_file(*)", "command(*)", "unsandboxed(*)", "mcp(*)", "execute_url(*)",
        ]
        hook = guard.home / "agy_readonly_hook.py"
        assert _decision(hook, workspace, "view_file", {"AbsolutePath": str(workspace / "input.txt")}) == "allow"
        assert _decision(hook, workspace, "view_file", {"AbsolutePath": "input.txt"}) == "allow"
        assert _decision(hook, workspace, "list_dir", {"DirectoryPath": "."}) == "allow"
        assert _decision(hook, workspace, "find_by_name", {"SearchDirectory": str(workspace), "Pattern": "*"}) == "allow"
        assert _decision(hook, workspace, "write_to_file", {"TargetFile": str(workspace / "bad.txt")}) == "deny"
        assert _decision(hook, workspace, "run_command", {"CommandLine": "touch bad.txt"}) == "deny"
    finally:
        guard_module.cleanup(guard)
    assert not guard.home.exists()


def test_guard_issue_refuses_changed_standalone_hook_source(tmp_path, monkeypatch):
    _engine, guard_module, workspace, _adc, _binary = _configured_guard(tmp_path, monkeypatch)
    changed_source = tmp_path / "changed-hook.py"
    changed_source.write_text("print('unguarded')\n")
    monkeypatch.setattr(guard_module, "_HOOK_SOURCE", changed_source)

    with pytest.raises(guard_module.AgyGuardError, match="guard"):
        guard_module.issue(tmp_path / "run", workspace, {})

    homes = tmp_path / "run" / "agy-guards"
    assert not homes.exists() or not list(homes.iterdir())


def test_native_settings_must_keep_stable_bytes_and_mode(tmp_path, monkeypatch):
    """agy normalizes settings; only the measured stable 0600 form may survive retry."""
    _engine, guard_module, workspace, _adc, _binary = _configured_guard(tmp_path, monkeypatch)
    guard = guard_module.issue(tmp_path / "run", workspace, {})
    native_settings = guard.home / ".gemini/antigravity-cli/settings.json"
    try:
        native_settings.chmod(0o400)
        with pytest.raises(guard_module.AgyGuardError):
            guard_module.validate(guard)
        native_settings.chmod(0o600)
        guard_module.validate(guard)
        native_settings.write_text(json.dumps({"permissions": {
            "allow": [f"read_file({workspace})"],
            "deny": ["write_file(*)", "command(*)", "unsandboxed(*)", "mcp(*)", "execute_url(*)"],
        }}, separators=(",", ":")) + "\n")
        with pytest.raises(guard_module.AgyGuardError):
            guard_module.validate(guard)
    finally:
        guard_module.cleanup(guard)


def test_guard_denies_outside_traversal_and_symlink_reads(tmp_path, monkeypatch):
    """A tool path cannot escape the workspace by spelling or symlink."""
    _engine, guard_module, workspace, _adc, _binary = _configured_guard(tmp_path, monkeypatch)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n")
    (workspace / "escape").symlink_to(outside)
    guard = guard_module.issue(tmp_path / "run", workspace, {})
    try:
        hook = guard.home / "agy_readonly_hook.py"
        for path in (outside, workspace / "../outside.txt", workspace / "escape",
                     "../outside.txt", "escape"):
            assert _decision(hook, workspace, "view_file", {"AbsolutePath": str(path)}) == "deny"
    finally:
        guard_module.cleanup(guard)


def test_guard_checks_adc_metadata_without_reading_credential_bytes(tmp_path, monkeypatch):
    """The isolated child gets a path to ADC, never its secret bytes in Council memory."""
    _engine, guard_module, workspace, adc, _binary = _configured_guard(tmp_path, monkeypatch)
    actual_read = os.read
    adc_identity = (adc.stat().st_dev, adc.stat().st_ino)

    def read_unless_adc(descriptor, size):
        info = os.fstat(descriptor)
        assert (info.st_dev, info.st_ino) != adc_identity, "Council read credential bytes"
        return actual_read(descriptor, size)

    monkeypatch.setattr(os, "read", read_unless_adc)
    guard = guard_module.issue(tmp_path / "run", workspace, {})
    guard_module.validate(guard)
    guard_module.cleanup(guard)


def test_guard_hashes_a_binary_larger_than_128_megabytes_in_chunks(tmp_path, monkeypatch):
    """The installed direct binary is 185 MB; a file-size gate cannot reject it."""
    _engine, guard_module, _workspace, _adc, _binary = _configured_guard(tmp_path, monkeypatch)
    large = tmp_path / "large-binary"
    with large.open("wb") as stream:
        stream.truncate(129 * 1024 * 1024)
    large.chmod(0o700)

    assert len(guard_module._binary_hash(large)) == 64


def test_guard_rejects_tampered_policy_and_home_symlink(tmp_path, monkeypatch):
    """A later attempt must not trust a changed policy or a replaced HOME."""
    _engine, guard_module, workspace, _adc, _binary = _configured_guard(tmp_path, monkeypatch)
    guard = guard_module.issue(tmp_path / "run", workspace, {})
    policy = guard.home / "policy.json"
    policy.chmod(0o600)
    policy.write_text('{"workspace":"/"}')
    with pytest.raises(guard_module.AgyGuardError):
        guard_module.validate(guard)
    guard_module.cleanup(guard)

    guard = guard_module.issue(tmp_path / "run", workspace, {})
    saved = guard.home.with_name("saved-home")
    guard.home.rename(saved)
    guard.home.symlink_to(saved)
    with pytest.raises(guard_module.AgyGuardError):
        guard_module.validate(guard)
    guard.home.unlink()
    saved.rename(guard.home)
    guard_module.cleanup(guard)


@pytest.mark.parametrize("name", [".agents", ".agent", "_agents", "_agent"])
def test_workspace_customization_stops_panel_before_spend(tmp_path, monkeypatch, name):
    """Project-discovered hooks can run before the private HOME policy takes effect."""
    engine, _guard_module, workspace, _adc, _binary = _configured_guard(tmp_path, monkeypatch)
    (workspace / name).mkdir()
    launches = []
    monkeypatch.setattr(engine, "run_member", lambda *a, **kw: launches.append(a))
    agy = engine.build_real_spec("agy", "read", 10, {}, tmp_path / "run")
    agy.cwd = str(workspace)
    engine.make_readonly(agy)
    claude = engine.ProviderSpec("claude", ["missing-claude"], None, engine.extract_raw)

    with pytest.raises(Exception, match="agy|guard"):
        engine.run_council([claude, agy], retries=0, timeout=10, backoff=0,
                           workdir=tmp_path / "run", read_only=True,
                           install_signal_handler=False)
    assert launches == []


def test_workspace_customization_is_rechecked_before_retry(tmp_path, monkeypatch):
    """A hook directory appearing during the first attempt blocks the second."""
    engine, _guard_module, workspace, _adc, _binary = _configured_guard(tmp_path, monkeypatch)
    launches = []

    def failed_attempt(argv, *, stdin, timeout, env, cwd):
        launches.append(argv)
        (workspace / ".agents").mkdir()
        return subprocess.CompletedProcess(argv, 1, "", "provider error")

    monkeypatch.setattr(engine, "run_member", failed_attempt)
    agy = engine.build_real_spec("agy", "read", 10, {}, tmp_path / "run")
    agy.cwd = str(workspace)
    engine.make_readonly(agy)

    with pytest.raises(Exception, match="agy|guard"):
        engine.run_council([agy], retries=1, timeout=10, backoff=0,
                           workdir=tmp_path / "run", read_only=True,
                           install_signal_handler=False)
    assert len(launches) == 1


def test_workspace_customization_in_repo_ancestor_is_rejected(tmp_path, monkeypatch):
    """agy searches from a subdirectory up through the repository root."""
    _engine, guard_module, repository, _adc, _binary = _configured_guard(tmp_path, monkeypatch)
    (repository / ".git").write_text("gitdir: elsewhere\n")
    (repository / "_agents").mkdir()
    workspace = repository / "src"
    workspace.mkdir()

    with pytest.raises(guard_module.AgyGuardError):
        guard_module.issue(tmp_path / "run", workspace, {})


@pytest.mark.parametrize("missing", ["auth_mode", "adc", "binary"])
def test_bad_auth_or_binary_stops_the_whole_panel_before_spend(tmp_path, monkeypatch, missing):
    """A guard failure must not let Claude start while agy is rejected."""
    engine, _guard_module, workspace, adc, binary = _configured_guard(tmp_path, monkeypatch)
    if missing == "auth_mode":
        monkeypatch.delenv("AGY_ADC_AUTH")
    elif missing == "adc":
        adc.unlink()
    else:
        binary.unlink()
    launches = []
    monkeypatch.setattr(engine, "run_member", lambda *a, **kw: launches.append(a))
    agy = engine.build_real_spec("agy", "read", 10, {}, tmp_path / "run")
    agy.cwd = str(workspace)
    engine.make_readonly(agy)
    claude = engine.ProviderSpec("claude", ["missing-claude"], None, engine.extract_raw)

    with pytest.raises(Exception, match="agy|guard"):
        engine.run_council([claude, agy], retries=0, timeout=10, backoff=0,
                           workdir=tmp_path / "run", read_only=True,
                           install_signal_handler=False)
    assert launches == []


def test_guard_is_rechecked_before_retry_and_home_is_cleaned(tmp_path, monkeypatch):
    """A policy changed after the first attempt cannot authorize a second attempt."""
    engine, _guard_module, workspace, _adc, _binary = _configured_guard(tmp_path, monkeypatch)
    launches = []

    def failed_attempt(argv, *, stdin, timeout, env, cwd):
        launches.append((argv, env))
        policy = Path(env["HOME"]) / "policy.json"
        policy.chmod(0o600)
        policy.write_text('{"workspace":"/"}')
        return subprocess.CompletedProcess(argv, 1, "", "provider error")

    monkeypatch.setattr(engine, "run_member", failed_attempt)
    agy = engine.build_real_spec("agy", "read", 10, {}, tmp_path / "run")
    agy.cwd = str(workspace)
    engine.make_readonly(agy)

    with pytest.raises(Exception, match="guard"):
        engine.run_council([agy], retries=1, timeout=10, backoff=0,
                           workdir=tmp_path / "run", read_only=True,
                           install_signal_handler=False)
    assert len(launches) == 1
    assert "--dangerously-skip-permissions" not in launches[0][0]
    assert not Path(launches[0][1]["HOME"]).exists()


def test_retry_rejects_tampering_with_full_issued_environment(tmp_path, monkeypatch):
    """Matching spec and guard mutations cannot redirect XDG or disable ADC on retry."""
    engine, _guard_module, workspace, _adc, _binary = _configured_guard(tmp_path, monkeypatch)
    launches = []
    spec = engine.build_real_spec("agy", "read", 10, {}, tmp_path / "run")
    spec.cwd = str(workspace)
    engine.make_readonly(spec)

    def failed_attempt(argv, *, stdin, timeout, env, cwd):
        launches.append(argv)
        spec.environment["XDG_CONFIG_HOME"] = str(tmp_path / "outside-config")
        spec.agy_guard.environment["XDG_CONFIG_HOME"] = str(tmp_path / "outside-config")
        spec.environment["AGY_ADC_AUTH"] = "false"
        spec.agy_guard.environment["AGY_ADC_AUTH"] = "false"
        return subprocess.CompletedProcess(argv, 1, "", "provider error")

    monkeypatch.setattr(engine, "run_member", failed_attempt)
    with pytest.raises(Exception, match="guard"):
        engine.run_council([spec], retries=1, timeout=10, backoff=0,
                           workdir=tmp_path / "run", read_only=True,
                           install_signal_handler=False)
    assert len(launches) == 1


@pytest.mark.parametrize("mutated", ["prompt", "model"])
def test_retry_rejects_prompt_or_model_mutation(tmp_path, monkeypatch, mutated):
    """A shape-preserving edit cannot change what the guarded seat is asked to do."""
    engine, _guard_module, workspace, _adc, _binary = _configured_guard(tmp_path, monkeypatch)
    launches = []
    spec = engine.build_real_spec("agy", "read this fixture", 10,
                                  {"agy": {"model": "Gemini 3.5 Flash (High)"}},
                                  tmp_path / "run")
    spec.cwd = str(workspace)
    engine.make_readonly(spec)

    def failed_attempt(argv, *, stdin, timeout, env, cwd):
        launches.append(list(argv))
        if mutated == "prompt":
            spec.argv[-1] = "write to the checkout"
        else:
            spec.argv[spec.argv.index("--model") + 1] = "Gemini 3.5 Pro (High)"
        return subprocess.CompletedProcess(argv, 1, "", "provider error")

    monkeypatch.setattr(engine, "run_member", failed_attempt)
    with pytest.raises(Exception, match="guard"):
        engine.run_council([spec], retries=1, timeout=10, backoff=0,
                           workdir=tmp_path / "run", read_only=True,
                           install_signal_handler=False)
    assert len(launches) == 1


@pytest.mark.parametrize("extra", [
    "--dangerously-skip-permissions=true", "--mode=accept-edits", "--continue",
    "-c", "--project=another-project", "--log-file=/tmp/outside.log",
])
def test_readonly_preflight_rejects_argv_bypass_aliases(tmp_path, monkeypatch, extra):
    """Only Council's known flags may precede the prompt in a guarded invocation."""
    engine, _guard_module, workspace, _adc, _binary = _configured_guard(tmp_path, monkeypatch)
    launches = []
    monkeypatch.setattr(engine, "run_member", lambda *a, **kw: launches.append(a))
    agy = engine.build_real_spec("agy", "read", 10, {}, tmp_path / "run")
    agy.cwd = str(workspace)
    engine.make_readonly(agy)
    agy.argv.insert(agy.argv.index("-p"), extra)

    with pytest.raises(Exception, match="guard"):
        engine.run_council([agy], retries=0, timeout=10, backoff=0,
                           workdir=tmp_path / "run", read_only=True,
                           install_signal_handler=False)
    assert launches == []


def test_readonly_preflight_rejects_redirected_log_file(tmp_path, monkeypatch):
    """agy's own log output must stay in the run directory, not an injected path."""
    engine, _guard_module, workspace, _adc, _binary = _configured_guard(tmp_path, monkeypatch)
    launches = []
    monkeypatch.setattr(engine, "run_member", lambda *a, **kw: launches.append(a))
    agy = engine.build_real_spec("agy", "read", 10, {}, tmp_path / "run")
    agy.cwd = str(workspace)
    engine.make_readonly(agy)
    agy.argv[agy.argv.index("--log-file") + 1] = str(tmp_path / "outside.log")

    with pytest.raises(Exception, match="guard"):
        engine.run_council([agy], retries=0, timeout=10, backoff=0,
                           workdir=tmp_path / "run", read_only=True,
                           install_signal_handler=False)
    assert launches == []


def test_two_model_pinned_agy_seats_get_separate_private_homes(tmp_path, monkeypatch):
    """The shared panel preflights both seats without sharing their guard state."""
    engine, _guard_module, workspace, _adc, _binary = _configured_guard(tmp_path, monkeypatch)
    launches = []

    def successful_attempt(argv, *, stdin, timeout, env, cwd):
        launches.append((argv, env["HOME"], cwd))
        return subprocess.CompletedProcess(argv, 0,
            json.dumps({"status": "SUCCESS", "response": "A read-only answer."}), "")

    monkeypatch.setattr(engine, "run_member", successful_attempt)
    specs = []
    for model in ("Gemini 3.5 Flash (High)", "Gemini 3.5 Pro (High)"):
        spec = engine.build_real_spec("agy", "read", 10, {"agy": {"model": model}}, tmp_path / "run")
        spec.cwd = str(workspace)
        engine.make_readonly(spec)
        specs.append(spec)

    engine.run_council(specs, retries=0, timeout=10, backoff=0,
                       workdir=tmp_path / "run", read_only=True,
                       install_signal_handler=False)

    assert len(launches) == 2
    assert len({home for _argv, home, _cwd in launches}) == 2
    assert all(argv[1] == "--new-project" and "--model" in argv and cwd == str(workspace)
               and not Path(home).exists() for argv, home, cwd in launches)


def test_no_tools_eval_path_can_still_use_its_own_agy_boundary(tmp_path, monkeypatch):
    """The harness leaves read_only unset and owns its separate no-tools boundary."""
    engine, _guard_module = _modules()
    launches = []

    def absent_binary(argv, *, stdin, timeout, env, cwd):
        launches.append(argv)
        raise FileNotFoundError("stub")

    monkeypatch.setattr(engine, "run_member", absent_binary)
    spec = engine.build_real_spec("agy", "evaluate", 5, {}, tmp_path / "run")
    engine.make_readonly(spec)
    spec.argv[0] = "stub-agy"
    spec.argv[spec.argv.index("-p"):spec.argv.index("-p")] = [
        "--agent", "eval-no-tools", "--disable-slash-commands",
    ]
    spec.cwd = str(tmp_path / "eval-cwd")
    engine.run_council([spec], retries=0, timeout=5, backoff=0,
                       workdir=tmp_path / "run", install_signal_handler=False,
                       env={"HOME": str(tmp_path / "eval-home")})
    assert len(launches) == 1
    assert "--agent" in launches[0]
    assert "--new-project" not in launches[0]


def test_public_run_provider_rejects_unguarded_readonly_agy(tmp_path, monkeypatch):
    """A caller cannot bypass run_council's preflight by using the public worker."""
    engine, _guard_module, _workspace, _adc, _binary = _configured_guard(tmp_path, monkeypatch)
    launches = []
    monkeypatch.setattr(engine, "run_member", lambda *a, **kw: launches.append(a))
    spec = engine.build_real_spec("agy", "read", 10, {}, tmp_path / "run")
    engine.make_readonly(spec)

    with pytest.raises(Exception, match="guard"):
        engine.run_provider(spec, retries=0, timeout=10, backoff=0, workdir=tmp_path / "run")
    assert launches == []


def test_preissued_guard_is_registered_and_cleaned_by_panel(tmp_path, monkeypatch):
    """Council owns a supplied guard during the run, including signal cleanup."""
    engine, guard_module, workspace, _adc, _binary = _configured_guard(tmp_path, monkeypatch)
    spec = engine.build_real_spec("agy", "read", 10, {}, tmp_path / "run")
    spec.cwd = str(workspace)
    engine.make_readonly(spec)
    spec.argv.insert(1, "--new-project")
    guard = guard_module.issue(tmp_path / "run", workspace, {},
                               argv=spec.argv, model=spec.model)
    spec.agy_guard = guard
    spec.argv = list(guard.argv)
    spec.environment = dict(guard.environment)
    launches = []

    def successful_attempt(argv, *, stdin, timeout, env, cwd):
        launches.append(argv)
        assert guard.home in engine._LIVE_AGY_GUARDS
        return subprocess.CompletedProcess(argv, 0,
            json.dumps({"status": "SUCCESS", "response": "A read-only answer."}), "")

    monkeypatch.setattr(engine, "run_member", successful_attempt)
    try:
        engine.run_council([spec], retries=0, timeout=10, backoff=0,
                           workdir=tmp_path / "run", read_only=True,
                           install_signal_handler=False)
        assert len(launches) == 1
        assert not guard.home.exists()
    finally:
        guard_module.cleanup(guard)


def test_new_guard_home_is_registered_before_policy_files_are_written(tmp_path, monkeypatch):
    """A signal during guard issuance must see the exact HOME in the cleanup registry."""
    engine, guard_module, workspace, _adc, _binary = _configured_guard(tmp_path, monkeypatch)
    spec = engine.build_real_spec("agy", "read", 10, {}, tmp_path / "run")
    spec.cwd = str(workspace)
    engine.make_readonly(spec)
    original_write = guard_module._write_private_file
    seen = []

    def observe_write(path, content, mode=0o400):
        seen.append(path)
        home = next(parent for parent in path.parents if parent.name.startswith("seat-"))
        assert home in engine._LIVE_AGY_GUARDS
        original_write(path, content, mode)

    monkeypatch.setattr(guard_module, "_write_private_file", observe_write)
    monkeypatch.setattr(engine, "run_member", lambda argv, **_kw:
                        subprocess.CompletedProcess(argv, 0,
                            json.dumps({"status": "SUCCESS", "response": "Read-only."}), ""))
    engine.run_council([spec], retries=0, timeout=10, backoff=0,
                       workdir=tmp_path / "run", read_only=True,
                       install_signal_handler=False)
    assert seen


def test_signal_before_exclusive_home_creation_cleans_only_registered_path(tmp_path, monkeypatch):
    """The exact seat path must be registered while it is still nonexistent."""
    engine, _guard_module, workspace, _adc, _binary = _configured_guard(tmp_path, monkeypatch)
    spec = engine.build_real_spec("agy", "read", 10, {}, tmp_path / "run")
    spec.cwd = str(workspace)
    engine.make_readonly(spec)
    original_mkdir = Path.mkdir
    seen, exits = [], []

    def interrupted_mkdir(path, *args, **kwargs):
        if path.name.startswith("seat-"):
            seen.append(path)
            assert path in engine._LIVE_AGY_GUARDS
            assert not path.exists()
            assert kwargs.get("mode") == 0o700
            assert not kwargs.get("exist_ok", False)
            engine._signal_cleanup(signal.SIGTERM, None)
            assert path not in engine._LIVE_AGY_GUARDS
            assert not path.exists()
            raise OSError("synthetic signal after registration")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", interrupted_mkdir)
    monkeypatch.setattr(engine.os, "_exit", exits.append)
    monkeypatch.setattr(engine, "run_member", lambda *_a, **_kw:
                        pytest.fail("no seat may launch after the synthetic signal"))
    engine._STATE["handler_fired"] = False
    try:
        with pytest.raises(Exception, match="agy|guard"):
            engine.run_council([spec], retries=0, timeout=10, backoff=0,
                               workdir=tmp_path / "run", read_only=True,
                               install_signal_handler=False)
    finally:
        engine._STATE["handler_fired"] = False
    assert len(seen) == 1
    assert exits == [143]


def test_exclusive_home_collision_does_not_delete_an_existing_seat(tmp_path, monkeypatch):
    """A random-name collision is not an owned HOME and must remain untouched."""
    _engine, guard_module, workspace, _adc, _binary = _configured_guard(tmp_path, monkeypatch)
    token = "a" * 32
    monkeypatch.setattr(secrets, "token_hex", lambda _size: token)
    root = tmp_path / "run/agy-guards"
    root.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    existing = root / f"seat-{token}"
    existing.mkdir(mode=0o700)
    marker = existing / "keep.txt"
    marker.write_text("keep\n")
    registered = set()

    with pytest.raises(guard_module.AgyGuardError):
        guard_module.issue(tmp_path / "run", workspace, {},
                           register_home=registered.add,
                           unregister_home=registered.discard)
    assert marker.read_text() == "keep\n"
    assert registered == set()


def test_failed_guard_issue_unregisters_and_removes_partial_home(tmp_path, monkeypatch):
    """An issuance error after registration cannot leave a stale private HOME."""
    engine, guard_module, workspace, _adc, _binary = _configured_guard(tmp_path, monkeypatch)
    spec = engine.build_real_spec("agy", "read", 10, {}, tmp_path / "run")
    spec.cwd = str(workspace)
    engine.make_readonly(spec)
    registered = []

    def fail_write(path, _content, mode=0o400):
        home = next(parent for parent in path.parents if parent.name.startswith("seat-"))
        registered.append(home)
        assert home in engine._LIVE_AGY_GUARDS
        raise OSError("synthetic policy write failure")

    monkeypatch.setattr(guard_module, "_write_private_file", fail_write)
    with pytest.raises(guard_module.AgyGuardError):
        engine.run_council([spec], retries=0, timeout=10, backoff=0,
                           workdir=tmp_path / "run", read_only=True,
                           install_signal_handler=False)
    assert len(registered) == 1
    assert not registered[0].exists()
    assert registered[0] not in engine._LIVE_AGY_GUARDS


def test_signal_cleanup_removes_only_registered_guard_home(tmp_path, monkeypatch):
    """A terminal signal hard-exits, so normal panel finally cannot clean its HOME."""
    engine, guard_module, workspace, _adc, _binary = _configured_guard(tmp_path, monkeypatch)
    guard = guard_module.issue(tmp_path / "run", workspace, {})
    other = tmp_path / "outside.txt"
    other.write_text("keep\n")
    exits = []
    monkeypatch.setattr(engine.os, "_exit", exits.append)
    engine._LIVE_AGY_GUARDS.add(guard.home)
    engine._STATE["handler_fired"] = False
    try:
        engine._signal_cleanup(signal.SIGTERM, None)
        assert exits == [143]
        assert not guard.home.exists()
        assert guard.home not in engine._LIVE_AGY_GUARDS
        assert other.read_text() == "keep\n"
    finally:
        engine._STATE["handler_fired"] = False
        engine._LIVE_AGY_GUARDS.discard(guard.home)
        guard_module.cleanup(guard)
