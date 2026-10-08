"""The pilot retains evidence, refuses drift, and never estimates route tokens."""
from __future__ import annotations

import importlib.util
import json
import shutil
import shlex
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests/fixtures/context_efficiency/model-input"


@pytest.fixture(scope="module")
def pilot():
    path = ROOT / "scripts/context_efficiency_pilot.py"
    assert path.exists(), "bounded pilot harness is missing"
    spec = importlib.util.spec_from_file_location("context_efficiency_pilot", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def codex_result(answer='{"cases":[]}', *, usage=True, read=True):
    events = [{"type": "thread.started", "thread_id": "fixture-thread"},
              {"type": "turn.started"}]
    if read:
        events.append({"type": "item.completed", "item": {
            "id": "read-1", "type": "command_execution", "command": "cat sources/crate-attempts.py",
            "aggregated_output": "fixture read output", "exit_code": 0, "status": "completed"}})
    events.append({"type": "item.completed", "item": {
        "id": "answer", "type": "agent_message", "text": answer}})
    events.append({"type": "turn.completed", "usage": {
        "input_tokens": 1500, "cached_input_tokens": 1000, "output_tokens": 100
    }} if usage else {"type": "turn.completed"})
    return b"\n".join(json.dumps(event).encode() for event in events)


def test_native_usage_includes_first_mcp_later_reads_and_attribution(pilot):
    # Native terminal accounting covers the entire turn, including all tool traffic.
    events = [json.loads(line) for line in codex_result().splitlines()]
    events[2:2] = [{"type": "item.completed", "item": {
        "id": "mcp-first", "type": "mcp_tool_call", "status": "completed",
        "server": "fictional-reader", "tool": "read", "result": {"content": [{"text": "initial"}]}}},
        {"type": "item.completed", "item": {
        "id": "later", "type": "command_execution", "status": "completed", "exit_code": 0,
        "command": "rg citation sources", "aggregated_output": "later attributed retrieval"}}]
    result = pilot.parse_native("codex", b"\n".join(json.dumps(e).encode() for e in events))
    assert result["usage"] == {"input_tokens": 1500, "cached_input_tokens": 1000,
        "cache_creation_input_tokens": None, "output_tokens": 100,
        "provider_requests": None, "consumed_input_tokens": 1500}
    assert result["tool_access"] is True
    assert pilot.aggregate_usage([result["usage"], result["usage"]])["input_tokens"] == 3000


def test_missing_usage_and_counter_cannot_establish_savings(pilot):
    result = pilot.parse_native("codex", codex_result(usage=False))
    assert result["usage"]["input_tokens"] is None
    assert pilot.aggregate_usage([result["usage"]])["input_tokens"] is None
    assert pilot.comparison_decision(None, 20, counter_available=False) == "retain-baseline"
    assert pilot.comparison_decision(100, 20, counter_available=False) == "retain-baseline"


def test_claude_cache_categories_are_not_double_counted(pilot):
    raw = json.dumps({"type": "result", "subtype": "success", "is_error": False,
        "session_id": "fixture-claude", "result": "answer", "modelUsage": {"claude-opus-5-5": {}},
        "usage": {"input_tokens": 20, "output_tokens": 7,
                  "cache_read_input_tokens": 30, "cache_creation_input_tokens": 40}}).encode()
    result = pilot.parse_native("claude", raw)
    assert result["usage"]["input_tokens"] == 20
    assert result["usage"]["consumed_input_tokens"] == 90
    assert result["usage"]["cached_input_tokens"] == 30
    assert result["tool_access"] is None


def test_plan_has_control_first_and_22_distinct_outer_sessions(pilot):
    plan = pilot.campaign_plan()
    assert len(plan) == 22
    assert len({session["id"] for session in plan}) == 22
    assert plan[0]["kind"] == "baseline-control"
    assert len([s for s in plan if s["kind"] == "baseline-control"]) == 4
    assert len([s for s in plan if s["kind"] == "trial"]) == 16
    assert [s["arm_order"] for s in plan if s["kind"] == "evaluation"] == [["A", "B"], ["B", "A"]]
    assert all(s["observed_condition"] is None for s in plan)


def test_source_and_corpus_drift_refused(pilot, tmp_path):
    copied = tmp_path / "corpus"
    shutil.copytree(CORPUS, copied)
    expected = pilot.corpus_identity(CORPUS)
    source = copied / "sources/crate-attempts.py"
    source.write_bytes(source.read_bytes() + b"\nchanged\n")
    with pytest.raises(pilot.PilotError, match="source"):
        pilot.corpus_identity(copied)
    shutil.copyfile(CORPUS / "sources/crate-attempts.py", source)
    manifest = json.loads((copied / "manifest.json").read_text())
    manifest["cases"][0]["question"] = "drift"
    (copied / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(pilot.PilotError, match="corpus"):
        pilot.verify_identity(copied, expected)


def test_executor_directory_contains_only_fictional_input(pilot, tmp_path):
    identity = pilot.corpus_identity(CORPUS)
    pilot.prepare_input(CORPUS, tmp_path / "seat", identity)
    assert {p.name for p in (tmp_path / "seat").iterdir()} == {"manifest.json", "sources"}
    assert not list((tmp_path / "seat").rglob("gold.json"))
    assert pilot.corpus_identity(tmp_path / "seat") == identity


def test_failed_control_retained_and_recovery_does_not_replay(pilot, tmp_path):
    campaign = pilot.Campaign(tmp_path / "run", CORPUS)
    session = campaign.plan[0]
    campaign.reserve(session["id"])
    campaign.finish(session["id"], {"valid": False, "reason": "protocol-error"})
    recovered = pilot.Campaign(tmp_path / "run", CORPUS)
    assert recovered.manifest["outer_session_attempts"] == 1
    assert recovered.manifest["attempts"][0]["result"]["reason"] == "protocol-error"
    with pytest.raises(pilot.PilotError, match="stopped"):
        recovered.reserve(recovered.plan[1]["id"])
    with pytest.raises(pilot.PilotError):
        recovered.reserve(session["id"])


def test_uncertain_attempt_stops_campaign_without_retry(pilot, tmp_path):
    campaign = pilot.Campaign(tmp_path / "run", CORPUS)
    campaign.reserve(campaign.plan[0]["id"])
    recovered = pilot.Campaign(tmp_path / "run", CORPUS)
    assert recovered.manifest["attempts"][0]["status"] == "uncertain"
    assert recovered.manifest["outer_session_attempts"] == 1
    with pytest.raises(pilot.PilotError, match="uncertain"):
        recovered.reserve(campaign.plan[1]["id"])


def test_budget_is_persistent_and_out_of_order_launches_refused(pilot, tmp_path):
    campaign = pilot.Campaign(tmp_path / "run", CORPUS)
    with pytest.raises(pilot.PilotError, match="order"):
        campaign.reserve(campaign.plan[4]["id"])
    for session in campaign.plan:
        campaign.reserve(session["id"])
        campaign.finish(session["id"], {"valid": True, "tool_access": True})
    recovered = pilot.Campaign(tmp_path / "run", CORPUS)
    assert recovered.manifest["outer_session_attempts"] == 22
    with pytest.raises(pilot.PilotError):
        recovered.reserve(recovered.plan[0]["id"])


def test_native_commands_preserve_skills_and_read_tools(pilot, tmp_path):
    argv = pilot.native_argv("codex", tmp_path)
    assert "--json" in argv and argv[argv.index("--sandbox") + 1] == "read-only"
    for provider in ("codex", "claude", "agy"):
        command = pilot.native_argv(provider, tmp_path)
        assert not {"--ignore-user-config", "--restricted", "--safe-mode", "--tools", "--disable-slash-commands", "--strict-mcp-config"}.intersection(command)
    command = pilot.native_argv("maka", tmp_path, evaluation=True)
    assert "--connection" not in command
    assert command[command.index("--thinking") + 1] == "xhigh"


def test_plain_maka_answer_does_not_invent_usage_or_tool_access(pilot):
    result = pilot.parse_native("maka", b"A valid native answer")
    assert result["valid"] is True
    assert result["answer"] == "A valid native answer"
    assert result["tool_access"] is None
    assert all(value is None for value in result["usage"].values())


def test_case_outputs_preserve_source_and_locator_and_reject_unknown_ids(pilot):
    manifest = json.loads((CORPUS / "manifest.json").read_text())
    packet = {"cases": [{"id": case["id"], "answer": "fixture answer",
        "evidence": case["citations"]} for case in manifest["cases"]]}
    outputs = pilot.case_outputs(json.dumps(packet), manifest)
    assert len(outputs) == 20
    assert outputs[0]["id"] == "ce-01"
    assert outputs[0]["evidence"][0]["source_id"] == "synthetic:crate-attempts:r1"
    packet["cases"][0]["id"] = "ce-missing"
    with pytest.raises(pilot.PilotError, match="case"):
        pilot.case_outputs(json.dumps(packet), manifest)


def test_case_outputs_reject_fabricated_evidence(pilot):
    manifest = json.loads((CORPUS / "manifest.json").read_text())
    packet = {"cases": [{"id": case["id"], "answer": "fixture answer",
        "evidence": case["citations"]} for case in manifest["cases"]]}
    packet["cases"][0]["evidence"] = [{"source_id": "synthetic:invented:r1", "locator": {"kind": "lines", "start": 1, "end": 1}}]
    with pytest.raises(pilot.PilotError, match="evidence"):
        pilot.case_outputs(json.dumps(packet), manifest)


def test_started_pid_survives_uncertain_recovery(pilot, tmp_path):
    campaign = pilot.Campaign(tmp_path / "run", CORPUS)
    campaign.reserve(campaign.plan[0]["id"])
    campaign.started(campaign.plan[0]["id"], 12345)
    recovered = pilot.Campaign(tmp_path / "run", CORPUS)
    assert recovered.manifest["confirmed_outer_launches"] == 1
    assert recovered.manifest["attempts"][0]["pid"] == 12345
    assert recovered.manifest["attempts"][0]["status"] == "uncertain"


def test_control_without_read_telemetry_retains_unknown(pilot, tmp_path):
    campaign = pilot.Campaign(tmp_path / "run", CORPUS)
    campaign.reserve(campaign.plan[0]["id"])
    campaign.finish(campaign.plan[0]["id"], pilot.parse_native("codex", codex_result(read=False)))
    assert campaign.manifest["stopped"] is None
    assert campaign.manifest["attempts"][0]["result"]["tool_access"] is None


def test_observed_tool_failure_stops_campaign(pilot, tmp_path):
    campaign = pilot.Campaign(tmp_path / "run", CORPUS)
    campaign.reserve(campaign.plan[0]["id"])
    campaign.finish(campaign.plan[0]["id"], {"valid": True, "tool_access": False})
    assert campaign.manifest["stopped"] == "required-tool-access-failed"
    with pytest.raises(pilot.PilotError, match="stopped"):
        campaign.reserve(campaign.plan[1]["id"])


def test_fixture_process_uses_real_supervisor_and_retains_artifacts(pilot, tmp_path):
    campaign = pilot.Campaign(tmp_path / "run", CORPUS)
    manifest = json.loads((CORPUS / "manifest.json").read_text())
    packet = {"cases": [{"id": case["id"], "answer": "fixture answer",
        "evidence": case["citations"]} for case in manifest["cases"]]}
    raw = codex_result(json.dumps(packet))
    result = pilot.execute_session(campaign, campaign.plan[0],
        argv=(sys.executable, "-c", "import sys; sys.stdin.read(); print(" + repr(raw.decode()) + ")"))
    assert result["valid"] is True
    attempt = campaign.manifest["attempts"][0]
    assert attempt["status"] == "complete"
    assert (campaign.root / attempt["stdout_ref"]["path"]).read_bytes().strip() == raw
    assert len(attempt["case_refs"]) == 20
    assert campaign.manifest["confirmed_outer_launches"] == 1
    assert attempt["elapsed_seconds"] >= 0


def test_dry_run_is_read_only_and_launches_nothing(pilot, tmp_path, capsys):
    destination = tmp_path / "never-created"
    assert pilot.main(["--dry-run", "--run-dir", str(destination)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["planned_outer_sessions"] == 22
    assert output["paid_launches"] == 0
    assert output["counter_available"] is False
    assert output["decision"] == "retain-baseline"
    assert not destination.exists()


def test_agy_stdin_uses_the_established_native_user_envelope(pilot, tmp_path):
    campaign = pilot.Campaign(tmp_path / "run", CORPUS)
    for session in campaign.plan[:2]:
        campaign.reserve(session["id"])
        campaign.finish(session["id"], {"valid": True, "tool_access": True})
    manifest = json.loads((CORPUS / "manifest.json").read_text())
    packet = {"cases": [{"id": case["id"], "answer": "fixture answer",
        "evidence": case["citations"]} for case in manifest["cases"]]}
    raw = b'\n'.join(json.dumps(event).encode() for event in [
        {"event": "init", "init": {}, "conversation_id": "fixture-agy"},
        {"event": "result", "result": {"conversation_id": "fixture-agy", "status": "SUCCESS",
            "response": json.dumps(packet), "model": "gemini-3.8-flash-high"}}])
    script = ("import json, sys; packet=json.load(sys.stdin); "
        "assert packet['event']=='user'; assert isinstance(packet['message']['content'], str); "
        "print(" + repr(raw.decode()) + ")")
    result = pilot.execute_session(campaign, campaign.plan[2], argv=(sys.executable, "-c", script))
    assert result["valid"] is True
    assert result["tool_access"] is None


def test_required_codex_read_failure_is_not_masked_by_unrelated_success(pilot, tmp_path):
    events = [json.loads(line) for line in codex_result(read=False).splitlines()]
    events[2:2] = [{"type": "item.completed", "item": {
        "id": "required", "type": "command_execution", "command": "cat sources/crate-attempts.py",
        "status": "failed", "exit_code": 1, "aggregated_output": "Permission denied"}},
        {"type": "item.completed", "item": {
        "id": "unrelated", "type": "command_execution", "command": "printf hello",
        "status": "completed", "exit_code": 0, "aggregated_output": "hello"}}]
    result = pilot.parse_native("codex", b"\n".join(json.dumps(e).encode() for e in events))
    assert result["tool_access"] is False
    campaign = pilot.Campaign(tmp_path / "run", CORPUS)
    campaign.reserve(campaign.plan[0]["id"])
    campaign.finish(campaign.plan[0]["id"], result)
    assert campaign.manifest["stopped"] == "required-tool-access-failed"


@pytest.mark.parametrize("command", ["printf hello", "cat sources/profiles.json", "printf 'cat sources/crate-attempts.py'"])
def test_unrelated_codex_command_never_certifies_required_read(pilot, command):
    events = [json.loads(line) for line in codex_result().splitlines()]
    events[2]["item"]["command"] = command
    result = pilot.parse_native("codex", b"\n".join(json.dumps(e).encode() for e in events))
    assert result["tool_access"] is None


def test_unbound_mcp_success_never_certifies_required_read(pilot):
    events = [json.loads(line) for line in codex_result(read=False).splitlines()]
    events.insert(2, {"type": "item.completed", "item": {
        "id": "unrelated", "type": "mcp_tool_call", "status": "completed", "server": "fictional",
        "tool": "read", "arguments": {"path": "sources/profiles.json"}, "result": {"content": [{"text": "hello"}]}}})
    result = pilot.parse_native("codex", b"\n".join(json.dumps(e).encode() for e in events))
    assert result["tool_access"] is None


def test_claude_required_read_links_tool_ids_and_ignores_other_success(pilot):
    terminal = {"type": "result", "subtype": "success", "is_error": False,
        "session_id": "fixture-claude", "result": "answer"}
    events = [{"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "required", "name": "Read", "input": {"file_path": "sources/crate-attempts.py"}},
        {"type": "tool_use", "id": "other", "name": "Read", "input": {"file_path": "sources/profiles.json"}}]}},
        {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "required", "is_error": True, "content": "Permission denied"},
        {"type": "tool_result", "tool_use_id": "other", "is_error": False, "content": "hello"}]}}, terminal]
    result = pilot.parse_native("claude", b"\n".join(json.dumps(e).encode() for e in events))
    assert result["tool_access"] is False
    events[1]["message"]["content"][0].update(is_error=False, content="requested source read")
    result = pilot.parse_native("claude", b"\n".join(json.dumps(e).encode() for e in events))
    assert result["tool_access"] is True
    events[1]["message"]["content"][0]["tool_use_id"] = "unlinked"
    result = pilot.parse_native("claude", b"\n".join(json.dumps(e).encode() for e in events))
    assert result["tool_access"] is None


