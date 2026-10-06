"""Complete neighbor validation with bounded actual multi-object Docker observations."""
import base64
import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.lib.fanout.container_runtime import ContainerLauncher, RuntimeTranscript
from shared.lib.fanout.errors import ProviderRequestError
from test_fanout_container_runtime import FakeDocker, _foreign_orphan, spec


def _history(spec, count=73):
    docker = FakeDocker(spec)
    ids = [_foreign_orphan(docker, spec) for _ in range(count)]
    return docker, ids


def _inventory(launcher, spec):
    return launcher._inventory(spec, ("/workspace-source", "/session-source"), fence_owned=False)


def test_complete_73_neighbor_inventory_uses_ten_bounded_inspects(spec):
    docker, ids = _history(spec)
    assert _inventory(ContainerLauncher(docker), spec) == ()
    inspect = [argv[6:] for argv in docker.calls if argv[3:6] == ("inspect", "--type", "container")]
    assert len(inspect) == 10
    assert [len(batch) for batch in inspect] == [8] * 9 + [1]
    assert [cid for batch in inspect for cid in batch] == ids
    assert docker.events == ["inventory"]


@pytest.mark.parametrize("conflict", ["seat", "workspace", "session", "socket"])
def test_last_neighbor_in_73_history_still_denies_foreign_resource_conflict(spec, conflict):
    docker, _ = _history(spec, 72)
    _foreign_orphan(docker, spec, conflict=conflict)
    with pytest.raises(ProviderRequestError, match="foreign.*conflict"):
        _inventory(ContainerLauncher(docker), spec)
    assert "create" not in docker.events


@pytest.mark.parametrize("changed", ["missing", "duplicate", "extra", "reorder", "nonobject", "invalid-json", "oversized"])
def test_inventory_rejects_incomplete_or_ambiguous_batch_observations(spec, changed):
    docker, _ = _history(spec, 8)
    def run(argv, stdin):
        result = docker(argv, stdin)
        if argv[3:6] != ("inspect", "--type", "container"):
            return result
        items = json.loads(result.stdout)
        if changed == "missing":
            items.pop()
        elif changed == "duplicate":
            items[-1] = copy.deepcopy(items[0])
        elif changed == "extra":
            items.append(copy.deepcopy(items[0]))
        elif changed == "reorder":
            items.reverse()
        elif changed == "nonobject":
            items[0] = None
        raw = (b"{" if changed == "invalid-json" else b" " * (1024 * 1024 + 1)
               if changed == "oversized" else json.dumps(items).encode())
        return subprocess.CompletedProcess((), 0, raw, b"")
    with pytest.raises(ProviderRequestError):
        _inventory(ContainerLauncher(run), spec)


def test_batched_runtime_transcript_replays_actual_argv_and_full_raw_outputs(spec):
    docker, ids = _history(spec)
    transcript = RuntimeTranscript()
    def run(argv, stdin):
        result = docker(argv, stdin)
        transcript.append(argv[3:], result.stdout)
        return result
    assert _inventory(ContainerLauncher(run), spec) == ()
    batches = transcript.observations[1:]
    assert len(batches) == 10
    assert batches[0]["argv"] == ["inspect", "--type", "container", *ids[:8]]
    assert len(json.loads(base64.b64decode(batches[0]["stdout"]))) == 8
    replay = RuntimeTranscript(transcript.observations)
    def retained(argv, stdin):
        return subprocess.CompletedProcess((), 0, replay.replay(*argv[3:]), b"")
    assert _inventory(ContainerLauncher(retained), spec) == ()
    replay.require_complete()


@pytest.mark.parametrize("changed", ["missing", "reordered", "extra"])
def test_batched_runtime_transcript_refuses_incomplete_or_reordered_evidence(spec, changed):
    docker, _ = _history(spec, 9)
    transcript = RuntimeTranscript()
    def run(argv, stdin):
        result = docker(argv, stdin)
        transcript.append(argv[3:], result.stdout)
        return result
    _inventory(ContainerLauncher(run), spec)
    observations = copy.deepcopy(transcript.observations)
    if changed == "missing":
        observations.pop()
    elif changed == "reordered":
        observations[1], observations[2] = observations[2], observations[1]
    else:
        observations.append({"argv": ["unexpected"], "stdout": ""})
    replay = RuntimeTranscript(observations)
    def retained(argv, stdin):
        return subprocess.CompletedProcess((), 0, replay.replay(*argv[3:]), b"")
    with pytest.raises(ProviderRequestError):
        _inventory(ContainerLauncher(retained), spec)
        replay.require_complete()
