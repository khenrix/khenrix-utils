"""What a receipt is evidence OF.

`receipt_gate` compared two input hashes, and `_write_receipt` checked a subprocess exit
code. Neither asks whether anything was TESTED — so an all-skipped run, a command that runs
zero tests, and a certifier weakened between runs all left a fresh green receipt.
"""
from collections import Counter
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
sys.path.insert(0, str(ROOT / "scripts"))

import checks  # noqa: E402
import eval_harness  # noqa: E402
from test_eval_receipt_v3 import JUDGE_CONFIG, put, write_proof


@pytest.mark.parametrize("judge,effort", [("codex", "xhigh"), ("claude", "xhigh"), ("agy", "high")])
def test_judge_launch_uses_review_effort_without_changing_executor_config(tmp_path, monkeypatch, judge, effort):
    cfg = {judge: {"model": "explicit-model", "thinking": "medium"}}
    argv = []
    registry = eval_harness.eval_runtime.ProviderRegistry.default(
        version_probe=lambda _: "1.0.0", contract_probe=lambda _: None)
    def turn(provider, prompt, config, workdir, **kwargs):
        spec = eval_harness.eval_runtime.build_native_spec(
            provider, prompt, config, cwd=tmp_path, registry=registry, role=kwargs["role"])
        assert spec.thinking == effort
        assert kwargs["readonly"] is True and kwargs["retries"] == 0
        argv.append(spec.argv)
        return '{"winner":"A"}', {"valid": True}
    monkeypatch.setattr(eval_harness, "run_text", turn)
    eval_harness._run_judge(judge, "review", cfg, tmp_path, timeout=30,
                            verdict_ok=lambda raw: True)
    assert cfg == {judge: {"model": "explicit-model", "thinking": "medium"}}
    assert "explicit-model" in argv[0]
    if judge == "codex":
        assert 'model_reasoning_effort="xhigh"' in argv[0]
    elif judge == "claude":
        assert argv[0][argv[0].index("--effort") + 1] == effort
    else:
        assert effort in argv[0]


@pytest.mark.parametrize("providers,judge", [(("codex", "codex"), "claude"),
                                            (("codex",), "codex")])
def test_development_versions_include_judge_and_probe_each_cli_once(providers, judge):
    calls = []
    values = {"codex": b"codex-cli 0.160.0\n", "claude": b"2.1.284 (Claude Code)\n"}

    def probe(provider):
        calls.append(provider)
        return values[provider]

    measured = eval_harness.collect_cli_versions(providers, judge, version_runner=probe)

    expected = ({"claude": "2.1.284 (Claude Code)", "codex": "codex-cli 0.160.0"}
                if judge == "claude" else {"codex": "codex-cli 0.160.0"})
    assert measured == expected
    assert sorted(calls) == sorted(expected)


@pytest.mark.parametrize("raw", [None, "codex-cli 0.160.0", b"", b" \n", b"\xff",
                                  b"codex 0.160.0\nextra", b"codex 0.160.0\n\n",
                                  b"codex\r0.160.0", b"codex\x000.160.0",
                                  b"codex\x1b[0m 0.160.0", b"codex\t0.160.0",
                                  b"x" * 257])
def test_development_versions_refuse_invalid_metadata(raw):
    with pytest.raises(RuntimeError, match="CLI version"):
        eval_harness.collect_cli_versions(("codex",), "codex", version_runner=lambda _: raw)


@pytest.mark.parametrize("providers,judge", [(("unknown",), "claude"),
                                            (("codex",), "unknown")])
def test_development_versions_refuse_unknown_executables_before_probe(providers, judge):
    def probe(_provider):
        pytest.fail("unknown executable reached the version observer")

    with pytest.raises(RuntimeError, match="CLI"):
        eval_harness.collect_cli_versions(providers, judge, version_runner=probe)


@pytest.fixture
def measured_campaign(tmp_path, monkeypatch):
    eval_dir = tmp_path / "evals" / "chunk-map"
    eval_dir.mkdir(parents=True)
    (eval_dir / "evals.json").write_text(json.dumps({"evals": [
        {"id": 0, "name": "sample", "prompt": "Answer", "assertions": ["correct"]},
    ]}))
    skill_dir = tmp_path / "shared/skills/chunk-map"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Test skill\nAnswer directly.\n")
    shutil.copy2(ROOT / "capabilities.toml", tmp_path / "capabilities.toml")
    monkeypatch.setattr(eval_harness, "ROOT", tmp_path)
    monkeypatch.setattr(eval_harness, "EVALS_ROOT", eval_dir.parent)
    monkeypatch.setattr(eval_harness, "skill_source_dir", lambda _skill, _provider: skill_dir)
    monkeypatch.setattr(eval_harness.eval_runtime, "resolve_mode_config", lambda _args: {
        "codex": {"model": "requested-codex"}, "claude": {"model": "requested-judge"},
    })
    monkeypatch.setattr(eval_harness.eval_runtime, "effective_timeout", lambda _args: 30)
    calls = []
    artifacts = []

    def provider_turn(provider, _prompt, _cfg, workdir, **_kwargs):
        calls.append(provider)
        if workdir.name.startswith("judge"):
            raw = '{"expectations": [{"passed": true}]}'
        elif workdir.name.startswith("compare-"):
            raw = '{"winner": "A"}'
        else:
            raw = "answer"
        workdir.mkdir(parents=True, exist_ok=True)
        artifact = workdir / f"{provider}.stdout.attempt-1.txt"
        artifact.write_text(raw)
        artifacts.append((artifact, raw))
        return raw, {"valid": True, "reason": "ok", "duration_sec": 0.01}

    monkeypatch.setattr(eval_harness, "run_text", provider_turn)
    args = eval_harness.parse_args(["--skill", "chunk-map", "--providers", "codex",
                                  "--judge", "claude", "--iteration", "1"])
    return args, eval_dir, calls, artifacts


