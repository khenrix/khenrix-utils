"""Hermetic process-boundary contracts for the fanout runtime."""
from __future__ import annotations

import fcntl
import importlib.util
import multiprocessing
import os
import signal
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FANOUT_ROOT = ROOT / "shared" / "lib" / "fanout"
# Council characterization imports its immutable legacy facade as ``fanout``.
# Keep this package isolated so aggregate gate collection order cannot replace it.
SPEC = importlib.util.spec_from_file_location(
    "_fanout_process_contracts",
    FANOUT_ROOT / "__init__.py",
    submodule_search_locations=[str(FANOUT_ROOT)],
)
assert SPEC and SPEC.loader
fanout = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fanout
SPEC.loader.exec_module(fanout)
process = sys.modules[f"{SPEC.name}.process"]

DEFAULT_EXECUTOR_SLOTS = fanout.DEFAULT_EXECUTOR_SLOTS
ProcessCommand = fanout.ProcessCommand
ProcessStatus = fanout.ProcessStatus
ProcessValidationError = fanout.ProcessValidationError
SlotCapacityConflictError = fanout.SlotCapacityConflictError
SlotTimeoutError = fanout.SlotTimeoutError
build_child_environment = fanout.build_child_environment
executor_slot = fanout.executor_slot
run_command = fanout.run_command


def _slot_worker(root: str, cap: int, start: multiprocessing.synchronize.Event,
                 live: multiprocessing.sharedctypes.Synchronized,
                 peak: multiprocessing.sharedctypes.Synchronized,
                 lock: multiprocessing.synchronize.Lock) -> None:
    start.wait(5)
    with executor_slot(root=root, cap=cap, timeout=2):
        with lock:
            live.value += 1
            peak.value = max(peak.value, live.value)
        time.sleep(0.15)
        with lock:
            live.value -= 1


def _crashed_slot_holder(root: str, ready: multiprocessing.synchronize.Event) -> None:
    with executor_slot(root=root, cap=1, timeout=1):
        ready.set()
        os._exit(0)


def _live_slot_holder(root: str, ready: multiprocessing.synchronize.Event,
                      release: multiprocessing.synchronize.Event) -> None:
    with executor_slot(root=root, cap=1, timeout=2):
        ready.set()
        release.wait(5)


def _slot_holder_with_cap(root: str, cap: int, ready: multiprocessing.synchronize.Event,
                          release: multiprocessing.synchronize.Event) -> None:
    with executor_slot(root=root, cap=cap, timeout=2):
        ready.set()
        release.wait(5)


def _capacity_lock_holder(root: str, ready: multiprocessing.synchronize.Event,
                          release: multiprocessing.synchronize.Event) -> None:
    with executor_slot(root=root, cap=1, timeout=2):
        pass
    fd = os.open(Path(root) / ".capacity.lock", os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        ready.set()
        release.wait(5)
    finally:
        os.close(fd)


def _capacity_timeout_caller(root: str, results: multiprocessing.queues.Queue) -> None:
    try:
        with executor_slot(root=root, cap=1, timeout=0.1):
            pass
    except SlotTimeoutError:
        results.put("timeout")
    else:
        results.put("admitted")


def _wait_for_process_death(pid: int) -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.02)
    pytest.fail(f"descendant process {pid} survived timeout cleanup")


def test_command_transports_prompt_bytes_only_over_stdin(tmp_path):
    """Putting prompt bytes in argv or a repr would disclose the provider request."""
    prompt = b"prompt-and-context-must-stay-private"
    command = ProcessCommand(
        argv=(sys.executable, "-c", "import sys; print(len(sys.stdin.buffer.read()))"),
        stdin=prompt,
        cwd=tmp_path,
        timeout=1,
        slot_root=tmp_path / "slots",
    )

    result = run_command(command)

    assert result.status is ProcessStatus.EXIT
    assert result.returncode == 0
    assert result.stdout == f"{len(prompt)}\n".encode()
    assert prompt not in " ".join(command.argv).encode()
    assert prompt.decode() not in repr(command)
    assert prompt.decode() not in repr(result)


