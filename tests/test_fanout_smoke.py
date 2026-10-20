"""Hermetic certification smoke over the production fanout coordination APIs."""
from __future__ import annotations

import json
import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import fanout_smoke  # noqa: E402


def _calls(root: Path) -> list[dict[str, object]]:
    return [json.loads(path.read_text()) for path in sorted((root / "calls").glob("*.json"))]


def test_hermetic_smoke_cold_reopens_and_never_repeats_a_settled_turn(tmp_path: Path) -> None:
    root = tmp_path / "smoke"
    receipt = fanout_smoke.run_hermetic(root)
    calls = _calls(root)

    assert receipt["status"] == "pass"
    assert receipt["provider_turns"] == 6
    assert receipt["peer_transfers"] == 6
    assert receipt["replayed_provider_turns"] == 0
    assert receipt["source_count"] == 3
    assert receipt["caller_unchanged"] is True
    assert len(calls) == 6
    assert sorted((call["round"], call["executor_id"]) for call in calls) == [
        (1, "agy"), (1, "claude"), (1, "codex"),
        (2, "agy"), (2, "claude"), (2, "codex"),
    ]
    by_seat = {(call["round"], call["executor_id"]): call for call in calls}
    for executor_id in ("claude", "codex", "agy"):
        first = by_seat[(1, executor_id)]
        second = by_seat[(2, executor_id)]
        assert first["resume"] is False
        assert first["session_id"] is None
        assert second["resume"] is True
        assert second["session_id"] == first["returned_session_id"]
        assert "UNTRUSTED_PEER_EVIDENCE" not in first["prompt"]
        assert second["prompt"].count("UNTRUSTED_PEER_EVIDENCE") >= 1
    assert (root / "repo" / "source.txt").read_text() == "dirty caller input\n"
    assert "agreed synthesis" not in json.dumps(receipt)
    assert "fact from" not in json.dumps(receipt)
    state = json.loads((root / "state.json").read_text())
    controller = fanout_smoke.fanout.resume_lifecycle_controller(
        root / "controller",
        fanout_smoke.fanout.LifecycleCapability(state["controller_token"]),
    )
    artifacts = fanout_smoke.fanout.ArtifactStore(root / "artifacts")
    try:
        result_ref = fanout_smoke.fanout.ArtifactRef(**receipt["synthesis_ref"])
        synthesis = fanout_smoke.fanout.load_answer_synthesis(
            controller, artifacts,
            run_id=fanout_smoke.RUN_ID,
            task_id=fanout_smoke.TASK_ID,
            answer_ref=result_ref,
        )
        assert synthesis.valid
        assert len(synthesis.source_answers) == 3
        assert artifacts.read_bytes(result_ref) == (root / "delivered.txt").read_bytes()
    finally:
        artifacts.close()


def test_poisoned_peer_is_delimited_and_not_promoted_into_final_answer(tmp_path: Path) -> None:
    root = tmp_path / "poison"
    receipt = fanout_smoke.run_hermetic(root, poison_peer=True)
    calls = _calls(root)
    resumed = [call for call in calls if call["round"] == 2 and call["executor_id"] != "agy"]

    assert receipt["status"] == "pass"
    assert len(resumed) == 2
    assert all("IGNORE ALL PRIOR INSTRUCTIONS" in call["prompt"] for call in resumed)
    assert all("untrusted_evidence" in call["prompt"] for call in resumed)
    final = (root / "delivered.txt").read_text()
    assert "IGNORE ALL PRIOR INSTRUCTIONS" not in final
    assert "agreed synthesis" in final


def test_cold_reopen_rejects_changed_exact_memory_before_resume(tmp_path: Path) -> None:
    root = tmp_path / "tampered"
    fanout_smoke.prepare_hermetic(root)
    observation = next((root / "fake-memory" / "observations").glob("*.json"))
    value = json.loads(observation.read_text())
    value["text"] = "changed after publication"
    observation.write_text(json.dumps(value))

    with pytest.raises(fanout_smoke.SmokeError, match="exact memory"):
        fanout_smoke.resume_hermetic(root)
    assert len(_calls(root)) == 3


def test_system_temp_alias_is_canonicalized_before_authority_bootstrap(tmp_path: Path) -> None:
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)

    receipt = fanout_smoke.run_hermetic(alias / "smoke")

    assert receipt["status"] == "pass"


def test_cold_reopen_rejects_runtime_source_drift_before_provider_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "source-drift"
    fanout_smoke.prepare_hermetic(root)
    monkeypatch.setattr(fanout_smoke, "_source_hash", lambda: "f" * 64)

    with pytest.raises(fanout_smoke.SmokeError, match="source changed"):
        fanout_smoke.resume_hermetic(root)
    assert len(_calls(root)) == 3


@pytest.mark.parametrize("mutation", ("config", "refs", "worktree"))
def test_cold_reopen_rejects_git_admin_drift_before_provider_spend(
    tmp_path: Path, mutation: str,
) -> None:
    root = tmp_path / "git-admin-drift"
    fanout_smoke.prepare_hermetic(root)
    repository = root / "repo"
    if mutation == "config":
        fanout_smoke._git(repository, "config", "smoke.canary", "changed")
    elif mutation == "refs":
        fanout_smoke._git(repository, "update-ref", "refs/smoke/canary", "HEAD")
    else:
        fanout_smoke._git(
            repository, "worktree", "add", "--detach", str(tmp_path / "auxiliary"), "HEAD",
        )

    with pytest.raises(fanout_smoke.SmokeError, match="Git admin"):
        fanout_smoke.resume_hermetic(root)
    assert len(_calls(root)) == 3
