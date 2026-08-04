# llm-forge Plan K — Wiring the Decision Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the seven confirmed gaps between what `llm-forge` has BUILT and what its production verbs can REACH, so the engine stops describing a run it cannot perform.

**Architecture:** Plans A–J built ~20,000 lines of tested forge modules; a 3-of-3 council review found the decision engine unwired. This plan adds production callers and refusals — a fusion brief written at `--start`, a `--ledger` verb that persists the orchestrator's §10 ledger so `--collect` can rank seats, evidence-instead-of-verdict on `--collect`, a fresh review clone that closes the linked-worktree `.git` hazard, three gate refusals the spec requires and the code never made, and the live three-provider write smoke §18 specifies. Every change is in `shared/lib/forge/**`; `marketplaces/**` is render output.

**Tech Stack:** Python 3.11+ stdlib only · `pytest` via `uvx` · `git` ≥ 2.53 through `shared/lib/forge/gitcmd.py` · `council.engine` for provider invocation · GNU Make targets in `/home/khenrix/git/khenrix-utils/Makefile`.

---

## Global Constraints

Copied verbatim from the brief. Every task's requirements implicitly include this section.

- Python **stdlib only**.
- Argv lists, never a shell.
- Git only via `gitcmd`, located by asking git, `-c` presets **before** the subcommand.
- **Fail closed.**
- **A verdict must never read cleaner than its evidence.**
- **A comment asserting something the code does not do is a defect.**
- **No test may invoke a real provider or spend money** — except G4's smoke, which is the deliberate exception and must be opt-in via its own make target, never in `verify` or `precommit`.
- **Shipped forge prose may not cite plan documents** — a detector enforces this (`tests/test_forge_packaging.py`).
- `shared/lib/forge/**` is source of truth; `marketplaces/**` is render output.
- Every task ends with `make render`, explicit-pathspec `git add` including `marketplaces`, then `make verify` and `make precommit` **run unpiped in the FOREGROUND with `$?` captured** — `make eval` is the only command that exceeds the 10-minute cap.
- Tests: `uvx --with pytest pytest -q tests/` — **bare `pytest -q` cannot collect** (leaked agy worktrees under `evals/*/workspace/`); **1839 currently passing**.
- `scripts/mutate.py` has no `--test` flag (real form `-- <cmd>`), does not decode `\n` in `--old`/`--new`, and requires a mutant that compiles.
- **Touching `shared/lib/council/` stales `llm-council`'s receipt** (gate: `fanout.py --self-test` + `make smoke-llm-council`, ~$0.22).
- **Touching `shared/lib/forge/**` stales `llm-forge`'s receipt** (gate: `DETERMINISTIC_GATED` → the `forge-handover-cli-gc-suites` suite; re-seed with `--providers claude,codex,agy`).

### Two questions every task must answer

Both shapes recurred three or more times across Plans H–J. Ask them of every collection and comparison this plan specifies:

1. **Does nothing leave the same record as nobody?** An unmeasured value must never serialize, render or compare as a measured zero/empty/absent one.
2. **Do two different failures compare equal?** Two distinct failure causes must not collapse into one indistinguishable record.

Each task below names the specific fail-open it must not have.

---

## Order of work, and what unblocks what

The ordering is not the brief's severity order, because severity is not dependency.

**The true unblock is Task 2, and the reason is mechanical.** `review.assert_ledger_is_out_of_reach` (`shared/lib/forge/review.py:363-405`) **refuses a run with no ledger on disk** — "there is no ledger … so this run cannot assert that its bytes are out of a reviewer's reach — the check would pass because the evidence is missing". It is the first statement of `review.run_round` (`review.py:820`). So §13's review cannot be wired at all, ever, until something writes a ledger into a run directory. Nothing does (`write_ledger` has zero references outside `ledger.py` and `tests/`). The same absence is why `cli._strongest` names nobody (`cli.py:448-463`). One verb closes both.

**Task 4 must precede any review wiring, and this plan therefore contains it even though it wires nothing.** Reviewers currently sit in a linked worktree of the user's own repository with `_SKIP_DIRS = (".git",)` (`review.py:1127`) — `.git` reaches the parent's `hooks/`, `config` and object store by ordinary relative path, and a reviewer writing `.git/hooks/pre-commit` leaves both bracket digests identical. If the review verb ships before the clone, the first wired review is the hazardous one. Closing it first means the wiring can never ship ahead of its containment.

**Task 1 depends on nothing and delivers immediately.** Every input to the fusion brief already exists in the per-seat records `runner._record` wrote (`runner.py:437-441`, `artifacts.paths` per attempt). It needs no ledger, no review, no new measurement.

**Task 3 changes `--collect`'s argv surface, so it lands after Task 2**, which changes what `--collect` reads off disk. Doing them in the other order means two conflicting edits to `cli.collect`.

**Tasks 5 and 6 are gate honesty** and must land before anyone quotes a real run, but nothing depends on them. **Task 7 is independent of everything** — it exercises the provider path, which none of the above touches.

### The split, stated honestly

This does not fit in ~6 tasks. **Plan K is these seven tasks. The rest of G1's spine and all of G6's resume go to Plan L**, which should be written after Task 4 merges. Plan L's scope, with the seam already named:

- **§13's review verb.** `review.loop` (`review.py:1612`) takes an injected `fix` callable with the contract `fix(findings, checkpoint) -> (new_checkpoint | None, verified: bool)` and has **no production implementation**. A production `fix` is a synthesis invocation, and §16 makes the synthesis author the trusted orchestrator, not this engine — so the verb is a round-by-round handshake (`--review` runs one round and stops; the orchestrator fixes; `--review` again), not an unattended loop. That is a design decision needing its own brainstorm, not a task here.
- **The priced synthesis verifier pass.** `gate.quote` sets `verifier_runs = seats + 1 + review_fixes` (`gate.py:269`) — the `+1` is synthesis's own, §6 requires it ("This applies identically to baseline calibration and to **synthesis verification**"), and a verifier pass costs **zero provider money**. It is deferred, not dismissed, and the argument is in Task 3's design note below.
- **G6b: resume.** `runner.run` calls `_refuse_a_second_pass` (`runner.py:1602`) and refuses re-driving whatever the first pass did, so a killed `--start` strands every provider call spent. Resume needs a phase-aware re-entry that `run` does not currently offer, plus the parallel-builder change (builders run serially at `runner.py:1700`, `for name in names`), and both belong with §14.2's crash recovery rather than bolted onto this plan.

---

## File structure

| Path | Responsibility | Task |
|---|---|---|
| `shared/lib/forge/brief.py` | **New.** §16's fusion brief: per-seat verify outcome, path sets, pairwise overlap, sole-toucher paths. Reads the run directory; spends nothing. | 1 |
| `shared/lib/forge/cli.py` | Front end. Gains `--ledger`, `--verified-at`/`--verify-exit`/`--verify-log`; loses `--synthesis-outcome`; `_strongest` resolves. | 1,2,3 |
| `shared/lib/forge/review.py` | Gains `clone_review_tree`; `loop` clones per round instead of reviewing the synthesis worktree. | 4 |
| `shared/lib/forge/gate.py` | Gains free-space refusal, wall-clock bound, setup-command provider detection. | 5,6 |
| `shared/lib/forge/runner.py` | Docstring corrections only (§10–§13 exist; the wiring did not). | 2 |
| `shared/skills/llm-forge/SKILL.md` | Operator-facing flow: the new verbs and flags. | 1,2,3,7 |
| `scripts/forge_smoke.py` | **New.** §18's live three-provider write smoke + source-hashed receipt. | 7 |
| `Makefile` | `smoke-llm-forge` target, opt-in, never in `verify`/`precommit`. | 7 |
| `tests/test_forge_brief.py` | **New.** | 1 |
| `tests/test_forge_cli.py` | Front-end behaviour. | 1,2,3 |
| `tests/test_forge_review.py` | Review clone containment. | 4 |
| `tests/test_forge_gate.py` | Gate refusals and quote lines. | 5,6 |
| `tests/test_forge_smoke.py` | **New.** Hermetic tests of the smoke's receipt logic — never invokes a provider. | 7 |

---

## Task 1: The fusion brief `--start` writes into the synthesis worktree

**Why first:** the fusion is the product, and it is currently an unscaffolded manual step reached hours later in a compacted context. Every input already exists in the seat records. This needs no ledger, no review and no new measurement.

**Files:**
- Create: `/home/khenrix/git/khenrix-utils/shared/lib/forge/brief.py`
- Create: `/home/khenrix/git/khenrix-utils/tests/test_forge_brief.py`
- Modify: `/home/khenrix/git/khenrix-utils/shared/lib/forge/cli.py` (in `start`, after `create_synthesis_worktree`, around line 264)
- Modify: `/home/khenrix/git/khenrix-utils/shared/skills/llm-forge/SKILL.md`

**Interfaces:**
- Consumes: `storage.seat_names(run_dir) -> tuple[str, ...]`; `runstate.read_seat(run_dir, name) -> dict | None`; `storage.atomic_write(path, data: bytes) -> None`; `handover.SYNTHESIS = "synthesis"`.
- Produces:
  - `brief.BRIEF: str` — the filename, `"FUSION-BRIEF.md"`.
  - `brief.UNKNOWN = None` — the sentinel for a path set nobody recorded.
  - `brief.BriefError(RuntimeError)`.
  - `brief.seat_paths(run_dir) -> dict[str, frozenset[str] | None]`
  - `brief.seat_verify(run_dir) -> dict[str, str | None]`
  - `brief.overlap(paths: dict) -> dict[tuple[str, str], int | None]`
  - `brief.sole(paths: dict) -> dict[str, list[str]]`
  - `brief.text(run_dir) -> str`
  - `brief.write(run_dir, checkout) -> Path`

**The fail-open this task must not have:** a seat whose path set could not be read must not render as a seat that touched nothing. An empty `frozenset()` makes that seat disjoint from every other, which makes every other seat's paths *sole-touched* — the brief would then tell the synthesis author "only claude touched `db.py`" when codex touched it too and nobody could read codex's record. `nothing` and `nobody` must not leave the same record (question 1), and two unreadable seats must not compare as "0 shared paths" (question 2).

- [ ] **Step 1: Write the failing tests**

Create `/home/khenrix/git/khenrix-utils/tests/test_forge_brief.py`:

```python
"""§16's fusion brief. Reads the run directory and spends nothing."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "lib"))

from forge import brief, runstate, storage  # noqa: E402


def _seat(run_dir, name, *, paths, outcome="PASS"):
    """One seat record in `runner._record`'s own shape, one attempt deep."""
    attempt = {
        "attempt": 1,
        "path": str(run_dir / "seats" / name / "attempt-1"),
        "branch": f"forge/aaaaaa/{name}",
        "sentinel": "SENTINEL-000000000000",
        "status": {"process": "ok", "artifacts": "usable", "proven_read": "proven",
                   "forge": "completed", "setup": "ok", "verify": "pass"},
        "verification": None if outcome is None else {"outcome": outcome, "reason": "r"},
        "verification_refused": None,
        "setup_run": None,
        "verifier_setup": None,
        "artifacts": ({"paths": list(paths), "origin": {}, "setup_overlap": [],
                       "verify_overlap": []}
                      if paths is not None else None),
        "candidate": {"baseline_ref": "refs/khenrix-forge/aaaaaa/base",
                      "baseline_commit": "a" * 40, "tracked_patch_bytes": 10,
                      "sidecars": [], "omitted": [], "generator_contract_id": None,
                      "gate_delta": None, "gate_surface": None},
        "launch": None,
        "prompt_identity": None,
    }
    runstate.write_seat(run_dir, name, {"name": name, "attempts": [attempt]})


def test_seat_paths_reads_the_last_attempts_path_set(tmp_path):
    _seat(tmp_path, "claude", paths=["a.py", "b.py"])
    assert brief.seat_paths(tmp_path) == {"claude": frozenset({"a.py", "b.py"})}


def test_an_unreadable_path_set_is_UNKNOWN_and_never_the_empty_set(tmp_path):
    """`nothing` and `nobody` must not leave the same record: an empty frozenset is the true
    claim "this seat changed nothing", which makes every other seat's path sole-touched."""
    _seat(tmp_path, "claude", paths=None)
    assert brief.seat_paths(tmp_path) == {"claude": brief.UNKNOWN}
    assert brief.seat_paths(tmp_path)["claude"] is not frozenset()


def test_a_seat_that_really_touched_nothing_is_the_empty_set_not_UNKNOWN(tmp_path):
    _seat(tmp_path, "claude", paths=[])
    assert brief.seat_paths(tmp_path) == {"claude": frozenset()}


def test_overlap_of_two_unknown_seats_is_None_and_never_zero(tmp_path):
    """Two different failures must not compare equal to a measured disjointness."""
    _seat(tmp_path, "claude", paths=None)
    _seat(tmp_path, "codex", paths=None)
    assert brief.overlap(brief.seat_paths(tmp_path)) == {("claude", "codex"): None}


def test_overlap_counts_shared_paths_for_two_known_seats(tmp_path):
    _seat(tmp_path, "claude", paths=["a.py", "b.py"])
    _seat(tmp_path, "codex", paths=["b.py", "c.py"])
    assert brief.overlap(brief.seat_paths(tmp_path)) == {("claude", "codex"): 1}


def test_sole_is_empty_when_any_seat_is_unknown(tmp_path):
    """"Only seat X touched db.py" is a claim about ALL seats, so it cannot be made from
    two of three — the unreadable seat is exactly the one that might also have touched it."""
    _seat(tmp_path, "claude", paths=["a.py"])
    _seat(tmp_path, "codex", paths=None)
    assert brief.sole(brief.seat_paths(tmp_path)) == {}


def test_sole_names_the_paths_exactly_one_seat_touched(tmp_path):
    _seat(tmp_path, "claude", paths=["a.py", "shared.py"])
    _seat(tmp_path, "codex", paths=["b.py", "shared.py"])
    assert brief.sole(brief.seat_paths(tmp_path)) == {"claude": ["a.py"], "codex": ["b.py"]}


def test_an_unrecorded_verify_outcome_is_None_and_not_a_failure(tmp_path):
    """§6.2 has no outcome for "nobody measured", so the brief may not invent one."""
    _seat(tmp_path, "claude", paths=["a.py"], outcome=None)
    assert brief.seat_verify(tmp_path) == {"claude": None}


def test_text_over_no_seat_is_refused(tmp_path):
    with pytest.raises(brief.BriefError, match="records no seat"):
        brief.text(tmp_path)


def test_text_says_in_words_that_an_unknown_seat_was_not_compared(tmp_path):
    _seat(tmp_path, "claude", paths=["a.py"])
    _seat(tmp_path, "codex", paths=None)
    body = brief.text(tmp_path)
    assert "not recorded" in body
    assert "no seat can be named the only one" in body


def test_write_puts_the_brief_in_the_checkout(tmp_path):
    _seat(tmp_path, "claude", paths=["a.py"])
    co = tmp_path / "synthesis"
    co.mkdir()
    p = brief.write(tmp_path, co)
    assert p == co / brief.BRIEF and p.read_text(encoding="utf-8").startswith("# Fusion brief")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uvx --with pytest pytest -q tests/test_forge_brief.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'forge.brief'` (collection error).

