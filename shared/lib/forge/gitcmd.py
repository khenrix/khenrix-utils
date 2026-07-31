"""The one audited way this package invokes git.

Every call is an argv list with an explicit environment — never a shell string, so a
path containing a metacharacter cannot become a command. Two env presets:

  READONLY       describe-only calls; GIT_OPTIONAL_LOCKS=0 stops read-oriented commands
                 opportunistically refreshing the USER's real index (spec §2.2).
  NO_USER_CONFIG global/system config disabled, for clone and for anything running inside
                 a seat clone: an empty template dir does NOT neutralise a global
                 core.hooksPath or url.*.insteadOf (spec §4.1).

Neither preset can make `git write-tree` safe against the real index — that command takes
index.lock unconditionally. Callers must supply GIT_INDEX_FILE instead.
"""
import os
import subprocess

READONLY = {"GIT_OPTIONAL_LOCKS": "0"}
NO_USER_CONFIG = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
# fsmonitor/untracked-cache are daemon state; a baseline must not depend on them.
NO_DAEMON_CACHE = ("-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false")

# Each of these overrides `-C <repo>`, and a hook, `git rebase --exec` or `git bisect run`
# exports several of them into everything it invokes. Inheriting one would silently point
# an engine call at the USER's repository — the one thing this package must never touch.
REDIRECTING_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
    "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES",
)


class GitError(RuntimeError):
    """A git invocation exited non-zero and the caller asked for check=True."""


def git(repo, *args, env_extra=None, check=True, binary=False, timeout=60):
    """Run git in `repo` with an argv list and an explicit environment.

    Environment order is a contract later tasks depend on: REDIRECTING_ENV is scrubbed from
    the inherited environment FIRST, then `env_extra` is applied. So an ambient GIT_DIR
    cannot redirect the call, while a caller that deliberately passes GIT_INDEX_FILE (as
    baseline construction does) still wins.
    """
    env = {k: v for k, v in os.environ.items() if k not in REDIRECTING_ENV}
    env.update(env_extra or {})
    r = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=not binary, timeout=timeout, env=env)
    if check and r.returncode != 0:
        err = r.stderr if not binary else r.stderr.decode("utf-8", "replace")
        raise GitError(f"git {' '.join(str(a) for a in args)} -> {r.returncode}: {err.strip()}")
    return r


def zero_oid(repo) -> str:
    """All-zeros OID at THIS repository's hash width — 40 for sha1, 64 for sha256.
    Used as update-ref's <expected-old> when creating a ref that must not already exist."""
    head = git(repo, "rev-parse", "HEAD", env_extra=READONLY).stdout.strip()
    return "0" * len(head)
