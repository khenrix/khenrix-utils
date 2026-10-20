"""Actual service mutation names remain denied while read controls stay usable."""
import json
import importlib

import pytest

from test_fanout_native_profiles import fanout
evaluation = importlib.import_module(f"{fanout.__name__}.evaluation")


MUTATORS = (
    "conversations_add_message", "add_issue_comment", "add_data_table_column",
    "add_data_table_rows", "archive_workflow", "call_agent", "create_agent",
    "create_data_table", "create_folder", "create_workflow_from_code", "delete_agent",
    "delete_data_table_column", "execute_workflow", "move_workflows_to_folder",
    "mutate_agent", "publish_agent", "publish_workflow", "rename_data_table",
    "rename_data_table_column", "restore_workflow_version", "revert_agent", "test_workflow",
    "unpublish_agent", "unpublish_workflow", "update_agent_integration", "update_folder",
    "update_workflow",
)


@pytest.mark.parametrize("name", MUTATORS)
def test_actual_service_mutations_are_rejected_by_native_maka(name):
    assert evaluation.native_tool_is_write(name)
    assert evaluation.native_tool_is_write("mcp__company_tools__" + name)


@pytest.mark.parametrize("name", ("attachment_get_data", "prepare_workflow_pin_data", "execute_query"))
def test_native_read_controls_are_not_classified_as_writes(name):
    assert not evaluation.native_tool_is_write(name)
    assert not evaluation.native_tool_is_write("mcp__company_tools__" + name)


def test_known_mutators_are_denied_on_configured_codex_aliases_without_disabling_reads(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.mkdir()
    (config / "config.toml").write_text('[mcp_servers.company_tools]\ncommand="mixed-mcp"\n')
    monkeypatch.setenv("CODEX_HOME", str(config))
    profile = fanout.ProviderRegistry.default().select("codex", "read-only", "standard")
    command = profile.adapter.build_command(fanout.ProviderRequest("codex", "read", cwd=tmp_path, profile=profile))
    policy = next(arg for arg in command.argv if arg.startswith('mcp_servers.company_tools.disabled_tools='))
    denied = json.loads(policy.split("=", 1)[1])
    assert set(MUTATORS) <= set(denied)
    assert not {"attachment_get_data", "prepare_workflow_pin_data", "execute_query"} & set(denied)


def test_claude_uses_actual_configured_service_mutation_names(tmp_path):
    profile = fanout.ProviderRegistry.default().select("claude", "read-only", "standard")
    command = profile.adapter.build_command(fanout.ProviderRequest("claude", "read", cwd=tmp_path, profile=profile))
    denied = command.argv[command.argv.index("--disallowedTools") + 1].split(",")
    assert "mcp__slack__conversations_add_message" in denied
    assert "mcp__github__add_issue_comment" in denied
    assert "mcp__n8n__test_workflow" in denied
    assert "mcp__n8n__prepare_workflow_pin_data" not in denied
