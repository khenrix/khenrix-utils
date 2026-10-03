"""Create and recover one admitted seat on the dedicated Docker context.

The caller seals the VM probe and supplies a guest-socket attestor over the
authenticated guest route. This module never resolves a guest path on macOS.
"""
from __future__ import annotations

import hashlib
import base64
import json
import posixpath
import re
import secrets
import stat
import subprocess
from dataclasses import asdict, dataclass
from typing import Callable

from .errors import ProviderRequestError
from .artifacts import ArtifactLimits, canonical_json


DOCKER = ("docker", "--context", "colima-fanout-isolated")
GUEST_SOCKET_ROOT = "/run/fanout"
SECURITY_ARGV = (
    "--network", "none", "--read-only", "--cap-drop", "ALL",
    "--security-opt", "no-new-privileges", "--pids-limit", "256",
    "--memory", "4g", "--memory-swap", "4g", "--cpus", "2",
    "--user", "65532:65532", "--ipc", "private", "--cgroupns", "private",
    "--restart", "no", "--pull", "never",
)
_LABEL = "io.khenrix.fanout."
_BOUNDARY = "container-seat-v1"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_IMAGE = re.compile(r"(?:[a-z0-9][a-z0-9._:/-]*@)?sha256:[0-9a-f]{64}\Z")
_CONTAINER_ID = re.compile(r"[0-9a-f]{64}\Z")
_MAX_DOCKER_OUTPUT = 1024 * 1024
# A full ID consumes 64 bytes plus its listing separator; seven fixed
# observations cover image, volumes, running inspect, info, network and listing.
_MAX_INVENTORY_IDS = (_MAX_DOCKER_OUTPUT + 1) // 65
_MAX_ACTIVE_SEATS = 6  # The coordinator's admitted seat cap.
_MAX_RUNTIME_OBSERVATIONS = _MAX_INVENTORY_IDS + 7
_MAX_RUNTIME_PROOF_BYTES = ArtifactLimits().max_file_bytes
_MEMORY = 4 * 1024**3
_NETWORK_ADDRESS_FIELDS = (
    "IPAddress", "Gateway", "GlobalIPv6Address", "IPv6Gateway",
    "LinkLocalIPv6Address", "MacAddress",
)
_VOLATILE_NETWORK_FIELDS = ("SandboxID", "SandboxKey")

DockerRunner = Callable[[tuple[str, ...], bytes | None], subprocess.CompletedProcess[bytes]]


@dataclass(frozen=True, slots=True)
class ContainerSpec:
    run_id: str
    task_id: str
    target_id: str
    seat_id: str
    image_digest: str
    workspace_volume: str
    session_volume: str
    socket_path: str
    cli_digest: str


@dataclass(frozen=True, slots=True)
class ContainerHandle:
    container_id: str
    inspect_sha256: str
    workspace_volume: str
    session_volume: str


@dataclass(frozen=True, slots=True)
class _RecoveryAssociation:
    spec: ContainerSpec
    handle: ContainerHandle
    predecessors: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class _InventoryScope:
    """Frozen associations supplied only after coordinator snapshot authentication."""

    stopped: tuple[tuple[str, str], ...]
    active: tuple[tuple[ContainerSpec, ContainerHandle], ...]


def decode_launch_record(value: object, spec: ContainerSpec, handle: ContainerHandle,
                         *, recover: bool) -> _RecoveryAssociation | None:
    """Decode an already controller-authenticated exact launch association."""
    ContainerLauncher._validate_spec(spec)
    if (not isinstance(handle, ContainerHandle)
            or not isinstance(handle.container_id, str) or _CONTAINER_ID.fullmatch(handle.container_id) is None
            or not isinstance(handle.inspect_sha256, str) or _SHA.fullmatch(handle.inspect_sha256) is None
            or (handle.workspace_volume, handle.session_volume) != (spec.workspace_volume, spec.session_volume)):
        raise ProviderRequestError("container launch handle differs")
    keys = {"spec", "handle", "route"} | ({"recovery"} if recover else set())
    if (not isinstance(value, dict) or set(value) != keys
            or value["spec"] != asdict(spec) or value["handle"] != asdict(handle)
            or value["route"] != ("recover" if recover else "start")):
        raise ProviderRequestError("container launch association differs")
    if not recover:
        return None
    recovery = value["recovery"]
    if (not isinstance(recovery, dict) or set(recovery) != {"spec", "handle", "predecessors"}
            or recovery["spec"] != asdict(spec) or recovery["handle"] != asdict(handle)
            or not isinstance(recovery["predecessors"], list)
            or len(recovery["predecessors"]) > _MAX_INVENTORY_IDS):
        raise ProviderRequestError("container recovery association differs")
    predecessors = recovery["predecessors"]
    if any(not isinstance(entry, list) or len(entry) != 2
           or not isinstance(entry[0], str) or _CONTAINER_ID.fullmatch(entry[0]) is None
           or entry[0] == handle.container_id
           or not isinstance(entry[1], str) or _SHA.fullmatch(entry[1]) is None
           for entry in predecessors):
        raise ProviderRequestError("container recovery predecessor proof is invalid")
    if len({entry[0] for entry in predecessors}) != len(predecessors):
        raise ProviderRequestError("container recovery predecessor proof is ambiguous")
    return _RecoveryAssociation(spec, handle, tuple(tuple(entry) for entry in predecessors))