def test_real_eval_measures_before_spend_and_records_versions(measured_campaign, monkeypatch):
    args, eval_dir, calls, _artifacts = measured_campaign
    probes = []

    def probe(provider):
        probes.append((provider, len(calls)))
        return {"codex": b"codex-cli 0.160.0\n", "claude": b"2.1.284 (Claude Code)\n"}[provider]

    monkeypatch.setattr(eval_harness, "observe_cli_version", probe)
    assert eval_harness.run(args) == 0

    expected = {"claude": "2.1.284 (Claude Code)", "codex": "codex-cli 0.160.0"}
    benchmark = json.loads((eval_dir / "workspace/iteration-1/benchmark.json").read_text())
    receipt = json.loads((eval_dir / "receipt.json").read_text())
    assert benchmark["metadata"]["cli_versions"] == expected
    assert benchmark["metadata"]["judge_config"] == {"model": "requested-judge", "thinking": "xhigh"}
    assert receipt["judge_config"] == {"model": "requested-judge", "thinking": "xhigh"}
    assert benchmark["metadata"]["cli_versions_after"] == expected
    assert receipt["cli_versions"] == expected
    assert receipt["provenance"] == "eval"
    assert len(calls) == 5
    assert probes == [("claude", 0), ("codex", 0), ("claude", 5), ("codex", 5)]


@pytest.mark.parametrize("failure", [b"", b"multi\nline", b"\xff", b"x" * 257,
                                    RuntimeError("installed CLI unavailable")])
def test_failed_initial_version_measurement_refuses_receipt_and_spend(
    measured_campaign, monkeypatch, failure, capsys,
):
    args, eval_dir, calls, artifacts = measured_campaign

    def probe(_provider):
        if isinstance(failure, Exception):
            raise failure
        return failure

    monkeypatch.setattr(eval_harness, "observe_cli_version", probe)
    assert eval_harness.run(args) == 1
    assert calls == [] and artifacts == []
    assert not (eval_dir / "receipt.json").exists()
    assert "without a receipt" in capsys.readouterr().err


@pytest.mark.parametrize("after", [b"codex-cli 0.161.0\n", b"", b"bad\nversion"])
@pytest.mark.parametrize("existing_receipt", [False, True])
def test_post_campaign_version_failure_keeps_raw_results_without_receipt(
    measured_campaign, monkeypatch, capsys, after, existing_receipt,
):
    args, eval_dir, calls, artifacts = measured_campaign
    receipt_path = eval_dir / "receipt.json"
    historical = b'{"historical": true}\n'
    if existing_receipt:
        receipt_path.write_bytes(historical)

    def probe(provider):
        if provider == "claude":
            return b"2.1.284 (Claude Code)\n"
        return after if calls else b"codex-cli 0.160.0\n"

    monkeypatch.setattr(eval_harness, "observe_cli_version", probe)
    assert eval_harness.run(args) == 1
    assert len(calls) == 5
    assert [path.read_text() for path, _raw in artifacts] == [raw for _path, raw in artifacts]
    benchmark = json.loads((eval_dir / "workspace/iteration-1/benchmark.json").read_text())
    assert benchmark["metadata"]["cli_versions"]["codex"] == "codex-cli 0.160.0"
    assert len(benchmark["runs"]) == 2
    if after == b"codex-cli 0.161.0\n":
        assert benchmark["metadata"]["cli_versions_after"]["codex"] == "codex-cli 0.161.0"
        assert "drift" in capsys.readouterr().err
    else:
        assert "without a receipt" in capsys.readouterr().err
    if existing_receipt:
        assert receipt_path.read_bytes() == historical
    else:
        assert not receipt_path.exists()


def test_seeded_receipt_never_claims_measured_cli_versions(tmp_path, monkeypatch):
    eval_dir = tmp_path / "chunk-map"
    eval_dir.mkdir()
    monkeypatch.setattr(eval_harness, "EVALS_ROOT", tmp_path)
    eval_harness._write_receipt("chunk-map", providers=["codex"], mode="standard",
                                judge="claude", delta=None, seeded=True,
                                cli_versions={"codex": "codex-cli 0.160.0"})
    receipt = json.loads((eval_dir / "receipt.json").read_text())
    assert receipt["provenance"].startswith("seeded:")
    assert "cli_versions" not in receipt


def test_campaign_source_change_after_first_turn_keeps_raw_without_receipt(
    measured_campaign, monkeypatch, capsys,
):
    args, eval_dir, calls, artifacts = measured_campaign
    real_boundary = eval_harness.run_text

    def changed_turn(*positional, **keywords):
        result = real_boundary(*positional, **keywords)
        if len(calls) == 1:
            (eval_harness.ROOT / "shared/skills/chunk-map/SKILL.md").write_text("Changed after measurement began.\n")
        return result

    monkeypatch.setattr(eval_harness, "run_text", changed_turn)
    monkeypatch.setattr(eval_harness, "observe_cli_version", lambda provider: (provider + " version").encode())
    assert eval_harness.run(args) == 1
    assert len(calls) == 5
    assert len(artifacts) == 5
    assert not (eval_dir / "receipt.json").exists()
    assert "changed during campaign" in capsys.readouterr().err


def test_eval_edit_between_loading_and_snapshot_refuses_before_spend(
    measured_campaign, monkeypatch, capsys,
):
    args, eval_dir, calls, artifacts = measured_campaign
    original_loader = eval_harness.load_evals

    def changed_loader(skill):
        loaded = original_loader(skill)
        (eval_dir / "evals.json").write_text(json.dumps({"evals": [
            {"id": 0, "name": "sample", "prompt": "New prompt", "assertions": ["new criterion"]}]}))
        return loaded

    monkeypatch.setattr(eval_harness, "load_evals", changed_loader)
    monkeypatch.setattr(eval_harness, "observe_cli_version", lambda provider: (provider + " version").encode())
    assert eval_harness.run(args) == 1
    assert calls == [] and artifacts == []
    assert not (eval_dir / "receipt.json").exists()
    assert "criteria" in capsys.readouterr().err


