"""The fusion brief: what a synthesis author needs, written where they will be standing.

§16 makes the orchestrator the synthesis author, and `cli.start` hands it a worktree, a run
id and a seat table. The table says which seats survived. It does not say what they TOUCHED,
and a fusion is a decision about paths — which two seats edited the same file, and which file
exactly one seat thought to edit at all. Both facts are already in the per-seat records
`runner._record` wrote, as `artifacts.paths` per attempt, so this reads the run directory and
spends nothing.

NOTHING HERE RANKS A SEAT. §12.5's order is taken over §10's claim ledger, §6.2's outcome,
§13's review risk and §12.1's measured size together, and a "most files touched" heading beside
a fusion brief would be read as a rank over one of the four. What this states is MEMBERSHIP:
who touched what, who touched it together, and who was alone.

THE LAST ATTEMPT AND NEVER THE UNION OF ALL OF THEM. §8.1 gives a retry a fresh clone, so
attempt 2's tree never carried attempt 1's edits. A union would describe a path set no clone
ever held, and hand the fusion a file list with no candidate behind it.

IT IS NOT WRITTEN INTO THE WORKTREE, AND THAT IS NOT A PREFERENCE. `cli._sidecars_of` keeps
every `??` record git's porcelain names, and `handover.mergeability` grants `MERGE_READY` only
when the tracked tree matches B1 AND the out-of-band set is empty. A brief beside the fusion
would make the second condition false on every run — every delivery reported `PATCH_ONLY`, and
`handover.out_of_band` handing the user a `cp` command for the engine's own scaffolding. So it
goes in the worktree's git directory, which git does not report and §15 reclaims with the
worktree. That directory is ASKED OF GIT and never joined: `Path(checkout) / ".git"` is a
directory in a clone and a FILE in the linked worktree §16 hands the synthesis author, which is
the only tree this is ever called on. `taskbundle.task_dir` resolves the same path the same way
for the same reason, and this is that construction rather than a second spelling of it.
"""
from __future__ import annotations

from pathlib import Path

from . import gitcmd, runstate, storage

BRIEF = "FUSION-BRIEF.md"

# What a seat's path set is when the record does not say. `None` AND NEVER `()`: an empty path
# set is the true statement "this seat changed nothing", which makes it disjoint from every
# other seat and makes every other seat's paths sole-touched. "Nobody recorded one" has to
# compare equal to nothing at all — including to another unreadable seat.
UNKNOWN = None


class BriefError(RuntimeError):
    """A run whose seats cannot be described, so no brief is written."""


def _last_attempt(run_dir, name) -> dict:
    rec = runstate.read_seat(run_dir, name)
    attempts = (rec or {}).get("attempts") or []
    return attempts[-1] if isinstance(attempts, list) and attempts else {}


def seat_paths(run_dir) -> dict:
    """`{seat: frozenset(paths) | UNKNOWN}` off the records on disk.

    THE TYPE CHECK IS NOT DECORATION. `artifacts` is `None` on a record whose attempt was
    written before the set was taken, and `paths` is a list of strings or the record is one
    this function cannot read. Either way the answer is `UNKNOWN`, because a list this
    function coerced would be a path set it invented.
    """
    out = {}
    for name in storage.seat_names(run_dir):
        art = _last_attempt(run_dir, name).get("artifacts")
        paths = art.get("paths") if isinstance(art, dict) else None
        if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
            out[name] = UNKNOWN
            continue
        out[name] = frozenset(paths)
    return out


def seat_verify(run_dir) -> dict:
    """`{seat: §6.2's outcome | None}` for the last attempt.

    `None` IS "NOBODY MEASURED" AND IS NOT A NON-PASS. §6.2 names four outcomes and has no
    word for an unrun gate, so a brief that rendered the absence as a failure would report a
    verdict over a clone nobody verified.
    """
    out = {}
    for name in storage.seat_names(run_dir):
        v = _last_attempt(run_dir, name).get("verification")
        out[name] = v.get("outcome") if isinstance(v, dict) else None
    return out


def overlap(paths: dict) -> dict:
    """`{(a, b): shared count | None}` for every unordered pair, `None` where either is UNKNOWN.

    `None`, NEVER `0`. Two seats whose path sets nobody recorded share no MEASURED path and
    also share no measurement, and rendering both as `0` tells the synthesis author the two are
    disjoint — the one sentence that sends them to fuse two edits to the same file as though
    they were edits to different ones. This is `seat_paths`'s refusal carried through the
    arithmetic rather than dropped by it.
    """
    names = sorted(paths)
    out = {}
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            pa, pb = paths[a], paths[b]
            out[(a, b)] = None if pa is UNKNOWN or pb is UNKNOWN else len(pa & pb)
    return out


