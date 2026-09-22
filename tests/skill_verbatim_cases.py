from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "components" / "skills"))
import upstreamctl  # noqa: E402


def git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments], cwd=repo, text=True, capture_output=True, check=True
    )
    return completed.stdout.strip()


def selected_hash(repo: Path, commit: str, paths: list[str]) -> str:
    output = git(repo, "ls-tree", "-r", commit, "--", *paths)
    payload = "".join(line + "\n" for line in sorted(output.splitlines()) if line).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def bundle_fixture(tmp_path: Path) -> tuple[Path, Path, str]:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git(upstream, "init", "-b", "main")
    git(upstream, "config", "user.name", "Test")
    git(upstream, "config", "user.email", "test@example.invalid")
    (upstream / "LICENSE").write_bytes(b"MIT\n")
    for name in ("other-skill", "using-superpowers"):
        skill = upstream / "skills" / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"---\nname: {name}\n---\n")
    executable = upstream / "skills" / "other-skill" / "scripts" / "run"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    git(upstream, "add", ".")
    git(upstream, "commit", "-m", "initial")
    initial = git(upstream, "rev-parse", "HEAD")

    repo = tmp_path / "khenrix"
    skills_root = repo / "shared" / "superpowers"
    for name in ("other-skill", "using-superpowers"):
        source = upstream / "skills" / name
        destination = skills_root / name
        for path in source.rglob("*"):
            relative = path.relative_to(source)
            target = destination / relative
            if path.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(path.read_bytes())
                target.chmod(0o755 if path.stat().st_mode & 0o111 else 0o644)

    local_executable = skills_root / "other-skill" / "scripts" / "run"
    local_executable.write_bytes(
        local_executable.read_bytes().replace(
            b"#!/bin/sh\n", b"#!/bin/sh\nexport SUPERPOWERS_DISABLE_TELEMETRY=1\n", 1
        )
    )

    owner = skills_root / "using-superpowers"
    (owner / "licenses").mkdir()
    (owner / "licenses" / "superpowers.LICENSE").write_bytes(b"MIT\n")
    manifest = owner / "upstreams.toml"
    manifest.write_text(
        "[[sources]]\n"
        'name = "superpowers"\n'
        f'repository = "{upstream}"\n'
        'ref = "refs/heads/main"\n'
        f'commit = "{initial}"\n'
        'paths = ["LICENSE", "skills"]\n'
        'license = "MIT"\n'
        'upstream_license_path = "LICENSE"\n'
        'local_license_path = "licenses/superpowers.LICENSE"\n'
        'adaptation = "verbatim test bundle"\n'
        f'path_tree_hash = "{selected_hash(upstream, initial, ["LICENSE", "skills"])}"\n'
        "\n[sources.verbatim_bundle]\n"
        'upstream_root = "skills"\n'
        'members = ["other-skill", "using-superpowers"]\n'
        "control_paths = [\n"
        '  "using-superpowers/upstreams.toml",\n'
        '  "using-superpowers/THIRD_PARTY_NOTICES.md",\n'
        '  "using-superpowers/licenses/superpowers.LICENSE",\n'
        "]\n"
        "\n[[sources.verbatim_bundle.overlays]]\n"
        'path = "other-skill/scripts/run"\n'
        'after = "#!/bin/sh\\n"\n'
        'insert = "export SUPERPOWERS_DISABLE_TELEMETRY=1\\n"\n'
        'reason = "test privacy overlay"\n'
    )
    (owner / "THIRD_PARTY_NOTICES.md").write_text(
        f"superpowers reviewed at `{initial}` with local license "
        "`licenses/superpowers.LICENSE`.\n"
    )
    return repo, upstream, initial


def commit_bundle_change(upstream: Path) -> str:
    skill = upstream / "skills" / "other-skill"
    (skill / "SKILL.md").write_text("---\nname: other-skill\n---\nupdated\n")
    new_script = skill / "scripts" / "new-tool"
    new_script.write_text("#!/bin/sh\nexit 2\n")
    new_script.chmod(0o755)
    git(upstream, "add", ".")
    git(upstream, "commit", "-m", "update bundle")
    return git(upstream, "rev-parse", "HEAD")