def test_command_uses_argv_without_a_shell(tmp_path):
    """Passing a metacharacter to a shell would run an unintended second command."""
    marker = tmp_path / "shell-ran"
    literal = f"value; touch {marker}"
    command = ProcessCommand(
        argv=(sys.executable, "-c", "import sys; print(sys.argv[1])", literal),
        stdin=b"",
        cwd=tmp_path,
        timeout=1,
        slot_root=tmp_path / "slots",
    )

    result = run_command(command)

    assert result.status is ProcessStatus.EXIT
    assert result.stdout == f"{literal}\n".encode()
    assert not marker.exists()


def test_child_starts_a_distinct_session_and_process_group(tmp_path):
    """Dropping session creation would make descendant cleanup target the caller's group."""
    command = ProcessCommand(
        argv=(sys.executable, "-c", "import os; print(os.getsid(0), os.getpgrp(), os.getpid())"),
        stdin=b"",
        cwd=tmp_path,
        timeout=1,
        slot_root=tmp_path / "slots",
    )

    result = run_command(command)

    session, group, pid = map(int, result.stdout.split())
    assert result.status is ProcessStatus.EXIT
    assert session == group == pid
    assert session != os.getsid(0)


def test_timeout_terminates_descendants_reaps_parent_and_keeps_output(tmp_path):
    """Killing only the direct child would leave its TERM-ignoring helper alive."""
    pid_file = tmp_path / "descendant.pid"
    child = (
        "import os, signal, sys, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "open(sys.argv[1], 'w').write(str(os.getpid())); "
        "time.sleep(60)"
    )
    parent = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}, sys.argv[1]]); "
        "print('parent-started', flush=True); time.sleep(60)"
    )
    command = ProcessCommand(
        argv=(sys.executable, "-c", parent, str(pid_file)),
        stdin=b"",
        cwd=tmp_path,
        timeout=0.15,
        term_grace=0.05,
        reap_timeout=1,
        slot_root=tmp_path / "slots",
    )

    result = run_command(command)

    assert result.status is ProcessStatus.TIMEOUT
    assert result.timeout is not None
    assert result.timeout.terminated_group
    assert result.timeout.killed_group
    assert result.timeout.reaped
    assert result.stdout == b"parent-started\n"
    assert pid_file.exists()
    _wait_for_process_death(int(pid_file.read_text(encoding="utf-8")))


def test_exit_and_spawn_failures_are_typed_results_without_prompt_leakage(tmp_path):
    """Conflating start failures with exits loses retry policy and can expose stdin text."""
    exit_result = run_command(ProcessCommand(
        argv=(sys.executable, "-c", "import sys; print('failed'); sys.exit(7)"),
        stdin=b"private input",
        cwd=tmp_path,
        timeout=1,
        slot_root=tmp_path / "slots",
    ))
    missing_result = run_command(ProcessCommand(
        argv=(str(tmp_path / "does-not-exist"),),
        stdin=b"private input",
        cwd=tmp_path,
        timeout=1,
        slot_root=tmp_path / "slots",
    ))

    assert exit_result.status is ProcessStatus.EXIT
    assert exit_result.returncode == 7
    assert exit_result.stdout == b"failed\n"
    assert missing_result.status is ProcessStatus.SPAWN_ERROR
    assert missing_result.returncode is None
    assert missing_result.stdout == missing_result.stderr == b""
    assert "private input" not in repr(missing_result)
    assert "private input" not in (missing_result.error or "")


@pytest.mark.parametrize("argv", [(), "python -V", ("",), ("python\x00",)])
def test_command_rejects_invalid_argv_before_launch(tmp_path, argv):
    """Accepting an invalid argv defers a configuration mistake into an ambiguous spawn error."""
    with pytest.raises(ProcessValidationError):
        ProcessCommand(argv=argv, stdin=b"", cwd=tmp_path, timeout=1)  # type: ignore[arg-type]


@pytest.mark.parametrize("timeout", [0, -1, float("inf")])
def test_command_rejects_non_positive_or_non_finite_timeouts(tmp_path, timeout):
    """An unbounded or negative timeout would defeat deadline and cleanup guarantees."""
    with pytest.raises(ProcessValidationError):
        ProcessCommand(argv=(sys.executable, "-V"), stdin=b"", cwd=tmp_path, timeout=timeout)