def sole(paths: dict) -> dict:
    """`{seat: sorted paths only that seat touched}`, and `{}` unless EVERY seat is known.

    "Only seat X touched `db.py`" is a claim about all the seats, so it cannot be made from
    some of them: the unreadable seat is exactly the one that might also have touched it. A
    partial answer here is the fail-open this whole module is shaped against, arriving as a
    heading the reader has no way to distrust.
    """
    if not paths or any(v is UNKNOWN for v in paths.values()):
        return {}
    out = {}
    for name, own in paths.items():
        others = set()
        for other, theirs in paths.items():
            if other != name:
                others |= theirs
        out[name] = sorted(own - others)
    return out


def _verdict(outcome) -> str:
    return "verify not recorded" if outcome is None else f"verify {outcome}"


def text(run_dir) -> str:
    """The brief, as markdown. Raises `BriefError` for a run with no seat to describe."""
    paths, verdicts = seat_paths(run_dir), seat_verify(run_dir)
    if not paths:
        raise BriefError(
            f"{run_dir} records no seat, so there is nothing to brief a fusion on — a brief "
            "over no seat renders as a fusion whose inputs nobody named")
    unknown = sorted(n for n, v in paths.items() if v is UNKNOWN)

    lines = ["# Fusion brief", "",
             "Membership, not rank. This says who touched what; it makes no claim about which",
             "seat is strongest — that order is taken over the claim ledger, the gate outcome,",
             "the review risk and the measured size together, and none of them is here.", ""]

    lines += ["## Seats", ""]
    for name in sorted(paths):
        p = paths[name]
        count = "path set not recorded" if p is UNKNOWN else f"{len(p)} path(s)"
        lines.append(f"- **{name}** — {_verdict(verdicts.get(name))}; {count}")
    lines.append("")

    lines += ["## Paths each seat changed (Fsetup -> Fwork)", ""]
    for name in sorted(paths):
        p = paths[name]
        lines.append(f"### {name}")
        if p is UNKNOWN:
            lines.append("This seat's path set is **not recorded**, so nothing below counts it.")
        elif not p:
            lines.append("This seat changed no path.")
        else:
            lines += [f"- `{q}`" for q in sorted(p)]
        lines.append("")

    lines += ["## Pairwise path overlap", ""]
    pairs = overlap(paths)
    if not pairs:
        lines.append("Fewer than two seats, so there is no pair to compare.")
    else:
        for (a, b), n in sorted(pairs.items()):
            lines.append(f"- `{a}` x `{b}`: "
                         + ("**not comparable** — at least one path set is not recorded"
                            if n is None else f"{n} shared path(s)"))
    lines.append("")

    lines += ["## Paths exactly one seat touched", ""]
    only = sole(paths)
    if not only:
        lines.append("**No seat can be named the only one to touch a path**: "
                     f"{', '.join(unknown) or 'a seat'} has no recorded path set, and this "
                     "claim is about every seat rather than about the ones that were readable.")
    else:
        for name in sorted(only):
            own = only[name]
            lines.append(f"- **{name}**: " + (", ".join(f"`{q}`" for q in own) if own
                                              else "no path is uniquely this seat's"))
    lines.append("")
    return "\n".join(lines)


def brief_path(checkout) -> Path:
    """`<checkout's absolute git dir>/khenrix-forge/FUSION-BRIEF.md`.

    ASKED, NEVER JOINED — `taskbundle.task_dir`'s rule, and this is called on the one tree that
    makes the difference load-bearing: in §16's linked worktree `.git` is a FILE. `rev-parse
    --absolute-git-dir` is measured safe on this package's git closures — no index, no hook, no
    diff driver — so it needs `READONLY` and nothing else.
    """
    try:
        out = gitcmd.git(checkout, "rev-parse", "--absolute-git-dir",
                         env_extra=gitcmd.READONLY).stdout.strip()
    except gitcmd.GitError as e:
        raise BriefError(f"git named no git directory for {checkout} ({e}), so there is "
                         "nowhere to put a brief that the handover will not report as a "
                         "deliverable the user has to copy out by hand") from e
    if not out:
        raise BriefError(f"git named no git directory for {checkout}")
    return Path(out) / "khenrix-forge" / BRIEF


def write(run_dir, checkout) -> Path:
    """Write the brief for `checkout`'s author and return its absolute path.

    `atomic_write`, so a brief that could not be rendered whole leaves no partial file for a
    synthesis author to fuse against.
    """
    dest = brief_path(checkout)
    dest.parent.mkdir(parents=True, exist_ok=True)
    storage.atomic_write(dest, text(run_dir).encode("utf-8"))
    return dest
