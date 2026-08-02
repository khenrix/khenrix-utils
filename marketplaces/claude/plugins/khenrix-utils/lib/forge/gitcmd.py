"""The one audited way this package invokes git.

Every call is an argv list with an explicit environment — never a shell string, so a
path containing a metacharacter cannot become a command.

NO_USER_CONFIG is applied to EVERY call rather than offered as an opt-in: an empty template
dir does NOT neutralise a global core.hooksPath or url.*.insteadOf (spec §4.1), and a
preset a caller can forget is a preset that will be forgotten. It stays exported because
it is a published interface and because callers that build their own environment for a
non-git subprocess still need it. It reaches the GLOBAL and SYSTEM files only; a
repository's own config is read by the git that runs in it, which is what the three argv
presets below are for. One env preset remains opt-in:

  READONLY       describe-only calls; GIT_OPTIONAL_LOCKS=0 stops read-oriented commands
                 opportunistically refreshing the USER's real index (spec §2.2).

Neither can make `git write-tree` safe against the real index — that command takes
index.lock unconditionally. Callers must supply GIT_INDEX_FILE instead.
"""
import os
import subprocess

READONLY = {"GIT_OPTIONAL_LOCKS": "0"}
NO_USER_CONFIG = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
# fsmonitor/untracked-cache are daemon state; a baseline must not depend on them. The
# fsmonitor half also carries a second, stronger reason, so dropping this from a call is not
# a caching decision: the value is a PROGRAM git runs, set in the repository's own
# `.git/config`, and §5 step 1 admits no repository-supplied code before authorization.
#
# WHICH CALLS NEED IT: the ones that LOAD AN INDEX. Measured on git 2.53.0 against a monitor
# script that touches a file — `ls-files` in every form (`--cached` alone, no `--others`),
# `status`, `diff` (including `diff <commit> -- <pathspec>`), `add`, `write-tree`,
# `check-attr`, `update-index` and `apply --index` all ran it; `rev-parse`, `show-ref`,
# `for-each-ref`, `symbolic-ref`, `cat-file`, `config`, `update-ref`, `commit-tree`, `clone`,
# a bare `apply` and `apply --numstat` did not. GIT_INDEX_FILE does NOT exempt a call:
# `write-tree` over `baseline`'s private index copy still ran the monitor, because the program
# comes from the repository's config and not from the index. Nor is the USER's repository the
# only tree this matters in: a builder SEAT's `.git/config` is the agent's to write, and until
# the flags went on `harvest.artifact_set` a seat that set the key had its program run by the
# engine, at harvest, after the agent's own process was gone.
#
# Every call site is held to the rule by
# `test_every_index_loading_call_carries_the_daemon_cache_flags`, which reads call SITES and
# not behaviour. That is the shape it has to be: the rule has been lost in every wave that
# touched it, at more sites than any one fixture reached, and one of those sites sat behind a
# CLEAN fixture for two waves because only a dirty tree takes the branch that reaches it.
#
# It does not cover git's HOOKS, which are the repository's OTHER program — NO_HOOKS below.
NO_DAEMON_CACHE = ("-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false")

# The repository's own hooks, suppressed on the calls this package makes into the USER's
# repository. Measured on git 2.53.0: `update-ref` runs `reference-transaction`, and `add`
# and `write-tree` run `post-index-change` — GIT_INDEX_FILE does NOT exempt those two,
# because writing the private copy is still an index write. `status` runs it as well unless
# GIT_OPTIONAL_LOCKS=0 keeps the index from being rewritten at all, which is why the
# READONLY describe calls need nothing from this.
#
# WHY ALL FIVE SITES IN `baseline.materialize` AND NOT THREE. The three `post-index-change`
# firings are plainly false: the user's hook is told their index moved when the only index
# written was `run_dir/baseline.index`. The two `update-ref` ones are the contested half —
# a ref really is created in the user's repository, so suppressing them silences a
# notification that is TRUE, which is the reviewer's argument for leaving them. The decision
# is the ref's NAMESPACE: `refs/khenrix-forge/<run-id>/base` is forge's own bookkeeping by
# name, and §9 lists it under "explicitly allowed" precisely because it is not the user's.
# Reversing that half is deleting this tuple from the two `update-ref` calls and nothing
# else.
#
# The `-c` FLAG form, never `config --local core.hooksPath`: the config write is what
# `verify._hooks_pin` does to a tree forge BUILT, and the same thing done here would mutate
# the user's `.git/config` — one of §9's seven protected items, and the one `gate.must_show`
# says `drift` cannot speak for.
#
# The flag form is also the only form that WORKS on a builder seat, which is the other tree
# this package runs git in. `-c` enters at command-line precedence, above the local file the
# agent owns; a `config --local` pin there is undone by the agent's next `git config --unset`
# (measured, git 2.53: the same `update-ref` fires the seat's hook one command after the pin).
# `fleet` therefore writes no pin at all and says why.
#
# No engine call into a seat is hook-firing TODAY — `diff` and `apply --numstat` are not,
# measured in both config scopes with and without READONLY — so this tuple appears at no seat
# call site. `test_every_hook_firing_call_carries_the_hooks_pin` is what puts it on the first
# one that is.
NO_HOOKS = ("-c", f"core.hooksPath={os.devnull}")

# The repository's THIRD program, and the one neither preset above reaches: a DIFF DRIVER.
# `diff.external` names a command for every path; `diff.<d>.command` and `diff.<d>.textconv`
# name one for the paths an in-tree `.gitattributes` marks `diff=<d>` — and both halves of
# that pair are the seat's to write. Measured on git 2.53.0, on this package's own invocation
# (`diff --binary <B> -- :(literal)…` under READONLY and NO_DAEMON_CACHE) in a builder seat:
# all three ran the seat's program, and `core.hooksPath=/dev/null` stopped none of them.
#
# LOAD-BEARING FOR THE PATCH, not only for the engine. Same measurement, reading the bytes:
# textconv yielded the converted text and `diff.<d>.command` the driver's own output, where an
# unrigged seat yields the real patch — and `diff.external` yielded ZERO BYTES AT EXIT 0, the
# silent empty candidate `--binary` and `:(literal)` are already spent preventing. With the
# flags the bytes are byte-identical to the unrigged seat's, so this cannot cost a legitimate
# patch anything.
#
# WHAT IT DOES NOT CLOSE, and no argv can: `filter.<d>.clean`. A diff against the WORKING TREE
# must convert worktree content into git's form, so a seat naming a clean filter has that
# program run too (measured). `git diff` has no flag to refuse it and the driver name is the
# seat's to choose, so `-c filter.<d>.clean=` cannot be spelled in advance.
#
# A SUBCOMMAND OPTION, unlike the two above: it is splatted AFTER `"diff"`, because a diff
# option in front of the subcommand is an error.
NO_DIFF_DRIVERS = ("--no-ext-diff", "--no-textconv")

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
