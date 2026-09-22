from __future__ import annotations

import json
import importlib.util
import fcntl
import os
import stat
import sys
import threading
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "components" / "skills"))
import skillctl  # noqa: E402


CAPABILITIES = """
[skill_delivery]
skills = ["khenrix-quality", "khenrix-writing"]
state_dir = "${HOME}/.local/state/khenrix-utils/skills"
instruction_source = "house-style.md"

[skill_delivery.targets]
claude = "${HOME}/.claude/skills"
codex_maka = "${HOME}/.agents/skills"
agy = "${HOME}/.gemini/config/skills"

[skill_delivery.instruction_targets]
claude = "${HOME}/.claude/CLAUDE.md"
codex = "${HOME}/.codex/AGENTS.md"
agy = "${HOME}/.gemini/GEMINI.md"
maka = "${HOME}/.maka/AGENTS.md"

[instructions.overlays]
claude = "overlays/claude.md"
"""


def fixture(tmp_path: Path) -> tuple[Path, Path, skillctl.Configuration]:
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    repo.mkdir()
    home.mkdir()
    (repo / "capabilities.toml").write_text(CAPABILITIES)
    (repo / "house-style.md").write_text(
        "prefix ignored\n"
        f"{skillctl.MANAGED_BEGIN}\n# managed v1\n{skillctl.MANAGED_END}\n"
        "suffix ignored\n"
    )
    (repo / "overlays").mkdir()
    (repo / "overlays" / "claude.md").write_text("## Claude-only\n\nKeep this overlay.\n")
    for name in ("khenrix-quality", "khenrix-writing"):
        skill = repo / "shared" / "skills" / name
        (skill / "references").mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"---\nname: {name}\ndescription: test\n---\n")
        (skill / "references" / "mode.md").write_text(f"{name} v1\n")
    config = skillctl.load_configuration(repo, home, None)
    return repo, home, config


def tree_snapshot(root: Path) -> dict[str, tuple[str, int, bytes | None]]:
    snapshot = {".": ("directory", stat.S_IMODE(root.stat().st_mode), None)}
    for item in sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix()):
        relative = item.relative_to(root).as_posix()
        if item.is_dir():
            snapshot[relative] = ("directory", stat.S_IMODE(item.stat().st_mode), None)
        else:
            snapshot[relative] = ("file", stat.S_IMODE(item.stat().st_mode), item.read_bytes())
    return snapshot


def test_plan_manages_only_two_exact_skills_and_four_instruction_blocks(tmp_path: Path) -> None:
    _, home, config = fixture(tmp_path)
    unrelated = home / ".agents" / "skills" / "leave-me" / "SKILL.md"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("mine\n")

    plan_id, entries = skillctl.plan(config)

    assert plan_id.startswith("sha256:")
    assert len(entries) == 10
    assert {entry.skill for entry in entries if entry.kind == "skill"} == {
        "khenrix-quality",
        "khenrix-writing",
    }
    assert {entry.target_name for entry in entries if entry.kind == "instructions"} == {
        "claude",
        "codex",
        "agy",
        "maka",
    }
    assert all(entry.action == "ADD" for entry in entries)
    instruction_hashes = {
        entry.target_name: entry.desired_hash
        for entry in entries
        if entry.kind == "instructions"
    }
    assert instruction_hashes["claude"] != instruction_hashes["codex"]
    assert instruction_hashes["codex"] == instruction_hashes["agy"] == instruction_hashes["maka"]
    assert unrelated.read_text() == "mine\n"


def test_instruction_blocks_match_the_broader_reconcile_controller(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location(
        "khenrix_reconcile_for_delivery_test", ROOT / "scripts" / "lib" / "reconcile.py"
    )
    assert spec and spec.loader
    reconcile = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reconcile)

    home = tmp_path / "home"
    home.mkdir()
    config = skillctl.load_configuration(ROOT, home, home / "state")
    caps = reconcile.load_caps()

    for target_name in ("claude", "codex", "agy"):
        assert skillctl.desired_instruction_block(config, target_name) == reconcile.managed_block(
            caps, target_name
        )
    assert skillctl.desired_instruction_block(config, "maka") == reconcile.managed_block(caps)


