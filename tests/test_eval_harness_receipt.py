"""What a receipt is evidence OF.

`receipt_gate` compared two input hashes, and `_write_receipt` checked a subprocess exit
code. Neither asks whether anything was TESTED — so an all-skipped run, a command that runs
zero tests, and a certifier weakened between runs all left a fresh green receipt.
"""
import json
import os
import subprocess
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
sys.path.insert(0, str(ROOT / "scripts"))

import checks  # noqa: E402
import eval_harness  # noqa: E402
import eval_trigger  # noqa: E402

POLICY_TOML = ("[models]\n[eval]\nrequired_providers=['codex','agy']\n"
               "judge='codex'\nmode='normal'\n")


def _write_evals(tmp_path, cases):
    path = tmp_path / "evals" / "demo" / "evals.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"evals": cases}))
    return path


def _valid_eval(**overrides):
    return {
        "id": 0,
        "name": "safe eval: punctuation is okay",
        "prompt": "inspect the fixture directory",
        "assertions": ["reports the fixture"],
        "files": ["nested/input.json"],
        **overrides,
    }


def _write_candidate_repo(tmp_path, *, skill="demo", source_body="# current\n",
                          rendered_body=None):
    rendered_body = source_body if rendered_body is None else rendered_body
    source = tmp_path / "shared" / "skills" / skill
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text(source_body)
    (tmp_path / "capabilities.toml").write_text(POLICY_TOML)
    manifest = tmp_path / "evals" / skill / "evals.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"evals": [_valid_eval()]}))
    fixture = manifest.parent / "fixtures" / "nested" / "input.json"
    fixture.parent.mkdir(parents=True)
    fixture.write_text('{"version": 1}')
    for provider in checks.CLIS:
        rendered = (tmp_path / "marketplaces" / provider / "plugins" / "khenrix-utils"
                    / "skills" / skill / "SKILL.md")
        rendered.parent.mkdir(parents=True)
        rendered.write_text(rendered_body)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True,
                   capture_output=True)
    return manifest, fixture


def _args(skill):
    return SimpleNamespace(skill=skill, providers="claude", mode="normal", judge="claude")


def test_eval_manifest_is_read_once_and_validates_every_runtime_path(monkeypatch, tmp_path):
    _write_evals(tmp_path, [_valid_eval()])
    calls = 0
    original = checks._regular_file_state

    def one_read(path, root):
        nonlocal calls
        calls += 1
        return original(path, root)

    monkeypatch.setattr(checks, "_regular_file_state", one_read)
    spec, raw = checks.load_eval_manifest(tmp_path, "demo")
    assert spec["evals"][0]["name"] == "safe eval: punctuation is okay"
    assert raw == (tmp_path / "evals" / "demo" / "evals.json").read_bytes()
    assert calls == 1


