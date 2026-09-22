from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import replace
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


def install_fake_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executable = tmp_path / "bin" / "git"
    executable.parent.mkdir()
    executable.write_text(
        f"#!{sys.executable}\n"
        "import os, sys, time\n"
        "command = sys.argv[1]\n"
        "if command == 'environment':\n"
        "    print('|'.join([os.environ.get('GIT_TERMINAL_PROMPT', ''), "
        "os.environ.get('GIT_ASKPASS', ''), os.environ.get('GCM_INTERACTIVE', '')]))\n"
        "elif command == 'large':\n"
        "    print('x' * 1024)\n"
        "elif command == 'slow':\n"
        "    time.sleep(1)\n"
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(executable.parent) + os.pathsep + os.environ["PATH"])


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


def test_git_is_noninteractive_timed_and_output_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_git(tmp_path, monkeypatch)
    assert upstreamctl.run_git(["environment"], cwd=tmp_path).strip() == "0||Never"
    monkeypatch.setattr(upstreamctl, "MAX_GIT_OUTPUT_BYTES", 128)
    with pytest.raises(upstreamctl.UpstreamError, match="output exceeded"):
        upstreamctl.run_git(["large"], cwd=tmp_path)


def test_git_timeout_becomes_a_bounded_upstream_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_git(tmp_path, monkeypatch)
    monkeypatch.setattr(upstreamctl, "GIT_TIMEOUT_SECONDS", 0.05)
    with pytest.raises(upstreamctl.UpstreamError, match="timed out"):
        upstreamctl.run_git(["slow"], cwd=tmp_path)


