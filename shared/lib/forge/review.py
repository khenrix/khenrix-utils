"""§13's review: the reviewer's input set, the in-process council call, and the record.

WHY THE COUNCIL IS CALLED IN PROCESS. The council CLI cannot express either contract §13
depends on. `parse_args` has no `--cwd`, so a shelled run gives agy a worktree while claude
and codex inherit the ORCHESTRATOR's cwd — the user's live checkout, dirty edits and all —
and the green header would describe a blind review that never happened. And `main()`
unconditionally injects the sentinel into the prompt, so a bundle-resident token would be a
second token nothing checks and a reviewer could quote the argv token having read nothing but
its launcher. Forge sets every reviewer's cwd itself and plants the token in the bundle.

WHAT THIS MODULE DELIBERATELY DOES NOT CALL, each with its reason:

  * `engine.apply_sentinel` — §13 puts the proof token INSIDE the bundle. Applying it to the
    prompt would make quoting it prove only that the seat read argv.
  * `engine.isolate_agy_worktree` — it repoints agy's cwd at a throwaway worktree
    (`engine.py:971-1015`). §13 requires all three reviewers' cwd to be the synthesis
    checkout; moving one of them is the ambient-context failure §13 is written to prevent.
  * `engine.make_readonly` / `apply_member_note` / `apply_readonly_posture` — all `main()`-only.
    An in-process caller inherits none of them. This module applies NONE of them either, so
    every reviewer carries `build_real_spec`'s bypass flag and could write into the checkout
    it is reviewing; `read_only=False` is passed to `run_council` so the council manifest
    records that absence rather than inheriting a claim about it. What ASKS a reviewer to
    keep its hands off the tree is `launcher_prompt`'s instruction and nothing stronger; what
    CHECKS afterwards is `loop`'s bracket (see `WORKTREE_KIND`), which refuses the round
    rather than preventing the write.
  * `seat.forge_spec` — see `reviewer_specs`. Its validator neutralises the sentinel.

ROUNDS RUN SEQUENTIALLY, AND THAT IS LOAD-BEARING. `engine._LIVE_PGIDS` (`engine.py:821`),
`_LIVE_WORKTREES` (`:923`) and `_STATE` (`:924`) are process-wide, so two concurrent
`run_council` calls share them and one round's teardown reaches the other's members. §13's
rounds are sequential by definition (round 2 reviews the fix round 1 asked for), so nothing
here needs concurrency and nothing here may introduce it.

`codex exec --json`, NOT `codex review`. Measured 2026-08-03: `codex review [OPTIONS]
[PROMPT]` has no `--json`, no `--model` and no `--cd`, so the engine's `extract_codex_json`
would turn every review into a silent `parse_failure` and "found nothing" would be
indistinguishable from "could not be read". A recorded deviation from §13's text: forge
supplies the review framing itself, as the prompt.
"""
import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass, fields
from pathlib import Path

from council import engine

from . import (fingerprint, fleet, gitcmd, journal as journalmod, snapshot, storage,
               taskbundle)
from .taskbundle import task_dir as taskbundle_task_dir

SEVERITIES = ("blocker", "important", "minor")
RESOLUTIONS = ("open", "fixed", "unresolved", "rejected")

# §13 asks for `--mode deep`. `mode` is a MANIFEST LABEL ONLY — passing it does not select
# `MODE_TIMEOUT["deep"]`, and the timeout is whatever the caller passes (`engine.py:1274`,
# `:90`). A manifest reading `mode: deep` beside a 300-second timeout is a record cleaner
# than its evidence, so the number is named here and a test pins the two together.
REVIEW_TIMEOUT_SEC = engine.MODE_TIMEOUT["deep"]

COUNCIL_KIND = "council_round"

_INPUTS = "inputs.json"
_INSTRUCTIONS = "REVIEW.md"
_TOKEN_FILE = "proof-token.txt"
# `<git-dir>/khenrix-forge/` holds BOTH this module's review inputs and §20's task bundle
# (`taskbundle.task_dir`). Nothing below scans it by name — see `reviewer_roots` — because a
# scan that names subdirectories covers the ones somebody remembered.
_FORGE_SUBDIR = "khenrix-forge"
_REVIEW_SUBDIR = (_FORGE_SUBDIR, "review")
_TASK_SUBDIR = (_FORGE_SUBDIR, "task")

_FENCE = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)


class ReviewError(RuntimeError):
    """A review this module will not run, or a record it will not write."""


def review_dir(checkout, round_: int) -> Path:
    """Where round `n`'s reviewer inputs live: `<git-dir>/khenrix-forge/review/round-<n>`.

    ASKED, NEVER JOINED, for `taskbundle.task_dir`'s reason: `Path(x) / ".git"` is a
    directory in an ordinary clone and a FILE in a linked worktree, so the join is right by
    luck and wrong the moment §16's synthesis worktree exists. `rev-parse
    --absolute-git-dir` loads no index, fires no hook and runs no diff driver.

    INSIDE THE GIT DIRECTORY, so the worktree stays clean: §7.3's change predicate and
    `snapshot`/`harvest` all range over the worktree, and a review input dropped there would
    arrive in the next candidate as an artifact the reviewer wrote.
    """
    _check_round(round_)
    out = gitcmd.git(checkout, "rev-parse", "--absolute-git-dir",
                     env_extra=gitcmd.READONLY).stdout.strip()
    if not out:
        raise ReviewError(f"git named no git directory for {checkout}")
    return Path(out).joinpath(*_REVIEW_SUBDIR, f"round-{round_}")


def _check_round(round_) -> None:
    """One spelling of "a review round is numbered from 1", read by `review_dir`,
    `round_dir` and `Round`. `bool` is excluded explicitly because `True == 1`, and a round
    named `True` would build `round-True` in one function and compare equal to round 1 in
    the next."""
    if not isinstance(round_, int) or isinstance(round_, bool) or round_ < 1:
        raise ReviewError(f"a review round is numbered from 1, not {round_!r}")


_TEMPLATE = """\
# Independent review — round {round_}

You are one of three reviewers looking at this change independently. You have a shell and a
git checkout. **Everything you are given is listed below; nothing else about this run was
prepared for you.**

## The change

* Synthesis checkpoint: `{checkpoint}`
* Baseline commit: `{baseline_commit}`
* Baseline tree: `{baseline_tree}`

Run the diff yourself and cite changed-file evidence for every finding:

```
git diff {baseline_commit}..{checkpoint}
```

## The task

{task_line}

## Artifacts

{artifact_line}

## Proof of reading

Quote this token verbatim somewhere in your answer: `{token}`

It is written only in this bundle. An answer that does not carry it is recorded as a seat
that did not read its input, which is a different thing from a seat that found nothing.

## How to answer

End your answer with exactly ONE fenced JSON block, and nothing after it:

```json
{{"findings": [{{"severity": "blocker", "claim": "…", "evidence": "path:line"}}]}}
```

`severity` is one of {severities}. `evidence` is REQUIRED on every finding and is the
changed-file citation asked for above — a `path:line`, or the command whose output you are
citing. A finding without one makes the whole answer unreadable, because a claim this run
cannot check is not one it can act on.

An empty `findings` list is a valid answer and means you found nothing — but the block must
be present, because a missing block is recorded as an answer that could not be read rather
than as a clean review.
"""


def launcher_prompt(bundle_path) -> str:
    """The small prompt that points at the bundle. §13: a launcher, not the review.

    claude and agy place the prompt in ARGV (`engine.build_real_spec`), and a task plus its
    resolved closure can still hit `E2BIG` without the raw diff — so the review instructions
    live in a file and this names it.

    THE "DO NOT MODIFY" LINE IS AN INSTRUCTION, NOT A MECHANISM. This module applies no
    read-only posture (see the module docstring), and `run_council` runs its members in a
    thread pool, so all three write-capable reviewers are inside the synthesis checkout at
    once and nothing here PREVENTS a write. What catches one afterwards is `loop`'s bracket
    (see `WORKTREE_KIND`), which makes a round whose tree moved inadmissible; this sentence
    is what asks, and that measurement is what checks.
    """
    return (f"Read {bundle_path}/{_INSTRUCTIONS} and follow it exactly. Your working "
            "directory is the checkout under review. Do not modify any file; this is a "
            "review.")


def write_reviewer_inputs(checkout, round_: int, *, checkpoint: str, baseline_commit: str,
                          baseline_tree: str, artifact_manifest, token: str,
                          task_bundle_present: bool) -> Path:
    """Lay round `n`'s inputs down inside the checkout's git directory, and return the path.

    EVERY FILENAME HERE IS A LITERAL IN THIS MODULE, which is why this writes plainly rather
    than through `bundle`'s dir-fd descent. That machinery exists because a MANIFEST supplies
    path components an attacker controls; nothing below takes a path from any record. The
    directory is created by this call and refused if it already exists, so no earlier write
    can have laid a symlink where a later one lands.

    AN ABSENT INPUT IS STATED, NEVER OMITTED. §16's out-of-band artifact manifest has no
    producer in this package and §20's task bundle may not have been materialized; a reviewer
    told "there is none" can weigh that, and one that simply never sees the section cannot.
    """
    for name, value in (("checkpoint", checkpoint), ("baseline_commit", baseline_commit),
                        ("baseline_tree", baseline_tree), ("token", token)):
        if not isinstance(value, str) or not value.strip():
            raise ReviewError(f"{name} is a non-empty string, not {value!r}")
    d = review_dir(checkout, round_)
    if d.exists():
        raise ReviewError(
            f"{d} already holds round {round_}'s reviewer inputs. A second write into a live "
            "round would change what a reviewer was given after it was given it, and the "
            "round's recorded prompt identity would then describe a bundle that no longer "
            "exists.")
    d.mkdir(parents=True)
    task_line = (
        "The immutable original task bundle is at "
        f"`$(git rev-parse --absolute-git-dir)/{'/'.join(_TASK_SUBDIR)}`."
        if task_bundle_present else
        "**There is no task bundle in this checkout.** This run did not materialize "
        "one, so the task text is not available to you here; review the diff against "
        "the claims it makes for itself.")
    artifact_line = (f"Out-of-band artifact manifest: `{artifact_manifest}`"
                     if artifact_manifest else
                     "There is **no out-of-band artifact manifest** for this run — §16's "
                     "manifest is not produced yet — so no artifact outside the git diff has "
                     "been declared to you.")
    text = _TEMPLATE.format(round_=round_, checkpoint=checkpoint,
                            baseline_commit=baseline_commit, baseline_tree=baseline_tree,
                            token=token, task_line=task_line, artifact_line=artifact_line,
                            severities=list(SEVERITIES))
    storage.atomic_write(d / _INSTRUCTIONS, text.encode("utf-8"))
    storage.atomic_write(d / _TOKEN_FILE, (token + "\n").encode("utf-8"))
    storage.atomic_write(d / _INPUTS, (json.dumps({
        "round": round_,
        "synthesis_checkpoint": checkpoint,
        "baseline_commit": baseline_commit,
        "baseline_tree": baseline_tree,
        "artifact_manifest": artifact_manifest,
        "task_bundle_present": bool(task_bundle_present),
    }, sort_keys=True, indent=2) + "\n").encode("utf-8"))
    return d


def _raise(err: OSError):
    """`os.walk`'s onerror. WITHOUT THIS THE SCAN BELOW IS A FALSE GREEN.

    `os.walk` with no `onerror` returns nothing for a subtree it cannot list, and says nothing
    about having skipped it — so a scan looking for the ledger's bytes finds none and
    certifies a tree it could not read. The same omission is why `snapshot`, `baseline`,
    `screen`, `inspect` and `taskbundle` all pass an `onerror` too.
    """
    raise ReviewError(
        f"the reviewer's tree could not be scanned whole ({err.filename}: {err.strerror}), so "
        "whether the ledger's bytes are in it is a question this run could not answer. §13's "
        "blindness has to be structural, and an unreadable subtree is not a clean one.") from err


def _covered(p: Path, roots) -> bool:
    return any(p.is_relative_to(r) for r in roots)