def test_old_development_cli_measurement_does_not_expire_unchanged_receipt(
    tmp_path, monkeypatch,
):
    put(tmp_path, "capabilities.toml", "[instructions]\n")
    put(tmp_path, "evals/chunk-map/evals.json", "{}\n")
    source = put(tmp_path, "shared/skills/chunk-map/SKILL.md", "body\n")
    receipt = {**checks.capture_evaluation_snapshot(tmp_path, "chunk-map", providers=["codex"]),
        "providers": ["codex"],
        "judge_config": JUDGE_CONFIG,
        "schema_version": 3, "provenance": "eval", "certified_by": "delta-gate",
        "raw_refs": [{"path": "workspace/raw.json", "sha256": "a" * 64}],
        "cli_versions": {"codex": "codex-cli 0.159.0"}}
    eval_dir = tmp_path / "evals/chunk-map"
    eval_dir.mkdir(parents=True, exist_ok=True)
    (eval_dir / "receipt.json").write_text(json.dumps(receipt))
    write_proof(tmp_path)
    monkeypatch.setattr(eval_harness, "observe_cli_version",
                        lambda _provider: pytest.fail("receipt reader probed host CLI"))
    assert checks.validate_receipt(tmp_path, "chunk-map") == []

    source.write_text("changed body\n")
    assert any("changed since last eval" in problem
               for problem in checks.validate_receipt(tmp_path, "chunk-map"))


def test_executor_receipt_gate_covers_owner_and_recovery_contracts():
    cmd = eval_harness.DETERMINISTIC_GATED["llm-fanout-execute"]
    named = {Path(a).name for a in cmd if a.endswith(".py")}
    assert named >= {"test_fanout_execute.py", "test_fanout_cold_recovery.py",
                     "test_fanout_repo.py", "test_fanout_verification.py"}
    assert eval_harness.DETERMINISTIC_GATE_NAMES["llm-fanout-execute"] == "fanout-execute-contracts"


def test_planner_receipt_gate_runs_its_deterministic_contracts():
    """Planner admission and compiler checks must certify its receipt, not only prose."""
    cmd = eval_harness.DETERMINISTIC_GATED["llm-fanout-plan"]
    named = {Path(argument).name for argument in cmd if argument.endswith(".py")}
    assert named >= {"test_fanout_plan_skill.py", "test_fanout_compiler.py",
                     "test_fanout_plan_schema.py", "test_fanout_skills.py",
                     "test_skill_delivery.py", "test_render_packaging.py"}
    assert eval_harness.DETERMINISTIC_GATE_NAMES["llm-fanout-plan"] == "fanout-plan-contracts"
    assert all(not Path(argument).is_absolute() for argument in
               eval_harness.portable_gate_command(cmd))


def test_deterministic_receipt_command_removes_the_machine_checkout_path():
    executable = eval_harness.DETERMINISTIC_GATED["llm-fanout-plan"]
    portable = eval_harness.portable_gate_command(executable)

    assert portable[:5] == executable[:5]
    assert all(not Path(argument).is_absolute() for argument in portable)
    assert all(str(Path.home()) not in argument for argument in portable)
    assert "tests/test_fanout_compiler.py" in portable
    assert "tests/test_fanout_plan_schema.py" in portable


@pytest.mark.parametrize(
    "leaking_argument",
    [
        "/Users/alice@example.test/src/repo/tests/test_gate.py",
        "/home/alice/src/repo/tests/test_gate.py",
        "~/src/repo/tests/test_gate.py",
        "--suite=/Users/alice/src/repo/tests/test_gate.py",
        "${HOME}/src/repo/tests/test_gate.py",
    ],
)
def test_receipt_gate_rejects_machine_specific_paths(
    tmp_path, monkeypatch, leaking_argument
):
    eval_dir = tmp_path / "evals" / "example"
    eval_dir.mkdir(parents=True)
    (eval_dir / "receipt.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "source_hash": "source",
                "eval_set_hash": "evals",
                "provenance": "seeded: blessed current committed state",
                "self_test": True,
                "gate_command": ["pytest", leaking_argument],
            }
        )
    )
    monkeypatch.setattr(checks, "source_hash", lambda _root, _skill: "source")
    monkeypatch.setattr(checks, "eval_set_hash", lambda _root, _skill: "evals")

    problems = checks.validate_receipt(tmp_path, "example")

    assert any("machine-specific absolute/home path" in problem for problem in problems)


def test_receipt_gate_accepts_repo_relative_gate_paths(tmp_path, monkeypatch):
    eval_dir = tmp_path / "evals" / "example"
    eval_dir.mkdir(parents=True)
    (eval_dir / "receipt.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "source_hash": "source",
                "eval_set_hash": "evals",
                "provenance": "seeded: blessed current committed state",
                "self_test": True,
                "gate_command": ["pytest", "tests/test_gate.py"],
            }
        )
    )
    monkeypatch.setattr(checks, "source_hash", lambda _root, _skill: "source")
    monkeypatch.setattr(checks, "eval_set_hash", lambda _root, _skill: "evals")

    assert checks.receipt_path_privacy_problems(
        {"gate_command": ["pytest", "tests/test_gate.py"]}, "example") == []


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
    counts = eval_harness._pytest_counts("1074 passed, 2 deselected in 34.20s\n")
    assert counts["tests_run"] == 1074 and counts["skipped"] == 0
    assert eval_harness._counts_are_evidence(counts)


def test_a_run_with_any_skip_is_refused():
    """A skip in the CERTIFYING suite is a test that did not run, and the receipt would
    otherwise say the suite passed."""
    counts = eval_harness._pytest_counts("100 passed, 1 skipped in 2.00s\n")
    assert not eval_harness._counts_are_evidence(counts)


def test_the_certifier_and_the_test_manifest_are_in_the_source_closure():
    """Weakening DETERMINISTIC_GATED, or deleting a test it names, must stale the receipt.

    The closure held the skill's own directory and the shared engine. It did not hold the
    thing that decides what "certified" means, so a gate could be narrowed and every existing
    receipt stayed fresh.
    """
    paths = [rel for rel, _h in checks.instrument_manifest(ROOT)]
    for rel in ("scripts/eval_harness.py", "scripts/lib/checks.py", "Makefile"):
        assert rel in paths, f"{rel} is not in the planner's source closure"
    assert len(paths) == len(set(paths)), \
        f"a file is hashed twice: {sorted({p for p in paths if paths.count(p) > 1})}"


