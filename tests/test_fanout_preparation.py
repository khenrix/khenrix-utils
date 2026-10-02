"""Local preparation retains exact evidence and never repeats an uncertain start."""
from __future__ import annotations

import dataclasses
import importlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from shared.lib.fanout.artifacts import ArtifactRef, ArtifactStore, canonical_json
from shared.lib.fanout.container_source import release_source_binding
from shared.lib.fanout.process import ProcessCommand
from shared.lib.fanout.runstate import LocalAnchorAuthority, RunJournal


@pytest.fixture
def prep():
    name = "shared.lib.fanout.preparation"
    assert importlib.util.find_spec(name), "reusable preparation API is missing"
    return importlib.import_module(name)


@pytest.fixture
def source_root(tmp_path):
    from shared.lib.fanout import container_source
    root = tmp_path / "source"
    for name in container_source._source_paths(ROOT) + container_source._memory_paths(ROOT):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    return root


def _recipe(prep, binding, *, limit=1024 * 1024, evidence=()):
    return prep.PreparationRecipe.from_bytes(canonical_json({
        "schema_version": "fanout-preparation-recipe-v1",
        "plan_sha256": "a" * 64, "release_binding": binding.to_dict(),
        "stages": [{"id": "source-binding", "timeout": 3, "max_output_bytes": limit}],
        "evidence": [dataclasses.asdict(ref) for ref in evidence],
    }))


@pytest.fixture
def run(prep, source_root, tmp_path, monkeypatch):
    artifacts = ArtifactStore(tmp_path / "artifacts")
    binding = release_source_binding(source_root)
    recipe = _recipe(prep, binding)
    inputs = prep.preparation_inputs("local-preparation", recipe, artifacts)
    authority = LocalAnchorAuthority.bootstrap(tmp_path / "authority", run_root=tmp_path / "journal", repo_root=source_root)
    journal, owner = RunJournal.create(tmp_path / "journal", inputs, anchor_store=authority)
    fixture = tmp_path / "fixture.py"
    fixture.write_text("import pathlib,sys\np=pathlib.Path(sys.argv[1])\np.write_bytes(p.read_bytes()+b'x' if p.exists() else b'x')\nsys.stdout.buffer.write(bytes(range(256))*1024)\nsys.stderr.buffer.write(b'complete stderr')\n")
    marker = tmp_path / "launches"

    def command(stage, binding, source, slots):
        return ProcessCommand(argv=(sys.executable, str(fixture), str(marker)), stdin=b"",
                              cwd=source, timeout=stage.timeout,
                              max_output_bytes=stage.max_output_bytes, slot_root=slots,
                              term_grace=0.01)

    monkeypatch.setattr(prep, "_stage_command", command)
    state = dict(artifacts=artifacts, binding=binding, recipe=recipe, inputs=inputs,
                 authority=authority, journal=journal, owner=owner, marker=marker,
                 fixture=fixture, source=source_root, root=tmp_path)
    yield state
    state["journal"].close()
    artifacts.close()


def _execute(prep, run):
    return prep.prepare_local(run["recipe"], source_root=run["source"],
                              artifacts=run["artifacts"], journal=run["journal"], owner=run["owner"])


def _reopen(run):
    run["journal"].close()
    run["journal"] = RunJournal.resume(run["root"] / "journal", run["inputs"], run["owner"], anchor_store=run["authority"])


def test_complete_binary_evidence_reopens_without_second_launch(prep, run):
    result = _execute(prep, run)
    assert result["status"] == "local-preparation-complete"
    assert result["certification"] is False
    summary = result["stages"][0]
    stdout = ArtifactRef(**summary["stdout_ref"])
    assert run["artifacts"].read_bytes(stdout) == bytes(range(256)) * 1024
    assert run["artifacts"].read_bytes(ArtifactRef(**summary["stderr_ref"])) == b"complete stderr"
    assert summary["pid"] > 0 and summary["returncode"] == 0
    _reopen(run)
    assert _execute(prep, run) == result
    assert run["marker"].read_bytes() == b"x"


@pytest.mark.parametrize("phase", ("dispatch-intent", "process-started"))
def test_interrupted_stage_is_uncertain_after_real_journal_reopen(prep, run, phase):
    run["journal"].append("dispatch-intent", task_id="preparation", seat_id="source-binding", attempt=1, round=1)
    if phase == "process-started":
        run["journal"].append(phase, task_id="preparation", seat_id="source-binding", attempt=1, round=1)
    _reopen(run)
    with pytest.raises(prep.PreparationError, match="uncertain|interrupted"):
        _execute(prep, run)
    assert not run["marker"].exists()
    assert run["journal"].state.seat_phase("preparation", "source-binding", 1, 1) == "uncertain-attempt"