def test_apply_preserves_siblings_outside_text_and_existing_parent_modes(tmp_path: Path) -> None:
    repo, home, config = fixture(tmp_path)
    for name in ("khenrix-quality", "khenrix-writing"):
        source = repo / "shared" / "skills" / name
        source.chmod(0o700)
        (source / "references").chmod(0o700)
        (source / "SKILL.md").chmod(0o600)
        (source / "references/mode.md").chmod(0o600)
    (repo / "shared/skills/khenrix-quality/references/mode.md").chmod(0o700)
    skill_root = home / ".claude" / "skills"
    skill_root.mkdir(parents=True)
    os.chmod(skill_root, 0o755)
    unrelated = skill_root / "personal" / "SKILL.md"
    unrelated.parent.mkdir()
    unrelated.write_text("personal\n")
    claude = home / ".claude" / "CLAUDE.md"
    claude.write_text("before\n\nafter\n")
    os.chmod(claude, 0o640)

    skillctl.apply(config, expect=None, as_json=False)

    assert stat.S_IMODE(skill_root.stat().st_mode) == 0o755
    assert stat.S_IMODE(claude.stat().st_mode) == 0o640
    assert unrelated.read_text() == "personal\n"
    assert claude.read_text().startswith("before\n\nafter\n")
    assert "# managed v1" in claude.read_text()
    assert "## Claude-only" in claude.read_text()
    for target_name in ("codex", "agy", "maka"):
        assert "## Claude-only" not in config.instruction_targets[target_name].read_text()
    for root in config.targets.values():
        assert (root / "khenrix-quality" / "SKILL.md").is_file()
        assert (root / "khenrix-writing" / "SKILL.md").is_file()
        for name in ("khenrix-quality", "khenrix-writing"):
            installed = root / name
            assert stat.S_IMODE(installed.stat().st_mode) == 0o755
            assert stat.S_IMODE((installed / "references").stat().st_mode) == 0o755
            assert stat.S_IMODE((installed / "SKILL.md").stat().st_mode) == 0o644
        assert stat.S_IMODE(
            (root / "khenrix-quality/references/mode.md").stat().st_mode
        ) == 0o755
        assert stat.S_IMODE(
            (root / "khenrix-writing/references/mode.md").stat().st_mode
        ) == 0o644
    _, entries = skillctl.plan(config)
    assert all(entry.action == "MATCH" for entry in entries)

    receipt = json.loads((config.state_dir / skillctl.RECEIPT_NAME).read_text())
    assert receipt["schema_version"] == 1
    assert set(receipt["skills"]) == {"khenrix-quality", "khenrix-writing"}
    assert set(receipt["instructions"]) == {"claude", "codex", "agy", "maka"}
    assert receipt["maka_instructions"] == receipt["instructions"]["maka"]
    skillctl.doctor(config, as_json=False)


def test_apply_waits_for_shared_operation_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, home, config = fixture(tmp_path)
    skillctl.apply(config, expect=None, as_json=False)
    (repo / "shared/skills/khenrix-quality/SKILL.md").write_text("quality v2\n")
    attempted_lock = threading.Event()
    entered_apply = threading.Event()
    failures: list[BaseException] = []
    original = skillctl._apply_locked
    real_flock = skillctl.fcntl.flock
    worker: threading.Thread

    def observed_apply(*args, **kwargs):
        entered_apply.set()
        return original(*args, **kwargs)

    def run_apply() -> None:
        try:
            skillctl.apply(config, expect=None, as_json=False)
        except BaseException as error:
            failures.append(error)

    def observed_flock(descriptor: int, operation: int):
        if threading.current_thread() is worker and operation & fcntl.LOCK_EX:
            attempted_lock.set()
        return real_flock(descriptor, operation)

    monkeypatch.setattr(skillctl, "_apply_locked", observed_apply)
    monkeypatch.setattr(skillctl.fcntl, "flock", observed_flock)
    lock_path = config.state_dir / skillctl.OPERATION_LOCK_NAME
    with lock_path.open("r+b", buffering=0) as handle:
        real_flock(handle.fileno(), fcntl.LOCK_EX)
        worker = threading.Thread(target=run_apply)
        worker.start()
        assert attempted_lock.wait(timeout=2)
        assert not entered_apply.is_set()
        assert worker.is_alive()
        real_flock(handle.fileno(), fcntl.LOCK_UN)
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert failures == []
    assert entered_apply.is_set()
    assert (home / ".agents/skills/khenrix-quality/SKILL.md").read_text() == "quality v2\n"


