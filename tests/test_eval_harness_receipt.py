"""What a receipt is evidence OF.

`receipt_gate` compared two input hashes, and `_write_receipt` checked a subprocess exit
code. Neither asks whether anything was TESTED — so an all-skipped run, a command that runs
zero tests, and a certifier weakened between runs all left a fresh green receipt.
"""
import json
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
import fanout_baseline  # noqa: E402


@pytest.mark.parametrize("judge,effort", [("codex", "xhigh"), ("claude", "xhigh"), ("agy", "high")])
def test_judge_launch_uses_review_effort_without_changing_executor_config(tmp_path, monkeypatch, judge, effort):
    cfg = {judge: {"model": "explicit-model", "thinking": "medium"}}
    argv = []
    def turn(provider, prompt, config, workdir, **kwargs):
        spec = eval_harness.eval_runtime.build_spec(provider, prompt, config)
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
        assert "--effort" not in argv[0]  # no-tools agy tier stays in its model


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
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Test skill\nAnswer directly.\n")
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

    monkeypatch.setattr(fanout_baseline, "installed_version", probe)
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
                                    fanout_baseline.BaselineError("installed CLI unavailable")])
def test_failed_initial_version_measurement_refuses_receipt_and_spend(
    measured_campaign, monkeypatch, failure, capsys,
):
    args, eval_dir, calls, artifacts = measured_campaign

    def probe(_provider):
        if isinstance(failure, Exception):
            raise failure
        return failure

    monkeypatch.setattr(fanout_baseline, "installed_version", probe)
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

    monkeypatch.setattr(fanout_baseline, "installed_version", probe)
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


def test_old_development_cli_measurement_does_not_expire_unchanged_receipt(
    tmp_path, monkeypatch,
):
    receipt = json.loads((ROOT / "evals/chunk-map/receipt.json").read_text())
    receipt["cli_versions"] = {"codex": "codex-cli 0.159.0"}
    eval_dir = tmp_path / "evals/chunk-map"
    eval_dir.mkdir(parents=True)
    (eval_dir / "receipt.json").write_text(json.dumps(receipt))
    monkeypatch.setattr(checks, "source_hash", lambda _root, _skill: receipt["source_hash"])
    monkeypatch.setattr(checks, "eval_set_hash", lambda _root, _skill: receipt["eval_set_hash"])
    monkeypatch.setattr(fanout_baseline, "installed_version",
                        lambda _provider: pytest.fail("receipt reader probed host CLI"))
    assert checks.validate_receipt(tmp_path, "chunk-map") == []

    monkeypatch.setattr(checks, "source_hash", lambda _root, _skill: "changed-source")
    assert any("changed since last eval" in problem
               for problem in checks.validate_receipt(tmp_path, "chunk-map"))


