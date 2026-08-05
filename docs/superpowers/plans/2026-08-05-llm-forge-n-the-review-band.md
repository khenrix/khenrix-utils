# llm-forge N — the review band, and the prerequisite that cannot be built

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the reproduced defects in §13's review path and §15's reclamation, and retire the blocking prerequisite that gated all of it — because the engine's own reasoning already shows it cannot be built.

**Architecture:** Plan K's order-of-work made one thing blocking: *"L0, first and blocking: close Task 4's reviewer-root residual. The likely shape is a sibling root outside `run_dir`. **Nothing that convenes a real panel may ship before this.**"* Plan M carried that forward as Plan N's item 1. **It is void, and the engine says so in its own docstring** — see below. Retiring it is this plan's first act, because every later task was gated behind a fix that does not exist. What remains is ordered by *severity of a reproduced defect*: **N1** stops a run nobody reviewed from shipping as reviewed; **N2** and **N3** close §15's two reclamation defects, both of which delete or mis-describe the user's own work.

**Tech Stack:** Python 3.11+, stdlib only. `pytest` via `uvx --with pytest pytest`. `make verify` is the gate; `make precommit` is the commit boundary.

## Global Constraints

- Python is **stdlib-only**. No pip dependencies, no install step.
- `make verify` is the gate; `make precommit` is the commit boundary. A skill change needs an eval receipt.
- Commit directly to `main`. Do not push without asking.
- Reconcile stays non-destructive: it only adds missing entries or updates ones tagged `khenrix-managed`.
- Every `tests/test_forge_*.py` must be named in `FORGE_TESTS` or `FORGE_SLOW_TESTS`.
- The two binding rules: **FAIL CLOSED**, and **A VERDICT MUST NEVER READ CLEANER THAN ITS EVIDENCE**. The founding premise: **a check the builder could have rigged is not a check.**

## Test discipline

Assert the **external question** — "can this inventory distinguish these two trees?", "does anything call this?", "what ELSE can this proven capability do?" — never a restatement of the implementation. Where a proposed test would only confirm the code agrees with itself, say so and write a different one. Plan M lost three tests to this rule and caught all three by measuring first; **reproduce before you fix, and grep for what already tests the predicate before concluding it cannot fail.**

---

## Task 0: Retire the blocking prerequisite — it cannot be built

**No code changes. This is a decision, recorded, and it unblocks everything below.**

Plan K required a sibling review root outside `run_dir` before any real panel could be convened. `review.assert_ledger_is_out_of_reach`'s own docstring (`review.py:434-445`) already refutes it, in the shipped source:

> *"Both checks are about PATHS, and there is no permission boundary anywhere in this design: reviewers run as the operator's own UID with a shell, so a reviewer that goes looking can read the run directory by absolute path, glob `$XDG_STATE_HOME/khenrix-forge/*/ledger.json`, or `find` for it — none of which any arrangement of roots prevents. **Moving the review clone somewhere else does not close this: a sibling of the run directory is reachable by a computable name, so it only relocates the `..`.** §13's blindness is therefore BY CONSTRUCTION … and is not enforced containment. Closing it for real needs an OS boundary (a mount namespace, a separate UID, a container) or a ledger that is never written to disk for the duration of the round."*

So the prerequisite is a change that would read like a security fix and buy nothing — **a verdict reading cleaner than its evidence, built deliberately**, which is the one thing this project refuses. Neither escape hatch is available: an OS boundary is out of scope for a stdlib-only package that shells out to three CLIs as the operator, and a ledger absent from disk for the round's duration is defeated by the same `find` the docstring names.

**Verified before retiring it — nothing overclaims.** `SKILL.md:276-280` already tells the operator *"blindness is asserted against the ledger's path rather than enforced by the operating system … Treat the panel as **blind by construction, not by containment**."* The engine's docstring says the same. There is no claim anywhere that the relocation would have made true.

- [ ] **Step 1: Record the retirement in the plan of record**

Add to `docs/superpowers/plans/2026-08-04-llm-forge-k-wiring-the-decision-engine.md`, immediately under its "L0, first and blocking" bullet:

```markdown
> **RETIRED 2026-08-05, and not by being done.** `review.assert_ledger_is_out_of_reach`'s
> docstring already argues that relocation buys nothing — "a sibling of the run directory is
> reachable by a computable name, so it only relocates the `..`" — and names the only two real
> closures (an OS boundary, or a ledger absent from disk for the round), neither available to a
> stdlib-only package running three CLIs as the operator. §13's blindness is BY CONSTRUCTION,
> `SKILL.md` says so to the operator, and nothing claims otherwise. This bullet gated the review
> band behind a fix that cannot exist; the gate is removed rather than satisfied. See Plan N.
```