def _digests_under(roots, target: bytes, cap: int) -> list:
    """Every path a reviewer standing in `roots` can read whose bytes are `target`.

    SYMLINKS ARE FOLLOWED, AND THE VERSION THAT SKIPPED THEM WAS FAIL-OPEN. Its argument was
    that a link's target "is either in this tree and visited on its own, or outside it and
    not in the reviewer's tree" — the second half is false. A reviewer has a shell; `cat
    notes.txt` where `notes.txt` is a symlink to the run directory's `ledger.json` reads the
    ledger, and the target being outside the walk is precisely what made the skip miss it.
    A directory symlink is worse still: `os.walk` lists it and does not descend, so
    `state -> <run dir>` hid the real ledger behind one entry the scan looked straight at.

    So: file entries are stat'd and read THROUGH the link, and a directory symlink whose
    target is not already covered by a root becomes another root (`visited` bounds the
    recursion a link to an ancestor would otherwise create). A link that cannot be
    dereferenced at all is skipped rather than refused — a reviewer cannot read it either,
    so it carries no bytes; a REGULAR file that cannot be read is still a refusal, because
    that one is a subtree this scan could not answer for.

    THE SIZE TEST IS ALSO WHAT KEEPS THIS FROM BLOCKING, and that is why `target` may not be
    empty (`assert_ledger_is_out_of_reach` refuses one that is). Measured on this machine
    2026-08-03: a fifo, a unix socket, `/dev/null`, `/dev/zero`, `/dev/urandom` and every
    `/dev/loop*` block device all report `st_size == 0`, so a non-empty target excludes every
    non-regular entry before anything opens it. With an EMPTY target they would all match, and
    the read of the first fifo in the tree would never return.

    ONE BUDGET FOR THE WHOLE SCAN, not one per root: the caller's question is whether the
    ledger is anywhere in the reviewer's reach, and a per-root cap answers a smaller one
    every time the root list grows.
    """
    hits, seen = [], 0
    want = hashlib.sha256(target).hexdigest()
    roots = [Path(r) for r in roots]
    visited, i = set(), 0
    while i < len(roots):
        root = roots[i]
        i += 1
        for dirpath, dirnames, filenames in os.walk(root, onerror=_raise):
            here = Path(dirpath)
            for dn in dirnames:
                link = here / dn
                if not link.is_symlink():
                    continue
                try:
                    dest = link.resolve(strict=True)
                except OSError:
                    continue          # dangling or looping: nothing to read through it
                if not dest.is_dir() or _covered(dest, roots) or dest in visited:
                    continue
                visited.add(dest)
                roots.append(dest)    # a reviewer can `cd` through it, so the scan must too
            for fn in filenames:
                seen += 1
                if seen > cap:
                    raise ReviewError(
                        f"more than {cap} files are reachable from {[str(r) for r in roots]}; "
                        "the ledger-exclusion scan stopped before it finished, and a partial "
                        "scan proves nothing about the part it did not reach")
                p = here / fn
                try:
                    st = p.stat()     # follows the link, which is the point
                except OSError as e:
                    if p.is_symlink():
                        continue      # unreadable through the link for the reviewer too
                    _raise(e)
                if st.st_size != len(target):
                    continue
                try:
                    same = hashlib.sha256(p.read_bytes()).hexdigest() == want
                except OSError as e:
                    if p.is_symlink():
                        continue
                    _raise(e)
                if same:
                    hits.append(str(p))
    return hits


def reviewer_roots(checkout) -> tuple:
    """Every root a reviewer sitting in `checkout` can read: the worktree, and its git dir.

    TWO ROOTS, NOT ONE, AND THE SECOND IS WHY THIS IS A FUNCTION. In an ordinary clone the git
    directory is INSIDE the worktree, so one walk over the checkout sweeps it up and a
    single-root scan is total by luck. In a LINKED WORKTREE — the exact configuration §16's
    synthesis worktree introduces, and the one `review_dir`'s docstring is already written for
    — the git directory is `<main>/.git/worktrees/<name>`, outside the checkout entirely.
    §20's task bundle lives at `<git-dir>/khenrix-forge/task` and reviewers ARE given it, so
    in that configuration the one directory the ledger must above all not be copied into sat
    outside every root the scan looked at, and the scan certified the tree clean. Measured: with a
    copy of the ledger in the task bundle of a linked worktree, the two-root version of this
    check PASSED.

    THE WHOLE GIT DIRECTORY, NOT `khenrix-forge` UNDER IT. Naming a subdirectory covers the
    ones somebody remembered — `review` was named and `task` was not, which is the entire
    defect. A reviewer with a shell can read all of it, and in an ordinary clone the walk over
    the checkout already does; one rule for both configurations is what makes the guarantee
    STRUCTURAL rather than incidental.
    """
    co = Path(checkout).resolve()
    out = gitcmd.git(checkout, "rev-parse", "--absolute-git-dir",
                     env_extra=gitcmd.READONLY).stdout.strip()
    if not out:
        raise ReviewError(f"git named no git directory for {checkout}, so this run cannot "
                          "enumerate the roots a reviewer in it could read")
    gd = Path(out).resolve()
    return (co,) if gd.is_relative_to(co) else (co, gd)


# The ref a review clone is taken at. §4's construction path is `--revision=<ref>`, so the
# checkpoint needs a name before it can be cloned from. Under §9's own namespace, so `--gc`'s
# walk reclaims it with everything else this run created.
REVIEW_REF = "refs/khenrix-forge/{run_id}/review-{round_}"


class _AtCheckpoint:
    """The three fields `fleet.clone_seat` reads off a baseline, answered for a REVIEW clone.

    A shim rather than a `baseline.Baseline`, because a `Baseline` is B1's record and this is
    not B1 — but it carries all three, and every one of them is an honest statement about a
    checkpoint:

      * `.ref` (`fleet.py:195`) is the name the clone is taken at, and the run id is split out
        of it, which is why `REVIEW_REF` sits in §9's namespace.
      * `.commit` (`fleet.py:326`) is compared against the clone's `HEAD` and raises `SeatError`
        on a mismatch. For a review clone the honest value IS the checkpoint: that is the
        commit the round reviews, and the assertion is the one that catches a clone that landed
        somewhere else.
      * `.filesystem_manifest` (`fleet.py:333`) is read as `(… or {})`, and `{}` is the true
        statement "no filesystem manifest is asserted over this tree" — a review clone is taken
        at a checkpoint, not restored from an inventory.

    A SHIM CARRYING ONLY `.ref` WAS THE FIRST DRAFT AND COULD NOT DRIVE THE FUNCTION AT ALL.
    Measured: `fleet.clone_seat` raised `AttributeError: '_AtRef' object has no attribute
    'commit'` — which is not a `FleetError`, so the `except` below would not have caught it and
    the refusal it raises would never have been reached. The argument that a shim should carry
    less because "a checkpoint is not B1" had it backwards: carrying less did not avoid stating
    something false, it avoided stating anything, and the function needs the statements.
    """

    def __init__(self, ref: str, commit: str) -> None:
        self.ref = ref
        self.commit = commit
        self.filesystem_manifest = {}


def assert_ledger_is_out_of_reach(run_dir, *, checkout, other_clones) -> None:
    """§13's blindness, as a mechanical assertion rather than a sentence in a prompt.

    "The ledger path is not passed" is not enough: §13 sets every reviewer's cwd to this
    checkout and a reviewer has a shell, so the guarantee has to be that THE BYTES ARE NOT IN
    THE TREE. Two complementary checks, neither of which subsumes the other:

      1. PATH. A run directory under one of the roots puts the real ledger in a tree the
         review can read with no copy involved, and no content scan is needed to see it.
      2. CONTENT. A copy under another name has a different path and the same bytes, which is
         what the digest sweep catches — through symlinks as well, see `_digests_under`.

    EVERY ROOT, AND THE LIST IS DERIVED RATHER THAN REMEMBERED. `reviewer_roots` answers the
    two the reviewer sits in. `other_clones` is the rest: §13's blindness holds only if the
    ledger's bytes are under NO clone root — not a seat, not the synthesis checkout, not a
    verifier clone — and those paths this module CANNOT derive, because `fleet.clone_seat`
    takes its destination from its caller (`fleet.py:166`). It is
    a REQUIRED argument and is never defaulted: `()` is a caller stating that no other clone
    root exists at this moment, which is a claim somebody made and can be wrong out loud; an
    omitted argument is a claim nobody made and is wrong silently.

    A ROOT THAT COULD NOT BE SCANNED IS A REFUSAL, not a skip. `os.walk(onerror=_raise)` turns
    a missing root, a root that is a file, and an unreadable subtree all into `ReviewError` —
    measured: `os.walk` calls `onerror` with `FileNotFoundError` for a missing root and
    `NotADirectoryError` for a file. The version of this function that wrote
    `if review_root.is_dir():` skipped a root it could not see, which is the same false green
    one level up.

    A MISSING LEDGER IS A REFUSAL. You cannot assert that bytes are out of reach without
    reading them, and "there is no ledger, so nothing was leaked" is a clean verdict produced
    by the absence of the evidence rather than by the evidence.

    WHAT THIS CANNOT DO, STATED SO THE NEXT READER DOES NOT INHERIT A PROMISE THE OS NEVER
    MADE. Both checks are about PATHS, and there is no permission boundary anywhere in this
    design: reviewers run as the operator's own UID with a shell, so a reviewer that goes
    looking can read the run directory by absolute path, glob `$XDG_STATE_HOME/khenrix-forge/
    */ledger.json`, or `find` for it — none of which any arrangement of roots prevents.
    Moving the review clone somewhere else does not close this: a sibling of the run directory
    is reachable by a computable name, so it only relocates the `..`. §13's blindness is therefore BY CONSTRUCTION — the
    reviewer is not given the path, the bytes are not in its tree, and the prompt does not
    name them — and is not enforced containment. Closing it for real needs an OS boundary
    (a mount namespace, a separate UID, a container) or a ledger that is never written to
    disk for the duration of the round. Until then this function buys "no reviewer trips over
    it", which is worth having and is not the same claim.
    """
    lp = Path(storage.ledger_path(run_dir))
    try:
        blob = lp.read_bytes()
    except FileNotFoundError as e:
        raise ReviewError(
            f"there is no ledger at {lp}, so this run cannot assert that its bytes are out of "
            "a reviewer's reach — the check would pass because the evidence is missing, which "
            "is the shape §10.1 exists to refuse") from e
    except OSError as e:
        raise ReviewError(f"the ledger at {lp} could not be read ({e.strerror}), so its "
                          "containment could not be checked") from e
    if not blob:
        # An empty ledger is not evidence of containment, and the sweep below cannot answer
        # the question with it: every empty file in the tree holds those bytes, and the sweep
        # would `read()` the first fifo it met and never return (see `_digests_under`).
        raise ReviewError(
            f"the ledger at {lp} is zero bytes, so there is nothing whose reach this run "
            "could assert. A file a truncated write left behind is not a ledger, and the "
            "check would otherwise pass by having no bytes to look for")

    roots = list(reviewer_roots(checkout))
    for extra in other_clones:
        p = Path(extra).resolve()
        if p not in roots:
            roots.append(p)
    for root in roots:
        if lp.resolve().is_relative_to(root):
            raise ReviewError(
                f"the ledger is at {lp}, which is under {root} — a tree §13's review can "
                "read. §13 gives every reviewer a shell, so a ledger inside one of these "
                "roots is passed to the review however carefully the prompt avoids naming it.")
        # THE OTHER DIRECTION, WHICH THE CHECK ABOVE DOES NOT ASK. A root
        # UNDER the ledger's own directory is the same reach with the hops reversed: from
        # `<run>/review/round-1/checkout` the ledger is `../../../ledger.json`, one ordinary
        # relative path from a cwd §13 hands the reviewer. And it is a READ — the
        # `worktree_identity` bracket beside this measures WRITES, so no tightening of that
        # bracket could ever have caught it. Containment is a two-way question and this asked
        # it one way.
        if root.is_relative_to(lp.resolve().parent):
            raise ReviewError(
                f"a reviewer root is at {root}, inside {lp.parent} — the directory holding "
                f"the ledger. §13's blindness is asserted against the ledger's PATH, and a "
                "reviewer with a shell reaches it from there by `..`, which is a read no "
                "write-measuring bracket can see.")
    hits = _digests_under(roots, blob, storage.Quota.for_harvest().max_files)
    if hits:
        raise ReviewError(
            f"the ledger's exact bytes are present at {sorted(set(hits))}, inside a root §13's "
            "review can read. §13's blind review is the strongest call in this design; a copy "
            "under another name defeats it as completely as passing the path would.")


def reviewer_specs(names, *, prompt: str, timeout: int, cwd, token: str, workdir,
                   cfg=None, build=engine.build_real_spec) -> list:
    """One `ProviderSpec` per reviewer: forge's cwd, forge's token, the council's validator.

    BUILT THROUGH `engine.build_real_spec` RATHER THAN HAND-ROLLED, because that function
    holds per-provider knowledge this module must not fork — agy's Go-style flag parsing
    STOPS at the first positional, so every flag has to precede the prompt or
    `--dangerously-skip-permissions` is silently dropped and agy returns empty in seconds.
    A second copy of that rule here is a second place for it to be lost.

    `validator` IS LEFT `None` ON PURPOSE, AND THIS IS THE OPPOSITE OF WHAT A BUILDER NEEDS.
    `seat.forge_spec` installs `_forge_validator`, which delegates to `engine.evaluate` on a
    copy of the spec with `min_chars=0` and `sentinel=None` REGARDLESS of what the passed spec
    carries (`seat.py:239-266`, its own docstring). For a builder that is right: a terse
    sign-off after forty minutes of edits must not trigger a re-run on top of half-finished
    work. For a reviewer it is fatal — one that never opened the bundle would score `valid`,
    and §13's whole proof-token argument would evaporate. `None` here means `run_provider`
    falls back to `engine.evaluate`, which calls `score_seat(text, spec.sentinel,
    spec.min_chars)` (`engine.py:1109`).

    A PANEL OF NONE AND A PANEL WITH A NAME IN IT TWICE ARE BOTH REFUSED, because the record
    downstream counts seats. An empty `names` produces a `Round` with no findings and no
    silent seats — which is the exact reading of "three reviewers found nothing", produced by
    convening nobody. A repeated name produces two specs that `run_round` resolves against ONE
    council record, so one seat's answer is counted twice and a panel of one reports as a
    panel of two.
    """
    if not isinstance(token, str) or not token.strip():
        raise ReviewError(f"a reviewer's proof token is a non-empty string, not {token!r}")
    names = list(names)
    if not names:
        raise ReviewError(
            "a review round convenes at least one reviewer: an empty panel records no "
            "findings and no silent seats, which is indistinguishable from three reviewers "
            "who found nothing")
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise ReviewError(
            f"{dupes} appear more than once in this panel; every seat is resolved against the "
            "council record of its own name, so a repeated name counts one answer twice")
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    specs = []
    for name in names:
        spec = build(name, prompt, timeout, cfg or {}, workdir)
        spec.cwd = str(cwd)
        spec.sentinel = token
        if spec.validator is not None:
            raise ReviewError(
                f"{name}: this spec carries a validator, which would replace the council's "
                "own `evaluate` — and the only validator in this package forces "
                "`sentinel=None`, so a reviewer that never opened the bundle would score "
                "valid")
        specs.append(spec)
    return specs