- [ ] **Step 3: Write `brief.py`**

Create `/home/khenrix/git/khenrix-utils/shared/lib/forge/brief.py`:

```python
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
"""
from __future__ import annotations

from pathlib import Path

from . import runstate, storage

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


def write(run_dir, checkout) -> Path:
    """Write the brief into `checkout` and return its path.

    `atomic_write`, so a brief that could not be rendered whole leaves no partial file for a
    synthesis author to fuse against.
    """
    dest = Path(checkout) / BRIEF
    storage.atomic_write(dest, text(run_dir).encode("utf-8"))
    return dest
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uvx --with pytest pytest -q tests/test_forge_brief.py`
Expected: PASS — 11 passed.

- [ ] **Step 5: Call it from `cli.start`**

In `/home/khenrix/git/khenrix-utils/shared/lib/forge/cli.py`, add `brief` to the package import list (the `from . import (fingerprint, gate, …)` block near line 34) so it reads:

```python
from . import (brief as briefmod, fingerprint, gate, gitcmd, handover, journal, launch,
               preflight, review as reviewmod, runstate, storage, taskbundle, ultra, verify)
```

Then in `start`, replace these three lines (currently at `cli.py:264-266`):

```python
    print(f"run: {run_id}", file=out)
    print(f"synthesis worktree: {synth}", file=out)
    for line in _seat_table(run_dir):
```

with:

```python
    print(f"run: {run_id}", file=out)
    print(f"synthesis worktree: {synth}", file=out)
    # THE FLEET IS ALREADY PAID FOR, SO THIS REPORTS RATHER THAN REFUSES — and "fail closed"
    # is satisfied by never writing a brief that describes a run it could not read, not by
    # destroying a run whose providers have already answered. The refusal is printed where the
    # operator is looking, and the "Next:" line below never claims a brief that is not there.
    try:
        print(f"fusion brief: {briefmod.write(run_dir, synth)}", file=out)
    except (briefmod.BriefError, OSError, storage.StorageError) as e:
        print(f"  ✗ no fusion brief was written: {e}", file=out)
    for line in _seat_table(run_dir):
```

- [ ] **Step 6: Add the front-end test**

Append to `/home/khenrix/git/khenrix-utils/tests/test_forge_cli.py`:

```python
def test_start_writes_a_fusion_brief_into_the_synthesis_worktree(tmp_path, monkeypatch,
                                                                 capsys):
    """The fusion is the product and §16 hands it to the orchestrator hours later in a
    compacted context. An unscaffolded worktree is where that goes wrong."""
    from forge import brief as briefmod
    run_id, run_dir, out = _drive_a_start(tmp_path, monkeypatch)
    synth = run_dir / handover.SYNTHESIS
    body = (synth / briefmod.BRIEF).read_text(encoding="utf-8")
    assert body.startswith("# Fusion brief")
    assert "Pairwise path overlap" in body
    assert f"fusion brief: {synth / briefmod.BRIEF}" in out
```

> `_drive_a_start` is the existing helper at the top of `tests/test_forge_cli.py`; it takes
> `monkeypatch` as a required positional and returns `(run_id, run_dir, stdout_text)`. If its
> current return shape differs, adapt the unpacking — do not change the helper.

- [ ] **Step 7: Run the front-end suite**

Run: `uvx --with pytest pytest -q tests/test_forge_cli.py tests/test_forge_brief.py`
Expected: PASS.

- [ ] **Step 8: Document the brief in SKILL.md**

In `/home/khenrix/git/khenrix-utils/shared/skills/llm-forge/SKILL.md`, in the `--start` section immediately after the paragraph beginning "`--start` runs the static preflight", add:

```markdown
It also writes **`FUSION-BRIEF.md`** into the synthesis worktree: each seat's verify outcome,
the paths each seat changed, the pairwise path-overlap matrix, and the paths exactly one seat
touched. It is membership, not rank — nothing in it says which seat is strongest. A seat whose
path set was not recorded is named as such and is excluded from the overlap counts and from
the sole-toucher list, because "only this seat touched that file" is a claim about every seat.
```

- [ ] **Step 9: Render, verify, commit**

```bash
cd /home/khenrix/git/khenrix-utils
make render
git add shared/lib/forge/brief.py shared/lib/forge/cli.py shared/skills/llm-forge/SKILL.md tests/test_forge_brief.py tests/test_forge_cli.py marketplaces
```

Then run each of these in the FOREGROUND, unpiped, capturing `$?`:

```bash
make verify
echo "verify rc=$?"
```

```bash
make precommit
echo "precommit rc=$?"
```

Both must print `rc=0`. `precommit` will report a stale `llm-forge` receipt because
`shared/lib/forge/**` changed; re-seed it before committing:

```bash
python3 scripts/eval_harness.py --seed-receipt --skill llm-forge --providers claude,codex,agy
git add evals/llm-forge/receipt.json
```

```bash
git commit -m "feat(forge): the fusion is the product, so --start scaffolds it"
```

---

## Task 2: `--ledger` — the verb that makes §10 through §13 reachable

**Why second:** `review.assert_ledger_is_out_of_reach` (`review.py:395-405`) **refuses a run with no ledger**, and it is `run_round`'s first statement. Until something writes one, §13's review cannot be wired at all. The same absence is the whole reason `cli._strongest` names nobody. One verb closes both, spends nothing, and unblocks Plan L.

**Design note — the engine does not author the ledger, and must not.** §10 says writing it "requires reading all three artifact sets"; §13 says "The orchestrator consults the ledger *after* receiving independent findings"; §16 makes the orchestrator the synthesis author. So the missing piece is not a generator — it is a verb that **accepts** an orchestrator-authored ledger, validates it through `ledger.write_ledger`'s existing refusals, and persists it where §13 and §12.5 look.

**Files:**
- Modify: `/home/khenrix/git/khenrix-utils/shared/lib/forge/cli.py` (`build_parser` ~line 743, `main` ~line 777, `_strongest` at 448-463, new `_ledger` function)
- Modify: `/home/khenrix/git/khenrix-utils/shared/lib/forge/runner.py` (module docstring line 12; `run` docstring line 1624)
- Modify: `/home/khenrix/git/khenrix-utils/tests/test_forge_cli.py`
- Modify: `/home/khenrix/git/khenrix-utils/shared/skills/llm-forge/SKILL.md`

**Interfaces:**
- Consumes: `brief.BRIEF` (Task 1); `ledger.write_ledger(run_dir, l: Ledger) -> None`; `ledger.read_ledger(run_dir) -> Ledger`; `ledger.Ledger`, `ledger.Row`, `ledger.Dependency`, `ledger.SeatEvidence`, `ledger.Criterion`, `ledger.LedgerError`; `coverage.check(l, *, tree, pytest_argv=None, run=subprocess.run) -> coverage.Report`; `coverage.CoverageError`; `rubric.dimensions_from(seat, *, report, gate_outcome, review_risk, size) -> rubric.Dimensions`; `rubric.strongest(dims) -> tuple`; `storage.ledger_path(run_dir) -> Path`.
- Produces:
  - `cli._ledger(args, *, out) -> int` — the `--ledger` verb.
  - `cli._strongest(run_dir, *, tree) -> tuple[str | None, str]` — **signature changes**: it now takes the synthesis checkout it must run coverage against. Task 3 calls it as `_strongest(run_dir, tree=synth)`.

**The fail-open this task must not have:** a ledger that decoded but whose coverage could not be computed must not produce a `strongest` of `None` with a reason that reads like the ordinary "nothing to rank" answer. Those are two different failures (question 2), and the second is a run that has all four inputs and lost one. Equally, a run with a ledger and a run without one must not reach the same sentence (question 1) — the existing two-branch reason in `_strongest` already distinguishes them and must keep doing so with a third branch added, not replaced.

- [ ] **Step 1: Write the failing tests**

Append to `/home/khenrix/git/khenrix-utils/tests/test_forge_cli.py`:

```python
def _a_ledger_file(path, *, row_id_claim="records carry a monotonic seq"):
    """§10's shape as JSON, one row, one mechanically-checkable criterion."""
    from forge import ledger as ledgermod
    rid = ledgermod.row_id("R1", row_id_claim)
    path.write_text(json.dumps({
        "version": ledgermod.VERSION,
        "union_diff_bytes": 1,
        "degraded": False,
        "rows": [{
            "id": rid,
            "requirement_id": "R1",
            "kind": "behavior",
            "component": "core",
            "semantic_claim": row_id_claim,
            "status": "accepted",
            "dependencies": [],
            "seat_evidence": [{"seat": "claude", "stance": "supports",
                               "evidence": "seq column added", "prompt_sha256": "0" * 64}],
            "counterevidence": "",
            "acceptance_criteria": [{"kind": "symbol", "path": "a.py", "symbol": "seq"}],
            "synthesis_evidence": None,
            "verification_receipt": None,
            "risk": "low",
            "rationale": "the task asks for it",
        }],
    }), encoding="utf-8")


def test_ledger_verb_persists_the_orchestrators_ledger(tmp_path, monkeypatch, capsys):
    """§13's `assert_ledger_is_out_of_reach` REFUSES a run with no ledger, so without this
    verb the review this engine prices can never be convened at all."""
    run_id, run_dir, _ = _drive_a_start(tmp_path, monkeypatch)
    src = tmp_path / "ledger.json"
    _a_ledger_file(src)
    rc = cli.main(["--ledger", run_id, "--repo", str(tmp_path / "repo"),
                   "--ledger-file", str(src)], out=io.StringIO())
    assert rc == 0 and storage.ledger_path(run_dir).exists()


def test_ledger_verb_refuses_a_ledger_the_module_will_not_write(tmp_path, monkeypatch):
    """`write_ledger`'s refusals are the validation. A verb that caught and reported them as
    "written" would put an unchecked ledger where §12.5 reads a rank off it."""
    run_id, run_dir, _ = _drive_a_start(tmp_path, monkeypatch)
    src = tmp_path / "ledger.json"
    src.write_text(json.dumps({"version": 1, "union_diff_bytes": 1, "degraded": False,
                               "rows": []}), encoding="utf-8")
    buf = io.StringIO()
    rc = cli.main(["--ledger", run_id, "--repo", str(tmp_path / "repo"),
                   "--ledger-file", str(src)], out=buf)
    assert rc == 1 and not storage.ledger_path(run_dir).exists()


def test_ledger_verb_refuses_a_run_that_does_not_exist(tmp_path):
    src = tmp_path / "ledger.json"
    _a_ledger_file(src)
    buf = io.StringIO()
    rc = cli.main(["--ledger", "zzzzzz", "--repo", str(tmp_path),
                   "--ledger-file", str(src)], out=buf)
    assert rc == 1 and "has no run" in buf.getvalue()


def test_strongest_over_a_run_with_no_ledger_says_so(tmp_path, monkeypatch):
    run_id, run_dir, _ = _drive_a_start(tmp_path, monkeypatch)
    who, why = cli._strongest(run_dir, tree=run_dir / handover.SYNTHESIS)
    assert who is None and "recorded none" in why


def test_strongest_distinguishes_an_uncomputable_coverage_from_an_absent_ledger(
        tmp_path, monkeypatch):
    """Two different failures must not compare equal: a run with all four inputs that lost
    one is not a run that never had a ledger."""
    run_id, run_dir, _ = _drive_a_start(tmp_path, monkeypatch)
    src = tmp_path / "ledger.json"
    _a_ledger_file(src)
    cli.main(["--ledger", run_id, "--repo", str(tmp_path / "repo"),
              "--ledger-file", str(src)], out=io.StringIO())
    who, why = cli._strongest(run_dir, tree=tmp_path / "does-not-exist")
    assert who is None
    assert "could not be computed" in why and "recorded none" not in why
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uvx --with pytest pytest -q tests/test_forge_cli.py -k "ledger or strongest"`
Expected: FAIL — `error: unrecognized arguments: --ledger-file` and `TypeError: _strongest() got an unexpected keyword argument 'tree'`.

- [ ] **Step 3: Add the verb to the parser**

In `/home/khenrix/git/khenrix-utils/shared/lib/forge/cli.py`, add to `build_parser`'s mutually-exclusive `verb` group (after the `--gc` line, currently ~751):