def test_skill_names_are_validated_before_any_path_join_or_seed_write(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    repo.mkdir()
    outside.mkdir()
    (outside / "evals.json").write_text(json.dumps({"evals": [_valid_eval(files=[])]}))
    monkeypatch.setattr(eval_harness, "ROOT", repo)
    monkeypatch.setattr(eval_harness, "EVALS_ROOT", repo / "evals")

    for name in ("../../outside", "../outside", "/tmp/outside", "Demo", "a_b"):
        with pytest.raises(ValueError, match="invalid skill name"):
            checks.load_eval_manifest(repo, name)
        with pytest.raises(ValueError, match="invalid skill name"):
            checks.source_hash(repo, name)
        with pytest.raises(ValueError, match="invalid skill name"):
            eval_harness.seed_receipts(_args(name))
        with pytest.raises(ValueError, match="invalid skill name"):
            eval_harness.run_eval_for_provider(
                name, "claude", _valid_eval(), "claude", {}, repo / "iteration",
                timeout=1, retries=0, readonly=True, skill_body="# captured\n")
    assert not (outside / "receipt.json").exists()


def test_candidate_capture_refuses_a_stale_rendered_body(monkeypatch, tmp_path):
    _write_candidate_repo(tmp_path, source_body="# new\n", rendered_body="# old\n")
    monkeypatch.setattr(eval_harness, "ROOT", tmp_path)
    monkeypatch.setattr(eval_harness, "EVALS_ROOT", tmp_path / "evals")
    with pytest.raises(SystemExit, match="rendered skill body is stale"):
        eval_harness.capture_candidate("demo", ["claude"])


def test_make_eval_renders_before_starting_provider_execution():
    makefile = (ROOT / "Makefile").read_text()
    assert "eval: render ##" in makefile


def test_make_eval_keeps_all_user_values_out_of_recipe_expansion(tmp_path):
    marker = tmp_path / "injected"
    make_function = f"$(shell touch {marker})"
    result = subprocess.run(
        ["make", "-n", "eval", f"SKILL={make_function}",
         f"PROVIDERS={make_function}", f"MODE={make_function}",
         "TIMEOUT=--option-shaped", "RETRIES=1; touch never",
         "MODELCLAUDE=`id`", "MODELCODEX=$HOME", "MODELAGY=quoted value"],
        cwd=ROOT, capture_output=True, text=True)

    assert result.returncode == 0, result.stdout + result.stderr
    assert not marker.exists()
    commands = [line.strip() for line in result.stdout.splitlines()]
    assert commands.count(
        "python3 scripts/eval_harness.py --from-make-process=$PPID") == 1


@pytest.mark.parametrize(("target", "assignment"), (
    ("eval-trigger", "SKILL"), ("eval-arena", "SKILLS")))
def test_make_trigger_targets_keep_user_values_out_of_expansion(
        tmp_path, target, assignment):
    marker = tmp_path / "injected"
    hostile = f"$(shell touch {marker})"
    result = subprocess.run(
        ["make", "-n", target, f"{assignment}={hostile}", f"MODE={hostile}",
         f"JUDGE={hostile}"], cwd=ROOT, capture_output=True, text=True)

    assert result.returncode == 0, result.stdout + result.stderr
    assert not marker.exists()
    assert "--from-make-process=$PPID --make-target=" in result.stdout


@pytest.mark.parametrize(("target", "want"), (
    ("trigger", ["--skill=$(shell nope)", "--mode=deep", "--judge=codex"]),
    ("arena", ["--arena=one,two; nope", "--mode=normal", "--judge=agy"]),
))
def test_trigger_make_adapter_recovers_literal_atoms(tmp_path, target, want):
    proc = tmp_path / "456"
    proc.mkdir()
    assignment = (b"SKILL=$(shell nope)\0" if target == "trigger"
                  else b"SKILLS=one,two; nope\0")
    mode = b"MODE=deep\0" if target == "trigger" else b"MODE=normal\0"
    judge = b"JUDGE=codex\0" if target == "trigger" else b"JUDGE=agy\0"
    (proc / "cmdline").write_bytes(b"make\0" + assignment + mode + judge)

    assert eval_trigger._argv_from_make_process(
        "456", target, proc_root=tmp_path) == want


def test_make_environment_adapter_scrubs_parent_make_state():
    env = {
        "KHENRIX_EVAL_SKILL_RAW": "--option-shaped",
        "KHENRIX_EVAL_PROVIDERS_RAW": "codex,agy",
        "SKILL": "expanded", "PROVIDERS": "expanded",
        "MAKEFLAGS": "--eval=bad", "MFLAGS": "-e", "MAKEOVERRIDES": "SKILL",
        "KEEP": "yes",
    }

    argv = eval_harness._argv_from_make_env(env)

    assert argv == ["--skill=--option-shaped", "--providers=codex,agy"]
    assert env == {"KEEP": "yes"}


def test_eval_policy_digest_is_semantic_and_part_of_every_skill_identity(tmp_path):
    _write_candidate_repo(tmp_path)
    original_policy = checks.eval_policy_hash(tmp_path)
    original_source = checks.source_hash(tmp_path, "demo")

    (tmp_path / "capabilities.toml").write_text(
        "[models]\n\n[eval]\nmode = 'normal'\njudge = 'codex'\n"
        "required_providers = [ 'codex', 'agy' ]\n")
    assert checks.eval_policy_hash(tmp_path) == original_policy
    assert checks.source_hash(tmp_path, "demo") == original_source

    (tmp_path / "capabilities.toml").write_text(
        "[models]\n[eval]\nrequired_providers=['claude','codex','agy']\n"
        "judge='codex'\nmode='normal'\n")
    assert checks.eval_policy_hash(tmp_path) != original_policy
    assert checks.source_hash(tmp_path, "demo") != original_source


def test_policy_defaults_are_the_canonical_codex_gemini_panel(monkeypatch, tmp_path):
    _write_candidate_repo(tmp_path)
    monkeypatch.setattr(eval_harness, "ROOT", tmp_path)
    args = eval_harness._apply_policy_defaults(eval_harness.parse_args(["--skill=demo"]))

    assert args.providers == "codex,agy"
    assert args.judge == "codex"
    assert args.mode == "normal"


def test_precommit_invokes_the_fail_closed_all_final_receipt_gate():
    makefile = (ROOT / "Makefile").read_text()
    assert "verify-all-final-receipts:" in makefile
    assert "checks.final_receipt_gate(checks.ROOT)" in makefile
    assert "$(MAKE) --no-print-directory verify-all-final-receipts" in makefile


def test_make_process_adapter_recovers_literal_argv_and_scrubs_environment(tmp_path):
    process = tmp_path / "123"
    process.mkdir()
    (process / "cmdline").write_bytes(
        b"make\0eval\0SKILL=$(shell touch nope)\0PROVIDERS=codex,agy; echo nope\0"
        b"MODE=normal\n--seed-receipt\0TIMEOUT=--option-shaped\0RETRIES=2\0"
        b"MODELCLAUDE=`id`\0MODELCODEX=$HOME\0MODELAGY=quoted value\0")
    env = {"SKILL": "expanded", "MAKEFLAGS": "--eval=bad", "KEEP": "yes"}

    argv = eval_harness._argv_from_make_process(
        "123", env, proc_root=tmp_path)

    assert argv == [
        "--skill=$(shell touch nope)",
        "--providers=codex,agy; echo nope",
        "--mode=normal\n--seed-receipt",
        "--timeout=--option-shaped",
        "--retries=2",
        "--model-claude=`id`",
        "--model-codex=$HOME",
        "--model-agy=quoted value",
    ]
    assert env == {"KEEP": "yes"}


def test_receipt_validator_refuses_an_outside_symlink_without_reading_it(tmp_path):
    repo = tmp_path / "repo"
    receipt_dir = repo / "evals" / "demo"
    receipt_dir.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text("[]")
    (receipt_dir / "receipt.json").symlink_to(outside)

    problems = checks.validate_receipt(repo, "demo")

    assert any("unreadable" in problem and "symlink" in problem for problem in problems)
    assert not any("root must be a JSON object" in problem for problem in problems)


def test_empty_fixture_directory_changes_the_typed_eval_identity(tmp_path):
    _write_candidate_repo(tmp_path)
    before = checks.eval_input_snapshot(tmp_path, "demo")
    empty = tmp_path / "evals" / "demo" / "fixtures" / "empty"
    empty.mkdir()
    after = checks.eval_input_snapshot(tmp_path, "demo")

    assert ("fixtures/empty", empty.lstat().st_mode & 0o7777) in after.fixture_dirs
    assert before.fixture_dirs != after.fixture_dirs
    assert before.eval_set_hash != after.eval_set_hash


def test_seed_refuses_a_requested_missing_fixture_without_a_receipt(monkeypatch, tmp_path):
    manifest, _fixture = _write_candidate_repo(tmp_path)
    manifest.write_text(json.dumps({"evals": [_valid_eval(files=["missing"])]}))
    monkeypatch.setattr(eval_harness, "ROOT", tmp_path)
    monkeypatch.setattr(eval_harness, "EVALS_ROOT", tmp_path / "evals")

    with pytest.raises(ValueError, match="requested fixture.*does not exist"):
        eval_harness.seed_receipts(_args("demo"))
    assert not (tmp_path / "evals" / "demo" / "receipt.json").exists()


def test_fixture_execution_uses_the_captured_bytes_not_a_later_live_edit(
        monkeypatch, tmp_path):
    _manifest, fixture = _write_candidate_repo(tmp_path)
    monkeypatch.setattr(eval_harness, "ROOT", tmp_path)
    monkeypatch.setattr(eval_harness, "EVALS_ROOT", tmp_path / "evals")
    candidate = eval_harness.capture_candidate("demo", ["claude"])
    fixture.write_text('{"version": 2}')
    snapshot_dir = eval_harness._materialize_input_snapshot(
        candidate, tmp_path / "evals" / "demo" / "workspace" / "captured")
    assert (snapshot_dir / "nested" / "input.json").read_text() == '{"version": 1}'


@pytest.mark.parametrize("changed", ["source", "manifest", "fixture", "rendered"])
def test_midrun_candidate_drift_never_writes_a_receipt(monkeypatch, tmp_path, changed):
    manifest, fixture = _write_candidate_repo(tmp_path)
    monkeypatch.setattr(eval_harness, "ROOT", tmp_path)
    monkeypatch.setattr(eval_harness, "EVALS_ROOT", tmp_path / "evals")
    candidate = eval_harness.capture_candidate("demo", ["claude"])

    if changed == "source":
        (tmp_path / "shared" / "skills" / "demo" / "SKILL.md").write_text("# changed\n")
    elif changed == "manifest":
        spec = json.loads(manifest.read_text())
        spec["evals"][0]["prompt"] = "changed prompt"
        manifest.write_text(json.dumps(spec))
    elif changed == "fixture":
        fixture.write_text('{"version": 2}')
    else:
        rendered = (tmp_path / "marketplaces" / "claude" / "plugins" / "khenrix-utils"
                    / "skills" / "demo" / "SKILL.md")
        rendered.write_text("# changed\n")

    with pytest.raises(SystemExit):
        eval_harness._write_receipt(
            "demo", providers=["claude"], mode="normal", judge="claude",
            delta=None, seeded=True, candidate=candidate)
    assert not (tmp_path / "evals" / "demo" / "receipt.json").exists()


def test_deterministic_command_roster_cannot_change_during_certification(
        monkeypatch, tmp_path):
    _write_candidate_repo(tmp_path, skill="llm-forge")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_forge_a.py").write_text("def test_a(): pass\n")
    monkeypatch.setattr(eval_harness, "ROOT", tmp_path)
    monkeypatch.setattr(eval_harness, "EVALS_ROOT", tmp_path / "evals")
    candidate = eval_harness.capture_candidate(
        "llm-forge", ["claude"], include_bodies=False)

    def change_roster(command, **_kwargs):
        (tests / "test_forge_b.py").write_text("def test_b(): pass\n")
        return subprocess.CompletedProcess(command, 0, stdout="1 passed\n", stderr="")

    monkeypatch.setattr(eval_harness.subprocess, "run", change_roster)
    with pytest.raises(SystemExit, match="deterministic gate command|source"):
        eval_harness._write_receipt(
            "llm-forge", providers=["claude"], mode="normal", judge="claude",
            delta=None, seeded=True, candidate=candidate)
    assert not (tmp_path / "evals" / "llm-forge" / "receipt.json").exists()


def test_edit_during_final_double_capture_never_publishes_a_receipt(monkeypatch, tmp_path):
    manifest, _fixture = _write_candidate_repo(tmp_path)
    monkeypatch.setattr(eval_harness, "ROOT", tmp_path)
    monkeypatch.setattr(eval_harness, "EVALS_ROOT", tmp_path / "evals")
    candidate = eval_harness.capture_candidate("demo", ["claude"])
    original_snapshot = checks.eval_input_snapshot
    changed = False

    def snapshot_then_edit(root, skill):
        nonlocal changed
        snapshot = original_snapshot(root, skill)
        if not changed:
            changed = True
            spec = json.loads(manifest.read_text())
            spec["evals"][0]["prompt"] = "changed at the final capture seam"
            manifest.write_text(json.dumps(spec))
        return snapshot

    monkeypatch.setattr(checks, "eval_input_snapshot", snapshot_then_edit)
    with pytest.raises(SystemExit, match="changed during the eval"):
        eval_harness._write_receipt(
            "demo", providers=["claude"], mode="normal", judge="claude",
            delta=None, seeded=True, candidate=candidate)
    assert changed
    assert not (tmp_path / "evals" / "demo" / "receipt.json").exists()


def test_edit_at_atomic_replace_seam_removes_the_just_published_receipt(
        monkeypatch, tmp_path):
    manifest, _fixture = _write_candidate_repo(tmp_path)
    monkeypatch.setattr(eval_harness, "ROOT", tmp_path)
    monkeypatch.setattr(eval_harness, "EVALS_ROOT", tmp_path / "evals")
    candidate = eval_harness.capture_candidate("demo", ["claude"])
    real_replace = eval_harness.os.replace

    def replace_then_edit(source, destination):
        real_replace(source, destination)
        spec = json.loads(manifest.read_text())
        spec["evals"][0]["prompt"] = "changed at atomic replacement"
        manifest.write_text(json.dumps(spec))

    monkeypatch.setattr(eval_harness.os, "replace", replace_then_edit)
    with pytest.raises(SystemExit, match="changed during the eval"):
        eval_harness._write_receipt(
            "demo", providers=["claude"], mode="normal", judge="claude",
            delta=None, seeded=True, candidate=candidate)
    assert not (tmp_path / "evals" / "demo" / "receipt.json").exists()


def test_deterministic_certifier_observes_the_immutable_candidate_not_live_bytes(
        monkeypatch, tmp_path):
    _write_candidate_repo(tmp_path, skill="llm-forge")
    tests = tmp_path / "tests"
    tests.mkdir()
    test_file = tests / "test_forge_a.py"
    original = "def test_a():\n    assert False\n"
    test_file.write_text(original)
    monkeypatch.setattr(eval_harness, "ROOT", tmp_path)
    monkeypatch.setattr(eval_harness, "EVALS_ROOT", tmp_path / "evals")
    candidate = eval_harness.capture_candidate(
        "llm-forge", ["claude"], include_bodies=False)

    def altered_execution(command, *, cwd, **_kwargs):
        assert Path(cwd) != tmp_path
        test_file.write_text("def test_a():\n    assert True\n")
        observed = (Path(cwd) / "tests" / "test_forge_a.py").read_text()
        test_file.write_text(original)
        assert "assert False" in observed
        return subprocess.CompletedProcess(command, 1, stdout="1 failed\n", stderr="")

    monkeypatch.setattr(eval_harness.subprocess, "run", altered_execution)
    with pytest.raises(SystemExit, match="deterministic tests failed"):
        eval_harness._write_receipt(
            "llm-forge", providers=["claude"], mode="normal", judge="claude",
            delta=None, seeded=True, candidate=candidate)
    assert not (tmp_path / "evals" / "llm-forge" / "receipt.json").exists()


def test_deterministic_snapshot_has_private_git_identity_and_records_its_hash(
        monkeypatch, tmp_path):
    _write_candidate_repo(tmp_path, skill="llm-forge")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_forge_a.py").write_text("def test_a():\n    assert True\n")
    monkeypatch.setattr(eval_harness, "ROOT", tmp_path)
    monkeypatch.setattr(eval_harness, "EVALS_ROOT", tmp_path / "evals")
    candidate = eval_harness.capture_candidate(
        "llm-forge", ["claude"], include_bodies=False)
    real_run = eval_harness._REAL_SUBPROCESS_RUN

    def inspect_snapshot(command, *, cwd, **_kwargs):
        snapshot = Path(cwd)
        assert snapshot != tmp_path
        assert (snapshot / ".git").is_dir()
        listed = real_run(
            ["git", "ls-files", "--", "tests/test_forge_a.py"], cwd=snapshot,
            capture_output=True, text=True, check=True)
        assert listed.stdout.strip() == "tests/test_forge_a.py"
        assert "tests/test_forge_a.py" in command
        return subprocess.CompletedProcess(command, 0, stdout="1 passed\n", stderr="")

    monkeypatch.setattr(eval_harness.subprocess, "run", inspect_snapshot)
    eval_harness._write_receipt(
        "llm-forge", providers=["claude"], mode="normal", judge="claude",
        delta=None, seeded=True, candidate=candidate)

    receipt = json.loads(
        (tmp_path / "evals" / "llm-forge" / "receipt.json").read_text())
    assert receipt["gate_command"] == checks.deterministic_gate_command(
        tmp_path, "llm-forge")
    assert receipt["gate_tree_hash"] == candidate.gate_tree.gate_tree_hash


def test_eval_manifest_allows_omitted_optional_files(tmp_path):
    case = _valid_eval()
    case.pop("files")
    _write_evals(tmp_path, [case])
    assert checks.load_eval_manifest(tmp_path, "demo")[0]["evals"] == [case]


def test_receipt_gate_keeps_canonical_skills_enrolled_when_their_manifest_is_deleted(tmp_path):
    alpha = tmp_path / "shared" / "skills" / "alpha"
    beta = tmp_path / "shared" / "skill-templates" / "beta"
    alpha.mkdir(parents=True)
    beta.mkdir(parents=True)
    (alpha / "SKILL.md").write_text("# alpha\n")
    (beta / "SKILL.md.tmpl").write_text("# beta\n")
    _write_evals(tmp_path, [_valid_eval()])
    (tmp_path / "evals" / "demo").rename(tmp_path / "evals" / "alpha")
    (tmp_path / "evals" / "orphan").mkdir()

    assert checks.expected_eval_skills(tmp_path) == ["alpha", "beta", "orphan"]
    (tmp_path / "evals" / "alpha" / "evals.json").unlink()
    problems = checks.receipt_gate(tmp_path, advisory=False)
    assert any("alpha has no eval manifest" in problem for problem in problems)
    assert any("beta has no eval manifest" in problem for problem in problems)
    assert any("orphan has no eval manifest" in problem for problem in problems)


@pytest.mark.parametrize("skill", ["khenrix-wiki-add", "khenrix-wiki-sync", "llm-forge"])
def test_deterministic_receipt_requires_complete_gate_evidence(skill):
    receipt = {
        "self_test": True,
        "provenance": "eval",
        "certified_by": checks.SELF_TEST_CERTIFIERS[skill],
        "deterministic_gate": checks.SELF_TEST_CERTIFIERS[skill],
        "gate_command": checks.deterministic_gate_command(ROOT, skill),
        "gate_tree_hash": "0" * 64,
        "gate_counts": {"tests_run": 1, "skipped": 0, "failed": 0},
    }
    assert checks.is_self_test_gated(skill, receipt)
    for bad in (
        {key: value for key, value in receipt.items() if key != "gate_command"},
        {key: value for key, value in receipt.items() if key != "gate_tree_hash"},
        {key: value for key, value in receipt.items() if key != "gate_counts"},
        {**receipt, "gate_counts": {"tests_run": 0, "skipped": 0, "failed": 0}},
        {**receipt, "gate_counts": {"tests_run": 1, "skipped": 1, "failed": 0}},
        {**receipt, "gate_counts": {"tests_run": 1, "skipped": 0, "failed": 1}},
        {**receipt, "gate_command": [""],
         "gate_counts": {"tests_run": True, "skipped": 0, "failed": 0}},
        {**receipt, "gate_command": ["python3", "-m", "unittest"]},
    ):
        assert not checks.is_self_test_gated(skill, bad)


def test_llm_council_keeps_its_distinct_deterministic_evidence_shape():
    receipt = {
        "self_test": True,
        "provenance": "eval",
        "certified_by": "fanout --self-test",
        "gate_command": checks.deterministic_gate_command(ROOT, "llm-council"),
        "gate_tree_hash": "0" * 64,
    }
    assert checks.is_self_test_gated("llm-council", receipt)
    assert not checks.is_self_test_gated(
        "llm-council", {**receipt, "gate_command": ["true"]})


def test_llm_council_validator_requires_the_exact_shared_command(tmp_path):
    _write_candidate_repo(tmp_path, skill="llm-council")
    receipt_path = tmp_path / "evals" / "llm-council" / "receipt.json"
    receipt = {
        "schema_version": checks.CURRENT_RECEIPT_SCHEMA,
        "skill": "llm-council",
        "source_hash": checks.source_hash(tmp_path, "llm-council"),
        "eval_set_hash": checks.eval_set_hash(tmp_path, "llm-council"),
        "providers": [],
        "provenance": "eval",
        "self_test": True,
        "certified_by": checks.SELF_TEST_CERTIFIERS["llm-council"],
        "gate_command": ["true"],
        "gate_tree_hash": checks.gate_tree_snapshot(tmp_path).gate_tree_hash,
    }
    receipt_path.write_text(json.dumps(receipt))
    expected = checks.deterministic_gate_command(tmp_path, "llm-council")
    problems = checks.validate_receipt(tmp_path, "llm-council")
    assert any("gate_command" in problem and repr(expected) in problem
               for problem in problems)

    receipt["gate_command"] = expected
    receipt_path.write_text(json.dumps(receipt))
    assert checks.validate_receipt(tmp_path, "llm-council") == []


def test_gate_tree_identity_includes_empty_directories_modes_and_regular_bytes(tmp_path):
    _write_candidate_repo(tmp_path, skill="llm-forge")
    empty = tmp_path / "empty"
    empty.mkdir()
    before = checks.gate_tree_snapshot(tmp_path)
    empty.chmod(0o700)
    mode_changed = checks.gate_tree_snapshot(tmp_path)
    payload = tmp_path / "payload.txt"
    payload.write_text("one")
    content_one = checks.gate_tree_snapshot(tmp_path)
    payload.write_text("two")
    content_two = checks.gate_tree_snapshot(tmp_path)

    assert any(rel == "empty" for rel, _mode in before.directories)
    assert before.gate_tree_hash != mode_changed.gate_tree_hash
    assert content_one.gate_tree_hash != content_two.gate_tree_hash


def test_tuneup_logs_stay_outside_a_valid_deterministic_receipt(tmp_path):
    _write_candidate_repo(tmp_path, skill="llm-council")
    log_dir = tmp_path / "docs" / "tuneups" / "log"
    log_dir.mkdir(parents=True)
    active_log = log_dir / "skill-tuneup.jsonl"
    active_log.write_text('{"finding_id":"run-start"}\n')
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "-f",
         "docs/tuneups/log/skill-tuneup.jsonl"],
        check=True, capture_output=True)

    certified = checks.gate_tree_snapshot(tmp_path)
    log_prefix = ("docs", "tuneups", "log")
    assert all(Path(rel).parts[:3] != log_prefix
               for rel, _mode in certified.directories)
    assert all(Path(rel).parts[:3] != log_prefix
               for rel, _mode, _content in certified.files)

    receipt_path = tmp_path / "evals" / "llm-council" / "receipt.json"
    receipt_path.write_text(json.dumps({
        "schema_version": checks.CURRENT_RECEIPT_SCHEMA,
        "skill": "llm-council",
        "source_hash": checks.source_hash(tmp_path, "llm-council"),
        "eval_set_hash": checks.eval_set_hash(tmp_path, "llm-council"),
        "providers": [],
        "provenance": "eval",
        "self_test": True,
        "certified_by": checks.SELF_TEST_CERTIFIERS["llm-council"],
        "gate_command": checks.deterministic_gate_command(tmp_path, "llm-council"),
        "gate_tree_hash": certified.gate_tree_hash,
    }))
    assert checks.validate_receipt(tmp_path, "llm-council") == []

    with active_log.open("a") as stream:
        stream.write('{"finding_id":"cycle-end"}\n')
    assert checks.gate_tree_snapshot(tmp_path) == certified
    assert checks.validate_receipt(tmp_path, "llm-council") == []

    nested_log = log_dir / "archive" / "new.jsonl"
    nested_log.parent.mkdir()
    nested_log.write_text('{"finding_id":"new"}\n')
    assert checks.gate_tree_snapshot(tmp_path) == certified
    assert checks.validate_receipt(tmp_path, "llm-council") == []

    nested_log.write_text('{"finding_id":"changed"}\n')
    assert checks.gate_tree_snapshot(tmp_path) == certified
    assert checks.validate_receipt(tmp_path, "llm-council") == []

    adjacent = tmp_path / "docs" / "tuneups" / "review.md"
    adjacent.write_text("reviewed\n")
    assert checks.gate_tree_snapshot(tmp_path).gate_tree_hash != certified.gate_tree_hash
    assert any("deterministic gate tree changed" in problem
               for problem in checks.validate_receipt(tmp_path, "llm-council"))


