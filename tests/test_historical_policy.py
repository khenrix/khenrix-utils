"""Historical sources cannot re-enter active discovery, delivery or refresh."""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/lib"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "components/skills"))
import checks
import fanout_baseline as baseline
import portability
import skillctl
import charts


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


render = load("historical_render", ROOT / "scripts/render.py")
refresh = load("historical_refresh", ROOT / "scripts/refresh.py")
POLICY = {"skills": ["llm-council", "llm-forge"], "libraries": ["council", "forge"]}
HISTORICAL_SOURCE = ROOT / "tests/fixtures/llm-fanout-historical-original"


def capabilities():
    caps = tomllib.loads((ROOT / "capabilities.toml").read_text())
    caps["historical_only"] = copy.deepcopy(POLICY)
    return caps


@pytest.mark.parametrize("field,value", [
    ("skills", ["llm-council", "llm-council"]),
    ("skills", ["llm-council", "unknown"]),
    ("libraries", ["council", "../forge"]),
    ("libraries", ["council", "wikisync"]),
    ("libraries", ["forge", "council"]),
])
def test_invalid_historical_declaration_refuses_active_structure(field, value):
    caps = capabilities()
    caps["historical_only"][field] = value
    assert any("historical" in issue for issue in checks.structure_checks(ROOT, caps))


def test_historical_sources_are_absent_from_active_discovery():
    assert {p.name for p in portability._shared_skills(ROOT)}.isdisjoint(POLICY["skills"])
    assert set(checks._evald_skills(ROOT)).isdisjoint(POLICY["skills"])
    assert "llm-fanout-plan" in checks._evald_skills(ROOT)
    assert all(not any(f"receipt: {name} " in issue for name in POLICY["skills"])
               for issue in checks.receipt_gate(ROOT, advisory=False))


def test_missing_historical_declaration_refuses_the_repository_manifest():
    caps = capabilities()
    del caps["historical_only"]
    assert any("historical" in issue for issue in checks.structure_checks(ROOT, caps))


@pytest.mark.parametrize("version", [None, 99])
@pytest.mark.parametrize("reader", ["structure", "render", "delivery", "receipts",
                                     "refresh", "portability", "charts", "models"])
def test_managed_readers_require_policy_independently_of_version(tmp_path, monkeypatch, version, reader):
    caps = capabilities()
    del caps["historical_only"]
    caps.pop("version")
    if version is not None:
        caps["version"] = version
    repo = tmp_path / "managed"
    repo.mkdir()
    (repo / "capabilities.toml").write_text("" if version is None else f"version = {version}\n")
    copied = []

    def refuse_copy(*args, **kwargs):
        copied.append(args)
        raise AssertionError("managed reader reached copying before requiring historical policy")

    monkeypatch.setattr(shutil, "copy2", refuse_copy)
    monkeypatch.setattr(shutil, "copytree", refuse_copy)
    if reader == "structure":
        assert any("historical" in issue for issue in checks.structure_checks(repo, caps))
    else:
        if reader == "render":
            monkeypatch.setattr(render, "ROOT", repo)
            monkeypatch.setattr(render, "load_caps", lambda: caps)
            operation = render.render
        elif reader == "delivery":
            monkeypatch.setattr(skillctl.tomllib, "load", lambda _: caps)
            operation = lambda: skillctl.load_configuration(ROOT, tmp_path / "home", None)
        elif reader == "refresh":
            source = repo / "marketplaces/agy/plugins/khenrix-utils"
            source.mkdir(parents=True)
            destination = tmp_path / "installed"
            destination.mkdir()
            monkeypatch.setattr(refresh, "ROOT", repo)
            monkeypatch.setattr(refresh, "installed_dirs", lambda _: [destination])
            operation = lambda: refresh.sync("agy")
        elif reader == "models":
            monkeypatch.setattr(checks, "_load_caps", lambda _: caps)
            operation = lambda: checks.model_crosscheck(ROOT)
        else:
            operation = {"receipts": checks._evald_skills, "portability": portability._shared_skills,
                         "charts": charts.chart_skills}[reader]
            operation = lambda operation=operation: operation(repo)
        expected = skillctl.DeliveryError if reader == "delivery" else ValueError
        with pytest.raises(expected, match="historical"):
            operation()
    assert copied == []
    assert not (repo / "marketplaces/claude").exists()