def test_apply_repairs_noncanonical_existing_skill_modes(tmp_path: Path) -> None:
    _, home, config = fixture(tmp_path)
    skillctl.apply(config, expect=None, as_json=False)
    installed = home / ".agents/skills/khenrix-quality"
    installed.chmod(0o700)
    (installed / "references").chmod(0o700)
    (installed / "SKILL.md").chmod(0o600)

    _, entries = skillctl.plan(config)
    affected = [
        entry
        for entry in entries
        if entry.skill == "khenrix-quality" and entry.target_name == "codex_maka"
    ]
    assert [entry.action for entry in affected] == ["UPDATE"]

    skillctl.apply(config, expect=None, as_json=False)
    assert stat.S_IMODE(installed.stat().st_mode) == 0o755
    assert stat.S_IMODE((installed / "references").stat().st_mode) == 0o755
    assert stat.S_IMODE((installed / "SKILL.md").stat().st_mode) == 0o644


def test_doctor_rejects_receipt_missing_shared_contract_fields(tmp_path: Path) -> None:
    _, _, config = fixture(tmp_path)
    skillctl.apply(config, expect=None, as_json=False)
    receipt_path = config.state_dir / skillctl.RECEIPT_NAME
    receipt = json.loads(receipt_path.read_text())
    receipt["plan_id"] = ""
    receipt.pop("applied_at")
    receipt_path.write_text(json.dumps(receipt))

    with pytest.raises(skillctl.DeliveryError, match="doctor found"):
        skillctl.doctor(config, as_json=False)


@pytest.mark.parametrize(
    "invalid_receipt",
    [[], {"schema_version": 1}, {"schema_version": 1, "skills": []}],
)
def test_doctor_reports_structurally_invalid_receipt_without_crashing(
    tmp_path: Path, invalid_receipt: object
) -> None:
    _, _, config = fixture(tmp_path)
    skillctl.apply(config, expect=None, as_json=False)
    receipt_path = config.state_dir / skillctl.RECEIPT_NAME
    receipt_path.write_text(json.dumps(invalid_receipt))

    with pytest.raises(skillctl.DeliveryError, match="doctor found"):
        skillctl.doctor(config, as_json=False)


def test_doctor_rejects_non_mapping_nested_receipt_records(tmp_path: Path) -> None:
    _, _, config = fixture(tmp_path)
    skillctl.apply(config, expect=None, as_json=False)
    receipt_path = config.state_dir / skillctl.RECEIPT_NAME
    receipt = json.loads(receipt_path.read_text())
    receipt["skills"]["khenrix-quality"] = []
    receipt_path.write_text(json.dumps(receipt))

    with pytest.raises(skillctl.DeliveryError, match="doctor found"):
        skillctl.doctor(config, as_json=False)


def test_content_addressed_expectation_refuses_changed_plan(tmp_path: Path) -> None:
    repo, _, config = fixture(tmp_path)
    plan_id, _ = skillctl.plan(config)
    (repo / "shared" / "skills" / "khenrix-quality" / "SKILL.md").write_text("changed\n")

    with pytest.raises(skillctl.DeliveryError, match="plan changed"):
        skillctl.apply(config, expect=plan_id, as_json=False)


def test_receipt_write_failure_rolls_back_all_live_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, home, config = fixture(tmp_path)
    instruction_target = home / ".maka" / "AGENTS.md"
    instruction_target.parent.mkdir()
    instruction_target.write_bytes(b"user text\r\n")
    instruction_target.chmod(0o640)

    # Exercise UPDATE rollback beside the ADD entries created for every other target.
    skill_target = home / ".agents" / "skills" / "khenrix-quality"
    (skill_target / "references").mkdir(parents=True)
    (skill_target / "SKILL.md").write_bytes(b"local skill bytes\r\n")
    (skill_target / "references" / "local.md").write_bytes(b"local reference\x00bytes")
    skill_target.chmod(0o700)
    (skill_target / "references").chmod(0o710)
    (skill_target / "SKILL.md").chmod(0o600)
    (skill_target / "references" / "local.md").chmod(0o640)
    before_skill = tree_snapshot(skill_target)
    original_writer = skillctl.write_private_json

    def fail_receipt(path: Path, value: object, cfg: skillctl.Configuration) -> None:
        if path.name == skillctl.RECEIPT_NAME:
            raise OSError("simulated receipt failure")
        original_writer(path, value, cfg)

    monkeypatch.setattr(skillctl, "write_private_json", fail_receipt)
    with pytest.raises(OSError, match="simulated receipt failure"):
        skillctl.apply(config, expect=None, as_json=False)

    assert instruction_target.read_bytes() == b"user text\r\n"
    assert stat.S_IMODE(instruction_target.stat().st_mode) == 0o640
    assert tree_snapshot(skill_target) == before_skill
    for root in config.targets.values():
        for name in ("khenrix-quality", "khenrix-writing"):
            target = root / name
            if target == skill_target:
                continue
            assert not target.exists()


