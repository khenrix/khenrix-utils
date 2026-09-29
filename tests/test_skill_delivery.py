from __future__ import annotations

import json
import importlib.util
import fcntl
import os
import shutil
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


def bundle_fixture(
    tmp_path: Path, mapping: dict[str, dict[str, str]] | None = None
) -> tuple[Path, Path, skillctl.Configuration]:
    repo, home, _ = fixture(tmp_path)
    runtime = repo / "shared" / "lib" / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "__init__.py").write_bytes(b"VERSION = 1\n")
    tool = runtime / "run.sh"
    tool.write_bytes(b"#!/bin/sh\nexit 0\n")
    tool.chmod(0o700)
    mapping = mapping or {"khenrix-quality": {"lib/runtime": "shared/lib/runtime"}}
    tables = "".join(
        f"[skill_delivery.skill_bundles.{skill}]\n"
        + "".join(f'"{target}" = "{source}"\n' for target, source in bundles.items())
        for skill, bundles in mapping.items()
    )
    manifest = repo / "capabilities.toml"
    manifest.write_text(
        manifest.read_text().replace("[skill_delivery.targets]", tables + "\n[skill_delivery.targets]")
    )
    return repo, home, skillctl.load_configuration(repo, home, None)


def plant_unsafe_ignored_bundle_entry(repo: Path, kind: str) -> None:
    source = repo / "shared/lib/runtime"
    if kind == "symlink-cache":
        outside = repo / "outside-cache"
        outside.mkdir()
        (source / "__pycache__").symlink_to(outside, target_is_directory=True)
    else:
        os.mkfifo(source / "runtime.pyc")


def tree_snapshot(root: Path) -> dict[str, tuple[str, int, bytes | None]]:
    snapshot = {".": ("directory", stat.S_IMODE(root.stat().st_mode), None)}
    for item in sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix()):
        relative = item.relative_to(root).as_posix()
        if item.is_dir():
            snapshot[relative] = ("directory", stat.S_IMODE(item.stat().st_mode), None)
        else:
            snapshot[relative] = ("file", stat.S_IMODE(item.stat().st_mode), item.read_bytes())
    return snapshot


def test_plan_manages_only_declared_skills_and_four_instruction_blocks(tmp_path: Path) -> None:
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


def test_configuration_accepts_an_additional_valid_direct_copy_skill(tmp_path: Path) -> None:
    repo, home, _ = fixture(tmp_path)
    extra = repo / "shared" / "superpowers" / "using-superpowers"
    extra.mkdir(parents=True)
    (extra / "SKILL.md").write_text(
        "---\nname: using-superpowers\ndescription: test\n---\nbody\n"
    )
    manifest = repo / "capabilities.toml"
    manifest.write_text(
        manifest.read_text().replace(
            'skills = ["khenrix-quality", "khenrix-writing"]',
            'skills = ["khenrix-quality", "khenrix-writing", "using-superpowers"]',
        ).replace(
            'instruction_source = "house-style.md"',
            'source_roots = ["shared/skills", "shared/superpowers"]\n'
            'instruction_source = "house-style.md"',
        )
    )

    config = skillctl.load_configuration(repo, home, None)
    _, entries = skillctl.plan(config)

    assert config.skills[-1] == "using-superpowers"
    assert len([entry for entry in entries if entry.kind == "skill"]) == 9