def test_unavailable_candidate_advances_durably_without_launch(pilot, tmp_path):
    campaign = pilot.Campaign(tmp_path / "run", CORPUS)
    for session in campaign.plan[:5]:
        campaign.reserve(session["id"])
        campaign.finish(session["id"], {"valid": True, "tool_access": True})
    result = pilot.execute_session(campaign, campaign.plan[5])
    assert result["reason"] == "counter-unavailable"
    recovered = pilot.Campaign(tmp_path / "run", CORPUS)
    assert recovered.next_session()["id"] == "codex-baseline-warm"
    assert recovered.manifest["outer_session_attempts"] == 5
    assert recovered.manifest["confirmed_outer_launches"] == 0
    assert recovered.manifest["unavailable_steps"] == [{"id": "codex-candidate-cold", "reason": "counter-unavailable"}]
    assert not (campaign.root / "seats/codex-candidate-cold").exists()


def test_skips_preserve_uncertain_fence_and_all_baselines_reachable(pilot, tmp_path):
    campaign = pilot.Campaign(tmp_path / "run", CORPUS)
    while session := campaign.next_session():
        if session["kind"] == "evaluation" or session.get("arm") == "candidate":
            pilot.execute_session(campaign, session)
        else:
            campaign.reserve(session["id"])
            campaign.finish(session["id"], {"valid": True, "tool_access": True})
    recovered = pilot.Campaign(tmp_path / "run", CORPUS)
    assert recovered.next_session() is None
    assert recovered.manifest["outer_session_attempts"] == 12
    assert len(recovered.manifest["unavailable_steps"]) == 10
    assert len([attempt for attempt in recovered.manifest["attempts"] if attempt["kind"] == "trial"]) == 8
    uncertain = pilot.Campaign(tmp_path / "uncertain", CORPUS)
    uncertain.reserve(uncertain.plan[0]["id"])
    with pytest.raises(pilot.PilotError, match="uncertain"):
        uncertain.unavailable(uncertain.plan[5]["id"], "counter-unavailable")


