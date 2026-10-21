"""Native counters keep their scopes through reporting and resumed turns."""
import json
from dataclasses import replace

import pytest

from test_fanout_native_profiles import fanout, process, providers, SPEC


def codex_usage(tmp_path, *, session="shared", total=100, cached=40, role="executor", resume=False):
    raw = b"\n".join(json.dumps(row).encode() for row in [
        {"type": "thread.started", "thread_id": session},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "answer"}},
        {"type": "turn.completed", "usage": {"input_tokens": total,
            "cached_input_tokens": cached, "output_tokens": total // 10, "extra_counter": 3.5}},
    ]) + b"\n"
    profile = fanout.ProviderRegistry.default().select("codex", "read-only", "standard", role=role)
    request = fanout.ProviderRequest("codex", "read evidence", cwd=tmp_path, profile=profile,
        role=role, resume=resume, session_id=session if resume else None)
    parsed = profile.adapter.parse(raw)
    result = providers._make_result(request, profile.adapter,
        process.ProcessResult(process.ProcessStatus.EXIT, 0, raw, b""), parsed, 1, 0.1)
    return result


def test_raw_numeric_usage_survives_scalar_compatibility_and_result_binding(tmp_path):
    result = codex_usage(tmp_path)
    assert result.usage == {"input": 100, "output": 10, "cache_read": 40}
    evidence = result.usage_evidence.to_dict()
    assert evidence["raw_counters"]["usage.extra_counter"] == 3.5
    assert evidence["counters"]["input"] == {
        "value": 100, "scope": "session-cumulative", "kind": "provider-reported", "status": "reported"}
    assert evidence["role"] == "executor" and evidence["resumed"] is False
    assert evidence["session_id"] == "shared"
    assert evidence["scope_evidence"] == "upstream-source-uncharacterized"
    assert providers.UsageEvidence.from_dict(evidence).to_dict() == evidence


def test_all_roles_and_reference_turns_share_cumulative_session_accounting(tmp_path):
    initial = codex_usage(tmp_path, total=100)
    reference = codex_usage(tmp_path, total=130, cached=50, resume=True)
    owner = codex_usage(tmp_path, total=180, cached=60, role="owner", resume=True)
    judge = codex_usage(tmp_path, session="judge", total=70, cached=10, role="evaluator")
    summary = providers.aggregate_usage_evidence([
        initial.usage_evidence, reference.usage_evidence, owner.usage_evidence,
        judge.usage_evidence, owner.usage_evidence], required_roles=("executor", "owner", "evaluator"))
    assert summary["totals"]["input"] == 250
    assert summary["totals"]["consumed_input"] == 250
    assert summary["totals"]["cache_read"] == 70
    assert summary["by_role"]["executor"]["input"] == 130
    assert summary["by_role"]["owner"]["input"] == 50
    assert summary["by_role"]["evaluator"]["input"] == 70
    assert summary["entries"] == 4
    assert summary["measured_savings_eligible"] is False


def test_unseen_resume_baseline_and_counter_reset_remain_unknown(tmp_path):
    resumed = codex_usage(tmp_path, total=180, resume=True)
    assert providers.aggregate_usage_evidence([resumed.usage_evidence])["totals"]["input"] is None
    first = codex_usage(tmp_path, total=100)
    reset = codex_usage(tmp_path, total=50, resume=True)
    assert providers.aggregate_usage_evidence([first.usage_evidence, reset.usage_evidence])["totals"]["input"] is None


def test_cumulative_roles_use_capture_order_after_report_paths_are_sorted(tmp_path):
    first = codex_usage(tmp_path, total=100)
    owner = codex_usage(tmp_path, total=180, role="owner", resume=True)
    summary = providers.aggregate_usage_evidence([owner.usage_evidence, first.usage_evidence])
    assert summary["totals"]["input"] == 180
    assert summary["by_role"]["owner"]["input"] == 80


def test_wire_zero_is_preserved_without_claiming_observed_zero(tmp_path):
    zero = codex_usage(tmp_path, total=0, cached=0)
    assert zero.usage["input"] == 0
    assert zero.usage_evidence.to_dict()["counters"]["input"]["status"] == "unqualified-zero"
    assert providers.aggregate_usage_evidence([zero.usage_evidence])["totals"]["input"] is None


def test_missing_owner_or_evaluator_cannot_look_like_a_complete_experiment(tmp_path):
    result = codex_usage(tmp_path)
    summary = providers.aggregate_usage_evidence([result.usage_evidence],
        required_roles=("executor", "owner", "evaluator"))
    assert summary["missing_roles"] == ["evaluator", "owner"]
    assert summary["totals"]["input"] is None
    assert summary["by_role"]["executor"]["input"] == 100