def test_update_backup_and_bounded_restore(tmp_path: Path) -> None:
    repo, home, config = fixture(tmp_path)
    maka = home / ".maka" / "AGENTS.md"
    maka.parent.mkdir()
    maka.write_text("user before\n")
    skillctl.apply(config, expect=None, as_json=False)
    old_tree = home / ".agents" / "skills" / "khenrix-quality"
    old_tree.chmod(0o700)
    (old_tree / "references").chmod(0o710)
    (old_tree / "SKILL.md").chmod(0o600)
    (old_tree / "references" / "mode.md").chmod(0o640)
    old_snapshot = tree_snapshot(old_tree)

    (repo / "shared" / "skills" / "khenrix-quality" / "SKILL.md").write_text("quality v2\n")
    (repo / "house-style.md").write_text(
        f"{skillctl.MANAGED_BEGIN}\n# managed v2\n{skillctl.MANAGED_END}\n"
    )
    skillctl.apply(config, expect=None, as_json=False)
    receipt = json.loads((config.state_dir / skillctl.RECEIPT_NAME).read_text())
    backup_id = receipt["backup_id"]
    assert backup_id
    maka.write_text(maka.read_text() + "user after\n")

    skillctl.restore(config, backup_id)

    assert tree_snapshot(old_tree) == old_snapshot
    restored = maka.read_text()
    assert "# managed v1" in restored
    assert "# managed v2" not in restored
    assert "user before" in restored and "user after" in restored
    assert not (config.state_dir / skillctl.RECEIPT_NAME).exists()


@pytest.mark.parametrize(
    "original",
    [b"", b"no trailing newline", b"first\r\nsecond\r\n"],
)
def test_restore_reproduces_unchanged_preexisting_instruction_bytes(
    tmp_path: Path, original: bytes
) -> None:
    _, home, config = fixture(tmp_path)
    target = home / ".maka" / "AGENTS.md"
    target.parent.mkdir()
    target.write_bytes(original)
    os.chmod(target, 0o640)

    skillctl.apply(config, expect=None, as_json=False)
    receipt = json.loads((config.state_dir / skillctl.RECEIPT_NAME).read_text())
    skillctl.restore(config, receipt["backup_id"])

    assert target.exists()
    assert target.read_bytes() == original
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_apply_preserves_crlf_bytes_outside_the_managed_block(tmp_path: Path) -> None:
    _, home, config = fixture(tmp_path)
    target = home / ".maka" / "AGENTS.md"
    target.parent.mkdir()
    target.write_bytes(b"first\r\nsecond\r\n")

    skillctl.apply(config, expect=None, as_json=False)

    assert target.read_bytes().startswith(b"first\r\nsecond\r\n")


def test_restore_refuses_skill_changed_after_apply(tmp_path: Path) -> None:
    repo, home, config = fixture(tmp_path)
    skillctl.apply(config, expect=None, as_json=False)
    (repo / "shared" / "skills" / "khenrix-quality" / "SKILL.md").write_text("v2\n")
    skillctl.apply(config, expect=None, as_json=False)
    receipt = json.loads((config.state_dir / skillctl.RECEIPT_NAME).read_text())
    target = home / ".agents" / "skills" / "khenrix-quality" / "SKILL.md"
    target.write_text("local edit\n")

    with pytest.raises(skillctl.DeliveryError, match="changed after apply"):
        skillctl.restore(config, receipt["backup_id"])


