"""Owned native Maka evaluator behavior; no model or service is contacted."""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from test_fanout_native_profiles import fanout

evaluation = importlib.import_module(f"{fanout.__name__}.evaluation")
transport_module = importlib.import_module(f"{fanout.__name__}.maka_transport")
process = importlib.import_module(f"{fanout.__name__}.process")


def _request():
    return evaluation.JudgeRequest("case", "interactive", "Check the cited fact", "A", "B",
                                   "gpt-6.1-sol", "xhigh", "api-key-relay")


def test_legacy_tool_free_transport_cannot_start_or_earn_native_ballot():
    calls = []

    class LegacyTransport:
        target_id = "maka"
        no_tools_characterization_sha256 = "a" * 64

        def judge(self, request):
            calls.append(request)
            return {"winner": "A"}

    with pytest.raises(evaluation.JudgeTransportError, match="native"):
        evaluation.MakaJudgeAdapter(LegacyTransport())
    assert calls == []


def test_failed_native_bookkeeping_retains_private_stderr_without_model_launch(tmp_path, monkeypatch):
    native, _ = _native(tmp_path, monkeypatch)
    calls = []

    def refuse(command, **kwargs):
        calls.append(command)
        return process.ProcessResult(process.ProcessStatus.EXIT, 1, b"partial", b"synthetic helper refusal")

    monkeypatch.setattr(transport_module, "run_command", refuse)
    with pytest.raises(evaluation.JudgeTransportError):
        native.evaluate("Read a synthetic fact.")
    assert len(calls) == 1
    attempt = next((tmp_path / "captures").iterdir())
    error = attempt / "session.stderr.bin"
    assert error.read_bytes() == b"synthetic helper refusal"
    assert error.stat().st_mode & 0o777 == 0o600
    assert not (attempt / "launch.json").exists()


def _native(tmp_path, monkeypatch, *, change=None, output=None, failed=False):
    package = tmp_path / "package"
    package.mkdir()
    launcher = tmp_path / "maka"
    launcher.write_text("#!/bin/sh\nexit 99\n")
    launcher.chmod(0o700)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            spec = json.loads(command.stdin)
            owned = {"session_id": spec["session_id"], "workspace_root": str(tmp_path),
                     "permission_mode": "bypass",
                     "host": {"pid": 123, "hostEpoch": "epoch"}}
            return process.ProcessResult(process.ProcessStatus.EXIT, 0, json.dumps(owned).encode(), b"")
        if len(calls) == 2:
            if failed:
                return process.ProcessResult(process.ProcessStatus.TIMEOUT, None, b"partial", b"timeout")
            return process.ProcessResult(process.ProcessStatus.EXIT, 0,
                output or b'{"winner":"B"}\n', b"")
        spec = json.loads(command.stdin)
        if spec.get("operation") == "cleanup":
            return process.ProcessResult(process.ProcessStatus.EXIT, 0,
                json.dumps({"session_id": spec["session_id"], "permission_mode": "bypass", "cleanup_complete": True}).encode(), b"")
        owned = {key: spec[key] for key in ("session_id", "workspace_root", "host", "permission_mode")}
        identity = {"sessionId": spec["session_id"], "invocationId": "inv", "runId": "run", "turnId": "turn"}
        def event(label, role, content, **extra):
            return {**identity, "id": label, "ts": 1, "partial": False,
                    "role": role, "author": "tool" if role == "tool" else "agent",
                    "content": content, **extra}
        invocation = {**identity, "opening": {"route": {"llmConnectionSlug": "keychain-openai", "modelId": "gpt-6.1-sol"},
                       "configuration": {"cwd": str(tmp_path), "permissionMode": "bypass", "collaborationMode": "agent"}},
                       "terminalEvent": event("end", "system", None, status="completed")}
        data = {**owned, "invocation": invocation, "records": [
            event("user", "user", {"kind": "text", "text": calls[1].stdin.decode()}),
            event("call", "model", {"kind": "function_call", "id": "read", "name": "Read", "args": {"path": "fact.md"}}),
            {**identity, "id": "dispatch", "ts": 1, "partial": False, "role": "system", "author": "system",
             "actions": {"toolDispatch": {"protocol": "t1_after_preflight_v1", "operationId": "op", "providerToolCallId": "read",
                         "toolName": "Read", "canonicalArgsHash": "a" * 64, "recoveryMode": "never_auto_retry"}}},
            event("read", "tool", {"kind": "function_response", "id": "read", "name": "Read", "result": "fact"}),
            event("answer", "model", {"kind": "text", "text": (output or b'{"winner":"B"}').decode().strip()}),
            invocation["terminalEvent"],
        ]}
        if change:
            change(data)
        return process.ProcessResult(process.ProcessStatus.EXIT, 0, json.dumps(data).encode(), b"")

    monkeypatch.setattr(transport_module, "run_command", run, raising=False)
    native = transport_module.NativeMakaTransport(launcher=launcher, package_root=package,
        capture_root=tmp_path / "captures", cwd=tmp_path, node="node", timeout=0.2)
    return native, calls


