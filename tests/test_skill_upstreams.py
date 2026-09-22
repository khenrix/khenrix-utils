from __future__ import annotations

import copy
import base64
import hashlib
import json
import os
import re
import shutil
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


CLAUDE_MEM_DIRECT_PRIVACY_GUARDS = (
    '"CLAUDE_MEM_SEMANTIC_INJECT": "false"',
    '"CLAUDE_MEM_CLOUD_SYNC_TOKEN": ""',
    '"CLAUDE_MEM_CLOUD_SYNC_USER_ID": ""',
    '"CLAUDE_MEM_CLOUD_SYNC_HUB_URL": ""',
    '"CLAUDE_MEM_CLOUD_SYNC_DEVICE_ID": ""',
    '"CLAUDE_MEM_CLOUD_SYNC_DEVICE_NAME": ""',
    '"CLAUDE_MEM_CLOUD_SYNC_WS": "false"',
    '"CLAUDE_MEM_PRO_MEMORY_KEY": ""',
    '"CLAUDE_MEM_PRO_MEMORY_BASE_URL": ""',
    '"CLAUDE_MEM_PRO_MEMORY_MODEL": ""',
    '"CLAUDE_MEM_SERVER_URL": ""',
    '"CLAUDE_MEM_SERVER_API_KEY": ""',
    '"CLAUDE_MEM_SERVER_PROJECT_ID": ""',
    '"CLAUDE_MEM_SERVER_BETA_URL": ""',
    '"CLAUDE_MEM_SERVER_BETA_API_KEY": ""',
    '"CLAUDE_MEM_SERVER_BETA_PROJECT_ID": ""',
    '"CLAUDE_MEM_TRANSCRIPTS_ENABLED": "false"',
    '"CLAUDE_MEM_CODEX_TRANSCRIPT_INGESTION": "false"',
    '"CLAUDE_MEM_TELEMETRY": "0"',
    '"CLAUDE_MEM_TELEMETRY_ERRORS": "0"',
    '"DO_NOT_TRACK": "1"',
    '"DISABLE_ERROR_REPORTING": "1"',
    '"DISABLE_TELEMETRY": "1"',
)


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
    assert upstreamctl.run_git_bounded(["environment"], cwd=tmp_path).strip() == "0||Never"
    monkeypatch.setattr(upstreamctl, "MAX_GIT_OUTPUT_BYTES", 128)
    with pytest.raises(upstreamctl.UpstreamError, match="output exceeded"):
        upstreamctl.run_git_bounded(["large"], cwd=tmp_path)


def test_git_timeout_becomes_a_bounded_upstream_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_git(tmp_path, monkeypatch)
    monkeypatch.setattr(upstreamctl, "GIT_TIMEOUT_SECONDS", 0.05)
    with pytest.raises(upstreamctl.UpstreamError, match="timed out"):
        upstreamctl.run_git_bounded(["slow"], cwd=tmp_path)