@pytest.mark.parametrize("name", POLICY["skills"])
def test_direct_delivery_cannot_select_a_historical_skill(tmp_path, name):
    (tmp_path / "shared/skills").mkdir(parents=True)
    (tmp_path / "shared/superpowers").mkdir()
    text = (ROOT / "capabilities.toml").read_text()
    start = text.index("skills = [", text.index("[skill_delivery]"))
    end = text.index("]", start) + 1
    (tmp_path / "capabilities.toml").write_text(text[:start] + f'skills = ["{name}"]' + text[end:])
    with pytest.raises(skillctl.DeliveryError, match="historical"):
        skillctl.load_configuration(tmp_path, tmp_path / "home", None)


def test_active_model_check_does_not_import_a_historical_runtime(tmp_path):
    (tmp_path / "capabilities.toml").write_text(
        'version = 1\n[historical_only]\nskills = ["llm-council", "llm-forge"]\n'
        'libraries = ["council", "forge"]\n[models]\ncodex = ["active-model"]\n'
        '[settings.defaults.codex]\nmodel = "wrong-default"\n')
    assert checks.model_crosscheck(tmp_path) == [
        "model-crosscheck: settings.defaults.codex.model 'wrong-default' not in capabilities [models].codex"]


def test_render_removes_historical_skill_and_library_output():
    render.render()
    for cli in render.CLIS:
        plugin = render.plugin_dir(cli)
        assert all(not (plugin / "skills" / name).exists() for name in POLICY["skills"])
        assert all(not (plugin / "lib" / name).exists() for name in POLICY["libraries"])
        assert (plugin / "lib/wikisync/__init__.py").is_file()


@pytest.mark.parametrize("raw_source", ["shared/lib/council", "shared/lib/forge/engine.py",
    "shared/lib", "shared", "shared/skills/llm-forge", "shared/skills"])
def test_native_bundle_cannot_wrap_historical_sources(tmp_path, raw_source):
    repo = tmp_path / "repo"
    shutil.copytree(ROOT / "shared/skills/khenrix-quality", repo / "shared/skills/khenrix-quality")
    shutil.copytree(ROOT / "shared/skills/llm-council", repo / "shared/skills/llm-council")
    shutil.copytree(ROOT / "shared/skills/llm-forge", repo / "shared/skills/llm-forge")
    shutil.copytree(ROOT / "shared/lib/council", repo / "shared/lib/council")
    shutil.copytree(ROOT / "shared/lib/forge", repo / "shared/lib/forge")
    (repo / "shared/superpowers").mkdir()
    shutil.copy2(ROOT / "house-style.md", repo / "house-style.md")
    shutil.copytree(ROOT / "overlays", repo / "overlays")
    caps_text = (ROOT / "capabilities.toml").read_text()
    start = caps_text.index("skills = [", caps_text.index("[skill_delivery]"))
    end = caps_text.index("]", start) + 1
    caps_text = caps_text[:start] + 'skills = ["khenrix-quality"]' + caps_text[end:]
    start = caps_text.index("[skill_delivery.skill_bundles]")
    end = caps_text.index("[skill_delivery.targets]", start)
    caps_text = caps_text[:start] + (
        '[skill_delivery.skill_bundles.khenrix-quality]\n'
        f'"lib/old" = "{raw_source}"\n\n') + caps_text[end:]
    (repo / "capabilities.toml").write_text(caps_text)
    with pytest.raises(skillctl.DeliveryError, match="historical"):
        skillctl.load_configuration(repo, tmp_path / "home", None)