```python
    verb.add_argument("--ledger", metavar="RUN_ID",
                      help="persist the §10 claim ledger the ORCHESTRATOR authored, which "
                           "§13's review and §12.5's rank both read and neither can be "
                           "reached without")
```

and, beside the other per-verb options (after `--accept`, ~line 771):

```python
    ap.add_argument("--ledger-file", dest="ledger_file",
                    help="--ledger: the JSON file holding §10's rows. Validated by "
                         "`ledger.write_ledger`'s own refusals and written only if it passes "
                         "every one of them")
```

- [ ] **Step 4: Write the verb**

In `/home/khenrix/git/khenrix-utils/shared/lib/forge/cli.py`, add `ledger as ledgermod`, `coverage as coveragemod` and `rubric as rubricmod` to the package import block, then add this function immediately above `_gc`:

```python
def _ledger(args, *, out) -> int:
    """§10's claim ledger, authored by the ORCHESTRATOR and persisted by this engine.

    WHY THIS IS A VERB AND NOT A GENERATOR. §10 says writing the ledger "requires reading all
    three artifact sets"; §13 says the orchestrator consults it AFTER independent findings; §16
    makes the orchestrator the synthesis author. So there is nothing here for this engine to
    author, and everything for it to CHECK — which is `ledger.write_ledger`, whose refusals are
    the validation. This function adds no check of its own and catches none of its.

    WHY IT EXISTS AT ALL. `review.assert_ledger_is_out_of_reach` refuses a run whose ledger it
    cannot read, and it is `run_round`'s first statement — so without a written ledger §13's
    review is unreachable rather than merely unrun. `rubric.dimensions_from` needs a coverage
    report over the same rows, so `_strongest` names nobody for the same one reason.

    IT REFUSES A RUN THAT DOES NOT EXIST rather than opening one. `storage.run_root` creates,
    which is why `collect` removes the directory it made on that path; this does the same.
    """
    if not args.ledger_file:
        raise CliError("--ledger needs --ledger-file: the rows are the orchestrator's and "
                       "there is nothing for this engine to write without them")
    repo = Path(args.repo).resolve()
    run_dir = storage.run_root(repo, args.ledger, must_be_new=False)
    if not storage.manifest_path(run_dir).exists():
        try:
            run_dir.rmdir()
        except OSError:
            pass
        return _fail(out, [f"{repo} has no run {args.ledger!r}: nothing under XDG_STATE_HOME "
                           "records one, so there is no run to give a ledger to."])
    text = _read(args.ledger_file, f"the ledger {args.ledger_file}")
    try:
        payload = json.loads(text)
    except ValueError as e:
        raise CliError(f"the ledger {args.ledger_file} is not readable as JSON: {e}") from e
    if not isinstance(payload, dict):
        raise CliError(f"§10's ledger is an object, not a {type(payload).__name__}")
    try:
        l = ledgermod.decode_payload(payload)
        ledgermod.write_ledger(run_dir, l)
    except ledgermod.LedgerError as e:
        # NOT CAUGHT AND REPORTED AS WRITTEN. Every refusal in that module is a row §12.5 would
        # otherwise be ranked on and §13 would otherwise be asserted blind against.
        raise CliError(f"this ledger was not written: {e}") from e
    print(f"ledger: {storage.ledger_path(run_dir)} "
          f"({len(l.rows)} row(s), hash {ledgermod.ledger_hash(l)})", file=out)
    return 0
```

> **`ledger.decode_payload` may not exist under that name.** `ledger.py` has a private
> `_decode(raw, source)` at line 687 taking bytes. Before writing the call above, read
> `shared/lib/forge/ledger.py:687-760` and use the module's own public decoder if there is one.
> If `_decode` is the only route, add a three-line public wrapper in `ledger.py`:
> ```python
> def decode_payload(payload) -> Ledger:
>     """A `Ledger` out of an already-parsed object, through the ONE decoder `read_ledger`
>     uses — so a ledger handed in on the command line is checked by the same code that
>     checks one read back off disk, rather than by a second reading of the same schema."""
>     return _decode(json.dumps(payload, sort_keys=True).encode("utf-8"), "the supplied ledger")
> ```
> Match `_decode`'s actual parameter types when you write it.

- [ ] **Step 5: Route the verb in `main`**

In `main`, after the `if args.collect:` branch (~line 785), add:

```python
        if args.ledger:
            return _ledger(args, out=out)
```

and add `ledgermod.LedgerError` and `coveragemod.CoverageError` to `main`'s narrow `except` tuple, beside `reviewmod.ReviewError`.

- [ ] **Step 6: Make `_strongest` resolve**

Replace the whole of `_strongest` in `/home/khenrix/git/khenrix-utils/shared/lib/forge/cli.py` (lines 448-463) with:

```python
def _strongest(run_dir, *, tree) -> tuple:
    """§12.5's `(seat | None, why)` pair, over the ledger this run recorded.

    `tree` IS THE SYNTHESIS CHECKOUT, and it is required rather than derived: `coverage.check`
    evaluates §10.1's predicates against a real tree, and a default computed here would be this
    function choosing which tree the claims are true of.

    THREE ANSWERS, AND THEY ARE THREE BECAUSE THEY ARE THREE DIFFERENT FACTS. No ledger is a
    run that recorded nothing to rank. A ledger whose coverage could not be computed is a run
    that had all four of §12.5's inputs and lost one — a different failure, and one that must
    not read like the first. A ledger with a coverage report over it can still name nobody,
    which is the ORDINARY answer: `coverage._schema` is `unresolved` by construction and
    `coverage._prose` is `unresolved` for any untraced criterion, so a real ledger carrying
    either kind makes the report incomplete and `rubric.strongest` refuses to rank on it.

    NAMING THE BEST-DESCRIBED SEAT INSTEAD WOULD BE THE FAIL-OPEN `rubric.strongest` ITSELF
    REFUSES: "ranking the measurable ones and reporting their winner turns 'the strongest seat
    we were able to measure' into 'the strongest seat'."
    """
    if not storage.ledger_path(run_dir).exists():
        return None, ("no strongest seat can be named: §12.5 ranks seats on §10's claim "
                      "ledger and this run recorded none, so there is nothing to compare and "
                      "no dimension any seat could be ranked on")
    try:
        l = ledgermod.read_ledger(run_dir)
        report = coveragemod.check(l, tree=Path(tree))
    except (ledgermod.LedgerError, coveragemod.CoverageError, OSError) as e:
        return None, ("no strongest seat can be named: this run recorded a claim ledger and "
                      f"its coverage could not be computed ({e}). That is not the same as a "
                      "run with nothing to rank — the inputs are here and one of them was "
                      "lost, so the rank is withheld rather than taken over the rest")
    dims = []
    for line in _seat_lines(run_dir):
        dims.append(rubricmod.dimensions_from(
            line.name, report=report, gate_outcome=line.verify_outcome,
            review_risk=None, size=None))
    return rubricmod.strongest(tuple(dims))
```

> **`rubric.dimensions_from`'s exact parameter types are load-bearing.** Read
> `shared/lib/forge/rubric.py:191-224` before writing this call and pass what it names:
> `review_risk` and `size` are §13's risk and §12.1's measured size, and if that module
> refuses `None` for either, pass the honest unmeasured value it defines instead — never a
> zero, which is a measurement.

- [ ] **Step 7: Update `_strongest`'s call site**

In `collect` (~line 626), change:

```python
    strongest, agreement = _strongest(run_dir), _agreement(run_dir)
```

to:

```python
    strongest, agreement = _strongest(run_dir, tree=synth), _agreement(run_dir)
```

- [ ] **Step 8: Correct the three stale docstrings (G9)**

These three assert something the code does not do. The modules exist and are tested; what had no implementation is the **wiring**.

In `/home/khenrix/git/khenrix-utils/shared/lib/forge/runner.py` line 12, replace:

```
§5 gate priced — journalled write-ahead, driven off the run directory and nothing else, and
stopping at three verified candidates with NOTHING CHOOSING BETWEEN THEM: §10 through §13 have
no implementation, so the phase after `comparing` would be one with nothing in it.
```

with:

```
§5 gate priced — journalled write-ahead, driven off the run directory and nothing else, and
stopping at three verified candidates with NOTHING CHOOSING BETWEEN THEM. §10 through §13 are
built and tested; what this loop does not do is REACH them, and the front end is where that
edge belongs — a phase taken here would choose between candidates inside the loop that made
them.
```

