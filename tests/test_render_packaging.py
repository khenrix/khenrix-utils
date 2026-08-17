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
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKET = ROOT / "marketplaces"


def _render() -> None:
    subprocess.run([sys.executable, str(ROOT / "scripts" / "render.py")],
                   check=True, cwd=ROOT, capture_output=True)


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
