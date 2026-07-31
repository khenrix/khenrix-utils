"""The facade must preserve module IDENTITY, not just names (spec §17)."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FANOUT = ROOT / "shared" / "skills" / "llm-council" / "scripts" / "fanout.py"


def test_facade_is_the_engine_module():
    sys.path.insert(0, str(FANOUT.parent))
    sys.path.insert(0, str(ROOT / "shared" / "lib"))
    try:
        import fanout
        import council.engine
        assert fanout is council.engine, (
            "facade must sys.modules-swap to the engine; a star-import copy "
            "breaks monkeypatch-through-globals")
    finally:
        sys.path.pop(0); sys.path.pop(0)


def test_engine_lives_under_shared_lib():
    import_path = ROOT / "shared" / "lib" / "council" / "engine.py"
    assert import_path.is_file()


def test_stub_resolves_from_new_engine_location():
    sys.path.insert(0, str(ROOT / "shared" / "lib"))
    try:
        import council.engine as e
        assert Path(e.STUB).is_file(), e.STUB
    finally:
        sys.path.pop(0)


def test_closure_includes_engine_at_new_path():
    sys.path.insert(0, str(ROOT / "scripts" / "lib"))
    try:
        import checks
        rels = [r for r, _ in checks.source_manifest(checks.ROOT, "llm-council")]
    finally:
        sys.path.pop(0)
    assert any(r == "shared/lib/council/engine.py" for r in rels), (
        "engine left the closure — make precommit would silently stop "
        "protecting llm-council (spec §17 breakage 1)")
    assert any(r.endswith("scripts/fanout.py") for r in rels)   # facade still counted