In `runner.py` line 1624 (inside `run`'s docstring), replace:

```
    IT STOPS AT `comparing`, with the fleet's candidates verified on disk and NOTHING CHOOSING
    BETWEEN THEM. §10 through §13 — the claim ledger, the agreement rule, the strategy and the
    review — have no implementation, so the next edge is a phase with nothing in it.
```

with:

```
    IT STOPS AT `comparing`, with the fleet's candidates verified on disk and NOTHING CHOOSING
    BETWEEN THEM. §10 through §13 — the claim ledger, the agreement rule, the strategy and the
    review — are built and tested; this loop reaches none of them, and that is the front end's
    edge to take rather than a gap in the modules.
```

- [ ] **Step 9: Run the tests**

Run: `uvx --with pytest pytest -q tests/test_forge_cli.py tests/test_forge_runner.py`
Expected: PASS.

- [ ] **Step 10: Document the verb in SKILL.md**

In `/home/khenrix/git/khenrix-utils/shared/skills/llm-forge/SKILL.md`, add a section between the `--start` and `--collect` sections:

```markdown
## 3. `--ledger` — hand the engine the claim ledger you wrote

```bash
python3 "$FORGE" --ledger <run-id> --repo /path/to/repo --ledger-file /path/to/ledger.json
```

The ledger is **yours**, not the engine's: writing it means reading all three artifact sets,
which is the fusion work. The engine validates it and stores it — every refusal you see is a
row that would otherwise be ranked on or asserted blind against.

Do this **before** anything that reviews or ranks. A run with no ledger cannot be reviewed at
all (the blindness assertion refuses a check whose evidence is missing) and cannot name a
strongest seat, and the handover will say so in both places rather than going quiet.
```

Renumber the following sections.

- [ ] **Step 11: Render, verify, commit**

```bash
cd /home/khenrix/git/khenrix-utils
make render
git add shared/lib/forge/cli.py shared/lib/forge/ledger.py shared/lib/forge/runner.py shared/skills/llm-forge/SKILL.md tests/test_forge_cli.py marketplaces
```

```bash
make verify
echo "verify rc=$?"
```

```bash
make precommit
echo "precommit rc=$?"
```

Re-seed the receipt, then:

```bash
git commit -m "feat(forge): the ledger the orchestrator writes is the one thing §13 cannot be reached without"
```

---

## Task 3: `--collect` takes evidence, not a verdict — and refuses a fusion that is one seat

**Why third:** it changes `--collect`'s argv surface, and Task 2 changed what `--collect` reads off disk. Doing them in the other order means two conflicting edits to one function.

**The false rationale being removed.** `cli.py`'s module docstring says verifying the fusion "would be a fourth §6 pass … that §5.2 never quoted". That is false: `gate.py:270` sets `verifier_runs = seats + 1 + review_fixes` and comments the `+1` as "plus synthesis's own"; §5.2 lists "synthesis verification after each fix"; §6 says "This applies identically to baseline calibration and to **synthesis verification**". A comment asserting something the code does not do is a defect, and so is one asserting something the spec does not say.

**Design note — why the priced verifier pass is still deferred, and it is deferred rather than dismissed.** A verifier pass costs **zero provider money** (a clone, a setup command and a verify command are shell work), so cost is not the objection. The objection is sequencing: wiring it flips `Provenance.synthesis_measured` to `True`, which changes which paragraph `handover.text` prints ("Verified means" instead of "the orchestrator reports"). §13 requires "Re-run verify and cut a new checkpoint after **every** fix". If the measurement lands before the review verb, the engine measures the fusion once and then every post-review fix goes unmeasured under a header that now says "Verified" — a verdict reading cleaner than its evidence, introduced by the change meant to stop exactly that. It ships with Plan L's review verb, in the same commit, or not at all.

**What lands here instead:** the same word — "reports" — over evidence the engine can check, at zero cost.

**Files:**
- Modify: `/home/khenrix/git/khenrix-utils/shared/lib/forge/cli.py` (module docstring lines 15-21; `build_parser`; `collect`)
- Modify: `/home/khenrix/git/khenrix-utils/tests/test_forge_cli.py`
- Modify: `/home/khenrix/git/khenrix-utils/shared/skills/llm-forge/SKILL.md`

**Interfaces:**
- Consumes: `cli._rev(checkout, rev) -> str` (`cli.py:309`); `handover.branch(run_id, name) -> str`; `handover.SYNTHESIS`; `verify.PASS`, `verify.FAIL`, `verify.OUTCOMES`; `storage.seat_names(run_dir)`; `runstate.read_seat`.
- Produces:
  - `cli._reported_outcome(args) -> tuple[str | None, str | None]` — `(outcome, evidence_line)`.
  - `cli._refuse_a_seats_candidate(repo, run_dir, *, run_id, tree) -> None`.
  - New argv: `--verified-at OID`, `--verify-exit N`, `--verify-log PATH`. **`--synthesis-outcome` is removed.**

**The fail-open this task must not have:** omitting all three evidence flags must leave `synthesis_outcome=None` — the existing "no verify verdict was reported for the fusion" branch (`handover.py:737-743`) — and must never become `PASS`. Supplying *some* of them must be **refused**, not silently treated as none: a partial evidence set is a caller's mistake, and dropping it is a verdict lost while the header reads as though none was offered (question 1). And exit 1 and exit 127 must not render identically — the exit code goes into the evidence line, so a `FAIL` caused by a missing binary is distinguishable from one caused by a failing test (question 2).

- [ ] **Step 1: Write the failing tests**

Append to `/home/khenrix/git/khenrix-utils/tests/test_forge_cli.py`:

```python
def test_collect_takes_the_oid_the_verify_ran_at_and_refuses_a_stale_one(tmp_path,
                                                                         monkeypatch):
    """A verdict about a tree that is not the tree being handed over is a verdict about
    another artifact. This is checkable and costs nothing."""
    run_id, run_dir, _ = _drive_a_start(tmp_path, monkeypatch)
    buf = io.StringIO()
    rc = cli.main(["--collect", run_id, "--repo", str(tmp_path / "repo"), "--accept",
                   "--verified-at", "b" * 40, "--verify-exit", "0"], out=buf)
    assert rc == 1
    assert "is not this run's synthesis HEAD" in buf.getvalue()


def test_collect_refuses_an_oid_with_no_exit_status_beside_it(tmp_path, monkeypatch):
    """A PARTIAL evidence set is a caller's mistake. Dropping it silently loses a verdict
    while the header reads as though none was offered."""
    run_id, run_dir, _ = _drive_a_start(tmp_path, monkeypatch)
    synth = run_dir / handover.SYNTHESIS
    head = cli._rev(synth, "HEAD")
    buf = io.StringIO()
    rc = cli.main(["--collect", run_id, "--repo", str(tmp_path / "repo"), "--accept",
                   "--verified-at", head], out=buf)
    assert rc == 1 and "--verify-exit" in buf.getvalue()


def test_collect_with_no_evidence_reports_no_verdict_and_never_a_pass(tmp_path, monkeypatch):
    run_id, run_dir, _ = _drive_a_start(tmp_path, monkeypatch)
    buf = io.StringIO()
    rc = cli.main(["--collect", run_id, "--repo", str(tmp_path / "repo"), "--accept"],
                  out=buf)
    body = buf.getvalue()
    assert rc == 0 and "no verify verdict was reported" in body and "verify PASS" not in body


def test_the_evidence_line_carries_the_exact_exit_code(tmp_path, monkeypatch):
    """Two different failures must not compare equal: 127 (no such command) and 1 (a failing
    test) are both FAIL in §6.2's vocabulary and are not the same event."""
    run_id, run_dir, _ = _drive_a_start(tmp_path, monkeypatch)
    synth = run_dir / handover.SYNTHESIS
    head = cli._rev(synth, "HEAD")
    buf = io.StringIO()
    cli.main(["--collect", run_id, "--repo", str(tmp_path / "repo"), "--accept",
              "--verified-at", head, "--verify-exit", "127"], out=buf)
    assert "exit 127" in buf.getvalue()


def test_synthesis_outcome_flag_is_gone(tmp_path):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--collect", "aaaaaa", "--synthesis-outcome", "PASS"])


def test_collect_refuses_a_fusion_byte_identical_to_a_seats_candidate(tmp_path, monkeypatch):
    """The deliverable is a fusion. A tree identical to one seat's is that seat's candidate
    promoted, which is the one thing this skill exists not to do."""
    run_id, run_dir, _ = _drive_a_start(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    synth = run_dir / handover.SYNTHESIS
    seat = storage.seat_names(run_dir)[0]
    _git(repo, "update-ref", f"refs/heads/{handover.branch(run_id, handover.SYNTHESIS)}",
         _git(repo, "rev-parse", handover.branch(run_id, seat)).stdout.strip())
    _git(synth, "reset", "--hard", handover.branch(run_id, seat))
    buf = io.StringIO()
    rc = cli.main(["--collect", run_id, "--repo", str(repo), "--accept"], out=buf)
    assert rc == 1
    assert f"this is seat {seat}'s candidate, not a fusion" in buf.getvalue()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uvx --with pytest pytest -q tests/test_forge_cli.py -k "evidence or verified_at or exit_code or synthesis_outcome or byte_identical or no_verdict"`
Expected: FAIL — `unrecognized arguments: --verified-at`.

- [ ] **Step 3: Replace the false rationale in the module docstring**

In `/home/khenrix/git/khenrix-utils/shared/lib/forge/cli.py`, replace lines 15-21 (the paragraph beginning "IT DOES NOT MEASURE THE SYNTHESIS") with:

```
IT DOES NOT MEASURE THE SYNTHESIS, and the header it prints says so in words. What it takes
instead is EVIDENCE rather than a verdict: the exit status of the verify the orchestrator ran,
and the OID it ran at — refused unless that OID is this run's synthesis HEAD, because a verdict
about another tree is a verdict about another artifact. The engine still runs nothing, so
`Provenance.synthesis_measured` is the constant `False` and `handover.text` prints "who said
so" rather than §16.1's "Verified here means" paragraph. There is deliberately no flag that
flips that constant.

THE OLD RATIONALE FOR TAKING A BARE WORD WAS FALSE AND IS RECORDED HERE SO IT IS NOT RE-DERIVED.
It said a fusion verification "was never quoted". §5.2 lists synthesis verification after each
fix; §6 says the fresh-verifier rule "applies identically to baseline calibration and to
synthesis verification"; and `gate.quote` prices it — `verifier_runs = seats + 1 + review_fixes`,
where the `+1` is synthesis's own. The pass is not unquoted, it is UNWIRED, and wiring it is a
separate change because it flips the header to the "Verified" paragraph while §13 requires a
re-verify after every review fix — so measuring once and then not again would be the exact
defect that paragraph exists to prevent.
```

- [ ] **Step 4: Change the parser**

In `build_parser`, **delete** the `--synthesis-outcome` argument block entirely (lines 772-776) and add in its place:

```python
    ap.add_argument("--verified-at", dest="verified_at", metavar="OID",
                    help="--collect: the OID the orchestrator's verify ran at. Refused unless "
                         "it is this run's synthesis HEAD — a verdict about another tree is a "
                         "verdict about another artifact")
    ap.add_argument("--verify-exit", dest="verify_exit", type=int, metavar="N",
                    help="--collect: the exit status that verify returned. This engine did "
                         "not run it and the handover says so; the status is EVIDENCE, and "
                         "the exact number is printed so a 127 is not read as a 1")
    ap.add_argument("--verify-log", dest="verify_log", metavar="PATH",
                    help="--collect: the captured stdout of that run, recorded by size and "
                         "digest so the handover cites something a reader can re-derive")
```

- [ ] **Step 5: Write the evidence reader and the non-fusion refusal**

In `/home/khenrix/git/khenrix-utils/shared/lib/forge/cli.py`, add these two functions immediately above `collect`:

```python
def _reported_outcome(args, *, head: str) -> tuple:
    """`(outcome | None, evidence line | None)` out of §16's evidence flags.

    THE THREE FLAGS ARE ALL-OR-NOTHING BECAUSE A PARTIAL SET IS A CALLER'S MISTAKE AND NOT AN
    ABSENCE. Dropping a lone `--verified-at` would lose a verdict the operator meant to report
    while the header rendered "no verify verdict was reported for the fusion" — the missing
    argument and the deliberate silence spelling the same. `--verify-log` is the one optional
    member: it adds a citation and it decides nothing.

    THE MAP FROM AN EXIT CODE IS TWO-VALUED ON PURPOSE. §6.2 names four outcomes and only two
    of them can be read off one exit status; `FLAKY` is a claim about two runs and this engine
    has one number, so a flag that could assert it would be a verdict cleaner than its
    evidence. The EXACT code goes in the line, because `FAIL` at 127 (no such command) and
    `FAIL` at 1 (a failing test) are one word for two events.
    """
    at, code, log = args.verified_at, args.verify_exit, args.verify_log
    if at is None and code is None and log is None:
        return None, None
    if at is None or code is None:
        raise CliError(
            "§16's fusion evidence is `--verified-at <oid>` AND `--verify-exit <n>` together. "
            f"Got verified_at={at!r}, verify_exit={code!r}. A partial set is a verdict lost "
            "under a header that would then say none was offered, so it is refused rather "
            "than dropped.")
    if at != head:
        raise CliError(
            f"--verified-at {at} is not this run's synthesis HEAD ({head}). A verify that ran "
            "at another OID measured another tree, and §16.1's header would attribute its "
            "result to the one being handed over.")
    outcome = verify.PASS if code == 0 else verify.FAIL
    line = f"reported by the orchestrator at {head}, exit {code}"
    if log is not None:
        blob = Path(log).read_bytes() if Path(log).is_file() else None
        if blob is None:
            raise CliError(f"--verify-log {log} is not a file this engine can read; a citation "
                           "that does not resolve is worse than no citation")
        line += (f", log {len(blob)} byte(s) sha256:"
                 f"{hashlib.sha256(blob).hexdigest()[:12]}")
    return outcome, line


def _refuse_a_seats_candidate(repo, run_dir, *, run_id: str, tree: str) -> None:
    """§16: the deliverable is a FUSION. A synthesis tree identical to one seat's candidate is
    that seat promoted, which is the one thing this skill exists not to do.

    TREE OIDS, NOT DIFF BYTES. Both trees are built over B1, so "byte-identical diff" and
    "identical tree" are the same statement, and git already computed the second.

    A SEAT WHOSE BRANCH CANNOT BE RESOLVED IS REFUSED WHEN IT PRODUCED A CANDIDATE AND SKIPPED
    WHEN IT DID NOT — never skipped on both. `handover.transport_seat` is called only for a
    seat that produced one, so an absent branch is legitimate for a seat that produced
    nothing; for a seat whose record says `usable`, an absent branch is a candidate this check
    cannot compare against, and passing the check by not making the comparison is how the
    identical fusion ships.
    """
    for name in storage.seat_names(run_dir):
        b = handover.branch(run_id, name)
        try:
            seat_tree = _rev(repo, f"{b}^{{tree}}")
        except CliError:
            rec = runstate.read_seat(run_dir, name) or {}
            attempts = rec.get("attempts") or []
            status = (attempts[-1].get("status") or {}) if attempts else {}
            if status.get("artifacts") == "usable":
                raise CliError(
                    f"seat {name} recorded a usable artifact set and its branch {b} does not "
                    "resolve, so this collect cannot check whether the fusion is that seat's "
                    "candidate. §16's deliverable is a fusion and an unmade comparison is how "
                    "a promoted candidate ships.") from None
            continue
        if seat_tree == tree:
            raise CliError(
                f"the synthesis tree is byte-identical to seat {name}'s candidate: this is "
                f"seat {name}'s candidate, not a fusion. §16's deliverable is a new answer "
                "assembled from the best of all seats — if that IS the answer, say so out of "
                "band; this engine will not hand it over under a fusion's header.")
```

Add `import hashlib` to the module's import block.

- [ ] **Step 6: Wire both into `collect`**

In `collect`, immediately after the `head = _rev(synth, "HEAD")` / `tree = _rev(synth, "HEAD^{tree}")` pair, insert:

```python
    # BOTH BEFORE THE SPEND, on this function's own standing rule: every refusal it can make
    # from disk comes above §13.1's cloud review, so a run with nothing to hand over never
    # pays $5-25 to be told so.
    _refuse_a_seats_candidate(repo, run_dir, run_id=manifest.run_id, tree=tree)
    reported, evidence = _reported_outcome(args, head=head)
```

Then in the `handover.Provenance(...)` construction, replace:

```python
        seats=seats, synthesis_outcome=args.synthesis_outcome,
```

with:

```python
        seats=seats, synthesis_outcome=reported,
```

and after `body = handover.text(...)`, before `handover.write_handover`, add:

```python
    if evidence:
        # PRINTED BESIDE THE HEADER RATHER THAN INSIDE IT. `handover.text` renders §16.1, whose
        # vocabulary is fixed; this is the citation for the sentence it already prints, and it
        # names the OID and the exact exit status so a reader can re-derive both.
        body = f"{body}\n\nSynthesis verify evidence: {evidence}."
```

- [ ] **Step 7: Run the tests**

Run: `uvx --with pytest pytest -q tests/test_forge_cli.py tests/test_forge_handover.py`
Expected: PASS. Existing tests passing `--synthesis-outcome` must be updated to the evidence flags — update them, do not delete them.

- [ ] **Step 8: Update SKILL.md**

In `/home/khenrix/git/khenrix-utils/shared/skills/llm-forge/SKILL.md`, find every mention of `--synthesis-outcome` and replace the block describing it with:

```markdown
### Reporting your verify result

The engine does not run verify over the fusion, and the handover says so in words. What it
takes is **evidence**, not a word:

```bash
--verified-at <oid>      # the OID your verify ran at — refused unless it is synthesis HEAD
--verify-exit <n>        # the exit status it returned; 0 reports PASS, anything else FAIL
--verify-log <path>      # optional: the captured stdout, cited by size and digest
```

`--verified-at` and `--verify-exit` go together — a lone one is refused, because a partial
evidence set is a verdict lost under a header that would say none was offered. Pass neither
and the header reports that no verdict was given, which is the honest reading and never a PASS.

There is no `FLAKY` here: that is a claim about two runs and this is one exit status.

### The fusion must be a fusion

`--collect` refuses a synthesis tree byte-identical to any single seat's candidate, with the
sentence *"this is seat X's candidate, not a fusion."* Promoting the strongest candidate as-is
is the thing this skill exists not to do. If that genuinely is the answer, deliver it out of
band rather than under a fusion's header.
```

- [ ] **Step 9: Render, verify, commit**

```bash
cd /home/khenrix/git/khenrix-utils
make render
git add shared/lib/forge/cli.py shared/skills/llm-forge/SKILL.md tests/test_forge_cli.py marketplaces
```

```bash
make verify
echo "verify rc=$?"
```

```bash
make precommit
echo "precommit rc=$?"
```

Re-seed the receipt, then:

```bash
git commit -m "fix(forge): --collect takes the evidence a verdict rests on, and refuses a fusion that is one seat"
```

---

## Task 4: Reviewers get a fresh clone at the checkpoint OID

**Why fourth, and why it is in this plan although it wires nothing:** `_SKIP_DIRS = (".git",)` (`review.py:1127`) prunes `.git` from the round bracket, and reviewers run with `cwd=checkout` where the checkout is the **synthesis worktree — a linked worktree of the user's own repository**. In a linked worktree `.git` is a file pointing at the parent's git dir, and `hooks/`, `config` and the object store are reachable by ordinary relative path. A reviewer writing `.git/hooks/pre-commit` leaves `before_digest == after_digest` and the round reports the tree undisturbed. All three council seats found this independently. §4 built independent clones for exactly this hazard; §16's worktree exemption covers the trusted synthesis *author*, not three unattended bypass-permissions reviewers.

**The two closures, and the choice.**

*Option A — a second `.git`-only bracket over the parent.* Measures the hazard. Costs: it must range over the parent's whole git dir including the object store, which for any real repository is the largest thing on disk and grows during the round; it cannot distinguish a reviewer's write from an ordinary git write the engine itself causes; and it converts a containment property into a detection property that fires *after* the reviewer has already written the hook. §4's own note is the precedent — "The tripwire is detection, never prevention."

*Option B — review in a fresh clone at the checkpoint OID.* Deletes the class. `.git` in a clone is the clone's own, so a hook written there runs for nobody the user cares about, and the existing `_SKIP_DIRS` prune becomes correct rather than dangerous. It costs one clone per round, which the quote already prices (`clones = _CALIBRATION_CLONES + builders + verifier_runs`, and a review clone is the same construction path). It reuses `fleet.clone_seat` wholesale, so every hardening §4 paid for — `--no-local`, `--no-hardlinks`, the empty template, `remote remove origin`, the config pins — applies with no second implementation.

**Chosen: Option B.** Deleting a class beats measuring it, the cost is one clone already in the quote, and it needs no new hardening code.

**Files:**
- Modify: `/home/khenrix/git/khenrix-utils/shared/lib/forge/review.py` (new `clone_review_tree`; `loop` at 1612)
- Modify: `/home/khenrix/git/khenrix-utils/tests/test_forge_review.py`

**Interfaces:**
- Consumes: `fleet.clone_seat(repo, baseline, dest, *, name, identity, template_dir=None) -> fleet.Seat` — reads `baseline.ref` and derives the run id as `baseline.ref.split("/")[2]`; `gitcmd.git(repo, *args, env_extra=, check=, timeout=)`; `gitcmd.NO_HOOKS`, `gitcmd.READONLY`, `gitcmd.NO_DAEMON_CACHE`; `taskbundle.read_task_bundle_if_recorded(run_dir)`, `taskbundle.materialize(bundle, src, dest)`, `taskbundle.verify_materialized(bundle, dest)`; `storage.task_source_path(run_dir)`; `review.round_dir(run_dir, round_)`.
- Produces:
  - `review.REVIEW_REF = "refs/khenrix-forge/{run_id}/review-{round_}"` (format string).
  - `review.clone_review_tree(repo, run_dir, *, run_id, round_, checkpoint, identity) -> Path`
  - `review.loop(...)` gains **required** keyword-only `repo`, `run_id`, `identity`, and `make_tree=clone_review_tree`. `checkout` stays — it is now the tree `fix` edits, not the tree reviewers read.

**The fail-open this task must not have:** `loop` must never fall back to reviewing `checkout` when the clone fails. A round that could not get its own tree is a round that cannot be convened, and reviewing the worktree instead is the hazard reintroduced by an `except` clause. Equally: the clone must be verified to be an independent repository before a reviewer is launched into it (question 2 — "the clone failed" and "the clone succeeded but is a worktree" must not compare equal), asserted by reading `.git` and refusing anything that is not a directory.

- [ ] **Step 1: Write the failing tests**

Append to `/home/khenrix/git/khenrix-utils/tests/test_forge_review.py`:

```python
def test_clone_review_tree_gives_the_panel_its_own_git_directory(tmp_path):
    """In a LINKED WORKTREE `.git` is a file reaching the parent's hooks, config and object
    store by ordinary relative path — and `_SKIP_DIRS` prunes exactly that path from the
    bracket, so a reviewer writing `.git/hooks/pre-commit` leaves both digests identical."""
    repo, run_dir, run_id, head = _a_run_with_a_checkpoint(tmp_path)
    dest = review.clone_review_tree(repo, run_dir, run_id=run_id, round_=1,
                                    checkpoint=head, identity=("F", "f@e.x"))
    assert (dest / ".git").is_dir(), "a review tree must not share the user's .git"
    assert _git(dest, "rev-parse", "HEAD").stdout.strip() == head


def test_clone_review_tree_ships_no_push_target(tmp_path):
    repo, run_dir, run_id, head = _a_run_with_a_checkpoint(tmp_path)
    dest = review.clone_review_tree(repo, run_dir, run_id=run_id, round_=1,
                                    checkpoint=head, identity=("F", "f@e.x"))
    assert _git(dest, "remote").stdout.split() == []


def test_clone_review_tree_carries_the_task_bundle(tmp_path):
    """`run_round` tells the panel "there is no task bundle in this checkout" when it is
    absent, so a review tree without one judges a candidate without the task it was given."""
    repo, run_dir, run_id, head = _a_run_with_a_checkpoint(tmp_path, with_bundle=True)
    dest = review.clone_review_tree(repo, run_dir, run_id=run_id, round_=1,
                                    checkpoint=head, identity=("F", "f@e.x"))
    assert Path(review.taskbundle_task_dir(dest)).is_dir()


def test_loop_reviews_the_clone_and_never_the_worktree(tmp_path):
    seen = []

    def _runner(run_dir, *, round_, checkout, **kw):
        seen.append(Path(checkout))
        return _a_clean_round(run_dir, round_)

    repo, run_dir, run_id, head = _a_run_with_a_checkpoint(tmp_path)
    co = run_dir / "synthesis"
    review.loop(run_dir, state=_reviewing(), checkout=co, checkpoint=head,
                baseline_commit=head, baseline_tree=_git(repo, "rev-parse", "HEAD^{tree}")
                .stdout.strip(), artifact_manifest=(), log=_a_log(run_dir),
                manifest=_a_manifest(review_rounds=1), fix=lambda f, c: (c, True),
                other_clones=(), repo=repo, run_id=run_id, identity=("F", "f@e.x"),
                run=_runner)
    assert seen and all(p != co for p in seen), "reviewers must not sit in the worktree"


def test_loop_stops_rather_than_reviewing_the_worktree_when_the_clone_fails(tmp_path):
    """A round that could not get its own tree is a round that cannot be convened. An
    `except` that fell back to `checkout` is the whole hazard, reintroduced."""
    def _boom(*a, **kw):
        raise review.ReviewError("no disk")

    repo, run_dir, run_id, head = _a_run_with_a_checkpoint(tmp_path)
    with pytest.raises(review.ReviewError, match="no disk"):
        review.loop(run_dir, state=_reviewing(), checkout=run_dir / "synthesis",
                    checkpoint=head, baseline_commit=head,
                    baseline_tree=_git(repo, "rev-parse", "HEAD^{tree}").stdout.strip(),
                    artifact_manifest=(), log=_a_log(run_dir),
                    manifest=_a_manifest(review_rounds=1), fix=lambda f, c: (c, True),
                    other_clones=(), repo=repo, run_id=run_id, identity=("F", "f@e.x"),
                    make_tree=_boom)
```

> The helpers `_a_run_with_a_checkpoint`, `_a_clean_round`, `_a_log`, `_a_manifest`,
> `_reviewing` and `_git` follow the conventions already in `tests/test_forge_review.py` — read
> that file's existing fixtures at the top and reuse them rather than defining new ones. Add
> `_a_run_with_a_checkpoint` if there is no equivalent: it must build a repo, a run directory
> with a manifest, and return `(repo_path, run_dir, run_id, head_oid)`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uvx --with pytest pytest -q tests/test_forge_review.py -k "review_tree or reviews_the_clone or clone_fails"`
Expected: FAIL — `AttributeError: module 'forge.review' has no attribute 'clone_review_tree'`.

- [ ] **Step 3: Write `clone_review_tree`**

In `/home/khenrix/git/khenrix-utils/shared/lib/forge/review.py`, add near the other module constants:

```python
# The ref a review clone is taken at. §4's construction path is `--revision=<ref>`, so the
# checkpoint needs a name before it can be cloned from. Under §9's own namespace, so `--gc`'s
# walk reclaims it with everything else this run created.
REVIEW_REF = "refs/khenrix-forge/{run_id}/review-{round_}"


class _AtRef:
    """The one field `fleet.clone_seat` reads off a baseline: `.ref`.

    A shim rather than a `baseline.Baseline`, because a `Baseline` carries a base commit, a
    tracked tree oid and a filesystem manifest — three facts about B1 that are not true of a
    checkpoint, and constructing one here would state them.
    """

    def __init__(self, ref: str) -> None:
        self.ref = ref
```

and this function, above `run_round`:

```python
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
        fleetmod.clone_seat(repo, _AtRef(ref), dest, name=f"review-{round_}",
                            identity=identity)
    except fleetmod.FleetError as e:
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
```

Add whatever imports are missing to `review.py`'s import block — `fleet as fleetmod`, `taskbundle`, `storage`, `gitcmd`, `Path` — matching the module's existing import style.

- [ ] **Step 4: Make `loop` use it**

In `review.loop`'s signature (line 1612), add the four keyword-only parameters:

```python
def loop(run_dir, *, state, checkout, checkpoint: str, baseline_commit: str,
         baseline_tree: str, artifact_manifest, log, manifest, fix, other_clones,
         repo, run_id: str, identity, run=None, make_tree=clone_review_tree) -> tuple:
```

Inside the `while n < rounds:` body, immediately after `n += 1` and **before** `before_digest, before = worktree_identity(checkout, quota)`, insert:

```python
        # A FRESH TREE PER ROUND, because `fix` edits the synthesis worktree between rounds and
        # each round must be read at ITS OWN checkpoint. `make_tree` raising stops the loop
        # where it stands: a round that could not get its own tree is a round that cannot be
        # convened, and falling back to `checkout` would put three unattended reviewers in a
        # tree that shares the user's git directory — the whole hazard, reintroduced by an
        # `except`. There is deliberately no `except` here.
        tree = make_tree(run_dir, run_id=run_id, round_=n, checkpoint=current,
                         identity=identity, repo=repo) \
            if False else make_tree(repo, run_dir, run_id=run_id, round_=n,
                                    checkpoint=current, identity=identity)
```

> Write the call in one form — `make_tree(repo, run_dir, run_id=run_id, round_=n,
> checkpoint=current, identity=identity)` — matching `clone_review_tree`'s signature exactly.
> The `if False else` above is a transcription artifact; delete it.

Then change the bracket and the round call to range over `tree` rather than `checkout`:

```python
        before_digest, before = worktree_identity(tree, quota)
        record_worktree_before(log, round_=n, digest=before_digest, entries=len(before))
        r = runner(run_dir, round_=n, checkout=tree, checkpoint=current,
                   baseline_commit=baseline_commit, baseline_tree=baseline_tree,
                   artifact_manifest=artifact_manifest,
                   other_clones=tuple(other_clones) + (Path(checkout),), log=log)
        after_digest, after = worktree_identity(tree, quota)
```

> `other_clones` now carries the synthesis worktree, because `assert_ledger_is_out_of_reach`
> requires **every** clone root and the reviewer no longer sits in the one it used to derive
> from `checkout`. Read that function's docstring (`review.py:363-394`) before writing this
> line and match what it expects.

- [ ] **Step 5: Update `loop`'s docstring**

Add this paragraph to `loop`'s docstring, after the "EVERY ROUND IS BRACKETED" paragraph:

```
    EVERY ROUND READS A FRESH CLONE AND NEVER THE SYNTHESIS WORKTREE. `checkout` is the tree
    `fix` edits and is the handover surface §16 lets be a linked worktree on the trust of its
    author; the panel is three unattended bypass-permissions agents, which that trust does not
    cover. `make_tree` builds the round's own clone at the round's own checkpoint through §4's
    construction, and a failure there stops the loop rather than falling back — the fallback is
    the hazard.
```

- [ ] **Step 6: Run the review suite**

Run: `uvx --with pytest pytest -q tests/test_forge_review.py`
Expected: PASS. Existing `review.loop` call sites in the suite must gain `repo=`, `run_id=` and `identity=` — update them, do not weaken the signature by defaulting any of the three.

- [ ] **Step 7: Render, verify, commit**

```bash
cd /home/khenrix/git/khenrix-utils
make render
git add shared/lib/forge/review.py tests/test_forge_review.py marketplaces
```

```bash
make verify
echo "verify rc=$?"
```

```bash
make precommit
echo "precommit rc=$?"
```

Re-seed the receipt, then:

```bash
git commit -m "fix(forge): three unattended reviewers do not get the user's .git"
```

---

## Task 5: §4's disk rejection

**Why fifth:** nothing depends on it, but it must land before anyone quotes a real run. §4 ends "Reject the run if the disk estimate cannot sustain independent clones. Do not trade source-object safety for space." There is **no free-space read anywhere in the package** (verified: no `disk_usage`, `statvfs` or `f_bavail` in `shared/` or `scripts/`). The quote prints ~56.7 GB and nothing compares it to anything.

**Files:**
- Modify: `/home/khenrix/git/khenrix-utils/shared/lib/forge/gate.py` (new `free_bytes`, new `refuse_for_disk`; `must_show`; `open_run`)
- Modify: `/home/khenrix/git/khenrix-utils/tests/test_forge_gate.py`

**Interfaces:**
- Consumes: `storage.forge_root() -> Path`; `gate.Quote.peak_disk_gb: float`; `gate.GateError`.
- Produces:
  - `gate.free_bytes(path) -> int` — raises `GateError` when it cannot be read.
  - `gate.refuse_for_disk(quote_, *, root=None) -> str | None` — the refusal sentence, or `None`.

**The fail-open this task must not have:** an unreadable free-space figure must **not** be treated as enough space. `shutil.disk_usage` raises `OSError` for a path that does not exist, and the run directory root under `XDG_STATE_HOME` may not exist yet — so the read must walk to the nearest existing ancestor rather than swallow the error, and a genuinely unreadable path must refuse (question 1: "nobody measured" ≠ "there is room"). And "0 bytes free" must not render the same sentence as "free space could not be read" (question 2).

- [ ] **Step 1: Write the failing tests**

Append to `/home/khenrix/git/khenrix-utils/tests/test_forge_gate.py`:

```python
def test_free_bytes_walks_to_the_nearest_existing_ancestor(tmp_path):
    """The forge root under XDG_STATE_HOME does not exist before the first run, and a run
    directory's filesystem is its ancestor's."""
    assert gate.free_bytes(tmp_path / "a" / "b" / "c") > 0


def test_free_bytes_refuses_a_path_with_no_readable_ancestor(monkeypatch, tmp_path):
    def _boom(_):
        raise OSError(5, "I/O error")
    monkeypatch.setattr(gate.shutil, "disk_usage", _boom)
    with pytest.raises(gate.GateError, match="free space"):
        gate.free_bytes(tmp_path)


def test_a_run_that_does_not_fit_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(gate, "free_bytes", lambda _p: 1_000_000_000)
    q = gate.quote(_a_report(tmp_path), seats=3, attempts=3, review_rounds=2,
                   seat_timeout_sec=3600)
    why = gate.refuse_for_disk(q, root=tmp_path)
    assert why and "56" in why and "1.0 GB" in why


def test_a_run_that_fits_is_not_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(gate, "free_bytes", lambda _p: 500_000_000_000)
    q = gate.quote(_a_report(tmp_path), seats=3, attempts=3, review_rounds=2,
                   seat_timeout_sec=3600)
    assert gate.refuse_for_disk(q, root=tmp_path) is None


def test_unreadable_free_space_is_not_read_as_enough_space(monkeypatch, tmp_path):
    """"Nobody measured" must not compare equal to "there is room" — and it must not render
    the same sentence as a genuine shortfall either."""
    def _boom(_p):
        raise gate.GateError("free space at /x could not be read: I/O error")
    monkeypatch.setattr(gate, "free_bytes", _boom)
    q = gate.quote(_a_report(tmp_path), seats=3, attempts=3, review_rounds=2,
                   seat_timeout_sec=3600)
    why = gate.refuse_for_disk(q, root=tmp_path)
    assert why and "could not be read" in why and "1.0 GB free" not in why
```

> `_a_report` is the existing helper in `tests/test_forge_gate.py` that builds a
> `preflight.Report`. Reuse it. `seat_timeout_sec=` is added in Task 6 — if Task 6 has not
> landed yet, drop that keyword from these five tests and add it when Task 6 lands.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uvx --with pytest pytest -q tests/test_forge_gate.py -k "free_bytes or disk or fits"`
Expected: FAIL — `AttributeError: module 'forge.gate' has no attribute 'free_bytes'`.

- [ ] **Step 3: Write the two functions**

Add `import shutil` to `/home/khenrix/git/khenrix-utils/shared/lib/forge/gate.py`, then add these two functions above `must_show`:

```python
def free_bytes(path) -> int:
    """Free bytes on the filesystem that will hold `path`, or a refusal.

    IT WALKS TO THE NEAREST EXISTING ANCESTOR because the forge root under `XDG_STATE_HOME`
    does not exist before the first run, and a directory's filesystem is its ancestor's. This
    is not an error-swallow: the walk stops at the root, and a path with no readable ancestor
    at all RAISES.

    IT NEVER ANSWERS A NUMBER IT DID NOT READ. §4 rejects a run the disk cannot sustain, so
    "free space could not be read" answered as a large number is the refusal disarmed by its
    own failure mode — nobody measured and there is room have to be two different answers.
    """
    p = Path(path).resolve()
    last = None
    for cand in (p, *p.parents):
        try:
            return shutil.disk_usage(cand).free
        except OSError as e:
            last = e
    raise GateError(f"free space at {p} could not be read: {last}. §4 rejects a run the disk "
                    "cannot sustain, and a run whose disk this engine could not measure is "
                    "not one it can say fits.")


def refuse_for_disk(quote_, *, root=None) -> str | None:
    """§4's rejection sentence, or `None` when the run fits.

    `None` MEANS MEASURED AND SUFFICIENT AND NOTHING ELSE. Every other outcome — a shortfall,
    or a read that failed — comes back as a sentence, and the two sentences differ: a caller
    that printed one for the other would tell an operator with a broken `statvfs` that their
    disk is full, or an operator with a full disk that the engine could not look.

    THE PEAK IS THE QUOTE'S OWN AND NOT A SECOND ESTIMATE. `quote.peak_disk_gb` is what §5.2
    showed the operator; comparing against a number computed here would refuse a run on a
    figure nobody was shown.
    """
    if not isinstance(quote_, Quote):
        raise GateError(f"a Quote is required, not {type(quote_).__name__}; §4's rejection is "
                        "against the peak §5.2 showed, and there is no other peak to use")
    target = Path(root) if root is not None else storage.forge_root()
    need = int(quote_.peak_disk_gb * 1e9)
    try:
        have = free_bytes(target)
    except GateError as e:
        return (f"§4 rejects this run: {e} The quote's peak is ~{quote_.peak_disk_gb} GB and "
                "this engine could not check it against anything.")
    if have >= need:
        return None
    return (f"§4 rejects this run: it peaks at ~{quote_.peak_disk_gb} GB under {target} and "
            f"that filesystem has {have / 1e9:.1f} GB free. §4 says not to trade source-object "
            "safety for space, so the clones are not made shallow, hardlinked or shared — free "
            "space, reduce --seats or --attempts, and re-price.")
```

Add `storage` to `gate.py`'s package imports if it is not already there.

- [ ] **Step 4: Show it at the gate and refuse at `open_run`**

In `must_show`, immediately after the `lines = list(preflight.refusals(report))` block and its appended sentence, insert:

```python
    disk = refuse_for_disk(quote_)
    if disk:
        # AT THE TOP, with §2.3's refusals, because it is one: `open_run` will not open a run
        # over it and nothing below is reachable while it stands.
        lines.insert(0, disk)
```

In `open_run`, before anything is created, add:

```python
    disk = refuse_for_disk(quote_)
    if disk:
        raise GateError(disk)
```

> `open_run`'s current signature is `open_run(report, confirmation, run_id)` and does not take
> a `Quote`. Read it before editing. If it does not have one in scope, add a keyword-only
> `quote_` parameter with **no default** — a default would let a caller open a run against no
> disk check at all — and pass it from `cli.start`, which already holds `quote_`.

- [ ] **Step 5: Run the gate suite**

Run: `uvx --with pytest pytest -q tests/test_forge_gate.py tests/test_forge_cli.py`
Expected: PASS. Existing `open_run` call sites gain the new keyword — update them.

- [ ] **Step 6: Render, verify, commit**

```bash
cd /home/khenrix/git/khenrix-utils
make render
git add shared/lib/forge/gate.py shared/lib/forge/cli.py tests/test_forge_gate.py tests/test_forge_cli.py marketplaces
```

```bash
make verify
echo "verify rc=$?"
```

```bash
make precommit
echo "precommit rc=$?"
```

Re-seed the receipt, then:

```bash
git commit -m "feat(forge): §4 rejects a run the disk cannot sustain, which nothing checked"
```

---

## Task 6: The wall-clock bound, and the setup command's provider spend

Two gaps in what the gate tells the truth about, both in `gate.py`, both refusals about spend the operator is asked to approve.

**G6a.** `gate.py:328-330` says "wall clock: not quoted — a duration is measured by running the gate". §5.2 lists wall clock among what the gate must quote. A duration cannot be measured statically, but a **bound** can: builders run serially (`runner.py:1700`, `for name in names`) under `MODE_TIMEOUT["forge"]` each, so `seats × attempts × window` is a real upper bound — ~9 h on a default run, which is the number the operator most needs before answering.

**G6b.** `gate.must_show`'s own docstring already records the miss: "A SETUP command that reaches a provider CLI is not detected, and it is the more expensive miss — setup runs once per builder clone and once per verifier clone." On a default run that is **18 setup runs**. The detector exists (`gate.provider_invoking_verify`) and is simply not pointed at the setup command.

**Files:**
- Modify: `/home/khenrix/git/khenrix-utils/shared/lib/forge/gate.py` (`quote`, `must_show`)
- Modify: `/home/khenrix/git/khenrix-utils/shared/lib/forge/cli.py` (`start`)
- Modify: `/home/khenrix/git/khenrix-utils/tests/test_forge_gate.py`

**Interfaces:**
- Consumes: `gate.provider_invoking_verify(repo, command) -> tuple[str, ...]`; `verify.Command.parse(rows).steps`; `council.engine.MODE_TIMEOUT`.
- Produces:
  - `gate.quote(report, *, seats=3, attempts=3, review_rounds=2, ultrareview=True, seat_timeout_sec)` — **`seat_timeout_sec` is a required keyword with no default.**
  - `gate.must_show(report, quote_, command, *, setup)` — **`setup` is a required keyword-only `verify.Command`.**

**The fail-open this task must not have:** a missing setup command must **refuse**, never report "no provider spend in setup" — an unscreened command and a screened-clean one must not leave the same record (question 1). And the wall-clock line must be labelled a **bound**, not an estimate: a number the operator reads as "about how long this takes" when it is "the longest this can take" is a verdict cleaner than its evidence. A `seat_timeout_sec` that is not a usable positive int must refuse rather than fall back — that is §19's second-timeout-mechanism failure arriving through a default.

- [ ] **Step 1: Write the failing tests**

Append to `/home/khenrix/git/khenrix-utils/tests/test_forge_gate.py`:

```python
def test_the_quote_bounds_wall_clock_rather_than_declining_to(tmp_path):
    """§5.2 lists wall clock. A duration cannot be measured statically; a BOUND can, because
    builders run serially under one window each."""
    q = gate.quote(_a_report(tmp_path), seats=3, attempts=3, review_rounds=2,
                   seat_timeout_sec=3600)
    line = next(l for l in q.lines if l.startswith("wall clock"))
    assert "upper bound" in line and "9" in line and "not quoted" not in line


def test_the_wall_clock_line_says_what_it_excludes(tmp_path):
    q = gate.quote(_a_report(tmp_path), seats=3, attempts=3, review_rounds=2,
                   seat_timeout_sec=3600)
    line = next(l for l in q.lines if l.startswith("wall clock"))
    assert "setup" in line and "verify" in line


def test_an_unusable_seat_window_is_refused_and_never_defaulted(tmp_path):
    for bad in (0, -1, None, True, "3600"):
        with pytest.raises(gate.GateError, match="seat_timeout_sec"):
            gate.quote(_a_report(tmp_path), seats=3, attempts=3, review_rounds=2,
                       seat_timeout_sec=bad)


def test_must_show_screens_the_setup_command_too(tmp_path):
    """Setup runs 18 times on a default run — the more expensive miss, and the one the
    detector was never pointed at."""
    report = _a_report(tmp_path, with_council_makefile=True)
    q = gate.quote(report, seats=3, attempts=3, review_rounds=2, seat_timeout_sec=3600)
    lines = gate.must_show(report, q, verify.Command(steps=_steps([["true"]])),
                           setup=verify.Command(steps=_steps([["make", "precommit"]])))
    assert any("setup command" in l for l in lines)


def test_must_show_refuses_a_missing_setup_command(tmp_path):
    """An unscreened command and a screened-clean one must not leave the same record."""
    report = _a_report(tmp_path)
    q = gate.quote(report, seats=3, attempts=3, review_rounds=2, seat_timeout_sec=3600)
    with pytest.raises(gate.GateError, match="setup Command"):
        gate.must_show(report, q, verify.Command(steps=_steps([["true"]])), setup=None)
```

> `_steps` and `_a_report`'s `with_council_makefile=` variant follow the existing conventions
> in `tests/test_forge_gate.py`. Read that file's helpers and extend them rather than
> inventing new ones; the council-Makefile fixture is what the existing
> `provider_invoking_verify` tests already use.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uvx --with pytest pytest -q tests/test_forge_gate.py -k "wall_clock or seat_window or setup_command"`
Expected: FAIL — `quote() got an unexpected keyword argument 'seat_timeout_sec'`.

- [ ] **Step 3: Add the wall-clock bound to `quote`**

Change `quote`'s signature in `/home/khenrix/git/khenrix-utils/shared/lib/forge/gate.py`:

```python
def quote(report, *, seats=3, attempts=3, review_rounds=2, ultrareview=True,
          seat_timeout_sec) -> Quote:
```

Add to its docstring:

```
    `seat_timeout_sec` IS REQUIRED AND HAS NO DEFAULT. It is §19's window, and §19 exists
    because a second timeout mechanism silently degraded a three-seat panel to two. A default
    here would be this function inventing the number the bound below is computed from, which
    is the same defect one layer out.
```

After the `review_rounds = _confirmed_count(...)` line, add:

```python
    if not isinstance(seat_timeout_sec, int) or isinstance(seat_timeout_sec, bool) \
            or seat_timeout_sec < 1:
        raise GateError(
            f"seat_timeout_sec={seat_timeout_sec!r} is not a window this run can be bounded "
            "by. §19 forbids a second timeout mechanism, so this does not pick one — resolve "
            "it from `council.engine.MODE_TIMEOUT['forge']` and pass it.")
```

and after `peak_disk_gb = ...`, add:

```python
    # SERIAL, WHICH IS WHY THE PRODUCT IS A BOUND. `runner.run` drives the seats in a
    # generator over the fleet's names, one after another, and each attempt gets the whole
    # window — so `seats x attempts x window` is the longest the builders can take and is
    # reached only if every attempt runs to its cap.
    builder_hours = builders * seat_timeout_sec / 3600.0
```

Then replace the "wall clock: not quoted" line in `lines` with:

```python
        f"wall clock: builders alone have an UPPER BOUND of {builder_hours:.1f} h = "
        f"{builders} builder run(s) x {seat_timeout_sec}s, because §7's seats run one after "
        "another and each attempt gets the whole §19 window. It is a bound and not an "
        "estimate: a run whose seats all finish early takes a fraction of it. NOT INCLUDED — "
        f"the {setup_runs} setup runs, the {verify_runs} verify runs, §13's review rounds, "
        "§13.1's cloud review, and every clone. Those are shell and network time this engine "
        "cannot bound statically, so the real ceiling is above this number rather than near it",
```

- [ ] **Step 4: Screen the setup command in `must_show`**

Change `must_show`'s signature:

```python
def must_show(report, quote_, command, *, setup) -> tuple[str, ...]:
```

Replace the docstring's final paragraph ("WHAT IS NOT SCREENED …") with:

```
    BOTH COMMANDS ARE SCREENED, AND `setup` IS REQUIRED FOR THE REASON THE VERIFY COMMAND IS.
    §5.2's rule is about a command that transitively reaches a provider CLI, and it is worse
    for setup than for verify: setup runs once per builder clone AND once per verifier clone —
    18 times on a default run against 9 verifies — so an unscreened setup is the more expensive
    miss. `None` is refused rather than read as "no setup", because a command nobody screened
    and one screened clean must not leave the same record.
```

After the existing `if getattr(command, "steps", None) is None:` guard, add:

```python
    if getattr(setup, "steps", None) is None:
        raise GateError(f"a setup Command is required, not {type(setup).__name__}; §5.2's "
                        "provider-invocation rule covers it and it runs more often than verify")
```

Then after the verify-command line already in `lines`, add:

```python
    setup_findings = provider_invoking_verify(report.facts.root, setup)
    if setup_findings:
        lines.append(
            f"the setup command reaches a provider CLI in {len(setup_findings)} place(s) — "
            f"{'; '.join(setup_findings[:4])}"
            f"{' …' if len(setup_findings) > 4 else ''}. §5.2 refuses one that does, or prices "
            f"it as its own explicit line, and setup runs {quote_.setup_runs} times on this "
            "quote — so this spend is a multiple of the quote above and is not in it")
    else:
        lines.append(
            "setup command: followed to the end and no provider spend was found in it. That "
            "is this reader's answer over what it can follow, never a claim that the command "
            "is cheap")
```

- [ ] **Step 5: Update `cli.start`'s two call sites**

In `/home/khenrix/git/khenrix-utils/shared/lib/forge/cli.py`'s `start`, change:

```python
    quote_ = gate.quote(report, seats=args.seats, attempts=args.attempts,
                        review_rounds=args.review_rounds, ultrareview=not args.no_ultra)
```

to:

```python
    quote_ = gate.quote(report, seats=args.seats, attempts=args.attempts,
                        review_rounds=args.review_rounds, ultrareview=not args.no_ultra,
                        seat_timeout_sec=timeout)
```

and, after the existing `if "verify" not in answers:` guard, add:

```python
    if "setup" not in answers:
        # §5.2's provider-invocation rule covers setup and setup runs more often than verify.
        # A sheet with no setup command is not a run with no setup step — it is a run whose
        # setup nobody screened.
        raise CliError("the answer sheet names no `setup` command, and §5.2's "
                       "provider-invocation screen covers it — it runs once per builder clone "
                       "and once per verifier clone, more often than verify does")
```

and change:

```python
    for line in gate.must_show(report, quote_, command):
```

to:

```python
    for line in gate.must_show(report, quote_, command,
                               setup=verify.Command(steps=_steps(answers["setup"]))):
```

- [ ] **Step 6: Run the gate and CLI suites**

Run: `uvx --with pytest pytest -q tests/test_forge_gate.py tests/test_forge_cli.py`
Expected: PASS. Every existing `gate.quote(...)` and `gate.must_show(...)` call in the suites gains the new required keyword — update them all; do not add a default to either.

- [ ] **Step 7: Update SKILL.md's cost box**

In `/home/khenrix/git/khenrix-utils/shared/skills/llm-forge/SKILL.md`, in the `> **Cost.**` block, after the sentence ending "priced separately in **usage credits ($5–25, or one of three one-time free runs)**.", add:

```markdown
> The gate also prints a **wall-clock upper bound** for the builders — `seats × attempts ×`
> the §19 window, ~9 h on a default run, because the seats run one after another and each
> attempt gets the whole window. It is a bound, not an estimate, and it excludes setup, verify,
> review and the cloud review, so the real ceiling is above it.
```

- [ ] **Step 8: Render, verify, commit**

```bash
cd /home/khenrix/git/khenrix-utils
make render
git add shared/lib/forge/gate.py shared/lib/forge/cli.py shared/skills/llm-forge/SKILL.md tests/test_forge_gate.py tests/test_forge_cli.py marketplaces
```

```bash
make verify
echo "verify rc=$?"
```

```bash
make precommit
echo "precommit rc=$?"
```

Re-seed the receipt, then:

```bash
git commit -m "feat(forge): the gate bounds the hours and screens the command that runs eighteen times"
```

---

## Task 7: §18's live three-provider write smoke

**Why last, and why it is not optional:** `launch.py` and `runner.py` both state "**NOTHING HERE PROVES THE REAL PROVIDER PATH WORKS**". ~20,000 lines are exercised against a fake launcher; the Makefile's only live target is `smoke-llm-council` — one provider, read-only. §18 specifies the cheap version and it is **three provider calls, roughly 15% of one run's spend**. Two council seats judged it ship-blocking.

**This is the deliberate exception to "no test may spend money." It must be opt-in via its own make target and must never appear in `verify` or `precommit`.**

**Files:**
- Create: `/home/khenrix/git/khenrix-utils/scripts/forge_smoke.py`
- Create: `/home/khenrix/git/khenrix-utils/tests/test_forge_smoke.py`
- Modify: `/home/khenrix/git/khenrix-utils/Makefile`
- Modify: `/home/khenrix/git/khenrix-utils/shared/skills/llm-forge/SKILL.md`

**Interfaces:**
- Consumes: `forge.launch.make_launcher(...)`; `forge.fleet.forge_child_env(repo_path)`; `forge.gitcmd.git(...)`; `council.engine.MODE_TIMEOUT`; `council.engine.run_provider` (through `make_launcher`, never called directly).
- Produces:
  - `forge_smoke.adapter_hash() -> str` — sha256 over the adapter source closure.
  - `forge_smoke.cli_versions() -> dict[str, str | None]`
  - `forge_smoke.receipt_is_fresh(receipt: dict, *, adapter: str, versions: dict) -> tuple[bool, str]`
  - `forge_smoke.main(argv=None) -> int`
  - Receipt at `/home/khenrix/git/khenrix-utils/evals/llm-forge/smoke-receipt.json`.

**The fail-open this task must not have:** a receipt must go stale when **either** the adapter source **or** any of the three CLI versions changes — a receipt keyed to only one of them certifies a path that moved. A provider whose version could not be read must make the receipt stale, never fresh (question 1: an unread version is not an unchanged one). And "the provider was not installed" must not compare equal to "the provider ran and wrote no marker" (question 2) — those are different facts about the adapter path and only the second is a defect in this code.

- [ ] **Step 1: Write the hermetic tests**

Create `/home/khenrix/git/khenrix-utils/tests/test_forge_smoke.py`:

```python
"""§18's live smoke, tested WITHOUT invoking a provider. Nothing here spends money — the
receipt logic is pure and the provider path is exercised only by `make smoke-llm-forge`."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "shared" / "lib"))

import forge_smoke  # noqa: E402


def test_adapter_hash_moves_when_the_adapter_moves(tmp_path, monkeypatch):
    a = forge_smoke.adapter_hash()
    monkeypatch.setattr(forge_smoke, "ADAPTER_SOURCES",
                        forge_smoke.ADAPTER_SOURCES[:-1])
    assert forge_smoke.adapter_hash() != a


def test_a_receipt_is_stale_when_the_adapter_changed():
    r = {"adapter_sha256": "old", "cli_versions": {"claude": "1", "codex": "2", "agy": "3"}}
    ok, why = forge_smoke.receipt_is_fresh(
        r, adapter="new", versions={"claude": "1", "codex": "2", "agy": "3"})
    assert not ok and "adapter" in why


def test_a_receipt_is_stale_when_any_cli_version_changed():
    r = {"adapter_sha256": "a", "cli_versions": {"claude": "1", "codex": "2", "agy": "3"}}
    ok, why = forge_smoke.receipt_is_fresh(
        r, adapter="a", versions={"claude": "1", "codex": "9", "agy": "3"})
    assert not ok and "codex" in why


def test_an_unreadable_cli_version_makes_the_receipt_stale_never_fresh():
    """An unread version is not an unchanged one. A receipt fresh over a version nobody could
    read certifies a path that may have moved."""
    r = {"adapter_sha256": "a", "cli_versions": {"claude": "1", "codex": "2", "agy": "3"}}
    ok, why = forge_smoke.receipt_is_fresh(
        r, adapter="a", versions={"claude": "1", "codex": None, "agy": "3"})
    assert not ok and "codex" in why


def test_a_matching_receipt_is_fresh():
    v = {"claude": "1", "codex": "2", "agy": "3"}
    r = {"adapter_sha256": "a", "cli_versions": dict(v)}
    ok, why = forge_smoke.receipt_is_fresh(r, adapter="a", versions=v)
    assert ok and why == ""


def test_a_receipt_missing_a_provider_is_stale():
    r = {"adapter_sha256": "a", "cli_versions": {"claude": "1", "codex": "2"}}
    ok, why = forge_smoke.receipt_is_fresh(
        r, adapter="a", versions={"claude": "1", "codex": "2", "agy": "3"})
    assert not ok and "agy" in why


def test_the_smoke_is_in_no_gate_target():
    """The deliberate money exception must be opt-in. A target reached by `verify` or
    `precommit` spends on every commit."""
    mk = (ROOT / "Makefile").read_text(encoding="utf-8")
    for line in mk.splitlines():
        if line.startswith(("verify:", "precommit:", "test:", "council-test:",
                            "eval-test:", "forge-test-slow:")):
            assert "smoke-llm-forge" not in line, line
    assert "smoke-llm-forge:" in mk
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uvx --with pytest pytest -q tests/test_forge_smoke.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'forge_smoke'`.

- [ ] **Step 3: Write `scripts/forge_smoke.py`**

Create `/home/khenrix/git/khenrix-utils/scripts/forge_smoke.py`:

```python
#!/usr/bin/env python3
"""§18's live three-provider write smoke: the one thing that proves the real provider path.

WHY THIS EXISTS. `forge/launch.py` and `forge/runner.py` both say in words that nothing in
the package's suite invokes a real provider — `launch` is injected everywhere and every test
passes a fake. About twenty thousand lines are therefore exercised against a stub, and the
adapter is the one seam a stub cannot stand in for. The council target beside this one is
one provider and read-only, which proves nothing about three write-enabled seats.

WHAT IT DOES, and it is deliberately the cheapest thing that proves the claim: a tiny
disposable repository, one clone per provider, each asked to write a distinct marker file in
its own clone and to quote the proof token; then the markers are harvested, and the ORIGINAL
checkout is shown unchanged. Three provider calls — roughly 15% of one default run.

WHAT IT COSTS AND WHY IT IS OPT-IN. It spends real money, so it is reachable only through
`make smoke-llm-forge` and appears in no gate target. `make verify` and `make precommit` must
stay free.

THE RECEIPT IS KEYED TO BOTH THINGS THAT CAN MOVE. A receipt naming only the adapter goes
green over a CLI that changed its argv; one naming only the CLIs goes green over an adapter
that stopped passing the scrubbed environment. Both, or the receipt certifies a path that
moved. A version this script could not READ makes the receipt stale rather than fresh — an
unread version is not an unchanged one.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "lib"))