def local_snapshot(root: Path) -> dict[str, tuple[bytes, bool]]:
    result: dict[str, tuple[bytes, bool]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result[path.relative_to(root).as_posix()] = (
                path.read_bytes(),
                bool(path.stat().st_mode & 0o111),
            )
    return result


def test_status_checks_every_bundle_file_and_executable_bit(tmp_path: Path) -> None:
    repo, _, _ = bundle_fixture(tmp_path)
    source = upstreamctl.load_sources(repo)[0]
    assert upstreamctl.inspect_source(source)["status"] == "CURRENT"

    script = repo / "shared" / "superpowers" / "other-skill" / "scripts" / "run"
    script.chmod(0o644)
    record = upstreamctl.inspect_source(source)
    assert record["status"] == "VERBATIM_BUNDLE_MISMATCH"
    assert any("executable bit differs" in item for item in record["verbatim_bundle_mismatches"])

    script.chmod(0o755)
    skill = repo / "shared" / "superpowers" / "other-skill" / "SKILL.md"
    skill.write_text("changed locally\n")
    record = upstreamctl.inspect_source(source)
    assert record["status"] == "VERBATIM_BUNDLE_MISMATCH"
    assert any("content differs" in item for item in record["verbatim_bundle_mismatches"])


def test_record_refuses_a_stale_verbatim_bundle(tmp_path: Path) -> None:
    repo, upstream, _ = bundle_fixture(tmp_path)
    commit = commit_bundle_change(upstream)
    source = upstreamctl.load_sources(repo)[0]
    before = local_snapshot(repo / "shared" / "superpowers")

    with pytest.raises(upstreamctl.UpstreamError, match="use the sync command"):
        upstreamctl.record(source, commit)

    assert local_snapshot(repo / "shared" / "superpowers") == before


def test_sync_replaces_bundle_provenance_bytes_and_modes(tmp_path: Path) -> None:
    repo, upstream, initial = bundle_fixture(tmp_path)
    commit = commit_bundle_change(upstream)
    source = upstreamctl.load_sources(repo)[0]

    upstreamctl.sync(source, commit)

    refreshed = upstreamctl.load_sources(repo)[0]
    record = upstreamctl.inspect_source(refreshed)
    assert record["status"] == "CURRENT"
    assert refreshed.commit == commit
    assert initial not in upstreamctl.notice_path(refreshed).read_text()
    assert upstreamctl.notice_path(refreshed).read_text().count(commit) == 1
    local_script = repo / "shared" / "superpowers" / "other-skill" / "scripts" / "new-tool"
    assert local_script.read_bytes() == (
        upstream / "skills" / "other-skill" / "scripts" / "new-tool"
    ).read_bytes()
    assert local_script.stat().st_mode & 0o111
    assert "SUPERPOWERS_DISABLE_TELEMETRY=1" in (
        repo / "shared" / "superpowers" / "other-skill" / "scripts" / "run"
    ).read_text()


def test_sync_rolls_back_all_members_when_a_rename_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, upstream, _ = bundle_fixture(tmp_path)
    commit = commit_bundle_change(upstream)
    source = upstreamctl.load_sources(repo)[0]
    skills_root = repo / "shared" / "superpowers"
    before = local_snapshot(skills_root)
    real_replace = upstreamctl.os.replace
    calls = 0

    def fail_during_install(source_path: Path, target_path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 4:
            raise OSError("injected bundle replacement failure")
        real_replace(source_path, target_path)

    monkeypatch.setattr(upstreamctl.os, "replace", fail_during_install)
    with pytest.raises(upstreamctl.UpstreamError, match="cannot replace verbatim bundle"):
        upstreamctl.sync(source, commit)

    assert local_snapshot(skills_root) == before
    assert not list(skills_root.glob(".khenrix-upstream-sync-*"))
    refreshed = upstreamctl.load_sources(repo)[0]
    assert refreshed.commit == source.commit


def test_sync_refuses_local_drift_before_writing(tmp_path: Path) -> None:
    repo, upstream, _ = bundle_fixture(tmp_path)
    commit = commit_bundle_change(upstream)
    local_skill = repo / "shared" / "superpowers" / "other-skill" / "SKILL.md"
    local_skill.write_text("unreviewed local edit\n")
    source = upstreamctl.load_sources(repo)[0]
    before = local_snapshot(repo / "shared" / "superpowers")

    with pytest.raises(upstreamctl.UpstreamError, match="before syncing"):
        upstreamctl.sync(source, commit)

    assert local_snapshot(repo / "shared" / "superpowers") == before


def test_bundle_rejects_an_undeclared_upstream_skill(tmp_path: Path) -> None:
    repo, upstream, _ = bundle_fixture(tmp_path)
    extra = upstream / "skills" / "new-skill"
    extra.mkdir()
    (extra / "SKILL.md").write_text("---\nname: new-skill\n---\n")
    git(upstream, "add", ".")
    git(upstream, "commit", "-m", "add member")
    commit = git(upstream, "rev-parse", "HEAD")
    source = upstreamctl.load_sources(repo)[0]

    with pytest.raises(upstreamctl.UpstreamError, match="members differ"):
        upstreamctl.sync(source, commit)


def test_bundle_manifest_requires_all_local_control_paths(tmp_path: Path) -> None:
    repo, _, _ = bundle_fixture(tmp_path)
    manifest = next(repo.glob("shared/superpowers/*/upstreams.toml"))
    manifest.write_text(
        re.sub(
            r'\s*"using-superpowers/THIRD_PARTY_NOTICES.md",\n',
            "\n",
            manifest.read_text(),
        )
    )
    with pytest.raises(upstreamctl.UpstreamError, match="control_paths must include"):
        upstreamctl.load_sources(repo)


def test_real_bundle_members_exactly_match_top_level_directories() -> None:
    root = ROOT / "shared" / "superpowers"
    manifest = root / "using-superpowers" / "upstreams.toml"
    with manifest.open("rb") as handle:
        sources = tomllib.load(handle)["sources"]
    declared = sources[0]["verbatim_bundle"]["members"]
    directories = sorted(path.name for path in root.iterdir() if path.is_dir())

    assert declared == directories


def test_visual_launcher_disables_telemetry_under_a_clean_environment(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    capture = tmp_path / "telemetry-value"
    node = bin_dir / "node"
    node.write_text(
        "#!/bin/sh\n"
        ': "${CAPTURE:?missing capture path}"\n'
        "printf '%s' \"${SUPERPOWERS_DISABLE_TELEMETRY:-missing}\" > \"$CAPTURE\"\n"
    )
    node.chmod(0o755)
    script = (
        ROOT
        / "shared"
        / "superpowers"
        / "brainstorming"
        / "scripts"
        / "start-server.sh"
    )
    environment = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "CAPTURE": str(capture),
    }

    completed = subprocess.run(
        [str(script), "--project-dir", str(tmp_path / "project"), "--foreground"],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert capture.read_text() == "1"
