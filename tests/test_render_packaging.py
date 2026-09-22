"""render must not ship Python bytecode into the plugins.

One of four `copytree` calls in render.py lacked the `__pycache__`/`*.pyc` ignore list the
other three carried, so bytecode from any local run — a `--self-test`, an eval — was copied
into all three plugins and then merged into every live CLI install by refresh.py, which
uses `dirs_exist_ok=True` and never deletes at the destination. Nothing could see it:
`.gitignore` excludes both forms at BOTH ends, so precommit's `git diff --quiet --
marketplaces/` is structurally blind, and `render.py --check` compares nothing at all.

That is why this test asserts the OUTPUT rather than inspecting the call sites: an
`ignore=` argument is easy to assert and easy to satisfy while the defect survives through
a different path. Two paths are planted here because they fail for different reasons — one
is brought IN by a copy, the other APPEARS IN PLACE and survives because `lib/` is built
with `copy2` into a directory that is never `rmtree`'d.
"""
import importlib.util
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MARKET = ROOT / "marketplaces"
SPEC = importlib.util.spec_from_file_location("khenrix_render", ROOT / "scripts" / "render.py")
assert SPEC and SPEC.loader
render_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(render_module)


def _render() -> None:
    # Exercise packaging itself. Repository-wide chart/eval receipt validation belongs to
    # render.py --check and must not make this output regression depend on unrelated edits.
    render_module.render()


def _plant(cache_dir: Path) -> tuple[Path, bool]:
    """Drop a .pyc in `cache_dir`, reporting whether we created the dir itself."""
    created = not cache_dir.exists()
    cache_dir.mkdir(parents=True, exist_ok=True)
    stamp = cache_dir / "planted_by_test.cpython-313.pyc"
    stamp.write_bytes(b"\x00\x00\x00\x00")
    return stamp, created


def test_render_emits_no_bytecode_from_either_path():
    incoming = ROOT / "shared" / "skills" / "llm-council" / "scripts" / "__pycache__"
    residue = MARKET / "claude" / "plugins" / "khenrix-utils" / "lib" / "__pycache__"
    planted = [_plant(incoming), _plant(residue)]
    try:
        _render()
        stray_pyc = sorted(str(p.relative_to(ROOT)) for p in MARKET.rglob("*.pyc"))
        stray_dirs = sorted(str(p.relative_to(ROOT)) for p in MARKET.rglob("__pycache__"))
        assert stray_pyc == [], f"bytecode rendered into the plugins: {stray_pyc}"
        assert stray_dirs == [], f"__pycache__ rendered into the plugins: {stray_dirs}"
    finally:
        for stamp, created in planted:
            if created:
                shutil.rmtree(stamp.parent, ignore_errors=True)
            else:
                stamp.unlink(missing_ok=True)


# A second "is it idempotent across renders" test was written and then DELETED rather than
# kept: nothing imports during a render, so a second render cannot reintroduce bytecode and
# the assertion was a restatement of the one above — at the cost of two more full 3-CLI
# renders inside `make verify`. The residue-in-`pdir` path it looked like it covered is
# already the second plant above.


def test_native_only_skills_are_absent_from_every_plugin_bundle():
    # Planting a stale copy proves render removes old output as well as skipping new copies.
    skills = ("khenrix-quality", "khenrix-writing")
    for skill in skills:
        stale = MARKET / "claude" / "plugins" / "khenrix-utils" / "skills" / skill
        stale.mkdir(parents=True, exist_ok=True)
        (stale / "SKILL.md").write_text("stale plugin copy\n")

    _render()

    bundled = sorted(str(p.relative_to(ROOT)) for skill in skills
                      for p in MARKET.glob(
                          f"*/plugins/khenrix-utils/skills/{skill}/SKILL.md"))
    assert bundled == [], f"native-only skill has a second plugin path: {bundled}"

    # Capability/template validation treats ordinary shared entries as discoverability
    # metadata; a declared native-only skill need not exist in a rendered plugin tree.
    problems = render_module.checks.structure_checks(ROOT, render_module.load_caps())
    named = [p for p in problems if any(skill in p for skill in skills)]
    assert named == [], f"native-only declarations rejected by structure checks: {named}"


@pytest.mark.parametrize(
    ("skill", "body", "expected"),
    [
        (
            "khenrix-quality",
            "---\nname: wrong-but-valid\ndescription: test\n---\nbody\n",
            "must match native skill directory 'khenrix-quality'",
        ),
        (
            "khenrix-writing",
            "---\nname: khenrix-writing\n---\nbody\n",
            "missing 'description'",
        ),
        (
            "khenrix-writing",
            "---\nname: khenrix-writing\ndescription: test\n---\n" + "line\n" * 501,
            "recommended <500",
        ),
    ],
)
def test_check_validates_native_only_skill_metadata_and_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    skill: str,
    body: str,
    expected: str,
) -> None:
    (tmp_path / "shared" / "skills" / "khenrix-quality").mkdir(parents=True)
    (tmp_path / "shared" / "skills" / "khenrix-writing").mkdir(parents=True)
    for name in render_module.NATIVE_ONLY_SKILLS:
        content = f"---\nname: {name}\ndescription: test\n---\nbody\n"
        (tmp_path / "shared" / "skills" / name / "SKILL.md").write_text(content)
    (tmp_path / "shared" / "skills" / skill / "SKILL.md").write_text(body)
    (tmp_path / "capabilities.toml").write_text("")

    monkeypatch.setattr(render_module, "ROOT", tmp_path)
    monkeypatch.setattr(render_module, "CLIS", ())
    monkeypatch.setattr(render_module.checks, "run_all", lambda _root: [])

    assert render_module.check() == 1
    assert expected in capsys.readouterr().out


def test_check_rejects_stale_native_only_marketplace_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for name in render_module.NATIVE_ONLY_SKILLS:
        source = tmp_path / "shared" / "skills" / name
        source.mkdir(parents=True)
        (source / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: test\n---\nbody\n"
        )
    stale = (
        tmp_path
        / "marketplaces"
        / "claude"
        / "plugins"
        / "khenrix-utils"
        / "skills"
        / "khenrix-quality"
    )
    stale.mkdir(parents=True)
    (stale / "SKILL.md").write_text(
        "---\nname: khenrix-quality\ndescription: stale\n---\nbody\n"
    )
    (tmp_path / "capabilities.toml").write_text("")

    monkeypatch.setattr(render_module, "ROOT", tmp_path)
    monkeypatch.setattr(render_module, "CLIS", ("claude",))
    monkeypatch.setattr(render_module.checks, "run_all", lambda _root: [])

    assert render_module.check() == 1
    assert "native-only skill must not be bundled" in capsys.readouterr().out