from council import engine                      # noqa: E402
from forge import fleet, gitcmd, launch, seat    # noqa: E402

# The adapter's source closure: every file whose change can alter what a provider is asked or
# what environment it is asked in. `seat` owns the spec, `launch` owns the prompt and the
# fingerprint, `fleet` owns the scrubbed environment, `engine` owns the invocation.
ADAPTER_SOURCES = (
    ROOT / "shared" / "lib" / "forge" / "launch.py",
    ROOT / "shared" / "lib" / "forge" / "seat.py",
    ROOT / "shared" / "lib" / "forge" / "fleet.py",
    ROOT / "shared" / "lib" / "council" / "engine.py",
)
PROVIDERS = ("claude", "codex", "agy")
RECEIPT = ROOT / "evals" / "llm-forge" / "smoke-receipt.json"
MARKER = "FORGE-SMOKE.txt"


def adapter_hash() -> str:
    """One digest over the adapter source closure, in a fixed order."""
    h = hashlib.sha256()
    for p in ADAPTER_SOURCES:
        h.update(p.name.encode("utf-8"))
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def cli_versions() -> dict:
    """`{provider: version string | None}`. `None` is "could not be read" and is never "".

    A provider that is not installed and one whose `--version` failed are the same answer here
    on purpose — both mean this script did not learn what version would run — and both make a
    receipt stale. What they are NOT the same as is a provider that ran and wrote nothing,
    which is a defect in the adapter and is reported separately by the smoke itself.
    """
    out = {}
    for name in PROVIDERS:
        exe = shutil.which(name)
        if exe is None:
            out[name] = None
            continue
        try:
            r = subprocess.run([exe, "--version"], capture_output=True, text=True,
                               timeout=30, check=False)
        except (OSError, subprocess.SubprocessError):
            out[name] = None
            continue
        out[name] = r.stdout.strip() or r.stderr.strip() or None
    return out