def parse_findings(text) -> tuple:
    """The reviewer's structured findings, or `(None, why)` when the answer cannot be read.

    THE WHOLE POINT OF THIS FUNCTION IS THE DIFFERENCE BETWEEN `[]` AND `None`. A present,
    parseable block with an empty list is a reviewer saying "I found nothing" — a real answer
    §13's loop may act on. Everything else is "this answer could not be read", which §13's
    loop must record as a SILENT SEAT rather than as a clean review. Every early return below
    is one of the ways the second thing looks like the first.

    A MALFORMED ROW DROPS THE ROW, NOT THE WHOLE ANSWER — the one exception to that, and why
    it is one. An earlier version voided the ENTIRE answer over one row missing `claim` or
    `evidence`, which discarded every OTHER finding in the same block too: real, evidenced
    claims a three-provider panel was paid to produce, thrown out because a sibling row in the
    same JSON skipped a field. That is not the fail-closed reading it looks like — a genuine
    blocker with good evidence, sitting beside a sloppy row, went silent along with it, which
    is a VERDICT READING CLEANER THAN THE EVIDENCE THE PANEL ACTUALLY RETURNED: the blocker
    was reported and paid for, and the record showed nothing. Dropping only the malformed row
    keeps every properly-evidenced finding in play; `why` carries what was dropped and why so
    the drop itself is never silent (`run_round` puts it on the journal), and an empty
    `claim`/`evidence` is still never repaired with an invented value — only left out. A block
    that yields NO usable row this way is not "found nothing": see the `not kept` check below,
    which keeps that distinction rather than trading it for this one.
    """
    if not isinstance(text, str):
        return None, f"a reviewer's answer is text, not {type(text).__name__}"
    blocks = _FENCE.findall(text)
    if not blocks:
        return None, ("no fenced ```json block was found in this answer, so its findings "
                      "could not be read; that is not the same as finding nothing")
    if len(blocks) > 1:
        return None, (f"this answer carries more than one fenced ```json block ({len(blocks)}"
                      "), and nothing may pick between them")
    try:
        payload = json.loads(blocks[0])
    except ValueError as e:
        return None, f"the fenced block is not readable as json: {e}"
    if not isinstance(payload, dict) or not isinstance(payload.get("findings"), list):
        return None, "the fenced block carries no `findings` list"
    kept, dropped = [], []
    for row in payload["findings"]:
        if not isinstance(row, dict):
            return None, f"a finding is an object, not {type(row).__name__}"
        if row.get("severity") not in SEVERITIES:
            return None, (f"a finding's severity is one of {list(SEVERITIES)}, not "
                          f"{row.get('severity')!r}")
        # `claim`/`evidence` ARE CHECKED HERE BECAUSE REVIEW.md DEMANDS THEM. The instructions
        # tell every reviewer to cite changed-file evidence for every finding, and a demand
        # nothing checks is a sentence, not a requirement — the blocker line
        # `terminal_from_record` prints would then name a claim with nothing behind it.
        missing = [name for name in ("claim", "evidence")
                  if not isinstance(row.get(name), str) or not row[name].strip()]
        if missing:
            label = row.get("claim") if isinstance(row.get("claim"), str) and \
                row["claim"].strip() else "(no claim)"
            dropped.append(f"{label!r} missing {' and '.join(missing)}")
            continue
        kept.append(row)
    if payload["findings"] and not kept:
        # EVERY row in a non-empty answer was dropped: there is nothing left to act on, and
        # `[]` here would misreport "the reviewer looked and found nothing" as "the reviewer
        # answered and none of it could be acted on" — the exact conflation this function
        # exists to refuse.
        return None, f"every finding in this answer was dropped ({'; '.join(dropped)})"
    return kept, ("; ".join(dropped) if dropped else "")


def finding_id(round_: int, seat: str, severity: str, claim: str, evidence: str) -> str:
    """Content-derived, never a counter — §10's rule for ledger rows, applied here.

    Coverage checks compare findings across rounds; if round 2 splits or inserts a finding
    and the ids shift, the comparison compares stale identity. The ROUND is in the hash
    because "the same claim, raised again after a fix" is a different fact from "the same
    claim, still open", and the resolution field is not, because a finding's id must not
    change when it is resolved.

    `evidence` IS IN THE HASH, so one claim cited at two places is two findings rather than
    one id written twice. Without it a reviewer that raised the same wording against two
    call sites — ordinary output — produced two `Finding`s sharing an id, which `Round`
    accepted, `write_round` stored, and `write_resolutions` then refused, crashing the loop
    AFTER the three-provider panel had been paid for. What collides now is a byte-identical
    repeat, which carries nothing the first row does not.
    """
    blob = "\0".join((str(round_), seat, severity, claim, evidence)).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]


@dataclass(frozen=True)
class Finding:
    """One reviewer's one claim, at rest.

    `evidence` IS A FIELD AND IS NON-EMPTY, because REVIEW.md asks every reviewer to cite
    changed-file evidence for every finding and a record that dropped it would make that the
    one demand the prompt states and the record cannot show. It is what a fix pass reads to
    find the thing being complained about, and what an operator reads beside
    `terminal_from_record`'s blocker line. A producer with nothing to cite says so in words
    rather than leaving it blank — an empty string is refused here, so "no evidence was
    given" cannot be spelled the same way as "this field was never filled in".
    """
    id: str
    round: int
    seat: str
    severity: str
    claim: str
    evidence: str
    resolution: str

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ReviewError(f"severity is one of {list(SEVERITIES)}, "
                              f"not {self.severity!r}")
        if self.resolution not in RESOLUTIONS:
            raise ReviewError(f"resolution is one of {list(RESOLUTIONS)}, "
                              f"not {self.resolution!r}")
        for name in ("id", "seat", "claim", "evidence"):
            v = getattr(self, name)
            if not isinstance(v, str) or not v.strip():
                raise ReviewError(f"{name} is a non-empty string, not {v!r}")


@dataclass(frozen=True)
class Round:
    """One review round, as the record §13 requires the terminal transition to read.

    `seats_silent` IS WHY THIS TYPE EXISTS. A round that recorded only findings would let a
    panel of one describe itself as a panel of three: two failed reviewers contribute zero
    findings, and zero findings reads as "no blockers". Each entry is `(seat, reason)`, and
    the reason is the council's own — `parse_failure`, `auth_or_quota`, `nonzero_exit`, or
    this module's `unreadable_findings` / `unreadable_result_file` / `no_record`.

    IT VALIDATES ITSELF BECAUSE EVERY SIBLING RECORD IN THIS PACKAGE DOES — `Size`,
    `Decision`, `Progress`, `Dimensions`, `Finding`, `Resolution`, `DiffSize`, `Ultra` — and
    because this is the one the TERMINAL reads. A hand-built `Round` is exactly what `read_round` produces off disk,
    so the checks below run on the record a crashed run is reconstructed from, not only on
    the one this process just built.

    WHAT IT DOES NOT CHECK is that the panel is non-empty: `Round(1, sha, (), (), (), ())` is
    a legal record, because a round whose panel could not be convened still has to be
    recordable. The refusal to convene nobody lives in `reviewer_specs`, one call earlier.
    """
    round: int
    checkpoint: str
    findings: tuple
    identities: tuple
    seats_responded: tuple
    seats_silent: tuple

    def __post_init__(self) -> None:
        try:
            _check_round(self.round)
        except ReviewError as e:
            raise ReviewError(f"{e}; `round_dir` refuses anything else and this record would "
                              "then have nowhere to be written") from e
        if not isinstance(self.checkpoint, str) or not self.checkpoint.strip():
            raise ReviewError(
                f"a round names the checkpoint it reviewed, not {self.checkpoint!r}: a review "
                "of an unnamed tree cannot be re-derived by `--collect`")
        wrong = sorted({type(f).__name__ for f in self.findings
                        if not isinstance(f, Finding)})
        if wrong:
            raise ReviewError(f"a round's findings are Finding records, not {wrong}")
        ids = [f.id for f in self.findings]
        repeated = sorted({i for i in ids if ids.count(i) > 1})
        if repeated:
            # THE WRITE HALF OF `_one_row_per_finding`. That function refuses two RESOLUTIONS
            # for one finding, on both routes, because the terminal keys them by id and the
            # extra row overwrites rather than conflicts. The findings half had no such guard,
            # so two `Finding`s sharing an id were accepted here, stored by `write_round`, and
            # refused by `write_resolutions` — a crash in `loop` after the panel was paid for,
            # over ordinary reviewer output. `finding_id` now separates one claim cited twice;
            # what reaches this branch is a repeat with no distinguishing content at all, or a
            # record built by hand, and neither has a reading the terminal could give it.
            raise ReviewError(
                f"{repeated} identify more than one of this round's findings. Resolutions are "
                "keyed by finding id, so two findings sharing one are resolved by whichever "
                "row a fix pass wrote last — and `write_resolutions` refuses the pair outright.")
        for f in self.findings:
            if f.round != self.round:
                raise ReviewError(
                    f"finding {f.id} carries round {f.round} and is being recorded in round "
                    f"{self.round}. `terminal_from_record` reads each record's OWN number, so "
                    "a finding filed under the wrong one is classified at the wrong time — "
                    "and 'fixed in the last round' is the branch that decides `degraded`.")
        for name in self.seats_responded:
            if not isinstance(name, str) or not name.strip():
                raise ReviewError(
                    f"a responding seat is named, not {name!r}: the terminal counts the panel "
                    "as `responded + silent`, so an unnamed entry is a reviewer in the count "
                    "and in no record")
        for entry in self.seats_silent:
            if len(entry) != 2 or not all(isinstance(x, str) and x.strip() for x in entry):
                raise ReviewError(
                    f"a silent seat is recorded as (seat, reason) with both named, not "
                    f"{entry!r}: an unnamed reason is a reviewer dropped from the panel with "
                    "no record of why, which is the state `seats_silent` exists to prevent")
        for group, label in ((self.seats_responded, "responded"),
                             ([s for s, _ in self.seats_silent], "silent")):
            repeated = sorted({n for n in group if list(group).count(n) > 1})
            if repeated:
                raise ReviewError(
                    f"{repeated} appear more than once in `seats_{label}`; the terminal counts "
                    "the panel by length, so a repeated name is one reviewer counted twice")
        both = sorted(set(self.seats_responded) & {s for s, _ in self.seats_silent})
        if both:
            raise ReviewError(
                f"{both} are recorded as having both answered and been silent; the terminal "
                "counts the panel as `responded + silent`, so a seat in each is a panel of "
                "four described by three reviewers")


def round_dir(run_dir, round_: int) -> Path:
    _check_round(round_)
    return Path(run_dir) / "review" / f"round-{round_}"


def findings_path(run_dir, round_: int) -> Path:
    """UNDER THE RUN DIRECTORY, never in a clone. §13 requires the record to be forge's own
    and durable; `run_provider` writes its `<name>.result.txt` with a plain `write_text`
    (`engine.py:1235-1240`), so a pointer into the council workdir is not a record."""
    return round_dir(run_dir, round_) / "findings.json"


def _payload(r: Round) -> dict:
    return {
        "round": r.round,
        "checkpoint": r.checkpoint,
        "findings": [{f.name: getattr(x, f.name) for f in fields(Finding)}
                     for x in r.findings],
        "identities": list(r.identities),
        "seats_responded": list(r.seats_responded),
        "seats_silent": [list(x) for x in r.seats_silent],
    }


def write_round(run_dir, r: Round) -> str:
    """Write the round's record and return its content hash. ON RECEIPT, before any
    classification — §13: "Findings are durable state, not model memory."

    WRITE-ONCE. `exclusive_write` rather than `atomic_write`, for the manifest's reason: a
    round that was answered and then re-recorded is a review whose findings changed after the
    fact, and the `ready`/`review_blocked` transition reads this file.
    """
    if not isinstance(r, Round):
        raise ReviewError(f"a Round is required, not {type(r).__name__}")
    path = findings_path(run_dir, r.round)
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = (json.dumps(_payload(r), sort_keys=True, indent=2) + "\n").encode("utf-8")
    try:
        storage.exclusive_write(path, blob)
    except FileExistsError as e:
        raise ReviewError(
            f"{path} already records round {r.round}'s findings and is never rewritten: the "
            "terminal transition reads this file, and a second write would let a run report "
            "an outcome and then change it") from e
    return hashlib.sha256(blob).hexdigest()