def test_gate_tree_refuses_a_git_visible_symlink(tmp_path):
    _write_candidate_repo(tmp_path, skill="llm-forge")
    (tmp_path / "link").symlink_to(tmp_path / "capabilities.toml")
    with pytest.raises(ValueError, match="symlink"):
        checks.gate_tree_snapshot(tmp_path)


def test_gate_tree_ignores_ambient_foreign_git_dir_and_index(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    _write_candidate_repo(repo, skill="llm-forge")
    ignored = (repo / "marketplaces" / "claude" / "plugins" / "khenrix-utils"
               / "skills" / "llm-forge" / "SKILL.md")
    (repo / ".gitignore").write_text("marketplaces/\n")
    rel = ignored.relative_to(repo).as_posix()
    subprocess.run(["git", "-C", str(repo), "add", "-f", "--", rel],
                   check=True, capture_output=True)
    expected = checks.gate_tree_snapshot(repo)
    assert any(path == rel for path, _mode, _body in expected.files)

    foreign = tmp_path / "foreign"
    subprocess.run(["git", "init", "-q", str(foreign)], check=True,
                   capture_output=True)
    monkeypatch.setenv("GIT_DIR", str(foreign / ".git"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(foreign / ".git" / "index"))

    redirected = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-z", "--cached", "--others",
         "--exclude-standard"], capture_output=True, check=True)
    assert rel.encode() not in redirected.stdout.split(b"\0")
    assert checks.gate_tree_snapshot(repo) == expected


