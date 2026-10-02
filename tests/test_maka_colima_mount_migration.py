"""Exercise the real Maka Colima guard with isolated host-command boundaries."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
START = ROOT / "components" / "maka" / "scripts" / "colima-start.sh"


def _harness(tmp_path: Path, *, status: str, mount: str,
             fail_first_start: bool = False) -> tuple[dict[str, str], Path, Path]:
    home = tmp_path / "home"
    profile = home / ".colima" / "maka-amd64"
    profile.mkdir(parents=True)
    lab = home / ".local" / "share" / "khenrix-utils" / "maka"
    lab.mkdir(parents=True)
    lab.chmod(0o700)
    config = profile / "colima.yaml"
    config.write_text(
        "arch: x86_64\nmounts:\n"
        f"  - location: {mount}\n"
        "    writable: true\n# preserve this line\n",
        encoding="utf-8",
    )
    config.chmod(0o600)
    state = tmp_path / "colima-state.json"
    state.write_text(json.dumps({
        "status": status, "context": "default", "starts": 0, "stops": 0,
        "fail_first_start": fail_first_start,
    }), encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    colima = fake_bin / "colima"
    colima.write_text(f"#!{sys.executable}\n" + '''
import json
import os
import sys
from pathlib import Path

state_path = Path(os.environ["FAKE_COLIMA_STATE"])
state = json.loads(state_path.read_text(encoding="utf-8"))
args = sys.argv[1:]
if args == ["list", "-j"]:
    print(json.dumps({"name": "default", "status": "Broken", "arch": "aarch64",
                      "cpus": 4, "memory": 8589934592, "disk": 107374182400,
                      "runtime": "docker"}))
    print(json.dumps({"name": "maka-amd64", "status": state["status"],
                      "arch": "x86_64", "cpus": 4, "memory": 8589934592,
                      "disk": 64424509440, "runtime": "docker"}))
elif args[:1] == ["stop"]:
    state["stops"] += 1
    state["status"] = "Stopped"
    state_path.write_text(json.dumps(state), encoding="utf-8")
elif args[:1] == ["start"]:
    state["starts"] += 1
    if "--activate=false" not in args:
        state["context"] = "colima-maka-amd64"
    if state["fail_first_start"]:
        state["fail_first_start"] = False
        state_path.write_text(json.dumps(state), encoding="utf-8")
        print("injected Colima start failure", file=sys.stderr)
        sys.exit(19)
    state["status"] = "Running"
    state_path.write_text(json.dumps(state), encoding="utf-8")
elif args[:1] == ["ssh"]:
    command = args[args.index("--") + 1:]
    if command == ["mount"]:
        config = Path(os.environ["HOME"]) / ".colima/maka-amd64/colima.yaml"
        location = next(line.split("location: ", 1)[1].strip()
                        for line in config.read_text(encoding="utf-8").splitlines()
                        if "- location: " in line)
        print(f"lima-0 on {location} type virtiofs (rw,relatime)")
    elif command[:3] == ["sudo", "-n", "cat"]:
        print("[Service]\\nLimitCORE=0")
else:
    print(f"unexpected fake Colima call: {args!r}", file=sys.stderr)
    sys.exit(99)
''', encoding="utf-8")
    colima.chmod(0o700)
    docker = fake_bin / "docker"
    docker.write_text(f"#!{sys.executable}\n" + '''
import json
import os
import sys
from pathlib import Path

if sys.argv[1:] == ["context", "show"]:
    state = json.loads(Path(os.environ["FAKE_COLIMA_STATE"]).read_text(encoding="utf-8"))
    print(state["context"])
elif sys.argv[1:3] == ["version", "--format"]:
    print("x86_64 29.8.1")
else:
    sys.exit(99)
''', encoding="utf-8")
    docker.chmod(0o700)
    env = {
        **os.environ,
        "HOME": str(home), "MAKA_LAB_ROOT": str(lab),
        "FAKE_COLIMA_STATE": str(state),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
    }
    return env, config, state


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(START)], env=env, text=True, capture_output=True,
        check=False, timeout=30,
    )


@pytest.mark.parametrize("legacy_suffix", [
    "git/agentic-setup/components/maka", "git/maka-lab",
])
def test_exact_legacy_mount_relocates_to_installed_lab_without_exposing_home(
    tmp_path: Path, legacy_suffix: str,
) -> None:
    old_mount = tmp_path / "home" / legacy_suffix
    env, config, state = _harness(
        tmp_path, status="Broken", mount=str(old_mount),
    )
    original = config.read_bytes()

    result = _run(env)

    assert result.returncode == 0, result.stderr
    installed = tmp_path / "home/.local/share/khenrix-utils/maka"
    assert config.read_bytes() == original.replace(
        str(old_mount).encode(), str(installed).encode()
    )
    backup_name = (
        "colima.yaml.khenrix-utils-before-agentic-setup-relocation"
        if legacy_suffix == "git/agentic-setup/components/maka"
        else "colima.yaml.khenrix-utils-before-relocation"
    )
    backup = config.with_name(backup_name)
    assert backup.read_bytes() == original
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    after = json.loads(state.read_text(encoding="utf-8"))
    assert after["status"] == "Running"
    assert after["context"] == "default"


def test_failed_restart_restores_exact_config_and_previous_running_state(
    tmp_path: Path,
) -> None:
    old_mount = tmp_path / "home/git/agentic-setup/components/maka"
    env, config, state = _harness(
        tmp_path, status="Running", mount=str(old_mount), fail_first_start=True,
    )
    original = config.read_bytes()

    result = _run(env)

    assert result.returncode != 0
    assert "injected Colima start failure" in result.stderr
    assert config.read_bytes() == original
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    after = json.loads(state.read_text(encoding="utf-8"))
    assert after["status"] == "Running"
    assert after["context"] == "default"
    assert after["starts"] == 2
    assert after["stops"] == 2


def test_unknown_extra_mount_is_rejected_without_start_or_rewrite(
    tmp_path: Path,
) -> None:
    old_mount = tmp_path / "home/git/agentic-setup/components/maka"
    env, config, state = _harness(tmp_path, status="Broken", mount=str(old_mount))
    config.write_text(
        config.read_text(encoding="utf-8") +
        f"  - location: {tmp_path / 'home'}\n    writable: true\n",
        encoding="utf-8",
    )
    original = config.read_bytes()

    result = _run(env)

    assert result.returncode != 0
    assert "must mount only the Maka lab writable" in result.stderr
    assert config.read_bytes() == original
    after = json.loads(state.read_text(encoding="utf-8"))
    assert after["starts"] == after["stops"] == 0


def test_forged_lab_root_cannot_mount_a_private_home_directory(
    tmp_path: Path,
) -> None:
    private = tmp_path / "home/.ssh"
    private.mkdir(parents=True)
    env, config, state = _harness(tmp_path, status="Running", mount=str(private))
    env["MAKA_LAB_ROOT"] = str(private)
    original = config.read_bytes()

    result = _run(env)

    assert result.returncode != 0
    assert config.read_bytes() == original
    assert json.loads(state.read_text(encoding="utf-8"))["starts"] == 0


def test_installed_lab_path_cannot_be_a_symlink_to_a_private_directory(
    tmp_path: Path,
) -> None:
    installed = tmp_path / "home/.local/share/khenrix-utils/maka"
    env, config, state = _harness(tmp_path, status="Running", mount=str(installed))
    private = tmp_path / "home/.ssh"
    private.mkdir(parents=True)
    installed.rmdir()
    installed.symlink_to(private, target_is_directory=True)
    original = config.read_bytes()

    result = _run(env)

    assert result.returncode != 0
    assert config.read_bytes() == original
    assert json.loads(state.read_text(encoding="utf-8"))["starts"] == 0


def test_current_profile_config_cannot_be_a_symlink(
    tmp_path: Path,
) -> None:
    installed = tmp_path / "home/.local/share/khenrix-utils/maka"
    env, config, state = _harness(tmp_path, status="Running", mount=str(installed))
    elsewhere = tmp_path / "elsewhere.yaml"
    elsewhere.write_bytes(config.read_bytes())
    config.unlink()
    config.symlink_to(elsewhere)

    result = _run(env)

    assert result.returncode != 0
    assert config.is_symlink()
    assert json.loads(state.read_text(encoding="utf-8"))["starts"] == 0


def test_agentic_setup_migration_does_not_reuse_an_unrelated_legacy_backup(
    tmp_path: Path,
) -> None:
    old_mount = tmp_path / "home/git/agentic-setup/components/maka"
    env, config, _ = _harness(tmp_path, status="Broken", mount=str(old_mount))
    original = config.read_bytes()
    unrelated = config.with_name("colima.yaml.khenrix-utils-before-relocation")
    unrelated.write_text("unrelated old backup\n", encoding="utf-8")
    unrelated.chmod(0o600)

    result = _run(env)

    assert result.returncode == 0, result.stderr
    assert unrelated.read_text(encoding="utf-8") == "unrelated old backup\n"
    dedicated = config.with_name(
        "colima.yaml.khenrix-utils-before-agentic-setup-relocation"
    )
    assert dedicated.read_bytes() == original


@pytest.mark.parametrize("legacy_suffix,backup_name", [
    ("git/agentic-setup/components/maka",
     "colima.yaml.khenrix-utils-before-agentic-setup-relocation"),
    ("git/maka-lab", "colima.yaml.khenrix-utils-before-relocation"),
])
def test_stale_persistent_backup_blocks_migration_before_rewrite(
    tmp_path: Path, legacy_suffix: str, backup_name: str,
) -> None:
    old_mount = tmp_path / "home" / legacy_suffix
    env, config, state = _harness(tmp_path, status="Broken", mount=str(old_mount))
    original = config.read_bytes()
    backup = config.with_name(backup_name)
    backup.write_text("stale backup for another source\n", encoding="utf-8")
    backup.chmod(0o600)

    result = _run(env)

    assert result.returncode != 0
    assert config.read_bytes() == original
    assert backup.read_text(encoding="utf-8") == "stale backup for another source\n"
    after = json.loads(state.read_text(encoding="utf-8"))
    assert after["starts"] == after["stops"] == 0