def read_round(run_dir, round_: int) -> Round:
    """The round's record, type-checked. Raises when it is absent or unreadable."""
    path = findings_path(run_dir, round_)
    try:
        raw = path.read_bytes()
    except FileNotFoundError as e:
        raise ReviewError(
            f"{path} does not exist, so round {round_}'s findings were never recorded. §13's "
            "transition reads the record rather than the return value, and a missing record "
            "is `outcome_unknown` — never a clean round.") from e
    try:
        row = json.loads(raw)
    except ValueError as e:
        raise ReviewError(f"{path} is not readable as JSON: {e}") from e
    names = ("round", "checkpoint", "findings", "identities", "seats_responded",
             "seats_silent")
    if not isinstance(row, dict):
        raise ReviewError(f"{path} holds {type(row).__name__}, not a round record")
    missing = [n for n in names if n not in row]
    if missing:
        raise ReviewError(f"{path} is missing {missing}")
    unknown = sorted(set(row) - set(names))
    if unknown:
        raise ReviewError(f"{path} carries fields this engine does not know: {unknown}")
    try:
        findings = tuple(Finding(**f) for f in row["findings"])
    except TypeError as e:
        raise ReviewError(f"{path} carries a finding this engine cannot read: {e}") from e
    return Round(round=row["round"], checkpoint=row["checkpoint"],
                 findings=findings,
                 identities=tuple(row["identities"]),
                 seats_responded=tuple(row["seats_responded"]),
                 seats_silent=tuple(tuple(x) for x in row["seats_silent"]))


def _result_text(record) -> tuple:
    """The FULL answer this reviewer gave, or `(None, reason)`.

    NOT `record["result_text"]`, which `run_provider` passes through `_truncate`
    (`engine.py:1263`). A long, correct review whose fenced block fell past the cut would
    read as `unreadable_findings`, and this module's whole contract is that "could not be
    read" and "found nothing" stay apart — so a truncation defect must not manufacture one.

    AND THE BYTES PARSED ARE THE BYTES THAT WERE VALIDATED, which re-reading a PATH does not
    give you by itself. `run_provider` validates an answer — length floor, sentinel quote —
    then writes it and hands back the path; this function read that path back with nothing
    binding the two. REPRODUCED: with the record unchanged, rewriting the file between the
    two reads returned the new content and `""` for the reason, so a `[blocker]` replaced by
    "this candidate is clean" parsed exactly as cleanly as the original.

    THE WINDOW IS INSIDE THE ROUND AND IS NOT HYPOTHETICAL. `run_council` waits for every
    provider, so the race is not with this call — it is BETWEEN REVIEWERS: seat A writes its
    result and exits, seat B runs on as the same user with a shell, and the council workdir
    (`<run-dir>/review/round-N/council/`) sits OUTSIDE the tree the round brackets, so a write
    there moves no digest anywhere.

    A MISSING DIGEST IS A REFUSAL, not a pass. `run_provider` records one for every seat as of
    the same change that added this check, so a record without one did not come from this
    engine — and "nobody could show these are the validated bytes" must not read as "here is
    what the reviewer said". Both failures come back as a REASON, which `run_round` turns into
    a SILENT seat: a seat whose answer cannot be trusted said nothing, and §13 counts it as
    nothing rather than as a clean review.
    """
    path = record.get("result_file")
    if not isinstance(path, str) or not path:
        return None, "unreadable_result_file"
    try:
        blob = Path(path).read_bytes()
    except OSError:
        return None, "unreadable_result_file"
    want = record.get("result_sha256")
    if not isinstance(want, str) or not want:
        return None, "result_digest_missing"
    if hashlib.sha256(blob).hexdigest() != want:
        return None, "result_file_changed"
    return blob.decode("utf-8", errors="replace"), ""


def run_round(run_dir, *, round_: int, checkout, checkpoint: str, baseline_commit: str,
              baseline_tree: str, artifact_manifest, log, other_clones,
              names=tuple(engine.DEFAULT_PROVIDERS), cfg=None,
              run_council=engine.run_council, build=engine.build_real_spec,
              probe=fingerprint.build, make_token=engine.make_sentinel) -> Round:
    """One review round: plant the inputs, convene the panel, record what came back.

    ORDER, AND WHAT EACH STEP MAKES TRUE. The ledger assertion runs BEFORE anything is
    written or spent, so a run whose blindness cannot be guaranteed costs nothing. Then the
    inputs, then §14.1's write-ahead intent, then the panel, then the record — written ON
    RECEIPT and before any classification, because §13's named failure is a compaction between
    "round 2 returned" and "the orchestrator classified it" leaving `--collect` unable to tell
    two OPPOSITE terminal states apart.

    THE RETURN VALUE IS A CONVENIENCE; THE RECORD IS THE FACT. The terminal transition reads
    `read_round`, not this — a caller that trusts the return value learns nothing about
    whether the round survived to disk.

    `probe` and `run_council` are injected and every test passes a fake: §5.2 prices a real
    panel in provider calls and a suite that spends them is one nobody runs.
    """
    assert_ledger_is_out_of_reach(run_dir, checkout=checkout, other_clones=other_clones)
    token = make_token()
    task_present = Path(taskbundle_task_dir(checkout)).is_dir()
    inputs = write_reviewer_inputs(checkout, round_, checkpoint=checkpoint,
                                   baseline_commit=baseline_commit,
                                   baseline_tree=baseline_tree,
                                   artifact_manifest=artifact_manifest, token=token,
                                   task_bundle_present=task_present)
    # AFTER the inputs are laid down: the token file is now inside the tree, and the check
    # above proved only that the LEDGER is not. Re-asserting here would re-scan for the same
    # bytes and find the same answer, so it is deliberately not repeated — what changed is
    # this module's own files, whose contents this module wrote.
    prompt = launcher_prompt(inputs)
    workdir = round_dir(run_dir, round_) / "council"
    specs = reviewer_specs(list(names), prompt=prompt, timeout=REVIEW_TIMEOUT_SEC,
                           cwd=checkout, token=token, workdir=workdir, cfg=cfg, build=build)
    op = f"review-round-{round_}"
    log.record(journalmod.intent(COUNCIL_KIND), operation_id=op, round=round_,
               checkpoint=checkpoint, reviewers=sorted(s.name for s in specs))
    manifest = run_council(
        specs,
        retries=0,                      # §13: --retries 0, and `gate.quote` priced it that way
        timeout=REVIEW_TIMEOUT_SEC,     # the number, not the label — see REVIEW_TIMEOUT_SEC
        backoff=0.0,
        workdir=workdir,                # NEVER run_dir: same filename as the run manifest
        prompt=None,                    # its sha256 is not any seat's identity (§11)
        requested=[s.name for s in specs],
        mode="deep",
        read_only=False,                # nothing here applies a read-only posture; say so
        install_signal_handler=False,   # its handler os._exit()s past the `done` record
        env=reviewer_env(),             # no reviewer writes bytecode into the reviewed tree
    )
    findings, identities, responded, silent = [], [], [], []
    dropped_rows = []
    by_name = {r.get("name"): r for r in manifest.get("providers", [])}
    for spec in specs:
        record = by_name.get(spec.name)
        identities.append(fingerprint.as_row(probe(
            prompt=prompt, token=token, cli=spec.name,
            bundle_sha256=None, model_requested=spec.model, model_reported=None)))
        if record is None:
            silent.append((spec.name, "no_record"))
            continue
        if not record.get("valid"):
            silent.append((spec.name, str(record.get("reason") or "invalid")))
            continue
        text, why = _result_text(record)
        if text is None:
            silent.append((spec.name, why))
            continue
        rows, why = parse_findings(text)
        if rows is None:
            silent.append((spec.name, "unreadable_findings"))
            continue
        responded.append(spec.name)
        if why:
            # A ROW WAS DROPPED, NOT THE SEAT: `parse_findings` kept every properly-evidenced
            # finding in this answer and only skipped the malformed row(s) (see its own
            # docstring). That must not vanish just because the seat otherwise responded
            # cleanly — the journal is the record of what actually happened this round, and
            # `responded` alone would read as if nothing had been dropped.
            dropped_rows.append((spec.name, why))
        for row in rows:
            f = Finding(
                id=finding_id(round_, spec.name, row["severity"], row["claim"],
                              row["evidence"]),
                round=round_, seat=spec.name, severity=row["severity"],
                claim=row["claim"], evidence=row["evidence"], resolution="open")
            # A BYTE-IDENTICAL REPEAT IS ONE FINDING, and dropping the second is the only
            # thing this record can represent: the id is content-derived, so the two are the
            # same row and `Round` refuses the pair. Restating a blocker verbatim is ordinary
            # model output, and the version without this crashed `loop` at
            # `write_resolutions` — after §5.2's three-provider panel had been spent.
            if any(f.id == prior.id for prior in findings):
                continue
            findings.append(f)
    r = Round(round=round_, checkpoint=checkpoint,
              findings=tuple(findings), identities=tuple(identities),
              seats_responded=tuple(sorted(responded)),
              seats_silent=tuple(sorted(silent)))
    digest = write_round(run_dir, r)
    log.record(journalmod.done(COUNCIL_KIND), operation_id=op, round=round_,
               findings_sha256=digest, responded=len(responded), silent=len(silent),
               dropped_rows=dropped_rows)
    return r


# ONE STRING FOR ONE PREDICATE. §13 and §14.2 say "verified but not independently REVIEWED";
# §13.1 says "RE-reviewed". Two spellings of one fact is two things a reader has to notice
# are the same, and a grep for either finds half the run's states. Every module that reports
# this imports the constant.
VERIFIED_NOT_INDEPENDENTLY_REVIEWED = "verified but not independently reviewed"

READY = "ready"
DEGRADED = "degraded"
REVIEW_BLOCKED = "review_blocked"
TERMINALS = (READY, DEGRADED, REVIEW_BLOCKED)

_BLOCKER = "blocker"
# EVERY MEMBER OF `RESOLUTIONS` IS READ, and this pair is why. The version that spelled the
# blocking set `("open", "unresolved")` left `rejected` matching NO branch: the blocker went
# into neither roll-up, vanished from the verdict, and the run's headline read
# `ready` — "every panel whole and no blocker left open" — over a blocker somebody had
# dismissed. `rejected` BLOCKS. A `Resolution` records no rationale and no author, so
# "somebody considered this finding and rejected it" and "nobody acted on this finding" are
# the same bytes on disk, and `review_blocked` is what §13's last paragraph needs to mean the
# second one. A rejection worth acting on is a recorded human judgement and belongs on the
# ledger, where §10.1's method axis can carry who made it.
_BLOCKING = ("open", "unresolved", "rejected")
_FIXED = "fixed"
_READINGS = (*_BLOCKING, _FIXED)


@dataclass(frozen=True)
class Resolution:
    """What happened to one finding, and whether the fix survived the gate.

    `verified` IS NOT DERIVABLE FROM `resolution`, and the pair is checked here. §13 requires
    a fix to be re-verified in a fresh clone and a fix that BREAKS verify to be reverted with
    the finding reported unresolved — so `fixed` with `verified=False` describes a state §13
    forbids, and a record carrying it would let a reverted change count as a repair.
    """
    finding_id: str
    resolution: str
    checkpoint: str | None
    verified: bool

    def __post_init__(self) -> None:
        if self.resolution not in RESOLUTIONS:
            raise ReviewError(f"resolution is one of {list(RESOLUTIONS)}, "
                              f"not {self.resolution!r}")
        if not isinstance(self.verified, bool):
            raise ReviewError(f"verified is a bool, not {self.verified!r}")
        if self.resolution == "fixed":
            if not self.verified:
                raise ReviewError(
                    "a finding recorded `fixed` was verified: §13 reverts a fix that breaks "
                    "verify and reports the finding unresolved, so `fixed` and unverified is "
                    "a state this run may not be in")
            if not isinstance(self.checkpoint, str) or not self.checkpoint.strip():
                raise ReviewError(
                    "a fix names the checkpoint it produced; §13 cuts a fresh checkpoint "
                    "after every fix and §14.1 makes git the ordering of record")


def resolutions_path(run_dir, round_: int) -> Path:
    return round_dir(run_dir, round_) / "resolutions.json"


def _one_row_per_finding(rows, where: str) -> None:
    """A finding is resolved once, or this record has no reading.

    `terminal_from_record` keys the round's resolutions BY FINDING ID, so a second row for one
    finding does not conflict — it silently replaces the first, and `fixed` beside `open` for
    the same blocker resolves to whichever the writer happened to put last. Refused on the way
    out and again on the way back in, because the two routes are different: the write covers
    this module's own producer, and the read covers the file a resumed run and `--collect`
    actually classify from.
    """
    ids = [r.finding_id for r in rows]
    repeated = sorted({i for i in ids if ids.count(i) > 1})
    if repeated:
        raise ReviewError(
            f"{where}: {repeated} carry more than one resolution. The terminal keys this "
            "record by finding id, so the extra rows do not conflict — they overwrite, and a "
            "blocker recorded both `fixed` and `open` is read as whichever came last.")


