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
    records that absence rather than inheriting a claim about it. What keeps a reviewer's
    hands off the tree is `launcher_prompt`'s instruction and nothing stronger — see the
    residual noted there.
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
from dataclasses import dataclass, fields
from pathlib import Path

from council import engine

from . import fingerprint, gitcmd, journal as journalmod, storage
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

`severity` is one of {severities}. An empty `findings` list is a valid answer and means you
found nothing — but the block must be present, because a missing block is recorded as an
answer that could not be read rather than as a clean review.
"""


def launcher_prompt(bundle_path) -> str:
    """The small prompt that points at the bundle. §13: a launcher, not the review.

    claude and agy place the prompt in ARGV (`engine.build_real_spec`), and a task plus its
    resolved closure can still hit `E2BIG` without the raw diff — so the review instructions
    live in a file and this names it.

    THE "DO NOT MODIFY" LINE IS AN INSTRUCTION, NOT A MECHANISM, and it is the only thing
    standing between three concurrently-running reviewers and the checkout they share: this
    module applies no read-only posture (see the module docstring), and `run_council` runs
    its members in a thread pool, so all three are inside the synthesis checkout at once.
    Nothing here verifies afterwards that the tree is unchanged.
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

    AN ABSENT INPUT IS STATED, NEVER OMITTED. §16's out-of-band artifact manifest is a later
    plan's artifact and §20's task bundle may not have been materialized; a reviewer told
    "there is none" can weigh that, and one that simply never sees the section cannot.
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
    in that configuration the one directory Decision 3 names above all others sat outside
    every root the scan looked at, and the scan certified the tree clean. Measured: with a
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
    two the reviewer sits in. `other_clones` is the rest of Decision 3's list — "not a seat,
    not the synthesis checkout, not a verifier clone" — whose paths this module CANNOT derive,
    because `fleet.clone_seat` takes its destination from its caller (`fleet.py:166`). It is
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
    for row in payload["findings"]:
        if not isinstance(row, dict):
            return None, f"a finding is an object, not {type(row).__name__}"
        if row.get("severity") not in SEVERITIES:
            return None, (f"a finding's severity is one of {list(SEVERITIES)}, not "
                          f"{row.get('severity')!r}")
        if not isinstance(row.get("claim"), str) or not row["claim"].strip():
            return None, "a finding carries a non-empty claim"
    return payload["findings"], ""


def finding_id(round_: int, seat: str, severity: str, claim: str) -> str:
    """Content-derived, never a counter — §10's rule for ledger rows, applied here.

    Coverage checks compare findings across rounds; if round 2 splits or inserts a finding
    and the ids shift, the comparison compares stale identity. The ROUND is in the hash
    because "the same claim, raised again after a fix" is a different fact from "the same
    claim, still open", and the resolution field is not, because a finding's id must not
    change when it is resolved.
    """
    blob = "\0".join((str(round_), seat, severity, claim)).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]


@dataclass(frozen=True)
class Finding:
    """One reviewer's one claim, at rest."""
    id: str
    round: int
    seat: str
    severity: str
    claim: str
    resolution: str

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ReviewError(f"severity is one of {list(SEVERITIES)}, "
                              f"not {self.severity!r}")
        if self.resolution not in RESOLUTIONS:
            raise ReviewError(f"resolution is one of {list(RESOLUTIONS)}, "
                              f"not {self.resolution!r}")
        for name in ("id", "seat", "claim"):
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

    IT VALIDATES ITSELF BECAUSE EVERY SIBLING RECORD IN THIS PLAN DOES — `Size`, `Decision`,
    `Progress`, `Dimensions`, `Finding`, `Resolution`, `Ultra` — and because this is the one
    the TERMINAL reads. A hand-built `Round` is exactly what `read_round` produces off disk,
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
    """
    path = record.get("result_file")
    if not isinstance(path, str) or not path:
        return None, "unreadable_result_file"
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace"), ""
    except OSError:
        return None, "unreadable_result_file"


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
    )
    findings, identities, responded, silent = [], [], [], []
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
        for row in rows:
            findings.append(Finding(
                id=finding_id(round_, spec.name, row["severity"], row["claim"]),
                round=round_, seat=spec.name, severity=row["severity"],
                claim=row["claim"], resolution="open"))
    r = Round(round=round_, checkpoint=checkpoint,
              findings=tuple(findings), identities=tuple(identities),
              seats_responded=tuple(sorted(responded)),
              seats_silent=tuple(sorted(silent)))
    digest = write_round(run_dir, r)
    log.record(journalmod.done(COUNCIL_KIND), operation_id=op, round=round_,
               findings_sha256=digest, responded=len(responded), silent=len(silent))
    return r
