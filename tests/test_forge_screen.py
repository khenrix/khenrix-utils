"""The screen runs BEFORE any provider starts — a post-harvest scan is too late."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

from forge import screen, storage  # noqa: E402
from forge_fixtures import make_repo, write  # noqa: E402

# Assembled from two fragments so this FILE never holds a literal the repo's own
# scan_secrets would match. The obvious sample token (`xoxp-1234567890abcde`) cannot be
# used: its sha256 is already in checks.SECRET_ALLOW_SHA, so the screen suppresses it —
# which would make test_detects_* fail and, worse, make every "findings == []" assertion
# below pass for the wrong reason. test_detects_a_token_in_a_selected_file pins that this
# value IS detected, so the skip tests prove the skip and not the allowlist.
TOKEN = "xoxp-" + "0000000000deadbeef"


def test_detects_a_token_in_a_selected_file(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "cfg.py", f'TOKEN = "{TOKEN}"\n')
    findings, breaches = screen.screen_tree(repo, ["cfg.py"])
    assert breaches == []
    assert findings and findings[0].path == "cfg.py" and findings[0].line == 1


def test_clean_tree_is_clean(tmp_path):
    repo = make_repo(tmp_path)
    findings, breaches = screen.screen_tree(repo, ["seed.txt"])
    assert findings == [] and breaches == []


def test_binary_file_is_skipped_not_decoded(tmp_path):
    repo = make_repo(tmp_path)
    (Path(repo) / "blob.bin").write_bytes(b"\x00\x01\x02" + TOKEN.encode())
    findings, _ = screen.screen_tree(repo, ["blob.bin"])
    assert findings == [], "NUL-containing file must be skipped, not scanned"


def test_oversized_file_breaches_and_fails_closed(tmp_path):
    repo = make_repo(tmp_path)
    (Path(repo) / "big.txt").write_text("x" * 5000)
    q = storage.Quota(max_files=10, max_file_bytes=100, max_total_bytes=10_000)
    findings, breaches = screen.screen_tree(repo, ["big.txt"], quota=q)
    assert breaches and "file_bytes" in breaches[0]


def test_high_risk_names_are_flagged_by_path(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, ".env.local", "NOTHING_MATCHING_A_PATTERN=1\n")
    findings, _ = screen.screen_tree(repo, [".env.local"])
    assert any(f.pattern == "high-risk-filename" for f in findings)


def test_skips_image_suffixes(tmp_path):
    repo = make_repo(tmp_path)
    (Path(repo) / "logo.png").write_text(TOKEN)
    findings, _ = screen.screen_tree(repo, ["logo.png"])
    assert findings == []


def test_directory_selection_walks_its_files(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "pkg/a.py", "ok\n")
    write(repo, "pkg/b.py", f'K = "{TOKEN}"\n')
    findings, _ = screen.screen_tree(repo, ["pkg"])
    assert [f.path for f in findings] == ["pkg/b.py"]


def test_a_selected_symlinked_directory_is_not_followed_out_of_the_tree(tmp_path):
    """`is_dir()` follows symlinks, so a bare is_dir/is_file split walks the target.

    rglob itself already refuses to descend into a symlinked subdirectory, so the escape
    only exists for a link named directly in the selection — which is exactly the case an
    attacker-authored repo controls.
    """
    repo = make_repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.txt").write_text(f'K = "{TOKEN}"\n')
    (Path(repo) / "linkdir").symlink_to(outside, target_is_directory=True)
    findings, breaches = screen.screen_tree(repo, ["linkdir"])
    assert (findings, breaches) == ([], []), "must not read through a link out of the tree"
