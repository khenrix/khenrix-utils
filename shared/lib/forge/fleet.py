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

WHAT THE CLONE DOES WITH THE CONFIG IS MOVE IT, NOT REMOVE IT. The seat's `.git/config` is
no longer the user's — and it is the AGENT's, for the whole run. The keys in it that name a
PROGRAM (`core.fsmonitor`, `core.hooksPath`, `diff.external`, `diff.<d>.command`,
`diff.<d>.textconv`, `filter.<d>.clean`) are read by whichever git the ENGINE later runs in
that tree, which is at harvest, after the agent's own process has exited. `clone_seat`
deliberately writes no pin against them, and the reason is a difference between two trees that
looks like a difference of care: `verify._hooks_pin` can promise something because a verifier
has exactly one writer between the pin and `_assert_hooks_pinned`, whereas a seat's agent
writes continuously and undoes a `config --local` pin with one command (measured, git 2.53 —
the hook fires again on the very next call). A pin here would look like that defence and make
none of its claim.

What defends the engine is what the engine passes on its OWN calls, held to every call site by
the closures in `tests/test_forge_seams.py`. Two mechanisms, not one, and the difference is
worth knowing before trusting either: `NO_DAEMON_CACHE` and `NO_HOOKS` are `-c` presets that
win by PRECEDENCE, entering above the local file the agent owns; `NO_DIFF_DRIVERS` is a pair
of subcommand options that turn the feature OFF, which is why it can be splatted only after
the subcommand and why no config the seat writes reaches around it.

THE LIST ABOVE IS NOT ALL COVERED. `filter.<d>.clean` runs on the harvest diff and git offers
no flag that refuses it, so the seat's program runs and the candidate carries its output
(`harvest.artifact_set` records the cost at the call site). Prevention is not available; what
is, and is not built, is DETECTION of the same shape `inspect._filtered_paths` already uses
against the user's repository — `check-attr -z filter` over the harvested paths, which names a
rigged seat rather than reading a value back.

The empty `--template=` below decides the seat's STARTING state — measured, under it no hook
fires during `clone_seat` itself — and binds nothing the agent does afterwards. A caller that
supplies its own `template_dir` gets hooks that DO fire: measured, the `clone` and the
`checkout -b` below each ran `post-checkout`, `post-index-change` and `reference-transaction`
out of it. So all four calls the hooks closure reads as firing — `clone`, both `remote` forms
and `checkout -b` — carry `NO_HOOKS` instead of resting on the template being empty. The
`remote` pair fires nothing today, and for a reason outside those calls rather than a property
of them: a `--revision=` clone is written no fetch refspec, so it has no tracking ref to delete.
"""
import hashlib
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import gitcmd


class FleetError(RuntimeError):
    """A seat could not be built into the state the threat model requires."""


class ForgeEnvError(FleetError):
    """The environment handed to a seat cannot be built."""


class SeatError(FleetError):
    """The checkout does not match the baseline it was supposed to materialize."""


@dataclass(frozen=True)
class Seat:
    """A built seat, plus the record of what was replayed into it (spec §4 item 4).

    `replayed` exists because the replay is a silent no-op when it fails: nothing in the
    clone's own state distinguishes "the source had no ignore semantics" from "we looked
    in the wrong place", and both leave a seat that clones cleanly and checks out B1. The
    record is what lets a caller — or a test — tell those apart.

    `branch` and `verified` both default to the state a seat CANNOT be launched in — no
    branch, nothing proved — so a `Seat` built by any route other than `clone_seat` makes
    the weaker claim rather than inheriting a readiness it never established.

    `verified` IS ONLY AS STRONG AS THE BASELINE IT WAS CHECKED AGAINST. `clone_seat` asserts
    HEAD and then walks `Baseline.filesystem_manifest` path by path, so a `Baseline` carrying
    an EMPTY manifest makes the second half vacuous and still answers True — no exception, no
    trace.

    THE GUARD IS ON THE READ PATH AND NOT THE WRITE PATH, which this used to state the other
    way round. `baseline.materialize` DOES produce an empty one: it inventories the tracked
    tree and the selected untracked paths, so a repository with nothing in either leaves the
    dict `{}`, and `_record_filesystem_manifest` writes that to disk — the case
    `read_filesystem_manifest` names among the routes to `{}` there. What is closed is the
    way back IN: that function refuses an empty manifest, so `baseline.restore` — which reads
    through it rather than defaulting the field — cannot build a `Baseline` carrying one, and
    a run rebuilding B from its run directory, the caller that would otherwise hand this an
    empty dict without noticing, goes through exactly that. A caller holding `materialize`'s
    return value directly does not, and neither does one building a `Baseline` by hand, since
    the field still defaults to `{}`. So `verified` is a claim about the manifest this was
    handed and never about the repository behind it.
    """
    path: Path
    replayed: tuple = field(default_factory=tuple)
    branch: str = ""
    verified: bool = False


def _sha256_file(p: Path) -> str:
    """Mirrors `baseline._sha256_file`. The two must agree byte-for-byte on how a file is
    digested, since this compares its output against that one's manifest."""
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_link(p: Path) -> str:
    """Mirrors `baseline._sha256_link` and `snapshot._symlink_entry` for the same reason —
    surrogateescape included, since a link target is a filesystem name and a strict encode
    raises on a non-UTF-8 one."""
    return hashlib.sha256(os.readlink(p).encode("utf-8", "surrogateescape")).hexdigest()