@pytest.mark.parametrize("skill", ["chunk-map", "khenrix-setup"])
@pytest.mark.parametrize("instrument", ["shared/lib/fanout/eval_provider.py",
                                        "shared/lib/fanout/providers.py",
                                        "shared/lib/fanout/process.py",
                                        "shared/lib/fanout/collaboration.py",
                                        "shared/lib/fanout/artifacts.py",
                                        "shared/lib/fanout/errors.py",
                                        "shared/lib/fanout/skills.py",
                                        "scripts/eval_harness.py"])
def test_eval_instrument_changes_stale_shared_proof_not_ordinary_behavior(tmp_path, skill, instrument):
    """Changing the evaluator or its version observer must invalidate revision evidence."""
    repo = tmp_path / "repo"
    manifest = checks.source_manifest(ROOT, skill) + checks.instrument_manifest(ROOT)
    paths = [rel for rel, _hash in manifest if not rel.startswith("skill_facts:")]
    assert instrument in paths
    for relative in paths:
        source = ROOT / relative
        destination = repo / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    # source_manifest always parses capabilities.toml, including for skills with no facts.
    if not (repo / "capabilities.toml").exists():
        shutil.copy2(ROOT / "capabilities.toml", repo / "capabilities.toml")
    eval_dir = repo / "evals" / skill
    eval_dir.mkdir(parents=True)
    (eval_dir / "evals.json").write_text("{}\n")
    if not (repo / "shared/skills" / skill / "SKILL.md").is_file():
        put(repo, f"shared/skills/{skill}/SKILL.md", "Synthetic source selected for this instrument test.\n")
    snapshot = checks.capture_evaluation_snapshot(repo, skill, providers=["codex"])
    selected = snapshot["selected_input_manifest"]
    receipt = {**snapshot, "schema_version": 3, "provenance": "eval", "certified_by": "delta-gate", "judge_config": JUDGE_CONFIG,
               "providers": ["codex"],
               "cli_versions": {"codex": "observed-version"},
               "selected_input_manifest": selected, "selected_input_hash": checks.manifest_hash(selected),
               "raw_refs": [{"path": "workspace/raw.json", "sha256": "a" * 64}]}
    (eval_dir / "receipt.json").write_text(json.dumps(receipt))
    write_proof(repo)
    assert checks.validate_receipt(repo, skill) == []

    changed = repo / instrument
    changed.write_bytes(changed.read_bytes() + b"\n# simulated certifier change\n")

    problems = checks.validate_receipt(repo, skill)
    assert checks.source_hash(repo, skill) == snapshot["source_hash"]
    assert any("instrument proof" in problem for problem in problems)


def test_a_receipt_claiming_no_self_test_is_refused(tmp_path, monkeypatch):
    """receipt_gate compared input hashes only, so a receipt with matching hashes and
    `self_test: false` was accepted — "the certification failed" and "the certification
    passed" left the same verdict at the gate."""
    rec = {"self_test": True, "certified_by": "fanout-plan-contracts", "provenance": "eval"}
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


@pytest.mark.parametrize("skill", ["llm-fanout-plan", "llm-fanout-execute"])
def test_native_deterministic_receipt_records_the_actual_command(tmp_path, monkeypatch, skill):
    directory = tmp_path / "evals" / skill
    directory.mkdir(parents=True)
    put(tmp_path, f"shared/skills/{skill}/SKILL.md", "Synthetic skill under certification.\n")
    put(tmp_path, f"evals/{skill}/evals.json", "{}\n")
    shutil.copy2(ROOT / "capabilities.toml", tmp_path / "capabilities.toml")
    monkeypatch.setattr(eval_harness, "ROOT", tmp_path)
    monkeypatch.setattr(eval_harness, "EVALS_ROOT", directory.parent)
    command = eval_harness.DETERMINISTIC_GATED[skill]
    monkeypatch.setattr(eval_harness.subprocess, "run", lambda argv, **kwargs:
        subprocess.CompletedProcess(argv, 0, "7 passed in 0.1s\n", ""))
    eval_harness._write_receipt(skill, providers=["codex"], mode="normal",
        judge="claude", delta=0, seeded=False,
        evaluated_snapshot=checks.capture_evaluation_snapshot(tmp_path, skill, judge_config=JUDGE_CONFIG, providers=["codex"]),
        judge_config=JUDGE_CONFIG, raw_refs=[], cli_versions={"codex": "observed-version", "claude": "observed-judge-version"})
    receipt = json.loads((directory / "receipt.json").read_text())
    assert receipt["gate_command"] == eval_harness.portable_gate_command(command)
    assert receipt["certified_by"] == eval_harness.DETERMINISTIC_GATE_NAMES[skill]
    assert receipt["gate_counts"]["tests_run"] == 7
    capture = json.loads((directory / receipt["raw_refs"][0]["path"]).read_text())
    assert capture["stdout"] == "7 passed in 0.1s\n"


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


@pytest.mark.parametrize("phase", ["grading", "comparison"])
@pytest.mark.parametrize("bad", ["{malformed", "{}"])
def test_malformed_judge_reply_recovers_in_one_fresh_workspace(
    tmp_path, monkeypatch, phase, bad
):
    """Removing the semantic retry would turn a successful second verdict into 0/N or unreadable."""
    ev = {"id": 0, "name": "sample", "prompt": "Answer this", "assertions": ["first", "second"]}
    good = (json.dumps({"expectations": [{"passed": True}, {"passed": False}]})
            if phase == "grading" else json.dumps({"winner": "A"}))
    replies = iter([bad, good])
    workdirs = []
    artifacts = []

    def fake_run_text(_provider, _prompt, _cfg, workdir, **_kwargs):
        raw = next(replies)
        workdir.mkdir(parents=True, exist_ok=True)
        artifact = workdir / "claude.stdout.attempt-1.txt"
        artifact.write_text(raw)
        workdirs.append(workdir)
        artifacts.append(artifact)
        return raw, {"valid": True, "reason": "ok"}

    monkeypatch.setattr(eval_harness, "run_text", fake_run_text)
    if phase == "grading":
        result = eval_harness.grade("answer", ev, "with_skill", "claude", {},
                                    tmp_path / "case", timeout=30)
        assert result["passed"] == 1
    else:
        result = eval_harness.compare("answer A", "answer B", ev, "claude", {},
                                      tmp_path / "case", timeout=30, provider="claude")
        assert result["winner_condition"] == "with_skill"
    assert result["judge_ok"] is True
    assert len(workdirs) == 2 and workdirs[0] != workdirs[1]
    assert [path.read_text() for path in artifacts] == [bad, good]