@pytest.mark.parametrize("field,value", [
    ("id", "../escape"),
    ("id", "a/b"),
    ("id", "x" * 121),
    ("name", "../escape"),
    ("name", "a\\b"),
    ("name", "x\x01y"),
    ("name", "x" * 121),
    ("files", ["../secret"]),
    ("files", ["/absolute"]),
    ("files", ["nested\\input.json"]),
    ("files", ["nested//input.json"]),
    ("files", ["nested/./input.json"]),
    ("files", ["nested/../input.json"]),
    ("files", ["nested/\x01input.json"]),
    ("files", ["x" * 241]),
])
def test_eval_manifest_refuses_unsafe_workspace_or_fixture_paths(tmp_path, field, value):
    _write_evals(tmp_path, [_valid_eval(**{field: value})])
    with pytest.raises(ValueError, match="invalid"):
        checks.load_eval_manifest(tmp_path, "demo")


@pytest.mark.parametrize("field,value", [
    ("id", "../escape"),
    ("id", "nested/id"),
    ("name", "../escape"),
    ("name", "nested\\name"),
])
def test_workspace_construction_revalidates_id_and_name(tmp_path, field, value):
    itdir = tmp_path / "iteration"
    itdir.mkdir()
    with pytest.raises(ValueError):
        eval_harness._eval_workspace_base(_valid_eval(**{field: value}), itdir)
    assert list(itdir.iterdir()) == []