def write_resolutions(run_dir, round_: int, rows) -> str:
    """Record what one round's fix pass did. Write-once, for `write_round`'s reason."""
    rows = tuple(rows)
    wrong = sorted({type(r).__name__ for r in rows if not isinstance(r, Resolution)})
    if wrong:
        raise ReviewError(f"resolutions are Resolution records, not {wrong}")
    path = resolutions_path(run_dir, round_)
    _one_row_per_finding(rows, str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = (json.dumps([{f.name: getattr(r, f.name) for f in fields(Resolution)}
                        for r in rows], sort_keys=True, indent=2) + "\n").encode("utf-8")
    try:
        storage.exclusive_write(path, blob)
    except FileExistsError as e:
        raise ReviewError(f"{path} already records round {round_}'s resolutions and is never "
                          "rewritten") from e
    return hashlib.sha256(blob).hexdigest()


def read_resolutions(run_dir, round_: int):
    """This round's resolutions, or `None` when no fix pass ran.

    `None` RATHER THAN `()`. A round that recorded no fix and a round whose fix resolved
    nothing are different facts, and only one of them means the blockers are still open
    because nobody tried. `terminal_from_record` reads both as blocking, but it says which.
    """
    path = resolutions_path(run_dir, round_)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        rows = json.loads(raw)
    except ValueError as e:
        raise ReviewError(f"{path} is not readable as JSON: {e}") from e
    if not isinstance(rows, list):
        raise ReviewError(f"{path} holds a list of resolutions, not "
                          f"{type(rows).__name__}")
    try:
        out = tuple(Resolution(**r) for r in rows)
    except TypeError as e:
        # `read_round`'s rule, one record over: a row this engine cannot construct is a
        # refusal in this module's own vocabulary rather than a TypeError out of a dataclass.
        raise ReviewError(f"{path} carries a resolution this engine cannot read: {e}") from e
    _one_row_per_finding(out, str(path))
    return out


# ------------------------------------------- the checkout the reviewers were told not to touch
#
# §13's reviewers are WRITE-CAPABLE and run CONCURRENTLY inside the tree they are reviewing:
# this module applies no read-only posture (see the module docstring for each thing it
# deliberately does not call) and `run_council` runs its members in a thread pool, so all
# three sit in the synthesis checkout at once. Until this pair of measurements, the only thing
# between them and the change under review was `launcher_prompt`'s sentence — a check the
# party being checked could walk around, with the reviewer standing in the builder's place.
#
# WHAT IT BUYS AND WHAT IT DOES NOT. It DETECTS a checkout that moved while the panel was in
# it; it does not PREVENT the write. The round's findings are then inadmissible and the run
# refuses, rather than reporting a terminal over a review of a tree that changed underneath
# it. Detection was chosen over `engine.make_readonly` deliberately: nothing here can show
# that a headless plan-mode reviewer can still run the `git diff` the bundle asks for, and
# finding out costs real provider calls — a posture that silently blinds the panel is worse
# than a measurement that catches the panel writing. This one costs no provider call at all.
#
# FOUR THINGS IT DOES NOT COVER, each written down because a guard whose gaps are unstated is
# read as covering them:
#
#   * THE GIT DIRECTORY IS NOT MEASURED — `snapshot.take` prunes `.git`, and this passes that
#     default deliberately. Git writes in there on the reviewer's behalf during the very
#     command the bundle asks for: `git diff` refreshes and rewrites the index's stat cache.
#     A measurement ranging over it would fire on every round that did as it was told, and a
#     check that always fires is not a check. The reviewer inputs and §20's task bundle live
#     there too. `test_git_doing_what_the_bundle_asks_does_not_disturb_the_checkout` pins both
#     halves of that trade.
#     INDEX CHURN IS THE REASON, NOT THE RISK. What the exclusion actually leaves open is
#     `.git/hooks/`: a write-capable reviewer can drop an executable there, and it outlives
#     the round — every later git command in this checkout is the repository's OTHER program
#     running. What bounds that is `gitcmd.NO_HOOKS`, the `-c core.hooksPath=/dev/null` preset
#     every hook-firing call in this package carries before its subcommand (`gitcmd.py:89`,
#     held to by `test_every_hook_firing_call_carries_the_hooks_pin`). The repo-local
#     `.git/config` is the same gap one field over: `core.fsmonitor` names a program too, and
#     it survives both `NO_HOOKS` (which only redirects `core.hooksPath`) and `NO_USER_CONFIG`
#     (which only blanks the GLOBAL and SYSTEM files, never the repository's own). What bounds
#     THAT is `gitcmd.NO_DAEMON_CACHE`'s `-c core.fsmonitor=false` (`gitcmd.py:50`), held to by
#     `test_every_index_loading_call_carries_the_daemon_cache_flags`. So the exclusion is
#     bounded by a defence rather than merely accepted — for FORGE's git calls, on both
#     programs. It bounds nothing run outside that convention: the reviewer's own `git diff`,
#     and the operator's commands in this checkout afterwards, still fire whatever is there.
#   * A WRITE OUTSIDE THE CHECKOUT is invisible. A reviewer has a shell and a whole
#     filesystem; this answers one question about one tree.
#   * A WRITE THAT WAS UNDONE byte-for-byte before the panel returned leaves nothing behind.
#     The measurement is content-keyed, not event-keyed.
#   * IT NAMES NO CULPRIT, and cannot. Any process that touched the checkout while the panel
#     ran lands here, including the operator's own editor. That is the fail-closed direction:
#     the round is refused, never attributed.
WORKTREE_KIND = "review_worktree"

# How many changed paths the completion record carries. The list is a SAMPLE for the sentence
# and never the verdict — see `record_worktree_after`.
_CHANGED_SAMPLE = 20

# `snapshot.take`'s default and NOTHING ELSE. This tuple used to carry `__pycache__`,
# `.pytest_cache`, `.mypy_cache` and `.ruff_cache` as well, on the argument that running the
# suite is the cheapest and likeliest thing a reviewer does with the shell §13 gives it and
# that a burnt three-provider panel is too high a price for "bytes no reviewer wrote by hand
# and no diff would show".
#
# THAT COST WAS STATED WRONG, AND MEASURED SO. A file written into `__pycache__` is bytecode
# Python executes IN PREFERENCE TO the untouched source beside it: an honest `.pyc` whose
# 16-byte header still names the real source's mtime and size, with a different code object
# marshalled in behind it, ran while `m.py` on disk said something else. So the entry was not
# a cache exclusion; it was an allow-listed, unmeasured location FROM WHICH THE CODE UNDER
# REVIEW COULD BE REPLACED, inside a tree three write-capable reviewers share — and the act
# the exclusion existed to permit, running the suite, is the same act that loads the planted
# bytecode. One reviewer poisons it, the other two get a green suite over code that is not the
# code, and the bracket reports the round undisturbed.
#
# THE CAUSE IS FIXED INSTEAD OF THE SYMPTOM: `reviewer_env` sets `PYTHONDONTWRITEBYTECODE=1`
# and adds `-p no:cacheprovider` to `PYTEST_ADDOPTS` in the panel's own child environment, so
# a reviewer that runs the suite writes neither directory. Nothing then needs exempting, and a
# cache directory that appears anyway is a fact about the round worth refusing it over.
#
# THE SAME ATTACK, RELOCATED OUTSIDE THE TREE. `PYTHONDONTWRITEBYTECODE` stops a NEW `.pyc`
# from being written; it says nothing about one already sitting wherever
# `PYTHONPYCACHEPREFIX` points CPython to look, which need not be under `checkout` at all —
# `worktree_identity` below only ever ranges over the tree it is handed. `reviewer_env` copies
# `os.environ` wholesale, so an ambient value there is not one reviewer's problem: it is
# handed to all three, one shared, external location the code under review can be replaced
# from without ever touching a byte this bracket measures. `reviewer_env` now drops the
# variable rather than leaving it to whatever the ambient environment happened to carry.
#
# WHAT THAT COSTS, written down because a guard whose gaps are unstated is read as covering
# them — and this one now states a cost in the other direction: a reviewer that runs a tool
# whose cache this environment does NOT suppress (`mypy`, `ruff`) writes into the tree and
# burns that round. Their caches were on the removed list with no measurement behind them —
# the comment argued pytest and named two more — and an unmeasured writable location inside
# the reviewed tree is the thing this whole bracket exists to notice. The round is refused,
# never attributed (see `WORKTREE_KIND`), so the answer is to re-run the round.
_SKIP_DIRS = (".git",)


def reviewer_env(base=None, *, repo_path=None) -> dict:
    """The environment §13's panel runs under: `base`, with the writes a reviewer's ordinary
    tools make into the tree BY SIDE EFFECT switched off at the source, and every redirector
    that decides WHICH source a reviewer's tools read closed or rebuilt from a measured set.

    WHY AT THE SOURCE. The bracket around each round is the only thing standing between three
    concurrent write-capable reviewers and the checkout under review, and every name exempted
    from it is a place inside that tree where a write leaves no record. `__pycache__` was
    exempted and is executable (see `_SKIP_DIRS`); the exemption is gone and this is what
    replaces it. `scripts/mutate.py` sets the same variable for the same class of problem —
    a process whose own import would otherwise write the bytes it is about to measure.

    THREE NAMES, EACH DOING ONE THING. `PYTHONDONTWRITEBYTECODE` is read by every CPython
    child at startup and stops `__pycache__`; `-p no:cacheprovider` disables the pytest plugin
    that writes `.pytest_cache` at the rootdir, and is APPENDED to whatever `PYTEST_ADDOPTS`
    the caller already had rather than replacing it — an operator's `--rootdir=…` is theirs,
    and dropping it would change what the reviewer's suite does as a side effect of a guard.
    `PYTHONPYCACHEPREFIX` is DROPPED rather than set, and is the reason this is three names
    and not two: `PYTHONDONTWRITEBYTECODE` stops a NEW `.pyc` being written, but says nothing
    about one already sitting wherever this variable points CPython to look — which need not
    be under the checkout at all. An ambient value here relocates the exact attack the
    `__pycache__` exemption used to permit to a mirrored tree entirely OUTSIDE what
    `worktree_identity` ranges over, and since `base` defaults to a wholesale copy of
    `os.environ`, one ambient value reaches every reviewer this call builds a spec for — one
    shared, external location the source under review can be replaced from. Dropped, not
    pinned to a safe value: CPython treats an unset `PYTHONPYCACHEPREFIX` as "use the default
    beside the source", which is the one location `_SKIP_DIRS` no longer exempts.

    THE `HOSTILE_ENV` STRIP IS SPELLED HERE, NOT DELEGATED TO `fleet.scrub_env`. That function
    was read before this was written, and it answers a DIFFERENT direction: it drops values
    that point BACK into a `repo_path` it is given, which is a seat accidentally reusing the
    checkout it was cloned FROM. A reviewer redirected to a location that has nothing to do
    with any checkout — an ambient `GIT_DIR`, a `GIT_CONFIG_COUNT` injector — is the opposite
    problem, so `gitcmd.HOSTILE_ENV` is imported and applied directly instead of restated,
    which is what keeps this reviewer strip and `fleet.forge_child_env`'s seat strip reading
    the same list rather than two lists a future change can drift apart. `repo_path` stays in
    the signature: when a caller supplies one, `fleet.scrub_env` is still the right function
    for what it actually does, and is applied on top of the strip above. `run_round` passes
    none today — the only repository it has at that call is the checkout UNDER review, and
    scrubbing values that point into the one tree a reviewer is there to read would remove
    access the panel needs rather than access it should not have.

    PATH IS REBUILT, NEVER DELETED. A reviewer with no `PATH` cannot find `git`, its own
    toolchain, or itself, and the round fails for a reason that reads as a provider failure
    after the round is already paid for. The rebuilt value is `os.defpath` plus the resolved
    parent directory of each of `claude`/`codex`/`agy` that resolves — under the CALLER's own
    `PATH` at call time, not one read from `base`: `base` may be a synthetic dict a caller or
    a test composed with no `PATH` in it at all, and the three binaries live wherever THIS
    process's own shims put them, not wherever `base` claims. If none of the three resolves,
    this raises rather than handing back a `PATH` that is silently unusable.

    `PYTHONPATH`/`NODE_OPTIONS` ARE EMPTIED; `PYTHONHOME` IS DELETED — the three are not
    interchangeable. An empty `PYTHONPATH` or `NODE_OPTIONS` is unambiguous: an interpreter
    reads zero additional search entries the same way whether the variable is empty or unset.
    `PYTHONHOME` is measured rather than assumed: on this machine (CPython 3.14.4, both the
    system interpreter and a fresh venv) `PYTHONHOME=""` and an unset `PYTHONHOME` produced
    identical `sys.prefix` and `sys.path`. That equivalence is this build's behaviour, not a
    documented guarantee every CPython release shares, and this function does not spend a
    reviewer's round finding out it does not hold on whatever interpreter actually ran the
    panel — `pop` is read as "no home" on every implementation, by construction, so it is
    what this drops rather than a value that merely tested the same today.

    `NODE_OPTIONS` REACHES `claude` AND NO OTHER REVIEWER. Measured on this machine: `claude`
    resolves through `node_modules/@anthropic-ai/claude-code` and reads `NODE_OPTIONS`
    (`--require` preloads a script before its own code runs) like any Node process; `codex` is
    a static musl binary and `agy` is a Go binary, and neither is Node. Emptying this variable
    closes one of three reviewers' preload hole and does nothing for the other two — a comment
    claiming it protects all three would be a comment asserting something the code does not do.

    THE SAME SHAPE ONE BRANCH OVER: THE DYNAMIC LINKER, NOT THE INTERPRETER. `PATH`,
    `PYTHONPATH`/`PYTHONHOME` and `NODE_OPTIONS` are one mechanism — an interpreter or module
    loader told where to read code from — and `LD_PRELOAD`/`LD_LIBRARY_PATH` are the same
    hazard one layer down, at the ELF dynamic linker `ld.so` consults before ANY of a binary's
    own code runs. Measured on this machine: `claude` resolves to a dynamically linked
    executable (`claude.exe`, not a wrapper around a separate `node`) and `agy` is
    dynamically linked against glibc; `LD_PRELOAD=<a built .so>` printed from a constructor
    that ran before `--version` printed anything, for BOTH. `codex` is confirmed statically
    linked (`ldd` reports "statically linked", no interpreter entry at all) and is unreached —
    a THIRD reach pattern from `NODE_OPTIONS`'s, covering the other two seats instead of one,
    which is exactly why a single "protects all three" sentence would be wrong for any of the
    three variables in this docstring and none of them get one. Both are EMPTIED, not popped:
    `ld.so` parses each as a delimited list, and an empty string parses to zero entries — no
    "prefix of everything" reading exists for a list, which is what makes emptying safe here
    where `PYTHONHOME` is not. This is a MEASURED PAIR, not a re-opened enumeration: the same
    question was asked of `GOPATH` (irrelevant — it steers `go build`, not a binary already
    compiled, and `agy` ships as one), `RUBYLIB`/`PERL5LIB`/`CLASSPATH` (irrelevant — none of
    the three CLIs is a Ruby, Perl or JVM program), and `PYTHONSTARTUP` (irrelevant outside an
    interactive REPL, which none of the three CLIs' own subprocesses run). `DYLD_*`, the same
    mechanism's macOS name, is unmeasured — this machine has no macOS to measure it on, and
    this docstring does not claim a machine it cannot see.

    WHAT IS STILL OPEN. `LLM_FORGE_DEPTH` is not set here: the reviewer's own recursion guard
    is `engine.child_env`'s `LLM_COUNCIL_DEPTH`, applied by `run_provider` on top of whatever
    this hands it, and forge's `/llm-forge` recursion guard remains `fleet.forge_child_env`'s
    alone — a reviewer is not a seat, gets no fresh clone and no hooks pin, and nothing here
    claims otherwise.
    """
    env = {k: v for k, v in (base if base is not None else os.environ).items()
          if k not in gitcmd.HOSTILE_ENV}
    if repo_path is not None:
        env = fleet.scrub_env(env, repo_path)
    env.update(gitcmd.NO_USER_CONFIG)

    caller_path = os.environ.get("PATH", os.defpath)
    dirs = []
    for name in ("claude", "codex", "agy"):
        found = shutil.which(name, path=caller_path)
        if found is None:
            continue
        d = os.path.dirname(os.path.abspath(found))
        if d not in dirs:
            dirs.append(d)
    if not dirs:
        raise ReviewError(
            f"none of claude, codex or agy resolves under the caller's own PATH "
            f"({caller_path!r}); a reviewer's PATH must not be silently unusable, because "
            "discovering that costs a round that is already paid for")
    env["PATH"] = os.pathsep.join((*dirs, os.defpath))

    env["PYTHONPATH"] = ""
    env["NODE_OPTIONS"] = ""
    env.pop("PYTHONHOME", None)
    env["LD_PRELOAD"] = ""
    env["LD_LIBRARY_PATH"] = ""

    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("PYTHONPYCACHEPREFIX", None)
    addopts = env.get("PYTEST_ADDOPTS") or ""
    env["PYTEST_ADDOPTS"] = f"{addopts} -p no:cacheprovider".strip()
    return env


def _inventory_digest(entries: dict) -> str:
    """One string for one tree state.

    `kind` is included where `snapshot.diff` omits it, so a path whose TYPE changed moves this
    digest even where the diff has nothing to say about it. That is why the digest is the
    authority and the diff is the explanation, and not the other way round.

    `json.dumps` at its default `ensure_ascii` escapes the lone surrogates `os.walk` puts in a
    non-UTF-8 filename, so the encode below cannot raise on one — the failure that took
    `baseline.materialize` down on an ordinary tracked link.
    """
    rows = sorted([e.path, e.digest, e.mode, e.size, e.kind] for e in entries.values())
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode("utf-8")).hexdigest()


