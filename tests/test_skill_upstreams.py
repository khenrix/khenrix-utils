from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "components" / "skills"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
import upstreamctl  # noqa: E402
import inventory  # noqa: E402


def git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments], cwd=repo, text=True, capture_output=True, check=True
    )
    return completed.stdout.strip()


def selected_hash(repo: Path, commit: str, paths: list[str]) -> str:
    output = git(repo, "ls-tree", "-r", commit, "--", *paths)
    payload = "".join(line + "\n" for line in sorted(output.splitlines()) if line).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def fixture(tmp_path: Path) -> tuple[Path, Path, str]:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git(upstream, "init", "-b", "main")
    git(upstream, "config", "user.name", "Test")
    git(upstream, "config", "user.email", "test@example.invalid")
    (upstream / "LICENSE").write_text("MIT\n")
    (upstream / "skill").mkdir()
    (upstream / "skill" / "SKILL.md").write_text("v1\n")
    (upstream / "README.md").write_text("readme v1\n")
    git(upstream, "add", ".")
    git(upstream, "commit", "-m", "initial")
    initial = git(upstream, "rev-parse", "HEAD")

    repo = tmp_path / "khenrix"
    manifest = repo / "shared" / "skills" / "composite" / "upstreams.toml"
    manifest.parent.mkdir(parents=True)
    paths = ["LICENSE", "skill"]
    manifest.write_text(
        "[[sources]]\n"
        'name = "sample"\n'
        f'repository = "{upstream}"\n'
        'ref = "refs/heads/main"\n'
        f'commit = "{initial}"\n'
        'paths = ["LICENSE", "skill"]\n'
        'license = "MIT"\n'
        'upstream_license_path = "LICENSE"\n'
        'local_license_path = "licenses/sample.LICENSE"\n'
        'adaptation = "test adaptation"\n'
        f'path_tree_hash = "{selected_hash(upstream, initial, paths)}"\n'
    )
    (manifest.parent / "licenses").mkdir()
    (manifest.parent / "licenses" / "sample.LICENSE").write_text("MIT\n")
    (manifest.parent / "THIRD_PARTY_NOTICES.md").write_text(
        f"# Third-party notices\n\nsample reviewed at `{initial}` with local license "
        "`licenses/sample.LICENSE`.\n"
    )
    return repo, upstream, initial


def test_status_distinguishes_unrelated_and_relevant_changes(tmp_path: Path) -> None:
    repo, upstream, _ = fixture(tmp_path)
    source = upstreamctl.load_sources(repo)[0]
    assert upstreamctl.inspect_source(source)["status"] == "CURRENT"

    (upstream / "README.md").write_text("readme v2\n")
    git(upstream, "add", "README.md")
    git(upstream, "commit", "-m", "unrelated")
    assert upstreamctl.inspect_source(source)["status"] == "REPO_AHEAD"

    (upstream / "skill" / "SKILL.md").write_text("v2\n")
    git(upstream, "add", "skill/SKILL.md")
    git(upstream, "commit", "-m", "relevant")
    record = upstreamctl.inspect_source(source)
    assert record["status"] == "UPDATE"
    assert record["remote_path_tree_hash"] != record["declared_path_tree_hash"]


def test_diff_is_scoped_and_record_updates_pin_notice_and_exact_license_copy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, upstream, _ = fixture(tmp_path)
    (upstream / "skill" / "SKILL.md").write_text("v2\n")
    (upstream / "LICENSE").write_bytes(b"MIT revision 2\n\xff\n")
    (upstream / "README.md").write_text("do not show me\n")
    git(upstream, "add", ".")
    git(upstream, "commit", "-m", "mixed")
    commit = git(upstream, "rev-parse", "HEAD")
    source = upstreamctl.load_sources(repo)[0]

    upstreamctl.show_diff(source)
    output = capsys.readouterr().out
    assert "skill/SKILL.md" in output
    assert "LICENSE" in output
    assert "README.md" not in output

    with pytest.raises(upstreamctl.UpstreamError, match="upstream license bytes changed"):
        upstreamctl.record(source, commit)
    upstreamctl.record(source, commit, accept_license_change=True)
    parsed = tomllib.loads(source.manifest.read_text())["sources"][0]
    assert parsed["commit"] == commit
    assert parsed["path_tree_hash"] == selected_hash(upstream, commit, ["LICENSE", "skill"])
    assert parsed["adaptation"] == "test adaptation"
    notice = source.manifest.parent / "THIRD_PARTY_NOTICES.md"
    assert notice.read_text().count(commit) == 1
    assert source.commit not in notice.read_text()
    assert (source.manifest.parent / "licenses" / "sample.LICENSE").read_bytes() == (
        b"MIT revision 2\n\xff\n"
    )