def test_command_rejects_non_directory_cwd_and_invalid_slot_settings(tmp_path):
    """Starting with an unchecked cwd or cap makes the result depend on subprocess quirks."""
    file = tmp_path / "not-a-directory"
    file.write_text("x", encoding="utf-8")

    with pytest.raises(ProcessValidationError):
        ProcessCommand(argv=(sys.executable, "-V"), stdin=b"", cwd=file, timeout=1)
    with pytest.raises(ProcessValidationError):
        ProcessCommand(argv=(sys.executable, "-V"), stdin=b"", cwd=tmp_path, timeout=1, slot_cap=0)


def test_child_environment_admits_only_safe_values_and_neutralizes_git(tmp_path):
    """Passing ambient Git, provider, or memory variables would execute host configuration."""
    environment = build_child_environment(
        {
            "PATH": "/safe/bin",
            "LANG": "sv_SE.UTF-8",
            "GIT_DIR": str(tmp_path / "host-repository"),
            "GIT_WORK_TREE": str(tmp_path),
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": str(tmp_path / "hooks"),
            "GIT_CONFIG_PARAMETERS": "'core.hooksPath'='hostile'",
            "OPENAI_API_KEY": "provider-secret",
            "ANTHROPIC_API_KEY": "provider-secret",
            "GOOGLE_APPLICATION_CREDENTIALS": str(tmp_path / "ambient.json"),
            "CLAUDE_MEM_TOKEN": "memory-secret",
            "MEMORY_SEARCH_HELPER": "memory-helper",
            "KHENRIX_NESTED_AGENT": "0",
            "LLM_FANOUT_DEPTH": "1",
        },
        overrides={"LANG": "C"},
        max_depth=3,
    )

    assert environment["PATH"] == "/safe/bin"
    assert environment["LANG"] == "C"
    assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
    assert environment["GIT_CONFIG_SYSTEM"] == os.devnull
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["KHENRIX_NESTED_AGENT"] == "1"
    assert environment["LLM_FANOUT_DEPTH"] == "2"
    for name in (
        "GIT_DIR", "GIT_WORK_TREE", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0", "GIT_CONFIG_PARAMETERS", "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
        "CLAUDE_MEM_TOKEN", "MEMORY_SEARCH_HELPER",
    ):
        assert name not in environment


def test_executor_child_disables_agy_auto_update_even_if_ambient_requests_it(tmp_path):
    """An in-place CLI update between rounds must not invalidate a pinned run."""
    command = ProcessCommand(
        argv=(sys.executable, "-c", "import os; print(os.getenv('AGY_CLI_DISABLE_AUTO_UPDATE'))"),
        stdin=b"",
        cwd=tmp_path,
        timeout=2,
        slot_root=tmp_path / "slots",
    )

    result = run_command(
        command,
        base_environment={"PATH": os.environ.get("PATH", ""),
                          "AGY_CLI_DISABLE_AUTO_UPDATE": "false"},
    )

    assert result.status is ProcessStatus.EXIT
    assert result.returncode == 0
    assert result.stdout == b"true\n"


def test_adc_auth_mode_requires_a_literal_explicit_override():
    """The agy mode switch may cross on request, while ambient auth and secrets cannot."""
    base = {
        "PATH": "/safe/bin", "AGY_ADC_AUTH": "true",
        "CLAUDE_MEM_TOKEN": "memory-sentinel", "GEMINI_API_KEY": "key-sentinel",
    }

    assert "AGY_ADC_AUTH" not in build_child_environment(base)
    environment = build_child_environment(base, overrides={"AGY_ADC_AUTH": "true"})

    assert environment["AGY_ADC_AUTH"] == "true"
    assert "CLAUDE_MEM_TOKEN" not in environment
    assert "GEMINI_API_KEY" not in environment
    for value in ("false", "TRUE", "key-sentinel", ""):
        with pytest.raises(ProcessValidationError, match="AGY_ADC_AUTH"):
            build_child_environment(base, overrides={"AGY_ADC_AUTH": value})