def worktree_identity(checkout, quota) -> tuple:
    """One digest over everything a reviewer could have written in `checkout`, and the
    inventory it was taken from.

    `snapshot.take` IS THE MEASUREMENT, rather than a fourth way to describe a tree. §7.3's
    change predicate is already content-hash + mode + size and explicitly never mtime, already
    prunes `.git`, already refuses a subtree it could not list, and already ships `diff`
    beside it. This call adds NOTHING to that default — `_SKIP_DIRS` is `snapshot.take`'s own
    `(".git",)`, spelled here so that widening it is an edit in this module rather than a
    changed default somewhere else; what a previous version added, and what it cost, is
    written down there. The two nearer candidates do not fit: the baseline's filesystem
    manifest is built inside `materialize`, which also writes a ref into the USER's repo, and
    `verify.gate_surface` answers gate-defining NAMES only — a reviewer editing the source
    under review would not appear in it at all, which is the fail-open direction.

    THE QUOTA IS THE CALLER'S, on `_digests_under`'s rule: the number that decides whether a
    measurement is complete is spelled at the call site where somebody chose it, not defaulted
    where nobody did.

    A BREACH IS A REFUSAL, and it is the one fail-open this function has to close by hand:
    `snapshot.take` reports a quota breach by returning an EMPTY inventory beside the breach
    line, so two breached measurements digest identically and the round would read as
    undisturbed on the strength of two scans that inventoried nothing.
    """
    try:
        entries, breaches = snapshot.take(checkout, quota=quota, skip_dirs=_SKIP_DIRS)
    except (snapshot.SnapshotError, OSError) as e:
        raise ReviewError(
            f"{checkout} could not be inventoried whole ({e}), so whether a reviewer wrote "
            "into the tree it was reviewing is a question this round cannot answer") from e
    if breaches:
        raise ReviewError(
            f"{checkout} is over the inventory quota ({'; '.join(breaches)}). `snapshot.take` "
            "answers a breach with an EMPTY inventory, and two of those digest alike — so the "
            "round would read as undisturbed on two scans that measured nothing.")
    return _inventory_digest(entries), entries


def _worktree_operation(round_: int) -> str:
    _check_round(round_)
    return f"review-worktree-{round_}"


def _check_rounds_run(rounds_run) -> None:
    """One spelling of "a review ran at least one round", read by `worktree_disturbances` and
    `terminal_from_record`. `bool` is excluded for `_check_round`'s reason."""
    if not isinstance(rounds_run, int) or isinstance(rounds_run, bool) or rounds_run < 1:
        raise ReviewError(f"a review ran at least one round, not {rounds_run!r}")


def record_worktree_before(log, *, round_: int, digest: str, entries: int) -> None:
    """§14.1's write-ahead half: the checkout as it stood before the panel was convened.

    RECORDED AHEAD rather than held in memory and written once at the end, for §14.1's reason:
    a crash between the two halves then leaves an ORPHAN, and a round whose after-measurement
    never happened is `outcome_unknown` about its own tree. `terminal_from_record` refuses it
    exactly as it refuses an orphaned council round.
    """
    log.record(journalmod.intent(WORKTREE_KIND), operation_id=_worktree_operation(round_),
               round=round_, tree_digest=digest, entries=entries)


def record_worktree_after(log, *, round_: int, digest: str, entries: int, changed) -> None:
    """The completion half.

    `changed` IS A BOUNDED SAMPLE AND IS NEVER THE VERDICT. The two digests are, and they are
    compared by the reader; a reader that trusted a list the writer summarised would be
    reading the writer's conclusion instead of its evidence.
    """
    rows = [f"{p}: {verb}" for p, verb in sorted(dict(changed).items())]
    log.record(journalmod.done(WORKTREE_KIND), operation_id=_worktree_operation(round_),
               round=round_, tree_digest=digest, entries=entries,
               changed=rows[:_CHANGED_SAMPLE], changed_total=len(rows))


def worktree_disturbances(events, *, rounds_run: int) -> tuple:
    """Every round in 1..`rounds_run` whose checkout cannot be shown to have survived its own
    review.

    `rounds_run` IS AN ARGUMENT AND IS REQUIRED, because otherwise this function's name and
    its docstring both say "every round" while it can only answer for the rounds the journal
    HAPPENS to mention. Measured before it took one: `worktree_disturbances(()) == ()` — a run
    with no bracket at all, every round unmeasured, reported as having nothing against it.
    Totality lived one level up, in `terminal_from_record`'s own scan, which made this
    function's guarantee a fact about who called it rather than about what it checks; that is
    the exact shape the bracket contract closed for `terminal_from_record` itself, one level
    further up again. A caller cannot supply the round count silently: `None` and an omitted
    argument both raise.

    THREE WAYS A ROUND FAILS TO ACCOUNT FOR ITS OWN TREE, and none of them is silence:

      * NO COMPLETED PAIR AT ALL. A round nobody bracketed leaves byte-for-byte the journal a
        round measured clean leaves — nothing — so "the bracket did not run" would compare
        equal to "the bracket ran and the tree held".
      * A HALF-RECORDED PAIR, which `journal.orphans` finds: measured before the panel and
        never re-measured after it.
      * A DIGEST THAT IS ABSENT OR IS NOT A STRING. Both read back as `None`, and
        `None == None` is the fail-open: two unmeasured halves agree, and a tree nobody looked
        at reports as an undisturbed one.

    `journal.orphans` does the pairing, and it has already refused a repeated start and a
    `_done` with no `_start` by the time the second loop runs — so a completion here always
    has a start to compare against.
    """
    _check_rounds_run(rounds_run)
    events = tuple(events)
    out = [f"round {e.data.get('round')} ({e.operation_id}): the checkout was measured before "
           "the panel and never re-measured after it, so whether a reviewer wrote into the "
           "tree it was reviewing is unknown"
           for e in journalmod.orphans(events)
           if e.event == journalmod.intent(WORKTREE_KIND)]
    starts = {e.operation_id: e for e in events
              if e.event == journalmod.intent(WORKTREE_KIND)}
    for e in events:
        if e.event != journalmod.done(WORKTREE_KIND):
            continue
        before = starts[e.operation_id].data.get("tree_digest")
        after = e.data.get("tree_digest")
        if not all(isinstance(d, str) and d.strip() for d in (before, after)):
            out.append(
                f"round {e.data.get('round')} ({e.operation_id}): the checkout's identity was "
                f"recorded as {before!r} before the panel and {after!r} after it, and an "
                "unmeasured pair compares equal — so this round would report a tree nobody "
                "measured as one nobody touched")
            continue
        if before == after:
            continue
        sample = e.data.get("changed") or []
        total = e.data.get("changed_total")
        out.append(
            f"round {e.data.get('round')}: the checkout was {before[:12]} before the panel and "
            f"{after[:12]} after it; snapshot.diff names {total} path(s)"
            + (f", first: {', '.join(str(s) for s in sample)}" if sample else
               " — no path at all beside two different digests is a path whose TYPE moved, "
               "which snapshot.diff does not compare"))
    measured = {e.operation_id for e in events
                if e.event == journalmod.done(WORKTREE_KIND)}
    out.extend(
        f"round {n} ({_worktree_operation(n)}): no completed checkout measurement is on the "
        "journal at all, so this round cannot say its findings describe a tree that stood "
        "still — and without this it leaves exactly the record a round measured clean leaves"
        for n in range(1, rounds_run + 1)
        if _worktree_operation(n) not in measured)
    return tuple(out)