def test_legacy_git_runner_is_unchanged_while_report_runner_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _, _ = fixture(tmp_path)
    source = upstreamctl.load_sources(repo)[0]
    inspected = upstreamctl.inspect_source(source)
    install_fake_git(tmp_path, monkeypatch)
    for name in ("GIT_TERMINAL_PROMPT", "GIT_ASKPASS", "GCM_INTERACTIVE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(upstreamctl, "MAX_GIT_OUTPUT_BYTES", 128)

    assert upstreamctl.run_git(["environment"], cwd=tmp_path).strip() == "||"
    assert len(upstreamctl.run_git(["large"], cwd=tmp_path)) > 128
    assert upstreamctl.run_git_bounded(["environment"], cwd=tmp_path).strip() == "0||Never"
    with pytest.raises(upstreamctl.UpstreamError, match="output exceeded"):
        upstreamctl.run_git_bounded(["large"], cwd=tmp_path)

    bounded_requests: list[bool] = []

    def record_inspection(
        _source: upstreamctl.Source, *, bounded: bool = False
    ) -> dict[str, object]:
        bounded_requests.append(bounded)
        return inspected

    monkeypatch.setattr(upstreamctl, "inspect_source", record_inspection)
    assert upstreamctl.owner_source_record(source)["status"] == "CURRENT"
    assert bounded_requests == [True]


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


def test_report_validation_closes_and_validates_complete_source_records(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, _, _ = fixture(tmp_path)
    source = upstreamctl.load_sources(repo)[0]
    assert (
        upstreamctl.status(
            [source],
            as_json=True,
            consumer="agentic-setup",
            owner_revision="a" * 40,
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    report.pop("report_digest")

    with_unknown = copy.deepcopy(report)
    with_unknown["sources"][0]["surprise"] = "not in v2"
    with pytest.raises(upstreamctl.UpstreamError, match="unknown.*surprise"):
        upstreamctl.validate_owner_report(with_unknown)

    malformed = [
        ("pinned_selector", 42),
        ("candidate_selector", 42),
        ("relationship", "copied-ish"),
        ("update_mode", "automatic"),
        ("review_commands", []),
        ("candidate_commit", "not-a-commit"),
        ("pinned_license_digest", "not-a-digest"),
        ("verbatim_bundle_mismatches", "none"),
    ]
    for field, value in malformed:
        changed = copy.deepcopy(report)
        changed["sources"][0][field] = value
        with pytest.raises(upstreamctl.UpstreamError, match=field):
            upstreamctl.validate_owner_report(changed)

    unsafe_version = copy.deepcopy(report)
    unsafe_version["sources"][0].update(
        pinned_package_version="1.0.0",
        candidate_package_version="unsafe\nversion",
        pinned_package_integrity="sha512-" + base64.b64encode(b"p" * 64).decode(),
        candidate_package_integrity="sha512-" + base64.b64encode(b"c" * 64).decode(),
    )
    with pytest.raises(upstreamctl.UpstreamError, match="candidate_package_version"):
        upstreamctl.validate_owner_report(unsafe_version)


def test_npm_owner_record_exposes_actionable_release_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = next(
        item for item in upstreamctl.load_sources(ROOT) if item.name == "maka-release"
    )
    candidate_integrity = "sha512-" + base64.b64encode(b"c" * 64).decode()
    monkeypatch.setattr(
        upstreamctl,
        "inspect_source",
        lambda _source, bounded=False: {
            "status": "UPDATE",
            "remote_commit": "b" * 40,
            "candidate_selector": "npm:maka-agent@0.2.0-dev.46.20260922",
            "candidate_package_version": "0.2.0-dev.46.20260922",
            "candidate_package_integrity": candidate_integrity,
            "pinned_path_tree_hash": upstreamctl.canonical_source_digest(source),
            "remote_path_tree_hash": "sha256:" + "c" * 64,
            "pinned_license_hash": "sha256:" + "d" * 64,
            "local_license_hash": "sha256:" + "d" * 64,
            "verbatim_bundle_mismatches": [],
        },
    )

    record = upstreamctl.owner_source_record(source)

    assert record["candidate_selector"] == "npm:maka-agent@0.2.0-dev.46.20260922"
    assert record["pinned_package_version"] == source.package_version
    assert record["candidate_package_version"] == "0.2.0-dev.46.20260922"
    assert record["pinned_package_integrity"] == source.package_integrity
    assert record["candidate_package_integrity"] == candidate_integrity


def test_manifest_rejects_unknown_source_keys(tmp_path: Path) -> None:
    repo, _, _ = fixture(tmp_path)
    manifest = next(repo.glob("shared/skills/*/upstreams.toml"))
    manifest.write_text(manifest.read_text() + 'packge_version = "typo"\n')

    with pytest.raises(upstreamctl.UpstreamError, match="unknown.*packge_version"):
        upstreamctl.load_sources(repo)


def test_manifest_rejects_a_fetch_url_containing_credentials(tmp_path: Path) -> None:
    repo, _, _ = fixture(tmp_path)
    manifest = next(repo.glob("shared/skills/*/upstreams.toml"))
    manifest.write_text(
        manifest.read_text() + 'fetch_url = "https://secret@example.invalid/repo.git"\n'
    )

    with pytest.raises(upstreamctl.UpstreamError, match="fetch_url.*credentials"):
        upstreamctl.load_sources(repo)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("fetch_url", "ftp://example.invalid/repo.git"),
        ("web_url", "ftp://example.invalid/repo"),
        ("web_url", "https://user:password@example.invalid/repo"),
    ],
)
def test_manifest_rejects_unsafe_or_credential_bearing_urls(
    tmp_path: Path, field: str, value: str
) -> None:
    repo, _, _ = fixture(tmp_path)
    manifest = next(repo.glob("shared/skills/*/upstreams.toml"))
    manifest.write_text(manifest.read_text() + f'{field} = "{value}"\n')

    with pytest.raises(upstreamctl.UpstreamError, match=field):
        upstreamctl.load_sources(repo)


def test_consumer_report_redacts_secret_like_check_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _, _ = fixture(tmp_path)
    source = upstreamctl.load_sources(repo)[0]
    token = "ghp_" + "a" * 36

    def fail_check(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise upstreamctl.UpstreamError(
            f"git failed for https://user:hunter2@example.invalid/repo?token={token} "
            f"Authorization: Bearer {token}"
        )

    monkeypatch.setattr(upstreamctl, "inspect_source", fail_check)
    record = upstreamctl.owner_source_record(source)

    assert record["status"] == "CHECK_INCOMPLETE"
    assert token not in record["error"]
    assert "hunter2" not in record["error"]
    assert "[REDACTED]" in record["error"]


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


def test_tracks_product_selects_newest_stable_numeric_tag_and_ignores_prereleases(
    tmp_path: Path,
) -> None:
    """A lexicographic tag sort would choose v1.9.0 or the v2 prerelease."""

    repo, upstream, initial = fixture(tmp_path)
    git(upstream, "tag", "-a", "v1.9.0", "-m", "old stable", initial)
    git(upstream, "tag", "-a", "v1.10.0", "-m", "new stable", initial)
    (upstream / "README.md").write_text("prerelease only\n")
    git(upstream, "add", "README.md")
    git(upstream, "commit", "-m", "future prerelease")
    prerelease = git(upstream, "rev-parse", "HEAD")
    git(upstream, "tag", "-a", "v2.0.0-beta.1", "-m", "prerelease", prerelease)

    manifest = next(repo.glob("shared/skills/*/upstreams.toml"))
    text = manifest.read_text().replace(
        'ref = "refs/heads/main"',
        'ref = "refs/tags/v1.9.0"\n'
        'watch = "stable_tags"\n'
        'tag_pattern = "^v([0-9]+)\\\\.([0-9]+)\\\\.([0-9]+)$"',
    )
    text = text.replace('adaptation = "test adaptation"', 'adaptation = "test adaptation"\nrelationship = "tracks_product"')
    manifest.write_text(text)

    source = upstreamctl.load_sources(repo)[0]
    record = upstreamctl.inspect_source(source)

    assert record["status"] == "UPDATE"
    assert record["remote_commit"] == initial
    assert record["candidate_selector"] == "refs/tags/v1.10.0"


def test_optional_license_requires_an_explicit_validated_declaration(tmp_path: Path) -> None:
    repo, _, _ = fixture(tmp_path)
    manifest = next(repo.glob("shared/skills/*/upstreams.toml"))
    text = manifest.read_text()
    text = re.sub(r'^upstream_license_path = .*\n', '', text, flags=re.MULTILINE)
    text = re.sub(r'^local_license_path = .*\n', '', text, flags=re.MULTILINE)
    text = text.replace('license = "MIT"', 'license = "MIT"\nlicense_optional = true')
    manifest.write_text(text)
    notice = manifest.parent / "THIRD_PARTY_NOTICES.md"
    notice.write_text(
        re.sub(
            r" with local license `licenses/sample.LICENSE`",
            "; license copy intentionally omitted (`license_optional = true`)",
            notice.read_text(),
        )
    )
    (manifest.parent / "licenses" / "sample.LICENSE").unlink()

    source = upstreamctl.load_sources(repo)[0]
    assert source.license_optional is True
    assert upstreamctl.inspect_source(source)["pinned_license_hash"] == (
        "sha256:" + hashlib.sha256(b"").hexdigest()
    )

    manifest.write_text(manifest.read_text().replace("license_optional = true\n", ""))
    with pytest.raises(upstreamctl.UpstreamError, match="license"):
        upstreamctl.load_sources(repo)


def test_record_advances_a_stable_product_watch_without_a_vendored_license(
    tmp_path: Path,
) -> None:
    repo, upstream, initial = fixture(tmp_path)
    git(upstream, "tag", "-a", "v1.9.0", "-m", "old stable", initial)
    (upstream / "skill" / "SKILL.md").write_text("v2\n")
    git(upstream, "add", "skill/SKILL.md")
    git(upstream, "commit", "-m", "new product")
    candidate = git(upstream, "rev-parse", "HEAD")
    git(upstream, "tag", "-a", "v1.10.0", "-m", "new stable", candidate)
    manifest = next(repo.glob("shared/skills/*/upstreams.toml"))
    text = manifest.read_text().replace(
        'ref = "refs/heads/main"',
        'ref = "refs/tags/v1.9.0"\nwatch = "stable_tags"\n'
        'tag_pattern = "^v([0-9]+)\\\\.([0-9]+)\\\\.([0-9]+)$"',
    )
    text = re.sub(r'^upstream_license_path = .*\n', '', text, flags=re.MULTILINE)
    text = re.sub(r'^local_license_path = .*\n', '', text, flags=re.MULTILINE)
    text = text.replace('license = "MIT"', 'license = "MIT"\nlicense_optional = true')
    manifest.write_text(text)
    notice = manifest.parent / "THIRD_PARTY_NOTICES.md"
    notice.write_text(
        f"sample release v1.9.0 at `{initial}`; license copy intentionally omitted "
        "(`license_optional = true`).\n"
    )
    (manifest.parent / "licenses" / "sample.LICENSE").unlink()

    source = upstreamctl.load_sources(repo)[0]
    upstreamctl.record(source, candidate)

    parsed = tomllib.loads(manifest.read_text())["sources"][0]
    assert parsed["ref"] == "refs/tags/v1.10.0"
    assert parsed["commit"] == candidate
    assert "v1.10.0" in notice.read_text()
    assert candidate in notice.read_text()


def test_real_owner_report_sources_cover_complete_transitive_capability_sets() -> None:
    sources = {source.name: source for source in upstreamctl.load_sources(ROOT)}
    superpowers = {
        "brainstorming",
        "diagnosing-superpowers",
        "dispatching-parallel-agents",
        "executing-plans",
        "finishing-a-development-branch",
        "receiving-code-review",
        "requesting-code-review",
        "subagent-driven-development",
        "systematic-debugging",
        "test-driven-development",
        "using-git-worktrees",
        "using-superpowers",
        "verification-before-completion",
        "writing-plans",
        "writing-skills",
    }
    assert sources["superpowers"].affected_capability_ids == tuple(sorted(superpowers))
    assert sources["markitdown-product"].relationship == "tracks_product"
    assert sources["markitdown-product"].affected_capability_ids == ("markitdown",)
    assert sources["i-have-adhd"].affected_capability_ids == ("khenrix-quality",)
    assert sources["no-ai-slop"].affected_capability_ids == ("khenrix-quality",)
    assert sources["ponytail"].affected_capability_ids == ("khenrix-quality",)
    assert sources["humanizer"].affected_capability_ids == ("khenrix-writing",)
    assert sources["maka-release"].affected_capability_ids == (
        "maka-auth-mode",
        "maka-connection-catalog",
        "maka-mcp",
        "maka-runtime-policy",
        "maka-wrapper",
    )
    assert sources["claude-mem-release"].affected_capability_ids == (
        "agy-memory-hooks",
        "codex-memory-hooks",
        "mem-search",
        "memory-runtime-controller",
    )


def test_all_khenrix_owned_skills_have_valid_codex_metadata() -> None:
    managed_shared = {"chunk-map", "llm-council", "llm-forge", "markitdown", "mikado-graph"}
    direct = {"khenrix-quality", "khenrix-writing"}
    superpowers = {
        path.name for path in (ROOT / "shared" / "superpowers").iterdir() if path.is_dir()
    }
    expected = managed_shared | direct | superpowers
    observed: set[str] = set()
    explicit_only: set[str] = set()

    for skill in sorted(expected):
        base = ROOT / "shared" / ("superpowers" if skill in superpowers else "skills") / skill
        metadata = base / "agents" / "openai.yaml"
        assert metadata.is_file(), skill
        text = metadata.read_text()
        assert re.search(r'^\s*display_name:\s*"[^"\n]+"$', text, re.MULTILINE), skill
        assert re.search(r'^\s*short_description:\s*"[^"\n]+"$', text, re.MULTILINE), skill
        prompt = re.search(r'^\s*default_prompt:\s*"([^"\n]+)"$', text, re.MULTILINE)
        assert prompt and f"${skill}" in prompt.group(1), skill
        implicit = re.search(
            r'^\s*allow_implicit_invocation:\s*(true|false)$', text, re.MULTILINE
        )
        assert implicit, skill
        if implicit.group(1) == "false":
            explicit_only.add(skill)
        observed.add(skill)

    assert observed == expected
    assert len(observed) == 22
    assert explicit_only == {"llm-forge"}


def test_maka_release_contract_covers_pin_source_integrity_and_review_surfaces() -> None:
    source = next(item for item in upstreamctl.load_sources(ROOT) if item.name == "maka-release")

    assert source.watch == "npm_releases"
    assert source.package_name == "maka-agent"
    assert source.package_version == "0.2.0-dev.44.20260920"
    assert source.package_integrity.startswith("sha512-")
    assert upstreamctl.canonical_source_digest(source) != source.path_tree_hash
    assert source.tag_pattern == r"^v0\.2\.0-dev\.([0-9]+)\.([0-9]{8})$"
    assert {
        "components/maka/.mise/locks/npm-maka-agent/0.2.0-dev.44.20260920/aube-lock.yaml",
        "components/maka/.mise/locks/npm-maka-agent/0.2.0-dev.44.20260920/package.json",
        "components/maka/mise.toml",
        "components/maka/mise.lock",
        "components/maka/scripts/install_component.py",
        "components/maka/scripts/component_doctor.py",
        "components/maka/controller/entrypoint.sh",
        "components/maka/scripts/controller-build.sh",
        "components/maka/controller/apply_maka_compat.py",
        "components/maka/controller/apply_maka_hosted_onboarding_compat.py",
        "components/maka/controller/assert-eval-runtime-path.mjs",
        "components/maka/interactive/maka_profile.py",
        "components/maka/interactive/maka_openai_relay.py",
        "components/maka/interactive/configure_maka_subscription.mjs",
        "components/maka/provenance.json",
        "components/maka/controller/provider-request-contract.mjs",
        "components/maka/controller/egress-overlay/egress_filter.py",
        "components/maka/scripts/test.sh",
    }.issubset(source.local_contract_paths)
    upstreamctl.validate_local_contract(source)

    corrupt = replace(source, local_contract_hash="sha256:" + "0" * 64)
    with pytest.raises(upstreamctl.UpstreamError, match="local contract hash"):
        upstreamctl.validate_local_contract(corrupt)
    mismatched_sri = replace(source, package_integrity="sha512-" + "A" * 86 + "==")
    with pytest.raises(upstreamctl.UpstreamError, match="package pin"):
        upstreamctl.validate_local_contract(mismatched_sri)


def test_claude_mem_contract_binds_sri_source_license_hooks_and_privacy_guards() -> None:
    source = next(
        item for item in upstreamctl.load_sources(ROOT) if item.name == "claude-mem-release"
    )

    assert source.watch == "npm_releases"
    assert source.package_name == "claude-mem"
    assert source.package_version == "13.25.1"
    assert source.package_integrity.startswith("sha512-")
    assert upstreamctl.canonical_source_digest(source) != source.path_tree_hash
    assert source.upstream_license_path == "LICENSE"
    assert {
        "components/memory/provenance.json",
        "components/memory/memoryctl.py",
        "components/memory/provider_relay.py",
        "components/memory/README.md",
    }.issubset(source.local_contract_paths)
    protected = {text for _, text in source.required_text}
    assert {
        "Nothing syncs history to Khenrix or claude-mem cloud\nservices.",
        "Chroma, cloud sync, telemetry, hosted memory fallback, Telegram,\ntranscript watching, and semantic prompt injection are disabled.",
        '"CLAUDE_MEM_CHROMA_ENABLED": "false"',
        '"CLAUDE_MEM_CLOUD_SYNC_WS": "false"',
        '{"enabled": False, "installId": "disabled-by-khenrix-utils", "decidedAt": "managed-by-khenrix-utils"}',
        'web_search="disabled"',
        'install_hooks(["claude", "codex", "agy"])',
    }.union(CLAUDE_MEM_DIRECT_PRIVACY_GUARDS).issubset(protected)
    upstreamctl.validate_local_contract(source)

    missing_guard = replace(
        source,
        required_text=source.required_text
        + (("components/memory/README.md", "privacy guard that is not present"),),
    )
    with pytest.raises(upstreamctl.UpstreamError, match="required privacy/review text"):
        upstreamctl.validate_local_contract(missing_guard)

    no_guards = replace(source, required_text=())
    with pytest.raises(upstreamctl.UpstreamError, match="required_text.*non-empty"):
        upstreamctl.validate_local_contract(no_guards)


@pytest.mark.parametrize("guard", CLAUDE_MEM_DIRECT_PRIVACY_GUARDS)
def test_claude_mem_each_direct_privacy_guard_detects_removal(
    tmp_path: Path, guard: str
) -> None:
    source = next(
        item for item in upstreamctl.load_sources(ROOT) if item.name == "claude-mem-release"
    )
    for relative in source.local_contract_paths:
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, destination)
    memoryctl = tmp_path / "components/memory/memoryctl.py"
    text = memoryctl.read_text()
    assert text.count(guard) == 1
    memoryctl.write_text(text.replace(guard, "REMOVED_PRIVACY_GUARD", 1))
    tampered = replace(
        source,
        repo_root=tmp_path,
        local_contract_hash=upstreamctl.local_contract_digest(
            tmp_path, source.local_contract_paths
        ),
    )

    with pytest.raises(upstreamctl.UpstreamError, match="required privacy/review text"):
        upstreamctl.validate_local_contract(tampered)


@pytest.mark.parametrize(
    ("source_name", "versions", "expected_version"),
    [
        (
            "maka-release",
            ["0.2.0-dev.9.20260831", "0.2.0-dev.44.20260920", "0.2.0-dev.46.20260922"],
            "0.2.0-dev.46.20260922",
        ),
        (
            "claude-mem-release",
            ["13.9.0", "13.25.1", "13.25.3", "14.0.0-beta.1"],
            "13.25.3",
        ),
    ],
)
def test_npm_release_watch_orders_numeric_versions_and_checks_pinned_sri(
    monkeypatch: pytest.MonkeyPatch,
    source_name: str,
    versions: list[str],
    expected_version: str,
) -> None:
    source = next(item for item in upstreamctl.load_sources(ROOT) if item.name == source_name)
    records: dict[str, object] = {}
    for index, version in enumerate(versions):
        records[version] = {
            "gitHead": source.commit if version == source.package_version else f"{index + 1:040x}",
            "dist": {
                "integrity": source.package_integrity
                if version == source.package_version
                else "sha512-" + base64.b64encode(b"candidate".ljust(64, b"!")).decode()
            },
        }
    monkeypatch.setattr(
        upstreamctl,
        "fetch_npm_metadata",
        lambda package: {"versions": records},
    )

    candidate, selector, integrity, pin_changed = upstreamctl.npm_release_candidate(source)

    assert selector == f"npm:{source.package_name}@{expected_version}"
    assert candidate == records[expected_version]["gitHead"]
    assert integrity == records[expected_version]["dist"]["integrity"]
    assert pin_changed is False

    records[source.package_version]["dist"]["integrity"] = (
        "sha512-" + base64.b64encode(b"changed".ljust(64, b"!")).decode()
    )
    assert upstreamctl.npm_release_candidate(source)[3] is True


def test_npm_watch_requires_canonical_package_selector(tmp_path: Path) -> None:
    repo, _, initial = fixture(tmp_path)
    manifest = next(repo.glob("shared/skills/*/upstreams.toml"))
    runtime = repo / "runtime-pin.txt"
    integrity = "sha512-" + base64.b64encode(b"p" * 64).decode()
    runtime.write_text(f"1.2.3\n{integrity}\n{initial}\n")
    digest = upstreamctl.local_contract_digest(repo, ("runtime-pin.txt",))
    manifest.write_text(
        manifest.read_text()
        + 'watch = "npm_releases"\n'
        + 'tag_pattern = "^v([0-9]+)\\\\.([0-9]+)\\\\.([0-9]+)$"\n'
        + 'package_name = "sample-package"\n'
        + 'package_version = "1.2.3"\n'
        + f'package_integrity = "{integrity}"\n'
        + "[sources.local_contract]\n"
        + 'paths = ["runtime-pin.txt"]\n'
        + f'hash = "{digest}"\n'
        + 'required_text = [{ path = "runtime-pin.txt", text = "1.2.3" }]\n'
    )

    with pytest.raises(upstreamctl.UpstreamError, match="canonical npm selector"):
        upstreamctl.load_sources(repo)

    manifest.write_text(
        manifest.read_text().replace(
            'ref = "refs/heads/main"', 'ref = "npm:sample-package@1.2.3"'
        )
    )
    assert upstreamctl.load_sources(repo)[0].ref == "npm:sample-package@1.2.3"


def test_npm_identical_pin_converges_to_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = next(item for item in upstreamctl.load_sources(ROOT) if item.name == "maka-release")
    canonical = f"npm:{source.package_name}@{source.package_version}"

    class MatchingCheckout:
        def __init__(self, _source: upstreamctl.Source, *, bounded: bool = False):
            pass

        def __enter__(self) -> "MatchingCheckout":
            return self

        def __exit__(self, *_: object) -> None:
            pass

        def path_hash(self, _commit: str) -> str:
            return source.path_tree_hash

        def file_bytes(self, _commit: str, _path: str) -> bytes:
            return b"license"

    monkeypatch.setattr(upstreamctl, "Checkout", MatchingCheckout)
    monkeypatch.setattr(
        upstreamctl,
        "validated_license_copy",
        lambda _source: (Path("LICENSE"), b"license"),
    )
    monkeypatch.setattr(
        upstreamctl,
        "npm_release_candidate",
        lambda _source, bounded=False: (
            source.commit,
            canonical,
            source.package_integrity,
            False,
        ),
    )

    inspected = upstreamctl.inspect_source(source)

    assert source.ref == canonical
    assert inspected["candidate_selector"] == canonical
    assert inspected["status"] == "CURRENT"


@pytest.mark.parametrize(
    ("selector_changed", "pin_failure", "tree_changed", "license_changed", "expected"),
    [
        (True, False, False, False, "UPDATE"),
        (False, True, False, False, "PIN_HASH_MISMATCH"),
        (False, False, True, False, "PIN_HASH_MISMATCH"),
        (False, False, False, True, "LICENSE_COPY_MISMATCH"),
    ],
)
def test_npm_pin_mismatches_do_not_report_current(
    monkeypatch: pytest.MonkeyPatch,
    selector_changed: bool,
    pin_failure: bool,
    tree_changed: bool,
    license_changed: bool,
    expected: str,
) -> None:
    original = next(
        item for item in upstreamctl.load_sources(ROOT) if item.name == "claude-mem-release"
    )
    canonical = f"npm:{original.package_name}@{original.package_version}"
    source = replace(original, ref=canonical)
    remote = "b" * 40 if selector_changed else source.commit
    selector = (
        f"npm:{source.package_name}@13.25.3" if selector_changed else canonical
    )

    class MismatchCheckout:
        def __init__(self, _source: upstreamctl.Source, *, bounded: bool = False):
            pass

        def __enter__(self) -> "MismatchCheckout":
            return self

        def __exit__(self, *_: object) -> None:
            pass

        def path_hash(self, commit: str) -> str:
            if tree_changed and commit == source.commit:
                return "sha256:" + "0" * 64
            return source.path_tree_hash

        def file_bytes(self, _commit: str, _path: str) -> bytes:
            return b"upstream-license"

    monkeypatch.setattr(upstreamctl, "Checkout", MismatchCheckout)
    monkeypatch.setattr(
        upstreamctl,
        "validated_license_copy",
        lambda _source: (
            Path("LICENSE"),
            b"local-license" if license_changed else b"upstream-license",
        ),
    )
    monkeypatch.setattr(
        upstreamctl,
        "npm_release_candidate",
        lambda _source, bounded=False: (
            remote,
            selector,
            source.package_integrity,
            pin_failure,
        ),
    )

    assert upstreamctl.inspect_source(source)["status"] == expected


def test_npm_sri_must_be_canonical_sha512_and_malformed_candidate_is_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = next(
        item for item in upstreamctl.load_sources(ROOT) if item.name == "claude-mem-release"
    )
    malformed = "sha512-" + base64.b64encode(b"too short").decode()

    with pytest.raises(upstreamctl.UpstreamError, match="SHA-512 integrity"):
        upstreamctl.validate_sha512_sri(malformed, label="test integrity")

    monkeypatch.setattr(
        upstreamctl,
        "fetch_npm_metadata",
        lambda package: {
            "versions": {
                source.package_version: {
                    "gitHead": source.commit,
                    "dist": {"integrity": source.package_integrity},
                },
                "13.25.3": {
                    "gitHead": "b" * 40,
                    "dist": {"integrity": malformed},
                },
            }
        },
    )

    record = upstreamctl.owner_source_record(source)
    assert record["status"] == "CHECK_INCOMPLETE"
    assert "SHA-512 integrity" in record["error"]


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