def test_native_read_records_bind_to_the_owned_session_and_unknown_model(tmp_path, monkeypatch):
    native, calls = _native(tmp_path, monkeypatch)
    ballot = evaluation.MakaJudgeAdapter(native).judge(_request())
    assert ballot["winner"] == "B"
    assert ballot["observed_model"] is None
    assert ballot["tool_calls"] == 1
    assert ballot["tool_records"][0]["content"]["name"] == "Read"
    assert ballot["tool_dispatches"][0]["actions"]["toolDispatch"]["toolName"] == "Read"
    assert ballot["session_id"] in calls[1].argv
    assert calls[1].trusted_native is True
    assert calls[1].argv[1:3] == ("run", "-")
    assert calls[1].argv.count("--yolo") == 1
    attempt = next((tmp_path / "captures").iterdir())
    assert json.loads((attempt / "journal.json").read_text())["permission_mode"] == "bypass"
    assert "--thinking" in calls[1].argv and "xhigh" in calls[1].argv
    assert len(calls) == 3


@pytest.mark.parametrize("change", [
    lambda d: d["invocation"].update(sessionId="someone-else"),
    lambda d: d["records"][0].update(runId="wrong"),
    lambda d: d["invocation"]["opening"]["route"].update(modelId="wrong"),
    lambda d: d["invocation"]["opening"]["configuration"].update(permissionMode="ask"),
    lambda d: d.update(permission_mode="ask"),
    lambda d: d["invocation"]["terminalEvent"].update(status="failed"),
    lambda d: d["records"].pop(0),
    lambda d: d["records"].pop(3),
    lambda d: d["records"].pop(2),
])
def test_wrong_or_incomplete_journal_earns_no_ballot_and_keeps_capture(tmp_path, monkeypatch, change):
    native, calls = _native(tmp_path, monkeypatch, change=change)
    with pytest.raises(evaluation.JudgeTransportError):
        evaluation.MakaJudgeAdapter(native).judge(_request())
    assert len(calls) == 4  # failed validation also checks the owned turn is stopped
    attempt = next((tmp_path / "captures").iterdir())
    assert (attempt / "stdout.bin").exists()
    assert (attempt / "journal.json").exists()


@pytest.mark.parametrize("mode", [None, "ask", "explore"])
def test_untrusted_preparation_mode_cannot_launch_a_model(tmp_path, monkeypatch, mode):
    native, calls = _native(tmp_path, monkeypatch)
    runner = transport_module.run_command

    def prepare(command, **kwargs):
        result = runner(command, **kwargs)
        if len(calls) == 1:
            owned = json.loads(result.stdout)
            owned["permission_mode"] = mode
            return process.ProcessResult(process.ProcessStatus.EXIT, 0, json.dumps(owned).encode(), b"")
        return result

    monkeypatch.setattr(transport_module, "run_command", prepare)
    with pytest.raises(evaluation.JudgeTransportError):
        native.evaluate("Read permitted evidence")
    assert len(calls) == 1