def test_execute_next_uses_processed_steps_instead_of_paid_attempt_count(pilot, tmp_path, capsys, monkeypatch):
    campaign = pilot.Campaign(tmp_path / "run", CORPUS)
    for session in campaign.plan[:5]:
        campaign.reserve(session["id"])
        campaign.finish(session["id"], {"valid": True, "tool_access": True})
    pilot.execute_session(campaign, campaign.plan[5])
    campaign.reserve(campaign.plan[6]["id"])
    campaign.finish(campaign.plan[6]["id"], {"valid": True, "tool_access": True})
    def refuse_native_probe(provider):
        raise AssertionError("a non-paid unavailable step must not probe or launch a native CLI")
    monkeypatch.setattr(pilot, "check_native_version", refuse_native_probe)
    assert pilot.main(["--execute-next", "--run-dir", str(campaign.root)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["outer_session_attempts"] == 6
    assert output["unavailable_steps"] == 2
    recovered = pilot.Campaign(campaign.root, CORPUS)
    assert recovered.next_session()["id"] == "claude-candidate-cold"


def test_unavailable_state_cannot_skip_a_baseline_or_replay_processed_step(pilot, tmp_path):
    campaign = pilot.Campaign(tmp_path / "run", CORPUS)
    with pytest.raises(pilot.PilotError, match="unavailable"):
        campaign.unavailable(campaign.plan[0]["id"], "counter-unavailable")
    assert campaign.manifest["outer_session_attempts"] == 0
    assert campaign.manifest["unavailable_steps"] == []
    for session in campaign.plan[:5]:
        campaign.reserve(session["id"])
        campaign.finish(session["id"], {"valid": True, "tool_access": True})
    pilot.execute_session(campaign, campaign.plan[5])
    with pytest.raises(pilot.PilotError, match="order"):
        campaign.reserve(campaign.plan[5]["id"])
    with pytest.raises(pilot.PilotError, match="order"):
        campaign.unavailable(campaign.plan[5]["id"], "counter-unavailable")


@pytest.mark.parametrize("command", [
    "sed -n '1,40p' sources/crate-attempts.py",
    "/bin/zsh -lc 'cat sources/crate-attempts.py'",
    "cat -- sources/crate-attempts.py",
])
def test_literal_requested_source_reads_remain_observable(pilot, command):
    events = [json.loads(line) for line in codex_result().splitlines()]
    events[2]["item"]["command"] = command
    result = pilot.parse_native("codex", b"\n".join(json.dumps(e).encode() for e in events))
    assert result["tool_access"] is True


def test_absolute_source_read_is_bound_to_executor_cwd(pilot, tmp_path):
    events = [json.loads(line) for line in codex_result().splitlines()]
    events[2]["item"]["command"] = "cat " + shlex.quote(str(tmp_path / "sources/crate-attempts.py"))
    raw = b"\n".join(json.dumps(e).encode() for e in events)
    assert pilot.parse_native("codex", raw, cwd=tmp_path)["tool_access"] is True
    assert pilot.parse_native("codex", raw, cwd=tmp_path / "other")["tool_access"] is None


def test_ambiguous_claude_result_is_not_positive_read_evidence(pilot):
    events = [{"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "read", "name": "Read", "input": {"file_path": "sources/crate-attempts.py"}}]}},
        {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "read", "is_error": False, "content": True}]}},
        {"type": "result", "subtype": "success", "is_error": False,
        "session_id": "fixture-claude", "result": "answer"}]
    result = pilot.parse_native("claude", b"\n".join(json.dumps(e).encode() for e in events))
    assert result["tool_access"] is None