def test_child_environment_admits_only_explicit_private_default_claude_adc(tmp_path, monkeypatch):
    home = tmp_path / "home"
    adc = home / ".config" / "gcloud" / "application_default_credentials.json"
    adc.parent.mkdir(parents=True)
    adc.write_text("synthetic credential")
    adc.chmod(0o600)
    monkeypatch.setenv("HOME", str(home))
    base = {
        "PATH": "/safe/bin", "HOME": str(home),
        "GOOGLE_APPLICATION_CREDENTIALS": str(tmp_path / "ambient.json"),
        "GOOGLE_CLOUD_QUOTA_PROJECT": "ambient-quota",
        "CLAUDE_MEM_TOKEN": "ambient-memory-token",
    }

    assert "GOOGLE_APPLICATION_CREDENTIALS" not in build_child_environment(base)
    environment = build_child_environment(
        base, overrides={"GOOGLE_APPLICATION_CREDENTIALS": str(adc)},
    )

    assert environment["GOOGLE_APPLICATION_CREDENTIALS"] == str(adc)
    assert "HOME" not in environment
    assert "GOOGLE_CLOUD_QUOTA_PROJECT" not in environment
    assert "CLAUDE_MEM_TOKEN" not in environment


@pytest.mark.parametrize("state", ["other-path", "missing", "public", "symlink"])
def test_child_environment_rejects_ineligible_claude_adc_override(tmp_path, monkeypatch, state):
    home = tmp_path / "home"
    adc = home / ".config" / "gcloud" / "application_default_credentials.json"
    adc.parent.mkdir(parents=True)
    adc.write_text("synthetic credential")
    adc.chmod(0o600)
    monkeypatch.setenv("HOME", str(home))
    override = adc
    if state == "other-path":
        override = tmp_path / "other.json"
        override.write_text("synthetic credential")
        override.chmod(0o600)
    elif state == "missing":
        adc.unlink()
    elif state == "public":
        adc.chmod(0o644)
    else:
        target = tmp_path / "target.json"
        target.write_text("synthetic credential")
        target.chmod(0o600)
        adc.unlink()
        adc.symlink_to(target)

    with pytest.raises(ProcessValidationError, match="GOOGLE_APPLICATION_CREDENTIALS"):
        build_child_environment({"PATH": "/safe/bin"},
                                overrides={"GOOGLE_APPLICATION_CREDENTIALS": str(override)})


@pytest.mark.parametrize("state", ["missing", "public", "symlink"])
def test_child_environment_rejects_unsafe_agy_xdg_config(tmp_path, state):
    """A private HOME cannot point XDG config at an unsafe directory."""
    home = tmp_path / "seat-home"
    home.mkdir(mode=0o700)
    config = home / ".config"
    if state == "public":
        config.mkdir(mode=0o755)
    elif state == "symlink":
        target = tmp_path / "other-config"
        target.mkdir()
        config.symlink_to(target)

    with pytest.raises(ProcessValidationError, match="XDG_CONFIG_HOME"):
        build_child_environment(
            {"PATH": "/safe/bin"},
            overrides={"HOME": str(home), "XDG_CONFIG_HOME": str(config)},
        )


@pytest.mark.parametrize("name", ["OPENAI_API_KEY", "GIT_DIR", "KHENRIX_NESTED_AGENT"])
def test_child_environment_rejects_unsafe_overrides(name):
    """Letting overrides reintroduce a denied variable bypasses the environment boundary."""
    with pytest.raises(ProcessValidationError):
        build_child_environment({"PATH": "/bin"}, overrides={name: "unsafe"})