def test_workspace_construction_refuses_a_preexisting_link(tmp_path):
    itdir = tmp_path / "iteration"
    outside = tmp_path / "outside"
    itdir.mkdir()
    outside.mkdir()
    (itdir / "eval-0").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        eval_harness._eval_workspace_base(_valid_eval(name="safe"), itdir)


def test_materialization_revalidates_paths_before_creating_the_destination(tmp_path):
    src = tmp_path / "fixtures"
    src.mkdir()
    (src / "known.txt").write_text("known")
    dest = tmp_path / "workspace"

    for name in ("../outside", "/outside", "nested\\outside", "nested//outside"):
        with pytest.raises(ValueError):
            eval_harness.materialize_fixtures({"files": [name]}, src, dest)
        assert not dest.exists(), f"{name!r} created a workspace before refusal"

    with pytest.raises(ValueError, match="missing"):
        eval_harness.materialize_fixtures({"files": ["missing.txt"]}, src, dest)
    assert not dest.exists(), "a missing fixture created a workspace"


def test_materialization_refuses_source_and_destination_symlink_escapes(tmp_path):
    src = tmp_path / "fixtures"
    src.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("synthetic")
    (src / "source-link.txt").symlink_to(outside / "secret.txt")
    dest = tmp_path / "workspace"

    with pytest.raises(ValueError, match="symlink"):
        eval_harness.materialize_fixtures({"files": ["source-link.txt"]}, src, dest)
    assert not dest.exists()

    (src / "nested").mkdir()
    (src / "nested" / "input.txt").write_text("safe")
    dest.mkdir()
    (dest / "nested").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        eval_harness.materialize_fixtures({"files": ["nested/input.txt"]}, src, dest)
    assert not (outside / "input.txt").exists()


