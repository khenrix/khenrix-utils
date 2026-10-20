"""All configured final identities are required for action-ready synthesis."""
import pytest
from test_fanout_execute import fanout, _task, _plan, _runtime


def _three_seat_runtime(tmp_path):
    task = _task("proposal")
    task["provider_policy"].update(executor_ids=["claude", "codex", "agy"], minimum_success=3)
    runtime = _runtime(tmp_path, _plan(task))
    runtime.coordinator.identical_answers = True
    runtime.service.start(owner=runtime.owner)
    runtime.service.resume(owner=runtime.owner)
    return runtime


@pytest.mark.parametrize("source_ids", [("claude", "codex"), ("claude", "codex", "codex")])
def test_action_ready_synthesis_requires_all_three_final_identities(tmp_path, source_ids):
    runtime = _three_seat_runtime(tmp_path)
    with pytest.raises(fanout.ExecutionPendingError, match="final seats"):
        runtime.service.verify_read_only_synthesis(
            "proposal", source_seat_ids=source_ids, synthesizer_id="owner",
            answer=b"Owner reconciled agreement and remaining uncertainty.\n", owner=runtime.owner,
        )


def test_equal_answer_bytes_from_all_three_seats_form_fresh_action_ready_synthesis(tmp_path):
    runtime = _three_seat_runtime(tmp_path)
    receipt = runtime.service.verify_read_only_synthesis(
        "proposal", source_seat_ids=("claude", "codex", "agy"), synthesizer_id="owner",
        answer=b"Owner reconciled agreement and remaining uncertainty.\n", owner=runtime.owner,
    )
    result = runtime.service.submit("proposal", receipt, owner=runtime.owner)
    assert result.artifact == receipt.answer_ref
    assert {seat for seat, _ in receipt.source_answers} == {"claude", "codex", "agy"}


def test_action_ready_result_rejects_source_selection_and_library_subset_receipt(tmp_path):
    runtime = _three_seat_runtime(tmp_path)
    barrier = next(iter(runtime.coordinator.barriers.values()))
    source = barrier.valid_terminals[0].answer_ref
    with pytest.raises(fanout.ExecutionPendingError, match="synthesis"):
        runtime.service.submit("proposal", source, owner=runtime.owner)
    subset = fanout.verify_answer_synthesis(
        runtime.artifacts, run_id=runtime.inputs.run_id, task_id="proposal",
        plan_sha256=runtime.inputs.compiled_plan_sha256, plan_revision=1,
        sources={terminal.seat_id: terminal.answer_ref for terminal in barrier.valid_terminals[:2]},
        synthesizer_id="owner", answer=b"Only two considered.\n", checks=(), controller=runtime.controller,
    )
    with pytest.raises(fanout.ExecutionPendingError, match="source|seat|synthesis"):
        runtime.service.submit("proposal", subset, owner=runtime.owner)
    assert runtime.scheduler.task_phase("proposal") == "reconciliation-pending"