def terminal_from_record(run_dir, *, rounds_run: int, events) -> tuple:
    """§13's terminal, read off the RECORD rather than off any return value.

    §13's named failure is exactly what this function exists to prevent: "a compaction
    between 'round 2 returned' and 'the orchestrator classified it' leaves `--collect` unable
    to tell those two OPPOSITE terminal states apart, and the wrong one ships a clean header
    over an unresolved blocker." So a round whose record is missing, or whose
    `council_round_start` has no matching `_done`, or whose checkout the journal says moved
    while the panel was in it, is a REFUSAL — never a round classified out of what happens to
    be on disk.

    PRECEDENCE: `review_blocked` > `degraded` > `ready`.

      * `review_blocked` — any blocker whose effective resolution is `open`, `unresolved` or
        `rejected`, including every blocker in a round with no resolutions record at all.
        Absence of a fix record is an unfixed blocker, and so is a rejection this record
        cannot attribute to anyone.
      * `degraded` — every blocker resolved, but something about the review is weaker than a
        clean one: a blocker fixed in the LAST round (nothing re-reviewed the fix), any round
        in which SOME reviewer was silent, or any round whose record names no reviewer at all.
        A round in which EVERY reviewer was silent is `review_blocked` above, not here: zero
        responders is not a weaker review but the absence of one, and `degraded` ships.
        `VERIFIED_NOT_INDEPENDENTLY_REVIEWED` is the phrase. §13.1 and §14.2 assign different
        terminals to the same evidence — §14.2 makes a successful post-round-2 fix
        `review_blocked`, while §13.1's "no new loop, no new state" lands an otherwise-clean
        run in `ready` — and this branch is where the two are reconciled: THE TERMINAL FOLLOWS
        THE FINDING'S RESOLUTION, NOT WHICH REVIEWER RAISED IT. An unresolved blocker is
        `review_blocked`; one fixed and verified but not re-reviewed is `degraded`. So §13.1's
        ultrareview fix and §14.2's post-round-2 fix land in the same place on the same
        evidence, and `review_blocked` keeps meaning what §13's last paragraph needs it to
        mean: a blocker is still open.
      * `ready` — every blocker resolved and re-reviewed, and every round's panel whole.

    THE BRACKET IS REQUIRED, NOT MERELY BELIEVED, and the requirement is `rounds_run`'s alone
    to state: every round from 1 to it has to carry a completed `WORKTREE_KIND` pair on the
    journal, and a round that carries none is a refusal rather than a round with nothing said
    against it. The version that only refused what the journal DID say was true by call-site
    discipline — `loop` was the sole writer of these records, so the pair was always there —
    which is a guarantee held up by there being one caller rather than by anything the
    function checks. A `--collect` resume reads these records without having run the loop, and
    at that point "only `loop` writes it" stops being a fact about the program. Put as the two
    questions this package asks of every guard: a round nobody measured left the same record
    as a round measured clean, and "the bracket did not run" compared equal to "the bracket
    ran and the tree held".

    THAT CHECK IS `worktree_disturbances`' AND NOT A SECOND ONE HERE. It was a separate scan
    in this function while the round count was not an argument to that one, which left the
    public function answering "every round" about only the rounds the journal mentioned — the
    same shape one level down. Passing `rounds_run` through moves the totality into the
    function whose name claims it, and leaves one answer rather than two that may drift.

    WHAT IT STILL DOES NOT ASSERT is that the pair on the journal was taken around THIS round's
    panel rather than around nothing — the journal is forge's own append-only record and this
    reads it at face value. The four things the bracket itself cannot see are listed at
    `WORKTREE_KIND`.
    """
    _check_rounds_run(rounds_run)
    # Materialised once: two separate scans follow, and a generator would be empty for the
    # second — which is the fail-open direction for it.
    events = tuple(events)
    orphaned = [e for e in journalmod.orphans(events)
                if e.event == journalmod.intent(COUNCIL_KIND)]
    if orphaned:
        raise ReviewError(
            "a council round started and recorded no result (operation "
            f"{', '.join(sorted(e.operation_id for e in orphaned))}), so whether it returned "
            "findings is unknown. §14.1 names that `outcome_unknown` and forbids retrying it "
            "silently; a terminal chosen here would be a clean header over a round nobody read.")
    disturbed = worktree_disturbances(events, rounds_run=rounds_run)
    if disturbed:
        raise ReviewError(
            "this run's synthesis checkout cannot be shown to have survived its own review — "
            + "; ".join(disturbed) + ". §13's reviewers are write-capable, share that checkout "
            "and nothing stops them writing in it, so findings taken over a tree that moved "
            "underneath them — or over one nobody measured — describe a state this run cannot "
            "vouch for. A terminal chosen here would be the check the reviewed party could "
            "have rigged, with the reviewer holding the pen.")
    blocked, degraded = [], []
    for n in range(1, rounds_run + 1):
        r = read_round(run_dir, n)               # raises when the record is missing
        res = read_resolutions(run_dir, n)
        by_id = {x.finding_id: x for x in (res or ())}
        for f in r.findings:
            if f.severity != _BLOCKER:
                continue
            fixed = by_id.get(f.id)
            if fixed is None:
                blocked.append(
                    f"round {n}: {f.seat} raised a blocker ({f.claim!r}) and "
                    + ("no fix pass was recorded for that round"
                       if res is None else "this round's fix pass did not resolve it"))
                continue
            if fixed.resolution not in _READINGS:
                # Unreachable while `_READINGS` covers `RESOLUTIONS` — which a test asserts —
                # and here anyway, because the failure it guards is the one this whole
                # function exists to prevent: a blocker matching no branch is appended to
                # neither roll-up and the run reports `ready` over a finding it never
                # classified. A refusal is the only safe reading of an unread value.
                raise ReviewError(
                    f"round {n}: {f.seat}'s blocker carries resolution {fixed.resolution!r}, "
                    f"which §13's terminal has no reading for. Every member of "
                    f"{list(RESOLUTIONS)} must appear in `_READINGS`; one that does not "
                    "disappears from the verdict rather than failing it.")
            if fixed.resolution in _BLOCKING:
                blocked.append(f"round {n}: {f.seat}'s blocker ({f.claim!r}) is "
                               f"{fixed.resolution}"
                               + (" — a rejected finding carries no rationale and no author "
                                  "on this record, so it is not distinguishable from one "
                                  "nobody acted on" if fixed.resolution == "rejected" else ""))
            elif n == rounds_run:
                degraded.append(
                    f"round {n}: {f.seat}'s blocker ({f.claim!r}) was fixed at checkpoint "
                    f"{fixed.checkpoint} and no later round reviewed it — "
                    f"{VERIFIED_NOT_INDEPENDENTLY_REVIEWED}")
        if r.seats_silent and not r.seats_responded:
            # ZERO RESPONDERS IS NOT A WEAKER REVIEW, IT IS NO REVIEW — and `degraded` SHIPS.
            # That label tells its reader a panel looked and found the work merely imperfect;
            # here every seat timed out and nothing independent read the candidate at all.
            # §13 exists to obtain that review, so delivering the run under a label implying
            # one is a verdict reading cleaner than its evidence, in the very field whose job
            # is to carry the distinction. Measured before this branch: a round answered by
            # 0 of 3, both brackets clean, classified `degraded`.
            #
            # ON THE ROUND'S OWN RECORD, not a roll-up across rounds: a run whose round 1 was
            # whole and whose round 2 nobody answered is a run whose last word came from
            # nobody, and a check that looked at the run as a whole would miss it.
            blocked.append(
                f"round {n} was answered by 0 of "
                f"{len(r.seats_responded) + len(r.seats_silent)} reviewers ("
                + ", ".join(f"{s}: {why}" for s, why in r.seats_silent)
                + "), so nothing independent looked at this candidate. §13's review did not "
                "happen, and a run shipped as `degraded` claims one that did")
        elif r.seats_silent:
            # A PARTIAL panel stays `degraded` DELIBERATELY. One reviewer answering really is
            # a weaker review, which is what this label means; folding 1-of-3 in with 0-of-3
            # would be the same two-states-one-label defect with the states swapped.
            degraded.append(
                f"round {n} was answered by {len(r.seats_responded)} of "
                f"{len(r.seats_responded) + len(r.seats_silent)} reviewers; silent: "
                + ", ".join(f"{s} ({why})" for s, why in r.seats_silent))
        elif not r.seats_responded:
            # `reviewer_specs` refuses to convene an empty panel, and this reads the RECORD
            # one level up from it — where `Round`'s own docstring makes a panel-less round
            # legal "because a round whose panel could not be convened still has to be
            # recordable". Zero findings beside zero seats is byte-for-byte what three
            # reviewers who found nothing leave, so without this branch nobody reviewing reads
            # as everybody reviewing and finding nothing.
            degraded.append(
                f"round {n} names no reviewer at all — neither a seat that answered nor a "
                f"seat recorded silent — so its empty finding list is "
                f"{VERIFIED_NOT_INDEPENDENTLY_REVIEWED}")
    if blocked:
        return REVIEW_BLOCKED, "; ".join(blocked)
    if degraded:
        return DEGRADED, "; ".join(degraded)
    return READY, (f"{rounds_run} round(s), every panel whole and no blocker left open")


def _persist(run_dir, state, terminal: str):
    """§14's edge, taken and written down. `runstate.advance` refuses an undeclared one, so a
    caller that is not in `reviewing` fails here rather than recording a phase it never
    reached."""
    from . import runstate                       # local: runstate imports nothing from here
    new = runstate.advance(state, terminal)
    runstate.write_state(run_dir, new)
    return new


def settle(run_dir, state, *, rounds_run: int, events) -> tuple:
    """Read the record, take §14's edge, and persist the new position.

    `runstate.advance` already refuses an undeclared edge and `reviewing`'s successors already
    include all three terminals, so this adds no graph — it adds the rule that the terminal
    comes from the record.
    """
    terminal, why = terminal_from_record(run_dir, rounds_run=rounds_run, events=events)
    return _persist(run_dir, state, terminal), why


def _tree_of(checkout, commit):
    """The checkpoint's TREE oid, or None. §12.3's sighting is about content recurring.

    FAILS CLOSED TO `None`, never to `""`: an empty-string tree id used as a sighting key
    would make every unrecorded attempt "the same tree" and fire the oscillation stop signal
    on the second one.
    """
    r = gitcmd.git(checkout, "rev-parse", f"{commit}^{{tree}}",
                   env_extra=gitcmd.READONLY, check=False)
    out = r.stdout.strip()
    return out if r.returncode == 0 and out else None


def _stop(run_dir, state, *, rounds_run: int, events, note: str) -> tuple:
    """The two exits `loop` reaches by its own control flow, taken off the record anyway.

    Both have just written `unresolved` rows for every one of that round's blockers, so
    `review_blocked` is what the record says and the loop's sentence — which names the cap or
    the failed verify, facts the record cannot carry — is APPENDED to the record's rather than
    substituted for it. The disagreement is refused rather than reported: a terminal chosen
    here that the record does not also reach would be a verdict reading cleaner, or merely
    differently, than the rows this loop had just finished writing.
    """
    terminal, why = terminal_from_record(run_dir, rounds_run=rounds_run, events=events)
    if terminal != REVIEW_BLOCKED:
        raise ReviewError(
            f"{note} — and the record then classified {terminal!r}. This loop wrote every one "
            "of that round's blockers `unresolved` immediately before reading it back, so the "
            "two disagree about the same rows and neither may be reported over the other.")
    _persist(run_dir, state, terminal)
    return terminal, f"{why}; {note}"


