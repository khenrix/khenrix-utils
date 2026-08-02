"""Fixture git repositories for the forge suites.

Every fixture command runs in a hermetic environment, for two independent reasons:
the DEVELOPER's global/system config is disabled because a `commit.gpgsign = true` would
otherwise fail every fixture commit and a global `core.hooksPath` would run their hooks
inside these throwaway repos — `global_identity` below substitutes a fixture-written file
holding two keys, which is a substitution and not a relaxation; the repository-location
variables are cleared because under an exported GIT_DIR + GIT_WORK_TREE (a hook, `git rebase
--exec`, `git bisect run`) `commit_all`'s `git add -A` would commit the developer's REAL
working tree.

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
    # A fixture repository with no LOCAL identity still has to be able to commit, and the
    # only honest place for one is where a real user keeps it: the global file. Named by its
    # own variable rather than by reading an ambient GIT_CONFIG_GLOBAL, so a developer's real
    # ~/.gitconfig still cannot reach a fixture repo and the first reason above survives.
    env["GIT_CONFIG_GLOBAL"] = os.environ.get("FORGE_FIXTURE_GIT_CONFIG_GLOBAL", os.devnull)
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    return env


def git(repo, *args, timeout: int = 60, check: bool = True):
    """Run git in `repo` under the hermetic environment above.

    Public because the suites need it as well as the builders below. A test standing in for
    a party that is NOT the engine — the agent inside a seat, or a fixture being set up —
    must not route through `forge.gitcmd`, or it would hide whether the property under test
    survives an ordinary git. The hermetic environment is still required for both reasons
    the module docstring gives.

    `check=False` is for the calls whose NON-ZERO exit is the assertion: `config --get` on an
    unset key exits 1, and a test whose premise is "this repository has no local identity"
    reads that code rather than an empty string, which an unset key and an empty value share.
    """
    r = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True, timeout=timeout, env=_env())
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr}")
    return r


def _run(repo, *args):
    return git(repo, *args, timeout=30)


GLOBAL_IDENTITY = ("Global Only", "global@example.invalid")


def global_identity(tmp_path, monkeypatch) -> Path:
    """Point the fixtures at a global config carrying `GLOBAL_IDENTITY`, and return its path.

    The ordinary machine, and the one no fixture built before this: `~/.gitconfig` holds the
    identity and the repository's own config holds none. Every call `forge.gitcmd` makes pins
    the global file to /dev/null, so the ENGINE is blind to this identity while the fixture's
    own git reads it — which is the condition itself, not a simulation of it.

    Set through `monkeypatch` so it is unset again at the end of the test; a leaked variable
    would give every later fixture a global identity and quietly restore the blindness this
    exists to remove.
    """
    cfg = Path(tmp_path) / "gitconfig-global"
    cfg.write_text(f"[user]\n\tname = {GLOBAL_IDENTITY[0]}\n\temail = {GLOBAL_IDENTITY[1]}\n")
    monkeypatch.setenv("FORGE_FIXTURE_GIT_CONFIG_GLOBAL", str(cfg))
    return cfg


def make_repo(tmp_path, name="repo", *, local_identity=True) -> Path:
    """A seeded repository. `local_identity=False` leaves `user.name`/`user.email` out of the
    repository's own config, and requires `global_identity` to have been called first —
    otherwise the seed commit fails, loudly, with git's own "Please tell me who you are"."""
    repo = Path(tmp_path) / name
    repo.mkdir(parents=True, exist_ok=True)
    _run(repo, "init", "-q", "-b", "main")
    if local_identity:
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