@pytest.mark.parametrize("kind", ("runtime", "memory"))
def test_component_drift_refuses_before_launch(prep, run, kind):
    path = run["source"] / ("shared/lib/fanout/broker.py" if kind == "runtime" else "components/memory/memory_ingress.py")
    path.write_bytes(path.read_bytes() + b"\n# changed\n")
    with pytest.raises(prep.PreparationError, match="source|binding"):
        _execute(prep, run)
    assert not run["marker"].exists()


@pytest.mark.parametrize("damage", ("tampered", "truncated"))
def test_recovery_verifies_actual_raw_artifact_bytes(prep, run, damage):
    result = _execute(prep, run)
    ref = ArtifactRef(**result["stages"][0]["stdout_ref"])
    path = run["artifacts"].root / ref.path
    path.write_bytes(b"changed" if damage == "tampered" else path.read_bytes()[:-1])
    _reopen(run)
    with pytest.raises((prep.PreparationError, ValueError), match="artifact|digest|evidence"):
        _execute(prep, run)
    assert run["marker"].read_bytes() == b"x"


def test_recovery_refuses_missing_command_input_instead_of_recreating_it(prep, run):
    result = _execute(prep, run)
    command = run["artifacts"].read_json(ArtifactRef(**result["stages"][0]["command_ref"]))
    path = run["artifacts"].root / command["stdin_ref"]["path"]
    path.unlink()
    _reopen(run)
    with pytest.raises(prep.PreparationError, match="artifact|evidence"):
        _execute(prep, run)
    assert not path.exists()
    assert run["marker"].read_bytes() == b"x"


def test_opaque_evidence_is_preserved_without_decoding_and_verified_on_recovery(prep, run):
    raw = b"\xff\x00private opaque validation\xfe" * 4096
    ref = run["artifacts"].write_bytes("opaque/evidence.bin", raw)
    recipe = _recipe(prep, run["binding"], evidence=(ref,))
    run["journal"].close()
    inputs = prep.preparation_inputs("opaque-preparation", recipe, run["artifacts"])
    authority = LocalAnchorAuthority.bootstrap(run["root"] / "opaque-authority", run_root=run["root"] / "opaque-journal", repo_root=run["source"])
    run["journal"], run["owner"] = RunJournal.create(run["root"] / "opaque-journal", inputs, anchor_store=authority)
    run["recipe"] = recipe
    result = _execute(prep, run)
    assert result["evidence"] == [dataclasses.asdict(ref)]
    assert run["artifacts"].read_bytes(ref) == raw
    (run["artifacts"].root / ref.path).write_bytes(raw[:-1])
    with pytest.raises(prep.PreparationError, match="evidence"):
        _execute(prep, run)
    assert run["marker"].read_bytes() == b"x"


def test_changed_store_association_refuses_even_with_copied_artifacts(prep, run):
    _execute(prep, run)
    replacement = run["root"] / "other-artifacts"
    shutil.copytree(run["artifacts"].root, replacement)
    with ArtifactStore.open_existing(replacement) as other:
        with pytest.raises(prep.PreparationError, match="inputs|store|association"):
            prep.prepare_local(run["recipe"], source_root=run["source"], artifacts=other,
                               journal=run["journal"], owner=run["owner"])
    assert run["marker"].read_bytes() == b"x"


def test_unsafe_source_root_refuses_before_launch(prep, run):
    alias = run["root"] / "alias"
    alias.symlink_to(run["source"], target_is_directory=True)
    with pytest.raises(prep.PreparationError, match="root|unsafe|symlink"):
        prep.prepare_local(run["recipe"], source_root=alias, artifacts=run["artifacts"],
                           journal=run["journal"], owner=run["owner"])
    assert not run["marker"].exists()


def test_nonzero_child_is_retained_failed_and_never_repeated(prep, run):
    run["fixture"].write_text(run["fixture"].read_text() + "sys.exit(7)\n")
    result = _execute(prep, run)
    assert result["status"] == "local-preparation-failed"
    assert result["stages"][0]["returncode"] == 7
    _reopen(run)
    assert _execute(prep, run) == result
    assert run["marker"].read_bytes() == b"x"


