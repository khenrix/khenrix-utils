"""The one audited way this package invokes git.

Every call is an argv list with an explicit environment — never a shell string, so a
path containing a metacharacter cannot become a command.

NO_USER_CONFIG is applied to EVERY call rather than offered as an opt-in: an empty template
dir does NOT neutralise a global core.hooksPath or url.*.insteadOf (spec §4.1), and a
preset a caller can forget is a preset that will be forgotten. It stays exported because
it is a published interface and because callers that build their own environment for a
non-git subprocess still need it. One env preset remains opt-in:

  READONLY       describe-only calls; GIT_OPTIONAL_LOCKS=0 stops read-oriented commands
                 opportunistically refreshing the USER's real index (spec §2.2).

Neither can make `git write-tree` safe against the real index — that command takes
index.lock unconditionally. Callers must supply GIT_INDEX_FILE instead.
"""
import os
import subprocess

READONLY = {"GIT_OPTIONAL_LOCKS": "0"}
NO_USER_CONFIG = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
# fsmonitor/untracked-cache are daemon state; a baseline must not depend on them.
NO_DAEMON_CACHE = ("-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false")

# Ambient values that decide which repository, index, object store or ref namespace a call
# resolves against — they win over `-C <repo>`. A hook, `git rebase --exec` or `git bisect
# run` exports several into everything it invokes, and inheriting one would silently point
# an engine call at the USER's repository. Dropping each narrows git back to what
# `-C <repo>` alone says.
#
# GIT_CONFIG_GLOBAL/GIT_CONFIG_SYSTEM are deliberately NOT here: they are the one pair that
# runs the other way, since removing them RESTORES ~/.gitconfig and its core.hooksPath. They
# are pinned to /dev/null in git() instead, so this package can only ever narrow config
# discovery, never widen a caller's already-hardened environment.
#
# NOT the list to strip from a child environment — that is HOSTILE_ENV below, of which this
# is a strictly narrower part. Redirection is one of three mechanisms, and a child built
# from this tuple alone still inherits the config injectors and the template dir.
REDIRECTING_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES",
)

# Everything an ambient environment must not carry into a git this package runs, or into a
# child it builds an environment for. Redirection is one of three mechanisms and the tuple
# above is named for that one, so the other two are added HERE rather than stretched into a
# name that would then describe them falsely.
#
# Every consumer drops this list, not a subset of it: `git()` below, `fleet.forge_child_env`
# for a seat, `verify._gate_env` for a gate. A name dropped in one of the three and kept in
# the other two is a hole, and the config-injector and template-dir names below were exactly
# that hole while they sat outside this tuple: GIT_CONFIG_PARAMETERS was stripped for the
# gate by a list written there, and every seat went on inheriting it.
HOSTILE_ENV = (
    *REDIRECTING_ENV,
    # The two ways config enters at COMMAND-LINE precedence — above the local file, so above
    # a clone's own core.hooksPath pin. Measured on git 2.53 in a repo with
    # `--local core.hooksPath=/dev/null` and both /dev/null pins set:
    # GIT_CONFIG_PARAMETERS="'core.hooksPath'='<dir>'" ran <dir>/pre-commit and the commit
    # exited 1. Not exotic either — git EXPORTS that one into every child whenever anything
    # up the tree ran `git -c …`.
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_PARAMETERS",
    # What `git init` and `git clone` copy into a NEW repository, hooks included. It is
    # environment rather than config, so the /dev/null pin below is not what stops it:
    # measured on git 2.53 with both pins set, `GIT_TEMPLATE_DIR=<dir> git init inner`
    # installed the template's pre-commit and the next commit in `inner` ran it.
    "GIT_TEMPLATE_DIR",
)


class GitError(RuntimeError):
    """A git invocation exited non-zero and the caller asked for check=True."""


def git(repo, *args, env_extra=None, check=True, binary=False, timeout=60):
    """Run git in `repo` with an argv list and an explicit environment.

    Environment order is a contract later tasks depend on: HOSTILE_ENV is scrubbed from the
    inherited environment, then NO_USER_CONFIG is pinned on, then `env_extra` is applied
    LAST. So an ambient GIT_DIR cannot redirect the call and the user's global config is
    never read, while a caller that deliberately passes GIT_INDEX_FILE (as baseline
    construction does) or points GIT_CONFIG_GLOBAL at a config of its own still wins.
    """
    env = {k: v for k, v in os.environ.items() if k not in HOSTILE_ENV}
    env.update(NO_USER_CONFIG)
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
