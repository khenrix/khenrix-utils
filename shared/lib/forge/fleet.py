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
    tmpl = Path(template_dir) if template_dir else (dest.parent / f".{dest.name}.tmpl")
    if template_dir is None:
        # A crashed earlier run can leave this path populated; "empty" has to be verified,
        # not assumed, or the defence installs whatever that run left behind.
        shutil.rmtree(tmpl, ignore_errors=True)
    tmpl.mkdir(parents=True, exist_ok=True)
    env = {**gitcmd.NO_USER_CONFIG, "GIT_OPTIONAL_LOCKS": "0"}

    # No --no-checkout: the seat needs the working tree, and --revision already pins the
    # checkout to B1 (measured on git 2.53 — HEAD detached at the ref, tree populated).
    gitcmd.git(repo, "clone", "--no-local", "--no-hardlinks", "--no-tags",
               f"--template={tmpl}", f"--revision={baseline.ref}",
               str(repo), str(dest), env_extra=env, timeout=600)

    # Close the push vector the clone just opened. Do this BEFORE any setup or agent runs.
    # Tolerate a non-zero exit (a future git that clones without a remote) but never a
    # surviving remote: the property is "no push target exists", so it is asserted, not
    # assumed from an exit code.
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

    if not template_dir:
        shutil.rmtree(tmpl, ignore_errors=True)
    return dest


def scrub_env(env: dict, repo_path) -> dict:
    """Drop variables whose VALUE resolves into the original checkout.

    By predicate, not by name-list. "Sanitize PATH" is exactly wrong on a shim-based
    machine: uvx and friends reach PATH via mise shims outside the repo, and a blanket
    scrub kills the toolchain, failing every candidate for an infrastructure reason
    (spec §4).
    """
    root = str(Path(repo_path).resolve())
    return {k: v for k, v in env.items()
            if not (isinstance(v, str) and root in v)}


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