def test_output_cap_retains_explicitly_incomplete_failure(prep, run):
    run["recipe"] = _recipe(prep, run["binding"], limit=1024)
    run["journal"].close()
    # A fresh independently anchored journal binds this distinct recipe.
    inputs = prep.preparation_inputs("capped-preparation", run["recipe"], run["artifacts"])
    authority = LocalAnchorAuthority.bootstrap(run["root"] / "cap-authority", run_root=run["root"] / "cap-journal", repo_root=run["source"])
    run["journal"], run["owner"] = RunJournal.create(run["root"] / "cap-journal", inputs, anchor_store=authority)
    result = _execute(prep, run)
    assert result["status"] == "local-preparation-failed"
    summary = result["stages"][0]
    assert summary["process_status"] == "output-limit" and summary["capture_complete"] is False
    assert run["artifacts"].read_bytes(ArtifactRef(**summary["stdout_ref"])) == bytes(range(256)) * 4


@pytest.mark.parametrize("payload", (b"{", b"x" * 65537, b'{"schema_version":"secret-canary","schema_version":"duplicate"}', b"[" * 2000 + b"]" * 2000),
                         ids=("malformed", "oversized", "duplicate", "deep"))
def test_malformed_or_oversized_recipe_has_no_content_diagnostic(prep, payload):
    with pytest.raises(prep.PreparationError) as error:
        prep.PreparationRecipe.from_bytes(payload)
    assert "secret-canary" not in str(error.value)


@pytest.mark.parametrize("mutation", ("argv", "provider", "too-many", "timeout", "cap"))
def test_recipe_cannot_expand_fixed_local_authority(prep, source_root, mutation):
    document = json.loads(_recipe(prep, release_source_binding(source_root)).to_bytes())
    stage = document["stages"][0]
    if mutation == "argv":
        stage["argv"] = [sys.executable, "-c", "print('must not execute')"]
    elif mutation == "provider":
        stage["id"] = "codex"
    elif mutation == "too-many":
        document["stages"] *= 100
    elif mutation == "timeout":
        stage["timeout"] = 1e30
    else:
        stage["max_output_bytes"] = 1e30
    with pytest.raises(prep.PreparationError):
        prep.PreparationRecipe.from_bytes(canonical_json(document))


def test_cli_validates_but_refuses_launch_without_capability_handoff(prep, source_root, tmp_path, capsys):
    sys.path.insert(0, str(ROOT / "scripts"))
    cli = importlib.import_module("fanout_prepare")
    path = tmp_path / "recipe.json"
    path.write_bytes(_recipe(prep, release_source_binding(source_root)).to_bytes())
    assert cli.main(["--recipe", str(path), "--source-root", str(source_root)]) == 1
    captured = capsys.readouterr()
    assert "owner" in captured.err and "handoff" in captured.err
    assert "owner_token" not in captured.out
    assert cli.main(["--recipe", str(path), "--source-root", str(source_root), "--validate-only"]) == 0


def test_production_stages_only_read_binding_and_python_identity(prep, source_root, tmp_path):
    binding = release_source_binding(source_root)
    recipe = prep.PreparationRecipe("a" * 64, binding, (
        prep.PreparationStage("source-binding"), prep.PreparationStage("host-runtime"),
    ))
    with ArtifactStore(tmp_path / "artifacts") as artifacts:
        inputs = prep.preparation_inputs("production-stages", recipe, artifacts)
        authority = LocalAnchorAuthority.bootstrap(tmp_path / "authority", run_root=tmp_path / "journal", repo_root=source_root)
        journal, owner = RunJournal.create(tmp_path / "journal", inputs, anchor_store=authority)
        with journal:
            result = prep.prepare_local(recipe, source_root=source_root, artifacts=artifacts,
                                        journal=journal, owner=owner)
        assert result["status"] == "local-preparation-complete"
        stages = result["stages"]
        assert json.loads(artifacts.read_bytes(ArtifactRef(**stages[0]["stdout_ref"]))) == binding.to_dict()
        identity = json.loads(artifacts.read_bytes(ArtifactRef(**stages[1]["stdout_ref"])))
        assert identity["python_version"] == sys.version
        assert len(identity["executable_sha256"]) == 64
        assert release_source_binding(source_root) == binding
        for stage in stages:
            command = artifacts.read_json(ArtifactRef(**stage["command_ref"]))
            assert command["argv"][1:4] == ["-B", str(source_root / "scripts/fanout_prepare.py"), "--internal-stage"]
            started = artifacts.read_json(ArtifactRef(**stage["started_ref"]))
            assert started["pid"] == stage["pid"]