def receipt_is_fresh(receipt, *, adapter: str, versions: dict) -> tuple:
    """`(ok, why)`. `ok` is True only when every keyed fact matches something READ."""
    if not isinstance(receipt, dict):
        return False, "there is no smoke receipt"
    if receipt.get("adapter_sha256") != adapter:
        return False, "the adapter source changed since the last live smoke"
    recorded = receipt.get("cli_versions")
    if not isinstance(recorded, dict):
        return False, "the smoke receipt records no CLI versions"
    for name in PROVIDERS:
        now, then = versions.get(name), recorded.get(name)
        if now is None:
            return False, (f"{name}'s version could not be read, so the receipt cannot say "
                           "the path it certified is the path that would run")
        if then != now:
            return False, f"{name} moved from {then!r} to {now!r} since the last live smoke"
    return True, ""


def _disposable_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir(parents=True)
    (repo / "README.md").write_text("forge smoke\n", encoding="utf-8")
    env = {**gitcmd.NO_USER_CONFIG}
    gitcmd.git(repo, "init", "-q", "-b", "main", env_extra=env)
    gitcmd.git(repo, "config", "user.name", "forge-smoke", env_extra=env)
    gitcmd.git(repo, "config", "user.email", "forge-smoke@example.invalid", env_extra=env)
    gitcmd.git(repo, "add", "README.md", env_extra=env)
    gitcmd.git(repo, *gitcmd.NO_HOOKS, "commit", "-q", "-m", "base", env_extra=env)
    return repo