- [ ] **Step 2: Commit the decision on its own**

```bash
cd /home/khenrix/git/khenrix-utils
git add docs/superpowers/plans/
git commit -m "docs(llm-forge): the review band's blocking prerequisite cannot be built, and is retired"
```

---

## Task 1: A round nobody answered must not ship as "degraded"

**The finding, reproduced 2026-08-05.** `terminal_from_record` puts *"any round whose record names no reviewer at all"* into `degraded` (`review.py:1504-1506`). Measured over a real record — round 1 answered by all three, round 2 answered by **none**, both bracketed clean:

```
after round 1 (3 responded)          -> ready
after round 2 (0 responded, 3 silent) -> degraded
   round 2 was answered by 0 of 3 reviewers; silent: claude (t), codex (t), agy (t)
```

`degraded` **ships**. It is §14's "weaker than a clean review" and the handover delivers it. But zero responders is not a weaker review — **it is no review**, and §13 exists to obtain an independent one. A delivery labelled `degraded` tells its reader a panel looked and found the work merely imperfect; here no panel looked at all. That is a verdict reading cleaner than its evidence, in the field whose whole job is to carry that distinction.

**The rule this task installs:** a round with **zero** responders is `review_blocked`. A round with *some* responders and some silent stays `degraded` — that genuinely is a weaker review, and conflating the two would be the same collapse in the other direction.

**Files:**
- Modify: `/home/khenrix/git/khenrix-utils/shared/lib/forge/review.py` (`terminal_from_record`, ~`:1498-1560`)
- Test: `/home/khenrix/git/khenrix-utils/tests/test_forge_review.py`
- Modify: `/home/khenrix/git/khenrix-utils/shared/skills/llm-forge/SKILL.md` if it describes the `degraded` branch

**Interfaces:**
- Consumes: `review.Round.seats_responded`, `review.Round.seats_silent`; `review.REVIEW_BLOCKED`, `review.DEGRADED`.
- Produces: no signature change. `terminal_from_record` returns `REVIEW_BLOCKED` for a round with no responders.

**The fail-open this task must not have:** the new branch must fire on the round's **own** record, not on a roll-up across rounds — a run whose round 1 was whole and whose round 2 was silent must be `review_blocked`, and a test that only checks a single-round run would pass over exactly that. And it must not swallow the *partial* panel: 1-of-3 and 0-of-3 must stay two different answers, since the whole finding is that two different states shared one label.

- [ ] **Step 1: Write the failing tests**

Append to `/home/khenrix/git/khenrix-utils/tests/test_forge_review.py`:

```python
def test_a_round_nobody_answered_is_blocked_rather_than_degraded(tmp_path):
    """MEASURED BEFORE THE FIX: a round answered by 0 of 3 classified `degraded`, which SHIPS.
    `degraded` tells its reader a panel looked and found the work merely imperfect; here no
    panel looked at all. §13 exists to obtain an independent review, and "no review" is not a
    weaker one."""
    run = _run_dir(tmp_path)
    log = journal.Journal(storage.journal_path(run))
    review.write_round(run, review.Round(
        1, "a" * 40, (), (), (),
        (("claude", "timeout"), ("codex", "timeout"), ("agy", "timeout"))))
    review.record_worktree_before(log, round_=1, digest="d", entries=1)
    review.record_worktree_after(log, round_=1, digest="d", entries=1, changed={})
    verdict, why = review.terminal_from_record(run, rounds_run=1, events=log.read())
    assert verdict == review.REVIEW_BLOCKED, (verdict, why)
    assert "0 of 3" in why or "no reviewer" in why, why


def test_a_partly_silent_panel_is_still_degraded_and_not_blocked(tmp_path):
    """THE DISCRIMINATION CHECK, and the reason the fix is not "any silence blocks". One
    reviewer answering IS a weaker review — which is what `degraded` means — and collapsing it
    into `review_blocked` would be the same two-states-one-label defect in the other
    direction."""
    run = _run_dir(tmp_path)
    log = journal.Journal(storage.journal_path(run))
    review.write_round(run, review.Round(
        1, "a" * 40, (), (), ("claude",), (("codex", "timeout"), ("agy", "timeout"))))
    review.record_worktree_before(log, round_=1, digest="d", entries=1)
    review.record_worktree_after(log, round_=1, digest="d", entries=1, changed={})
    verdict, _ = review.terminal_from_record(run, rounds_run=1, events=log.read())
    assert verdict == review.DEGRADED, verdict


def test_a_silent_round_after_a_whole_one_still_blocks(tmp_path):
    """THE FAIL-OPEN THIS TASK MUST NOT HAVE. The branch has to fire on the ROUND's own
    record, not on a roll-up: a run whose round 1 was whole and whose round 2 nobody answered
    is a run whose last word came from nobody. A single-round test passes over this."""
    run = _run_dir(tmp_path)
    log = journal.Journal(storage.journal_path(run))
    for n, resp, silent in (
            (1, ("claude", "codex", "agy"), ()),
            (2, (), (("claude", "t"), ("codex", "t"), ("agy", "t")))):
        review.write_round(run, review.Round(n, "a" * 40, (), (), resp, silent))
        review.record_worktree_before(log, round_=n, digest="d", entries=1)
        review.record_worktree_after(log, round_=n, digest="d", entries=1, changed={})
    verdict, _ = review.terminal_from_record(run, rounds_run=2, events=log.read())
    assert verdict == review.REVIEW_BLOCKED, verdict
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uvx --with pytest pytest -q tests/test_forge_review.py -k "nobody_answered or partly_silent or silent_round_after"`
Expected: the first and third FAIL on `degraded != review_blocked`; the second PASSES already — it is the invariant the fix must not break, written first so a break is visible.

