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
from pathlib import Path

READONLY = {"GIT_OPTIONAL_LOCKS": "0"}
NO_USER_CONFIG = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
# fsmonitor/untracked-cache are daemon state; a baseline must not depend on them.
NO_DAEMON_CACHE = ("-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false")


class GitError(RuntimeError):
    """A git invocation exited non-zero and the caller asked for check=True."""


def git(repo, *args, env_extra=None, check=True, binary=False, timeout=60):
    env = dict(os.environ)
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