def clone_seat(repo, baseline, dest, *, name, identity, template_dir=None) -> Seat:
    """Clone `baseline.ref` into `dest`; hand back a remote-less seat that can be launched.

    `name` is the seat's name (`claude`/`codex`/`agy`) and gives it its branch; `identity`
    is the `(author_name, author_email)` written into the clone's local config. Both are
    required rather than defaulted: a seat missing either cannot do its job — no branch
    means its work is unreachable, no identity means it cannot commit at all — and a
    default would ship that seat silently.
    """
    repo, dest = Path(repo), Path(dest)

    # Both caller errors are resolved BEFORE anything is created, in baseline.py's order
    # and for its reason: a refused seat has to leave nothing behind.
    #
    # An empty identity is REFUSED rather than written. `git config user.name ""` succeeds,
    # so the seat would clone, check out and verify clean, then fail its first commit with
    # the very error this argument exists to prevent — a failure landing on the agent
    # instead of the caller who passed it. `baseline._resolve_author` refuses the same
    # input for the same reason.
    #
    # The test is `isinstance(f, str)`, not `str(f)`: `str(None)` is the four-character
    # string "None", so `identity=(None, None)` — what an unresolved identity looks like —
    # passed this guard and died later as a raw TypeError inside `git config user.name`,
    # out of the one path whose entire purpose is a named refusal.
    if len(identity) != 2 or not all(isinstance(f, str) and f.strip() for f in identity):
        raise SeatError(
            f"cannot give seat {name!r} an identity: a name AND an email are required, "
            f"got {identity!r}. Refusing to write an empty one — the seat would look "
            "healthy and then be unable to commit.")
    run_id = baseline.ref.split("/")[2]     # refs/khenrix-forge/<run-id>/base
    branch = f"forge/{run_id}/{name}"
    # Asked of git rather than pattern-matched here, and asked BEFORE the clone: a name
    # that cannot be a ref otherwise surfaces as a raw GitError from `checkout -b` after
    # the whole clone has been paid for, which is neither this module's documented failure
    # type nor a message naming the argument at fault.
    if gitcmd.git(repo, "check-ref-format", f"refs/heads/{branch}",
                  env_extra=gitcmd.READONLY, check=False).returncode != 0:
        raise SeatError(
            f"seat name {name!r} does not form a legal branch: {branch!r}")

    dest.parent.mkdir(parents=True, exist_ok=True)
    # Only what THIS call creates may be cleaned up on the way out (below). A caller that
    # points two seats at one path, or names a directory that already holds something, must
    # not have it deleted by a refusal.
    dest_preexisted = dest.exists()

    # An EMPTY template dir, so nothing a template carries — hooks above all — is installed
    # into the seat. Both machine-wide vectors are now closed BEFORE this flag, in two
    # different places, because they are two different mechanisms: `init.templateDir` is
    # config and dies on gitcmd's /dev/null pin, while GIT_TEMPLATE_DIR is environment,
    # which that pin does not reach — it is dropped by being one of `gitcmd.HOSTILE_ENV`.
    # Neither defence subsumes the other: an empty template does NOT neutralise a global
    # core.hooksPath or url.*.insteadOf.
    # `--template=` therefore stays as the latch that still holds if either is lost, and it
    # is not idle even so: it also excludes git's COMPILED-IN default template, so the seat
    # gets no `.git/hooks` directory at all (measured, git 2.53 — 14 sample hooks with the
    # flag deleted, no hooks directory with it).
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
        # KEEP --no-hardlinks even though deleting it breaks no test: git scopes it to the
        # local transport, which --no-local already disables, so it is an equivalent mutant
        # NO test can pin. It is the second latch — the thing that still holds if --no-local
        # is ever lost — not dead weight.
        # No --no-checkout: the seat needs the working tree, and --revision already pins the
        # checkout to B1 (HEAD detached at the ref, tree populated).
        #
        # NO_HOOKS reaches what the empty template below cannot: a CALLER-supplied
        # `template_dir` carrying hooks, which `git clone` installs into the destination and
        # then runs — measured, `post-checkout`, `post-index-change` and `reference-transaction`
        # out of the new repository, before any config write here could exist to stop them. It
        # is the source repository this runs in, so the user's hooks were never the risk.
        # WHAT IT DOES NOT DO is disinfect the seat: the template's hooks are still installed at
        # `.git/hooks` and still run for the agent's own commits (measured). The flag covers the
        # ENGINE's process, which is the only party this module can speak for.
        gitcmd.git(repo, *gitcmd.NO_HOOKS, "clone", "--no-local", "--no-hardlinks", "--no-tags",
                   f"--template={tmpl}", f"--revision={baseline.ref}",
                   str(repo), str(dest), env_extra=env, timeout=600)

        # Close the push vector the clone just opened. Do this BEFORE any setup or agent
        # runs. Tolerate a non-zero exit (a future git that clones without a remote) but
        # never a surviving remote: the property is "no push target exists", so it is
        # asserted, not assumed from an exit code.
        # NO_HOOKS because `remote remove` deletes the remote-tracking refs named by
        # `remote.<n>.fetch`, and a ref deletion runs `reference-transaction`. Nothing fires
        # here today for a reason outside this call: a `--revision=` clone is written no fetch
        # refspec at all (measured — `remote.origin.url` and `remote.origin.tagopt` only), so
        # there is nothing to delete. That is a property of the clone above, which is exactly
        # the kind of premise the closure exists so this call does not rest on.
        gitcmd.git(dest, *gitcmd.NO_HOOKS, "remote", "remove", "origin",
                   env_extra=env, check=False)
        remaining = gitcmd.git(dest, *gitcmd.NO_HOOKS, "remote", env_extra=env).stdout.split()
        if remaining:
            raise FleetError(
                f"seat {dest} still has remotes {remaining}: it ships a working push target "
                "aimed at the user's repository")

        # Ignore semantics that live in the source repo but are NOT cloned.
        #
        # ASK git where they are; never string-join `.git`. In a linked worktree
        # `<repo>/.git` is a FILE, so the joined path simply does not exist, `is_file()`
        # answers False without raising, and the seat silently gets no excludes at all —
        # the same trap baseline.py's alternate index already learned to avoid.
        # `--git-common-dir`, NOT `--absolute-git-dir`: those differ for a linked worktree
        # (per-worktree dir vs the shared one) and `info/exclude` lives in the COMMON dir.
        # Its output is relative to git's cwd, which `-C repo` makes `repo`; joining onto
        # `repo` is a no-op for the absolute form and correct for the relative one.
        common = repo / gitcmd.git(repo, "rev-parse", "--git-common-dir",
                                   env_extra=gitcmd.READONLY).stdout.strip()
        replayed = []
        src_exclude = common / "info" / "exclude"
        if src_exclude.is_file():
            dst_exclude = dest / ".git" / "info" / "exclude"
            dst_exclude.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_exclude, dst_exclude)
            replayed.append("info/exclude")

        # §4 item 6: the seat gets only its own branch. `--revision=` leaves a DETACHED
        # HEAD — measured on git 2.53, `rev-parse --abbrev-ref HEAD` answers the literal
        # string `HEAD` — so without this the seat's work becomes unreachable the moment
        # anything else moves.
        #
        # Both presets, because this call writes the index and moves HEAD: measured, it ran
        # `core.fsmonitor` and fired `post-checkout`, `post-index-change` and
        # `reference-transaction`, from `.git/hooks` and from a `core.hooksPath` directory
        # alike. The only way a seat holds any of those this early is a caller-supplied
        # `template_dir` — the agent has not started, and `git clone` copies no config — so
        # the flags close the second half of a door whose first half is the clone's own.
        gitcmd.git(dest, *gitcmd.NO_DAEMON_CACHE, *gitcmd.NO_HOOKS,
                   "checkout", "-q", "-b", branch, env_extra=env)

        # §4 item 2: identity, written into the clone's LOCAL config. Without it a seat
        # cannot commit at all: global config is disabled by design (§4.2), so git fails
        # with "Please tell me who you are". Resolving the identity is the ORCHESTRATOR's
        # job for the same reason baseline.py refuses to guess an author — a fabricated
        # name is a permanent false attribution — so it arrives as an argument.
        gitcmd.git(dest, "config", "user.name", identity[0], env_extra=env)
        gitcmd.git(dest, "config", "user.email", identity[1], env_extra=env)

        # §4 item 1 + §1: the TRUSTED PARENT recomputes readiness from primary evidence.
        # The same assertions in the suite instead would be a readiness check on the wrong
        # side of the boundary: no production caller runs the suite, so they would say
        # nothing about the seat this function hands back. `Seat.verified` is worth reading
        # only because the check is HERE.
        head = gitcmd.git(dest, "rev-parse", "HEAD", env_extra=env).stdout.strip()
        if head != baseline.commit:
            raise SeatError(f"seat checked out {head[:12]}, expected {baseline.commit[:12]}")
        for rel, want in (baseline.filesystem_manifest or {}).items():
            p = dest / rel
            # A symlink is checked by its TARGET TEXT, never hashed through: `_sha256_file`
            # opens THROUGH it, so hashing one describes content from outside the tree the
            # manifest claims to describe. The branch used to `continue`, because the
            # manifest held the target's content and nothing this side could reproduce it
            # without following the link; since Plan D's D-1 the manifest holds the target
            # text, so the entry is now VERIFIED rather than excused. The link test also has
            # to come before the absence check below: a correct seat can carry a link that
            # dangles from the seat's own depth, and `is_file()` answers False for one.
            if p.is_symlink():
                if _sha256_link(p) != want:
                    raise SeatError(f"seat symlink points elsewhere than the baseline: {rel}")
                continue
            # Absence is NOT skipped. A seat missing a path B1 contains is exactly the
            # transport failure this assertion exists to catch, and skipping it would
            # report `verified is True` for a seat that never received the file.
            if not p.is_file():
                raise SeatError(f"seat is missing a path the baseline manifest lists: {rel}")
            if _sha256_file(p) != want:
                raise SeatError(f"seat content differs from the baseline manifest: {rel}")
        verified = True
    except Exception:
        # The same argument the `finally` makes for the template dir, for the seat itself:
        # a refused seat must not leave a populated clone at `dest`. This function now has
        # five refusal paths reaching here, and `git clone` declines a non-empty target, so
        # the leftovers would break the retry as well as outliving the failure.
        if not dest_preexisted:
            shutil.rmtree(dest, ignore_errors=True)
        raise
    finally:
        # Also on the failure paths: a refused seat that leaves a populated template dir
        # behind hands the NEXT seat at this path whatever was in it.
        if engine_owned:
            shutil.rmtree(tmpl, ignore_errors=True)
    return Seat(path=dest, replayed=tuple(replayed), branch=branch, verified=verified)