- [ ] **Step 3: Add the branch**

In `terminal_from_record`, before the `degraded` branch that reads silence, add the zero-responder case. Read the surrounding code and match how it iterates the rounds — the branch belongs wherever the existing per-round silence check lives, so both read the same record the same way:

```python
        if not r.seats_responded:
            # ZERO RESPONDERS IS NOT A WEAKER REVIEW, IT IS NO REVIEW, and `degraded` SHIPS.
            # That label tells its reader a panel looked and found the work merely imperfect.
            # §13 exists to obtain an independent review; a round nobody answered obtained
            # none, and delivering it under a label that implies one is a verdict reading
            # cleaner than its evidence — in the field whose whole job is that distinction.
            #
            # A PARTIAL panel stays `degraded` deliberately: one reviewer answering really is
            # a weaker review, and folding 1-of-3 in with 0-of-3 would be this same defect
            # with the two states swapped.
            return REVIEW_BLOCKED, (
                f"round {r.round} was answered by 0 of "
                f"{len(r.seats_responded) + len(r.seats_silent)} reviewers, so nothing "
                "independent looked at this candidate. §13's review did not happen, and a "
                "run that ships as `degraded` claims one that did.")
```

Update the `degraded` bullet in the docstring's PRECEDENCE list: *"any round in which a reviewer was silent"* becomes *"any round in which SOME reviewer was silent — a round in which every one was is `review_blocked`, above."*

- [ ] **Step 4: Run the tests to verify they pass, then the whole review path**

Run: `uvx --with pytest pytest -q tests/test_forge_review.py tests/test_forge_handover.py tests/test_forge_cli.py`
Expected: PASS. A test asserting `degraded` for a fully-silent round is asserting the defect — correct it, and say so in the commit.

- [ ] **Step 5: Check SKILL.md**

```bash
grep -n "degraded" shared/skills/llm-forge/SKILL.md
```

If its prose describes the `degraded` branch as covering a silent panel, correct it to say that a **fully** silent round blocks. If it does not mention it, change nothing — a doc edit with no claim behind it is noise.

- [ ] **Step 6: Render, verify, commit**

```bash
cd /home/khenrix/git/khenrix-utils
make render
git add shared/lib/forge/review.py tests/ shared/skills/llm-forge marketplaces
make verify
git commit -m "fix(forge): a run nobody reviewed shipped as reviewed-but-degraded"
```

---

## Deferred to Plan O — explicit, so nothing is silently dropped

Plan M's list of twelve, minus what this plan takes and minus the retired prerequisite:

