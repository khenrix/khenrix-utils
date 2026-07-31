"""Independent seat clones (spec §4).

Linked worktrees share the parent's .git — refs, objects, config, hooks — so a
permission-bypassed agent can rewrite the user's branches or push, without leaving its cwd.
Seats therefore get real clones. Two details do the actual work:

  * the clone is made from the BASELINE REF, not from HEAD. `--single-branch` follows the
    source's HEAD and would hand every seat a tree without the user's uncommitted work.
  * `origin` is REMOVED. `git clone` always writes remote.origin.url, so a clone that
    merely exists still ships a working push target aimed at the user's repository;
    receive.denyCurrentBranch blocks only the checked-out branch.

Never `--local`/hardlinks: against git's own operations hardlinked objects are safe
(content-addressed, mode 444), but forge's whole premise is a process that may write
outside git's rules, and a truncate through a shared inode corrupts the user's repository.
"""
import os
import shutil
from pathlib import Path

from . import gitcmd


class FleetError(RuntimeError):
    """A seat could not be built into the state the threat model requires."""


def clone_seat(repo, baseline, dest, *, template_dir=None) -> Path:
    """Clone `baseline.ref` into `dest` and hand back a checked-out, remote-less seat."""
    repo, dest = Path(repo), Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    # An EMPTY template dir, so an ambient template cannot install hooks into the seat.
    # GIT_TEMPLATE_DIR is the live vector — it is environment, not config, so gitcmd's
    # /dev/null pin does not reach it — and `--template=` overrides it. The pin is what
    # closes the config forms, since an empty template does NOT neutralise a global
    # core.hooksPath or url.*.insteadOf.
    # One predicate for "the engine owns this directory": under `template_dir=""` a
    # `is None` test would take the default path without pre-cleaning it, silently
    # disabling the defence below.
    engine_owned = not template_dir
    tmpl = Path(template_dir) if template_dir else (dest.parent / f".{dest.name}.tmpl")
    if engine_owned:
        # A crashed earlier run can leave this path populated; "empty" has to be verified,
        # not assumed, or the defence installs whatever that run left behind.
        shutil.rmtree(tmpl, ignore_errors=True)
    tmpl.mkdir(parents=True, exist_ok=True)
    env = {**gitcmd.NO_USER_CONFIG, "GIT_OPTIONAL_LOCKS": "0"}

    try:
        # --no-local is load-bearing independently of --no-hardlinks: the local transport
        # copies the WHOLE object store, so the seat would also receive objects the
        # baseline ref does not reach — other branches, stashes, dangling history — none of
        # which the pre-launch secret screen looked at. --no-hardlinks alone still copies
        # them (measured on git 2.53).
        # No --no-checkout: the seat needs the working tree, and --revision already pins the
        # checkout to B1 (HEAD detached at the ref, tree populated).
        gitcmd.git(repo, "clone", "--no-local", "--no-hardlinks", "--no-tags",
                   f"--template={tmpl}", f"--revision={baseline.ref}",
                   str(repo), str(dest), env_extra=env, timeout=600)

        # Close the push vector the clone just opened. Do this BEFORE any setup or agent
        # runs. Tolerate a non-zero exit (a future git that clones without a remote) but
        # never a surviving remote: the property is "no push target exists", so it is
        # asserted, not assumed from an exit code.
        gitcmd.git(dest, "remote", "remove", "origin", env_extra=env, check=False)
        remaining = gitcmd.git(dest, "remote", env_extra=env).stdout.split()
        if remaining:
            raise FleetError(
                f"seat {dest} still has remotes {remaining}: it ships a working push target "
                "aimed at the user's repository")

        # Ignore semantics that live in the source repo but are NOT cloned.
        src_exclude = repo / ".git" / "info" / "exclude"
        if src_exclude.is_file():
            dst_exclude = dest / ".git" / "info" / "exclude"
            dst_exclude.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_exclude, dst_exclude)
    finally:
        # Also on the failure paths: a refused seat that leaves a populated template dir
        # behind hands the NEXT seat at this path whatever was in it.
        if engine_owned:
            shutil.rmtree(tmpl, ignore_errors=True)
    return dest