def test_model_written_claims_cannot_supply_execution_evidence(tmp_path, monkeypatch):
    native, calls = _native(tmp_path, monkeypatch,
        output=b'{"winner":"B","observed_model":"gpt-6.1-sol","tool_calls":0}')
    with pytest.raises(evaluation.JudgeTransportError):
        evaluation.MakaJudgeAdapter(native).judge(_request())
    assert len(calls) == 3


def test_timeout_retains_output_and_cannot_automatically_retry(tmp_path, monkeypatch):
    native, calls = _native(tmp_path, monkeypatch, failed=True)
    with pytest.raises(evaluation.JudgeTransportError):
        evaluation.MakaJudgeAdapter(native).judge(_request())
    assert len(calls) == 4  # exact native cleanup and journal capture follow failure
    assert calls[1].timeout == 30.2
    attempt = next((tmp_path / "captures").iterdir())
    assert (attempt / "stdout.bin").read_bytes() == b"partial"
    assert json.loads((attempt / "attempt.json").read_text())["status"] == "failed"


def test_cleanup_cannot_confirm_a_different_native_permission_mode(tmp_path, monkeypatch):
    native, calls = _native(tmp_path, monkeypatch, failed=True)
    runner = transport_module.run_command

    def cleanup(command, **kwargs):
        result = runner(command, **kwargs)
        if len(calls) > 2 and json.loads(command.stdin).get("operation") == "cleanup":
            evidence = json.loads(result.stdout)
            evidence["permission_mode"] = "ask"
            return process.ProcessResult(process.ProcessStatus.EXIT, 0, json.dumps(evidence).encode(), b"")
        return result

    monkeypatch.setattr(transport_module, "run_command", cleanup)
    with pytest.raises(evaluation.JudgeTransportError):
        native.evaluate("Read permitted evidence")
    attempt = next((tmp_path / "captures").iterdir())
    assert json.loads((attempt / "attempt.json").read_text())["cleanup_complete"] is False


@pytest.mark.parametrize("status,returncode,exited", [
    (process.ProcessStatus.EXIT, 1, True),
    (process.ProcessStatus.TIMEOUT, None, False),
])
def test_cleanup_receives_actual_process_exit_not_an_inferred_unstarted_turn(
    tmp_path, monkeypatch, status, returncode, exited,
):
    native, calls = _native(tmp_path, monkeypatch)
    runner = transport_module.run_command

    def startup_failure(command, **kwargs):
        result = runner(command, **kwargs)
        if len(calls) == 2:
            return process.ProcessResult(status, returncode, b"", b"synthetic startup refusal")
        return result

    monkeypatch.setattr(transport_module, "run_command", startup_failure)
    with pytest.raises(evaluation.JudgeTransportError):
        native.evaluate("Read a synthetic fact.")
    cleanup = json.loads(calls[2].stdin)
    assert cleanup["operation"] == "cleanup"
    assert cleanup["process_exited"] is exited
    assert cleanup["process_returncode"] == returncode
    assert len(calls) == 4


@pytest.mark.parametrize("name,args", [
    ("Write", {"path": "shared.md"}),
    ("mcp__slack__send_message", {"text": "unauthorized"}),
    ("mcp__jira__issues", {"action": "update"}),
])
def test_known_write_attempts_never_earn_a_ballot(tmp_path, monkeypatch, name, args):
    def write(data):
        data["records"][1]["content"].update(name=name, args=args)
        data["records"][3]["content"].update(name=name)
        data["records"][2]["actions"]["toolDispatch"].update(toolName=name)
    native, _ = _native(tmp_path, monkeypatch, change=write)
    with pytest.raises(evaluation.JudgeTransportError):
        evaluation.MakaJudgeAdapter(native).judge(_request())


def test_installed_skill_calls_are_accepted_and_the_native_read_result_is_retained(tmp_path, monkeypatch):
    def skill(data):
        data["records"][1]["content"].update(name="Skill")
        data["records"][3]["content"].update(name="Skill", result="Loaded relevant skill")
        data["records"][2]["actions"]["toolDispatch"].update(toolName="Skill")
    native, _ = _native(tmp_path, monkeypatch, change=skill)
    result = evaluation.MakaJudgeAdapter(native).judge(_request())
    assert result["tool_results"][0]["content"]["result"] == "Loaded relevant skill"