def test_claude_cost_estimate_and_main_loop_tokens_have_separate_scopes(tmp_path):
    raw = json.dumps({"type": "result", "subtype": "success", "is_error": False,
        "session_id": "claude-session", "result": "answer", "total_cost_usd": 0.4,
        "usage": {"input_tokens": 20, "output_tokens": 7,
            "cache_read_input_tokens": 30, "cache_creation_input_tokens": 40},
        "modelUsage": {"claude-opus-5-5": {"inputTokens": 120, "outputTokens": 17,
            "cacheReadInputTokens": 130, "cacheCreationInputTokens": 140, "costUSD": 0.4}}}).encode()
    parsed = fanout.ClaudeAdapter().parse(raw)
    evidence = parsed.usage_evidence.to_dict()
    assert evidence["counters"]["input"]["scope"] == "turn"
    assert evidence["counters"]["cost_usd"]["scope"] == "session-cumulative"
    assert evidence["counters"]["cost_usd"]["kind"] == "estimate"
    assert evidence["raw_counters"]["modelUsage.claude-opus-5-5.inputTokens"] == 120
    assert evidence["counters"]["consumed_input"]["value"] == 90
    assert evidence["coverage"] == "main-loop-only"


def test_claude_turn_tokens_sum_while_resumed_estimated_cost_uses_deltas():
    adapter = fanout.ClaudeAdapter()
    def evidence(tokens, cost, resumed):
        raw = json.dumps({"type": "result", "subtype": "success", "is_error": False,
            "session_id": "claude-session", "result": "answer", "total_cost_usd": cost,
            "usage": {"input_tokens": tokens, "output_tokens": 7,
                "cache_read_input_tokens": 30, "cache_creation_input_tokens": 40}}).encode()
        return replace(adapter.parse(raw).usage_evidence, resumed=resumed)
    summary = providers.aggregate_usage_evidence([evidence(20, 0.4, False), evidence(10, 0.6, True)])
    assert summary["totals"]["input"] == 30
    assert summary["totals"]["consumed_input"] == 170
    assert summary["totals"]["cost_usd"] == pytest.approx(0.6)
    assert summary["counter_kinds"]["cost_usd"] == "estimate"


def test_unknown_native_scope_is_not_summed_across_resumed_turns():
    adapter = fanout.AgyAdapter()
    def evidence(tokens, resumed):
        raw = b"\n".join(json.dumps(row).encode() for row in [
            {"event": "init", "init": {}, "conversation_id": "same-agy"},
            {"event": "result", "result": {"conversation_id": "same-agy", "status": "SUCCESS",
                "response": "answer", "usage": {"input_tokens": tokens, "output_tokens": 4}}}])
        return replace(adapter.parse(raw).usage_evidence, resumed=resumed)
    summary = providers.aggregate_usage_evidence([evidence(20, False), evidence(30, True)])
    assert summary["totals"]["input"] is None
    assert summary["totals"]["consumed_input"] is None


def test_usage_evidence_descriptor_rejects_changed_status_and_source_mismatch(tmp_path):
    result = codex_usage(tmp_path)
    document = result.usage_evidence.to_dict()
    document["counters"]["input"]["status"] = "measured"
    with pytest.raises(fanout.ProviderRequestError, match="changed semantics"):
        providers.UsageEvidence.from_dict(document)


def test_terminal_usage_roundtrip_preserves_legacy_absence_and_source_association(tmp_path):
    result = codex_usage(tmp_path)
    terminal = fanout.TerminalSeatResult(
        run_id="run", task_id="task", seat_id="seat", executor_id="codex", attempt=1, round=1,
        context_sha256="a" * 64, compiled_plan_sha256="b" * 64, skill_manifest_sha256="c" * 64,
        skill_bundle_sha256="d" * 64, staged_manifest_sha256="e" * 64,
        delivery_evidence_sha256="f" * 64, workspace_evidence_sha256=None,
        state="invalid", valid=False, reason="protocol-error", session_id="shared",
        answer_ref=None, stdout_ref=None, stderr_ref=None, answer_sha256="0" * 64)
    legacy = terminal.to_dict()
    assert "usage_evidence" not in legacy
    assert fanout.TerminalSeatResult.from_dict(legacy).to_dict() == legacy
    current = replace(terminal, usage_evidence=result.usage_evidence)
    assert fanout.TerminalSeatResult.from_dict(current.to_dict()).usage_evidence == result.usage_evidence
    with pytest.raises(fanout.CollaborationValidationError, match="usage.*provider"):
        replace(current, executor_id="claude")
    with pytest.raises(fanout.CollaborationValidationError, match="usage.*provider"):
        replace(current, stdout_ref=fanout.ArtifactRef("wrong-stdout", "1" * 64, 10))


def test_evaluation_totals_include_every_manifest_not_just_selected_verdicts(tmp_path):
    from test_eval_native_consumers import eval_harness
    executor = codex_usage(tmp_path)
    owner = codex_usage(tmp_path, session="owner", total=50, cached=10, role="owner")
    failed_judge = codex_usage(tmp_path, session="judge-first", total=20, cached=5, role="evaluator")
    final_judge = codex_usage(tmp_path, session="judge-second", total=30, cached=5, role="evaluator")
    for name, result in (("executor", executor), ("owner", owner),
                         ("judge", failed_judge), ("judge-retry", final_judge)):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "manifest.json").write_text(json.dumps({"providers": [{
            "valid": name != "judge", "usage_evidence": result.usage_evidence.to_dict()}]}))
    summary = eval_harness._experiment_usage_summary(tmp_path)
    assert summary["totals"]["input"] == 200
    assert summary["by_role"]["evaluator"]["input"] == 50
    assert summary["by_role"]["owner"]["input"] == 50
    assert summary["entries"] == 4