# Variables whose value is an os.pathsep-separated LIST of paths. This is a SHAPE list,
# not the policy name-list the spec rejects: membership decides how a value is PARSED,
# never whether it is scrubbed — the scrub decision is always the same predicate. Shape is
# declared rather than sniffed because "contains a colon" would shred LS_COLORS,
# SSH_AUTH_SOCK and any postgres://user:pass@host. A colon-separated variable that is NOT
# listed here keeps the wholesale drop, which is the fail-closed direction.
#
# LD_PRELOAD is deliberately ABSENT: ld.so accepts spaces as well as colons, so an
# os.pathsep-only split would leave a space-delimited repo-internal .so in place whenever
# it is not the first entry. Unlisted, it takes the whole-value test below, which reads
# both separators.
_PATH_SHAPED = frozenset({
    "PATH", "PYTHONPATH", "LD_LIBRARY_PATH", "MANPATH", "INFOPATH",
    "PKG_CONFIG_PATH", "CMAKE_PREFIX_PATH", "CLASSPATH", "NODE_PATH", "GOPATH",
    "PERL5LIB", "RUBYLIB", "XDG_DATA_DIRS", "XDG_CONFIG_DIRS",
})

# What may follow the checkout path for it to be a real reference rather than a longer,
# unrelated name: a separator, a delimiter, or end-of-value. `<root>-scratch` is a
# different directory; `<root>/include` and a bare `<root>` are not.
_RIGHT_BOUNDARY = r"(?=$|[/\s:;,'\"])"


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

    Everything else is matched as a SUBSTRING with a right boundary, because the checkout
    routinely appears inside a larger token rather than as the whole value —
    `CFLAGS=-I<repo>/include`, `RUSTFLAGS=-L native=<repo>/target`,
    `NODE_OPTIONS=--require <repo>/tools/hook.js`, `PYTEST_ADDOPTS=--rootdir=<repo>`. Those
    variables are dropped whole, never rewritten. The left side is unanchored, so a backup
    at `/mnt/backup<repo>/…` matches too; that over-scrubs in the fail-closed direction and
    is confined to variables already taking the wholesale drop.
    """
    roots = _repo_roots(repo_path)
    embeds = re.compile("(?:" + "|".join(re.escape(r) for r in sorted(roots)) + ")"
                        + _RIGHT_BOUNDARY)
    out = {}
    for k, v in env.items():
        if not isinstance(v, str):
            out[k] = v
        elif k in _PATH_SHAPED:
            # Entry-wise, and never the substring test: applied to PATH it would delete the
            # whole variable again the moment one entry pointed into the checkout.
            kept = [e for e in v.split(os.pathsep) if not _inside(e, roots)]
            if kept:
                out[k] = os.pathsep.join(kept)
        # The whole-value test is `_inside` as well as the substring search, because only
        # `_inside` normalises: `embeds` matches literal text, so `/git/./repo/src`,
        # `/git/other/../repo/s` and `/git//repo/src` all name the checkout while matching
        # none of the root spellings. The path-shaped branch above got that for free from
        # its per-entry `_inside`; this branch had no normalising test at all.
        elif not (embeds.search(v) or any(_inside(e, roots) for e in v.split(os.pathsep))):
            out[k] = v
    return out


def forge_child_env(repo_path, env=None) -> dict:
    """Scrubbed environment, git's redirectors removed, plus the forge recursion guard.

    The council engine's child_env increments LLM_COUNCIL_DEPTH only; without a forge
    guard a seat that reaches for /llm-forge spawns three more write-enabled seats, each
    of which can spawn three more.

    `scrub_env` alone is the WRONG boundary for `gitcmd.HOSTILE_ENV`, and misses those names
    in the direction that matters: it drops values pointing INTO the checkout, while
    `GIT_CONFIG_COUNT`, `GIT_ALTERNATE_OBJECT_DIRECTORIES` and an ambient absolute
    `GIT_DIR` do their damage by pointing somewhere ELSE — at /tmp, at an unrelated
    repository — so every one of them looks repo-external and survives. Two consequences,
    both of which undo work done a few lines earlier: `clone_seat` spends an empty
    `--template=` keeping hooks out of the seat, and an inherited
    `GIT_CONFIG_COUNT=1 / KEY_0=core.hooksPath` re-enables them for every git the seat
    runs; and a write-enabled agent under an ambient `GIT_DIR` operates on a repository
    that is not its clone.

    So the seat gets the same treatment `gitcmd.git` gives an engine call, in the same
    order: strip HOSTILE_ENV, then pin config discovery to /dev/null. Pinning LAST
    matters — `GIT_CONFIG_GLOBAL` is not in that list precisely because removing it would
    RESTORE ~/.gitconfig, so it is set rather than dropped. The list is read from `gitcmd`
    rather than restated so that a seat and an engine call cannot drift apart, and the drift
    is not hypothetical: while `GIT_CONFIG_PARAMETERS` and `GIT_TEMPLATE_DIR` sat outside
    that list, every seat inherited both — the first of them even after a strip written for
    the gate alone had closed it there.
    """
    base = dict(env if env is not None else os.environ)
    for k in gitcmd.HOSTILE_ENV:
        base.pop(k, None)
    out = scrub_env(base, repo_path)
    out.update(gitcmd.NO_USER_CONFIG)
    # A bare ValueError out of `int()` is indistinguishable from any other ValueError raised
    # beneath a function that otherwise only ever returns a dict, so the guard's own failure
    # is not something a caller can catch on purpose.
    raw = out.get("LLM_FORGE_DEPTH", "0") or "0"
    try:
        cur = int(raw)
    except ValueError as e:
        raise ForgeEnvError(
            f"ambient LLM_FORGE_DEPTH is not a number: {raw!r}") from e
    out["LLM_FORGE_DEPTH"] = str(cur + 1)
    return out
