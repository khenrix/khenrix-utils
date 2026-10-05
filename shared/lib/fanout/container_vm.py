"""Pure admission of the dedicated Colima VM's observed mount and control path."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .errors import ProviderRequestError


PROFILE = "fanout-isolated"
CONTEXT = "colima-fanout-isolated"
START_ARGV = (
    "colima", "start", "-p", PROFILE, "--arch", "x86_64",
    "--vm-type", "qemu", "--cpus", "4", "--memory", "8",
    "--disk", "60", "--mount", "none", "--activate=false",
    "--ssh-agent=false", "--ssh-config=false",
    "--port-forwarder", "none", "--template=false",
)


@dataclass(frozen=True)
class VMProfileSpec:
    profile: str
    docker_context: str
    arch: str


@dataclass(frozen=True)
class VMProbe:
    profile: str
    docker_endpoint: str
    effective_mounts_sha256: str
    guest_mounts_sha256: str
    stdio_roundtrip_sha256: str
    ssh_host_key_sha256: str

    @property
    def digest(self) -> str:
        return _sha256_json(asdict(self))


def _sha256_json(value: Any) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("ascii")
    return hashlib.sha256(data).hexdigest()


def _yaml_scalar(text: str, name: str) -> str:
    matches = re.findall(rf"^{re.escape(name)}:[ \t]*([^#\n]*)", text, re.MULTILINE)
    if len(matches) != 1:
        raise ProviderRequestError(f"effective VM configuration lacks {name}")
    return matches[0].strip().strip('"\'')


def _yaml_mounts(text: str, *, absent_is_empty: bool = False) -> list[dict[str, str]] | None:
    if absent_is_empty and re.search(r"^mounts:", text, re.MULTILINE) is None:
        return []
    value = _yaml_scalar(text, "mounts")
    if value in ("null", "~"):
        return None
    if value == "[]":
        return []
    if value:
        raise ProviderRequestError("effective VM host mount configuration is ambiguous")
    block = re.search(r"^mounts:[ \t]*\n((?:^(?:[ \t].*|-.*|#.*)?(?:\n|$))*)",
                      text, re.MULTILINE)
    if block is None:
        raise ProviderRequestError("effective VM host mount configuration is ambiguous")
    locations = re.findall(r"^[ \t]*-\s+location:\s*(\S.*?)\s*$", block.group(1),
                           re.MULTILINE)
    if not locations and block.group(1).strip():
        raise ProviderRequestError("effective VM host mount configuration is ambiguous")
    return [{"location": location.strip('"\'')} for location in locations]


def parse_profile_config(colima_yaml: str, lima_yaml: str,
                         docker_context: str) -> dict[str, object]:
    """Extract the admission fields from both generated config layers."""
    return {
        "mounts": _yaml_mounts(colima_yaml),
        "lima_mounts": _yaml_mounts(lima_yaml, absent_is_empty=True),
        "arch": _yaml_scalar(colima_yaml, "arch"),
        "vm_type": _yaml_scalar(colima_yaml, "vmType"),
        "docker_context": docker_context,
    }


_GUEST_FS_TYPES = {
    "autofs", "binfmt_misc", "bpf", "cgroup2", "configfs", "debugfs",
    "devpts", "devtmpfs", "efivarfs", "ext4", "fusectl", "hugetlbfs",
    "iso9660", "mqueue", "proc", "pstore", "securityfs", "sysfs",
    "tmpfs", "tracefs", "vfat",
}
_HOST_PATH = re.compile(
    r"(?:^|[\s/:,=])(?:/Users|/Volumes|/private/var|/host_mnt)(?=$|[\s/:,])"
)
_MAX_GUEST_MOUNTS = 1024
_MAX_MOUNT_LINE = 16384
_MAX_OVERLAY_PAIRS = 128
_DOCKER_ROOT = "/var/lib/docker/rootfs/overlayfs/"
_LIMA_ROOT = f"/mnt/lima-colima-{PROFILE}/docker/rootfs/overlayfs/"
_OVERLAY_TARGET = re.compile(
    rf"(?:{re.escape(_DOCKER_ROOT)}|{re.escape(_LIMA_ROOT)})[0-9a-f]{{64}}"
)
_SNAPSHOT = r"/var/lib/containerd/io\.containerd\.snapshotter\.v1\.overlayfs/snapshots/[0-9]+"
_OVERLAY_OPTIONS = re.compile(
    rf"rw,relatime,lowerdir={_SNAPSHOT}/fs(?::{_SNAPSHOT}/fs)*,"
    rf"upperdir=(?P<upper>{_SNAPSHOT})/fs,"
    rf"workdir=(?P<work>{_SNAPSHOT})/work,nouserxattr"
)
_NETNS_TARGET = re.compile(r"/run/docker/netns/[0-9a-f]{12}")


def require_vm_probe(spec: VMProfileSpec, observed_config: dict[str, object],
                     guest_mounts: list[str], docker_endpoint: str,
                     nonce: bytes, stdio_reply: bytes, *,
                     ssh_host_key: str | None = None,
                     pinned_host_key: str | None = None) -> VMProbe:
    """Admit only an observed dedicated profile with no host share or wrong route."""
    if spec != VMProfileSpec(PROFILE, CONTEXT, "x86_64"):
        raise ProviderRequestError("VM profile specification differs from dedicated profile")
    if observed_config.get("mounts", "missing") is not None:
        raise ProviderRequestError("effective Colima host mount is enabled or ambiguous")
    if observed_config.get("lima_mounts", "missing") != []:
        raise ProviderRequestError("effective Lima host mount is enabled or ambiguous")
    if observed_config.get("arch") != spec.arch or observed_config.get("vm_type") != "qemu":
        raise ProviderRequestError("effective VM architecture or type differs")
    if observed_config.get("docker_context") != spec.docker_context:
        raise ProviderRequestError("Docker context differs from dedicated profile")
    expected_endpoint = f"unix://{Path.home()}/.colima/{PROFILE}/docker.sock"
    if docker_endpoint != expected_endpoint:
        raise ProviderRequestError("Docker endpoint differs from dedicated profile")
    if (not isinstance(guest_mounts, list) or not guest_mounts
            or len(guest_mounts) > _MAX_GUEST_MOUNTS):
        raise ProviderRequestError("guest mount table is unavailable")
    static_mounts: list[str] = []
    overlays: dict[str, dict[str, str]] = {}
    netns_targets: set[str] = set()
    for mount in guest_mounts:
        if not isinstance(mount, str) or _HOST_PATH.search(mount):
            raise ProviderRequestError("guest host mount detected")
        if len(mount) > _MAX_MOUNT_LINE:
            raise ProviderRequestError("guest mount row is too long")
        fields = mount.split()
        if len(fields) < 3:
            raise ProviderRequestError("guest host mount is present or unclassifiable")
        source, target, fs_type = fields[:3]
        if fs_type == "overlay":
            options = _OVERLAY_OPTIONS.fullmatch(fields[3]) if len(fields) == 6 else None
            if (source != "overlay" or fields[4:] != ["0", "0"]
                    or mount != " ".join(fields)
                    or _OVERLAY_TARGET.fullmatch(target) is None
                    or options is None or options["upper"] != options["work"]):
                raise ProviderRequestError("guest Docker overlay mount is unclassifiable")
            container_id = target[-64:]
            pair = overlays.setdefault(container_id, {})
            if (target in pair or (pair and next(iter(pair.values())) != fields[3])
                    or len(overlays) > _MAX_OVERLAY_PAIRS):
                raise ProviderRequestError("guest Docker overlay pair is unclassifiable")
            pair[target] = fields[3]
            continue
        if fs_type == "nsfs":
            if (len(fields) != 6 or source != "nsfs" or fields[3:] != ["rw", "0", "0"]
                    or mount != " ".join(fields)
                    or _NETNS_TARGET.fullmatch(target) is None
                    or target in netns_targets):
                raise ProviderRequestError("guest Docker network namespace mount is unclassifiable")
            netns_targets.add(target)
            continue
        if (fs_type.lower() not in _GUEST_FS_TYPES
                or target.startswith(("/var/lib/docker/rootfs/overlayfs",
                                      "/run/docker/netns"))
                or "/docker/rootfs/overlayfs" in target):
            raise ProviderRequestError("guest host mount is present or unclassifiable")
        static_mounts.append(mount)
    if any(len(pair) != 2 for pair in overlays.values()) or len(netns_targets) > len(overlays):
        raise ProviderRequestError("guest Docker runtime mounts are incomplete")
    if not isinstance(nonce, bytes) or len(nonce) < 16 or stdio_reply != nonce:
        raise ProviderRequestError("SSH stdio nonce roundtrip failed")
    if (not isinstance(ssh_host_key, str)
            or re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/]+={0,2}", ssh_host_key) is None
            or pinned_host_key != ssh_host_key):
        raise ProviderRequestError("SSH host key is unknown or changed")
    mounts = {"colima": observed_config["mounts"],
              "lima": observed_config["lima_mounts"]}
    return VMProbe(
        profile=spec.profile,
        docker_endpoint=docker_endpoint,
        effective_mounts_sha256=_sha256_json(mounts),
        guest_mounts_sha256=hashlib.sha256("\n".join(static_mounts).encode()).hexdigest(),
        stdio_roundtrip_sha256=hashlib.sha256(nonce).hexdigest(),
        ssh_host_key_sha256=hashlib.sha256(ssh_host_key.encode("ascii")).hexdigest(),
    )