def test_deterministic_target_routing_does_not_depend_on_receipt_shape():
    malformed = {"self_test": True, "certified_by": "wrong", "provenance": "seeded"}
    for skill in checks.SELF_TEST_CERTIFIERS:
        assert checks.requires_deterministic_gate(skill)
        assert not checks.is_self_test_gated(skill, {})
        assert not checks.is_self_test_gated(skill, malformed)
    assert not checks.requires_deterministic_gate("ordinary-skill")
    assert not checks.is_self_test_gated("llm-forge", None)


def test_seeded_llm_council_receipt_records_real_certifier_without_manual_attestation(
        monkeypatch, tmp_path):
    evals = tmp_path / "evals"
    (evals / "llm-council").mkdir(parents=True)
    source = tmp_path / "shared" / "skills" / "llm-council"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("# council\n")
    (evals / "llm-council" / "evals.json").write_text(json.dumps({"evals": [{
        "id": 0, "name": "council", "prompt": "p", "assertions": ["a"]
    }]}))
    (tmp_path / "capabilities.toml").write_text(POLICY_TOML)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True,
                   capture_output=True)
    monkeypatch.setattr(eval_harness, "ROOT", tmp_path)
    monkeypatch.setattr(eval_harness, "EVALS_ROOT", evals)
    monkeypatch.setattr(
        eval_harness.subprocess, "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0),
    )

    args = SimpleNamespace(skill="llm-council", providers="codex,agy", mode="normal",
                           judge="codex")
    assert eval_harness.seed_receipts(args) == 0
    receipt = json.loads((evals / "llm-council" / "receipt.json").read_text())
    assert receipt["provenance"] == "eval"
    assert receipt["self_test"] is True
    assert receipt["certified_by"] == "fanout --self-test"
    assert receipt["gate_command"] == checks.deterministic_gate_command(
        tmp_path, "llm-council")
    assert receipt["gate_tree_hash"] == checks.gate_tree_snapshot(tmp_path).gate_tree_hash
    assert "synthesis_review" not in receipt


def test_the_deterministic_gate_runs_the_whole_forge_suite_not_three_modules():
    """The receipt names `forge-suite-all`; every Forge module on disk must be positional.

    The omitted set includes `test_forge_packaging.py` — the module that checks rendered
    façade resolution and the quote prose — so breaking the façade left the gate green.
    """
    cmd = eval_harness.DETERMINISTIC_GATED["llm-forge"]
    named = {Path(a).name for a in cmd if a.endswith(".py")}
    mk = (ROOT / "Makefile").read_text()
    on_disk = {p.name for p in (ROOT / "tests").glob("test_forge_*.py")}
    assert named == on_disk, (
        f"the gate runs {len(named)} of {len(on_disk)} forge suites; missing "
        f"{sorted(on_disk - named)}")