def test_refresh_preserves_exact_historical_backups_and_never_recreates_them(tmp_path, monkeypatch):
    shutil.copy2(ROOT / "capabilities.toml", tmp_path / "capabilities.toml")
    plugin = tmp_path / "marketplaces/agy/plugins/khenrix-utils"
    destination = tmp_path / "installed"
    for relative in ("skills/llm-council", "skills/llm-forge", "lib/council", "lib/forge"):
        for root, contents in ((plugin, b"source must not return\n"), (destination, b"old installed bytes\n")):
            target = root / relative
            target.mkdir(parents=True)
            (target / "sentinel").write_bytes(contents)
    (plugin / "skills/ordinary").mkdir()
    (plugin / "skills/ordinary/SKILL.md").write_text("ordinary\n")
    monkeypatch.setattr(refresh, "ROOT", tmp_path)
    monkeypatch.setattr(refresh, "installed_dirs", lambda cli: [destination])
    notes = refresh.sync("agy")
    assert all(not (destination / relative).exists() for relative in
               ("skills/llm-council", "skills/llm-forge", "lib/council", "lib/forge"))
    backups = list(tmp_path.glob(".khenrix-historical-backup-*"))
    assert len(backups) == 1
    assert sorted(p.read_bytes() for p in backups[0].rglob("sentinel")) == [b"old installed bytes\n"] * 4
    assert (destination / "skills/ordinary/SKILL.md").read_text() == "ordinary\n"
    assert any(str(backups[0]) in note for note in notes)


def test_refresh_refuses_a_symlink_at_an_owned_historical_child(tmp_path, monkeypatch):
    shutil.copy2(ROOT / "capabilities.toml", tmp_path / "capabilities.toml")
    destination = tmp_path / "installed"
    (destination / "lib").mkdir(parents=True)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / "sentinel").write_bytes(b"preserve\n")
    (destination / "lib/forge").symlink_to(unrelated, target_is_directory=True)
    monkeypatch.setattr(refresh, "ROOT", tmp_path)
    monkeypatch.setattr(refresh, "installed_dirs", lambda cli: [destination])
    with pytest.raises(ValueError, match="symlink"):
        refresh.sync("agy")
    assert (unrelated / "sentinel").read_bytes() == b"preserve\n"


def test_explicit_original_snapshot_uses_existing_checker_and_refuses_damage(tmp_path):
    assert hasattr(baseline, "prepare_historical"), "explicit original snapshot preparation is missing"
    snapshot = tmp_path / "owned" / "snapshot"
    baseline.prepare_historical(ROOT, snapshot, source=HISTORICAL_SOURCE)
    expected = json.loads((ROOT / "evals/llm-fanout/baseline-lock.json").read_text())
    assert baseline.check_lock(expected, snapshot) == []
    assert baseline.check_historical(expected, snapshot) == []
    original = snapshot / "shared/lib/council/engine.py"
    data = original.read_bytes()
    original.write_bytes(b"tampered\n")
    assert baseline.check_historical(expected, snapshot)
    original.write_bytes(data)
    extra = snapshot / "unlisted"
    extra.write_bytes(b"extra\n")
    extra.chmod(0o600)
    assert baseline.check_historical(expected, snapshot)
    extra.unlink()
    original.unlink()
    with pytest.raises(baseline.BaselineError):
        baseline.check_historical(expected, snapshot)