def test_depth_limit_refuses_recursion_before_process_launch(tmp_path):
    """Launching at the limit would permit recursive fanout to consume unbounded slots."""
    marker = tmp_path / "launched"
    command = ProcessCommand(
        argv=(sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"),
        stdin=b"",
        cwd=tmp_path,
        timeout=1,
        max_depth=2,
        slot_root=tmp_path / "slots",
    )

    with pytest.raises(ProcessValidationError, match="depth"):
        run_command(command, base_environment={"PATH": os.environ.get("PATH", ""), "LLM_FANOUT_DEPTH": "2"})

    assert not marker.exists()


def test_machine_wide_slots_cap_independent_processes_and_release_crashes(tmp_path):
    """A process-local semaphore or unreleased crash lock would over-admit or deadlock callers."""
    context = multiprocessing.get_context("spawn")
    root = tmp_path / "slots"
    start = context.Event()
    live = context.Value("i", 0)
    peak = context.Value("i", 0)
    lock = context.Lock()
    workers = [
        context.Process(target=_slot_worker, args=(str(root), 1, start, live, peak, lock))
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    start.set()
    for worker in workers:
        worker.join(5)
        assert worker.exitcode == 0
    assert peak.value == 1

    ready = context.Event()
    crashed = context.Process(target=_crashed_slot_holder, args=(str(root), ready))
    crashed.start()
    assert ready.wait(2)
    crashed.join(2)
    assert crashed.exitcode == 0
    with executor_slot(root=root, cap=1, timeout=1):
        pass


def test_machine_wide_slot_acquisition_is_bounded_and_state_root_is_safe(tmp_path):
    """Waiting forever or following a state-root symlink lets contention become a host escape."""
    context = multiprocessing.get_context("spawn")
    root = tmp_path / "slots"
    ready = context.Event()
    release = context.Event()
    holder = context.Process(target=_live_slot_holder, args=(str(root), ready, release))
    holder.start()
    assert ready.wait(2)
    started = time.monotonic()
    with pytest.raises(SlotTimeoutError):
        with executor_slot(root=root, cap=1, timeout=0.1):
            pass
    elapsed = time.monotonic() - started
    release.set()
    holder.join(2)
    assert holder.exitcode == 0
    assert 0.08 <= elapsed < 1

    target = tmp_path / "target"
    target.mkdir()
    unsafe = tmp_path / "unsafe-slots"
    unsafe.symlink_to(target, target_is_directory=True)
    with pytest.raises(ProcessValidationError):
        with executor_slot(root=unsafe, cap=1, timeout=0.1):
            pass


def test_slot_root_rejects_a_conflicting_capacity_from_an_independent_process(tmp_path):
    """Scanning a larger slot range would bypass an existing root's global ceiling."""
    context = multiprocessing.get_context("spawn")
    root = tmp_path / "slots"
    ready = context.Event()
    release = context.Event()
    holder = context.Process(target=_slot_holder_with_cap, args=(str(root), 1, ready, release))
    holder.start()
    assert ready.wait(2)
    try:
        with pytest.raises(SlotCapacityConflictError):
            with executor_slot(root=root, cap=6, timeout=0.1):
                pass
    finally:
        release.set()
        holder.join(2)
    assert holder.exitcode == 0


def test_capacity_record_lock_honors_bounded_admission_timeout(tmp_path):
    """A stalled capacity initializer must not make later slot callers wait forever."""
    context = multiprocessing.get_context("spawn")
    root = tmp_path / "slots"
    ready = context.Event()
    release = context.Event()
    results = context.Queue()
    holder = context.Process(target=_capacity_lock_holder, args=(str(root), ready, release))
    holder.start()
    assert ready.wait(2)
    caller = context.Process(target=_capacity_timeout_caller, args=(str(root), results))
    caller.start()
    try:
        assert results.get(timeout=1) == "timeout"
    finally:
        release.set()
        holder.join(2)
        caller.join(2)
    assert holder.exitcode == caller.exitcode == 0


def test_slot_root_replacement_before_open_is_rejected_without_writing_replacement(tmp_path, monkeypatch):
    """Using a path check without descriptor validation would write slot state into a swap."""
    root = tmp_path / "slots"
    root.mkdir(mode=0o700)
    replacement = tmp_path / "replacement"
    original = tmp_path / "original"
    real_open = process.os.open
    swapped = False

    def open_after_swap(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if path == "slots" and dir_fd is not None and not swapped:
            swapped = True
            root.rename(original)
            replacement.mkdir(mode=0o755)
            replacement.rename(root)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(process.os, "open", open_after_swap)

    with pytest.raises(ProcessValidationError, match="owner-private"):
        with executor_slot(root=root, cap=1, timeout=0.1):
            pass

    assert swapped
    assert not (root / "slot-0").exists()
    assert not (original / "slot-0").exists()


def test_slot_root_allows_an_ancestor_alias_but_rejects_a_final_symlink(tmp_path):
    """Rejecting every ancestor symlink would break valid OS path aliases such as /var."""
    physical_parent = tmp_path / "physical"
    physical_parent.mkdir(mode=0o700)
    alias_parent = tmp_path / "alias"
    alias_parent.symlink_to(physical_parent, target_is_directory=True)
    root = alias_parent / "slots"

    with executor_slot(root=root, cap=1, timeout=1):
        pass

    physical_root = physical_parent / "slots"
    assert physical_root.is_dir()
    assert (physical_root / ".capacity").read_bytes() == b"1\n"
    assert (physical_root / "slot-0").is_file()

    final_link = alias_parent / "linked-slots"
    final_link.symlink_to(physical_root, target_is_directory=True)
    with pytest.raises(ProcessValidationError):
        with executor_slot(root=final_link, cap=1, timeout=0.1):
            pass


@pytest.mark.skipif(
    os.path.realpath(tempfile.gettempdir()) == tempfile.gettempdir(),
    reason="the host tempfile path has no ancestor alias",
)
def test_slot_root_allows_the_host_tempfile_ancestor_alias():
    """Normalizing the parent capability must support the host's real tempfile alias."""
    with tempfile.TemporaryDirectory(dir=tempfile.gettempdir()) as parent:
        root = Path(parent) / "slots"

        with executor_slot(root=root, cap=1, timeout=1):
            pass

        assert (root.resolve() / ".capacity").read_bytes() == b"1\n"


def test_slot_root_rejects_explicit_traversal_before_normalizing_its_parent(tmp_path):
    """Resolving a caller-supplied parent must not turn an escaped root spelling valid."""
    escaped = tmp_path / "private" / ".." / "outside"

    with pytest.raises(ProcessValidationError):
        with executor_slot(root=escaped, cap=1, timeout=0.1):
            pass

    assert not (tmp_path / "outside").exists()


def test_cleanup_kills_and_reaps_group_when_interrupt_arrives_during_term_grace(tmp_path, monkeypatch):
    """Letting cancellation leave TERM grace early would orphan a TERM-ignoring descendant."""
    pid_file = tmp_path / "descendant.pid"
    parent_pid_file = tmp_path / "parent.pid"
    child = (
        "import os, signal, sys, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(60)"
    )
    parent = (
        "import os, subprocess, sys, time; "
        "open(sys.argv[2], 'w').write(str(os.getpid())); "
        f"subprocess.Popen([sys.executable, '-c', {child!r}, sys.argv[1]]); "
        "print('started', flush=True); time.sleep(60)"
    )
    command = ProcessCommand(
        argv=(sys.executable, "-c", parent, str(pid_file), str(parent_pid_file)),
        stdin=b"",
        cwd=tmp_path,
        timeout=0.15,
        term_grace=0.1,
        reap_timeout=1,
        slot_root=tmp_path / "slots",
    )
    real_sleep = process.time.sleep
    interrupted = False

    def interrupt_term_grace(seconds):
        nonlocal interrupted
        if seconds == command.term_grace and not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        real_sleep(seconds)

    monkeypatch.setattr(process.time, "sleep", interrupt_term_grace)

    with pytest.raises(KeyboardInterrupt):
        run_command(command)

    assert interrupted
    assert pid_file.exists()
    assert parent_pid_file.exists()
    _wait_for_process_death(int(pid_file.read_text(encoding="utf-8")))
    _wait_for_process_death(int(parent_pid_file.read_text(encoding="utf-8")))


def test_default_slot_cap_is_six():
    """Changing the default would violate the runtime's machine-wide executor ceiling."""
    assert DEFAULT_EXECUTOR_SLOTS == 6