def _prompt() -> str:
    return (f"Create a file named exactly {MARKER} in your current working directory. Its "
            "only content must be one line: the word FORGE-SMOKE followed by a space and "
            "then your own CLI's name (claude, codex or agy). Do not modify any other file. "
            "Then reply with the proof token you were given and nothing else.")


def _run_one(name: str, repo: Path, root: Path, timeout: int) -> dict:
    """One provider, in its OWN clone, through the production adapter.

    THROUGH `launch.make_launcher` AND NOTHING ELSE. A smoke that built its own spec would
    prove that `run_provider` works and nothing about the adapter, which is the only untested
    seam and the whole reason this file exists.
    """
    dest = root / "clones" / name
    baseline_ref = f"refs/khenrix-forge/smoke00/base"
    gitcmd.git(repo, *gitcmd.NO_HOOKS, "update-ref", baseline_ref, "HEAD",
               env_extra=gitcmd.READONLY)

    class _At:
        ref = baseline_ref

    seat_obj = fleet.clone_seat(repo, _At(), dest, name=name,
                               identity=("forge-smoke", "forge-smoke@example.invalid"))
    launcher = launch.make_launcher(prompt=_prompt(), timeout=timeout)
    token = engine.make_sentinel()
    env = fleet.forge_child_env(repo)
    record = launcher(name=name, seat_path=dest, token=token, env=env)

    marker = dest / MARKER
    wrote = marker.is_file()
    quoted = token in str(record.get("result_text") or "")
    return {"provider": name, "wrote_marker": wrote, "quoted_token": quoted,
            "marker_text": marker.read_text(encoding="utf-8").strip() if wrote else None,
            "valid": bool(record.get("valid")), "exit_code": record.get("exit_code")}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="forge_smoke", description=__doc__.splitlines()[0])
    ap.add_argument("--providers", default=",".join(PROVIDERS))
    ap.add_argument("--timeout", type=int, default=None)
    args = ap.parse_args(argv)

    window = args.timeout or engine.MODE_TIMEOUT.get("forge")
    if not isinstance(window, int) or window < 1:
        print("  ✗ council.engine.MODE_TIMEOUT has no usable `forge` entry", file=sys.stderr)
        return 1
    names = [n for n in args.providers.split(",") if n]

    root = Path(tempfile.mkdtemp(prefix="forge-smoke-"))
    try:
        repo = _disposable_repo(root)
        before = gitcmd.git(repo, "status", "--porcelain",
                            env_extra=gitcmd.READONLY).stdout
        head = gitcmd.git(repo, "rev-parse", "HEAD", env_extra=gitcmd.READONLY).stdout.strip()

        results = [_run_one(n, repo, root, window) for n in names]

        after = gitcmd.git(repo, "status", "--porcelain",
                           env_extra=gitcmd.READONLY).stdout
        head2 = gitcmd.git(repo, "rev-parse", "HEAD", env_extra=gitcmd.READONLY).stdout.strip()
        untouched = (before == after and head == head2)

        for r in results:
            mark = "✓" if (r["wrote_marker"] and r["quoted_token"]) else "✗"
            print(f"  {mark} {r['provider']}: marker={r['wrote_marker']} "
                  f"token={r['quoted_token']} valid={r['valid']} exit={r['exit_code']}")
        print(f"  {'✓' if untouched else '✗'} original checkout unchanged")

        ok = untouched and all(r["wrote_marker"] and r["quoted_token"] for r in results)
        if ok:
            RECEIPT.parent.mkdir(parents=True, exist_ok=True)
            RECEIPT.write_text(json.dumps({
                "adapter_sha256": adapter_hash(),
                "cli_versions": cli_versions(),
                "providers": results,
                "original_checkout_unchanged": untouched,
            }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            print(f"  receipt: {RECEIPT}")
        else:
            # NO RECEIPT ON A FAILED SMOKE, for `handover.write_handover`'s reason one gate
            # over: a receipt is read as a certification, and one written over a failure is a
            # false claim of exactly the kind the gate exists to catch.
            print("  ✗ smoke FAILED — no receipt written", file=sys.stderr)
        print(f"  artifacts: {root}")
        return 0 if ok else 1
    finally:
        pass  # the tree is left for inspection; it is under the system temp dir


if __name__ == "__main__":
    raise SystemExit(main())
```

> Two details to confirm against the code before running: `engine.make_sentinel` is the token
> minter `review.run_round` uses (`review.py:803`), and `fleet.clone_seat` derives the run id
> as `baseline.ref.split("/")[2]` — hence `smoke00` in the ref above. Read both and adjust if
> either differs.

- [ ] **Step 4: Add the make target**

In `/home/khenrix/git/khenrix-utils/Makefile`, add `smoke-llm-forge` to the `.PHONY` list, and add this target immediately after `smoke-llm-council`:

```makefile
# §18's live three-provider WRITE smoke. THE DELIBERATE MONEY EXCEPTION, and it is opt-in for
# that reason: it appears in no gate target, because `verify` and `precommit` run on every
# commit and must stay free. `smoke-llm-council` beside it is one provider and read-only,
# which proves nothing about three write-enabled seats — and `launch.py` and `runner.py` both
# say in words that nothing in the forge suite invokes a real provider.
# Three provider calls, roughly 15% of one default forge run.
smoke-llm-forge: ## Live 3-provider WRITE smoke through the real forge adapter (costs tokens, needs auth)
	$(PY) scripts/forge_smoke.py
```

- [ ] **Step 5: Run the hermetic tests**

Run: `uvx --with pytest pytest -q tests/test_forge_smoke.py`
Expected: PASS — 7 passed. **No provider is invoked.**

- [ ] **Step 6: Confirm the target is not in any gate**

```bash
cd /home/khenrix/git/khenrix-utils
grep -n "smoke-llm-forge" Makefile
```

Expected: the `.PHONY` line and the target's own two lines, and nothing else. It must not appear on the right-hand side of `verify:`, `precommit:`, `test:`, `council-test:`, `eval-test:` or `forge-test-slow:`.

- [ ] **Step 7: Document it in SKILL.md**

In `/home/khenrix/git/khenrix-utils/shared/skills/llm-forge/SKILL.md`, add near the end, before any troubleshooting section:

```markdown
## The provider path is proven by one opt-in smoke

The engine's own suite invokes **no** provider — every seat launch is a fake, deliberately, so
the suite costs nothing. That means the suite proves the engine and not the adapter. One
target proves the adapter:

```bash
make smoke-llm-forge      # costs tokens, needs auth — three provider calls
```

It builds a disposable repository, gives each CLI its own clone, asks each to write a distinct
marker and quote its proof token, harvests them, and shows the original checkout unchanged. On
success it writes a receipt naming the adapter source hash and all three CLI versions, so the
receipt goes stale when either moves. Run it whenever adapter or provider wiring changes.
```

- [ ] **Step 8: Render, verify, commit**

```bash
cd /home/khenrix/git/khenrix-utils
make render
git add scripts/forge_smoke.py tests/test_forge_smoke.py Makefile shared/skills/llm-forge/SKILL.md marketplaces
```

```bash
make verify
echo "verify rc=$?"
```

```bash
make precommit
echo "precommit rc=$?"
```

Both must print `rc=0` **without spending anything**. `scripts/forge_smoke.py` is not under
`shared/lib/forge/`, so it does not stale the forge receipt on its own; `SKILL.md` does, so
re-seed if `precommit` reports it.

```bash
git commit -m "test(forge): the adapter nothing exercised gets the one smoke that exercises it"
```

- [ ] **Step 9: Run the live smoke once, deliberately**

This is the only money-spending step in the plan. Run it in the foreground and read the output:

```bash
cd /home/khenrix/git/khenrix-utils
make smoke-llm-forge
echo "smoke rc=$?"
```

Expected: three `✓ <provider>: marker=True token=True` lines, `✓ original checkout unchanged`,
a receipt path, and `smoke rc=0`. **If any line is `✗`, that is the finding this whole task
existed to produce** — report it, do not paper over it, and do not commit a receipt (the
script writes none on failure).

```bash
git add evals/llm-forge/smoke-receipt.json
git commit -m "test(forge): the real provider path, proven once"
```

---

## Self-review

**1. Spec coverage against the brief's nine gaps.**

| Gap | Task | Covered |
|---|---|---|
| G1 — §10–§13 have no production caller | 2 (ledger verb, `_strongest` resolves) | **Partially, and stated.** The ledger half lands here and is what unblocks §13 mechanically (`assert_ledger_is_out_of_reach` refuses a run with no ledger). The review half needs a production `fix` and goes to Plan L with the seam named. |
| G2 — false rationale, `--collect` takes a verdict | 3 | Yes. Docstring corrected with the actual `gate.py:270` evidence; evidence flags replace `--synthesis-outcome`; the priced verifier pass is deferred **with an argument**, not dropped. |
| G3 — reviewers in a linked worktree | 4 | Yes. Option B chosen, Option A evaluated and rejected with reasons. |
| G4 — no live three-provider write smoke | 7 | Yes, opt-in, receipt keyed to adapter hash **and** three CLI versions. |
| G5 — `--start` should write a fusion brief | 1 (brief) + 3 (non-fusion refusal) | Yes, both halves. |
| G6 — interrupted `--start` unrecoverable; wall clock unquoted | 6 (wall-clock bound) | **Split.** The bound lands here; resume and parallel builders go to Plan L, stated in the order-of-work section. |
| G7 — §4's disk rejection unimplemented | 5 | Yes. |
| G8 — setup commands unscreened | 6 | Yes. |
| G9 — stale docstrings | 2 | Yes (`cli.py:454` rewritten as part of `_strongest`; `runner.py:12` and `runner.py:1624` corrected). |

**2. Placeholder scan.** Three steps carry a bounded "read this before writing" instruction rather than final code: Task 2 Step 4 (`ledger.decode_payload` may need a three-line public wrapper — the wrapper is written out), Task 2 Step 6 (`rubric.dimensions_from`'s parameter types), Task 5 Step 4 (`open_run`'s current signature). Each names the exact file and line range, states the decision rule, and gives the code for the likely case. They are verification instructions, not TBDs. Task 4 Step 4 contained a transcription artifact (`if False else`); it is flagged inline with the single correct form to write.

**3. Type consistency.** `_strongest` changes signature in Task 2 (`(run_dir, *, tree)`) and its only call site is updated in the same task; Task 3 does not touch it. `gate.quote` gains `seat_timeout_sec` and `gate.must_show` gains `setup` in Task 6, both required, both call sites updated in the same task. Task 5's tests use `seat_timeout_sec=` and carry an explicit note to drop it if Task 6 has not landed — the two tasks are order-independent otherwise. `brief.UNKNOWN is None` is used consistently as an identity check, never a truthiness check, in `seat_paths`, `overlap` and `sole`. `review.loop`'s new `repo`/`run_id`/`identity`/`make_tree` are keyword-only and `make_tree`'s signature matches `clone_review_tree(repo, run_dir, *, run_id, round_, checkpoint, identity)` at both the definition and the call.

**4. What is NOT in this plan, restated so it is not lost.** Plan L: the `--review` verb driving `review.loop` with a round-by-round orchestrator `fix` handshake; the priced synthesis verifier pass (which must ship with that verb, not before it); `--start` resume after an interrupted run; parallel builders. Task 4 must be merged before Plan L is written.
