# P — the last plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Everything still open across every plan in this repository, in one document, so that when it is executed there is nothing left that anybody wrote down.

**Architecture:** Six bands, ordered by what blocks what. **P1** builds the `fix` implementation, which is the largest unbuilt piece in the package and gates three separate items behind it. **P2** takes the two decisions that should be taken with P1 in hand and not before. **P3** is one wide mechanical rename. **P4** and **P5** are the slice remainders from the seven-slice council review, ordered by severity. **P6** finishes the two NON-forge plans that were found open when every plan in the repository was audited. **P7** is resume and parallel builders.

**Tech Stack:** Python 3.11+, stdlib only. `pytest` via `uvx --with pytest pytest`. `make verify` is the gate; `make precommit` is the commit boundary.

## Global Constraints

- Python is **stdlib-only**. No pip dependencies, no install step.
- `make verify` is the gate; `make precommit` is the commit boundary. A skill change needs an eval receipt.
- Commit directly to `main`. Do not push without asking.
- Reconcile stays non-destructive: it only adds missing entries or updates ones tagged `khenrix-managed`.
- Every `tests/test_forge_*.py` must be named in `FORGE_TESTS` or `FORGE_SLOW_TESTS`.
- The two binding rules: **FAIL CLOSED**, and **A VERDICT MUST NEVER READ CLEANER THAN ITS EVIDENCE**. The founding premise: **a check the builder could have rigged is not a check.**

---

## P0 — the protocol, binding on every task below

**Nothing in this plan is a defect until it has been reproduced.** The findings below come from a seven-slice council review and are *claims*. In the sessions that closed Plans K through O, the following happened repeatedly:

- A `gc` finding ("deletes refs by namespace prefix") **did not reproduce** — `_refs_of` already carries the run id and a trailing slash and deletion already passes the expected OID. Scheduling it would have spent a reviewer on nothing.
- Three tests written from a finding's own words **passed before their fix**, because the finding named the wrong mechanism.
- One finding was declared unreproducible **twice** — while a test in this repository had been asserting the exact blindness on purpose the whole time.
- Items on this plan's own list were checked while writing it and turned out **already closed** — `Size`'s negative/bool refusals, and the `gc` prefix finding. They are in "Explicitly NOT in this plan" with their measurements.

So **every task starts by reproducing its finding**, and the result is recorded either way:

- [ ] **Step 0, every task: reproduce, and write down what you saw.**
  - It reproduces → continue, and put the observed output in the commit message.
  - It does not → **do not fix it.** Delete the task from this plan with a one-line note saying what you measured, and move on. That is a completed task, not a skipped one.
  - The reproduction cannot be built → say so and leave the task open. An unbuildable reproduction is a finding about the finding.

And: **grep for what already tests the predicate before concluding it cannot fail.**

### Verification status of every item in this plan

| status | meaning |
|---|---|
| **REPRODUCED** | measured during authoring; the observation is in the task |
| **CARRIED** | from the council review, not yet measured — Step 0 applies in full |
| **BUILT-NOT-WIRED** | the code exists and nothing calls it; reachability is the claim |

---

## P1 — the `fix` implementation, and the loop it unblocks

**BUILT-NOT-WIRED, and the largest single gap in the package.** `review.loop` is built, tested, and has no production caller. It cannot have one: it takes an injected `fix(findings, checkpoint) -> (checkpoint|None, verified, candidate_run|None, baseline_run|None)` and this package has no implementation of it. Everything below is downstream:

- `--review` drives **one** round; a second cannot be bought
- `review_fixes` is priced in the quote and unspendable (`test_every_term_the_quote_prices_has_a_reachable_production_caller` lists it as known-unbuilt)
- §12.3's oscillation stop is wired and can only fire inside a loop nobody drives
- §13's "a blocker fixed after round 2 is *verified but not independently reviewed*" is a terminal no run can reach by the route §13 describes

**What `fix` must do**, from §13 and §6: apply a round's blockers in the synthesis worktree, re-verify **in a fresh clone the builder never touched**, cut a checkpoint, and hand back both `Run`s so `progress.from_runs` can say which failures are new.