def test_the_deterministic_gate_runs_the_whole_forge_suite_not_three_modules():
    """The receipt names `forge-handover-cli-gc-suites` and the Makefile names 31 suites.

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
    executable = eval_harness.DETERMINISTIC_GATED["llm-forge"]
    portable = eval_harness.portable_gate_command(executable)

    assert portable[:5] == executable[:5]
    assert all(not Path(argument).is_absolute() for argument in portable)
    assert all(str(Path.home()) not in argument for argument in portable)
    assert {Path(argument).name for argument in portable if argument.endswith(".py")} == {
        path.name for path in (ROOT / "tests").glob("test_forge_*.py")
    }


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

    assert checks.validate_receipt(tmp_path, "example") == []


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
    paths = [rel for rel, _h in checks.source_manifest(ROOT, "llm-forge")]
    for rel in ("scripts/eval_harness.py", "scripts/lib/checks.py", "Makefile"):
        assert rel in paths, f"{rel} is not in llm-forge's source closure"
    assert len(paths) == len(set(paths)), \
        f"a file is hashed twice: {sorted({p for p in paths if paths.count(p) > 1})}"


@pytest.mark.parametrize("skill", ["chunk-map", "khenrix-setup"])
@pytest.mark.parametrize("instrument", ["shared/lib/fanout/eval_provider.py",
                                        "shared/lib/fanout/providers.py",
                                        "shared/lib/fanout/process.py",
                                        "shared/lib/fanout/agy_guard.py",
                                        "shared/lib/fanout/artifacts.py",
                                        "shared/lib/fanout/errors.py",
                                        "shared/lib/fanout/skills.py",
                                        "scripts/fanout_baseline.py"])
def test_eval_instrument_changes_stale_every_behavior_eval_receipt(tmp_path, skill, instrument):
    """Changing the evaluator or its version observer must invalidate revision evidence."""
    repo = tmp_path / "repo"
    manifest = checks.source_manifest(ROOT, skill)
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
    receipt = {
        "source_hash": checks.source_hash(repo, skill),
        "eval_set_hash": checks.eval_set_hash(repo, skill),
        "provenance": "eval",
        "certified_by": "delta-gate",
    }
    (eval_dir / "receipt.json").write_text(json.dumps(receipt))
    assert checks.validate_receipt(repo, skill) == []

    changed = repo / instrument
    changed.write_bytes(changed.read_bytes() + b"\n# simulated certifier change\n")

    problems = checks.validate_receipt(repo, skill)
    assert any("changed since last eval" in problem for problem in problems)


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
    monkeypatch.setattr(fanout_baseline, "installed_version", lambda _provider: b"test-cli 1.0\n")

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
    assert not (eval_dir / "workspace" / "iteration-1" / "eval-1-second").exists()
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
def test_exhausted_judge_retains_completed_and_inflight_partial_benchmark(
    measured_campaign, monkeypatch, phase, retry_failure,
):
    args, eval_dir, calls, artifacts = measured_campaign
    evals = [{"id": number, "name": f"case-{number}", "prompt": "Answer", "assertions": ["correct"]}
             for number in range(3)]
    (eval_dir / "evals.json").write_text(json.dumps({"evals": evals}))
    monkeypatch.setattr(fanout_baseline, "installed_version", lambda _provider: b"fixture-cli 1.0\n")
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
    assert not (iteration / "eval-2-case-2").exists()
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
def test_malformed_advisory_only_certifies_actual_nonzero_noskip_owning_success(
    measured_campaign, monkeypatch, gate,
):
    args, old_dir, calls, artifacts = measured_campaign
    eval_dir = old_dir.with_name("llm-fanout-plan")
    old_dir.rename(eval_dir)
    args.skill = "llm-fanout-plan"
    monkeypatch.setattr(fanout_baseline, "installed_version", lambda _provider: b"fixture-cli 1.0\n")
    def malformed(provider, _prompt, _cfg, workdir, **_kwargs):
        calls.append(provider)
        raw = "{malformed" if workdir.name.startswith("judge") else "completed executor"
        workdir.mkdir(parents=True, exist_ok=True)
        path = workdir / f"{provider}.stdout.attempt-1.txt"
        path.write_text(raw)
        artifacts.append((path, raw))
        return raw, {"valid": True, "reason": "ok", "duration_sec": 0.1}
    monkeypatch.setattr(eval_harness, "run_text", malformed)
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
    assert len(benchmark["runs"]) == 1 and benchmark["runs"][0]["result"]["judge_error"] == 1
    assert len(artifacts) == 3
    assert len(observed) == 1
    assert observed[0].returncode == (1 if gate == "failed" else 0)
    assert [path.read_text() for path, _raw in artifacts] == [raw for _path, raw in artifacts]


def test_execute_receipt_selects_material_makefile_target_handover_and_eval_owners():
    named = {Path(argument).name for argument in eval_harness.DETERMINISTIC_GATED["llm-fanout-execute"]
             if argument.endswith(".py")}
    assert named >= {"test_fanout_branch_handover.py", "test_fanout_targets.py", "test_eval_provider.py"}
    command = eval_harness.portable_gate_command(eval_harness.DETERMINISTIC_GATED["llm-fanout-execute"])
    assert all(not Path(argument).is_absolute() for argument in command)