def test_historical_preparation_refuses_inside_checkout_parent_traversal_before_writes(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    snapshot = outside / ".." / "repo/owned/snapshot"
    assert not snapshot.is_relative_to(repo)
    assert snapshot.resolve().is_relative_to(repo)
    git_reads = []
    run = baseline.subprocess.run

    def original_git_blobs(argv, **kwargs):
        git_reads.append(argv)
        return run([argv[0], argv[1], str(ROOT), *argv[3:]], **kwargs)

    monkeypatch.setattr(baseline.subprocess, "run", original_git_blobs)
    with pytest.raises(baseline.BaselineError, match="outside|traversal"):
        baseline.prepare_historical(repo, snapshot)
    assert git_reads == []
    assert not (repo / "owned").exists()
    assert not snapshot.exists()
    assert not (snapshot.parent / "provenance.json").exists()


@pytest.mark.parametrize("damage", ["digest", "provenance", "mode", "symlink", "revision"])
def test_historical_snapshot_refuses_wrong_subject_or_provenance(tmp_path, damage):
    assert hasattr(baseline, "prepare_historical"), "explicit original snapshot preparation is missing"
    snapshot = tmp_path / "owned" / "snapshot"
    if damage == "revision":
        with pytest.raises(baseline.BaselineError):
            baseline.prepare_historical(ROOT, snapshot, revision="HEAD")
        assert not snapshot.exists()
        return
    baseline.prepare_historical(ROOT, snapshot, source=HISTORICAL_SOURCE)
    expected = json.loads((ROOT / "evals/llm-fanout/baseline-lock.json").read_text())
    if damage == "digest":
        expected["sources"]["shared/lib/council/engine.py"] = "0" * 64
    elif damage == "provenance":
        provenance = snapshot.parent / "provenance.json"
        data = json.loads(provenance.read_text())
        data["revision"] = "0" * 40
        provenance.write_text(json.dumps(data))
    elif damage == "mode":
        snapshot.chmod(0o755)
    else:
        alias = tmp_path / "alias"
        alias.symlink_to(snapshot, target_is_directory=True)
        snapshot = alias
    with pytest.raises(baseline.BaselineError):
        baseline.check_historical(expected, snapshot)


def test_filesystem_historical_source_is_offline_exact_and_private(tmp_path, monkeypatch):
    def no_git(*args, **kwargs):
        raise AssertionError("explicit source must never read Git or invoke a process")

    monkeypatch.setattr(baseline.subprocess, "run", no_git)
    snapshot = tmp_path / "owned/snapshot"
    baseline.prepare_historical(ROOT, snapshot, source=HISTORICAL_SOURCE)
    expected = json.loads((HISTORICAL_SOURCE / baseline.HISTORICAL_LOCK).read_bytes())
    originals = {p.relative_to(HISTORICAL_SOURCE): p.read_bytes()
                 for p in HISTORICAL_SOURCE.rglob("*") if p.is_file()}
    assert len(originals) == 60
    assert {p.relative_to(snapshot): p.read_bytes()
            for p in snapshot.rglob("*") if p.is_file()} == originals
    assert baseline.check_lock(expected, snapshot) == []
    assert baseline.check_historical(expected, snapshot) == []
    for path in [snapshot.parent, snapshot, *snapshot.rglob("*"), snapshot.parent / "provenance.json"]:
        info = path.lstat()
        assert info.st_uid == os.getuid()
        assert stat.S_IMODE(info.st_mode) == (0o700 if path.is_dir() else 0o600)
        if path.is_file():
            assert info.st_nlink == 1
    provenance = json.loads((snapshot.parent / "provenance.json").read_bytes())
    assert provenance["subject"] == "historical-source-blobs"
    assert provenance["revision"] == "f063e2f4dc6c976b3dd314685642678d8c8ab7bc"
    assert provenance["blob_count"] == 59


@pytest.mark.parametrize("damage", ["lock", "blob", "missing", "extra", "empty-directory",
    "file-symlink", "directory-symlink", "root-symlink", "ancestor-symlink", "fifo",
    "unreadable", "missing-root", "file-root", "parent-traversal"])
def test_invalid_filesystem_historical_source_refuses_before_any_destination_write(
    tmp_path, monkeypatch, damage,
):
    source = tmp_path / "source"
    shutil.copytree(HISTORICAL_SOURCE, source)
    blob = source / "shared/lib/council/engine.py"
    if damage in {"lock", "blob"}:
        (source / baseline.HISTORICAL_LOCK if damage == "lock" else blob).write_bytes(b"tampered\n")
    elif damage == "missing":
        blob.unlink()
    elif damage == "extra":
        (source / "extra").write_bytes(b"extra\n")
    elif damage == "empty-directory":
        (source / "extra").mkdir()
    elif damage == "file-symlink":
        blob.unlink()
        blob.symlink_to(HISTORICAL_SOURCE / "shared/lib/council/engine.py")
    elif damage == "directory-symlink":
        shutil.rmtree(source / "shared/lib/council")
        (source / "shared/lib/council").symlink_to(HISTORICAL_SOURCE / "shared/lib/council")
    elif damage in {"root-symlink", "ancestor-symlink"}:
        alias = tmp_path / "alias"
        alias.symlink_to(source if damage == "root-symlink" else tmp_path)
        source = alias if damage == "root-symlink" else alias / "source"
    elif damage == "fifo":
        blob.unlink()
        os.mkfifo(blob)
    elif damage == "unreadable":
        blob.chmod(0)
    elif damage == "missing-root":
        source = tmp_path / "missing"
    elif damage == "file-root":
        source = blob
    elif damage == "parent-traversal":
        source = source / "shared/.."
    monkeypatch.setattr(baseline.subprocess, "run", lambda *_, **__: pytest.fail("source fallback"))
    snapshot = tmp_path / "new-parent/snapshot"
    with pytest.raises(baseline.BaselineError):
        baseline.prepare_historical(ROOT, snapshot, source=source)
    assert not snapshot.parent.exists()
    assert not snapshot.exists()
    assert not (snapshot.parent / "provenance.json").exists()


def test_filesystem_historical_source_copies_the_bytes_it_hashed(tmp_path, monkeypatch):
    source = tmp_path / "source"
    shutil.copytree(HISTORICAL_SOURCE, source)
    blob = source / "shared/lib/council/engine.py"
    original = blob.read_bytes()
    read_bytes = Path.read_bytes

    def replace_after_read(path):
        data = read_bytes(path)
        if path == blob:
            blob.write_bytes(b"changed after source read\n")
        return data

    monkeypatch.setattr(Path, "read_bytes", replace_after_read)
    snapshot = tmp_path / "owned/snapshot"
    baseline.prepare_historical(ROOT, snapshot, source=source)
    assert (snapshot / "shared/lib/council/engine.py").read_bytes() == original


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("damage", ["traversal", "inside", "symlink", "freshness", "revision"])
def test_historical_modes_refuse_invalid_destination_before_source_access(
    tmp_path, monkeypatch, explicit, damage,
):
    repo = tmp_path / "repo"
    repo.mkdir(mode=0o700)
    snapshot = tmp_path / "owned/snapshot"
    revision = baseline.HISTORICAL_REVISION
    if damage == "traversal":
        snapshot = tmp_path / "unused/../owned/snapshot"
    elif damage == "inside":
        snapshot = repo / "owned/snapshot"
    elif damage == "symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(tmp_path)
        snapshot = alias / "owned/snapshot"
    elif damage == "freshness":
        snapshot.parent.mkdir(mode=0o700)
        snapshot.mkdir(mode=0o700)
    else:
        revision = "HEAD"
    monkeypatch.setattr(baseline.subprocess, "run", lambda *_, **__: pytest.fail("early Git read"))
    monkeypatch.setattr(Path, "read_bytes", lambda *_: pytest.fail("early filesystem read"))
    options = {"source": HISTORICAL_SOURCE} if explicit else {}
    with pytest.raises(baseline.BaselineError):
        baseline.prepare_historical(repo, snapshot, revision=revision, **options)
    assert not (snapshot.parent / "provenance.json").exists()
    if damage != "freshness":
        assert not snapshot.parent.exists()


def test_original_git_preparation_requests_only_exact_revision_and_locked_paths(tmp_path, monkeypatch):
    expected = json.loads((HISTORICAL_SOURCE / baseline.HISTORICAL_LOCK).read_bytes())
    paths = [baseline.HISTORICAL_LOCK, *[name for category in ("sources", "fixtures", "receipts")
                                       for name in expected[category]]]
    requests = []

    def original_blob(argv, **kwargs):
        requests.append(argv)
        assert kwargs == {"check": True, "capture_output": True}
        relative = argv[-1].split(":", 1)[1]
        return subprocess.CompletedProcess(argv, 0, (HISTORICAL_SOURCE / relative).read_bytes(), b"")

    monkeypatch.setattr(baseline.subprocess, "run", original_blob)
    snapshot = tmp_path / "owned/snapshot"
    assert baseline.main(["prepare-historical", "--root", str(ROOT),
                          "--historical-root", str(snapshot)]) == 0
    assert requests == [["git", "-C", str(ROOT), "show",
                         "f063e2f4dc6c976b3dd314685642678d8c8ab7bc:" + relative]
                        for relative in paths]
    assert baseline.check_historical(expected, snapshot) == []


@pytest.mark.parametrize("failure", [subprocess.CalledProcessError(128, ["git"]), FileNotFoundError("git")])
def test_unavailable_original_git_refuses_without_filesystem_fallback(tmp_path, monkeypatch, failure):
    def unavailable(*args, **kwargs):
        raise failure

    monkeypatch.setattr(baseline.subprocess, "run", unavailable)
    monkeypatch.setattr(Path, "read_bytes", lambda *_: pytest.fail("implicit source fallback"))
    snapshot = tmp_path / "owned/snapshot"
    with pytest.raises(baseline.BaselineError, match="unavailable"):
        baseline.prepare_historical(ROOT, snapshot)
    assert not snapshot.parent.exists()


def test_prepare_historical_cli_selects_explicit_filesystem_source(tmp_path, monkeypatch):
    monkeypatch.setattr(baseline.subprocess, "run", lambda *_, **__: pytest.fail("CLI source fallback"))
    snapshot = tmp_path / "owned/snapshot"
    assert baseline.main(["prepare-historical", "--root", str(ROOT),
        "--historical-root", str(snapshot), "--historical-source", str(HISTORICAL_SOURCE)]) == 0
    expected = json.loads((HISTORICAL_SOURCE / baseline.HISTORICAL_LOCK).read_bytes())
    assert baseline.check_historical(expected, snapshot) == []


def test_historical_source_selector_refuses_other_cli_operations(tmp_path, monkeypatch):
    monkeypatch.setattr(baseline, "check_historical", lambda *_: pytest.fail("ignored source selector"))
    assert baseline.main(["check-historical", "--root", str(ROOT),
        "--historical-root", str(tmp_path / "snapshot"),
        "--historical-source", str(HISTORICAL_SOURCE)]) == 2


@pytest.mark.parametrize("damage", ["parent-mode", "provenance", "provenance-symlink"])
def test_filesystem_preparation_preserves_private_parent_and_fresh_provenance_refusals(tmp_path, damage):
    parent = tmp_path / "owned"
    parent.mkdir(mode=0o700)
    provenance = parent / "provenance.json"
    if damage == "parent-mode":
        parent.chmod(0o755)
    elif damage == "provenance":
        provenance.write_bytes(b"existing provenance\n")
    else:
        provenance.symlink_to(tmp_path / "uncreated")
    snapshot = parent / "snapshot"
    with pytest.raises(baseline.BaselineError):
        baseline.prepare_historical(ROOT, snapshot, source=HISTORICAL_SOURCE)
    assert not snapshot.exists()
    if damage == "provenance":
        assert provenance.read_bytes() == b"existing provenance\n"
    elif damage == "provenance-symlink":
        assert provenance.is_symlink()
        assert not (tmp_path / "uncreated").exists()
    else:
        assert not provenance.exists()