def test_bundle_bytes_and_modes_are_part_of_plan_install_receipt_and_doctor(
    tmp_path: Path,
) -> None:
    repo, home, plain = fixture(tmp_path)
    plain_id, _ = skillctl.plan(plain)
    runtime = repo / "shared/lib/runtime"
    runtime.mkdir(parents=True)
    (runtime / "__init__.py").write_bytes(b"VERSION = 1\n")
    executable = runtime / "run.sh"
    executable.write_bytes(b"#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    manifest = repo / "capabilities.toml"
    manifest.write_text(
        manifest.read_text().replace(
            "[skill_delivery.targets]",
            '[skill_delivery.skill_bundles.khenrix-quality]\n'
            '"lib/runtime" = "shared/lib/runtime"\n\n'
            '[skill_delivery.skill_bundles.khenrix-writing]\n'
            '"lib/writing-runtime" = "shared/lib/runtime"\n\n'
            "[skill_delivery.targets]",
        )
    )
    config = skillctl.load_configuration(repo, home, None)
    plan_id, entries = skillctl.plan(config)
    quality = next(entry for entry in entries if entry.key == "skill:khenrix-quality:claude")
    assert plan_id != plain_id
    assert quality.desired_hash != skillctl.tree_hash(config.sources["khenrix-quality"])
    writing = next(entry for entry in entries if entry.key == "skill:khenrix-writing:claude")
    assert writing.desired_hash != skillctl.tree_hash(config.sources["khenrix-writing"])

    skillctl.apply(config, expect=plan_id, as_json=False)
    for root in config.targets.values():
        installed = root / "khenrix-quality" / "lib/runtime"
        assert (installed / "__init__.py").read_bytes() == b"VERSION = 1\n"
        assert stat.S_IMODE((installed / "run.sh").stat().st_mode) == 0o755
        assert (root / "khenrix-writing/lib/writing-runtime/__init__.py").read_bytes() == b"VERSION = 1\n"
    receipt = json.loads((config.state_dir / skillctl.RECEIPT_NAME).read_text())
    assert receipt["skills"]["khenrix-quality"]["source_hash"] == quality.desired_hash
    assert receipt["skills"]["khenrix-writing"]["source_hash"] == writing.desired_hash
    skillctl.doctor(config, as_json=False)

    installed_file = home / ".agents/skills/khenrix-quality/lib/runtime/__init__.py"
    installed_file.write_bytes(b"local edit\n")
    with pytest.raises(skillctl.DeliveryError, match="doctor found"):
        skillctl.doctor(config, as_json=False)
    installed_file.write_bytes(b"VERSION = 1\n")
    skillctl.doctor(config, as_json=False)

    (runtime / "__init__.py").write_bytes(b"VERSION = 2\n")
    changed_id, changed = skillctl.plan(config)
    assert changed_id != plan_id
    assert {entry.action for entry in changed if entry.skill == "khenrix-quality"} == {"UPDATE"}
    with pytest.raises(skillctl.DeliveryError, match="plan changed"):
        skillctl.apply(config, expect=plan_id, as_json=False)
    with pytest.raises(skillctl.DeliveryError, match="doctor found"):
        skillctl.doctor(config, as_json=False)


def test_bundle_tree_is_backed_up_and_restored_with_exact_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, home, config = bundle_fixture(tmp_path)
    skillctl.apply(config, expect=None, as_json=False)
    target = home / ".agents/skills/khenrix-quality"
    bundled = target / "lib/runtime/run.sh"
    bundled.chmod(0o700)
    before = tree_snapshot(target)
    (repo / "shared/lib/runtime/run.sh").write_bytes(b"#!/bin/sh\nexit 9\n")

    original_writer = skillctl.write_private_json

    def fail_receipt(path: Path, value: object, cfg: skillctl.Configuration) -> None:
        if path.name == skillctl.RECEIPT_NAME:
            raise OSError("simulated receipt failure")
        original_writer(path, value, cfg)

    monkeypatch.setattr(skillctl, "write_private_json", fail_receipt)
    with pytest.raises(OSError, match="simulated receipt failure"):
        skillctl.apply(config, expect=None, as_json=False)
    assert tree_snapshot(target) == before
    monkeypatch.setattr(skillctl, "write_private_json", original_writer)

    skillctl.apply(config, expect=None, as_json=False)
    receipt = json.loads((config.state_dir / skillctl.RECEIPT_NAME).read_text())
    assert bundled.read_bytes() == b"#!/bin/sh\nexit 9\n"
    skillctl.restore(config, receipt["backup_id"])

    assert tree_snapshot(target) == before
    assert not (config.state_dir / skillctl.RECEIPT_NAME).exists()


def test_bundle_edit_after_apply_refuses_restore(tmp_path: Path) -> None:
    repo, home, config = bundle_fixture(tmp_path)
    skillctl.apply(config, expect=None, as_json=False)
    (repo / "shared/lib/runtime/__init__.py").write_bytes(b"VERSION = 2\n")
    skillctl.apply(config, expect=None, as_json=False)
    receipt = json.loads((config.state_dir / skillctl.RECEIPT_NAME).read_text())
    bundled = home / ".agents/skills/khenrix-quality/lib/runtime/__init__.py"
    bundled.write_bytes(b"local edit\n")

    with pytest.raises(skillctl.DeliveryError, match="changed after apply"):
        skillctl.restore(config, receipt["backup_id"])
    assert bundled.read_bytes() == b"local edit\n"


@pytest.mark.parametrize("failure", ["copy", "verify"])
def test_bundled_restore_keeps_installed_tree_if_backup_stage_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    repo, home, config = bundle_fixture(tmp_path)
    skillctl.apply(config, expect=None, as_json=False)
    (repo / "shared/lib/runtime/__init__.py").write_bytes(b"VERSION = 2\n")
    skillctl.apply(config, expect=None, as_json=False)
    receipt = json.loads((config.state_dir / skillctl.RECEIPT_NAME).read_text())
    installed = home / ".gemini/config/skills/khenrix-quality"
    before = tree_snapshot(installed)
    if failure == "copy":
        original_copy = skillctl.shutil.copytree

        def fail_copy(source: Path, target: Path, **kwargs: object) -> Path:
            if str(source).startswith(str(config.state_dir / "backups")):
                raise OSError("simulated backup copy failure")
            return original_copy(source, target, **kwargs)

        monkeypatch.setattr(skillctl.shutil, "copytree", fail_copy)
        expected = "simulated backup copy failure"
    else:
        original_hash = skillctl.observed_tree_hash

        def fail_verification(root: Path, *, canonical_modes: bool = False) -> str:
            if ".khenrix-new-" in root.name:
                return "sha256:" + "0" * 64
            return original_hash(root, canonical_modes=canonical_modes)

        monkeypatch.setattr(skillctl, "observed_tree_hash", fail_verification)
        expected = "copied skill tree does not preserve"

    with pytest.raises((OSError, skillctl.DeliveryError), match=expected):
        skillctl.restore(config, receipt["backup_id"])

    assert tree_snapshot(installed) == before
    assert (config.state_dir / skillctl.RECEIPT_NAME).is_file()


def test_fanout_runtime_bundle_ignores_bytecode_but_tracks_source_changes(tmp_path: Path) -> None:
    repo, home, _ = fixture(tmp_path)
    skill_cache = repo / "shared/skills/khenrix-quality/__pycache__"
    skill_cache.mkdir()
    (skill_cache / "skill.cpython-312.pyc").write_bytes(b"ignored v1")
    source = repo / "shared/lib/fanout"
    source.parent.mkdir(parents=True)
    shutil.copytree(
        ROOT / "shared/lib/fanout", source,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    cache = source / "__pycache__"
    cache.mkdir()
    (cache / "runtime.cpython-312.pyc").write_bytes(b"ignored v1")
    (source / "stray.pyc").write_bytes(b"ignored v1")
    manifest = repo / "capabilities.toml"
    manifest.write_text(
        manifest.read_text().replace(
            "[skill_delivery.targets]",
            '[skill_delivery.skill_bundles.khenrix-quality]\n'
            '"lib/fanout" = "shared/lib/fanout"\n\n'
            "[skill_delivery.targets]",
        )
    )
    config = skillctl.load_configuration(repo, home, None)
    skillctl.apply(config, expect=None, as_json=False)
    installed = home / ".agents/skills/khenrix-quality/lib/fanout"
    assert (installed / "__init__.py").is_file()
    assert not (installed.parent.parent / "__pycache__").exists()
    assert not (installed / "__pycache__").exists()
    assert not (installed / "stray.pyc").exists()
    receipt = json.loads((config.state_dir / skillctl.RECEIPT_NAME).read_text())
    assert receipt["skills"]["khenrix-quality"]["source_hash"] == skillctl.tree_hash(
        installed.parent.parent
    )
    before_id, _ = skillctl.plan(config)

    (cache / "runtime.cpython-312.pyc").write_bytes(b"ignored v2")
    (skill_cache / "skill.cpython-312.pyc").write_bytes(b"ignored v2")
    (source / "stray.pyc").write_bytes(b"ignored v2")
    after_cache_id, _ = skillctl.plan(config)
    assert after_cache_id == before_id
    skillctl.doctor(config, as_json=False)

    (source / "__init__.py").write_bytes((source / "__init__.py").read_bytes() + b"\nCHANGED = True\n")
    after_source_id, _ = skillctl.plan(config)
    assert after_source_id != before_id
    with pytest.raises(skillctl.DeliveryError, match="doctor found"):
        skillctl.doctor(config, as_json=False)


@pytest.mark.parametrize("kind", ["symlink-cache", "fifo-pyc"])
def test_bundle_source_rejects_unsafe_ignored_entries_at_admission(
    tmp_path: Path, kind: str
) -> None:
    repo, home, _ = bundle_fixture(tmp_path)
    plant_unsafe_ignored_bundle_entry(repo, kind)

    with pytest.raises(skillctl.DeliveryError, match="symlink|unsupported"):
        skillctl.load_configuration(repo, home, None)


@pytest.mark.parametrize("kind", ["symlink-cache", "fifo-pyc"])
def test_bundle_plan_rejects_unsafe_ignored_entries_added_after_admission(
    tmp_path: Path, kind: str
) -> None:
    repo, _, config = bundle_fixture(tmp_path)
    plant_unsafe_ignored_bundle_entry(repo, kind)

    with pytest.raises(skillctl.DeliveryError, match="symlink|unsupported"):
        skillctl.plan(config)


@pytest.mark.parametrize("mutation", ["bytes", "mode"])
def test_bundle_apply_refuses_target_changed_during_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    repo, home, config = bundle_fixture(tmp_path)
    skillctl.apply(config, expect=None, as_json=False)
    (repo / "shared/lib/runtime/__init__.py").write_bytes(b"VERSION = 2\n")
    plan_id, _ = skillctl.plan(config)
    target = home / ".agents/skills/khenrix-quality/lib/runtime/__init__.py"
    original_backup = skillctl.create_backup

    def mutate_after_backup(
        cfg: skillctl.Configuration,
        current_plan: str,
        changed: list[skillctl.Entry],
        desired_sources: dict[str, Path],
    ) -> tuple[str, Path, dict[str, object]]:
        result = original_backup(cfg, current_plan, changed, desired_sources)
        if mutation == "bytes":
            target.write_bytes(b"user edit\n")
        else:
            target.chmod(0o600)
        return result

    monkeypatch.setattr(skillctl, "create_backup", mutate_after_backup)
    with pytest.raises(skillctl.DeliveryError, match="changed during apply"):
        skillctl.apply(config, expect=plan_id, as_json=False)

    if mutation == "bytes":
        assert target.read_bytes() == b"user edit\n"
    else:
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_bundle_apply_refuses_later_target_edit_without_rolling_it_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, home, config = bundle_fixture(tmp_path)
    skillctl.apply(config, expect=None, as_json=False)
    (repo / "shared/lib/runtime/__init__.py").write_bytes(b"VERSION = 2\n")
    plan_id, _ = skillctl.plan(config)
    first = home / ".claude/skills/khenrix-quality"
    later = home / ".agents/skills/khenrix-quality/lib/runtime/__init__.py"
    before_first = tree_snapshot(first)
    original_copy = skillctl.copy_skill_atomic
    calls = 0

    def mutate_later(source: Path, target: Path, cfg: skillctl.Configuration) -> None:
        nonlocal calls
        original_copy(source, target, cfg)
        calls += 1
        if calls == 1:
            later.write_bytes(b"user edit\n")

    monkeypatch.setattr(skillctl, "copy_skill_atomic", mutate_later)
    with pytest.raises(skillctl.DeliveryError, match="changed during apply"):
        skillctl.apply(config, expect=plan_id, as_json=False)

    assert tree_snapshot(first) == before_first
    assert later.read_bytes() == b"user edit\n"


def test_apply_refuses_symlink_swap_before_backup_without_reading_through_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _, config = bundle_fixture(tmp_path)
    skillctl.apply(config, expect=None, as_json=False)
    (repo / "house-style.md").write_text(
        f"{skillctl.MANAGED_BEGIN}\n# managed v2\n{skillctl.MANAGED_END}\n"
    )
    plan_id, _ = skillctl.plan(config)
    target = config.instruction_targets["maka"]
    outside = tmp_path / "outside.md"
    outside.write_text("outside stays untouched\n")
    original_plan = skillctl._plan

    def swap_after_plan(
        cfg: skillctl.Configuration, sources: dict[str, Path]
    ) -> tuple[str, list[skillctl.Entry]]:
        result = original_plan(cfg, sources)
        target.unlink()
        target.symlink_to(outside)
        return result

    original_read = skillctl.read_utf8_bytes

    def reject_symlink_read(path: Path) -> tuple[bytes, str]:
        if path == target and target.is_symlink():
            raise AssertionError("followed a swapped instruction symlink")
        return original_read(path)

    monkeypatch.setattr(skillctl, "_plan", swap_after_plan)
    monkeypatch.setattr(skillctl, "read_utf8_bytes", reject_symlink_read)
    with pytest.raises(skillctl.DeliveryError, match="symlink"):
        skillctl.apply(config, expect=plan_id, as_json=False)

    assert outside.read_text() == "outside stays untouched\n"


@pytest.mark.parametrize(
    ("mapping", "message"),
    [
        ({"not-delivered": {"lib/runtime": "shared/lib/runtime"}}, "undeclared"),
        ({"khenrix-quality": {"../escape": "shared/lib/runtime"}}, "destination.*escapes"),
        ({"khenrix-quality": {"/absolute": "shared/lib/runtime"}}, "destination.*escapes"),
        ({"khenrix-quality": {".": "shared/lib/runtime"}}, "destination.*escapes"),
        ({"khenrix-quality": {"SKILL.md": "shared/lib/runtime"}}, "collides"),
        ({"khenrix-quality": {"references": "shared/lib/runtime"}}, "collides"),
        ({"khenrix-quality": {"references/mode.md/sub": "shared/lib/runtime"}}, "collides"),
        ({"khenrix-quality": {"lib/runtime": "../outside"}}, "source.*escapes"),
        ({"khenrix-quality": {"lib/runtime": "/absolute"}}, "source.*escapes"),
        (
            {"khenrix-quality": {"lib": "shared/lib/runtime", "lib/nested": "shared/lib/runtime"}},
            "collides",
        ),
    ],
)
def test_bundle_configuration_rejects_undeclared_unsafe_or_colliding_mapping(
    tmp_path: Path, mapping: dict[str, dict[str, str]], message: str
) -> None:
    repo, home, _ = fixture(tmp_path)
    runtime = repo / "shared/lib/runtime"
    runtime.mkdir(parents=True)
    (runtime / "__init__.py").write_text("safe\n")
    tables = "".join(
        f"[skill_delivery.skill_bundles.{skill}]\n"
        + "".join(f'"{target}" = "{source}"\n' for target, source in bundles.items())
        for skill, bundles in mapping.items()
    )
    manifest = repo / "capabilities.toml"
    manifest.write_text(
        manifest.read_text().replace("[skill_delivery.targets]", tables + "\n[skill_delivery.targets]")
    )

    with pytest.raises(skillctl.DeliveryError, match=message):
        skillctl.load_configuration(repo, home, None)


@pytest.mark.parametrize("entry_kind", ["source-link", "nested-link", "fifo"])
def test_bundle_source_rejects_symlinks_and_nonregular_entries(
    tmp_path: Path, entry_kind: str
) -> None:
    repo, home, config = bundle_fixture(tmp_path)
    runtime = repo / "shared/lib/runtime"
    if entry_kind == "source-link":
        link = repo / "shared/lib/runtime-link"
        link.symlink_to(runtime, target_is_directory=True)
        source = "shared/lib/runtime-link"
    else:
        source = "shared/lib/runtime"
        if entry_kind == "nested-link":
            (runtime / "escape").symlink_to(repo / "house-style.md")
        else:
            os.mkfifo(runtime / "pipe")
    manifest = repo / "capabilities.toml"
    manifest.write_text(manifest.read_text().replace("shared/lib/runtime\"", f"{source}\""))

    with pytest.raises(skillctl.DeliveryError, match="symlink|unsupported|real directory"):
        skillctl.load_configuration(repo, home, None)


def test_bundle_plan_rejects_source_ancestor_replaced_by_symlink(tmp_path: Path) -> None:
    repo, _, config = bundle_fixture(tmp_path)
    source_parent = repo / "shared/lib"
    source_parent.rename(repo / "shared/lib-original")
    source_parent.symlink_to(repo / "shared/lib-original", target_is_directory=True)

    with pytest.raises(skillctl.DeliveryError, match="symlink"):
        skillctl.plan(config)


def test_configuration_rejects_a_skill_present_in_multiple_source_roots(
    tmp_path: Path,
) -> None:
    repo, home, _ = fixture(tmp_path)
    duplicate = repo / "shared" / "superpowers" / "khenrix-quality"
    duplicate.mkdir(parents=True)
    (duplicate / "SKILL.md").write_text(
        "---\nname: khenrix-quality\ndescription: duplicate\n---\n"
    )
    manifest = repo / "capabilities.toml"
    manifest.write_text(
        manifest.read_text().replace(
            'instruction_source = "house-style.md"',
            'source_roots = ["shared/skills", "shared/superpowers"]\n'
            'instruction_source = "house-style.md"',
        )
    )

    with pytest.raises(skillctl.DeliveryError, match="exactly one source root"):
        skillctl.load_configuration(repo, home, None)


def test_configuration_rejects_a_symlinked_source_root(tmp_path: Path) -> None:
    repo, home, _ = fixture(tmp_path)
    actual = repo / "actual-superpowers"
    actual.mkdir()
    (repo / "shared" / "superpowers").symlink_to(actual, target_is_directory=True)
    manifest = repo / "capabilities.toml"
    manifest.write_text(
        manifest.read_text().replace(
            'instruction_source = "house-style.md"',
            'source_roots = ["shared/skills", "shared/superpowers"]\n'
            'instruction_source = "house-style.md"',
        )
    )

    with pytest.raises(skillctl.DeliveryError, match="real directory|symlink"):
        skillctl.load_configuration(repo, home, None)


@pytest.mark.parametrize(
    ("name", "frontmatter", "message"),
    [
        ("../escape", "../escape", "invalid delivered skill name"),
        ("valid-name", "another-name", "frontmatter name must match"),
    ],
)
def test_configuration_rejects_unsafe_or_mismatched_direct_copy_skill(
    tmp_path: Path, name: str, frontmatter: str, message: str
) -> None:
    repo, home, _ = fixture(tmp_path)
    manifest = repo / "capabilities.toml"
    manifest.write_text(
        manifest.read_text().replace(
            'skills = ["khenrix-quality", "khenrix-writing"]',
            f'skills = ["khenrix-quality", "khenrix-writing", "{name}"]',
        )
    )
    if "/" not in name:
        extra = repo / "shared" / "skills" / name
        extra.mkdir(parents=True)
        (extra / "SKILL.md").write_text(
            f"---\nname: {frontmatter}\ndescription: test\n---\nbody\n"
        )

    with pytest.raises(skillctl.DeliveryError, match=message):
        skillctl.load_configuration(repo, home, None)


@pytest.mark.parametrize(
    "frontmatter",
    [
        "---\nname: khenrix-quality\n---\n",
        "---\nname: khenrix-quality\ndescription:\n---\n",
        '---\nname: khenrix-quality\ndescription: ""\n---\n',
        "---\nname: khenrix-quality\ndescription: >-\n---\n",
    ],
)
def test_configuration_rejects_missing_or_empty_skill_description(
    tmp_path: Path, frontmatter: str
) -> None:
    repo, home, _ = fixture(tmp_path)
    (repo / "shared/skills/khenrix-quality/SKILL.md").write_text(frontmatter)

    with pytest.raises(skillctl.DeliveryError, match="description must be non-empty"):
        skillctl.load_configuration(repo, home, None)


def test_configuration_accepts_a_non_empty_folded_skill_description(tmp_path: Path) -> None:
    repo, home, _ = fixture(tmp_path)
    (repo / "shared/skills/khenrix-quality/SKILL.md").write_text(
        "---\n"
        "name: khenrix-quality\n"
        "description: >-\n"
        "  First line of the description.\n"
        "  Second line of the description.\n"
        "license: MIT\n"
        "---\n"
    )

    config = skillctl.load_configuration(repo, home, None)

    assert config.sources["khenrix-quality"].name == "khenrix-quality"


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