def test_consumer_report_has_v2_contract_and_generic_affected_capabilities(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, _, _ = fixture(tmp_path)
    source = upstreamctl.load_sources(repo)[0]

    result = upstreamctl.status(
        [source],
        as_json=True,
        consumer="agentic-setup",
        owner_revision="a" * 40,
    )

    assert result == 0
    report = json.loads(capsys.readouterr().out)
    assert report["schema"] == "khenrix-upstreams/v2"
    assert report["consumer"] == "agentic-setup"
    assert report["owner_revision"] == "a" * 40
    assert report["integrity_ok"] is True
    assert report["check_complete"] is True
    assert report["current"] is True
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", report["report_digest"])
    record = report["sources"][0]
    assert record["source_id"] == "sample"
    assert record["affected_capability_ids"] == ["composite"]
    assert record["canonical_digest"] == source.path_tree_hash
    assert record["fetch_url"] == str(repo.parent / "upstream")
    assert record["web_url"] == str(repo.parent / "upstream")
    assert record["pinned_selector"] == "refs/heads/main"
    assert record["relationship"] == "adapted"

    unsigned = dict(report)
    unsigned.pop("report_digest")
    assert report["report_digest"] == upstreamctl.canonical_json_digest(unsigned)

    unsigned["current"] = False
    with pytest.raises(upstreamctl.UpstreamError, match="state booleans"):
        upstreamctl.validate_owner_report(unsigned)


def test_consumer_report_filters_sources_declared_for_other_consumers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, _, _ = fixture(tmp_path)
    manifest = next(repo.glob("shared/skills/*/upstreams.toml"))
    manifest.write_text(manifest.read_text() + 'consumers = ["another-consumer"]\n')
    sources = upstreamctl.load_sources(repo)

    result = upstreamctl.status(
        sources,
        as_json=True,
        consumer="agentic-setup",
        owner_revision="b" * 40,
    )

    assert result == 0
    report = json.loads(capsys.readouterr().out)
    assert report["sources"] == []
    assert report["current"] is True


def test_consumer_report_keeps_other_results_when_one_remote_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, _, _ = fixture(tmp_path)
    source = upstreamctl.load_sources(repo)[0]
    unreachable = replace(
        source,
        name="unreachable",
        repository=str(tmp_path / "missing-upstream"),
        fetch_url=str(tmp_path / "missing-upstream"),
    )

    result = upstreamctl.status(
        [source, unreachable],
        as_json=True,
        consumer="agentic-setup",
        owner_revision="c" * 40,
    )

    assert result == 3
    report = json.loads(capsys.readouterr().out)
    assert report["integrity_ok"] is True
    assert report["check_complete"] is False
    assert report["current"] is False
    records = {record["source_id"]: record for record in report["sources"]}
    assert records["sample"]["status"] == "CURRENT"
    assert records["unreachable"]["status"] == "CHECK_INCOMPLETE"
    assert "error" in records["unreachable"]


def test_consumer_report_exit_precedence_prefers_integrity_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, _, _ = fixture(tmp_path)
    source = upstreamctl.load_sources(repo)[0]
    corrupt = replace(source, path_tree_hash="sha256:" + "0" * 64)
    unreachable = replace(
        source,
        name="unreachable",
        repository=str(tmp_path / "missing-upstream"),
        fetch_url=str(tmp_path / "missing-upstream"),
    )

    result = upstreamctl.status(
        [corrupt, unreachable],
        as_json=True,
        consumer="agentic-setup",
        owner_revision="d" * 40,
    )

    assert result == 2
    report = json.loads(capsys.readouterr().out)
    assert report["integrity_ok"] is False
    assert report["check_complete"] is False
    assert report["current"] is False


def test_consumer_report_exit_precedence_prefers_incomplete_over_update(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, upstream, _ = fixture(tmp_path)
    (upstream / "skill" / "SKILL.md").write_text("v2\n")
    git(upstream, "add", "skill/SKILL.md")
    git(upstream, "commit", "-m", "relevant")
    source = upstreamctl.load_sources(repo)[0]
    unreachable = replace(
        source,
        name="unreachable",
        repository=str(tmp_path / "missing-upstream"),
        fetch_url=str(tmp_path / "missing-upstream"),
    )

    result = upstreamctl.status(
        [source, unreachable],
        as_json=True,
        consumer="agentic-setup",
        owner_revision="e" * 40,
    )

    assert result == 3
    report = json.loads(capsys.readouterr().out)
    assert report["integrity_ok"] is True
    assert report["check_complete"] is False
    assert report["current"] is False
    assert any(record["status"] == "UPDATE" for record in report["sources"])


def test_consumer_report_returns_one_for_a_relevant_update(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, upstream, _ = fixture(tmp_path)
    (upstream / "skill" / "SKILL.md").write_text("v2\n")
    git(upstream, "add", "skill/SKILL.md")
    git(upstream, "commit", "-m", "relevant")
    source = upstreamctl.load_sources(repo)[0]

    result = upstreamctl.status(
        [source],
        as_json=True,
        consumer="agentic-setup",
        owner_revision="f" * 40,
    )

    assert result == 1
    report = json.loads(capsys.readouterr().out)
    assert report["integrity_ok"] is True
    assert report["check_complete"] is True
    assert report["current"] is False
    assert report["sources"][0]["status"] == "UPDATE"


def test_report_validation_rejects_a_noncanonical_source_digest() -> None:
    report = {
        "schema": "khenrix-upstreams/v2",
        "consumer": "agentic-setup",
        "owner_revision": "a" * 40,
        "integrity_ok": True,
        "check_complete": True,
        "current": True,
        "sources": [
            {
                "source_id": "sample",
                "affected_capability_ids": ["sample-runtime"],
                "canonical_digest": "not-a-digest",
            }
        ],
        "errors": [],
    }

    with pytest.raises(upstreamctl.UpstreamError, match="canonical_digest"):
        upstreamctl.emit_owner_report(report)


def test_manifest_rejects_a_fetch_url_containing_credentials(tmp_path: Path) -> None:
    repo, _, _ = fixture(tmp_path)
    manifest = next(repo.glob("shared/skills/*/upstreams.toml"))
    manifest.write_text(
        manifest.read_text() + 'fetch_url = "https://secret@example.invalid/repo.git"\n'
    )

    with pytest.raises(upstreamctl.UpstreamError, match="fetch_url.*credentials"):
        upstreamctl.load_sources(repo)


def test_remote_checks_use_fetch_url_instead_of_the_display_repository(
    tmp_path: Path,
) -> None:
    repo, upstream, _ = fixture(tmp_path)
    manifest = next(repo.glob("shared/skills/*/upstreams.toml"))
    manifest.write_text(
        manifest.read_text().replace(
            f'repository = "{upstream}"',
            f'repository = "{tmp_path / "missing-display-repository"}"\n'
            f'fetch_url = "{upstream}"\n'
            'web_url = "https://example.invalid/upstream"',
        )
    )

    source = upstreamctl.load_sources(repo)[0]
    assert upstreamctl.inspect_source(source)["status"] == "CURRENT"


def test_malformed_manifest_emits_contract_failure_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, _, _ = fixture(tmp_path)
    manifest = next(repo.glob("shared/skills/*/upstreams.toml"))
    manifest.write_text(manifest.read_text().replace('commit = "', 'commit = "not-a-commit'))

    result = upstreamctl.main(
        [
            "--repo-root",
            str(repo),
            "status",
            "--json",
            "--consumer",
            "agentic-setup",
        ]
    )

    assert result == 2
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert captured.err == ""
    assert report["schema"] == "khenrix-upstreams/v2"
    assert report["integrity_ok"] is False
    assert report["check_complete"] is False
    assert report["current"] is False
    assert report["sources"] == []
    assert report["errors"]


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

# The bundle cases live in a non-test-named module so the repository's Makefile suite
# manifest still has one provenance-suite entry while pytest collects these tests here.
from skill_verbatim_cases import *  # noqa: F401,F403,E402