**Files:**
- Create: `/home/khenrix/git/khenrix-utils/shared/lib/forge/fix.py`
- Modify: `/home/khenrix/git/khenrix-utils/shared/lib/forge/cli.py` (a `--review --rounds` path, or a `--fix` verb — decide in Step 1 and say why)
- Test: `/home/khenrix/git/khenrix-utils/tests/test_forge_fix.py` (**name it in `FORGE_SLOW_TESTS`** — it clones)

**Interfaces:**
- Consumes: `verify.verify_candidate` (§6's fresh-clone verification), `fleet.clone_seat`, `handover.create_synthesis_worktree`'s worktree, `review.Finding`, `progress.from_runs`.
- Produces: `fix.apply(run_dir, repo, *, findings, checkpoint, manifest, identity) -> tuple` matching `loop`'s injected contract exactly — four values, `None` for both runs only when it genuinely measured none.

**The fail-open this must not have:** the fix is authored by the ORCHESTRATOR, so this engine must not claim to have verified something it asserted. A `fix` that returns `verified=True` without having run the confirmed command in a fresh clone is the whole §6 premise inverted — the builder marking its own work. And it must spend from `progress.cap_remaining`, counting **starts** rather than completions, so a crashed fix stays spent.

- [ ] **Step 1: Decide the surface and write the decision down**

`--review <run-id> --rounds N` (one verb, drives the loop) versus `--fix <run-id>` (separate verb the orchestrator calls between rounds). Write the choice and the argument into the module docstring before writing code. The deciding question: §16 makes the orchestrator the synthesis author, so does *this engine* apply a blocker's fix, or does it verify one the orchestrator applied? **If the latter, `fix.apply` never edits the tree and the task is much smaller** — say which, explicitly.

- [ ] **Step 2: Write the failing tests** — at minimum:
  - a fix whose re-verify FAILS returns `verified=False` and the loop reports the blockers unresolved (§13)
  - the re-verify runs in a clone that is not the synthesis worktree — assert the path, as `--review`'s own test does
  - `progress.cap_remaining` decrements on a fix that STARTED and crashed
  - the four-value contract is returned, and the loop's refusal of a 2-tuple still fires

- [ ] **Step 3–6: implement, run, render, commit.** Then remove `review_fixes` from `known_unbuilt` in `tests/test_forge_packaging.py` — that test asserts set EQUALITY and will go red on its own when the term becomes reachable, which is the signal that this task is done.

---

## P2 — the two decisions to take with P1 in hand

### P2a — reprice §13's rounds, or refuse the flag

**CARRIED, with measurements already in hand.** `review` and `review_fixes` are priced in `gate.quote`. After P1, `review` has a caller and `review_fixes` does too — at which point this item may **evaporate**, which is why it sits here and not before P1. If P1 chooses a surface where the fix is the orchestrator's, both terms stay unspendable and this decision is live.

Measured previously: zeroing the terms stops `--review-rounds` moving `provider_calls` at all, changes what `confirm` cross-checks, and breaks 13 gate tests that exist to enforce "the quote responds to its inputs". **The ceiling and the price are two numbers**; separating them is the work. Whichever way it settles, settle all four terms at once — K6 added the review clone to `peak_disk_gb` on the argument that the quote already prices the unbuilt stage everywhere else, and that argument has to stay coherent.

### P2b — §12.5's rank in `cli._strongest`

**BUILT-NOT-WIRED.** `rubric` ranks; `_strongest` names nobody and says which dimension is missing. §12.5 needs four inputs together — a coverage report over the ledger's rows, §6.2's gate outcome, §13's review risk, §12.1's measured size — and the front end reaches **one**. After P1 the review risk exists; coverage needs `coverage.check` called on the stored ledger, and measured size needs `strategy`'s `Size` over the candidate bundles, which nothing serializes.

**The fail-open:** a rank taken on three of four dimensions with the fourth defaulted is a verdict over evidence nobody has. `rubric._unmeasured` already returns the FIRST unrankable dimension — keep that, and let `_strongest` name nobody until all four are real.

---

## P3 — `Status.setup` → `Status.builder_setup`

**CARRIED.** L1.2 closed the VERDICT half — a failing verifier setup can no longer return `PASS`, and `seat.py`'s docstring was corrected where it claimed the field was a fresh-clone measurement while `run_seat` fills it from the BUILDER's clone. The rename itself is what is left: an on-disk schema change plus a required keyword across ~42 `classify_seat` call sites, 30 of them in `test_forge_seat.py`, colliding with the existing top-level `verifier_setup` key on the attempt row.

It is its own task because a reviewer should be looking at a rename, not at a rename riding on something else.

---

## P4 — the s5/s6/s7 concrete defects

Each is one task. Step 0 applies to every one.

### P4a — s5, the review bundle inside `.git` with no digest
**CARRIED.** Its path is in the reviewers' argv and nothing digests it. Same shape as the council-result finding closed in Plan O, one artifact over — and that one **did** reproduce, so this deserves a real attempt. Fix shape: digest at write, verify at read, and a mismatch makes the round inadmissible rather than producing findings.

### P4b — s5, repo-local diff drivers and `git replace` refs
**CARRIED.** Unmeasured for the reviewers' own `git diff`, so a planted driver changes what a reviewer sees. `gitcmd.HOSTILE_ENV` handles the environment; this is repository CONFIG and refs, which the environment scrub does not touch. Note that M2 closed the environment half of exactly this family.

### P4c — s5, ultrareview has no journal and no durable receipt
**REPRODUCED while authoring: `grep -c 'journal\|receipt' shared/lib/forge/ultra.py` → 0.** §13.1's cloud review spends $5–25 and leaves nothing on the journal and no durable record. A crash between request and report loses the fact that money was spent. Fix shape: the write-ahead intent/done pair every other spending operation uses, plus a receipt `--collect` can read back.

### P4d — s6, `--collect` discards §9 drift and §14.1 orphans
**CARRIED.** Both are computed and neither reaches the handover.

### P4e — s6, the cloud review's missing idempotency guard
**CARRIED, and the refusal text is the evidence:** it tells the operator to re-trigger, with nothing stopping a second $5–25 charge.

### P4f — s6, the seat-count denominator is "seats that left a record", not "seats quoted"
**CARRIED, and NARROWER than the review recorded it — read `_seat_lines`' docstring before starting.** The finding says the denominator comes from a disk glob rather than `manifest.seats`. It does: `storage.seat_names(run_dir)` enumerates seats **with a record file**. But the docstring already argues at length against the shrink it knows about — counting `runner.run`'s results tuple, where a seat whose every attempt was refused has no verdict and "the denominator shrank to fit the numerator" — and it deliberately reads disk records instead, and deliberately renders a verdict-less seat as `failed`/`unusable` so it "adds to §16.1's denominator and to none of its numerators".

So the open case is the one **that argument does not cover**: a seat that left *no record file at all*, because the launcher died before `runner._record` ran. That seat is absent from the glob and the denominator shrinks again, by the other route. `manifest.seats` is the number the operator confirmed and cannot shrink. **Step 0 must establish that a record-less seat is reachable** — if `run_seat` always writes a record before it can fail, the finding is closed and this task is deleted.

### P4g — s6, the verify command truncated to step 0
**REPRODUCED while authoring.** `cli.py:867` renders `" ".join(manifest.verify[0].argv)`. A two-step confirmed command `[["make","verify"],["make","lint"]]` reports as `make verify` — the handover names a gate narrower than the one that ran. Fix shape: render every step; the field is a string, so decide the separator and say why.

### P4h — s7, "fusion, not selection" is unenforced
**CARRIED.** The collector rejects only a tree identical to B₁, and `cli.py:772` says `--strategy` cannot be checked. This is the skill's *stated purpose* — "picking a winner is the thing this skill exists not to do" — with no mechanical check behind it. Likely honest answer: it cannot be enforced mechanically, in which case **say so where the claim is made** rather than leaving the claim unqualified. Decide which, explicitly.

### P4i — s7, the eval-baseline contamination
**CARRIED.** L3.1 fixed the false oracle that would have penalised a corrected baseline, which was the prerequisite. This is the remainder.

### P4j — s7, `--collect`'s re-payable review
**CARRIED.** Related to P4e; check whether one fix closes both before writing two.

### P4k — s7, `mutate.py`'s bytecode purge and path containment
**CARRIED.** A mutation tool without path containment can write outside the tree under test.

### P4l — s7, `eval_trigger.py`'s type coercions
**CARRIED.** `"false"` → true; `null` → `"None"` → the abstention label. Both are a string standing in for a value, which is this project's recurring shape.

### P4m — s7, `reconcile`'s orphaned-marker destruction and `backup()` overwrite
**CARRIED, and it touches the one invariant this repo states twice:** reconcile is non-destructive. A destructive path in it contradicts `CLAUDE.md` directly.

### P4n — s7, `--seats 1` versus "all three CLIs"
**CARRIED.** The skill's description says all three; the flag admits one.

---

## P5 — the s1–s4 tails

One task per bullet unless noted; Step 0 applies to each. Grouped where one fix closes several.

### P5a — s1
- the symlink gate referent
- **the make memo key TOGETHER WITH `_scan_make`'s `--directory=`/`-C` parser gap** — two holes in one detector, fix as one
- the calibration aggregate
- the control-plane integrity tripwire
- **Fwork byte-binding** — L1.5's other door
- durable-state reconstruction
- `Seat.verified` over `sidecars is None`
- two index definitions
- `_gate_taints`' `isinstance` gate
- `_command_paths`' silent `continue`
- `_AMBIENT_SKILL`'s short-path refusals
- `screen.py` carrying this repository's allow-list into foreign repos
- `fleet.clone_seat`'s bare `IndexError`
- `Quote`'s unvalidated fields

### P5b — s2
- `no_change` with `proven_read=False`
- **the executed-and-refuted check recorded as `not-run`** — "we ran it and it said no" and "we did not run it" collapsing into one record, which is this project's defining defect shape
- §8.1's missing input half
- `RunnerError`-as-retry
- the empty fleet reaching `comparing`
- **`FLAKY` unreachable in production — REPRODUCED while authoring.** `_run_verdict` returns `FLAKY` only when `again is not None`, and **nothing in `shared/lib/forge/` ever passes `again`**. So §6.2's flake outcome cannot occur, and `GATE_RANK` ranks a value nothing produces. Either wire the rerun or say the outcome is unreachable where it is declared.
- `_clip`'s evidence truncation
- `_verify_dim`'s collapse

### P5c — s3
- `installed_closure`'s permutation collision
- `verify_materialized`'s copied fields **and** the size/cap-blind `bundle_hash`
- the criterion-to-claim binding
- seat provenance
- the journal creation race
- hash criteria not distinguishing a file from a symlink

### P5d — s4
- `snapshot.take`'s undeclared `FileNotFoundError`
- **`Size` accepting `(0,0,0)` — REPRODUCED while authoring; negatives and bools are ALREADY REFUSED (`StrategyError`).** So this task is only the zero case: a `Size` claiming a candidate changed nothing, which compares as the smallest and would win a size-gated strategy. The rest of the original finding is closed — do not re-open it.
- `_dir_digest`
- the lost-journal/no-fixes collapse

---

## P6 — the two NON-forge plans, found open when every plan was audited

Neither is forge work. Both were found by auditing all twenty plan documents by DELIVERABLE, because checkboxes were never ticked in any plan in this repository and prove nothing.

### P6a — `2026-07-30-per-provider-eval-gating.md`
**REPRODUCED while authoring.** Missing:
- `scripts/lib/portability.py` — cross-CLI structural checks, called from `checks.run_all()` and **never from `render.py`**, which is in every skill's source-hash closure (the plan says so, and it is the reason the file is separate).
- `evals/khenrix-setup/triggers.json` and `evals/khenrix-upgrade/triggers.json` — only `khenrix-audit` has one.

`scripts/eval_trigger.py` **exists** (469 lines, a `--self-test` make target that passes, and it reads `triggers.json`) — so the engine is alive and two of its three skills have no data. Fix the data and the missing module; do not rewrite the engine.

### P6b — `2026-08-02-skill-charts-and-feedback-loop.md`
**REPRODUCED while authoring: nothing landed.** Neither `scripts/lib/charts.py` nor `docs/skill-charts/` exists, and nothing in `Makefile` or `capabilities.toml` references either. Execute the plan as written, or retire it with a reason — it is eleven tasks and should not sit half-known.

---

## P7 — resume and parallel builders

**CARRIED.** `--start` cannot resume an interrupted run, and builders run serially (`runner.py`'s `for name in names`). The serial loop is what makes K6's wall-clock bound a real bound — `seats × attempts × window` — so **parallelising builders changes a number the operator was quoted**, and the two must land together or the quote becomes a lie in the operator's disfavour's opposite direction.

---

## Explicitly NOT in this plan, with reasons

These were considered and are deliberately excluded. Do not re-add them without new evidence.

1. **A path-based fix for §13's blindness.** `assert_ledger_is_out_of_reach`'s own docstring argues that relocating the review clone buys nothing — "a sibling of the run directory is reachable by a computable name, so it only relocates the `..`" — and names the only two real closures: an OS boundary, or a ledger absent from disk for the round. Neither is available to a stdlib-only package running three CLIs as the operator. `SKILL.md` tells the operator the panel is **blind by construction, not by containment**. Retired in Plan N Task 0.
2. **`gc` deleting refs by namespace prefix.** Does not reproduce: `_refs_of` builds prefixes as `f"{p}{run_id}/"` with the trailing slash and deletion passes the expected OID. Tested on the prefix-collision shape (`abc` vs `abcd`); each selects only its own refs.
3. **`2026-07-31-llm-forge-b1-baseline-foundations.md`.** Superseded, not missed. It named `state.py`, `gitio.py`, `secrets.py`, `__main__.py`; the substrate plan built the same roles as `storage.py`, `gitcmd.py` and `screen.py`. Each role was verified to exist.
4. **`Size` refusing negatives and bools.** Already refused with `StrategyError`. Only the `(0,0,0)` case survives, and it is P5d.
5. **The incomplete-ledger hole — SPEC-LEVEL, UNSOLVED, AND NOT SOLVABLE HERE.** An orchestrator that writes a *true but incomplete* ledger is invisible to every check in this design: §12.4's fallback fires on claims that are *unsatisfied*, not on claims never written down, and §13's panel reviews the candidate rather than the ledger's completeness. A row omitted is a row nothing here can miss. It is excluded because no task in this repository can close it — closing it needs a second, independent derivation of the requirement set to diff against, which is a different tool.

---

## Self-review

**1. Coverage.** Every open item from Plan N's deferral list appears here, plus the two non-forge plans that list never knew about. The four things closed since that list was written are in "Explicitly NOT in this plan" with their measurements, not silently dropped.

**2. Placeholder scan.** P1 Step 1 and P4g deliberately end in a DECISION rather than an implementation, and both say so and name the deciding question. That is not a placeholder — a plan that pre-decided them would be pre-deciding the thing the task exists to settle. Everything else names a file, a finding and a fix shape.

**3. What was measured while authoring**, so the next reader knows which claims carry evidence: ultrareview's missing journal (0 references), the verify-command truncation (a two-step command renders as one), `FLAKY`'s production unreachability (`again` never passed), `Size(0,0,0)`'s acceptance and its negatives/bools ALREADY being refused, and both non-forge plans' missing deliverables. Everything else is marked CARRIED and Step 0 governs it.

**4. Ordering constraints.** P2a may evaporate depending on P1's Step 1 decision, and is placed after it for that reason. P2b needs P1's review risk. P7's two halves must land together or the quoted wall-clock bound stops being a bound.

**5. What this plan does not claim.** It is the last plan for everything *written down*. It is not a claim that the engine is then free of defects — seven slices of review found these, and an eighth would find more. What it does claim is that after this, nothing anybody recorded is still waiting.