@pytest.mark.parametrize("bad_winner", ["A or B", "AB", "a"])
def test_comparison_retries_a_noncanonical_winner_before_normalization(
    tmp_path, monkeypatch, bad_winner
):
    """A prefix that looks like slot A is not a verdict choosing exactly A."""
    first = json.dumps({"winner": bad_winner})
    replies = iter([first, '{"winner": "B"}'])
    workdirs = []

    def fake_run_text(_provider, _prompt, _cfg, workdir, **_kwargs):
        workdirs.append(workdir)
        return next(replies), {"valid": True, "reason": "ok"}

    monkeypatch.setattr(eval_harness, "run_text", fake_run_text)
    ev = {"id": 0, "name": "sample", "prompt": "Answer", "assertions": ["correct"]}

    result = eval_harness.compare("A", "B", ev, "claude", {}, tmp_path,
                                  timeout=30, provider="claude")

    assert result["judge_ok"] is True
    assert result["winner_condition"] == "without_skill"
    assert len(workdirs) == 2 and workdirs[0] != workdirs[1]


def test_comparison_accepts_an_exact_trimmed_winner_without_retry(tmp_path, monkeypatch):
    calls = []

    def fake_run_text(_provider, _prompt, _cfg, workdir, **_kwargs):
        calls.append(workdir)
        return '{"winner": " B "}', {"valid": True, "reason": "ok"}

    monkeypatch.setattr(eval_harness, "run_text", fake_run_text)
    ev = {"id": 0, "name": "sample", "prompt": "Answer", "assertions": ["correct"]}

    result = eval_harness.compare("A", "B", ev, "claude", {}, tmp_path,
                                  timeout=30, provider="claude")

    assert result["judge_ok"] is True
    assert result["winner_condition"] == "without_skill"
    assert len(calls) == 1


@pytest.mark.parametrize("phase", ["grading", "comparison"])
def test_valid_first_judge_reply_needs_no_semantic_retry(tmp_path, monkeypatch, phase):
    calls = []

    def fake_run_text(_provider, _prompt, _cfg, workdir, **_kwargs):
        calls.append(workdir)
        raw = (json.dumps({"expectations": [{"passed": True}]})
               if phase == "grading" else json.dumps({"winner": "A"}))
        return raw, {"valid": True, "reason": "ok"}

    monkeypatch.setattr(eval_harness, "run_text", fake_run_text)
    ev = {"id": 0, "name": "sample", "prompt": "Answer", "assertions": ["correct"]}
    if phase == "grading":
        result = eval_harness.grade("answer", ev, "with_skill", "claude", {},
                                    tmp_path, timeout=30)
        assert result["passed"] == 1
    else:
        result = eval_harness.compare("A", "B", ev, "claude", {}, tmp_path,
                                      timeout=30, provider="claude")
        assert result["winner_condition"] == "with_skill"
    assert result["judge_ok"] is True
    assert len(calls) == 1


@pytest.mark.parametrize("phase", ["grading", "comparison"])
@pytest.mark.parametrize("skill", ["chunk-map", "khenrix-quality"])
def test_two_malformed_judge_replies_stop_campaign_without_receipt(
    tmp_path, monkeypatch, phase, skill
):
    """An ordinary skill cannot earn a verdict from two malformed judge replies."""
    eval_root = tmp_path / "evals"
    eval_dir = eval_root / skill
    eval_dir.mkdir(parents=True)
    (eval_dir / "evals.json").write_text(json.dumps({"evals": [
        {"id": 0, "name": "first", "prompt": "Answer", "assertions": ["correct"]},
        {"id": 1, "name": "second", "prompt": "Answer", "assertions": ["correct"]},
    ]}))
    skill_dir = tmp_path / "selected-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Test skill\nAnswer directly.\n")
    monkeypatch.setattr(eval_harness, "EVALS_ROOT", eval_root)
    monkeypatch.setattr(eval_harness, "skill_source_dir", lambda _skill, _provider: skill_dir)
    monkeypatch.setattr(eval_harness.eval_runtime, "resolve_mode_config",
                        lambda _args: {"claude": {"model": "test-model"}})
    monkeypatch.setattr(eval_harness.eval_runtime, "effective_timeout", lambda _args: 30)
    monkeypatch.setattr(eval_harness, "observe_cli_version", lambda _provider: b"test-cli 1.0\n")

    # Keep the real campaign and its gate decision; replace only the costly provider call
    # and the deterministic subprocess behind receipt writing.
    monkeypatch.setattr(eval_harness, "_write_receipt",
                        lambda _skill, **_kwargs: (eval_dir / "receipt.json").write_text("issued"))
    malformed_artifacts = []
    calls = []

    def fake_run_text(_provider, _prompt, _cfg, workdir, **_kwargs):
        calls.append(workdir)
        is_first_eval = any(part.startswith("eval-0-") for part in workdir.parts)
        is_grading = workdir.name.startswith("judge")
        is_comparison = workdir.name.startswith("compare-")
        malformed = is_first_eval and ((phase == "grading" and is_grading)
                                       or (phase == "comparison" and is_comparison))
        if malformed:
            raw = "{malformed"
        elif is_grading:
            raw = json.dumps({"expectations": [{"passed": True}]})
        elif is_comparison:
            raw = json.dumps({"winner": "A"})
        else:
            raw = "answer"
        workdir.mkdir(parents=True, exist_ok=True)
        artifact = workdir / "claude.stdout.attempt-1.txt"
        artifact.write_text(raw)
        if malformed:
            malformed_artifacts.append(artifact)
        return raw, {"valid": True, "reason": "ok", "duration_sec": 0.01}

    monkeypatch.setattr(eval_harness, "run_text", fake_run_text)
    args = eval_harness.parse_args(["--skill", skill, "--iteration", "1"])

    assert eval_harness.run(args) == 1
    assert not (eval_dir / "receipt.json").exists()
    second = eval_dir / "workspace" / "iteration-1" / "eval-1-second"
    assert second.is_dir()  # all executors were retained before the judge failed
    assert not list(second.rglob("answer.md"))
    assert not list(second.rglob("grading.json"))
    assert len(malformed_artifacts) == 2
    assert malformed_artifacts[0].parent != malformed_artifacts[1].parent
    assert [path.read_text() for path in malformed_artifacts] == ["{malformed", "{malformed"]


