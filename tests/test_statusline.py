"""The shared renderer's visible, single-row contract."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import unicodedata


RENDERER = Path(__file__).resolve().parents[1] / "statusline" / "khenrix-statusline"


def render(cli: str, payload: dict | str, *, width: int = 120, color: bool = False,
           env_overrides: dict[str, str] | None = None) -> str:
    env = {**os.environ, "COLUMNS": str(width)}
    if color:
        env.pop("NO_COLOR", None)
    else:
        env["NO_COLOR"] = "1"
    env.update(env_overrides or {})
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    result = subprocess.run(
        [sys.executable, str(RENDERER), cli], input=raw, text=True,
        capture_output=True, check=True, env=env,
    )
    assert result.stderr == ""
    assert result.stdout.endswith("\n")
    assert result.stdout.count("\n") == 1
    return result.stdout.rstrip("\n")


def test_claude_uses_input_only_context_and_puts_estimated_cost_last(tmp_path):
    line = render("claude", {
        "model": {"display_name": "Claude Opus 5.5"},
        "workspace": {"current_dir": str(tmp_path / "project"), "git_worktree": "feature-status"},
        "fast_mode": True,
        "effort": {"level": "xhigh"},
        "context_window": {"used_percentage": None, "remaining_percentage": None,
                           "total_input_tokens": 20_000, "total_output_tokens": 5_000,
                           "context_window_size": 100_000},
        "rate_limits": {"five_hour": {"used_percentage": 23},
                        "seven_day": {"used_percentage": 91}},
        "cost": {"total_cost_usd": 1.23},
        "pr": {"number": 42},
    }, width=180)
    assert "Opus 5.5" in line
    assert "xhigh" in line and "FAST" in line
    assert "ctx 80% left" in line
    assert "5h 77% left" in line and "7d 9% left" in line
    assert "feature-status" in line
    assert line.endswith("est $1.23")
    assert line.index("ctx 80% left") < line.index("project") < line.index("5h 77% left")
    assert line.index("feature-status") < line.index("7d 9% left")


def test_agy_uses_supplied_vcs_and_labels_most_constrained_quota(tmp_path):
    line = render("agy", {
        "model": {"display_name": "Gemini 3.8 Flash (High)"},
        "cwd": str(tmp_path),
        "terminal_width": 180,
        "execution_mode": "planning",
        "context_window": {"remaining_percentage": 72},
        "quota": {"gemini-weekly": {"remaining_fraction": 0.12},
                  "other-daily": {"remaining_fraction": 0.60}},
        "vcs": {"type": "git", "branch": "feature/status", "dirty": True},
        "agent_state": "working",
        "tool_confirmation_pending": True,
        "task_count": 2,
        "sandbox": {"enabled": False},
    })
    assert "PLAN" in line
    assert "ctx 72% left" in line
    assert "gemini-weekly 12% left" in line
    assert line.index("gemini-weekly") < line.index("other-daily")
    assert "feature/status*" in line
    assert "CONFIRM" in line and "tasks 2" in line
    assert "sandbox off" in line


def test_narrow_line_drops_whole_segments_and_respects_claude_columns(tmp_path):
    line = render("claude", {
        "model": {"display_name": "Claude Opus 5.5"},
        "workspace": {"current_dir": str(tmp_path / "very-long-project-name")},
        "context_window": {"remaining_percentage": 80},
        "rate_limits": {"five_hour": {"used_percentage": 8}},
        "cost": {"total_cost_usd": 2.34},
    }, width=40)
    assert len(line) <= 40
    assert "Opus 5.5" in line and "ctx 80% left" in line
    assert "..." not in line
    assert "est $" not in line


def test_untrusted_strings_cannot_add_rows_or_terminal_controls(tmp_path):
    line = render("agy", {
        "model": {"display_name": "Gemini \x1b[31m\nSECRET"},
        "cwd": str(tmp_path),
        "terminal_width": 80,
        "vcs": {"branch": "main\r\nFAKE", "dirty": False},
    })
    assert "\x1b" not in line and "\r" not in line
    assert "\n" not in line
    assert len(line) <= 80


def test_malformed_payload_has_neutral_one_row_fallback():
    assert render("claude", "{bad json", width=40) == "status unavailable"


def test_claude_unborn_git_branch_and_dirty_file(tmp_path):
    subprocess.run(["git", "init", "--quiet", "--initial-branch=main", str(tmp_path)], check=True)
    (tmp_path / "new-file.txt").write_text("work in progress\n")
    line = render("claude", {"model": "Claude Opus 5.5", "cwd": str(tmp_path)}, width=80)
    assert "git main*" in line


def test_claude_supplied_worktree_branch_keeps_dirty_marker(tmp_path):
    subprocess.run(["git", "init", "--quiet", "--initial-branch=main", str(tmp_path)], check=True)
    (tmp_path / "new-file.txt").write_text("work in progress\n")
    line = render("claude", {"model": "Claude Opus 5.5", "cwd": str(tmp_path),
                             "worktree": {"branch": "feature"}}, width=80)
    assert "git feature*" in line


def test_agy_context_fallback_counts_output_tokens():
    line = render("agy", {"model": "Gemini 3.8 Flash",
                           "context_window": {"remaining_percentage": None,
                                              "used_percentage": None,
                                              "total_input_tokens": 88_244,
                                              "total_output_tokens": 61_074,
                                              "context_window_size": 1_048_576}}, width=80)
    assert "ctx 86% left" in line


def test_unicode_names_fit_display_cells_and_low_context_is_colored():
    payload = {"model": "Gemini 界界界界界界界界界界",
               "context_window": {"remaining_percentage": 9}}
    plain = render("agy", payload, width=40)
    cells = sum(2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
                for char in plain)
    assert cells <= 40
    assert "ctx 9% left" in plain
    assert "\x1b[31m" in render("agy", payload, width=40, color=True)


def test_agy_quota_ties_sort_by_exact_bucket_id_without_running_git(tmp_path):
    marker = tmp_path / "git-called"
    fake_git = tmp_path / "git"
    fake_git.write_text(f'#!/bin/sh\ntouch "{marker}"\n')
    fake_git.chmod(0o755)
    line = render("agy", {
        "model": "Gemini 3.8 Flash (High)", "cwd": str(tmp_path),
        "terminal_width": 160,
        "quota": {"z-bucket": {"remaining_fraction": 0.12},
                  "a-bucket": {"remaining_fraction": 0.12}},
        "vcs": {"type": "git", "branch": "feature", "dirty": True},
    }, env_overrides={"PATH": str(tmp_path)})
    assert line.index("a-bucket") < line.index("z-bucket")
    assert "git feature*" in line
    assert not marker.exists()