@pytest.mark.parametrize("updated_existing", [False, True])
def test_restore_refuses_skill_mode_change_after_add_or_update(
    tmp_path: Path, updated_existing: bool
) -> None:
    repo, home, config = fixture(tmp_path)
    skillctl.apply(config, expect=None, as_json=False)
    if updated_existing:
        (repo / "shared" / "skills" / "khenrix-quality" / "SKILL.md").write_bytes(b"v2\n")
        skillctl.apply(config, expect=None, as_json=False)
    receipt = json.loads((config.state_dir / skillctl.RECEIPT_NAME).read_text())
    target = home / ".agents" / "skills" / "khenrix-quality"
    before = (target / "SKILL.md").read_bytes()
    target.chmod(0o700)
    (target / "SKILL.md").chmod(0o600)

    with pytest.raises(skillctl.DeliveryError, match="changed after apply"):
        skillctl.restore(config, receipt["backup_id"])

    assert target.exists()
    assert (target / "SKILL.md").read_bytes() == before
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert stat.S_IMODE((target / "SKILL.md").stat().st_mode) == 0o600


def test_restore_rejects_manifest_target_outside_selective_ownership(tmp_path: Path) -> None:
    repo, home, config = fixture(tmp_path)
    skillctl.apply(config, expect=None, as_json=False)
    (repo / "shared" / "skills" / "khenrix-quality" / "SKILL.md").write_text("v2\n")
    skillctl.apply(config, expect=None, as_json=False)
    receipt = json.loads((config.state_dir / skillctl.RECEIPT_NAME).read_text())
    backup = config.state_dir / "backups" / receipt["backup_id"]
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    victim = home / "unrelated-project"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep\n")
    manifest["entries"][0]["target"] = str(victim)
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(skillctl.DeliveryError, match="outside selective delivery ownership"):
        skillctl.restore(config, receipt["backup_id"])
    assert (victim / "keep.txt").read_text() == "keep\n"


def test_symlinked_managed_path_is_refused_without_touching_target(tmp_path: Path) -> None:
    _, home, config = fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / ".agents").mkdir()
    (home / ".agents" / "skills").symlink_to(outside, target_is_directory=True)

    with pytest.raises(skillctl.DeliveryError, match="symlink"):
        skillctl.plan(config)
    assert list(outside.iterdir()) == []


def test_cli_keeps_symlinked_home_visible_for_rejection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, _, _ = fixture(tmp_path)
    actual_home = tmp_path / "actual-home"
    actual_home.mkdir()
    home_link = tmp_path / "home-link"
    home_link.symlink_to(actual_home, target_is_directory=True)

    result = skillctl.main(
        ["--repo-root", str(repo), "--home", str(home_link), "plan"]
    )

    assert result == 2
    assert "symlinked HOME" in capsys.readouterr().err
    assert list(actual_home.iterdir()) == []


@pytest.mark.parametrize(
    ("original", "replacement", "label"),
    [
        (
            'claude = "${HOME}/.claude/skills"',
            'claude = "${HOME}/safe/../../../outside-skills"',
            "skill root claude",
        ),
        (
            'codex = "${HOME}/.codex/AGENTS.md"',
            'codex = "${HOME}/safe/../../../outside-instructions"',
            "codex instruction target",
        ),
        (
            'state_dir = "${HOME}/.local/state/khenrix-utils/skills"',
            'state_dir = "${HOME}/safe/../../../outside-state"',
            "skill state directory",
        ),
    ],
)
def test_configuration_rejects_parent_traversal_without_resolving_managed_paths(
    tmp_path: Path, original: str, replacement: str, label: str
) -> None:
    repo, home, _ = fixture(tmp_path)
    manifest = repo / "capabilities.toml"
    manifest.write_text(manifest.read_text().replace(original, replacement))

    with pytest.raises(skillctl.DeliveryError, match=rf"{label} escapes HOME"):
        skillctl.load_configuration(repo, home, None)


def test_state_override_rejects_parent_traversal(tmp_path: Path) -> None:
    repo, home, _ = fixture(tmp_path)
    traversal = home / "safe" / ".." / ".." / "outside-state"

    with pytest.raises(skillctl.DeliveryError, match="skill state directory escapes HOME"):
        skillctl.load_configuration(repo, home, traversal)


def test_restore_rejects_parent_traversal_backup_id(tmp_path: Path) -> None:
    _, _, config = fixture(tmp_path)

    with pytest.raises(skillctl.DeliveryError, match="single directory name"):
        skillctl.select_backup(config, "../outside-backup")


def test_malformed_instruction_markers_fail_closed(tmp_path: Path) -> None:
    _, home, config = fixture(tmp_path)
    target = home / ".maka" / "AGENTS.md"
    target.parent.mkdir()
    target.write_text(f"text\n{skillctl.MANAGED_BEGIN}\n")

    with pytest.raises(skillctl.DeliveryError, match="unpaired"):
        skillctl.plan(config)