@pytest.mark.parametrize("phase", ["grading", "comparison"])
def test_judge_transport_error_keeps_its_reason_without_semantic_retry(
    tmp_path, monkeypatch, phase
):
    """The added parse retry must not reinterpret an existing transport failure."""
    calls = []

    def fake_run_text(_provider, _prompt, _cfg, workdir, **_kwargs):
        calls.append(workdir)
        return "", {"valid": False, "reason": "timeout"}

    monkeypatch.setattr(eval_harness, "run_text", fake_run_text)
    ev = {"id": 0, "name": "sample", "prompt": "Answer", "assertions": ["correct"]}
    if phase == "grading":
        result = eval_harness.grade("answer", ev, "with_skill", "claude", {},
                                    tmp_path, timeout=30)
    else:
        result = eval_harness.compare("A", "B", ev, "claude", {}, tmp_path,
                                      timeout=30, provider="claude")
    assert result["judge_ok"] is False
    assert result["judge_reason"] == "timeout"
    assert len(calls) == 1


@pytest.mark.parametrize("phase", ("grading", "comparison"))
@pytest.mark.parametrize("retry_failure", ("malformed", "timeout"))
@pytest.mark.parametrize("skill", ("chunk-map", "llm-fanout-plan"))
def test_exhausted_judge_retains_completed_and_inflight_partial_benchmark(
    measured_campaign, monkeypatch, phase, retry_failure, skill,
):
    args, eval_dir, calls, artifacts = measured_campaign
    if skill != args.skill:
        changed_dir = eval_dir.with_name(skill)
        eval_dir.rename(changed_dir)
        eval_dir = changed_dir
        args.skill = skill
        source = eval_harness.ROOT / "shared/skills" / skill
        source.mkdir()
        (source / "SKILL.md").write_text("Synthetic deterministic-gated measured source.\n")
        monkeypatch.setattr(eval_harness, "skill_source_dir", lambda _skill, _provider: source)
    evals = [{"id": number, "name": f"case-{number}", "prompt": "Answer", "assertions": ["correct"]}
             for number in range(3)]
    (eval_dir / "evals.json").write_text(json.dumps({"evals": evals}))
    monkeypatch.setattr(eval_harness, "observe_cli_version", lambda _provider: b"fixture-cli 1.0\n")
    bad_attempts = []
    def turn(provider, _prompt, _cfg, workdir, **_kwargs):
        calls.append(provider)
        case = next(part for part in workdir.parts if part.startswith("eval-"))
        target = case.startswith("eval-1-") and (
            workdir.parent.name == "codex__without_skill" if phase == "grading"
            else workdir.name.startswith("compare-"))
        valid, reason = True, "ok"
        if target:
            raw = "{malformed"
            if bad_attempts and retry_failure == "timeout":
                raw, valid, reason = "", False, "timeout"
            bad_attempts.append((workdir, raw, reason))
        elif workdir.name.startswith("judge"):
            raw = '{"expectations":[{"passed":true}]}'
        elif workdir.name.startswith("compare-"):
            raw = '{"winner":"A"}'
        else:
            raw = f"{case}/{workdir.name}/completed executor"
        workdir.mkdir(parents=True, exist_ok=True)
        path = workdir / f"{provider}.stdout.attempt-1.txt"
        path.write_text(raw)
        artifacts.append((path, raw))
        return raw, {"valid": valid, "reason": reason, "duration_sec": 0.25}
    monkeypatch.setattr(eval_harness, "run_text", turn)
    assert eval_harness.run(args) == 1
    iteration = eval_dir / "workspace/iteration-1"
    pending = iteration / "eval-2-case-2"
    assert pending.is_dir()
    assert len(list(pending.rglob("*.stdout.attempt-1.txt"))) == 2
    assert not list(pending.rglob("answer.md"))
    assert not list(pending.rglob("grading.json"))
    assert not (eval_dir / "receipt.json").exists()
    benchmark = json.loads((iteration / "benchmark.json").read_text())
    assert len(benchmark["runs"]) == 4
    assert benchmark["runs"][0]["result"]["passed"] == 1
    assert benchmark["runs"][2]["result"]["passed"] == 1
    assert benchmark["runs"][3]["result"]["executor_error"] == 0
    assert benchmark["runs"][3]["result"]["time_seconds"] == 0.25
    assert benchmark["comparisons"][0]["judge_ok"] is True
    if phase == "grading":
        last = benchmark["runs"][3]
        assert last["result"]["judge_error"] == 1 and last["result"]["passed"] == 0
        assert last["expectations"][0]["evidence"] == "no verdict returned"
    else:
        assert benchmark["comparisons"][1]["winner_condition"] is None
        assert benchmark["comparisons"][1]["judge_ok"] is False
    error = benchmark["metadata"]["judge_error"]
    assert ("timeout" if retry_failure == "timeout" else "unreadable verdict") in error
    assert len(bad_attempts) == 2 and bad_attempts[0][0] != bad_attempts[1][0]
    assert all(str(path) in error for path, _raw, _reason in bad_attempts)
    assert [path.read_text() for path, _raw in artifacts] == [raw for _path, raw in artifacts]


