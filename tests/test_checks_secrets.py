"""The external question for a secret scanner: over WHICH BYTES is its emptiness a claim?

`checks.py --self-test` already pins that the patterns match. These ask the question the
patterns cannot answer about themselves — whether a file that IS present and DOES hold a
secret can leave the same record as a clean tree.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import checks  # noqa: E402

# Matches AKIA[0-9A-Z]{16} and is NOT in SECRET_ALLOW_SHA. The obvious choice —
# AKIAIOSFODNN7EXAMPLE — is one of this repo's own three allowlisted decoys, so a fixture
# using it comes back clean even when the scanner is working, which reads as a broken
# scanner and is in fact a fixture too well known to fail.
LIVE = "AKIA" + "Q7ZB3KXJ2M9WLPRT"


def _repo(tmp_path):
    subprocess.run(["git", "init", "-q", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    return tmp_path


def test_a_tracked_file_whose_name_git_quotes_is_still_scanned(tmp_path):
    """`git ls-files` without -z prints a C-escaped DISPLAY form for any name outside plain
    ASCII. Opening that literal raises FileNotFoundError, which the ENOENT branch reads as an
    ordinary deletion — so a tracked "café.txt" holding a live key was never scanned at all.
    """
    r = _repo(tmp_path)
    (r / "café.txt").write_text(f'AWS_KEY = "{LIVE}"\n')
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    problems = checks.scan_secrets(r)
    assert any("café.txt" in p for p in problems), \
        f"a tracked non-ASCII filename was skipped rather than scanned: {problems}"


def test_a_secret_staged_but_cleaned_from_the_working_tree_is_still_found(tmp_path):
    """The index holds the bytes a commit ships. A clean working tree is not a clean commit."""
    r = _repo(tmp_path)
    (r / "conf.py").write_text(f'KEY = "{LIVE}"\n')
    subprocess.run(["git", "add", "conf.py"], cwd=r, check=True)
    (r / "conf.py").write_text("KEY = os.environ['KEY']\n")   # cleaned, NOT staged
    problems = checks.scan_secrets(r)
    assert any("conf.py" in p for p in problems), \
        f"the staged secret that will be committed was not seen: {problems}"


def test_a_tracked_file_deleted_from_the_working_tree_is_scanned_from_the_index(tmp_path):
    """The ENOENT branch's argument — "no working-tree bytes, so no working-tree secret" — is
    sound about the working tree and was standing in for the whole claim. The index still has
    the bytes, and this is the case that distinguishes "scan the other namespace" from "skip".
    """
    r = _repo(tmp_path)
    (r / "gone.py").write_text(f'KEY = "{LIVE}"\n')
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    subprocess.run(["git", "commit", "-qm", "x"], cwd=r, check=True)
    (r / "gone.py").unlink()                                  # deleted, NOT staged
    problems = checks.scan_secrets(r)
    assert any("gone.py" in p for p in problems), \
        f"a tracked file with index bytes was skipped because the worktree copy was gone: {problems}"


def test_a_clean_repository_is_still_clean(tmp_path):
    """The guard against over-tightening: a scanner that refuses everything is not a scanner."""
    r = _repo(tmp_path)
    (r / "ok.py").write_text("KEY = os.environ['KEY']\n")
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    assert checks.scan_secrets(r) == []
