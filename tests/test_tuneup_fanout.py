"""Tuneup lock timing follows the admitted review policy, not the old council."""
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "_tuneup_fanout_contracts", ROOT / "shared/skills/skill-tuneup/scripts/tuneup.py",
)
tuneup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tuneup)


def test_lock_window_tracks_actual_two_round_deep_review_and_policy_changes(tmp_path):
    runtime = ROOT / "shared/lib/fanout"
    planner = tmp_path / "plan.py"
    shutil.copy2(ROOT / "shared/skills/llm-fanout-plan/scripts/plan.py", planner)
    assert tuneup.review_window_minutes(runtime, planner) == 60
    planner.write_text(planner.read_text().replace('"rounds": 2,', '"rounds": 5,') + "\n# policy changed\n")
    assert tuneup.review_window_minutes(runtime, planner) == 150
    assert tuneup.LOCK_STALE_MIN < tuneup.review_window_minutes(runtime, planner)


@pytest.mark.parametrize("fault", ("none", "archive", "extract"))
def test_self_target_checked_archive_refuses_launch_on_either_failure(tmp_path, fault):
    repo = tmp_path / "repo"
    repo.mkdir()
    environment = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
                       GIT_CONFIG_NOSYSTEM="1", TMPDIR=str(tmp_path))
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", "HEAD"],
                             capture_output=True, check=True, env=environment).stdout
    tracked_tree = subprocess.run(["git", "-C", str(ROOT), "ls-tree", "-r", "-z", "HEAD"],
                                  capture_output=True, check=True, env=environment).stdout
    committed_blob_count = sum(
        entry.split(b" ", 2)[1] == b"blob" for entry in tracked_tree.split(b"\0") if entry
    )
    subprocess.run(["tar", "-x", "-C", str(repo)], input=archive, check=True, env=environment)
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=environment)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, env=environment)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture",
                    "-c", "user.email=fixture@example.invalid", "-c", "core.hooksPath=/dev/null",
                    "commit", "-qm", "complete source"], check=True, env=environment)
    (repo / "shared/lib/fanout/compiler.py").write_text("raise RuntimeError('dirty reviewer imported')\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    real_git, real_tar = shutil.which("git"), shutil.which("tar")
    if fault == "archive":
        wrapper = bin_dir / "git"
        wrapper.write_text(f'#!/bin/sh\nif [ "$3" = archive ]; then exec "{real_git}" -C "$2" archive missing-ref; fi\nexec "{real_git}" "$@"\n')
        wrapper.chmod(0o700)
    elif fault == "extract":
        wrapper = bin_dir / "tar"
        wrapper.write_text(f'#!/bin/sh\nexec "{real_tar}" "$@" -C "{tmp_path / "missing-destination"}"\n')
        wrapper.chmod(0o700)
    environment["PATH"] = str(bin_dir) + os.pathsep + environment["PATH"]
    doc = (ROOT / "shared/skills/skill-tuneup/references/self-target-rules.md").read_text()
    extraction = doc.split("```bash\n", 1)[1].split("\n```", 1)[0]
    marker = tmp_path / "reviewer-launched"
    script = 'KU="$1"\n' + extraction + '\nprintf "%s" "$REVIEW_KU" > "$2"\n'
    result = subprocess.run(["bash", "-c", script, "fixture", str(repo), str(marker)],
                            capture_output=True, text=True, env=environment)
    if fault != "none":
        assert result.returncode != 0
        assert not marker.exists(), "failed extraction reached reviewer assignment/launch"
        return
    assert result.returncode == 0, result.stderr
    extracted = Path(marker.read_text())
    # Every tracked source byte comes from the original commit, not the dirty modules.
    with tarfile.open(fileobj=io.BytesIO(archive)) as committed_tree:
        entries = [entry for entry in committed_tree if entry.isfile() or entry.issym()]
        assert len(entries) == committed_blob_count
        for entry in entries:
            want = os.fsencode(entry.linkname) if entry.issym() else committed_tree.extractfile(entry).read()
            leaf = extracted / entry.name
            actual = os.fsencode(os.readlink(leaf)) if leaf.is_symlink() else leaf.read_bytes()
            assert actual == want, entry.name
    skills = tmp_path / "skills"
    skills.mkdir()
    question = tmp_path / "question.txt"
    question.write_text("Review the supplied evidence independently.")
    planner = extracted / "shared/skills/llm-fanout-plan/scripts/plan.py"
    admitted = subprocess.run([sys.executable, str(planner), "from-question", "--question-file", str(question),
        "--approval", "invocation-authorized", "--tier", "deep", "--skill-root", str(skills),
        "--none-reason", "Supplied evidence is sufficient."], capture_output=True, text=True, env=environment)
    assert admitted.returncode == 0, admitted.stderr
    policy = json.loads(admitted.stdout)["compiled"]["plan"]["defaults"]
    assert policy["executor_ids"] == ["claude", "codex", "agy"]
    assert (policy["rounds"], policy["retries"], policy["minimum_success"]) == (2, 0, 2)


@pytest.mark.parametrize("outcome", ("complete", "below-quorum", "uncertain"))
def test_tuneup_review_rounds_keep_quorum_and_never_replay_possible_spend(tmp_path, outcome):
    import test_fanout_collaboration as cases
    runtime = cases.fanout
    executors = ("claude", "codex", "agy")
    store = runtime.ArtifactStore(tmp_path / "artifacts")
    packet = cases._packet(store, tmp_path, executors=executors, rounds=2, retries=0)
    seats = tuple(cases._seat(tmp_path, name) for name in executors)
    provider = cases._Provider(invalid={"codex", "agy"} if outcome == "below-quorum" else set())
    coordinator, journal, _, _ = cases._coordinator(tmp_path, packet, executors, provider=provider)
    attempts = []
    def turn(request, **kwargs):
        attempts.append((request.executor_id, request.resume))
        if outcome == "uncertain" and request.executor_id in {"codex", "agy"}:
            raise RuntimeError("possible synthetic spend")
        return provider(request, **kwargs)
    coordinator.provider_runner = turn
    policy = cases._round_policy(executor_ids=executors, rounds=2, retries=0)
    try:
        first = coordinator.execute_round(packet, seats, policy, round=1)
        assert len(attempts) == 3
        if outcome == "complete":
            assert first.status == "round-complete"
            second = coordinator.execute_round(packet, seats, policy, round=2, peer_source=first)
            assert second.status == "round-complete"
            answers = [coordinator.artifacts.read_bytes(terminal.answer_ref) for terminal in second.valid_terminals]
            assert len(answers) == len(set(answers)) == 3
            sources = {terminal.seat_id: terminal.answer_ref for terminal in second.valid_terminals}
            with pytest.raises(runtime.CandidateValidationError, match="distinct"):
                runtime.verify_answer_synthesis(coordinator.artifacts, run_id=packet.run_id,
                    task_id=packet.task_id, plan_sha256=packet.compiled_plan_sha256, plan_revision=1,
                    sources=sources, synthesizer_id="invoking-owner", answer=answers[0], checks=(),
                    controller=coordinator.lifecycle_controller)
            synthesis = runtime.verify_answer_synthesis(coordinator.artifacts, run_id=packet.run_id,
                task_id=packet.task_id, plan_sha256=packet.compiled_plan_sha256, plan_revision=1,
                sources=sources, synthesizer_id="invoking-owner", answer=b"New owner synthesis of independent evidence.",
                checks=(), controller=coordinator.lifecycle_controller)
            assert synthesis.valid and synthesis.synthesizer_id == "invoking-owner"
            assert coordinator.artifacts.read_bytes(synthesis.answer_ref) not in answers
            assert sorted(attempts) == sorted((provider, resumed) for provider in executors for resumed in (False, True))
            assert coordinator.execute_round(packet, seats, policy, round=2, recovery=second) == second
            assert len(attempts) == 6
        else:
            assert first.status == "failed-minimum"
            assert len(first.valid_terminals) == 1
            if outcome == "uncertain":
                with pytest.raises(runtime.CollaborationDurabilityError, match="uncertain"):
                    coordinator.execute_round(packet, seats, policy, round=1, recovery=first)
                assert {seat for (task, seat, attempt, round_), phase in journal.state.seat_phases.items()
                        if phase == "uncertain-attempt"} == {"seat-codex", "seat-agy"}
            else:
                assert coordinator.execute_round(packet, seats, policy, round=1, recovery=first).status == "failed-minimum"
            assert len(attempts) == 3
    finally:
        journal.close()
        coordinator.artifacts.close()
        store.close()


def test_tuneup_review_failed_authenticated_preflight_cannot_dispatch(tmp_path):
    import test_fanout_execute_skill as cases
    helper_spec = importlib.util.spec_from_file_location("_tuneup_execute", cases.CLI)
    helper = importlib.util.module_from_spec(helper_spec)
    sys.modules[helper_spec.name] = helper
    helper_spec.loader.exec_module(helper)
    packet, _ = cases._question_packet(tmp_path)
    repo = cases._repository(tmp_path)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="memory"):
        helper.prepare_run(packet, repo_root=repo, run_root=tmp_path / "run",
            authority_root=tmp_path / "authority", private_root=private,
            skill_roots=(tmp_path / "skills",), budget=6, runtime=cases.fanout,
            registry=cases._registry(), memory_factory=cases._NeverMemory,
            memory_preflight=lambda: False,
            provider_runner=lambda *_args, **_kwargs: pytest.fail("failed preflight dispatched"))
    assert not (tmp_path / "run").exists() and not (tmp_path / "authority").exists()
    assert list(private.iterdir()) == []