@pytest.mark.parametrize("gate", ("passed", "failed", "skipped", "empty"))
def test_valid_advisory_only_certifies_actual_nonzero_noskip_owning_success(
    measured_campaign, monkeypatch, gate,
):
    args, old_dir, calls, artifacts = measured_campaign
    eval_dir = old_dir.with_name("llm-fanout-plan")
    old_dir.rename(eval_dir)
    args.skill = "llm-fanout-plan"
    source = eval_harness.ROOT / "shared/skills/llm-fanout-plan"
    source.mkdir()
    (source / "SKILL.md").write_text("Synthetic measured planner.\n")
    monkeypatch.setattr(eval_harness, "skill_source_dir", lambda _skill, _provider: source)
    monkeypatch.setattr(eval_harness, "observe_cli_version", lambda _provider: b"fixture-cli 1.0\n")
    def valid_turn(provider, _prompt, _cfg, workdir, **_kwargs):
        calls.append(provider)
        raw = ('{"expectations":[{"passed":true}]}' if workdir.name.startswith("judge")
               else '{"winner":"A"}' if workdir.name.startswith("compare-") else "completed executor")
        workdir.mkdir(parents=True, exist_ok=True)
        path = workdir / f"{provider}.stdout.attempt-1.txt"
        path.write_text(raw)
        artifacts.append((path, raw))
        return raw, {"valid": True, "reason": "ok", "duration_sec": 0.1}
    monkeypatch.setattr(eval_harness, "run_text", valid_turn)
    # The certifier still launches a native owning process and parses its real counts.
    expected = "https://ex.com/?token=REDACTED" if gate != "failed" else "deliberately wrong fault"
    owner = ("import sys,unittest\nsys.path.insert(0,'shared/lib')\n"
             "from wikisync.canonurl import redact_credentials\n"
             "class Owner(unittest.TestCase):\n"
             + (" @unittest.skip('controlled skip')\n" if gate == "skipped" else "")
             + " def test_credentials(self):\n"
             + f"  self.assertEqual(redact_credentials('https://ex.com/?token=secret'),{expected!r})\n"
             + ("suite=unittest.TestSuite()\n" if gate == "empty"
                else "suite=unittest.defaultTestLoader.loadTestsFromTestCase(Owner)\n")
             + "result=unittest.TextTestRunner().run(suite)\nsys.exit(not result.wasSuccessful())\n")
    monkeypatch.setitem(eval_harness.DETERMINISTIC_GATED, args.skill, ["python3", "-c", owner])
    native_run = subprocess.run
    observed = []
    def owning_run(command, **kwargs):
        result = native_run(command, **kwargs)
        observed.append(result)
        print(json.dumps({"owning_argv": command, "returncode": result.returncode,
                          "stdout": result.stdout, "stderr": result.stderr}))
        return result
    monkeypatch.setattr(eval_harness.subprocess, "run", owning_run)
    if gate == "passed":
        assert eval_harness.run(args) == 0
        receipt = json.loads((eval_dir / "receipt.json").read_text())
        assert receipt["self_test"] is True and receipt["provenance"] == "eval"
        assert receipt["gate_counts"] == {"tests_run": 1, "skipped": 0, "failed": 0}
    else:
        with pytest.raises(SystemExit, match="deterministic|counts"):
            eval_harness.run(args)
        assert not (eval_dir / "receipt.json").exists()
    benchmark = json.loads((eval_dir / "workspace/iteration-1/benchmark.json").read_text())
    assert len(benchmark["runs"]) == 2 and all(run["result"]["judge_error"] == 0 for run in benchmark["runs"])
    assert len(artifacts) == 5
    assert len(observed) == 1
    assert observed[0].returncode == (1 if gate == "failed" else 0)
    assert [path.read_text() for path, _raw in artifacts] == [raw for _path, raw in artifacts]


def test_execute_receipt_selects_material_makefile_target_handover_and_eval_owners():
    named = {Path(argument).name for argument in eval_harness.DETERMINISTIC_GATED["llm-fanout-execute"]
             if argument.endswith(".py")}
    assert named >= {"test_fanout_branch_handover.py", "test_fanout_targets.py", "test_eval_provider.py"}
    command = eval_harness.portable_gate_command(eval_harness.DETERMINISTIC_GATED["llm-fanout-execute"])
    assert all(not Path(argument).is_absolute() for argument in command)


EXECUTOR_MEMBERS = (
    "test_fanout_execute_skill.py", "test_fanout_execute.py",
    "test_fanout_cold_recovery.py", "test_fanout_storage.py",
    "test_fanout_memory.py", "test_fanout_repo.py", "test_fanout_targets.py",
    "test_fanout_branch_handover.py", "test_eval_provider.py",
    "test_fanout_verification.py", "test_skill_delivery.py",
)
FANOUT_MEMBERS = (
    "test_fanout_artifacts.py", "test_fanout_benchmark.py",
    "test_fanout_bootstrap_packages.py", "test_fanout_branch_handover.py",
    "test_fanout_cold_recovery.py", "test_fanout_collaboration.py",
    "test_fanout_compiler.py", "test_fanout_context_view.py",
    "test_fanout_controller.py", "test_fanout_execute.py",
    "test_fanout_execute_skill.py", "test_fanout_lifecycle.py",
    "test_fanout_local_authority.py", "test_fanout_maka_native.py",
    "test_fanout_memory.py", "test_fanout_multirepo_integration.py",
    "test_fanout_native_profiles.py", "test_fanout_native_write_policy.py",
    "test_fanout_owner_actions.py", "test_fanout_owner_reconciliation.py",
    "test_fanout_plan_schema.py", "test_fanout_plan_skill.py",
    "test_fanout_process.py", "test_fanout_profiles.py",
    "test_fanout_providers.py", "test_fanout_recorded_cli.py",
    "test_fanout_repo.py", "test_fanout_runstate.py", "test_fanout_scheduler.py",
    "test_fanout_skills.py", "test_fanout_smoke.py", "test_fanout_storage.py",
    "test_fanout_targets.py", "test_fanout_verification.py",
    "test_eval_provider.py", "test_tuneup_fanout.py",
)
PARALLEL_COMMAND = [
    "uvx", "--python", "3.12.14", "--from", "pytest==9.1.1",
    "--with", "pytest-xdist==3.8.0", "--with", "execnet==2.1.2",
    "pytest", "-q", "-n", "4", "--dist", "loadfile", "--max-worker-restart", "0",
]