def test_a_command_that_runs_no_tests_does_not_earn_a_receipt(tmp_path):
    """An all-skipped pytest run exits 0, and so does `true`. A receipt written on an exit
    code says a process finished, not that anything was checked."""
    counts = eval_harness._pytest_counts("no tests ran in 0.01s\n")
    assert counts["tests_run"] == 0
    assert not eval_harness._counts_are_evidence(counts), \
        "zero executed tests must not be evidence"


def test_an_all_skipped_run_does_not_earn_a_receipt():
    counts = eval_harness._pytest_counts("5 skipped in 0.10s\n")
    assert counts["skipped"] == 5 and counts["tests_run"] == 0
    assert not eval_harness._counts_are_evidence(counts)


def test_a_real_run_with_passes_is_evidence():
    """The guard against over-tightening: if nothing counted, no receipt could ever be
    written and the gate would be closed by making it impossible."""
    counts = eval_harness._pytest_counts("1076 passed in 34.20s\n")
    assert counts["tests_run"] == 1076 and counts["skipped"] == 0
    assert eval_harness._counts_are_evidence(counts)


def test_a_run_with_any_deselection_is_refused():
    counts = eval_harness._pytest_counts("1074 passed, 2 deselected in 34.20s\n")
    assert counts["skipped"] == 2
    assert not eval_harness._counts_are_evidence(counts)


def test_a_run_with_any_skip_is_refused():
    """A skip in the CERTIFYING suite is a test that did not run, and the receipt would
    otherwise say the suite passed."""
    counts = eval_harness._pytest_counts("100 passed, 1 skipped in 2.00s\n")
    assert not eval_harness._counts_are_evidence(counts)


@pytest.mark.parametrize(("summary", "expected"), [
    ("1 xfailed in 0.10s\n", {"tests_run": 0, "skipped": 1, "failed": 0}),
    ("1 xpassed in 0.10s\n", {"tests_run": 0, "skipped": 1, "failed": 0}),
    ("3 passed, 1 xfailed in 0.10s\n",
     {"tests_run": 3, "skipped": 1, "failed": 0}),
    ("3 passed, 1 xpassed in 0.10s\n",
     {"tests_run": 3, "skipped": 1, "failed": 0}),
    ("3 passed, 1 xfailed, 1 xpassed in 0.10s\n",
     {"tests_run": 3, "skipped": 2, "failed": 0}),
])
def test_pytest_xresults_are_never_clean_certification(summary, expected):
    counts = eval_harness._pytest_counts(summary)
    assert counts == expected
    assert not eval_harness._counts_are_evidence(counts)


def test_real_pytest_xresult_summary_is_not_certifying(tmp_path):
    suite = tmp_path / "test_xresults.py"
    suite.write_text(
        "import pytest\n\n"
        "@pytest.mark.xfail(reason='expected witness')\n"
        "def test_expected_failure():\n"
        "    assert False\n\n"
        "@pytest.mark.xfail(reason='unexpected witness')\n"
        "def test_unexpected_pass():\n"
        "    assert True\n")
    env = dict(os.environ)
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("PYTEST_PLUGINS", None)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-c", os.devnull,
         "--rootdir", str(tmp_path), str(suite)],
        cwd=tmp_path, env=env, capture_output=True, text=True)

    assert result.returncode == 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "xfailed" in combined and "xpassed" in combined
    counts = eval_harness._pytest_counts(combined)
    assert counts == {"tests_run": 0, "skipped": 2, "failed": 0}
    assert not eval_harness._counts_are_evidence(counts)


def test_the_certifier_and_the_test_manifest_are_in_the_source_closure():
    """Weakening DETERMINISTIC_GATED, or deleting a test it names, must stale the receipt.

    The closure held the skill's own directory and the shared engine. It did not hold the
    thing that decides what "certified" means, so a gate could be narrowed and every existing
    receipt stayed fresh.
    """
    paths = [rel for rel, _h in checks.source_manifest(ROOT, "llm-forge")]
    for rel in ("scripts/eval_harness.py", "scripts/lib/checks.py", "Makefile"):
        assert rel in paths, f"{rel} is not in llm-forge's source closure"
    assert len(paths) == len(set(paths)), \
        f"a file is hashed twice: {sorted({p for p in paths if paths.count(p) > 1})}"


def test_a_receipt_claiming_no_self_test_is_refused(tmp_path, monkeypatch):
    """receipt_gate compared input hashes only, so a receipt with matching hashes and
    `self_test: false` was accepted — "the certification failed" and "the certification
    passed" left the same verdict at the gate."""
    rp = ROOT / "evals" / "llm-forge" / "receipt.json"
    rec = json.loads(rp.read_text())
    rec["self_test"] = False
    bad = tmp_path / "receipt.json"
    bad.write_text(json.dumps(rec))
    assert checks._receipt_is_certified(rec) is False
    rec["self_test"] = True
    assert checks._receipt_is_certified(rec) is True


# ------------------------------------------------------------------ the blind A/B verdict
def test_an_unreadable_comparison_is_not_a_tie():
    """COMPARE_TMPL asks for "winner": "A" or "B" and never offers "tie" — so every tie this
    harness produced was a parse failure, an empty answer, or an off-slot response wearing a
    verdict's clothes. A judge that timed out yields raw="" -> {} -> "tie".

    This is `eval_trigger.parse_verdict`'s already-fixed bug one module over: that function's
    docstring names it exactly — "a judge that timed out, hit a quota wall or answered in
    prose was recorded as having said 'do not activate'".
    """
    key = {"A": "with_skill", "B": "without_skill"}
    for raw in ("", "the judge crashed", '{"winner": null}', '{"winner": "Q"}', "{"):
        c = eval_harness.parse_comparison(raw, key)
        assert c["winner_condition"] is None, \
            f"an unreadable comparison ({raw!r}) resolved to {c['winner_condition']!r}"


def test_a_real_verdict_still_resolves():
    """The guard against over-tightening: if nothing resolved, the blind A/B would report
    nothing and the collapse would be closed by making the signal useless."""
    key = {"A": "with_skill", "B": "without_skill"}
    assert eval_harness.parse_comparison('{"winner": "A"}', key)["winner_condition"] == "with_skill"
    assert eval_harness.parse_comparison('{"winner": "B"}', key)["winner_condition"] == "without_skill"