# Variables whose value is an os.pathsep-separated LIST of paths. This is a SHAPE list,
# not the policy name-list the spec rejects: membership decides how a value is PARSED,
# never whether it is scrubbed — the scrub decision is always the same predicate. Shape is
# declared rather than sniffed because "contains a colon" would shred LS_COLORS,
# SSH_AUTH_SOCK and any postgres://user:pass@host. A colon-separated variable that is NOT
# listed here keeps the wholesale drop, which is the fail-closed direction.
_PATH_SHAPED = frozenset({
    "PATH", "PYTHONPATH", "LD_LIBRARY_PATH", "LD_PRELOAD", "MANPATH", "INFOPATH",
    "PKG_CONFIG_PATH", "CMAKE_PREFIX_PATH", "CLASSPATH", "NODE_PATH", "GOPATH",
    "PERL5LIB", "RUBYLIB", "XDG_DATA_DIRS", "XDG_CONFIG_DIRS",
})


def _repo_roots(repo_path) -> set:
    """The checkout's path in both the lexical and the symlink-resolved form.

    A value may name either — `resolve()` alone matches only the resolved form, so a
    symlinked checkout referred to by its symlink path would slip through.
    """
    p = os.fspath(repo_path)
    return {os.path.normpath(os.path.abspath(p)), os.path.realpath(p)}


def _inside(value: str, roots: set) -> bool:
    """True when `value` names a path at or beneath one of `roots`.

    Lexical, and only for values that are already ABSOLUTE. `os.path.realpath` is never
    applied to the value: it resolves a relative string against the CWD, so with the
    process sitting in the repo it turns `EDITOR=vim` into `<repo>/vim` and classifies
    every non-path scalar as repo-internal.

    The boundary test is `== root` or `root + os.sep`, not a substring: a sibling checkout
    at `<root>-scratch` is not inside `<root>`.
    """
    if not os.path.isabs(value):
        return False
    n = os.path.normpath(value)
    return any(n == r or n.startswith(r + os.sep) for r in roots)


def scrub_env(env: dict, repo_path) -> dict:
    """Drop what points into the original checkout — by entry for path-shaped variables.

    By predicate, not by name-list. "Sanitize PATH" is exactly wrong on a shim-based
    machine: uvx and friends reach PATH via mise shims outside the repo, and a blanket
    scrub kills the toolchain, failing every candidate for an infrastructure reason
    (spec §4). Deleting a whole PATH because ONE of its entries is repo-internal is the
    same failure by a different route, and one entry is the normal case — a plugin `bin`
    directory, a `.venv/bin`, a direnv-exported tool dir. So a path-shaped variable keeps
    the entries that survive, and is dropped only when NO entry does.

    A partially scrubbed PATH is a WEAKER claim than a deleted one, and deliberately so:
    a surviving `~/.local/bin/foo` may symlink into `<repo>/.venv/bin/foo`, and mise/asdf
    shims resolve versions through their own config rather than PATH. That residual reach
    is ACCEPTED here and delegated to §5 calibration and the §9 tripwire. This scrub stops
    accidental reuse of the user's checkout; it was never a containment boundary.
    """
    roots = _repo_roots(repo_path)
    out = {}
    for k, v in env.items():
        if not isinstance(v, str):
            out[k] = v
        elif k in _PATH_SHAPED:
            kept = [e for e in v.split(os.pathsep) if not _inside(e, roots)]
            if kept:
                out[k] = os.pathsep.join(kept)
        elif not any(_inside(e, roots) for e in v.split(os.pathsep)):
            # Unlisted variables are still segmented for the TEST — an embedded repo path
            # must be caught — but the whole variable is dropped, never rewritten.
            out[k] = v
    return out


def forge_child_env(repo_path, env=None) -> dict:
    """Scrubbed environment plus the forge recursion guard.

    The council engine's child_env increments LLM_COUNCIL_DEPTH only; without a forge
    guard a seat that reaches for /llm-forge spawns three more write-enabled seats, each
    of which can spawn three more.
    """
    base = dict(env if env is not None else os.environ)
    out = scrub_env(base, repo_path)
    cur = int(out.get("LLM_FORGE_DEPTH", "0") or "0")
    out["LLM_FORGE_DEPTH"] = str(cur + 1)
    return out
