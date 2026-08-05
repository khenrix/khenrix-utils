"""§15 — cleanup, and the disk accounting that makes it decidable.

`storage.py` has referred to "the `--gc` walk" as an existing contract since it was written,
and the §5 gate renders an operator-facing sentence saying it is mandatory. This is it. The
naming scheme it walks is `storage.run_root`'s, which exists so that "a resume and a `--gc`
walk look for the same names".

WHAT THIS WILL NOT DO. §9 forbids a repo-wide `git worktree prune`, so nothing here prunes:
the worktrees this removes are the ones it can name from a run directory it read. And §15
refuses to delete a synthesis worktree or branch not marked handed over, which is a refusal on
a MISSING record rather than on a negative one — `handover.read_handover` answers `None` for
"not handed over" and raises for everything else, so an unreadable record reaches the operator
instead of quietly becoming a refusal they cannot explain.

EVERY DELETION IS INSIDE STORAGE THIS ENGINE OWNS, AND OWNERSHIP IS ESTABLISHED RATHER THAN
ASSUMED. A run directory that is a symbolic link, or that resolves outside
`storage.forge_root()`, is refused: `rmtree` on a link raises, and a walk that followed one
would report another tree's bytes as this run's and then delete through it. §4 does not admit
trading source-object safety for space, and a path this walk cannot place inside forge's own
storage is a path it declines to touch rather than one it removes carefully.

THE PATHS GIT REPORTS ARE RESOLVED AND THE PATHS THIS ENGINE BUILDS ARE NOT. Measured on git
2.53.0: `worktree add` through a symlinked parent records the REAL path, and `worktree list
--porcelain` prints that — while `worktree remove` accepts either spelling. So a string
comparison against `run_dir / "synthesis"` reads "git does not have this registered" for every
ordinary run on a machine whose `~/.local/state` (or home) goes through a link, which is the
one refusal below that has no remedy. Both sides are resolved before they are compared, and
the synthesis path is refused outright if it is ITSELF a link, since resolving that one would
match a registered worktree somewhere else and hand `worktree remove` the operator's own tree.
"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import gitcmd, handover as handovermod, runstate, storage

# What may become a directory name under forge's own state root. `new_run_id` draws six hex
# characters, so this is wider than any id this engine issues and narrow enough that no id can
# name a directory outside the one it is joined into. Dots are admitted INSIDE the name and
# not at the front, which is what keeps `.` and `..` out without forbidding `r1.2`.
#
# REFERENCED AND NEVER RESPELLED, on the same argument this file already makes for
# `_FORGE_REF_PREFIXES` one paragraph down: the module that decides a value owns the strings
# that spell it. `storage.run_root` interpolates the id and creates the directory, so the rule
# is its to state — and while there were two copies, an id this file admitted and `storage`
# did not passed validation here and then raised from there, with a refusal naming a rule
# gc's own text contradicts.
_RUN_ID = storage._RUN_ID

# §9's two explicitly-allowed ref namespaces, REFERENCED AND NEVER RESPELLED, on `handover`'s
# precedent for `seat._FORGE`: the module that decides a value owns the strings that spell it.
# A copy here is a second place to be right about which refs are forge's own — and the copy
# fails in the direction that deletes, because a prefix this file admitted and `runstate` did
# not would be a namespace nothing declared protected and this walk removed anyway.
_FORGE_REF_PREFIXES = runstate._FORGE_REF_PREFIXES


class GcError(RuntimeError):
    """A run this walk will not delete."""


@dataclass(frozen=True)
class Usage:
    """One past run's footprint. §15: "Report total disk held by past runs."

    `bytes_` AND `files` ARE NULLABLE AND A NULL IS NOT A ZERO. A run directory this walk could
    not read is not a run holding nothing — and a total that silently omits a 40 GB run is this
    report inverted. `why` carries every reason the row is incomplete, joined, because a walk
    failure and an unreadable handover record are two independent facts about one run.

    `handed_over` IS NULLABLE FOR THE SAME REASON ONE FIELD OVER. `True` and `False` are
    answers `read_handover` gave; `None` is the state where it raised and there is no answer.
    Reporting an unreadable record as `False` prints "NOT handed over" about a delivery the
    operator may well have made, which is the one sentence that would make them delete it.
    """
    run_id: str
    path: str
    bytes_: int | None
    files: int | None
    handed_over: bool | None
    why: str = ""


def _real(path) -> str:
    """A path with every symlink in it resolved, for comparing against git's own answer.

    `os.path.realpath` rather than `Path.resolve()` only so the result is a string on both
    sides of every comparison below; they resolve the same way.
    """
    return os.path.realpath(str(path))


def _raise(error: OSError):
    """`os.walk`'s `onerror`, which defaults to SWALLOWING the error and yielding nothing for
    that subtree. A run whose `seats/` directory could not be listed would otherwise be
    measured as the sum of everything else — a clean number over content nobody read, which is
    the same defect `screen._walk` records having been measured with."""
    raise error


def _walk_bytes(root: Path) -> tuple[int, int]:
    """(bytes, files) under `root`, following no symlink. Raises `OSError` — the caller decides
    what an unreadable run means, because only the caller knows whether it is reporting or
    deleting.

    A SYMLINKED SUBDIRECTORY IS NOT DESCENDED INTO AND ITS TARGET IS NOT COUNTED, which is not
    an omission: those bytes are held by whatever the link points at and `rmtree` will unlink
    the link rather than the tree, so counting them here would report disk this run does not
    hold and that removing it would not reclaim.
    """
    total = count = 0
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False, onerror=_raise):
        for f in filenames:
            st = (Path(dirpath) / f).lstat()
            total += st.st_size
            count += 1
    return total, count


def usage(repo) -> tuple[Usage, ...]:
    """§15's disk report, one row per past run plus whatever could not be measured.

    THE ENUMERATION IS `storage.run_dirs`'s AND NOT A SECOND COPY OF IT. The naming scheme —
    the state root, this engine's own subdirectory, and `<digest of the resolved repo>-<run
    id>` — has exactly one home, because two spellings of one predicate will eventually
    disagree and this one fails SILENTLY: if `run_root` moves and this walk does not, `usage()`
    returns `()` and `--gc all` prints "no forge runs are on disk for this repository" over a
    disk holding all of them. Nothing in this module spells any part of that path, and
    `test_gc_asks_storage_where_a_run_lives_rather_than_deriving_it` reads the source to say so
    — which is why the pieces are described here rather than written out.
    """
    try:
        dirs = storage.run_dirs(repo)
    except OSError as e:
        # NOT `()`. An empty tuple is "no run has ever been opened for this repository", which
        # `--gc all` prints as a reassurance; a state directory that exists and cannot be
        # listed is this walk failing, and the two must not print alike.
        raise GcError(
            f"the forge state directory could not be listed ({e}), so this walk cannot say "
            "what is on disk for this repository. That is not the same answer as there being "
            "no runs, and it is not summed as zero.") from e
    rows, skip = [], len(storage.run_digest(repo)) + 1
    for d in dirs:
        run_id = d.name[skip:]
        if d.is_symlink():
            # NEITHER FIELD IS ANSWERED, and that is the point rather than an omission. Every
            # question this row asks would be answered ABOUT THE TARGET: `os.walk` follows the
            # top of the tree it is given whatever `followlinks` says, and `read_handover`
            # would open the record of whatever run the link points at and report it as this
            # one's. `collect` refuses the same path for the same reason one step on.
            rows.append(Usage(
                run_id, str(d), None, None, None,
                f"{d} is a symbolic link rather than a run directory, so nothing here is "
                "measured: its size and its handover record are both facts about whatever it "
                "points at, and `--gc` will not delete through it"))
            continue
        why = []
        try:
            handed = handovermod.read_handover(d) is not None
        except (handovermod.HandoverError, OSError) as e:
            # `None`, NOT `False`. "This run was not handed over" is a licence question with a
            # real answer; "this engine could not read the record" is not an answer to it, and
            # printing the two the same way tells an operator a delivery they made is
            # unfinished work.
            handed = None
            why.append(f"its handover record could not be read ({e})")
        try:
            total, count = _walk_bytes(d)
        except OSError as e:
            # BOTH REASONS SURVIVE. The walk failing does not un-fail the handover read, and a
            # row that dropped the earlier reason here would name the size problem and silently
            # lose the record problem — one report, one cause, in a function whose whole job is
            # to name what it could not measure.
            why.insert(0, f"this run's directory could not be walked ({e}), so its size is "
                          "unknown — it is named here rather than summed as zero")
            rows.append(Usage(run_id, str(d), None, None, handed, "; ".join(why)))
            continue
        rows.append(Usage(run_id, str(d), total, count, handed, "; ".join(why)))
    return tuple(rows)


def worktrees(repo) -> tuple[dict, ...]:
    """`git worktree list --porcelain`, parsed into one dict per tree.

    Measured (git 2.53.0): this form runs no `core.fsmonitor`, fires no hook and runs no diff
    driver — but the two presets go on anyway, because the closures in
    `tests/test_forge_seams.py` read membership off the FIRST POSITIONAL WORD and `worktree
    add`/`remove` both fire. That costs this read a flag pair it does not need, which is the
    fail-closed direction and exactly what the closure's own comment says it prefers.

    A LOCK IS THE KEY'S PRESENCE, NOT ITS VALUE. Measured: a lock taken with no reason prints
    the bare word `locked`, so `cur["locked"]` is `""` and a caller testing the VALUE would
    read an ordinary lock as no lock and hand `worktree remove` a tree git refuses to remove.

    Unusual characters in a path are quoted by this format (git's own documentation says to
    use `-z` for paths containing newlines). Nothing here unquotes them: a quoted path fails
    to match the run directory this engine built, and `collect` then REFUSES rather than
    removing something it named wrongly, which is the direction that error has to fail in.
    """
    return _parse_worktrees(gitcmd.git(repo, *gitcmd.NO_DAEMON_CACHE, *gitcmd.NO_HOOKS,
                                       "worktree", "list", "--porcelain",
                                       env_extra=gitcmd.READONLY).stdout)


def _parse_worktrees(out: str) -> tuple[dict, ...]:
    """The porcelain's own shape: one block per tree, blank-line separated, `key value` lines
    with a bare key for an attribute that carries no value. Split out from the call so the
    format can be tested without a repository holding every shape at once."""
    trees, cur = [], {}
    for line in out.splitlines():
        if not line.strip():
            if cur:
                trees.append(cur)
            cur = {}
            continue
        key, _, value = line.partition(" ")
        cur[key] = value
    if cur:
        trees.append(cur)
    return tuple(trees)


def _refs_of(repo, run_id: str) -> dict:
    """Every ref in `repo` that belongs to THIS run, name to the OID it points at.

    §9 NAMES TWO NAMESPACES AND THIS WALK RECLAIMS BOTH. `refs/heads/forge/<run-id>/synthesis`
    is the deliverable branch, and `refs/khenrix-forge/<run-id>/…` holds B1's base commit and
    every seat's transported work — `transport_seat` fetches into it from the user's side, so
    it is in the USER's repository and it pins every object those seats produced. A cleanup
    that removed the tree and the branch and left those behind would report a run reclaimed
    while the bulk of what it cost stayed on the operator's disk with nothing naming it, which
    is the same reading `worktree remove` invites one namespace over.

    THE RUN ID IS PART OF THE PREFIX, so a second run's refs are outside this set by
    construction rather than by a filter someone has to keep right.
    """
    prefixes = tuple(f"{p}{run_id}/" for p in _FORGE_REF_PREFIXES)
    return {name: oid for name, oid in runstate._show_ref(repo).items()
            if name.startswith(prefixes)}


def _refuse_a_path_this_walk_does_not_own(run_dir: Path) -> None:
    """The precondition for every removal below: this directory is inside forge's own storage.

    TWO CHECKS BECAUSE THERE ARE TWO WAYS TO ESCAPE. A run directory that is a SYMLINK
    resolves to somewhere else entirely — `rmtree` raises on one, and the same link one
    directory over (`<digest>-b` pointing at `<digest>-a`) resolves back inside forge's own
    store, so the parent test alone would clear it and this walk would delete another run's
    evidence under the name of an empty one. And a resolved parent that is not
    `storage.forge_root()` is a directory this engine did not create, whatever it is called.
    """
    base = storage.forge_root()
    if run_dir.is_symlink():
        raise GcError(
            f"{run_dir} is a symbolic link, not a run directory this engine created. What it "
            "points at was not measured and will not be deleted: §4 does not admit trading "
            "the safety of objects this walk cannot account for against disk. Remove the link "
            "by hand if it is yours.")
    try:
        real, real_base = Path(_real(run_dir)), Path(_real(base))
    except OSError as e:                                    # pragma: no cover - defensive
        raise GcError(f"{run_dir} could not be resolved ({e}), so this walk cannot say "
                      "whether it is inside forge's own storage") from e
    if real.parent != real_base:
        raise GcError(
            f"{run_dir} resolves to {real}, which is not inside {real_base}. This walk deletes "
            "only what it can place in forge's own storage, and a path whose ownership it "
            "cannot establish is refused rather than removed.")


def collect(repo, run_id: str, *, force: bool = False) -> tuple[str, ...]:
    """§15's cleanup for one run. Returns what was removed, or raises rather than removing it.

    THE REFUSAL IS ON A MISSING RECORD, NOT A NEGATIVE ONE. `handover.read_handover` answers
    `None` only for a run that has no `handover.json`; every other failure raises out of this
    function and reaches the operator. A `gc` that caught that raise would refuse — which looks
    like the safe direction and is not: the operator would find a run they did hand over become
    undeletable, with nothing saying why.

    `force` IS NOT AN OVERRIDE OF THAT. It skips the not-handed-over refusal and nothing else,
    because §15's refusal exists to protect a deliverable that cannot be rebuilt and an
    operator who types `--force` has said they know that. Every other refusal below is about
    this engine not being able to describe what it would delete, and no flag clears one.

    THE ORDER IS EVERY REFUSAL FIRST, THEN EVERY REMOVAL, on `cli.collect`'s precedent. The
    removals here cannot be taken back, so a refusal reached after the worktree is gone would
    leave a half-reclaimed run whose report says only what raised.
    """
    # THE RUN ID BECOMES A PATH, AND IT ARRIVES FROM ARGV. `run_root` joins it into a
    # directory name and CREATES with `parents=True`, so a `..` or a separator in it makes
    # directories outside forge's own storage before anything below can refuse them —
    # `--gc 'x/../../../elsewhere'` creates `elsewhere` and is only then refused. The
    # ownership check catches the deletion; this catches the creation, which no later check
    # can undo.
    if not _RUN_ID.match(run_id or ""):
        raise GcError(
            f"{run_id!r} is not a run id: a run id is letters, digits, dots, hyphens and "
            "underscores, starting with a letter or a digit. It becomes a directory name "
            "under forge's own state directory, and a separator or a `..` in it names a "
            "directory somewhere else entirely.")
    run_dir = storage.run_root(repo, run_id, must_be_new=False)
    _refuse_a_path_this_walk_does_not_own(run_dir)
    if not storage.manifest_path(run_dir).exists():
        # `run_root` CREATES, so a `--gc` on an id nothing ever opened would otherwise leave
        # the directory behind for the next `--gc all` to report as a run. `rmdir` removes it
        # only while it is EMPTY, so this can never take a partial run's evidence with it —
        # §8.1's failed attempt is a file in this directory, and a directory holding one
        # refuses to be removed here and is reported below instead.
        try:
            run_dir.rmdir()
        except OSError:
            pass
        raise GcError(
            f"{run_dir} records no manifest, so this walk cannot say what run it would be "
            "deleting. §15's cleanup is per-run and a directory with no identity is not one.")
    h = handovermod.read_handover(run_dir)
    if h is None and not force:
        raise GcError(
            f"run {run_id} has no handover record, so its synthesis worktree and branch are "
            "not marked handed over and §15 refuses to delete them. A patch-based handover "
            "that will never merge is still a handover — record it with "
            "`--collect <run-id> --accept`, or pass --force if the work is genuinely "
            "abandoned.")

    synth = run_dir / handovermod.SYNTHESIS
    if synth.is_symlink():
        raise GcError(
            f"{synth} is a symbolic link. Resolving it would match whatever registered "
            "worktree it points at — the operator's own, for one — and hand that path to "
            "`git worktree remove`. This walk removes the synthesis tree a run created and "
            "nothing else, so a link in its place is refused.")
    trees = worktrees(repo)
    # BOTH SIDES NORMALIZED, AND ONLY ONE OF THEM HAS BEEN SEEN TO NEED IT. Measured: git
    # records the RESOLVED path, so `_real` over its answer is a no-op on every listing this
    # engine has produced — it is here so the comparison is between two paths reduced the same
    # way rather than between one that was and one that was not. The side that is load-bearing
    # is the lookup: `synth` is built out of a run directory this engine spelled, and on a
    # machine whose state directory goes through a link the two spellings differ.
    registered = {_real(t["worktree"]): t for t in trees if t.get("worktree")}
    entry = registered.get(_real(synth))
    if synth.exists() and entry is None:
        # A PATH THIS ENGINE CANNOT NAME IS NOT A PATH IT MAY DELETE. Falling through to the
        # `rmtree` below would remove the directory while `.git/worktrees/<name>` stays
        # registered in the user's repository — and §9 forbids the `worktree prune` that is the
        # only thing that reclaims it, so the leak is permanent and silent. `worktree list`
        # not naming a directory that exists is a state this walk does not understand, and
        # `--force` does not clear it: `force` waives the not-handed-over refusal, which is a
        # decision the operator can make, and this is not one.
        raise GcError(
            f"{synth} exists and `git worktree list` in {repo} does not name it. This walk "
            "will not delete a worktree it cannot see registered: removing the directory "
            "would leave the admin entry behind, and §9 forbids the repo-wide `worktree "
            "prune` that would reclaim it. Re-register or remove it by hand, then re-run.")

    refs = _refs_of(repo, run_id)
    # A REF ANOTHER WORKTREE HAS CHECKED OUT IS NOT THIS RUN'S TO DELETE, and git will not stop
    # this: measured on 2.53.0, `git branch -D` refuses a branch a linked worktree is on while
    # `update-ref -d` deletes it and leaves that worktree with an all-zero HEAD. The synthesis
    # tree itself is excluded because removing it below is what releases its branch.
    held = sorted(f"{t.get('branch')} is checked out at {t['worktree']}"
                  for path, t in registered.items()
                  if t.get("branch") in refs and path != _real(synth))
    if held:
        raise GcError(
            f"this run's refs are checked out by a worktree this walk did not create: {held}. "
            "`update-ref -d` would delete them anyway and leave that tree on an unborn HEAD, "
            "so the run is refused instead. Remove those worktrees by hand, then re-run.")

    removed = []
    if entry is not None:
        # UNLOCK ONLY IF LOCKED. Measured on git 2.53.0: `worktree unlock` on an unlocked tree
        # is rc=128, so §9's unlock-then-remove done unconditionally fails on the ordinary
        # case. The lock state comes from the porcelain listing rather than from a try/except,
        # because catching a 128 would also catch the ones that mean something else. The other
        # direction is measured too: `worktree remove` on a LOCKED tree is rc=128 and `--force`
        # does not clear it ("use 'remove -f -f' to override or unlock first"), so the unlock
        # is what makes the removal possible rather than a tidying step before it.
        if "locked" in entry:
            gitcmd.git(repo, *gitcmd.NO_DAEMON_CACHE, *gitcmd.NO_HOOKS,
                       "worktree", "unlock", str(synth), env_extra=gitcmd.READONLY)
            removed.append(f"unlock {synth}")
        try:
            gitcmd.git(repo, *gitcmd.NO_DAEMON_CACHE, *gitcmd.NO_HOOKS,
                       "worktree", "remove", str(synth), env_extra=gitcmd.READONLY)
        except gitcmd.GitError as e:
            # MEASURED, AND IT IS THE ORDINARY OBJECTION: `worktree remove` is rc=128 for a
            # tree holding a modified tracked file or an untracked one (an IGNORED file is
            # fine, which is what §16's out-of-band artifacts are). Passing `--force` here
            # would delete a fusion the orchestrator never committed, from the one tree it
            # exists in, on a walk the operator asked to reclaim disk with. Nothing has been
            # removed at this point, so the refusal costs them only the re-run.
            raise GcError(
                f"the synthesis worktree {synth} could not be removed ({e}). Nothing was "
                "deleted. Commit or discard what it holds — or `git worktree remove --force` "
                "it by hand if you have read what would go — then re-run `--gc`.") from e
        removed.append(f"worktree {synth}")

    # A SECOND DELETION, BECAUSE `worktree remove` DOES NOT DO IT. Measured: the branch
    # survives every removal. A walk that stopped above would report a run cleaned while its
    # branch — and every object reachable from it — stayed in the user's repository forever.
    if not refs:
        # RECORDED RATHER THAN SILENT. "There was nothing of this run left to delete" and "the
        # deletion happened" are different facts, and a report that can only name removals
        # cannot state the first — which is the one an operator chasing a missing branch needs.
        removed.append(f"no refs of run {run_id} remained under {list(_FORGE_REF_PREFIXES)}")
    for name in sorted(refs):
        label = ("branch " + name[len("refs/heads/"):]) if name.startswith("refs/heads/") \
            else f"ref {name}"
        try:
            # THE EXPECTED OID IS THE WHOLE POINT OF THE ARGUMENT. Measured: a bare
            # `update-ref -d <ref>` on a ref that does not exist exits 0 — so a walk that
            # reported success off the exit code would print `branch forge/<run-id>/synthesis`
            # for a branch nobody deleted, which is a run in which NOTHING happened leaving the
            # record of one in which something did. What keeps that off the report is the
            # enumeration above; what the OID adds is the other direction, a ref that MOVED
            # since this walk read it, which without it would be deleted at its new value.
            gitcmd.git(repo, *gitcmd.NO_DAEMON_CACHE, *gitcmd.NO_HOOKS,
                       "update-ref", "-d", name, refs[name], env_extra=gitcmd.READONLY)
        except gitcmd.GitError as e:
            raise GcError(
                f"{name} was {refs[name]} when this walk read it and `update-ref -d` refused "
                f"to delete it at that value ({e}). Something moved it since, so this run is "
                f"no longer the only writer of its own namespace. Already removed: "
                f"{removed or 'nothing'}.") from e
        removed.append(label)

    try:
        shutil.rmtree(run_dir)
    except OSError as e:
        # WHAT WENT IS STILL REPORTED. The worktree and the refs above are gone and cannot be
        # put back, so an error that reached the operator naming only the directory would
        # describe less than this walk did.
        raise GcError(
            f"{run_dir} could not be removed ({e}). Already removed: {removed or 'nothing'}. "
            "The rest of this run's directory is still on disk and is safe to delete by "
            "hand.") from e
    removed.append(f"run directory {run_dir}")
    return tuple(removed)