class RuntimeTranscript:
    """Bound complete Docker observations and consume them in their recorded order."""

    def __init__(self, observations: object = None, *, active_peers: int = 0):
        if type(active_peers) is not int or not 0 <= active_peers < _MAX_ACTIVE_SEATS:
            raise ProviderRequestError("container runtime peer count is invalid")
        self._max_observations = _MAX_RUNTIME_OBSERVATIONS + 2 * active_peers
        self.observations: list[dict[str, object]] = []
        self._commands: set[tuple[str, ...]] = set()
        self._outputs: list[bytes] = []
        self._bytes = 2  # Canonical JSON list brackets.
        self._position = 0
        if observations is not None:
            if not isinstance(observations, list) or not 1 <= len(observations) <= self._max_observations:
                raise ProviderRequestError("container runtime observations are invalid")
            for item in observations:
                if (not isinstance(item, dict) or set(item) != {"argv", "stdout"}
                        or not isinstance(item["argv"], list) or not isinstance(item["stdout"], str)
                        or len(item["stdout"]) > 4 * ((_MAX_DOCKER_OUTPUT + 2) // 3)):
                    raise ProviderRequestError("container runtime observation is invalid")
                try:
                    raw = base64.b64decode(item["stdout"], validate=True)
                except (ValueError, UnicodeError) as error:
                    raise ProviderRequestError("container runtime observation is invalid") from error
                if base64.b64encode(raw).decode("ascii") != item["stdout"]:
                    raise ProviderRequestError("container runtime observation encoding differs")
                self.append(tuple(item["argv"]), raw)

    def append(self, argv: tuple[str, ...], raw: bytes) -> None:
        if (not argv or any(not isinstance(arg, str) or not arg or "\0" in arg for arg in argv)
                or not isinstance(raw, bytes) or len(raw) > _MAX_DOCKER_OUTPUT):
            raise ProviderRequestError("container runtime observation exceeds proof budget or is invalid")
        if argv in self._commands:
            raise ProviderRequestError("container runtime observation command is ambiguous")
        item = {"argv": list(argv), "stdout": base64.b64encode(raw).decode("ascii")}
        size = len(canonical_json(item)) + bool(self.observations)
        if (len(self.observations) >= self._max_observations
                or self._bytes + size > _MAX_RUNTIME_PROOF_BYTES):
            raise ProviderRequestError("container runtime observation exceeds proof budget")
        self._bytes += size
        self._commands.add(argv)
        self.observations.append(item)
        self._outputs.append(raw)

    def replay(self, *argv: str) -> bytes:
        if (self._position >= len(self.observations)
                or self.observations[self._position]["argv"] != list(argv)):
            raise ProviderRequestError("container runtime observation command is missing or reordered")
        raw = self._outputs[self._position]
        self._position += 1
        return raw

    def require_complete(self) -> None:
        if self._position != len(self.observations):
            raise ProviderRequestError("container runtime observations were not fully consumed")


@dataclass(frozen=True, slots=True)
class GuestSocketAttestation:
    """Authenticated guest lstat data; the attestor owns route authentication."""

    path: str
    uid: int
    mode: int
    device: int
    inode: int
    ctime_ns: int


GuestSocketAttestor = Callable[[str], GuestSocketAttestation]


def private_volume_names(run_id: str, task_id: str, target_id: str,
                         seat_id: str) -> tuple[str, str]:
    """Derive private names from controller identities, never a guest path."""
    values = (run_id, task_id, target_id, seat_id)
    if any(not isinstance(value, str) or _ID.fullmatch(value) is None for value in values):
        raise ProviderRequestError("container identity is invalid")
    identity_hash = hashlib.sha256("\0".join(values).encode("ascii")).hexdigest()
    return f"fanout-workspace-{identity_hash}", f"fanout-session-{identity_hash}"


def _default_docker(argv: tuple[str, ...], stdin: bytes | None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(argv, input=stdin, capture_output=True, check=False)


def _json_one(payload: bytes, operation: str) -> dict[str, object]:
    try:
        items = json.loads(payload)
    except (UnicodeDecodeError, ValueError) as error:
        raise ProviderRequestError(f"Docker {operation} returned invalid JSON") from error
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        raise ProviderRequestError(f"Docker {operation} returned an ambiguous object")
    return items[0]


def _object(value: object, name: str) -> dict:
    if not isinstance(value, dict):
        raise ProviderRequestError(f"container {name} inspection is unavailable")
    return value


def _empty(value: object) -> bool:
    return value is None or value == [] or value == {}


def _nullable_string_list(value: object) -> bool:
    return value is None or (isinstance(value, list)
                             and all(isinstance(entry, str) for entry in value))


def _nullable_string_map(value: object) -> bool:
    return value is None or (isinstance(value, dict)
                             and all(isinstance(key, str) and isinstance(entry, str)
                                     for key, entry in value.items()))


def _guest_path(value: object) -> str:
    if (not isinstance(value, str) or not value.startswith("/")
            or value.startswith("//") or "\0" in value):
        raise ProviderRequestError("container guest mount path is invalid")
    normalized = posixpath.normpath(value)
    if not normalized.startswith("/") or normalized.startswith("//"):
        raise ProviderRequestError("container guest mount path is invalid")
    return normalized


def _within(path: str, root: str) -> bool:
    return root == "/" or path == root or path.startswith(root + "/")


class ContainerLauncher:
    def __init__(self, docker_runner: DockerRunner | None = None, *,
                 guest_socket_attestor: GuestSocketAttestor | None = None):
        self._docker = docker_runner or _default_docker
        self._socket_attestor = guest_socket_attestor
        self._recovery: _RecoveryAssociation | None = None

    def _run(self, *args: str) -> bytes:
        argv = DOCKER + args
        try:
            result = self._docker(argv, None)
        except (OSError, subprocess.SubprocessError) as error:
            raise ProviderRequestError(f"Docker {args[0]} operation failed") from error
        if (not isinstance(result, subprocess.CompletedProcess)
                or result.returncode != 0 or not isinstance(result.stdout, bytes)
                or len(result.stdout) > _MAX_DOCKER_OUTPUT):
            raise ProviderRequestError(f"Docker {args[0]} operation failed")
        return result.stdout

    @staticmethod
    def _validate_spec(spec: ContainerSpec) -> None:
        if not isinstance(spec, ContainerSpec):
            raise ProviderRequestError("container specification is invalid")
        expected = private_volume_names(spec.run_id, spec.task_id, spec.target_id,
                                        spec.seat_id)
        if (spec.workspace_volume, spec.session_volume) != expected:
            raise ProviderRequestError("container private volume identity differs")
        if not isinstance(spec.socket_path, str) or spec.socket_path != (
                f"{GUEST_SOCKET_ROOT}/{spec.seat_id}.sock"):
            raise ProviderRequestError("container guest socket path differs")
        if not isinstance(spec.image_digest, str) or _IMAGE.fullmatch(spec.image_digest) is None:
            raise ProviderRequestError("container image is not pinned by digest")
        if not isinstance(spec.cli_digest, str) or _SHA.fullmatch(spec.cli_digest) is None:
            raise ProviderRequestError("container CLI digest is invalid")

    def _socket_proof(self, spec: ContainerSpec) -> GuestSocketAttestation:
        if self._socket_attestor is None:
            raise ProviderRequestError("authenticated guest socket attestor is required")
        try:
            proof = self._socket_attestor(spec.socket_path)
        except Exception as error:
            raise ProviderRequestError("guest socket attestation failed") from error
        if (not isinstance(proof, GuestSocketAttestation)
                or proof.path != spec.socket_path or proof.uid != 65532
                or not isinstance(proof.mode, int) or not stat.S_ISSOCK(proof.mode)
                or stat.S_IMODE(proof.mode) != 0o600
                or any(not isinstance(value, int) or value <= 0
                       for value in (proof.device, proof.inode, proof.ctime_ns))):
            raise ProviderRequestError("guest socket attestation differs")
        return proof

    @staticmethod
    def _labels(spec: ContainerSpec) -> dict[str, str]:
        return {
            _LABEL + "boundary": _BOUNDARY,
            _LABEL + "run": spec.run_id,
            _LABEL + "task": spec.task_id,
            _LABEL + "target": spec.target_id,
            _LABEL + "seat": spec.seat_id,
            _LABEL + "image_digest": spec.image_digest,
            _LABEL + "cli_digest": spec.cli_digest,
            _LABEL + "workspace_volume": spec.workspace_volume,
            _LABEL + "session_volume": spec.session_volume,
            _LABEL + "socket_path": spec.socket_path,
        }

    @staticmethod
    def _volume_labels(spec: ContainerSpec, kind: str) -> dict[str, str]:
        return {key: value for key, value in ContainerLauncher._labels(spec).items()
                if key in {_LABEL + field for field in ("boundary", "run", "task", "target", "seat")}} | {
                    _LABEL + "volume_kind": kind,
                }

    def _image_id(self, spec: ContainerSpec) -> str:
        item = _json_one(self._run("image", "inspect", spec.image_digest), "image inspect")
        image_id = item.get("Id")
        if (not isinstance(image_id, str) or not image_id.startswith("sha256:")
                or _SHA.fullmatch(image_id[7:]) is None):
            raise ProviderRequestError("Docker image identity is invalid")
        repo_digests = item.get("RepoDigests")
        if (spec.image_digest.startswith("sha256:") and image_id != spec.image_digest
                or "@sha256:" in spec.image_digest
                and (not isinstance(repo_digests, list)
                     or spec.image_digest not in repo_digests)):
            raise ProviderRequestError("Docker image digest reference differs")
        return image_id

    def _volume(self, spec: ContainerSpec, kind: str, *, create: bool) -> str:
        name = spec.workspace_volume if kind == "workspace" else spec.session_volume
        labels = self._volume_labels(spec, kind)
        if create:
            argv = ["volume", "create", "--driver", "local"]
            for key, value in labels.items():
                argv.extend(("--label", f"{key}={value}"))
            created = self._run(*argv, name).decode("ascii", "strict").strip()
            if created != name:
                raise ProviderRequestError("Docker private volume creation changed identity")
        item = _json_one(self._run("volume", "inspect", name), "volume inspect")
        mountpoint = item.get("Mountpoint")
        if (item.get("Name") != name or item.get("Driver") != "local"
                or item.get("Scope") != "local" or not _empty(item.get("Options"))
                or item.get("Labels") != labels
                or not isinstance(mountpoint, str) or not mountpoint.startswith("/")
                or mountpoint.startswith(("/Users/", "/Volumes/", "/private/", "/host_mnt/"))):
            raise ProviderRequestError("Docker private volume identity or driver differs")
        if _guest_path(mountpoint) != mountpoint:
            raise ProviderRequestError("Docker private volume mountpoint is not canonical")
        return mountpoint

    @staticmethod
    def _mount_args(spec: ContainerSpec) -> tuple[str, ...]:
        return (
            "--mount", f"type=volume,src={spec.workspace_volume},dst=/workspace",
            "--mount", f"type=volume,src={spec.session_volume},dst=/session",
            "--mount", f"type=bind,src={spec.socket_path},dst=/run/fanout/broker.sock,readonly",
        )

    @staticmethod
    def _name(spec: ContainerSpec) -> str:
        digest = spec.workspace_volume.removeprefix("fanout-workspace-")
        return f"fanout-seat-{digest}-{secrets.token_hex(8)}"

    def _inspect(self, container_id: str, spec: ContainerSpec, name: str | None,
                 image_id: str, volume_sources: tuple[str, str], *,
                 expected_state: str = "created", item: dict | None = None,
                 shared: dict | None = None) -> str:
        if item is None:
            item = _json_one(self._run("inspect", "--type", "container", container_id),
                             "container inspect")
        shared = {} if shared is None else shared
        actual_name = item.get("Name")
        if name is None:
            prefix = "/fanout-seat-" + spec.workspace_volume.removeprefix("fanout-workspace-") + "-"
            valid_name = (isinstance(actual_name, str) and actual_name.startswith(prefix)
                          and re.fullmatch(r"[0-9a-f]{16}", actual_name[len(prefix):]) is not None)
        else:
            valid_name = actual_name == "/" + name
        if item.get("Id") != container_id or not valid_name:
            raise ProviderRequestError("container identity differs")
        if item.get("Image") != image_id:
            raise ProviderRequestError("container image identity differs")
        config = _object(item.get("Config"), "configuration")
        labels = _object(config.get("Labels"), "labels")
        expected_labels = self._labels(spec)
        if any(labels.get(key) != value for key, value in expected_labels.items()):
            if labels.get(_LABEL + "cli_digest") != spec.cli_digest:
                raise ProviderRequestError("container CLI digest label differs")
            raise ProviderRequestError("container run or boundary label differs")
        if config.get("Image") != spec.image_digest:
            raise ProviderRequestError("container image reference differs")
        if config.get("User") != "65532:65532":
            raise ProviderRequestError("container user differs")

        host = _object(item.get("HostConfig"), "host configuration")
        if host.get("NetworkMode") != "none":
            raise ProviderRequestError("container network mode differs")
        # Docker's empty PID mode creates a private namespace; "private" is invalid.
        if host.get("PidMode") != "":
            raise ProviderRequestError("container PID namespace differs")
        if host.get("IpcMode") != "private":
            raise ProviderRequestError("container IPC namespace differs")
        if host.get("UTSMode") != "":
            raise ProviderRequestError("container UTS namespace differs")
        if host.get("CgroupnsMode") != "private":
            raise ProviderRequestError("container cgroup namespace differs")
        if host.get("ReadonlyRootfs") is not True:
            raise ProviderRequestError("container read-only root differs")
        if host.get("Privileged") is not False or host.get("SecurityOpt") not in (
                ["no-new-privileges"], ["no-new-privileges:true"]):
            raise ProviderRequestError("container privilege policy differs")
        if (host.get("CapDrop") != ["ALL"] or not _empty(host.get("CapAdd"))
                or not _empty(host.get("Devices")) or not _empty(host.get("DeviceRequests"))):
            raise ProviderRequestError("container capability or device policy differs")
        if (host.get("PidsLimit") != 256 or host.get("Memory") != _MEMORY
                or host.get("MemorySwap") != _MEMORY or host.get("NanoCpus") != 2_000_000_000):
            raise ProviderRequestError("container resource limits differ")
        if "OomKillDisable" not in host:
            raise ProviderRequestError("container OOM policy differs")
        oom_kill_disable = host["OomKillDisable"]
        if (oom_kill_disable is not False
                and (expected_state != "running" or oom_kill_disable is not None)):
            raise ProviderRequestError("container OOM policy differs")
        restart = _object(host.get("RestartPolicy"), "restart policy")
        if restart.get("Name") != "no":
            raise ProviderRequestError("container restart policy differs")
        if (not _empty(host.get("Binds")) or not _empty(host.get("VolumesFrom"))
                or not _empty(host.get("Tmpfs"))
                or host.get("PublishAllPorts") is not False):
            raise ProviderRequestError("container mount or published port differs")
        if not _empty(host.get("PortBindings")):
            raise ProviderRequestError("container network port binding differs")

        host_mounts = host.get("Mounts")
        if (not isinstance(host_mounts, list) or len(host_mounts) != 3
                or any(not isinstance(mount, dict) for mount in host_mounts)):
            raise ProviderRequestError("container host mount specification differs")
        host_by_target = {mount.get("Target"): mount for mount in host_mounts}
        expected_host = {
            "/workspace": ("volume", spec.workspace_volume, False),
            "/session": ("volume", spec.session_volume, False),
            "/run/fanout/broker.sock": ("bind", spec.socket_path, True),
        }
        if set(host_by_target) != set(expected_host):
            raise ProviderRequestError("container host mount specification differs")
        for target, (kind, source, read_only) in expected_host.items():
            mount = host_by_target[target]
            if (not isinstance(mount, dict) or mount.get("Type") != kind
                    or mount.get("Source") != source or mount.get("Target") != target
                    or mount.get("ReadOnly", False) is not read_only):
                raise ProviderRequestError("container host mount specification differs")
        mounts = item.get("Mounts")
        if (not isinstance(mounts, list) or len(mounts) != 3
                or any(not isinstance(mount, dict) for mount in mounts)):
            raise ProviderRequestError("container mount inventory differs")
        by_destination = {mount.get("Destination"): mount for mount in mounts}
        expected_mounts = {
            "/workspace": ("volume", volume_sources[0], spec.workspace_volume, "local", True),
            "/session": ("volume", volume_sources[1], spec.session_volume, "local", True),
            "/run/fanout/broker.sock": ("bind", spec.socket_path, "", "", False),
        }
        if set(by_destination) != set(expected_mounts):
            raise ProviderRequestError("container mount inventory differs")
        for target, (kind, source, name_value, driver, writable) in expected_mounts.items():
            mount = by_destination[target]
            if (not isinstance(mount, dict) or mount.get("Type") != kind
                    or mount.get("Source") != source or mount.get("Destination") != target
                    or mount.get("Name", "") != name_value
                    or mount.get("Driver", "") != driver or mount.get("RW") is not writable):
                raise ProviderRequestError("container mount inventory differs")

        network = _object(item.get("NetworkSettings"), "network settings")
        networks = network.get("Networks")
        if (not isinstance(networks, dict) or set(networks) != {"none"}
                or not isinstance(networks["none"], dict)):
            raise ProviderRequestError("container network attachment differs")
        attachment = networks["none"]
        if (any(attachment.get(field) for field in _NETWORK_ADDRESS_FIELDS)
                or any(not _empty(attachment.get(field)) for field in (
                    "IPAMConfig", "Links", "Aliases", "DriverOpts", "DNSNames"))
                or attachment.get("IPPrefixLen", 0) != 0
                or attachment.get("GlobalIPv6PrefixLen", 0) != 0):
            raise ProviderRequestError("container network attachment differs")
        network_id = attachment.get("NetworkID")
        endpoint_id = attachment.get("EndpointID")
        if expected_state == "created":
            if network_id != "" or endpoint_id != "":
                raise ProviderRequestError("container network identity differs")
        elif (not isinstance(network_id, str) or _SHA.fullmatch(network_id) is None
              or not isinstance(endpoint_id, str) or _SHA.fullmatch(endpoint_id) is None):
            raise ProviderRequestError("container network identity differs")
        ports = network.get("Ports")
        if not _empty(ports) and (not isinstance(ports, dict)
                                  or any(not _empty(value) for value in ports.values())):
            raise ProviderRequestError("container network ports differ")
        if (any(network.get(field) for field in _NETWORK_ADDRESS_FIELDS)
                or not _empty(network.get("SecondaryIPAddresses"))
                or not _empty(network.get("SecondaryIPv6Addresses"))):
            raise ProviderRequestError("container network address differs")
        sandbox_id = network.get("SandboxID", "")
        sandbox_key = network.get("SandboxKey", "")
        if (not isinstance(sandbox_id, str) or not isinstance(sandbox_key, str)
                or sandbox_id and _SHA.fullmatch(sandbox_id) is None
                or sandbox_key and sandbox_key != f"/var/run/docker/netns/{sandbox_id[:12]}"):
            raise ProviderRequestError("container network sandbox differs")
        state = _object(item.get("State"), "state")
        if (state.get("Status") != expected_state
                or state.get("Running") is not (expected_state == "running")
                or any(state.get(field) is not False
                       for field in ("Paused", "Restarting", "Dead", "OOMKilled"))):
            raise ProviderRequestError("container state differs")
        if expected_state == "running" and oom_kill_disable is None:
            try:
                if "info" not in shared:
                    shared["info"] = json.loads(self._run("info", "--format", "{{json .}}"))
                docker_info = shared["info"]
            except (UnicodeDecodeError, ValueError) as error:
                raise ProviderRequestError("Docker info returned invalid JSON") from error
            if (not isinstance(docker_info, dict)
                    or docker_info.get("CgroupVersion") != "2"
                    or docker_info.get("OomKillDisable") is not False):
                raise ProviderRequestError("container OOM capability differs")
        if expected_state == "running":
            if "network" not in shared:
                shared["network"] = _json_one(self._run("network", "inspect", "none"), "network inspect")
            none_network = shared["network"]
            members = none_network.get("Containers")
            member = members.get(container_id) if isinstance(members, dict) else None
            if (none_network.get("Name") != "none"
                    or none_network.get("Driver") != "null"
                    or none_network.get("Id") != network_id
                    or not isinstance(member, dict)
                    or set(member) != {"Name", "EndpointID", "MacAddress",
                                       "IPv4Address", "IPv6Address"}
                    or member.get("Name") != actual_name[1:]
                    or member.get("EndpointID") != endpoint_id
                    or any(member.get(field) != "" for field in (
                        "MacAddress", "IPv4Address", "IPv6Address"))):
                raise ProviderRequestError("container network inventory differs")

        observed = {key: item[key] for key in ("Id", "Image", "Name", "Config",
                                               "HostConfig", "NetworkSettings")}
        observed["NetworkSettings"] = {key: value for key, value in network.items()
                                        if key not in _VOLATILE_NETWORK_FIELDS}
        if expected_state == "running":
            observed["NetworkSettings"]["Networks"] = {
                "none": {**attachment, "NetworkID": "", "EndpointID": ""},
            }
        observed["HostConfig"] = {**host, "Mounts": [host_by_target[target]
                                                    for target in sorted(host_by_target)]}
        if oom_kill_disable is None:
            observed["HostConfig"]["OomKillDisable"] = False
        observed["Mounts"] = [by_destination[target] for target in sorted(by_destination)]
        data = json.dumps(observed, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True).encode("ascii")
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _stopped_observation(container_id: str, spec: ContainerSpec, item: dict) -> str:
        config = _object(item.get("Config"), "stopped orphan configuration")
        labels = _object(config.get("Labels"), "stopped orphan labels")
        host = _object(item.get("HostConfig"), "stopped orphan host configuration")
        restart = _object(host.get("RestartPolicy"), "stopped orphan restart policy")
        state = _object(item.get("State"), "stopped orphan state")
        if (item.get("Id") != container_id
                or labels.get(_LABEL + "boundary") != _BOUNDARY
                or labels.get(_LABEL + "run") != spec.run_id
                or restart.get("Name") != "no"
                or state.get("Status") != "exited" or state.get("Running") is not False
                or any(state.get(field) is not False for field in (
                    "Paused", "Restarting", "Dead", "OOMKilled"))):
            raise ProviderRequestError("recovery predecessor is not verified stopped")
        network = _object(item.get("NetworkSettings"), "stopped orphan network settings")
        networks = _object(network.get("Networks"), "stopped orphan network attachments")
        string_fields = (
            (item, ("Image", "Name")), (config, ("Image", "User")),
            (labels, tuple(ContainerLauncher._labels(spec))),
            (host, ("NetworkMode", "PidMode", "IpcMode", "UTSMode", "CgroupnsMode")),
            (network, ("SandboxID", "SandboxKey")),
        )
        if (any(not isinstance(observed.get(field), str)
                for observed, fields in string_fields for field in fields)
                or _IMAGE.fullmatch(item["Image"]) is None
                or _IMAGE.fullmatch(config["Image"]) is None
                or not item["Name"].startswith("/")
                or any(type(host.get(field)) is not bool for field in (
                    "ReadonlyRootfs", "Privileged", "PublishAllPorts"))
                or any(type(host.get(field)) is not int for field in (
                    "PidsLimit", "Memory", "MemorySwap", "NanoCpus"))
                or type(restart.get("MaximumRetryCount")) is not int
                or "OomKillDisable" not in host
                or host["OomKillDisable"] is not None
                and type(host["OomKillDisable"]) is not bool):
            raise ProviderRequestError("recovery predecessor security observation is incomplete")
        for field, entry_type in (
                ("SecurityOpt", str), ("CapDrop", str), ("CapAdd", str),
                ("Binds", str), ("VolumesFrom", str), ("Devices", dict),
                ("DeviceRequests", dict)):
            value = host.get(field)
            if (field not in host or value is not None
                    and (not isinstance(value, list)
                         or any(not isinstance(entry, entry_type) for entry in value))):
                raise ProviderRequestError("recovery predecessor security observation is incomplete")
        for device in host["Devices"] or []:
            if any(not isinstance(device.get(field), str) for field in (
                    "PathOnHost", "PathInContainer", "CgroupPermissions")):
                raise ProviderRequestError("recovery predecessor device observation is incomplete")
        for request in host["DeviceRequests"] or []:
            if (any(field not in request for field in (
                    "Driver", "Count", "DeviceIDs", "Capabilities", "Options"))
                    or not isinstance(request["Driver"], str)
                    or type(request["Count"]) is not int
                    or not _nullable_string_list(request["DeviceIDs"])
                    or not _nullable_string_map(request["Options"])):
                raise ProviderRequestError("recovery predecessor device request is incomplete")
            capabilities = request["Capabilities"]
            if (capabilities is not None and (not isinstance(capabilities, list)
                    or any(not _nullable_string_list(row) for row in capabilities))):
                raise ProviderRequestError("recovery predecessor device capabilities are incomplete")
        # Docker omits empty Tmpfs; keep omission distinct from null in the hash.
        tmpfs = host.get("Tmpfs")
        if (tmpfs is not None and (not isinstance(tmpfs, dict)
                or any(not isinstance(path, str) or not isinstance(options, str)
                       for path, options in tmpfs.items()))):
            raise ProviderRequestError("recovery predecessor security observation is incomplete")
        for observed, field in ((host, "PortBindings"), (network, "Ports")):
            if (field not in observed or observed[field] is not None
                    and not isinstance(observed[field], dict)):
                raise ProviderRequestError("recovery predecessor security observation is incomplete")
            for port, bindings in (observed[field] or {}).items():
                if (not isinstance(port, str) or bindings is not None
                        and (not isinstance(bindings, list)
                             or any(not isinstance(binding, dict)
                                    or any(not isinstance(binding.get(member), str)
                                           for member in ("HostIp", "HostPort"))
                                    for binding in bindings))):
                    raise ProviderRequestError("recovery predecessor port observation is incomplete")
        for mounts, destination, host_mount in (
                (item.get("Mounts"), "Destination", False),
                (host.get("Mounts"), "Target", True)):
            if not isinstance(mounts, list):
                raise ProviderRequestError("recovery predecessor mount observation is incomplete")
            destinations: set[str] = set()
            for mount in mounts:
                if (not isinstance(mount, dict)
                        or mount.get("Type") not in ("bind", "volume")
                        or not isinstance(mount.get("Source"), str)
                        or not isinstance(mount.get(destination), str)
                        or host_mount and type(mount.get("ReadOnly", False)) is not bool
                        or not host_mount and type(mount.get("RW")) is not bool
                        or not host_mount and any(not isinstance(mount.get(field, ""), str)
                                                  for field in ("Name", "Driver"))
                        or not host_mount and mount["Type"] == "volume"
                        and any(not isinstance(mount.get(field), str)
                                for field in ("Name", "Driver"))):
                    raise ProviderRequestError("recovery predecessor mount observation is incomplete")
                normalized_destination = _guest_path(mount[destination])
                if not host_mount:
                    if normalized_destination in destinations:
                        raise ProviderRequestError("recovery predecessor has duplicate mount destinations")
                    destinations.add(normalized_destination)
                if not host_mount or mount["Type"] == "bind":
                    _guest_path(mount["Source"])
        if not networks:
            raise ProviderRequestError("recovery predecessor network observation is incomplete")
        for attachment in networks.values():
            attachment = _object(attachment, "stopped orphan network attachment")
            if (any(not isinstance(attachment.get(field), str) for field in (
                    "NetworkID", "EndpointID", "IPAddress", "Gateway", "GlobalIPv6Address",
                    "IPv6Gateway", "MacAddress"))
                    or any(type(attachment.get(field)) is not int for field in (
                        "IPPrefixLen", "GlobalIPv6PrefixLen"))):
                raise ProviderRequestError("recovery predecessor network observation is incomplete")
            for field, kind in (("IPAMConfig", dict), ("DriverOpts", dict),
                                ("Links", list), ("Aliases", list), ("DNSNames", list)):
                if (field not in attachment or attachment[field] is not None
                        and not isinstance(attachment[field], kind)):
                    raise ProviderRequestError("recovery predecessor network observation is incomplete")
            if (any(not _nullable_string_list(attachment[field])
                    for field in ("Links", "Aliases", "DNSNames"))
                    or not _nullable_string_map(attachment["DriverOpts"])):
                raise ProviderRequestError("recovery predecessor endpoint observation is incomplete")
            ipam = attachment["IPAMConfig"]
            if (ipam is not None and (
                    any(field in ipam and not isinstance(ipam[field], str)
                        for field in ("IPv4Address", "IPv6Address"))
                    or "LinkLocalIPs" in ipam and not _nullable_string_list(ipam["LinkLocalIPs"]))):
                raise ProviderRequestError("recovery predecessor IPAM observation is incomplete")
        # Quarantined images and mounts may differ from the new seat. Their complete
        # stopped observation must stay unchanged; this does not admit them to run.
        # Docker's top-level mount inventory has no stable order.
        observed = {**item, "Mounts": sorted(item["Mounts"], key=lambda mount: mount["Destination"])}
        data = json.dumps(observed, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True).encode("ascii")
        return hashlib.sha256(data).hexdigest()

    def _inventory(self, spec: ContainerSpec, volume_sources: tuple[str, str], *,
                   fence_owned: bool, exclude_id: str | None = None,
                   predecessors: tuple[tuple[str, str], ...] | None = None,
                   scope: _InventoryScope | None = None,
                   shared: dict | None = None, image_id: str | None = None) -> tuple[tuple[str, str], ...]:
        active = {}
        shared = {} if shared is None else shared
        if scope is not None:
            if (type(scope) is not _InventoryScope or not isinstance(scope.stopped, tuple)
                    or not isinstance(scope.active, tuple) or len(scope.stopped) > _MAX_INVENTORY_IDS
                    or len(scope.active) > _MAX_ACTIVE_SEATS or fence_owned):
                raise ProviderRequestError("container inventory scope is invalid")
            if (any(not isinstance(entry, tuple) or len(entry) != 2
                    or not isinstance(entry[0], str) or _CONTAINER_ID.fullmatch(entry[0]) is None
                    or not isinstance(entry[1], str) or _SHA.fullmatch(entry[1]) is None
                    for entry in scope.stopped)
                    or len(dict(scope.stopped)) != len(scope.stopped)):
                raise ProviderRequestError("container inventory stopped association is invalid")
            predecessors = scope.stopped
            active_ids = set()
            for entry in scope.active:
                if not isinstance(entry, tuple) or len(entry) != 2:
                    raise ProviderRequestError("container inventory active association is invalid")
                peer_spec, peer_handle = entry
                self._validate_spec(peer_spec)
                if (not isinstance(peer_handle, ContainerHandle)
                        or not isinstance(peer_handle.container_id, str)
                        or _CONTAINER_ID.fullmatch(peer_handle.container_id) is None
                        or not isinstance(peer_handle.inspect_sha256, str)
                        or _SHA.fullmatch(peer_handle.inspect_sha256) is None
                        or (peer_handle.workspace_volume, peer_handle.session_volume) != (
                            peer_spec.workspace_volume, peer_spec.session_volume)
                        or peer_spec.run_id != spec.run_id or peer_spec.image_digest != spec.image_digest
                        or peer_handle.container_id in active_ids or peer_handle.container_id in dict(predecessors)):
                    raise ProviderRequestError("container inventory active association is invalid")
                active_ids.add(peer_handle.container_id)
                if peer_handle.container_id == exclude_id:
                    continue
                sources = (self._volume(peer_spec, "workspace", create=False),
                           self._volume(peer_spec, "session", create=False))
                if self._resource_conflict(spec, {"Mounts": [], "HostConfig": {"Mounts": []}},
                                           self._labels(peer_spec), volume_sources):
                    raise ProviderRequestError("active sibling resource conflict")
                for existing_spec, _, existing_sources in active.values():
                    if self._resource_conflict(existing_spec, {"Mounts": [], "HostConfig": {"Mounts": []}},
                                               self._labels(peer_spec), existing_sources):
                        raise ProviderRequestError("active sibling resource conflict")
                active[peer_handle.container_id] = peer_spec, peer_handle, sources
        output = self._run(
            "container", "ls", "-a", "--no-trunc",
            "--format", "{{.ID}}",
        )
        try:
            ids = output.decode("ascii", "strict").splitlines()
        except UnicodeDecodeError as error:
            raise ProviderRequestError("Docker recovery inventory is invalid") from error
        if len(ids) != len(set(ids)) or any(_CONTAINER_ID.fullmatch(value) is None for value in ids):
            raise ProviderRequestError("Docker recovery inventory is invalid")
        owned: list[tuple[str, bool]] = []
        expected_predecessors = dict(predecessors or ())
        seen_predecessors: set[str] = set()
        seen_active: set[str] = set()
        stopped_observations: list[tuple[str, str]] = []
        foreign_conflict = False
        owned_conflict = False
        for container_id in ids:
            if container_id == exclude_id:
                continue
            item = _json_one(self._run("inspect", "--type", "container", container_id),
                             "orphan inspect")
            if item.get("Id") != container_id:
                raise ProviderRequestError("recovery container identity differs")
            if container_id in active:
                peer_spec, peer_handle, peer_sources = active[container_id]
                observed = self._inspect(container_id, peer_spec, None, image_id, peer_sources,
                                         expected_state="running", item=item, shared=shared)
                if observed != peer_handle.inspect_sha256:
                    raise ProviderRequestError("active sibling security digest differs")
                if self._resource_conflict(spec, item, self._labels(peer_spec), volume_sources):
                    raise ProviderRequestError("active sibling resource conflict")
                seen_active.add(container_id)
                continue
            if container_id in expected_predecessors:
                if self._stopped_observation(container_id, spec, item) != expected_predecessors[container_id]:
                    raise ProviderRequestError("recovery predecessor observation changed")
                seen_predecessors.add(container_id)
                continue
            raw_labels = _object(item.get("Config"), "orphan configuration").get("Labels")
            labels = {} if raw_labels is None else _object(raw_labels, "orphan labels")
            exact_owner = (labels.get(_LABEL + "boundary") == _BOUNDARY
                           and labels.get(_LABEL + "run") == spec.run_id)
            if not exact_owner:
                foreign_conflict |= self._resource_conflict(spec, item, labels,
                                                            volume_sources)
                for peer_spec, _, peer_sources in active.values():
                    foreign_conflict |= self._resource_conflict(peer_spec, item, labels, peer_sources)
                continue
            if not fence_owned:
                if predecessors is not None:
                    raise ProviderRequestError("existing seat resource conflict in recovery predecessor inventory")
                owned_conflict |= self._resource_conflict(spec, item, labels,
                                                          volume_sources)
                continue
            state = _object(item.get("State"), "orphan state")
            if type(state.get("Running")) is not bool:
                raise ProviderRequestError("recovery orphan state is ambiguous")
            owned.append((container_id, state["Running"]))
        for container_id, running in owned:
            self._run("update", "--restart", "no", container_id)
            if running:
                self._run("stop", "--time", "0", container_id)
            stopped = _json_one(self._run("inspect", "--type", "container", container_id),
                                "stopped orphan inspect")
            stopped_host = _object(stopped.get("HostConfig"), "stopped orphan host configuration")
            restart = _object(stopped_host.get("RestartPolicy"), "stopped orphan restart policy")
            if (stopped.get("Id") != container_id or restart.get("Name") != "no"
                    or _object(stopped.get("State"), "stopped orphan state").get("Running")
                    is not False):
                raise ProviderRequestError("recovery failed to fence orphan container")
            stopped_observations.append((container_id, self._stopped_observation(
                container_id, spec, stopped)))
        if seen_predecessors != set(expected_predecessors):
            raise ProviderRequestError("recovery predecessor inventory differs")
        if seen_active != set(active):
            raise ProviderRequestError("active sibling inventory differs")
        if foreign_conflict:
            raise ProviderRequestError("foreign run resource conflict in recovery inventory")
        if owned_conflict:
            raise ProviderRequestError("existing seat resource conflict in inventory")
        return tuple(stopped_observations)

    @staticmethod
    def _resource_conflict(spec: ContainerSpec, item: dict, labels: dict,
                           volume_sources: tuple[str, str]) -> bool:
        if (labels.get(_LABEL + "seat") == spec.seat_id
                or labels.get(_LABEL + "workspace_volume") == spec.workspace_volume
                or labels.get(_LABEL + "session_volume") == spec.session_volume
                or labels.get(_LABEL + "socket_path") == spec.socket_path):
            return True
        mounts = item.get("Mounts")
        host_mounts = _object(item.get("HostConfig"), "foreign host configuration").get("Mounts")
        if host_mounts is None:
            host_mounts = []
        if (not isinstance(mounts, list) or not isinstance(host_mounts, list)
                or any(not isinstance(mount, dict) for mount in (*mounts, *host_mounts))):
            raise ProviderRequestError("foreign container mount inventory is unavailable")
        socket = _guest_path(spec.socket_path)
        volumes = tuple(_guest_path(source) for source in volume_sources)
        for mount in mounts:
            if (mount.get("Type") == "volume"
                    and mount.get("Name") in (spec.workspace_volume, spec.session_volume)):
                return True
            if mount.get("Type") in ("bind", "volume"):
                path = _guest_path(mount.get("Source"))
                if (mount.get("Type") == "bind" and _within(socket, path)
                        or any(_within(path, volume) or _within(volume, path)
                               for volume in volumes)):
                    return True
        for mount in host_mounts:
            if (mount.get("Type") == "volume"
                    and mount.get("Source") in (spec.workspace_volume, spec.session_volume)):
                return True
            if mount.get("Type") == "bind":
                path = _guest_path(mount.get("Source"))
                if (_within(socket, path)
                        or any(_within(path, volume) or _within(volume, path)
                               for volume in volumes)):
                    return True
        return False

    def _failed_launch_item(self, spec: ContainerSpec, name: str,
                            created_id: str | None, image_id: str,
                            sources: tuple[str, str]) -> dict:
        item = _json_one(self._run("inspect", "--type", "container", name),
                         "failed launch inspect")
        container_id = item.get("Id")
        config = _object(item.get("Config"), "failed launch configuration")
        host = _object(item.get("HostConfig"), "failed launch host configuration")
        labels = _object(config.get("Labels"), "failed launch labels")
        expected_labels = self._labels(spec)
        if (not isinstance(container_id, str)
                or _CONTAINER_ID.fullmatch(container_id) is None
                or created_id is not None and container_id != created_id
                or item.get("Name") != "/" + name
                or item.get("Image") != image_id
                or config.get("Image") != spec.image_digest
                or any(labels.get(key) != value for key, value in expected_labels.items())):
            raise ProviderRequestError("failed launch container ownership differs")
        expected = {
            "/workspace": ("volume", spec.workspace_volume, sources[0], "local", True),
            "/session": ("volume", spec.session_volume, sources[1], "local", True),
            "/run/fanout/broker.sock": ("bind", "", spec.socket_path, "", False),
        }
        mounts = item.get("Mounts")
        host_mounts = host.get("Mounts")
        if (not isinstance(mounts, list) or len(mounts) != 3
                or not isinstance(host_mounts, list) or len(host_mounts) != 3
                or any(not isinstance(mount, dict) for mount in (*mounts, *host_mounts))):
            raise ProviderRequestError("failed launch container mounts differ")
        by_target = {mount.get("Destination"): mount for mount in mounts}
        host_by_target = {mount.get("Target"): mount for mount in host_mounts}
        if set(by_target) != set(expected) or set(host_by_target) != set(expected):
            raise ProviderRequestError("failed launch container mounts differ")
        for target, (kind, mount_name, source, driver, writable) in expected.items():
            mount = by_target[target]
            host_mount = host_by_target[target]
            if (mount.get("Type") != kind or mount.get("Name", "") != mount_name
                    or mount.get("Source") != source or mount.get("Driver", "") != driver
                    or mount.get("RW") is not writable
                    or host_mount.get("Type") != kind
                    or host_mount.get("Source") != (mount_name if kind == "volume" else source)
                    or host_mount.get("ReadOnly", False) is not (not writable)):
                raise ProviderRequestError("failed launch container mounts differ")
        return item

    def _cleanup_failed_launch(self, spec: ContainerSpec, name: str,
                               created_id: str | None, image_id: str,
                               sources: tuple[str, str]) -> str:
        item = self._failed_launch_item(spec, name, created_id, image_id, sources)
        container_id = item["Id"]
        state = _object(item.get("State"), "failed launch state")
        if (type(state.get("Running")) is not bool
                or state.get("Status") not in (("running",) if state["Running"]
                                                 else ("created", "exited"))
                or any(state.get(field) is not False for field in (
                    "Paused", "Restarting", "Dead", "OOMKilled"))):
            raise ProviderRequestError("failed launch container state is ambiguous")
        self._run("update", "--restart", "no", container_id)
        if state["Running"]:
            self._run("stop", "--time", "0", container_id)
        stopped = self._failed_launch_item(spec, name, container_id, image_id, sources)
        stopped_state = _object(stopped.get("State"), "stopped failed launch state")
        restart = _object(_object(stopped.get("HostConfig"),
                                  "stopped failed launch host configuration").get(
                                      "RestartPolicy"), "stopped failed launch restart policy")
        if (stopped_state.get("Running") is not False
                or stopped_state.get("Status") not in ("created", "exited")
                or any(stopped_state.get(field) is not False for field in (
                    "Paused", "Restarting", "Dead", "OOMKilled"))
                or restart.get("Name") != "no"):
            raise ProviderRequestError("failed launch container stop was not verified")
        return container_id

    def _launch(self, spec: ContainerSpec, *, recover: bool,
                scope: _InventoryScope | None = None) -> ContainerHandle:
        if self._socket_attestor is None:
            raise ProviderRequestError("authenticated guest socket attestor is required")
        image_id = self._image_id(spec)
        workspace_source = self._volume(spec, "workspace", create=True)
        session_source = self._volume(spec, "session", create=True)
        sources = (workspace_source, session_source)
        predecessors = self._inventory(spec, sources, fence_owned=recover and scope is None,
                                       scope=scope, image_id=image_id)
        if scope is not None:
            predecessors = scope.stopped
        proof = self._socket_proof(spec)
        name = self._name(spec)
        argv = ["create", "--name", name]
        for key, value in self._labels(spec).items():
            argv.extend(("--label", f"{key}={value}"))
        argv.extend(SECURITY_ARGV)
        argv.extend(self._mount_args(spec))
        argv.append(spec.image_digest)
        container_id: str | None = None
        try:
            container_id = self._run(*argv).decode("ascii", "strict").strip()
            if _CONTAINER_ID.fullmatch(container_id) is None:
                container_id = None
                raise ProviderRequestError("Docker created an invalid container identity")
            created_digest = self._inspect(container_id, spec, name, image_id, sources)
            if self._socket_proof(spec) != proof:
                raise ProviderRequestError("guest socket object changed before start")
            if ((self._volume(spec, "workspace", create=False),
                 self._volume(spec, "session", create=False)) != sources):
                raise ProviderRequestError("container private volume changed before start")
            start_digest = self._inspect(container_id, spec, name, image_id, sources)
            if start_digest != created_digest:
                raise ProviderRequestError("container inspect changed before start")
            self._run("start", container_id)
            handle = ContainerHandle(container_id, start_digest,
                                     spec.workspace_volume, spec.session_volume)
            if recover:
                self._recovery = _RecoveryAssociation(spec, handle, predecessors)
            return handle
        except BaseException as primary:
            try:
                stopped_id = self._cleanup_failed_launch(spec, name, container_id,
                                                         image_id, sources)
            except BaseException as cleanup:
                primary.add_note(f"Failed launch cleanup could not verify {name} stopped; "
                                 f"retained resource possible: {cleanup}")
            else:
                primary.add_note(f"Failed launch cleanup verified {stopped_id} nonrunning; "
                                 "private volumes retained")
            raise

    def start(self, spec: ContainerSpec, *, scope: _InventoryScope | None = None) -> ContainerHandle:
        self._recovery = None
        self._validate_spec(spec)
        return self._launch(spec, recover=False, scope=scope)

    def recover(self, spec: ContainerSpec, *, scope: _InventoryScope | None = None) -> ContainerHandle:
        self._recovery = None
        self._validate_spec(spec)
        return self._launch(spec, recover=True, scope=scope)

    def attest_running(self, spec: ContainerSpec, handle: ContainerHandle, *,
                       scope: _InventoryScope | None = None) -> ContainerHandle:
        self._validate_spec(spec)
        if (not isinstance(handle, ContainerHandle)
                or not isinstance(handle.container_id, str)
                or _CONTAINER_ID.fullmatch(handle.container_id) is None
                or not isinstance(handle.inspect_sha256, str)
                or _SHA.fullmatch(handle.inspect_sha256) is None
                or (handle.workspace_volume, handle.session_volume) != (
                    spec.workspace_volume, spec.session_volume)):
            raise ProviderRequestError("container handle differs")
        if self._socket_attestor is None:
            raise ProviderRequestError("authenticated guest socket attestor is required")
        image_id = self._image_id(spec)
        sources = (self._volume(spec, "workspace", create=False),
                   self._volume(spec, "session", create=False))
        shared = {}
        observed = self._inspect(handle.container_id, spec, None, image_id, sources,
                                 expected_state="running", shared=shared)
        if observed != handle.inspect_sha256:
            raise ProviderRequestError("container security digest differs")
        predecessors = None
        if self._recovery is not None:
            if self._recovery.spec != spec or self._recovery.handle != handle:
                raise ProviderRequestError("container recovery association differs")
            predecessors = self._recovery.predecessors
        if scope is not None:
            if (type(scope) is not _InventoryScope or (spec, handle) not in scope.active
                    or any(dict(scope.stopped).get(cid) != digest for cid, digest in predecessors or ())):
                raise ProviderRequestError("container inventory scope changed launch association")
        self._inventory(spec, sources, fence_owned=False, exclude_id=handle.container_id,
                        predecessors=predecessors, scope=scope, shared=shared, image_id=image_id)
        self._socket_proof(spec)
        return handle
