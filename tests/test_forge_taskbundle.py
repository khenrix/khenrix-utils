"""§20: what the seat was given, as a manifest that can be re-derived from the seat."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

import os  # noqa: E402
import pytest  # noqa: E402
from forge import storage, taskbundle  # noqa: E402


def _tree(root: Path) -> None:
    """A bundle shaped like a real skill closure: a dot directory, an executable script,
    a 0600 file and a symlink — every shape git's clone template was measured to lose."""
    (root / ".claude" / "skills").mkdir(parents=True)
    (root / ".claude" / "skills" / "S.md").write_text("skill body\n")
    (root / "SKILL.md").write_text("entry\n")
    (root / "scripts").mkdir()
    tool = root / "scripts" / "tool.sh"
    tool.write_text("#!/bin/sh\necho hi\n")
    tool.chmod(0o755)
    secret = root / "config.ini"
    secret.write_text("k=v\n")
    secret.chmod(0o600)
    os.symlink("SKILL.md", root / "alias.md")


def test_the_manifest_carries_every_shape_the_clone_template_drops(tmp_path):
    _tree(tmp_path)
    b = taskbundle.scan(tmp_path, entrypoint="SKILL.md")
    by = {e.path: e for e in b.entries}
    assert set(by) == {".claude/skills/S.md", "SKILL.md", "scripts/tool.sh",
                       "config.ini", "alias.md"}
    assert by["scripts/tool.sh"].mode == 0o755
    assert by["config.ini"].mode == 0o600, \
        "0600 is the mode git's template copy was measured to rewrite to 0644"
    assert by["alias.md"].kind == "symlink"
    assert by["alias.md"].mode == 0 and by["alias.md"].size == 0, \
        "snapshot.Entry's rule: a link's mode and size are fabricated, not read"


def test_the_hash_separates_a_script_from_a_non_executable_copy(tmp_path):
    """§20's own stated failure — 'copying without modes hands it a script it cannot
    execute' — must not be invisible to the hash that proves identical materialization."""
    _tree(tmp_path)
    before = taskbundle.bundle_hash(taskbundle.scan(tmp_path, entrypoint="SKILL.md"))
    (tmp_path / "scripts" / "tool.sh").chmod(0o644)
    after = taskbundle.bundle_hash(taskbundle.scan(tmp_path, entrypoint="SKILL.md"))
    assert before != after


def test_the_hash_separates_two_entrypoints_over_identical_bytes(tmp_path):
    """§11 compares bundle hashes to decide 'identically prompted'. Two seats told to
    start in different places were not identically prompted."""
    _tree(tmp_path)
    a = taskbundle.bundle_hash(taskbundle.scan(tmp_path, entrypoint="SKILL.md"))
    b = taskbundle.bundle_hash(taskbundle.scan(tmp_path, entrypoint=".claude/skills/S.md"))
    assert a != b


def test_an_empty_bundle_is_refused_rather_than_hashed(tmp_path):
    """fleet.Seat.verified's argument, one module over: a check over an empty manifest is
    vacuous and still answers True."""
    with pytest.raises(taskbundle.TaskBundleError, match="no entries"):
        taskbundle.scan(tmp_path, entrypoint="SKILL.md")


def test_an_entrypoint_no_entry_names_is_refused(tmp_path):
    _tree(tmp_path)
    with pytest.raises(taskbundle.TaskBundleError, match="entrypoint"):
        taskbundle.scan(tmp_path, entrypoint="does/not/exist.md")


def test_a_git_directory_inside_a_bundle_is_refused(tmp_path):
    _tree(tmp_path)
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]\n")
    with pytest.raises(taskbundle.TaskBundleError, match="git"):
        taskbundle.scan(tmp_path, entrypoint="SKILL.md")


def test_an_escaping_symlink_is_refused_not_carried(tmp_path):
    """The escaping-link lesson, carried forward: a link whose target leaves the bundle
    describes content the bundle does not claim to hold."""
    root = tmp_path / "b"
    root.mkdir()
    (root / "SKILL.md").write_text("entry\n")
    os.symlink("../outside.txt", root / "esc.md")
    with pytest.raises(taskbundle.TaskBundleError, match="escapes"):
        taskbundle.scan(root, entrypoint="SKILL.md")


def test_a_special_file_is_refused_rather_than_given_a_payload(tmp_path):
    root = tmp_path / "b"
    root.mkdir()
    (root / "SKILL.md").write_text("entry\n")
    os.mkfifo(root / "pipe")
    with pytest.raises(taskbundle.TaskBundleError, match="special"):
        taskbundle.scan(root, entrypoint="SKILL.md")


def test_a_breached_cap_is_a_refusal_and_names_the_cap_it_applied(tmp_path):
    _tree(tmp_path)
    tiny = storage.Quota(max_files=2, max_file_bytes=1 << 20, max_total_bytes=1 << 20)
    with pytest.raises(taskbundle.TaskBundleError, match="files: 5 > 2"):
        taskbundle.scan(tmp_path, entrypoint="SKILL.md", quota=tiny)


def test_the_caps_actually_applied_are_recorded_not_assumed(tmp_path):
    _tree(tmp_path)
    b = taskbundle.scan(tmp_path, entrypoint="SKILL.md")
    q = storage.Quota.for_task_bundle()
    assert (b.max_files, b.max_file_bytes, b.max_total_bytes) == \
        (q.max_files, q.max_file_bytes, q.max_total_bytes), \
        "a bundle that fit and a bundle nobody measured must be different records"


def test_the_bundle_round_trips_through_the_run_directory(tmp_path):
    _tree(tmp_path / "src")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    b = taskbundle.scan(tmp_path / "src", entrypoint="SKILL.md")
    taskbundle.write_task_bundle(run_dir, b)
    assert taskbundle.read_task_bundle(run_dir) == b
    assert taskbundle.bundle_hash(taskbundle.read_task_bundle(run_dir)) == \
        taskbundle.bundle_hash(b)


def test_a_field_the_decoder_does_not_know_is_refused(tmp_path):
    _tree(tmp_path / "src")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    taskbundle.write_task_bundle(run_dir, taskbundle.scan(tmp_path / "src",
                                                          entrypoint="SKILL.md"))
    p = storage.task_bundle_path(run_dir)
    p.write_text(p.read_text().replace('"version": 1', '"version": 1, "extra": 2'))
    with pytest.raises(taskbundle.TaskBundleError, match="does not know"):
        taskbundle.read_task_bundle(run_dir)


def test_a_missing_bundle_raises_rather_than_reading_as_empty(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    with pytest.raises(taskbundle.TaskBundleError, match="does not exist"):
        taskbundle.read_task_bundle(run_dir)
