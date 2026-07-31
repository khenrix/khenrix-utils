"""Fixture git repositories for the forge suites.

Local user identity only, and global/system config disabled for every fixture command:
a developer's `commit.gpgsign = true` would otherwise fail every fixture commit, and a
global `core.hooksPath` would run their hooks inside these throwaway repos. The two
variables are inlined rather than imported from `forge.gitcmd.NO_USER_CONFIG` so the
fixture stays independent of the code it is used to test.
"""
import os
import subprocess
from pathlib import Path


def _run(repo, *args):
    r = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True, timeout=30,
                       env={**os.environ,
                            "GIT_CONFIG_GLOBAL": os.devnull,
                            "GIT_CONFIG_SYSTEM": os.devnull})
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr}")
    return r


def make_repo(tmp_path, name="repo") -> Path:
    repo = Path(tmp_path) / name
    repo.mkdir(parents=True, exist_ok=True)
    _run(repo, "init", "-q", "-b", "main")
    _run(repo, "config", "user.email", "fixture@example.invalid")
    _run(repo, "config", "user.name", "Fixture")
    (repo / "seed.txt").write_text("seed\n")
    _run(repo, "add", "seed.txt")
    _run(repo, "commit", "-q", "-m", "seed")
    return repo


def write(repo: Path, relpath: str, text: str) -> Path:
    p = Path(repo) / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def commit_all(repo: Path, msg: str) -> str:
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", msg)
    return _run(repo, "rev-parse", "HEAD").stdout.strip()
