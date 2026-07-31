"""Fixture git repositories for the forge suites.

Every fixture command runs in a hermetic environment, for two independent reasons:
global/system config is disabled because a developer's `commit.gpgsign = true` would
otherwise fail every fixture commit and a global `core.hooksPath` would run their hooks
inside these throwaway repos; the repository-location variables are cleared because under
an exported GIT_DIR + GIT_WORK_TREE (a hook, `git rebase --exec`, `git bisect run`)
`commit_all`'s `git add -A` would commit the developer's REAL working tree.

Disabling the config FILES is not the whole of the first reason, and the last two names
below are what it was missing — which is why the list is no longer named for locations.
Both were found by a test that exports them deliberately: `GIT_TEMPLATE_DIR` made
`make_repo`'s own `git init` install the developer's hook, so the seed commit failed, and
`GIT_CONFIG_PARAMETERS` enters at command-line precedence, above every file this env pins.
Git exports that one into any child whenever something up the tree ran `git -c …`.

The list is inlined rather than imported from `forge.gitcmd` so the fixture stays
independent of the code it is used to test.
"""
import os
import subprocess
from pathlib import Path

_HOSTILE_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_CONFIG_COUNT",
                "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES",
                "GIT_CONFIG_PARAMETERS", "GIT_TEMPLATE_DIR")


def _env():
    env = {k: v for k, v in os.environ.items() if k not in _HOSTILE_ENV}
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    return env


def git(repo, *args, timeout: int = 60):
    """Run git in `repo` under the hermetic environment above.

    Public because the suites need it as well as the builders below. A test standing in for
    a party that is NOT the engine — the agent inside a seat, or a fixture being set up —
    must not route through `forge.gitcmd`, or it would hide whether the property under test
    survives an ordinary git. The hermetic environment is still required for both reasons
    the module docstring gives.
    """
    r = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True, timeout=timeout, env=_env())
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr}")
    return r


def _run(repo, *args):
    return git(repo, *args, timeout=30)


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