def test_executor_gate_changes_scheduling_without_dropping_or_duplicating_cases():
    command = eval_harness.DETERMINISTIC_GATED["llm-fanout-execute"]
    first_file = next(i for i, argument in enumerate(command) if argument.endswith(".py"))
    assert command[:first_file] == PARALLEL_COMMAND
    assert command[first_file:] == [str(ROOT / "tests" / name) for name in EXECUTOR_MEMBERS]
    planner = eval_harness.DETERMINISTIC_GATED["llm-fanout-plan"]
    assert "-n" not in planner and "--dist" not in planner
    assert str(ROOT / "tests/test_render_packaging.py") in planner


@pytest.mark.parametrize("target", ["fanout-test", "core-test"])
def test_make_launches_audited_members_once_and_keeps_other_members_serial(tmp_path, target):
    log = tmp_path / "launches.jsonl"
    binaries = tmp_path / "bin"
    binaries.mkdir()
    # Replace only the external launch boundary; execute the real Makefile recipes.
    source = "#!" + sys.executable + "\n" + """
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
if name == 'python-probe':
    raise SystemExit(1)
with open(os.environ['GATE_LAUNCH_LOG'], 'a') as handle:
    handle.write(json.dumps([name, *sys.argv[1:]]) + '\\n')
"""
    for name in ("uvx", "mise", "python-probe"):
        binary = binaries / name
        binary.write_text(source)
        binary.chmod(0o700)
    environment = dict(os.environ, PATH=str(binaries) + os.pathsep + os.environ["PATH"],
                       GATE_LAUNCH_LOG=str(log))
    result = subprocess.run(
        ["make", "-f", str(ROOT / "Makefile"), target, "PY=" + str(binaries / "python-probe")],
        cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    launches = [json.loads(line) for line in log.read_text().splitlines()]
    pytest_launches = [argv for argv in launches if argv[0] == "uvx"]
    if target == "core-test":
        assert len(pytest_launches) == 1 and "-n" not in pytest_launches[0]
        assert "tests/test_render_packaging.py" in pytest_launches[0]
        return
    assert len(pytest_launches) == 2
    parallel, serial = pytest_launches
    assert parallel[:len(PARALLEL_COMMAND)] == PARALLEL_COMMAND
    assert "-n" not in serial and "--dist" not in serial
    named = lambda argv: [Path(argument).name for argument in argv if argument.endswith(".py")]
    assert Counter(named(parallel) + named(serial)) == Counter(FANOUT_MEMBERS)
    assert set(named(parallel)) == set(EXECUTOR_MEMBERS) - {"test_skill_delivery.py"}
    assert "test_fanout_plan_skill.py" in named(serial)
    assert len([argv for argv in launches if argv[0] == "mise"]) == 1


@pytest.mark.parametrize("summary,accepted,counts", [
    ("4 workers [8 items]\n........ [100%]\n8 passed in 1.20s\n", True,
     {"tests_run": 8, "skipped": 0, "failed": 0}),
    ("4 workers [8 items]\n7 passed, 1 skipped in 1.20s\n", False,
     {"tests_run": 7, "skipped": 1, "failed": 0}),
    ("4 workers [8 items]\n1 failed, 7 passed in 1.20s\n", False,
     {"tests_run": 8, "skipped": 0, "failed": 1}),
    ("4 workers [0 items]\nno tests ran in 0.20s\n", False,
     {"tests_run": 0, "skipped": 0, "failed": 0}),
])
def test_xdist_controller_counts_preserve_strict_receipt_acceptance(summary, accepted, counts):
    measured = eval_harness._pytest_counts(summary)
    assert measured == counts
    assert eval_harness._counts_are_evidence(measured) is accepted


def test_campaign_captures_every_executor_before_any_derived_publication(tmp_path, monkeypatch):
    """Moving grade/publication back inside capture exposes peer outputs mid-campaign."""
    skill = "chunk-map"
    directory = tmp_path / "evals" / skill
    directory.mkdir(parents=True)
    cases = [{"id": index, "name": f"case{index}", "prompt": "Answer",
              "assertions": ["correct"]} for index in range(2)]
    (directory / "evals.json").write_text(json.dumps({"evals": cases}))
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("# Synthetic guidance\n")
    monkeypatch.setattr(eval_harness, "EVALS_ROOT", directory.parent)
    monkeypatch.setattr(eval_harness, "skill_source_dir", lambda *_: source)
    monkeypatch.setattr(eval_harness.eval_runtime, "resolve_mode_config", lambda _: {
        "claude": {"model": "test"}, "codex": {"model": "test"}})
    monkeypatch.setattr(eval_harness.eval_runtime, "effective_timeout", lambda _: 30)
    monkeypatch.setattr(eval_harness, "observe_cli_version", lambda _: b"fixture 1.0")
    monkeypatch.setattr(eval_harness, "_write_receipt", lambda *a, **kw: None)
    captures = []
    judges = []

    def turn(provider, prompt, cfg, workdir, **kwargs):
        judge = workdir.name.startswith(("judge", "compare-"))
        if judge:
            assert len(captures) == 8
            judges.append(workdir)
            text = ('{"winner":"A"}' if workdir.name.startswith("compare-") else
                    '{"expectations":[{"passed":true}]}')
        else:
            assert not judges
            assert not list(directory.rglob("answer.md"))
            assert not list(directory.rglob("grading.json"))
            assert not list(directory.rglob("comparison.*.json"))
            captures.append((provider, workdir))
            text = "raw answer"
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / "raw.txt").write_text(text)
        return text, {"valid": True, "reason": "ok", "duration_sec": 0.01}

    monkeypatch.setattr(eval_harness, "run_text", turn)
    args = eval_harness.parse_args(["--skill", skill, "--providers", "claude,codex",
                                   "--iteration", "1"])
    assert eval_harness.run(args) == 0
    assert len(captures) == len(set(captures)) == 8
    assert len(list(directory.rglob("answer.md"))) == 8
    assert len(list(directory.rglob("grading.json"))) == 8
    assert len(list(directory.rglob("comparison.*.json"))) == 4


def test_campaign_refuses_reused_iteration_before_executor(measured_campaign, monkeypatch):
    args, directory, calls, artifacts = measured_campaign
    prior = directory / "workspace/iteration-1/peer/answer.md"
    prior.parent.mkdir(parents=True)
    prior.write_text("retained historical answer")
    assert eval_harness.run(args) == 1
    assert calls == []
    assert prior.read_text() == "retained historical answer"