def test_subscription_route_needs_an_explicit_transport_selection(tmp_path, monkeypatch):
    from dataclasses import replace
    native, calls = _native(tmp_path, monkeypatch)
    with pytest.raises(evaluation.JudgeTransportError):
        evaluation.MakaJudgeAdapter(native).judge(replace(_request(), auth_route="chatgpt-subscription"))
    assert calls == []


def test_normal_process_keeps_installed_read_environment_and_supervises_native_cli(tmp_path, monkeypatch):
    native, calls = _native(tmp_path, monkeypatch)
    native.timeout = 5
    fact = tmp_path / "installed-read.txt"
    fact.write_text("available")
    monkeypatch.setenv("NATIVE_PLUGIN_READ_PATH", str(fact))
    native.launcher.write_text(
        '#!/bin/sh\n'
        'test "$1" = run || exit 91\n'
        'test "$2" = - || exit 92\n'
        'test -r "$NATIVE_PLUGIN_READ_PATH" || exit 93\n'
        '/bin/cat >/dev/null\n'
        'printf \'%s\\n\' \'{"winner":"B"}\'\n'
    )
    fixture_runner = transport_module.run_command
    def run(command, **kwargs):
        fixture_result = fixture_runner(command, **kwargs)
        return process.run_command(command, **kwargs) if command.argv[1] == "run" else fixture_result
    monkeypatch.setattr(transport_module, "run_command", run)
    ballot = evaluation.MakaJudgeAdapter(native).judge(_request())
    assert ballot["winner"] == "B"
    assert len(calls) == 3


def test_generic_native_analysis_retains_full_answer_and_read_evidence(tmp_path, monkeypatch):
    native, calls = _native(tmp_path, monkeypatch, output=b"Finding: the cited fact is supported.")
    result = native.evaluate("Read the permitted artifact and report concrete findings.")
    assert result["answer"] == "Finding: the cited fact is supported."
    assert result["tool_results"][0]["content"]["result"] == "fact"
    assert result["observed_model"] is None
    assert calls[1].stdin.endswith(b"Read the permitted artifact and report concrete findings.")
    assert b"Do not modify shared repositories or services" in calls[1].stdin
    assert len(calls) == 3


def test_blank_generic_analysis_is_rejected_and_retained(tmp_path, monkeypatch):
    native, _ = _native(tmp_path, monkeypatch, output=b" \n\t")
    with pytest.raises(evaluation.JudgeTransportError):
        native.evaluate("Read and report findings")
    attempt = next((tmp_path / "captures").iterdir())
    assert (attempt / "stdout.bin").read_bytes() == b" \n\t"
    assert json.loads((attempt / "attempt.json").read_text())["status"] == "failed"


def test_inner_native_timeout_leaves_cancellation_grace_and_forced_exit_gets_owned_cleanup(tmp_path, monkeypatch):
    native, calls = _native(tmp_path, monkeypatch, failed=True)
    with pytest.raises(evaluation.JudgeTransportError):
        native.evaluate("Read and report findings")
    inner = float(calls[1].argv[calls[1].argv.index("--timeout") + 1])
    assert calls[1].timeout > inner
    cleanup = [json.loads(call.stdin) for call in calls if call.argv[1] != "run"
               and json.loads(call.stdin).get("operation") == "cleanup"]
    assert len(cleanup) == 1
    assert cleanup[0]["session_id"] == calls[1].argv[calls[1].argv.index("--resume") + 1]
    assert cleanup[0]["host"] == {"pid": 123, "hostEpoch": "epoch"}


def test_prompt_over_cleanup_read_bound_is_rejected_before_session_allocation(tmp_path, monkeypatch):
    native, calls = _native(tmp_path, monkeypatch)
    with pytest.raises(evaluation.JudgeTransportError):
        native.evaluate("x" * (4 * 1024 * 1024))
    assert calls == []
