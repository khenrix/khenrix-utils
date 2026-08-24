import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("khenrix_refresh", ROOT / "scripts" / "refresh.py")
refresh = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(refresh)


def _plugin(root: Path, body: bytes = b"body") -> Path:
    root.mkdir(parents=True)
    (root / "SKILL.md").write_bytes(body)
    script = root / "scripts" / "run.py"
    script.parent.mkdir()
    script.write_bytes(b"print('ok')\n")
    script.chmod(0o755)
    return root


def test_cli_filter_is_exact_and_ordered():
    assert refresh._selected_clis("codex,agy") == ("codex", "agy")
    with pytest.raises(ValueError, match="duplicates"):
        refresh._selected_clis("codex,codex")
    with pytest.raises(ValueError, match="unknown"):
        refresh._selected_clis("codex,other")


def test_install_hash_covers_bytes_paths_and_executable_bits(tmp_path):
    src = _plugin(tmp_path / "src")
    dest = _plugin(tmp_path / "dest")
    baseline = refresh.verify_install(src, dest)
    assert len(baseline) == 64

    (dest / "SKILL.md").write_bytes(b"changed")
    with pytest.raises(RuntimeError, match="hash mismatch"):
        refresh.verify_install(src, dest)

    (dest / "SKILL.md").write_bytes(b"body")
    (dest / "scripts" / "run.py").chmod(0o644)
    with pytest.raises(RuntimeError, match="hash mismatch"):
        refresh.verify_install(src, dest)


def test_install_verification_refuses_symlink_components(tmp_path):
    src = _plugin(tmp_path / "src")
    dest = _plugin(tmp_path / "dest")
    outside = tmp_path / "outside"
    outside.mkdir()
    (dest / "scripts" / "run.py").unlink()
    (dest / "scripts" / "run.py").symlink_to(outside / "run.py")

    with pytest.raises(RuntimeError, match="symlink"):
        refresh.verify_install(src, dest)