1. **The review verb itself** — `--review` driving one `run_round`; then the unattended `loop`; then §12.5's rank in `cli._strongest`; then the priced synthesis verifier pass, which must ship *with* the review verb rather than before it. **No longer gated** — Task 0 removed the gate, which was a fix that could not be built.
2. **Reprice §13's rounds, or refuse `--review-rounds` while nothing convenes one.** `review` and `review_fixes` — 9 of the quoted 19 calls — have no production caller and SKILL.md says so. Zeroing the terms stops `--review-rounds` moving `provider_calls` and makes `confirm` refuse every run (measured, 13 failures). The ceiling and the price are two numbers; separating them is the work, and it must settle all four terms at once — K6 added the review clone to `peak_disk_gb` on the argument that the quote already prices the unbuilt stage everywhere else.
3. **`Status.setup` → `Status.builder_setup`** — an on-disk schema change plus a required keyword across ~42 `classify_seat` call sites, colliding with the existing top-level `verifier_setup` key. Needs a reviewer looking at a rename.
4. **§15's reclamation pair** — `gc` deleting refs by namespace prefix rather than exact name+OID, and the handover asserting the synthesis branch from the run id while measuring HEAD with `--gc` then reclaiming the difference. **Not measured yet**; both are `--gc` deleting the user's own work, so they are Plan O's first band and must be reproduced before they are scheduled.
5. **s5 remainder** — council result files written outside the bracket with no integrity re-check; the review bundle inside `.git` with its path in argv and no digest; repo-local diff drivers and `git replace` refs unmeasured for reviewers' own `git diff`; ultrareview's absent journal and durable receipt; plus seven Mediums.
6. **s6 remainder** — `--collect` discarding §9 drift and §14.1 orphans; the cloud review's missing idempotency guard whose own refusal text tells the operator to re-trigger it; the seat-count denominator taken from a disk glob rather than `manifest.seats`; the verify command truncated to step 0; plus five Mediums.
7. **s7** — "fusion, not selection" unenforced (the collector rejects only a tree identical to B₁); the eval-baseline contamination; `--collect`'s re-payable review; `mutate.py`'s bytecode purge and missing path containment; `eval_trigger`'s type coercions; `reconcile`'s orphaned-marker destruction and `backup()` overwrite; `--seats 1` vs "all three CLIs".
8. **s1 remainder** — the symlink gate referent; the make memo key with `_scan_make`'s `--directory=`/`-C` gap; the calibration aggregate; the control-plane integrity tripwire; Fwork byte-binding; durable-state reconstruction; `Seat.verified` over `sidecars is None`; two index definitions; `_gate_taints`' `isinstance` gate; `_command_paths`' silent `continue`; `_AMBIENT_SKILL`'s short-path refusals; `screen.py` carrying this repo's allow-list into foreign repos; `fleet.clone_seat`'s bare `IndexError`; `Quote`'s unvalidated fields.
9. **s2 remainder** — `no_change` with `proven_read=False`; the executed-and-refuted check recorded as `not-run`; §8.1's missing input half; `RunnerError`-as-retry; the empty fleet reaching `comparing`; `FLAKY` unreachable; `_clip`'s evidence truncation; `_verify_dim`'s collapse.
10. **s3 remainder** — `installed_closure`'s permutation collision; `verify_materialized`'s copied fields and the size/cap-blind `bundle_hash`; the criterion-to-claim binding; seat provenance; the journal creation race; hash criteria not distinguishing a file from a symlink.
11. **s4 remainder** — `snapshot.take`'s undeclared `FileNotFoundError`; `Size` accepting `(0,0)`, negatives and bools; `_dir_digest`; the lost-journal/no-fixes collapse.
12. **`--start` resume after an interrupted run, and parallel builders.**

**What no plan fixes, restated so it is not lost.** An orchestrator that writes a **true but incomplete** ledger is invisible to every check this design has: §12.4's fallback fires on claims that are *unsatisfied*, not on claims never written down, and §13's panel reviews the candidate rather than the ledger's completeness. A row omitted is a row nothing here can miss. Spec-level; deliberately unsolved.

**And §13's blindness is by construction, permanently.** Task 0 explains why, `SKILL.md` tells the operator, and no future plan should schedule a path-based fix for it.

---

## Self-review

**1. Spec coverage.** Task 0 retires the gate that blocked Plan M's item 1 and everything behind it. Task 1 closes the one reproduced defect in the review path. Everything else is listed above with its finding.

**2. Placeholder scan.** One deliberate under-specification: Task 1 Step 3 says to match how `terminal_from_record` iterates its rounds rather than restating the loop, because the branch must read the same record the same way as the silence check beside it. No "TBD", no "add error handling", no "similar to Task N".

**3. Premises measured before writing** — the discipline Plan M added after five of its seven tasks moved under measurement:
- **Task 0's premise held and made the prerequisite void:** `review.py:434-445` argues relocation buys nothing, and `SKILL.md:276-280` already tells the operator blindness is by construction. Nothing overclaims, so nothing needs correcting alongside.
- **Task 1's defect reproduced exactly:** round 1 with three responders → `ready`; round 2 with none → `degraded`, over a real record with both brackets clean.
- **Deferred item 4 is explicitly NOT measured yet** and says so, because scheduling a `--gc` deletion fix on an unreproduced finding is how a plan spends a reviewer on a defect that may not exist.

**4. Type consistency.** No signature changes. `terminal_from_record` keeps `(run_dir, *, rounds_run, events) -> (str, str)` and gains one branch returning an existing constant.

**5. What this plan does not claim.** It does not convene a panel, rank a seat, or price a review round — those are Plan O's, and until they land the fusion tool's two headline judgements still decline by construction. That is honest and it is not finished.
