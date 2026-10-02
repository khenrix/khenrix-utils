"""Admission tests for the dedicated no-host-mount Colima profile."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.lib.fanout.container_vm import (  # noqa: E402
    START_ARGV,
    VMProfileSpec,
    parse_profile_config,
    require_vm_probe,
)
from shared.lib.fanout.errors import ProviderRequestError  # noqa: E402
from scripts import fanout_container_vm_probe as live_probe  # noqa: E402


SPEC = VMProfileSpec("fanout-isolated", "colima-fanout-isolated", "x86_64")
ENDPOINT = f"unix://{Path.home()}/.colima/fanout-isolated/docker.sock"
NONCE = b"controller-issued-32-byte-nonce-test"
HOST_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGZha2UtZ3Vlc3QtaG9zdC1rZXktZm9yLXRlc3Q="
OBSERVED = {
    "mounts": None,
    "lima_mounts": [],
    "arch": "x86_64",
    "vm_type": "qemu",
    "docker_context": "colima-fanout-isolated",
}
STATIC_MOUNTS = [
    "proc /proc proc rw,nosuid,nodev,noexec,relatime 0 0",
    "tmpfs /run tmpfs rw,nosuid,nodev,size=1625260k 0 0",
    "/dev/vdb1 /var/lib/docker ext4 rw,relatime 0 0",
]
CONTAINER_ID = "a" * 64
SNAPSHOT_ROOT = "/var/lib/containerd/io.containerd.snapshotter.v1.overlayfs/snapshots"
OVERLAY_OPTIONS = (
    "rw,relatime,"
    f"lowerdir={SNAPSHOT_ROOT}/52/fs:{SNAPSHOT_ROOT}/39/fs,"
    f"upperdir={SNAPSHOT_ROOT}/53/fs,workdir={SNAPSHOT_ROOT}/53/work,nouserxattr"
)
NETNS_ROW = "nsfs /run/docker/netns/123456abcdef nsfs rw 0 0"


def _overlay_pair(container_id=CONTAINER_ID, options=OVERLAY_OPTIONS):
    return [
        f"overlay /var/lib/docker/rootfs/overlayfs/{container_id} overlay {options} 0 0",
        "overlay /mnt/lima-colima-fanout-isolated/docker/rootfs/overlayfs/"
        f"{container_id} overlay {options} 0 0",
    ]


def _probe_mounts(mounts):
    return require_vm_probe(
        SPEC, OBSERVED, mounts, ENDPOINT, NONCE, NONCE,
        ssh_host_key=HOST_KEY, pinned_host_key=HOST_KEY,
    )


def test_explicit_empty_mount_list_is_rejected_even_without_guest_mounts():
    with pytest.raises(ProviderRequestError, match="host mount"):
        require_vm_probe(SPEC, {**OBSERVED, "mounts": []}, [], ENDPOINT, NONCE, NONCE)


def test_guest_host_mount_is_rejected_despite_nil_profile_mounts():
    with pytest.raises(ProviderRequestError, match="host mount"):
        require_vm_probe(SPEC, {**OBSERVED}, ["/Users:/Users"], ENDPOINT, NONCE, NONCE)


@pytest.mark.parametrize("root", ["/Users", "/Volumes", "/private/var", "/host_mnt"])
@pytest.mark.parametrize("row", [
    "{path} /mnt/guest ext4 rw 0 0",
    "/dev/vdb1 {path} ext4 rw 0 0",
    "/dev/vdb1 /mnt/guest ext4 rw,source={path} 0 0",
    "/dev/vdb1 /mnt/guest ext4 rw,source={path},nodev 0 0",
    "{path}\t/mnt/guest ext4 rw 0 0",
    "/dev/vdb1\t{path} ext4 rw 0 0",
    "/dev/vdb1 /mnt/guest ext4 rw,source={path}\t0 0",
], ids=["source", "target", "option-last", "option-before-comma",
        "tab-after-source", "tab-before-target", "tab-after-option"])
@pytest.mark.parametrize("suffix", ["", "/child"], ids=["exact-root", "descendant"])
def test_host_root_and_descendants_are_rejected(root, row, suffix):
    with pytest.raises(ProviderRequestError, match="host mount") as error:
        _probe_mounts([*STATIC_MOUNTS, row.format(path=root + suffix)])
    assert root not in str(error.value)


@pytest.mark.parametrize("root", ["/Users", "/Volumes", "/private/var", "/host_mnt"])
@pytest.mark.parametrize("row", [
    "{path} /mnt/guest ext4 rw 0 0",
    "/dev/vdb1 {path} ext4 rw 0 0",
    "/dev/vdb1 /mnt/guest ext4 rw,source={path} 0 0",
], ids=["source", "target", "option"])
def test_unrelated_guest_path_name_is_not_a_host_root(root, row):
    near_miss = row.format(path=root + "-guest")
    assert _probe_mounts([*STATIC_MOUNTS, near_miss]).guest_mounts_sha256 != (
        _probe_mounts(STATIC_MOUNTS).guest_mounts_sha256
    )


@pytest.mark.parametrize("fs_type", ["nfs4", "mysteryfs"])
def test_network_or_unknown_guest_mount_type_is_rejected(fs_type):
    with pytest.raises(ProviderRequestError, match="host mount"):
        require_vm_probe(SPEC, OBSERVED,
                         [f"server:/share /mnt/share {fs_type} rw 0 0"],
                         ENDPOINT, NONCE, NONCE)


def test_generated_lima_host_mount_is_rejected():
    with pytest.raises(ProviderRequestError, match="host mount"):
        require_vm_probe(
            SPEC, {**OBSERVED, "lima_mounts": [{"location": "~"}]},
            [], ENDPOINT, NONCE, NONCE,
        )


@pytest.mark.parametrize("endpoint", [
    "unix:///tmp/other.sock",
    f"unix://{Path.home()}/.colima/default/docker.sock",
    f"unix://{Path.home()}/.colima/maka-amd64/docker.sock",
])
def test_other_docker_endpoint_is_rejected(endpoint):
    with pytest.raises(ProviderRequestError, match="Docker endpoint"):
        require_vm_probe(SPEC, OBSERVED, [], endpoint, NONCE, NONCE)


@pytest.mark.parametrize("context", ["default", "colima-maka-amd64"])
def test_other_docker_context_is_rejected(context):
    with pytest.raises(ProviderRequestError, match="Docker context"):
        require_vm_probe(SPEC, {**OBSERVED, "docker_context": context},
                         [], ENDPOINT, NONCE, NONCE)


def test_ssh_duplex_reply_must_match_challenge():
    with pytest.raises(ProviderRequestError, match="stdio nonce"):
        require_vm_probe(SPEC, OBSERVED, ["proc /proc proc rw 0 0"],
                         ENDPOINT, NONCE, b"wrong")


def test_no_mount_profile_returns_observation_digests():
    probe = require_vm_probe(SPEC, OBSERVED, [
        "proc /proc proc rw,nosuid,nodev,noexec,relatime 0 0",
        "tmpfs /run tmpfs rw,nosuid,nodev 0 0",
    ], ENDPOINT, NONCE, NONCE, ssh_host_key=HOST_KEY,
    pinned_host_key=HOST_KEY)
    assert probe.profile == "fanout-isolated"
    assert probe.docker_endpoint == ENDPOINT
    assert len(probe.effective_mounts_sha256) == 64
    assert len(probe.guest_mounts_sha256) == 64
    assert len(probe.stdio_roundtrip_sha256) == 64
    assert len(probe.ssh_host_key_sha256) == 64
    assert len(probe.digest) == 64


def test_valid_docker_runtime_rows_keep_static_digest_stable():
    before = _probe_mounts(STATIC_MOUNTS)
    running = _probe_mounts([
        *STATIC_MOUNTS[:2], *_overlay_pair(), NETNS_ROW, STATIC_MOUNTS[2],
    ])
    assert running.guest_mounts_sha256 == before.guest_mounts_sha256
    assert running.effective_mounts_sha256 == before.effective_mounts_sha256
    assert running.ssh_host_key_sha256 == before.ssh_host_key_sha256


def test_static_mount_change_changes_digest_even_with_valid_runtime_rows():
    before = _probe_mounts(STATIC_MOUNTS)
    changed = _probe_mounts([
        *STATIC_MOUNTS[:2], *_overlay_pair(), NETNS_ROW,
        "/dev/vdb1 /var/lib/docker ext4 ro,relatime 0 0",
    ])
    assert changed.guest_mounts_sha256 != before.guest_mounts_sha256


def test_two_complete_runtime_pairs_and_two_netns_rows_are_admitted():
    second_id = "b" * 64
    second_netns = "nsfs /run/docker/netns/abcdef123456 nsfs rw 0 0"
    before = _probe_mounts(STATIC_MOUNTS)
    running = _probe_mounts([
        *STATIC_MOUNTS, *_overlay_pair(), *_overlay_pair(second_id),
        NETNS_ROW, second_netns,
    ])
    assert running.guest_mounts_sha256 == before.guest_mounts_sha256


@pytest.mark.parametrize("bad_row", [
    _overlay_pair()[0].rsplit(" ", 1)[0],
    _overlay_pair()[0] + " 0",
    _overlay_pair()[0][:-3] + "1 0",
    _overlay_pair()[0][:-3] + "0 1",
    _overlay_pair()[0].replace("overlay ", "/dev/vdb1 ", 1),
    _overlay_pair()[0].replace(" overlay ", " Overlay ", 1),
    _overlay_pair()[0].replace("/var/lib/docker/rootfs/overlayfs/", "/tmp/overlayfs/", 1),
    _overlay_pair()[0].replace(CONTAINER_ID, CONTAINER_ID.upper(), 1),
    _overlay_pair()[0].replace(CONTAINER_ID, CONTAINER_ID[:12], 1),
    _overlay_pair()[0].replace(CONTAINER_ID, f"../{CONTAINER_ID}", 1),
    _overlay_pair()[0].replace(CONTAINER_ID, f"{CONTAINER_ID}\\040escape", 1),
    _overlay_pair()[0].replace("lowerdir=" + SNAPSHOT_ROOT + "/52/fs:", "lowerdir=:", 1),
    _overlay_pair()[0].replace("lowerdir=" + SNAPSHOT_ROOT + "/52/fs:", "lowerdir=/tmp/lower:", 1),
    _overlay_pair()[0].replace("/52/fs:", "/../fs:", 1),
    _overlay_pair()[0].replace("/52/fs:", "/x/fs:", 1),
    _overlay_pair()[0].replace("/52/fs:", "/52/work:", 1),
    _overlay_pair()[0].replace("/39/fs,upperdir", ":,upperdir", 1),
    _overlay_pair()[0].replace("upperdir=" + SNAPSHOT_ROOT + "/53/fs", "upperdir=/tmp/upper", 1),
    _overlay_pair()[0].replace(
        "workdir=" + SNAPSHOT_ROOT + "/53/work",
        "workdir=" + SNAPSHOT_ROOT + "/54/work", 1),
    _overlay_pair()[0].replace("/53/work,nouserxattr", "/53/fs,nouserxattr", 1),
    _overlay_pair()[0].replace(",nouserxattr", "", 1),
    _overlay_pair()[0].replace(",nouserxattr", ",nouserxattr,unsafe", 1),
    _overlay_pair()[0].replace("rw,relatime,", "ro,relatime,", 1),
    _overlay_pair()[0].replace("rw,relatime,", "relatime,rw,", 1),
], ids=[
    "missing-field", "extra-field", "nonzero-dump", "nonzero-pass",
    "wrong-source", "wrong-type", "wrong-root", "uppercase-id", "short-id",
    "traversal", "escaped-id", "empty-lower-entry", "outside-lower",
    "traversal-lower", "nondigit-snapshot", "wrong-lower-leaf", "empty-last-lower",
    "outside-upper", "mismatched-work-snapshot", "wrong-work-leaf",
    "missing-nouserxattr", "extra-option", "readonly", "reordered-options",
])
def test_overlay_lookalike_is_rejected(bad_row):
    with pytest.raises(ProviderRequestError):
        _probe_mounts([*STATIC_MOUNTS, bad_row, _overlay_pair()[1]])


@pytest.mark.parametrize("rows", [
    [_overlay_pair()[0]],
    [_overlay_pair()[0], _overlay_pair()[0]],
    [_overlay_pair()[0], _overlay_pair("b" * 64)[1]],
    [_overlay_pair()[0], _overlay_pair()[1].replace(
        OVERLAY_OPTIONS, OVERLAY_OPTIONS.replace("/52/fs", "/51/fs", 1))],
    [_overlay_pair()[0], _overlay_pair()[1].replace(
        "lima-colima-fanout-isolated", "lima-colima-default")],
], ids=["missing-mirror", "duplicate-root", "mismatched-full-id",
        "mismatched-options", "alternate-profile"])
def test_incomplete_or_mismatched_overlay_pair_is_rejected(rows):
    with pytest.raises(ProviderRequestError):
        _probe_mounts([*STATIC_MOUNTS, *rows])


@pytest.mark.parametrize("netns_row", [
    NETNS_ROW.replace("nsfs ", "overlay ", 1),
    NETNS_ROW.replace(" nsfs ", " ext4 ", 1),
    NETNS_ROW.replace("/run/docker/netns/", "/run/other/netns/"),
    NETNS_ROW.replace("123456abcdef", "123456abcdeF"),
    NETNS_ROW.replace("123456abcdef", "123456abcde"),
    NETNS_ROW.replace(" rw ", " ro "),
    NETNS_ROW.rsplit(" ", 1)[0],
    NETNS_ROW + " 0",
    NETNS_ROW[:-3] + "1 0",
    NETNS_ROW[:-3] + "0 1",
], ids=["wrong-source", "wrong-type", "wrong-root", "uppercase-id",
        "short-id", "wrong-options", "missing-field", "extra-field",
        "nonzero-dump", "nonzero-pass"])
def test_netns_lookalike_is_rejected(netns_row):
    with pytest.raises(ProviderRequestError):
        _probe_mounts([*STATIC_MOUNTS, *_overlay_pair(), netns_row])


def test_orphan_duplicate_and_excess_netns_rows_are_rejected():
    for rows in ([NETNS_ROW],
                 [*_overlay_pair(), NETNS_ROW, NETNS_ROW],
                 [*_overlay_pair(), NETNS_ROW,
                  "nsfs /run/docker/netns/abcdef123456 nsfs rw 0 0"]):
        with pytest.raises(ProviderRequestError):
            _probe_mounts([*STATIC_MOUNTS, *rows])


def test_dynamic_looking_static_filesystem_is_rejected():
    root = f"/var/lib/docker/rootfs/overlayfs/{CONTAINER_ID}"
    with pytest.raises(ProviderRequestError):
        _probe_mounts([*STATIC_MOUNTS, f"/dev/vdb1 {root} ext4 rw 0 0"])
    with pytest.raises(ProviderRequestError):
        _probe_mounts([*STATIC_MOUNTS, "tmpfs /run/docker/netns/123456abcdef tmpfs rw 0 0"])


def test_host_path_in_runtime_options_is_rejected_without_echoing_path():
    sensitive = "/Users/private-project"
    bad = _overlay_pair()[0].replace(f"{SNAPSHOT_ROOT}/52/fs", sensitive, 1)
    with pytest.raises(ProviderRequestError) as error:
        _probe_mounts([*STATIC_MOUNTS, bad, _overlay_pair()[1]])
    assert sensitive not in str(error.value)


def test_mount_table_row_pair_and_line_bounds_are_enforced():
    with pytest.raises(ProviderRequestError):
        _probe_mounts([STATIC_MOUNTS[0]] * 1025)
    with pytest.raises(ProviderRequestError):
        _probe_mounts([STATIC_MOUNTS[0] + "x" * 16385])
    rows = [row for index in range(129) for row in _overlay_pair(f"{index:064x}")]
    with pytest.raises(ProviderRequestError):
        _probe_mounts([*STATIC_MOUNTS, *rows])


@pytest.mark.parametrize("pinned", [None,
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIG90aGVyLWtleS1tYXRlcmlhbC0xMjM0NTY="])
def test_unknown_or_changed_ssh_host_key_is_rejected(pinned):
    with pytest.raises(ProviderRequestError, match="SSH host key"):
        require_vm_probe(
            SPEC, OBSERVED, ["proc /proc proc rw 0 0"], ENDPOINT, NONCE, NONCE,
            ssh_host_key=HOST_KEY, pinned_host_key=pinned,
        )


def test_start_command_keeps_dedicated_profile_inactive_and_unmounted():
    assert START_ARGV == (
        "colima", "start", "-p", "fanout-isolated", "--arch", "x86_64",
        "--vm-type", "qemu", "--cpus", "4", "--memory", "8",
        "--disk", "60", "--mount", "none", "--activate=false",
        "--ssh-agent=false", "--ssh-config=false", "--port-forwarder", "none",
        "--template=false",
    )


def test_effective_config_parser_preserves_nil_and_generated_mount_entries():
    colima = "arch: x86_64\nvmType: qemu\nmounts: null\n"
    lima = "arch: x86_64\nmounts:\n    - location: /Users/example\n"
    observed = parse_profile_config(colima, lima, "colima-fanout-isolated")
    assert observed == {
        "mounts": None,
        "lima_mounts": [{"location": "/Users/example"}],
        "arch": "x86_64",
        "vm_type": "qemu",
        "docker_context": "colima-fanout-isolated",
    }


def test_generated_lima_config_without_mount_key_means_no_declared_share():
    colima = "arch: x86_64\nvmType: qemu\nmounts: null\n"
    lima = "vmType: qemu\narch: x86_64\nmountType: reverse-sshfs\n"
    observed = parse_profile_config(colima, lima, "colima-fanout-isolated")
    assert observed["lima_mounts"] == []
    probe = require_vm_probe(
        SPEC, observed, ["/dev/vdb1 /var/lib/docker ext4 rw,relatime 0 0"],
        ENDPOINT, NONCE, NONCE, ssh_host_key=HOST_KEY,
        pinned_host_key=HOST_KEY,
    )
    assert probe.profile == "fanout-isolated"


def test_unowned_lima_instance_is_rejected_before_claim(tmp_path):
    lima = tmp_path / ".colima/_lima/colima-fanout-isolated"
    lima.mkdir(parents=True)
    with pytest.raises(ProviderRequestError, match="existing.*Lima"):
        live_probe._claim_profile(tmp_path, set())
    assert not (tmp_path / ".colima/.fanout-isolated-fanout-owner.json").exists()


def test_unowned_docker_context_is_rejected_before_claim(tmp_path):
    (tmp_path / ".colima").mkdir()
    with pytest.raises(ProviderRequestError, match="existing.*Docker context"):
        live_probe._claim_profile(tmp_path, {"colima-fanout-isolated"})
    assert not (tmp_path / ".colima/.fanout-isolated-fanout-owner.json").exists()


def test_evidence_output_must_be_private_tmp(tmp_path):
    with pytest.raises(ValueError, match="private /private/tmp"):
        evidence = live_probe.Evidence(tmp_path / "raw.json")
        evidence.close()


def test_neighbor_snapshot_detects_config_or_identity_drift(tmp_path):
    colima_home = tmp_path / ".colima"
    for name, lima_name in (("default", "colima"),
                            ("maka-amd64", "colima-maka-amd64")):
        profile = colima_home / name
        profile.mkdir(parents=True)
        (profile / "colima.yaml").write_text(f"profile: {name}\n")
        lima = colima_home / "_lima" / lima_name
        lima.mkdir(parents=True)
        (lima / "lima.yaml").write_text(f"instance: {lima_name}\n")
    before_list = (
        '{"name":"default","status":"Broken","arch":"aarch64"}\n'
        '{"name":"maka-amd64","status":"Broken","arch":"x86_64"}\n'
    )
    before = live_probe._neighbor_snapshot(tmp_path, before_list)
    (colima_home / "maka-amd64" / "colima.yaml").write_text("changed: true\n")
    after = live_probe._neighbor_snapshot(tmp_path, before_list)
    assert before != after
    assert before["default"] == after["default"]
    changed_list = before_list.replace('"status":"Broken"', '"status":"Running"', 1)
    assert live_probe._neighbor_snapshot(tmp_path, changed_list) != after


def test_neighbor_or_active_context_drift_blocks_pass():
    before = {"default": {"identity": "before", "config_sha256": ["a", "b"]}}
    changed = {"default": {"identity": "after", "config_sha256": ["a", "b"]}}
    with pytest.raises(ProviderRequestError, match="neighbor profile drift"):
        live_probe._require_neighbor_stability(before, changed, "default", "default")
    with pytest.raises(ProviderRequestError, match="active Docker context"):
        live_probe._require_neighbor_stability(before, before, "default",
                                               "colima-fanout-isolated")


def test_stale_binding_rejects_replaced_lima_instance_before_start(tmp_path):
    colima_home = tmp_path / ".colima"
    profile = colima_home / "fanout-isolated"
    profile.mkdir(parents=True)
    (profile / "colima.yaml").write_text("mounts: null\n")
    lima = colima_home / "_lima/colima-fanout-isolated"
    lima.mkdir(parents=True)
    (lima / "lima.yaml").write_text("mountType: reverse-sshfs\n")
    (lima / "disk").write_bytes(b"guest disk")
    metadata = tmp_path / ".docker/contexts/meta/context-id"
    metadata.mkdir(parents=True)
    (metadata / "meta.json").write_text('{"Name":"colima-fanout-isolated"}')
    context = {
        "Name": "colima-fanout-isolated",
        "Endpoints": {"docker": {"Host":
            f"unix://{tmp_path}/.colima/fanout-isolated/docker.sock"}},
        "Storage": {"MetadataPath": str(metadata)},
    }
    before = live_probe._profile_identity(tmp_path, context, "a" * 64)
    lima.rename(lima.with_name("colima-fanout-isolated-old"))
    lima.mkdir()
    (lima / "lima.yaml").write_text("mountType: reverse-sshfs\n")
    (lima / "disk").write_bytes(b"replacement guest disk")
    after = live_probe._profile_identity(tmp_path, context, "a" * 64)
    with pytest.raises(ProviderRequestError, match="profile identity"):
        live_probe._require_profile_binding(before, after)


def test_missing_or_mismatched_sidecar_blocks_before_start():
    observed = {"profile": "fanout-isolated", "identity": "present"}
    with pytest.raises(ProviderRequestError, match="profile identity"):
        live_probe._require_profile_binding(None, observed)
    with pytest.raises(ProviderRequestError, match="profile identity"):
        live_probe._require_profile_binding(
            {"profile": "fanout-isolated", "identity": "old"}, observed)


def test_v1_marker_requires_explicit_live_migration_before_start():
    with pytest.raises(ProviderRequestError, match="explicit.*migration"):
        live_probe._prestart_mode(marker_created=False, binding_present=False,
                                  migrate_v1=False, vm_running=True)
    with pytest.raises(ProviderRequestError, match="running"):
        live_probe._prestart_mode(marker_created=False, binding_present=False,
                                  migrate_v1=True, vm_running=False)
    assert live_probe._prestart_mode(marker_created=False, binding_present=False,
                                     migrate_v1=True, vm_running=True) == "migrate"


def _prestart_evidence(monkeypatch, tmp_path):
    calls = []

    def run(argv, **_):
        calls.append(argv)
        if argv[:3] == ("docker", "context", "ls"):
            return "default\ncolima-fanout-isolated\n"
        if argv == ("colima", "list", "--json"):
            return ""
        if argv == ("docker", "context", "show"):
            return "default\n"
        if argv[:3] == ("docker", "context", "inspect"):
            return '{"Name":"colima-fanout-isolated"}'
        if argv == ("colima", "status", "-p", "fanout-isolated", "--json"):
            raise ProviderRequestError("not running")
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(live_probe.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(live_probe, "_neighbor_snapshot", lambda *_: {})
    monkeypatch.setattr(live_probe, "_claim_profile",
                        lambda *_: (tmp_path / "profile", tmp_path / "marker", False))
    evidence = SimpleNamespace(data={}, save=lambda: None, run=run)
    return evidence, calls


def test_stopped_v1_migration_blocks_without_start(monkeypatch, tmp_path):
    evidence, calls = _prestart_evidence(monkeypatch, tmp_path)
    monkeypatch.setattr(live_probe, "_read_binding", lambda *_: None)
    with pytest.raises(ProviderRequestError, match="already running"):
        live_probe._probe(evidence, migrate_v1=True)
    assert START_ARGV not in calls


def test_mismatched_sidecar_blocks_without_start(monkeypatch, tmp_path):
    evidence, calls = _prestart_evidence(monkeypatch, tmp_path)
    monkeypatch.setattr(live_probe, "_read_binding", lambda *_: {"identity": "old"})
    monkeypatch.setattr(live_probe, "_read_pinned_host_key", lambda *_: HOST_KEY)
    monkeypatch.setattr(live_probe, "_profile_identity", lambda *_: {"identity": "new"})
    with pytest.raises(ProviderRequestError, match="profile identity"):
        live_probe._probe(evidence)
    assert START_ARGV not in calls


def test_strict_ssh_argv_pins_server_identity_without_controlmaster():
    config = (
        "Host lima-colima-fanout-isolated\n"
        f"  IdentityFile \"{Path.home()}/.colima/_lima/_config/user\"\n"
        "  Hostname 127.0.0.1\n  User lima\n  Port 52345\n"
    )
    known_hosts = Path.home() / ".colima/.fanout-isolated-known_hosts"
    argv = live_probe._strict_ssh_argv(config, known_hosts,
                                       ("python3", "--version"))
    assert argv == (
        "ssh", "-F", "/dev/null", "-T", "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={known_hosts}",
        "-o", "GlobalKnownHostsFile=/dev/null",
        "-o", "NoHostAuthenticationForLocalhost=no",
        "-o", "HostKeyAlias=fanout-isolated",
        "-o", "HostKeyAlgorithms=ssh-ed25519", "-o", "CheckHostIP=no",
        "-o", "ControlMaster=no",
        "-o", "ControlPath=none", "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes", "-o", "PreferredAuthentications=publickey",
        "-o", "ForwardAgent=no", "-i",
        str(Path.home() / ".colima/_lima/_config/user"),
        "-p", "52345", "lima@127.0.0.1", "python3", "--version",
    )


def test_strict_ssh_quotes_remote_python_code_as_one_argument():
    config = (
        f"IdentityFile \"{Path.home()}/.colima/_lima/_config/user\"\n"
        "Hostname 127.0.0.1\nUser lima\nPort 52345\n"
    )
    argv = live_probe._strict_ssh_argv(
        config, Path.home() / ".colima/.fanout-isolated-known_hosts",
        ("python3", "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"),
    )
    assert argv[-3:] == (
        "python3", "-c",
        "'import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())'",
    )


def test_first_host_key_enrollment_needs_matching_guest_key(tmp_path):
    (tmp_path / ".colima").mkdir()
    with pytest.raises(ProviderRequestError, match="SSH host key"):
        live_probe._admit_host_key(tmp_path, HOST_KEY, None)
    assert not (tmp_path / ".colima/.fanout-isolated-known_hosts").exists()
    pinned, path = live_probe._admit_host_key(tmp_path, HOST_KEY, HOST_KEY)
    assert pinned == HOST_KEY
    assert path.read_text() == f"fanout-isolated {HOST_KEY}\n"
    assert path.stat().st_mode & 0o077 == 0


def test_existing_host_key_pin_rejects_changed_server(tmp_path):
    (tmp_path / ".colima").mkdir()
    live_probe._admit_host_key(tmp_path, HOST_KEY, HOST_KEY)
    changed = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIG90aGVyLWtleS1tYXRlcmlhbC0xMjM0NTY="
    with pytest.raises(ProviderRequestError, match="SSH host key"):
        live_probe._admit_host_key(tmp_path, changed, changed)