def clone_review_tree(repo, run_dir, *, run_id: str, round_: int, checkpoint: str,
                      identity) -> Path:
    """A fresh clone at `checkpoint` for one review round. Returns the checkout path.

    WHY A CLONE AND NOT THE SYNTHESIS WORKTREE. §16 lets the synthesis tree be a linked
    worktree because its author is the trusted invoking orchestrator. §13's reviewers are not:
    they are three unattended bypass-permissions agents with `cwd` set to that tree. In a
    linked worktree `.git` is a file resolving to the parent's git directory, so `hooks/`,
    `config` and the object store are reachable by ordinary relative path — and `_SKIP_DIRS`
    prunes `.git` from the round bracket, so a reviewer that writes `.git/hooks/pre-commit`
    leaves the before and after digests identical and the round reports the tree undisturbed.
    §4 built independent clones for exactly this hazard and this is that construction, reused
    rather than restated: `fleet.clone_seat` is what carries `--no-local`, `--no-hardlinks`,
    the empty template, the config pins and `remote remove origin`.

    A SECOND `.git`-ONLY BRACKET OVER THE PARENT WAS THE ALTERNATIVE AND WAS NOT TAKEN. It
    measures the hazard rather than deleting it: it must range over the object store, which is
    the largest thing on disk and which git itself writes during the round, and it fires after
    the hook is already on disk. Detection is not containment.

    THE INDEPENDENCE IS ASSERTED, NOT ASSUMED. `.git` must be a DIRECTORY here; a file is a
    linked worktree and is the exact thing this call exists to avoid handing a reviewer. "the
    clone failed" and "the clone succeeded and is a worktree" are two different failures and
    only one of them is loud on its own.

    WHAT THIS DOES NOT CLOSE, AND IT IS SAID HERE RATHER THAN LEFT TO BE INFERRED FROM THE
    PARAGRAPHS ABOVE. The clone removes the reviewer's reach into the USER's git directory. It
    does not remove the reviewer's reach out of its own tree: this checkout is
    `<run-dir>/review/round-<n>/checkout`, and a reviewer with a shell — which the blindness
    assertion's own docstring assumes it has — reaches `../../../ledger.json`, the panel's
    in-flight `../council/<name>.result.txt`, and `../../../seats/<seat>/attempt-<n>` by
    ordinary relative path. `assert_ledger_is_out_of_reach` answers a narrower question: that
    the ledger's bytes are not INSIDE the roots it was given. The residual is inherited rather
    than introduced — the synthesis worktree this replaces sat one directory further up the
    same tree — but a §13 round convened over it is a blind review only as far as the reviewer
    chooses to be blind, and no record here says otherwise. Moving the review root outside the
    run directory, and adding it to `other_clones` so the assertion still ranges over it, is
    the close; it is not this task.
    """
    ref = REVIEW_REF.format(run_id=run_id, round_=round_)
    try:
        gitcmd.git(repo, *gitcmd.NO_DAEMON_CACHE, *gitcmd.NO_HOOKS,
                   "update-ref", ref, checkpoint, env_extra=gitcmd.READONLY)
    except gitcmd.GitError as e:
        raise ReviewError(
            f"round {round_}'s checkpoint {checkpoint} could not be named at {ref} ({e}), so "
            "there is no ref to clone the review tree from") from e

    dest = Path(round_dir(run_dir, round_)) / "checkout"
    try:
        fleet.clone_seat(repo, _AtCheckpoint(ref, checkpoint), dest,
                            name=f"review-{round_}", identity=identity)
    except fleet.FleetError as e:
        raise ReviewError(
            f"round {round_}'s review tree could not be cloned at {checkpoint} ({e}). The "
            "round is not convened — reviewing the synthesis worktree instead would put three "
            "unattended agents in a tree sharing the user's git directory.") from e

    dot = dest / ".git"
    if not dot.is_dir():
        raise ReviewError(
            f"{dot} is not a directory, so {dest} is not an independent repository and a "
            "reviewer in it can reach a git directory that is not its own. The round is not "
            "convened.")

    bundle = taskbundle.read_task_bundle_if_recorded(run_dir)
    if bundle is not None:
        # `run_round` tells the panel "there is no task bundle in this checkout" when it finds
        # none, so a review tree without one judges a candidate without the task it was given.
        # The pair is `handover.create_synthesis_worktree`'s: materialize from the run's own
        # recorded bytes, then re-derive the manifest FROM THIS TREE, so "laid down" and "laid
        # down what the manifest describes" stay two claims.
        try:
            taskbundle.materialize(bundle, storage.task_source_path(run_dir), dest)
            taskbundle.verify_materialized(bundle, dest)
        except taskbundle.TaskBundleError as e:
            raise ReviewError(
                f"round {round_}'s review tree {dest} was cloned and this run's task bundle "
                f"did not arrive in it ({e}), so the panel would judge a candidate without "
                "the task. The round is not convened.") from e
    return dest


def loop(run_dir, *, state, checkout, checkpoint: str, baseline_commit: str,
         baseline_tree: str, artifact_manifest, log, manifest, fix, other_clones,
         repo, run_id: str, identity, run=None, make_tree=clone_review_tree) -> tuple:
    """§13's bounded review loop. NOT a convergence loop, and it never buys a third round.

    §13: "Round-1 blocker → fix, verify, checkpoint, round 2. Round-2 blocker → terminal state
    `review_blocked`, regardless of verify." The bound is `manifest.review_rounds`, which is
    the number §5.2 priced, and the fix budget is `manifest.synthesis_fix_cap`, which is the
    number §5.2 priced separately — see `progress.cap_remaining` for why counting STARTS is
    what makes a crashed fix stay spent.

    `other_clones` IS PASSED STRAIGHT THROUGH to `assert_ledger_is_out_of_reach` on every
    round, and it is required here for that function's reason: the seat and verifier clone
    paths are the caller's, and an argument nobody had to supply is §13's blindness enforced
    by memory.

    `fix` IS INJECTED AND HAS NO PRODUCTION IMPLEMENTATION IN THIS PACKAGE. Its contract is
    `fix(findings, checkpoint) -> (new_checkpoint | None, verified: bool, candidate_run,
    baseline_run)`: apply the round's blockers, re-verify in a FRESH clone the builder never
    touched (§6), cut a checkpoint, say whether verify passed, and hand back the two `Run`s
    that verify produced. A `(None, False, …)` or a `(_, False, …)` answer is §13's "a fix
    that breaks verify is reverted and the finding reported unresolved" — this loop records
    that and stops rather than trying again, because a second attempt at one round's blockers
    is the convergence loop §13 refuses.

    THE LAST TWO ARE WHAT §12.3 IS MEASURED FROM, and the contract widened to carry them.
    `progress.from_runs` needs the candidate's and the baseline's gate output to say which
    failures are NEW; this call site used to hard-code `progress.Progress(None, None)`, so
    every sighting was unmeasured by construction and `progress.oscillation` — specified by
    Plan I₂, present in the package, reachable from nothing — could not fire whatever
    happened. `None` for both IS a legitimate answer: a fix implementation that runs no gate
    cannot produce a `Run`, and saying so is different from a caller that never heard of the
    field. A 2-tuple is therefore REFUSED rather than padded, because padding makes those two
    the same record and the second is a bug.

    EVERY ROUND IS BRACKETED BY A MEASUREMENT OF THE CHECKOUT, and the bracket is re-taken per
    round rather than once around the whole loop: `fix` edits that same tree between rounds,
    which is its job. See `WORKTREE_KIND` for what the bracket buys and the four things it
    does not. A round whose tree moved stops the loop where it stands — the findings are
    inadmissible, so nothing may be spent acting on them — and the refusal itself is left to
    `terminal_from_record`, which reads the pair back off the journal. A crash between the
    write and the verdict therefore reaches the same answer.

    EVERY ROUND READS A FRESH CLONE AND NEVER THE SYNTHESIS WORKTREE. `checkout` is the tree
    `fix` edits and is the handover surface §16 lets be a linked worktree on the trust of its
    author; the panel is three unattended bypass-permissions agents, which that trust does not
    cover. `make_tree` builds the round's own clone at the round's own checkpoint through §4's
    construction, and a failure there stops the loop rather than falling back — the fallback is
    the hazard.

    WHAT THE CLONE DOES NOT COVER. It removes the reviewer's reach into the USER's git
    directory; it does not remove its reach OUT of its own tree. A round convened over
    `run_dir/review/round-<n>/checkout` is one relative path from the ledger, from the panel's
    in-flight answers under `run_dir/review/round-<n>/council/` and from every candidate clone
    under `run_dir/seats/`. `assert_ledger_is_out_of_reach` is the check that ranges over that,
    and it is why `other_clones` is required rather than defaulted.

    THE PHASE IS PERSISTED ON EVERY EXIT THAT RETURNS ONE. `state` is §14's position on the way
    in — `reviewing`, or `advance` refuses the edge — and each of the three returns below goes
    through `settle` or `_stop`, both of which write the new phase before answering. A loop
    that classified a run and left it recorded as still `reviewing` is the resume hazard the
    bracket contract is about one field over: `--collect` would find a position that says the
    review had not finished and a findings record that says it had. The RETURN VALUE is still
    the terminal string rather than the new `State`, on this module's standing rule that the
    return value is a convenience and the record is the fact — a caller that needs the other
    four dimensions reads `runstate.read_state`.

    A REFUSAL LEAVES THE PHASE ALONE, and that is deliberate. Every raise below and in
    `terminal_from_record` is a run whose terminal is not known; §14.1 calls that
    `outcome_unknown` and it is not one of `reviewing`'s declared successors, so there is no
    phase to move to and `reviewing` is the honest position to be found in.
    """
    from . import progress                       # local: progress imports nothing from here
    runner = run or run_round
    rounds = getattr(manifest, "review_rounds", None)
    if not isinstance(rounds, int) or isinstance(rounds, bool) or rounds < 1:
        raise ReviewError(
            f"the run's manifest records review_rounds={rounds!r}; §5.2 priced the panel by "
            "that number and a default chosen here would convene rounds nobody paid for")
    # `for_harvest`, not `default`: the synthesis checkout carries whatever §5's setup step
    # installed, and `default`'s 5 000-file cap breaches on this repository's own worktree.
    quota = storage.Quota.for_harvest()
    current = checkpoint
    n = 0
    while n < rounds:
        n += 1
        # A FRESH TREE PER ROUND, because `fix` edits the synthesis worktree between rounds and
        # each round must be read at ITS OWN checkpoint. `make_tree` raising stops the loop
        # where it stands: a round that could not get its own tree is a round that cannot be
        # convened, and falling back to `checkout` would put three unattended reviewers in a
        # tree that shares the user's git directory — the whole hazard, reintroduced by an
        # `except`. There is deliberately no `except` here.
        tree = make_tree(repo, run_dir, run_id=run_id, round_=n, checkpoint=current,
                         identity=identity)
        before_digest, before = worktree_identity(tree, quota)
        record_worktree_before(log, round_=n, digest=before_digest, entries=len(before))
        r = runner(run_dir, round_=n, checkout=tree, checkpoint=current,
                   baseline_commit=baseline_commit, baseline_tree=baseline_tree,
                   artifact_manifest=artifact_manifest,
                   other_clones=tuple(other_clones) + (Path(checkout),), log=log)
        after_digest, after = worktree_identity(tree, quota)
        record_worktree_after(log, round_=n, digest=after_digest, entries=len(after),
                              changed=snapshot.diff(before, after))
        if after_digest != before_digest:
            break
        blockers = [f for f in r.findings if f.severity == _BLOCKER]
        if not blockers:
            break
        if n >= rounds:
            # §13's terminal case: a round-2 blocker is `review_blocked` regardless of
            # verify, and the loop does not spend a fix it cannot have re-reviewed.
            break
        remaining = progress.cap_remaining(manifest, log.read())
        if remaining <= 0:
            write_resolutions(run_dir, n, tuple(
                Resolution(f.id, "unresolved", None, False) for f in blockers))
            return _stop(run_dir, state, rounds_run=n, events=log.read(), note=(
                f"round {n} raised {len(blockers)} blocker(s) and §12.3's synthesis-fix cap "
                "is exhausted, so no fix was attempted"))
        op = f"review-fix-{n}"
        progress.record_fix_start(log, operation_id=op, tree_oid=_tree_of(checkout, current))
        answer = fix(tuple(blockers), current)
        if not isinstance(answer, tuple) or len(answer) != 4:
            # NOT PADDED. A 2-tuple padded with `None`s makes "this fix measured no run" and
            # "this fix predates the contract" the same record, and the second is a caller bug
            # while the first is an answer. §12.3's question cannot be answered from a value
            # nobody supplied, and it must not LOOK answered.
            raise ReviewError(
                "§12.3's fix contract returns four values — (checkpoint|None, verified, "
                "candidate_run|None, baseline_run|None) — and this one returned "
                f"{len(answer) if isinstance(answer, tuple) else type(answer).__name__}. The "
                "last two are what `progress.from_runs` needs; `None` for both is a fix "
                "saying it measured no run, which is an answer, and a missing pair is not.")
        new_checkpoint, verified, cand_run, base_run = answer
        # MEASURED WHEN THE FIX MEASURED, AND `Progress(None, None)` ONLY WHEN IT DID NOT.
        # The literal that used to sit here made every sighting unmeasured by construction,
        # and `oscillation`'s own rule is that a sighting with unmeasured fingerprints forms
        # NO PAIR — so the stop signal could not fire however many times the run oscillated.
        prog = (progress.from_runs(cand_run, base_run)
                if cand_run is not None and base_run is not None
                else progress.Progress(None, None))
        progress.record_fix_done(
            log, operation_id=op,
            tree_oid=(_tree_of(checkout, new_checkpoint) if new_checkpoint else None),
            prog=prog)
        if not verified or not new_checkpoint:
            write_resolutions(run_dir, n, tuple(
                Resolution(f.id, "unresolved", None, False) for f in blockers))
            return _stop(run_dir, state, rounds_run=n, events=log.read(), note=(
                f"round {n}'s fix did not pass verify, so it was reverted and its "
                f"{len(blockers)} blocker(s) are reported unresolved (§13)"))
        write_resolutions(run_dir, n, tuple(
            Resolution(f.id, "fixed", new_checkpoint, True) for f in blockers))
        current = new_checkpoint
        # §12.3's STOP SIGNAL, and it is a stop rather than a verdict. The run has returned
        # to a state it has already been in — fix A traded failure X for Y and fix B traded
        # back — so another round buys nothing.
        #
        # `break`, NOT `_stop`, and the difference is what the record says. `_stop` is for a
        # round whose blockers this loop has just written `unresolved`, and it REFUSES when
        # the record classifies anything else — measured, it raised here, because an
        # oscillating round has just written its blockers `fixed` after a verified fix. The
        # round-cap exit two branches up takes the same `break`, and for the same reason: the
        # loop stops buying rounds and `settle` owns the terminal classification, which is
        # where it belongs. Nothing here gives `oscillation` a say in the verdict.
        #
        # `OSCILLATION_UNKNOWN` IS DELIBERATELY NOT A STOP. "We could not tell" ending a run
        # that has rounds left would make every fix measuring no gate a terminal state — a
        # verdict reading cleaner than its evidence, in the one place this whole mechanism
        # exists to keep honest.
        if progress.oscillation(log.read())[0] == progress.OSCILLATING:
            break
    new, why = settle(run_dir, state, rounds_run=n, events=log.read())
    return new.phase, why