def test_manifest_and_notice_drift_is_rejected_before_network_work(tmp_path: Path) -> None:
    repo, _, initial = fixture(tmp_path)
    notice = next(repo.glob("shared/skills/*/THIRD_PARTY_NOTICES.md"))
    notice.write_text(notice.read_text().replace(initial, "0" * 40))

    with pytest.raises(upstreamctl.UpstreamError, match="must mention.*exactly once"):
        upstreamctl.load_sources(repo)


def test_record_refuses_notice_changed_after_load_without_changing_manifest(tmp_path: Path) -> None:
    repo, _, initial = fixture(tmp_path)
    source = upstreamctl.load_sources(repo)[0]
    before = source.manifest.read_bytes()
    notice = source.manifest.parent / "THIRD_PARTY_NOTICES.md"
    notice.write_text(notice.read_text().replace(initial, "0" * 40))

    with pytest.raises(upstreamctl.UpstreamError, match="must mention.*exactly once"):
        upstreamctl.record(source, initial)
    assert source.manifest.read_bytes() == before


def test_record_rolls_manifest_back_when_notice_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, upstream, _ = fixture(tmp_path)
    (upstream / "skill" / "SKILL.md").write_text("v2\n")
    git(upstream, "add", "skill/SKILL.md")
    git(upstream, "commit", "-m", "relevant")
    commit = git(upstream, "rev-parse", "HEAD")
    source = upstreamctl.load_sources(repo)[0]
    notice = source.manifest.parent / "THIRD_PARTY_NOTICES.md"
    before_manifest = source.manifest.read_bytes()
    before_notice = notice.read_bytes()
    real_replace = upstreamctl.os.replace
    calls = 0

    def fail_second_replace(source_path: Path, target_path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected notice replacement failure")
        real_replace(source_path, target_path)

    monkeypatch.setattr(upstreamctl.os, "replace", fail_second_replace)
    with pytest.raises(
        upstreamctl.UpstreamError, match="cannot update manifest, notice, and license copy"
    ):
        upstreamctl.record(source, commit)

    assert source.manifest.read_bytes() == before_manifest
    assert notice.read_bytes() == before_notice


def test_record_rolls_manifest_and_notice_back_when_license_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, upstream, _ = fixture(tmp_path)
    (upstream / "LICENSE").write_text("MIT revision 2\n")
    git(upstream, "add", "LICENSE")
    git(upstream, "commit", "-m", "license update")
    commit = git(upstream, "rev-parse", "HEAD")
    source = upstreamctl.load_sources(repo)[0]
    notice = source.manifest.parent / "THIRD_PARTY_NOTICES.md"
    license_copy = source.manifest.parent / "licenses" / "sample.LICENSE"
    before_manifest = source.manifest.read_bytes()
    before_notice = notice.read_bytes()
    before_license = license_copy.read_bytes()
    real_replace = upstreamctl.os.replace
    calls = 0

    def fail_third_replace(source_path: Path, target_path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("injected license replacement failure")
        real_replace(source_path, target_path)

    monkeypatch.setattr(upstreamctl.os, "replace", fail_third_replace)
    with pytest.raises(
        upstreamctl.UpstreamError, match="cannot update manifest, notice, and license copy"
    ):
        upstreamctl.record(source, commit, accept_license_change=True)

    assert source.manifest.read_bytes() == before_manifest
    assert notice.read_bytes() == before_notice
    assert license_copy.read_bytes() == before_license


def test_declared_hash_mismatch_fails_status(tmp_path: Path) -> None:
    repo, _, _ = fixture(tmp_path)
    manifest = next(repo.glob("shared/skills/*/upstreams.toml"))
    manifest.write_text(
        re.sub(r"sha256:[0-9a-f]{64}", "sha256:" + "0" * 64, manifest.read_text())
    )
    source = upstreamctl.load_sources(repo)[0]
    assert upstreamctl.inspect_source(source)["status"] == "PIN_HASH_MISMATCH"


def test_status_and_record_reject_a_stale_local_license_copy(tmp_path: Path) -> None:
    repo, _, initial = fixture(tmp_path)
    source = upstreamctl.load_sources(repo)[0]
    license_copy = source.manifest.parent / "licenses" / "sample.LICENSE"
    license_copy.write_text("stale local license\n")

    record = upstreamctl.inspect_source(source)
    assert record["status"] == "LICENSE_COPY_MISMATCH"
    assert record["local_license_hash"] != record["pinned_license_hash"]
    assert upstreamctl.status([source], as_json=False) == 1
    before_manifest = source.manifest.read_bytes()
    before_notice = upstreamctl.notice_path(source).read_bytes()

    with pytest.raises(upstreamctl.UpstreamError, match="local license copy.*does not match"):
        upstreamctl.record(source, initial)
    assert source.manifest.read_bytes() == before_manifest
    assert upstreamctl.notice_path(source).read_bytes() == before_notice
    assert license_copy.read_text() == "stale local license\n"


def test_manifest_rejects_path_traversal(tmp_path: Path) -> None:
    repo, _, _ = fixture(tmp_path)
    manifest = next(repo.glob("shared/skills/*/upstreams.toml"))
    manifest.write_text(manifest.read_text().replace('["LICENSE", "skill"]', '["../secret"]'))
    with pytest.raises(upstreamctl.UpstreamError, match="traverse"):
        upstreamctl.load_sources(repo)


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    [
        ("upstream_license_path", "../LICENSE", "may not escape"),
        ("upstream_license_path", "OTHER-LICENSE", "outside its reviewed paths"),
        ("local_license_path", "../sample.LICENSE", "may not escape"),
        ("local_license_path", "sample.LICENSE", "must live under licenses"),
    ],
)
def test_manifest_rejects_unsafe_or_unreviewed_license_paths(
    tmp_path: Path, field: str, replacement: str, message: str
) -> None:
    repo, _, _ = fixture(tmp_path)
    manifest = next(repo.glob("shared/skills/*/upstreams.toml"))
    manifest.write_text(
        re.sub(
            rf'^{field} = "[^"]+"$',
            f'{field} = "{replacement}"',
            manifest.read_text(),
            flags=re.MULTILINE,
        )
    )
    with pytest.raises(upstreamctl.UpstreamError, match=message):
        upstreamctl.load_sources(repo)


def test_notice_must_name_the_declared_local_license_copy(tmp_path: Path) -> None:
    repo, _, _ = fixture(tmp_path)
    notice = next(repo.glob("shared/skills/*/THIRD_PARTY_NOTICES.md"))
    notice.write_text(notice.read_text().replace("licenses/sample.LICENSE", "another.LICENSE"))

    with pytest.raises(upstreamctl.UpstreamError, match="local license.*exactly once"):
        upstreamctl.load_sources(repo)


def test_upgrade_inventory_adds_only_declared_receipt_backed_native_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    plugin = tmp_path / "plugin"
    state = home / ".local" / "state" / "khenrix-utils" / "skills"
    caps = plugin / "capabilities.toml"
    plugin_skill = plugin / "skills" / "plugin-skill"
    plugin_skill.mkdir(parents=True)
    (plugin_skill / "SKILL.md").write_text("plugin\n")
    state.mkdir(parents=True)
    caps.write_text(
        "[skill_delivery]\n"
        'skills = ["khenrix-writing", "khenrix-quality"]\n'
        'state_dir = "${HOME}/.local/state/khenrix-utils/skills"\n'
        "[skill_delivery.targets]\n"
        'claude = "${HOME}/.claude/skills"\n'
        'codex_maka = "${HOME}/.agents/skills"\n'
        'agy = "${HOME}/.gemini/config/skills"\n'
    )
    roots = {
        "claude": home / ".claude" / "skills",
        "codex_maka": home / ".agents" / "skills",
        "agy": home / ".gemini" / "config" / "skills",
    }
    digest = "sha256:" + "1" * 64
    skills: dict[str, object] = {}
    for name in ("khenrix-quality", "khenrix-writing", "unrelated-native"):
        targets: dict[str, object] = {}
        for target_name, root in roots.items():
            skill_dir = root / name
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(name + "\n")
            targets[target_name] = {"path": str(skill_dir), "hash": digest}
        skills[name] = {"source_hash": digest, "targets": targets}
    (state / "install-receipt.json").write_text(
        json.dumps({"schema_version": 1, "skills": skills})
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(inventory.reconcile, "find_upwards", lambda *_: caps)

    expected = ["khenrix-quality", "khenrix-writing", "plugin-skill"]
    assert inventory.installed_skills("claude") == expected
    assert inventory.installed_skills("codex") == expected
    assert inventory.installed_skills("agy") == expected

    # Installation evidence is CLI-specific. Losing agy's managed copy does not
    # erase the still-receipted Codex/Maka copy from Codex's inventory.
    (roots["agy"] / "khenrix-writing" / "SKILL.md").unlink()
    assert inventory.installed_skills("agy") == ["khenrix-quality", "plugin-skill"]
    assert inventory.installed_skills("codex") == expected


def test_upgrade_inventory_does_not_claim_native_skills_without_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    plugin = tmp_path / "plugin"
    caps = plugin / "capabilities.toml"
    plugin.mkdir()
    caps.write_text(
        "[skill_delivery]\n"
        'skills = ["khenrix-quality", "khenrix-writing"]\n'
        'state_dir = "${HOME}/missing-state"\n'
        "[skill_delivery.targets]\n"
        'claude = "${HOME}/.claude/skills"\n'
        'codex_maka = "${HOME}/.agents/skills"\n'
        'agy = "${HOME}/.gemini/config/skills"\n'
    )
    native = home / ".agents" / "skills" / "khenrix-quality"
    native.mkdir(parents=True)
    (native / "SKILL.md").write_text("unowned copy\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(inventory.reconcile, "find_upwards", lambda *_: caps)

    assert inventory.installed_skills("codex") == []
