"""Tuneup lock timing follows the admitted review policy, not the old council."""
import importlib.util
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "_tuneup_fanout_contracts", ROOT / "shared/skills/skill-tuneup/scripts/tuneup.py",
)
tuneup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tuneup)


def test_lock_window_tracks_actual_two_round_deep_review_and_policy_changes(tmp_path):
    runtime = ROOT / "shared/lib/fanout"
    planner = tmp_path / "plan.py"
    shutil.copy2(ROOT / "shared/skills/llm-fanout-plan/scripts/plan.py", planner)
    assert tuneup.review_window_minutes(runtime, planner) == 60
    planner.write_text(planner.read_text().replace('"rounds": 2,', '"rounds": 5,') + "\n# policy changed\n")
    assert tuneup.review_window_minutes(runtime, planner) == 150
    assert tuneup.LOCK_STALE_MIN < tuneup.review_window_minutes(runtime, planner)
