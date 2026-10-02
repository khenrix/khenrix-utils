#!/usr/bin/env python3
"""No-provider proof of the dedicated Colima VM and authenticated stdio path."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shlex
import stat
import subprocess
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.lib.fanout.container_vm import (  # noqa: E402
    CONTEXT, PROFILE, START_ARGV, VMProfileSpec, parse_profile_config,
    require_vm_probe,
)
from shared.lib.fanout.errors import ProviderRequestError  # noqa: E402


class Evidence:
    def __init__(self, path: Path) -> None:
        if path.parent != Path("/private/tmp") or not path.name.endswith(".json"):
            raise ValueError("evidence output must be a private /private/tmp JSON path")
        self.path = path
        self.data: dict[str, object] = {"schema": 1, "profile": PROFILE,
                                       "status": "in_progress", "commands": []}
        self.fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        self.save()

    def save(self) -> None:
        encoded = (json.dumps(self.data, sort_keys=True, indent=2) + "\n").encode()
        os.lseek(self.fd, 0, os.SEEK_SET)
        os.write(self.fd, encoded)
        os.ftruncate(self.fd, len(encoded))
        os.fsync(self.fd)

    def run(self, argv: tuple[str, ...], *, input_bytes: bytes | None = None,
            timeout: int = 60) -> str | bytes:
        result = subprocess.run(argv, input=input_bytes, capture_output=True,
                                timeout=timeout, check=False)
        record = {"argv": list(argv), "exit_code": result.returncode,
                  "stdout": result.stdout.decode("utf-8", "replace") if input_bytes is None
                  else {"sha256": hashlib.sha256(result.stdout).hexdigest(),
                        "byte_count": len(result.stdout)},
                  "stderr": result.stderr.decode("utf-8", "replace")}
        self.data["commands"].append(record)
        self.save()
        if result.returncode != 0:
            raise ProviderRequestError(f"control command failed: {argv[0]} {argv[1]}")
        return result.stdout if input_bytes is not None else result.stdout.decode("utf-8", "replace")

    def close(self) -> None:
        os.close(self.fd)


def _claim_profile(home: Path, existing_contexts: set[str]) -> tuple[Path, Path, bool]:
    profile_dir = home / ".colima" / PROFILE
    lima_dir = home / ".colima" / "_lima" / f"colima-{PROFILE}"
    marker = home / ".colima" / f".{PROFILE}-fanout-owner.json"
    claim = {"schema": 1, "profile": PROFILE, "context": CONTEXT,
             "uid": os.getuid(), "start_argv": list(START_ARGV)}
    try:
        profile_mode = profile_dir.lstat().st_mode
    except FileNotFoundError:
        profile_mode = None
    try:
        lima_mode = lima_dir.lstat().st_mode
    except FileNotFoundError:
        lima_mode = None
    if profile_mode is not None and not stat.S_ISDIR(profile_mode):
        raise ProviderRequestError("dedicated Colima profile path is not a directory")
    if lima_mode is not None and not stat.S_ISDIR(lima_mode):
        raise ProviderRequestError("dedicated Lima instance path is not a directory")
    try:
        marker_mode = marker.lstat().st_mode
    except FileNotFoundError:
        marker_mode = None
    if marker_mode is not None:
        marker_stat = marker.lstat()
        if (not stat.S_ISREG(marker_mode) or marker_stat.st_uid != os.getuid()
                or marker_mode & 0o077 or json.loads(marker.read_text()) != claim):
            raise ProviderRequestError("existing dedicated Colima profile is not owned by this probe")
    else:
        if profile_mode is not None:
            raise ProviderRequestError("existing dedicated Colima profile is not owned by this probe")
        if lima_mode is not None:
            raise ProviderRequestError("existing dedicated Lima instance is not owned by this probe")
        if CONTEXT in existing_contexts:
            raise ProviderRequestError("existing dedicated Docker context is not owned by this probe")
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, (json.dumps(claim, sort_keys=True) + "\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)
    return profile_dir, marker, marker_mode is None


def _neighbor_snapshot(home: Path, profile_list: str) -> dict[str, object]:
    rows = [json.loads(line) for line in profile_list.splitlines() if line.strip()]
    snapshot: dict[str, object] = {}
    for name, lima_name in (("default", "colima"),
                            ("maka-amd64", "colima-maka-amd64")):
        matches = [row for row in rows if row.get("name") == name]
        if len(matches) != 1:
            raise ProviderRequestError(f"neighbor Colima profile {name} is ambiguous")
        paths = (home / ".colima" / name / "colima.yaml",
                 home / ".colima" / "_lima" / lima_name / "lima.yaml")
        snapshot[name] = {
            "identity": matches[0],
            "config_sha256": [hashlib.sha256(path.read_bytes()).hexdigest()
                              if path.is_file() else None for path in paths],
        }
    return snapshot


def _require_neighbor_stability(before: dict[str, object], after: dict[str, object],
                                active_before: str, active_after: str) -> None:
    if before != after:
        raise ProviderRequestError("neighbor profile drifted during VM proof")
    if active_before != active_after:
        raise ProviderRequestError("active Docker context drifted during VM proof")


def _file_identity(path: Path, *, directory: bool = False,
                   hash_contents: bool = False) -> dict[str, object]:
    info = path.lstat()
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise ProviderRequestError("dedicated profile identity contains an alias")
    identity: dict[str, object] = {"device": info.st_dev, "inode": info.st_ino}
    if hash_contents:
        identity["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return identity


def _profile_identity(home: Path, context: dict[str, object],
                      host_key_sha256: str) -> dict[str, object]:
    profile_dir = home / ".colima" / PROFILE
    lima_dir = home / ".colima" / "_lima" / f"colima-{PROFILE}"
    metadata = Path(context["Storage"]["MetadataPath"])
    endpoint = f"unix://{profile_dir}/docker.sock"
    if (context.get("Name") != CONTEXT
            or context["Endpoints"]["docker"]["Host"] != endpoint
            or metadata.parent != home / ".docker/contexts/meta"
            or re.fullmatch(r"[0-9a-f]{64}", host_key_sha256) is None):
        raise ProviderRequestError("dedicated profile identity has wrong context or endpoint")
    return {
        "schema": 2, "profile": PROFILE, "context": CONTEXT,
        "endpoint": endpoint, "ssh_host_key_sha256": host_key_sha256,
        "profile_dir": _file_identity(profile_dir, directory=True),
        "profile_config": _file_identity(profile_dir / "colima.yaml", hash_contents=True),
        "lima_dir": _file_identity(lima_dir, directory=True),
        "lima_config": _file_identity(lima_dir / "lima.yaml", hash_contents=True),
        "lima_disk": _file_identity(lima_dir / "disk"),
        "context_metadata_dir": {"path": str(metadata),
                                 **_file_identity(metadata, directory=True)},
        "context_metadata": _file_identity(metadata / "meta.json", hash_contents=True),
    }


def _require_profile_binding(binding: dict[str, object] | None,
                             observed: dict[str, object]) -> None:
    if binding is None or binding != observed:
        raise ProviderRequestError("dedicated profile identity is missing or changed")


def _prestart_mode(*, marker_created: bool, binding_present: bool,
                   migrate_v1: bool, vm_running: bool) -> str:
    if marker_created:
        if binding_present:
            raise ProviderRequestError("new profile unexpectedly has identity sidecar")
        return "new"
    if binding_present:
        return "existing"
    if not migrate_v1:
        raise ProviderRequestError("v1 marker needs explicit live migration")
    if not vm_running:
        raise ProviderRequestError("v1 migration needs the VM already running")
    return "migrate"


def _binding_path(home: Path) -> Path:
    return home / ".colima" / f".{PROFILE}-identity.json"


def _read_binding(path: Path) -> dict[str, object] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise ProviderRequestError("dedicated profile identity sidecar is not private")
    return json.loads(path.read_text())


def _write_binding(path: Path, binding: dict[str, object]) -> None:
    encoded = (json.dumps(binding, sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd, temp_name = tempfile.mkstemp(prefix=f".{PROFILE}-identity-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.link(temp_name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        os.unlink(temp_name)


def _ssh_settings(config: str) -> dict[str, str]:
    settings: dict[str, str] = {}
    for line in config.splitlines():
        parts = shlex.split(line, comments=True)
        if len(parts) == 2 and parts[0] in {"IdentityFile", "Hostname", "User", "Port"}:
            if parts[0] in settings:
                raise ProviderRequestError("dedicated SSH configuration is ambiguous")
            settings[parts[0]] = parts[1]
    home = Path.home()
    if (settings.get("IdentityFile") != str(home / ".colima/_lima/_config/user")
            or settings.get("Hostname") != "127.0.0.1"
            or settings.get("User") != "lima"):
        raise ProviderRequestError("dedicated SSH route differs from local profile")
    try:
        port = int(settings["Port"])
    except (KeyError, ValueError) as error:
        raise ProviderRequestError("dedicated SSH port is unavailable") from error
    if not 1 <= port <= 65535:
        raise ProviderRequestError("dedicated SSH port is invalid")
    return settings


def _strict_ssh_argv(config: str, known_hosts: Path,
                      guest_argv: tuple[str, ...]) -> tuple[str, ...]:
    settings = _ssh_settings(config)
    if known_hosts != Path.home() / ".colima/.fanout-isolated-known_hosts":
        raise ProviderRequestError("dedicated SSH host-key pin path differs")
    return (
        "ssh", "-F", "/dev/null", "-T",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={known_hosts}",
        "-o", "GlobalKnownHostsFile=/dev/null",
        "-o", "NoHostAuthenticationForLocalhost=no",
        "-o", "HostKeyAlias=fanout-isolated",
        "-o", "HostKeyAlgorithms=ssh-ed25519",
        "-o", "CheckHostIP=no",
        "-o", "ControlMaster=no",
        "-o", "ControlPath=none",
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "PreferredAuthentications=publickey",
        "-o", "ForwardAgent=no",
        "-i", settings["IdentityFile"], "-p", settings["Port"],
        "lima@127.0.0.1", *(shlex.quote(arg) for arg in guest_argv),
    )


def _admit_host_key(home: Path, scanned_key: str,
                    guest_key: str | None) -> tuple[str, Path]:
    pin_path = home / ".colima/.fanout-isolated-known_hosts"
    if re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/]+={0,2}", scanned_key) is None:
        raise ProviderRequestError("SSH host key scan is malformed")
    try:
        pin_stat = pin_path.lstat()
    except FileNotFoundError:
        pin_stat = None
    if pin_stat is None:
        if guest_key != scanned_key:
            raise ProviderRequestError("SSH host key enrollment lacks matching guest key")
        fd = os.open(pin_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, f"fanout-isolated {scanned_key}\n".encode("ascii"))
            os.fsync(fd)
        finally:
            os.close(fd)
    else:
        if (not stat.S_ISREG(pin_stat.st_mode) or pin_stat.st_uid != os.getuid()
                or pin_stat.st_mode & 0o077):
            raise ProviderRequestError("SSH host key pin is not private and owned")
        if pin_path.read_text() != f"fanout-isolated {scanned_key}\n":
            raise ProviderRequestError("SSH host key changed from owner-held pin")
    return scanned_key, pin_path


def _read_pinned_host_key(home: Path) -> str:
    pin_path = home / ".colima/.fanout-isolated-known_hosts"
    try:
        info = pin_path.lstat()
    except FileNotFoundError as error:
        raise ProviderRequestError("SSH host key pin is missing") from error
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise ProviderRequestError("SSH host key pin is not private and owned")
    match = re.fullmatch(
        r"fanout-isolated (ssh-ed25519 [A-Za-z0-9+/]+={0,2})\n",
        pin_path.read_text(),
    )
    if match is None:
        raise ProviderRequestError("SSH host key pin is malformed")
    return match.group(1)


def _attest_live(evidence: Evidence, home: Path,
                 *, allow_enroll: bool) -> tuple[dict[str, object], str]:
    evidence.run(("colima", "status", "-p", PROFILE, "--json"))
    context_names = evidence.run(("docker", "context", "ls", "--format", "{{.Name}}"))
    if CONTEXT not in context_names.splitlines():
        raise ProviderRequestError("dedicated Docker context is missing")

    colima_path = home / ".colima" / PROFILE / "colima.yaml"
    lima_path = home / ".colima" / "_lima" / f"colima-{PROFILE}" / "lima.yaml"
    colima_yaml = colima_path.read_text()
    lima_yaml = lima_path.read_text()
    evidence.data["effective_config_paths"] = [str(colima_path), str(lima_path)]
    evidence.data["effective_config"] = {"colima_yaml": colima_yaml, "lima_yaml": lima_yaml}
    evidence.save()

    context_raw = evidence.run(("docker", "context", "inspect", CONTEXT,
                                "--format", "{{json .}}"))
    context = json.loads(context_raw)
    endpoint = context["Endpoints"]["docker"]["Host"]
    observed = parse_profile_config(colima_yaml, lima_yaml, context["Name"])
    if observed["mounts"] is not None or observed["lima_mounts"] != []:
        raise ProviderRequestError("effective VM host mount blocks SSH host-key enrollment")

    ssh_config_path = home / ".colima" / "_lima" / f"colima-{PROFILE}" / "ssh.config"
    ssh_config = ssh_config_path.read_text()
    ssh_settings = _ssh_settings(ssh_config)
    port = ssh_settings["Port"]
    scan_raw = evidence.run(("ssh-keyscan", "-T", "10", "-t", "ed25519",
                             "-p", port, "127.0.0.1"))
    scan_lines = [line.split() for line in scan_raw.splitlines()
                  if line.strip() and not line.startswith("#")]
    if (len(scan_lines) != 1 or len(scan_lines[0]) != 3
            or scan_lines[0][0] != f"[127.0.0.1]:{port}"):
        raise ProviderRequestError("SSH host key scan is ambiguous")
    scanned_key = f"{scan_lines[0][1]} {scan_lines[0][2]}"
    pin_path = home / ".colima/.fanout-isolated-known_hosts"
    guest_key = None
    if not pin_path.exists() and allow_enroll:
        guest_pub = evidence.run(("colima", "ssh", "-p", PROFILE, "--",
                                  "cat", "/etc/ssh/ssh_host_ed25519_key.pub"))
        guest_parts = guest_pub.split()
        if len(guest_parts) < 2:
            raise ProviderRequestError("SSH host key guest witness is malformed")
        guest_key = f"{guest_parts[0]} {guest_parts[1]}"
    pinned_key, pin_path = _admit_host_key(home, scanned_key, guest_key)
    evidence.data["ssh_host_key_pin_path"] = str(pin_path)
    evidence.data["ssh_host_key_sha256"] = hashlib.sha256(
        pinned_key.encode("ascii")).hexdigest()
    evidence.save()

    docker_info = evidence.run(("docker", "--context", CONTEXT, "info",
                                "--format", "{{.ServerVersion}}"))
    if not docker_info.strip():
        raise ProviderRequestError("dedicated Docker daemon did not identify itself")

    mounts_raw = evidence.run(_strict_ssh_argv(
        ssh_config, pin_path, ("cat", "/proc/mounts")))
    guest_mounts = mounts_raw.splitlines()
    guest_python = evidence.run(_strict_ssh_argv(
        ssh_config, pin_path, ("python3", "--version")))
    if re.fullmatch(r"Python 3\.[0-9]+\.[0-9]+\s*", guest_python) is None:
        raise ProviderRequestError("guest Python 3 is unavailable")

    nonce = secrets.token_bytes(32)
    reply = evidence.run(_strict_ssh_argv(
        ssh_config, pin_path, ("python3", "-c",
                               "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())")),
        input_bytes=nonce)
    probe = require_vm_probe(VMProfileSpec(PROFILE, CONTEXT, "x86_64"),
                             observed, guest_mounts, endpoint, nonce, reply,
                             ssh_host_key=scanned_key, pinned_host_key=pinned_key)
    evidence.data["probe"] = asdict(probe)
    evidence.data["probe_sha256"] = probe.digest
    evidence.save()
    return context, hashlib.sha256(pinned_key.encode("ascii")).hexdigest()


def _probe(evidence: Evidence, *, migrate_v1: bool = False) -> None:
    home = Path.home()
    context_names = evidence.run(("docker", "context", "ls", "--format", "{{.Name}}"))
    before_list = evidence.run(("colima", "list", "--json"))
    before_active_context = evidence.run(("docker", "context", "show")).strip()
    before_neighbors = _neighbor_snapshot(home, before_list)
    evidence.data["neighbors_before"] = before_neighbors
    evidence.data["active_context_before"] = before_active_context
    evidence.save()
    profile_dir, marker, marker_created = _claim_profile(home, set(context_names.splitlines()))
    evidence.data["profile_dir"] = str(profile_dir)
    evidence.data["ownership_marker"] = str(marker)
    binding_path = _binding_path(home)
    evidence.data["identity_sidecar"] = str(binding_path)
    evidence.save()
    binding = _read_binding(binding_path)
    if not marker_created and binding is None:
        if not migrate_v1:
            _prestart_mode(marker_created=False, binding_present=False,
                           migrate_v1=False, vm_running=False)
        try:
            evidence.run(("colima", "status", "-p", PROFILE, "--json"))
            vm_running = True
        except ProviderRequestError:
            vm_running = False
        mode = _prestart_mode(marker_created=False, binding_present=False,
                              migrate_v1=True, vm_running=vm_running)
    else:
        mode = _prestart_mode(marker_created=marker_created,
                              binding_present=binding is not None,
                              migrate_v1=migrate_v1, vm_running=False)
    evidence.data["prestart_mode"] = mode
    evidence.save()
    if mode == "existing":
        pinned_key = _read_pinned_host_key(home)
        context_raw = evidence.run(("docker", "context", "inspect", CONTEXT,
                                    "--format", "{{json .}}"))
        identity = _profile_identity(home, json.loads(context_raw),
                                     hashlib.sha256(pinned_key.encode("ascii")).hexdigest())
        _require_profile_binding(binding, identity)
        evidence.data["prestart_identity_verified"] = True
        evidence.save()
    elif mode == "migrate":
        context, host_key_sha256 = _attest_live(evidence, home, allow_enroll=False)
        identity = _profile_identity(home, context, host_key_sha256)
        after_list = evidence.run(("colima", "list", "--json"))
        after_active_context = evidence.run(("docker", "context", "show")).strip()
        _require_neighbor_stability(before_neighbors,
                                    _neighbor_snapshot(home, after_list),
                                    before_active_context, after_active_context)
        _write_binding(binding_path, identity)
        evidence.data["v1_migration_identity_sha256"] = hashlib.sha256(
            json.dumps(identity, sort_keys=True).encode()).hexdigest()
        evidence.save()
    evidence.run(START_ARGV, timeout=1200)
    context, host_key_sha256 = _attest_live(evidence, home,
                                             allow_enroll=mode == "new")
    identity = _profile_identity(home, context, host_key_sha256)
    if mode != "new":
        _require_profile_binding(_read_binding(binding_path), identity)
    after_list = evidence.run(("colima", "list", "--json"))
    after_active_context = evidence.run(("docker", "context", "show")).strip()
    after_neighbors = _neighbor_snapshot(home, after_list)
    evidence.data["neighbors_after"] = after_neighbors
    evidence.data["neighbors_unchanged"] = before_neighbors == after_neighbors
    evidence.data["active_context_after"] = after_active_context
    evidence.save()
    _require_neighbor_stability(before_neighbors, after_neighbors,
                                before_active_context, after_active_context)
    if mode == "new":
        _write_binding(binding_path, identity)
    evidence.data["identity_sha256"] = hashlib.sha256(
        json.dumps(identity, sort_keys=True).encode()).hexdigest()
    evidence.data["status"] = "passed"
    evidence.save()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--migrate-v1", action="store_true")
    args = parser.parse_args()
    if args.profile != PROFILE:
        parser.error(f"only {PROFILE} is permitted")
    evidence = Evidence(args.output)
    try:
        _probe(evidence, migrate_v1=args.migrate_v1)
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired,
            ProviderRequestError) as error:
        evidence.data["status"] = "blocked"
        evidence.data["error"] = f"{type(error).__name__}: {error}"
        evidence.save()
        print(f"VM probe blocked; private evidence: {evidence.path}", file=sys.stderr)
        return 1
    finally:
        evidence.close()
    print(json.dumps({"profile": PROFILE, "probe_sha256": evidence.data["probe_sha256"],
                      "evidence_path": str(evidence.path)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