def test_unreadable_comparisons_are_excluded_from_the_tally_not_counted_as_ties():
    """A dead judge inflated the tie column, which is the column that decides the winner."""
    cs = [{"winner_condition": "with_skill", "winner_slot": "A"},
          {"winner_condition": None, "winner_slot": "?"},
          {"winner_condition": None, "winner_slot": "?"}]
    t = eval_harness._blind_tally(cs)
    assert t["with_skill"] == 1 and t["tie"] == 0
    assert t["unreadable"] == 2, "two silent judges must be reported, not absorbed"
    assert eval_harness.blind_winner(cs) == "with_skill"


def test_a_constant_slot_preference_is_not_a_tie():
    """The live witness: all six comparison.json files in this skill's own artifacts recorded
    winner_slot "A". `blind_pair` alternates which condition sits in slot A by eval-id parity,
    so a judge with a fixed slot preference maps to with, without, with, without... — a clean
    3-3 that is indistinguishable from six genuinely matched pairs. Nothing read winner_slot.

    n=6, one judge, one session: enough to show the collapse is real and unguarded, not
    enough to claim this judge is generally position-biased.
    """
    cs = [{"winner_condition": c, "winner_slot": "A"}
          for c in ("with_skill", "without_skill") * 3]
    assert eval_harness.blind_winner(cs) == "slot_degenerate", \
        "a judge that always answered the same slot is not a tie"


def test_an_absent_slot_is_not_a_repeated_slot():
    """The degeneracy check's own version of "nothing leaves the same record as nobody".

    A comparison built without `winner_slot` — the self-test's own cases, or any caller
    constructing one by hand — records no slot at all. Reading that as "every judgement chose
    the same slot" made a missing field indistinguishable from a position-biased judge, and
    turned three green self-test cases red.
    """
    no_slot = [{"winner_condition": "with_skill"}, {"winner_condition": "with_skill"},
               {"winner_condition": "without_skill"}]
    assert eval_harness.blind_winner(no_slot) == "with_skill"
    assert eval_harness.blind_winner([{"winner_condition": "with_skill", "winner_slot": "?"},
                                      {"winner_condition": "without_skill",
                                       "winner_slot": "?"}]) == "tie"


def test_the_gate_name_is_not_narrower_than_the_gate():
    """A receipt exists to say what ran, so a provenance string naming three suites while
    thirty-one execute is the same defect the receipt is supposed to prevent, one field over.

    Pinned loosely — the name is prose and may be reworded — but it may not name a SUBSET it
    no longer describes.
    """
    name = eval_harness.DETERMINISTIC_GATE_NAMES["llm-forge"]
    cmd = eval_harness.DETERMINISTIC_GATED["llm-forge"]
    suites = [a for a in cmd if a.endswith(".py")]
    for narrow in ("handover", "cli", "gc"):
        assert narrow not in name or len(suites) <= 3, (
            f"the gate name {name!r} names a subset while {len(suites)} suites run")


def test_the_counts_parser_reads_unittest_as_well_as_pytest():
    """Two of the three DETERMINISTIC_GATED skills run `unittest discover`, whose summary is a
    different shape. Reading only pytest's reported `tests_run: 0` for a run of 83 real tests,
    so the counts check refused a receipt it should have written — fail-closed, and wrong about
    which runner it was looking at.

    `Ran N tests` counts skips; pytest's `N passed` does not. The skips come back out so
    `tests_run` means the same thing for both: tests that actually executed.
    """
    assert eval_harness._pytest_counts("Ran 83 tests in 0.139s\n\nOK") == \
        {"tests_run": 83, "skipped": 0, "failed": 0}
    assert eval_harness._pytest_counts("Ran 83 tests in 0.1s\n\nOK (skipped=2)") == \
        {"tests_run": 81, "skipped": 2, "failed": 0}
    assert not eval_harness._counts_are_evidence(
        eval_harness._pytest_counts("Ran 83 tests in 0.1s\n\nFAILED (failures=1)"))
    assert not eval_harness._counts_are_evidence(
        eval_harness._pytest_counts("Ran 0 tests in 0.0s\n\nOK"))


# ---- what certifies an ORDINARY skill's receipt -----------------------------------------
def test_an_ordinary_skills_real_eval_writes_a_receipt_its_own_gate_accepts():
    """THE EXTERNAL QUESTION: can `make eval SKILL=<ordinary>` produce a receipt that
    `make precommit` then REFUSES? It could, and did.

    `self_test` is written only by the llm-council branch and the deterministic-gated branch.
    Every other skill's genuine run wrote `provenance: "eval"` and no `self_test`, and
    `_receipt_is_certified` read "absent" as the SEEDED shape — so the gate rejected the real
    result and the only way past was to seed over it with weaker evidence. Reproduced on
    khenrix-setup (delta +0.0695) and khenrix-upgrade (delta +0.0278), both refused.
    """
    assert checks._receipt_is_certified({"provenance": "eval",
                                         "certified_by": "delta-gate"}) is True


def test_an_eval_receipt_that_names_no_certifier_is_still_refused():
    """THE DISCRIMINATION CHECK — the fix must not make `provenance: "eval"` self-certifying.
    A receipt claiming a run happened while naming nothing that gated it claims nothing."""
    assert checks._receipt_is_certified({"provenance": "eval"}) is False
    assert checks._receipt_is_certified({"provenance": "eval", "certified_by": ""}) is False


def test_a_failed_self_test_is_not_rescued_by_a_certifier_name():
    """`self_test: False` is a FAILED certification and stays decisive. Reading a neighbouring
    field for reassurance is precisely what the original predicate's docstring refuses."""
    assert checks._receipt_is_certified(
        {"self_test": False, "certified_by": "delta-gate"}) is False


def test_the_seeded_shape_still_needs_no_certifier():
    """Seeding is an explicit human act blessing a committed state, not a claim a suite ran —
    unchanged by this fix, and asserted so the two shapes cannot collapse into one."""
    assert checks._receipt_is_certified(
        {"provenance": "seeded: blessed current committed state"}) is True
    assert checks._receipt_is_certified({}) is False
