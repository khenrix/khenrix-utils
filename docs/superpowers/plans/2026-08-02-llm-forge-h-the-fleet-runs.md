# llm-forge Plan H: the fleet runs

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Launch three seats, harvest what each produced, verify each candidate through §6's five steps in order, and record an honest status for every one — without fusing them.

**Architecture:** Seven plans built machinery and one built a gate. Nothing has ever launched a provider. This plan writes the caller that does: a per-seat runner (clone → setup → provider → harvest), a status value with §8's four **independent** dimensions, the §6 chronology finally sequenced rather than enforced at one joint, and a run loop that moves the state machine and journals every intent before its result. It stops at three verified candidates on disk. **Choosing among them is the next plan.**

**Tech Stack:** Python 3.11+ stdlib only. `git` 2.53 via `shared/lib/forge/gitcmd.py`. The council engine at `shared/lib/council/engine.py` for provider invocation. pytest via `uvx`.

## Global Constraints

- **Python stdlib only.** No pip dependencies.
- **Commands run as argv lists, never through a shell.**
- **Git is located by asking git**, never by string-joining `.git`. Every git call goes through `gitcmd.git`.
- **Fail closed.** A measurement that could not be taken is `None`/UNKNOWN, never an empty success.
- **A verdict must never read cleaner than its evidence.**
- **A comment asserting something the code does not do is a defect** — and so is a claim about a configuration the package refuses, unless you measured that it refuses it. Prefer measuring the *mechanism* you are about to name: the recurring defect in this project is a sentence measured on the wrong axis.
- **§19, verbatim and binding: the 120 s agy print-timeout cap is already gone upstream.** The engine computes `pt = max(5, int(timeout) - 5)`. **An implementer must not re-add a cap or build a second timeout mechanism.** The history is on record because during this design's own review agy died at 124 s with zero tokens, the panel silently degraded to 2 of 3, and the engine misclassified the death as `agy_error` rather than `timeout`.
- `shared/lib/forge/**` is source of truth; `marketplaces/**` is render output. Never hand-edit it — run `make render`.
- Every task ends with `make render`, an explicit-pathspec `git add` **including `marketplaces`**, then `make verify` and `make precommit`, then the commit. **Run the gates unpiped and capture `$?`** — a pipe reports the pipe's exit status.
- Use **`scripts/mutate.py`** for mutations. It refuses `pytest`'s exit 5 and requires a green unmutated baseline. Discard any row whose mutant broke the syntax.

## The disk question, and why this plan answers it by parameter

The quote says a default run peaks at **56.7 GB** — 17 coexisting clones at ~3.3 GB — and §4 says to *"reject the run if the disk estimate cannot sustain independent clones."* Every driver is a spec decision that defends itself: no hardlinks (§4: *"Do not trade source-object safety for space"*), §8.1's preserved retry clones, §6's fresh verifier per candidate.

**So this plan does not shrink the fleet; it makes the fleet the operator's declared number.** `seats` and `attempts` are `quote`'s parameters, and the branch review measured that they did not survive the gate. **That is now fixed and verified (2026-08-03):** `Quote`, `Confirmation` and `Manifest` all carry `seats` and `attempts` — `Manifest` is now **17** frozen fields, not 15 (`…, created_at, seats, attempts`). The write-once record's schema changed before any real run was opened, which was the whole reason it had to land first. Task 5 may rely on this; it is measured, not assumed. **Do not add a retention policy that deletes a failed attempt**; §8.1 requires it preserved as partial input, and a plan that quietly reclaims it makes the quote a lie in the other direction.

## What Plan G hands you, verbatim

Verify these against the code before relying on them. Every plan here has had draft code that was wrong, and several controller instructions have been measurably wrong. In each of the last twenty tasks an implementer overturned something by measuring instead of complying, and every overturn held under independent review — two of them found the finding was *larger* than stated.

- `gate.must_show(report, quote_, command)` — **three** arguments; a second argument that is not a `Quote` is a hard `GateError`, not an optional shape — `gate.confirm(report, quote_, answers) -> Confirmation`, `gate.open_run(report, confirmation, run_id) -> Path`, `gate.quote(report, *, seats=3, attempts=3, review_rounds=2, ultrareview=True) -> Quote`, `gate.provider_invoking_verify`, `gate.SPENDS/REMEDY/UNRESOLVED`, `gate.GateError`.

  > **Updated 2026-08-03 — re-verified against the code after Plan G's second fix wave.**
  > `Confirmation` now carries **eight** fields, not five:
  > `setup, verify, on_calibration_failure, strategy, accepted_gaps, author, seats, attempts`.
  > **It validates and normalizes them in `__post_init__`, not in `confirm`** — so an invalid
  > `Confirmation` cannot be constructed by any route, and `open_run`'s `isinstance` check is
  > load-bearing rather than decorative. Do not re-validate these fields in a caller; that
  > duplication is the exact defect the fix removed. Counts go through
  > `runstate.count(name, value, source, *, floor=1)` — **call it, do not re-spell it.**
  > `author` is a required `(name, email)` answer, because `gitcmd` pins global and system git
  > config to `/dev/null`, so nothing below the gate can resolve an identity.
- `preflight.inspect_repo(repo, selected_untracked=()) -> Report`, `preflight.refusals(report)`, `preflight.PreflightError`.
- `runstate.Manifest` (**17** frozen fields — `seats` and `attempts` added by Plan G's fix wave), `write_manifest`/`read_manifest`, `snapshot_refs(repo, selected, *, forge_refs=())`, `snapshot_index(repo)`, `drift(manifest, repo)`, `reconstruct(run_dir, repo)`, `PHASES`, `TERMINAL`, `State(phase, round, attempt, verified_checkpoint, deliverable_checkpoint)`, `advance(state, phase)` — **every non-terminal phase can now reach `failed` and `source_diverged`**, `write_seat`/`read_seat`/`write_state`/`read_state`, `OUTCOME_UNKNOWN`, `StateError`, `TransitionError`, `ManifestError`.
- `journal.Journal(path).record(event, *, operation_id, **data)`/`.read()`, `journal.Event`, `journal.intent(kind)`/`done(kind)`, `journal.orphans(events)`, `JournalError`. Every record carries `pid`, `process_start`, `boot_id` and a `*_source` for each — **two `"unavailable"` sentinels compare equal**, so a liveness check must consult `*_source == "proc"` on both records before comparing either value.
- `storage.run_root`, `new_run_id`, `atomic_write`, `exclusive_write`, `append_line`, `manifest_path`/`journal_path`/`seat_state_path`/`state_path`/`seat_names`, `Quota`, `StorageError`.
- `fleet.clone_seat(repo, baseline, dest, *, name, identity, template_dir=None) -> Seat(path, branch, verified, replayed)`, `fleet.forge_child_env(repo)`, `SeatError`/`FleetError`.
- `baseline.materialize(repo, run_dir, facts, selected_untracked, run_id, author=None) -> Baseline`.
- `harvest.record(seat_path, *, quota=None)`, `harvest.Phases(f0, fsetup, fwork, fverify)`, `harvest.artifact_set(phases, seat_path, base_commit) -> ArtifactSet(paths, origin, setup_overlap, tracked_diff, verify_overlap)`.
- `bundle.build(seat_path, artifacts, baseline, *, contract=None) -> CandidateBundle`, `bundle.materialize`, `bundle.with_gate_measurement`, `SidecarEntry`, `BundleError`.
- `verify.build_verifier(repo, baseline, candidate, dest, *, identity, contract, command) -> Verifier(path, candidate, contract, baseline_surface, candidate_surface)`, `validate_materialized`, `run_setup`, `fixed_point`, `calibrate`, `classify`, `gate_surface`, `Command.parse`, `Step`, `Run`, `OUTCOMES`.
- `inspect.repo_facts`, `rejections`, `GeneratorContract`, `detect_generators` (returns the **empty** contract for every repository).
- `gitcmd.git(...)`, `READONLY`, `NO_DAEMON_CACHE`, `NO_HOOKS`, `NO_DIFF_DRIVERS`, `HOSTILE_ENV`, `zero_oid`, `GitError`.
- `council.engine`: `ProviderSpec` (already has `min_chars` and `validator` fields), `score_seat(output, prompt_token=None, min_chars=MIN_SUBSTANTIVE_CHARS)`, `MIN_SUBSTANTIVE_CHARS = 400`, `make_sentinel()`, `apply_sentinel(prompt, token)`, `classify_sentinel`, `build_real_spec(name, prompt, timeout, …)`, `extract_*_json`, `sum_usage`.

### Five inherited facts that shape this plan

1. **`PASS` never reads `baseline_run`.** `_run_verdict` returns `PASS` on `cand.exit_code == 0` without consulting `base`. Calibration makes `BASELINE_RED_…` producible; it contributes nothing to `PASS`. Do not let a run report imply otherwise.
2. **`detect_generators` returns the empty contract**, so nothing is ever admitted and `PASS` requires a gate that rewrites no tracked file. On this repository `make verify` cannot satisfy that.
3. **§6's chronology is enforced at one joint only** — `run_setup` calls `validate_materialized`, and nothing else orders anything. **Sequencing it is this plan's job**, and it is Task 4.
4. **Nothing serializes `CandidateBundle`.** A writer that carries `gate_delta` without `gate_surface` re-creates a half-record `with_gate_measurement` refuses; the classifier's fourth taint is the tripwire. Carry both or neither.
5. **The closure tests in `tests/test_forge_seams.py`** assert, by AST over call sites, that every index-loading or hook-firing `gitcmd.git` call carries its flags. **You are adding engine calls into seat trees; expect them to fire and add the flags rather than the exemption.** They exist because the same hole was closed four times behind fixtures that could not see it.

## Deliberately out of scope

Fusion of any kind: §10's claim ledger, §11's agreement rule, §12's strategy, fallback and strongest-seat rubric, §13's review and ultrareview, §16's handover. This plan ends with three verified candidates and their statuses on disk; **nothing chooses between them.** Also out: §14's supervisor/payload receipt topology (a process-launcher change with its own blast radius), and `--gc`.

## File Structure

- **Create `shared/lib/forge/seat.py`** — §8's status value and its rules. One responsibility: what a seat produced and how much of it is trustworthy.
- **Create `shared/lib/forge/runner.py`** — one seat end to end, and the run loop over three. One responsibility: doing the thing, in order, journalled.
- **Create `tests/test_forge_seat.py`, `tests/test_forge_runner.py`** — added to `FORGE_TESTS` **in the task that creates them**.
- Modify `shared/lib/forge/verify.py` only if Task 4 needs a composed entry point; prefer composing in `runner.py`.

---

### Task 1: Seat status, four dimensions that do not collapse

**Files:** Create `shared/lib/forge/seat.py`, `tests/test_forge_seat.py`; modify `Makefile`.

**Interfaces produced:**
- `seat.Status` — frozen: `process: str` (`valid`/`invalid`), `artifacts: str` (`usable`/`unusable`), `proven_read: bool`, `forge: str` (`completed`/`partial`/`no_change`/`failed`), `setup: str`, `verify: str` (each `pass`/`fail`/`not-run`).
- `seat.classify_seat(*, process, artifacts, proven_read, changed, setup, verify, rationale=None) -> Status`.
- `seat.SeatStatusError(RuntimeError)`.

**Why four dimensions.** §8 opens with the reason: *"collapsing them is what let a silently-failed seat read as success."* Three rules are load-bearing and each needs its own test:

- *"A seat with useful artifacts but no proof token is **`partial`**, not completed."*
- *"A `no_change` requires a substantive rationale and independent verification — a correct conclusion that the task needs no edit must not be discarded."*
- *"Passing verify is **recorded, not required** for a seat to inform synthesis. A setup failure does not proceed merely because it produced files."*

- [ ] **Step 1: Write the failing test**

```python
# tests/test_forge_seat.py
def test_useful_artifacts_without_the_proof_token_are_partial_not_completed():
    """§8: the seat did the work and did not prove it read the task. Collapsing this into
    `completed` is what let a silently-failed seat read as success."""
    s = seat.classify_seat(process="valid", artifacts="usable", proven_read=False,
                           changed=True, setup="pass", verify="pass")
    assert s.forge == "partial"


def test_a_no_change_without_a_rationale_is_refused():
    """§8: a correct conclusion that the task needs no edit must not be discarded — but it
    has to be argued, or `no_change` is indistinguishable from a seat that did nothing."""
    with pytest.raises(seat.SeatStatusError):
        seat.classify_seat(process="valid", artifacts="unusable", proven_read=True,
                           changed=False, setup="pass", verify="pass")
    s = seat.classify_seat(process="valid", artifacts="unusable", proven_read=True,
                           changed=False, setup="pass", verify="pass",
                           rationale="the retry already backs off; adding one would double-sleep")
    assert s.forge == "no_change"


def test_a_failing_verify_is_recorded_and_does_not_change_the_forge_status():
    """§8: passing verify is RECORDED, not required, for a seat to inform synthesis."""
    a = seat.classify_seat(process="valid", artifacts="usable", proven_read=True,
                           changed=True, setup="pass", verify="fail")
    b = seat.classify_seat(process="valid", artifacts="usable", proven_read=True,
                           changed=True, setup="pass", verify="pass")
    assert a.forge == b.forge == "completed"
    assert (a.verify, b.verify) == ("fail", "pass")


def test_a_setup_failure_does_not_proceed_on_the_strength_of_its_files():
    """§8, verbatim: "A setup failure does not proceed merely because it produced files.\""""
    s = seat.classify_seat(process="valid", artifacts="usable", proven_read=True,
                           changed=True, setup="fail", verify="not-run")
    assert s.forge == "failed"


def test_the_four_dimensions_are_independent():
    """A status that can be reconstructed from one field has collapsed. Every dimension must
    vary while the others are held."""
    base = dict(process="valid", artifacts="usable", proven_read=True, changed=True,
                setup="pass", verify="pass")
    assert seat.classify_seat(**{**base, "process": "invalid"}).forge == "failed"
    assert seat.classify_seat(**{**base, "artifacts": "unusable"}).forge != "completed"
    assert seat.classify_seat(**{**base, "proven_read": False}).forge == "partial"
```

- [ ] **Step 2: Run to verify it fails** — `ModuleNotFoundError: No module named 'forge.seat'`.
- [ ] **Step 3: Implement.** Decide the precedence between the rules and **state it** — a `setup="fail"` seat with usable artifacts and no proof token is `failed`, not `partial`, and the order that produces that is a decision a reader must be able to check.
- [ ] **Step 4: Run to verify it passes.**
- [ ] **Step 5: Wire `FORGE_TESTS`; run `uvx pytest tests/test_forge_packaging.py -q`.**
- [ ] **Step 6: Mutate** — one site at a time, with a control. At minimum: `partial` → `completed`; drop the rationale requirement; make `verify` change `forge`; make `setup="fail"` fall through.
- [ ] **Step 7: Render, stage `marketplaces`, gate, commit** `feat(forge): a seat status whose four dimensions do not collapse`.

---

### Task 2: Forge's provider spec — validity that does not eat a good seat

**Files:** Create/modify in `shared/lib/forge/seat.py`; test in `tests/test_forge_seat.py`.

**Interfaces produced:** `seat.forge_spec(name, prompt, timeout, **kw) -> council.engine.ProviderSpec`, and `seat.read_proof(output, token) -> bool`.

**Why.** §8.1 is explicit about a live defect in the shared engine: `evaluate()` runs `score_seat` with `MIN_SUBSTANTIVE_CHARS = 400` plus a SENTINEL check, *"so a seat that implements flawlessly for forty minutes and signs off with 'Done — `make verify` passes.' scores `non_substantive`, which then triggers the retry loop to re-run the same argv **in the same cwd**, on top of its own half-finished work, twice by default."*

The fix has two halves and §8.1 spells out the one that is easy to get wrong:

> `min_chars = 0` and no sentinel invalidation for forge seats … **"No sentinel invalidation" means precisely: the token check is recorded and stripped of its power to invalidate.** It feeds §8's `proven read` dimension (`completed` vs `partial`) and never triggers the retry loop. **Under the other reading — no sentinel at all — `proven read` would be unmeasurable.**

`ProviderSpec` already carries `min_chars` and `validator`. **Check what `validator` is actually called with and what its return value does** before assuming the injection point does what you need; `eval_harness.py:245` already does the `min_chars = 0` opt-out and is the precedent to read.

- [ ] **Step 1: Write the failing test**

```python
def test_a_short_but_complete_seat_is_not_invalidated():
    """§8.1's live defect: a seat that works for forty minutes and signs off in one line
    scores `non_substantive` and gets re-run IN THE SAME CWD on its own half-finished work."""
    spec = seat.forge_spec("claude", "do the thing", 900)
    assert spec.min_chars == 0
    verdict = spec.validator("Done — `make verify` passes.", None) if spec.validator else None
    assert verdict is None or verdict.get("valid") is not False


def test_the_sentinel_is_recorded_and_cannot_invalidate():
    """The precise reading: the token check is RECORDED and stripped of its power to
    invalidate. No sentinel at all would make `proven read` unmeasurable."""
    token = engine.make_sentinel()
    assert seat.read_proof(f"I read the task. {token}", token) is True
    assert seat.read_proof("I read the task.", token) is False
    spec = seat.forge_spec("codex", engine.apply_sentinel("p", token), 900)
    v = spec.validator("no token here", token) if spec.validator else None
    assert v is None or v.get("valid") is not False, "a missing token must not invalidate"
```

- [ ] **Steps 2–7** as Task 1. Mutants at minimum: `min_chars=0` → `400`; the validator made to invalidate on a missing token; `read_proof` always True. Commit `feat(forge): a seat's validity is recorded, never a reason to re-run it in place`.

---

### Task 3: One seat, end to end

**Files:** Create `shared/lib/forge/runner.py`, `tests/test_forge_runner.py`; modify `Makefile`.

**Interfaces produced:**
- `runner.SeatResult` — frozen: `name`, `attempt: int`, `seat: fleet.Seat | None`, `status: seat.Status`, `artifacts`, `candidate`, `run: verify.Run | None`, `path: Path`.
- `runner.run_seat(manifest, run_dir, baseline, *, name, attempt, identity, launch) -> SeatResult`.
- `runner.RunnerError(RuntimeError)`.

**`launch` is injected**, and every test in this plan passes a fake. **No test in this plan may invoke a real provider** — that is what §5.2's quote is about, and a suite that spends money is one nobody runs.

**§8.1's retry rule is structural here:** *"Every retry attempt gets a fresh clone. The failed attempt is preserved as partial input. Never a reset-and-rerun in place."* So `attempt` is a parameter, the seat path carries it, and nothing deletes a previous attempt.

The order inside one seat: `clone_seat` → `harvest.record` (F0) → setup → `harvest.record` (Fsetup) → `launch` → `harvest.record` (Fwork) → `artifact_set` → `bundle.build`.

- [ ] **Step 1: Write the failing test**

```python
def test_a_retry_gets_a_fresh_clone_and_the_failed_attempt_survives(tmp_path):
    """§8.1: never a reset-and-rerun in place. The failed attempt is partial INPUT, so
    deleting it is losing evidence, not tidying."""
    ...
    first = runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT,
                            launch=_fake(lambda p: write(p, "half.py", "half\n")))
    second = runner.run_seat(m, run, b, name="claude", attempt=2, identity=IDENT,
                             launch=_fake(lambda p: write(p, "done.py", "done\n")))
    assert first.path != second.path
    assert first.path.exists(), "the failed attempt is preserved as partial input"
    assert not (second.path / "half.py").exists(), "attempt 2 did not start on attempt 1's work"


def test_a_launch_that_writes_nothing_is_no_change_not_failed(tmp_path):
    """§8: a correct conclusion that the task needs no edit must not be discarded."""


def test_the_seats_status_is_written_where_a_resume_will_read_it(tmp_path):
    """§14.2 lists per-seat atomic files among `--collect`'s inputs. A status held only in
    memory is one a crash erases."""
```

Write the fixtures and the remaining bodies yourself; `_fake(fn)` should be a launch callable that runs `fn(seat_path)` and returns a plausible provider result, so the chain is real and only the provider is not.

- [ ] **Steps 2–7** as before. Mutants: reuse the attempt-1 path; delete the failed attempt; skip the `Fsetup` inventory; write the seat status before the harvest rather than after. Commit `feat(forge): one seat, cloned fresh per attempt, harvested into a candidate`.

---

### Task 4: The chronology, sequenced

**Files:** Modify `shared/lib/forge/runner.py`, `tests/test_forge_runner.py`.

**Interfaces produced:** `runner.verify_candidate(manifest, run_dir, baseline, candidate, *, name, identity, calibration) -> tuple[str, str, verify.Verifier, verify.SetupResult | None]`.

> **Corrected 2026-08-03 after the task landed — the line above previously described an interface
> that was never built.** Two changes the implementer made and independent review upheld:
> **`contract` is gone**, sourced from `manifest.generator_contract`, because `build_verifier`
> compares contract **IDs only** — so a second contract sharing a non-empty id while carrying
> different `relations` clears the check and silently changes what `fixed_point` admits.
> **`calibration` is required, not `None`-able**: there is no honest default, since `classify`
> reads `baseline_run` only after the candidate's gate has failed, so a fabricated green `Run`
> claims a NEW failure nothing measured and a fabricated red one claims §6.2's baseline-red
> outcome on the evidence of no calibration — both reading cleaner than their evidence, in
> opposite directions. The return is a **4-tuple**; the fourth element is the verifier's own
> setup result, so a caller can record a failed verifier setup's exit code rather than only its
> sentence.

**Why.** §6 lists five steps and the engine currently orders exactly one of them. Verbatim: *"1. Harvest the seat (§7) — **before** verification. 2. Materialize the harvested candidate in a brand-new clone built through the same path as §4. 3. Run the confirmed setup command there. 4. Run the confirmed verify command there. 5. Repository hooks and any post-seat git configuration are disabled in verifier clones."*

And: *"The materialized candidate is hash-validated against the bundle **before** setup runs."*

- [ ] **Step 1: Write the failing test**

```python
def test_the_five_steps_run_in_the_order_section_6_lists(tmp_path):
    """The order is the argument, not an implementation detail: hash-validating after setup
    validates a tree setup has already moved, and §6 says so."""
    calls = []
    # spy on validate_materialized / run_setup / fixed_point through the real chain


def test_a_candidate_that_fails_hash_validation_never_reaches_setup(tmp_path):
    """Step 2 before step 3, enforced rather than documented."""


def test_the_verdict_carries_the_calibration_when_one_was_taken(tmp_path):
    """§6.2's BASELINE_RED_… is a claim ABOUT a calibration; a verdict that ignores it while
    one exists is a verdict reading cleaner than its evidence."""
```

- [ ] **Steps 2–7.** Note the inherited fact: `PASS` does not read `baseline_run`, so the third test pins that the calibration *reaches* `classify`, not that it changes a `PASS`. Say so in the docstring rather than implying the stronger thing. Commit `feat(forge): §6's five steps in the order §6 gives them`.

---

### Task 5: The run loop, journalled and resumable

**Files:** Modify `shared/lib/forge/runner.py`, `tests/test_forge_runner.py`, `tests/test_forge_seams.py` (**above** the `SEAM CLASS: refusals` banner).

**Interfaces produced:** `runner.run(run_dir, repo, *, identity, launch, seats=None) -> tuple[SeatResult, ...]`.

**Why journalled first.** §14.1: *"Write-ahead intent, then result. Append `…_start` before invoking and `…_done` after. A crash between them is distinguishable from a crash before — the only way idempotence can hold at all."* An orphan is `outcome_unknown` and is **never silently retried**.

`run` reads the manifest for the commands and — **if Plan G's fix wave landed the schema change, which you must check rather than assume** — for `seats`/`attempts`; otherwise it takes those two as arguments and says so. It advances the state machine through `setting_up → building → harvested → comparing`, journals every operation, and refuses to start when `drift` is non-empty.

- [ ] **Step 1: Write the failing test**

```python
def test_a_run_that_dies_mid_seat_reconstructs_as_outcome_unknown(tmp_path):
    """§14.1: the engine cannot distinguish never-started from partly-ran from completed —
    so it records enough that a READER can, and never retries what it cannot classify."""


def test_a_run_refuses_to_start_when_the_users_repository_has_moved(tmp_path):
    """§9: transition to `source_diverged` and do not continue to handover automatically."""


def test_every_phase_the_loop_enters_was_a_declared_transition(tmp_path):
    """`advance` raises on an undeclared edge, so a loop that moved the state by assignment
    would silently hold a graph the spec does not."""
```

Plus two seams above the banner: the runner/journal seam (an intent with no done reconstructs as an orphan through the **real** `reconstruct`), and the runner/runstate seam (the phase the loop wrote is one `advance` would allow).

- [ ] **Steps 2–7.** Then sweep `shared/lib/forge/**` for prose this plan falsified — in particular anything saying nothing launches a seat, or that §6's chronology is unsequenced. Commit `feat(forge): the loop that runs three seats and can be resumed from disk`.

---

## Self-review

**Spec coverage.** §8's four dimensions and its three rules → Task 1. §8.1's injectable validity, `min_chars=0`, sentinel-recorded-not-invalidating, fresh clone per retry → Tasks 2–3. §6's five ordered steps and the pre-setup hash validation → Task 4. §5 step 4 (build) → Tasks 3 and 5. §14.1's write-ahead journal and `outcome_unknown` → Task 5. §9's `source_diverged` refusal → Task 5. §19's no-second-timeout → a Global Constraint binding every task.

**Deliberately out of scope, each with a later home:** all fusion (§10, §11, §12, §13), handover (§16), §14's supervisor/receipt topology, `--gc`. §5 step 5's strategy rule is *recorded* by the gate and *applied* in the next plan; this plan must not apply it.

**What this plan does not close, stated rather than implied.** `PASS` still never reads `baseline_run`. `detect_generators` still returns the empty contract, so on this repository a candidate cannot reach `PASS` at all — the runner will report `GATE_CHANGED` or a failure, honestly, and a test asserting `PASS` on this repo's own gate would be asserting a fiction. Remotes and configuration remain §9's two unrecorded items. `filter.<d>.clean` remains open with its closure re-filed as detection.

**Placeholder scan.** Tasks 3–5 give test names and docstrings but leave several bodies to the implementer, deliberately: each needs a fake `launch` and a real chain around it, and a body written here would be a fixture invented without measuring. Every one states the property it must prove.

**Type consistency.** `seat.Status` is produced by `classify_seat` and carried on `runner.SeatResult`; `runner.run_seat` returns what `runner.run` collects; `verify_candidate` returns `classify`'s `(outcome, reason)` plus the `Verifier` whose `.candidate` carries the filled `gate_delta` and `gate_surface` — **not** the input bundle, which reads `None` and classifies `GATE_CHANGED`.

**One risk worth naming.** Task 3 injects `launch`, and every test passes a fake. That is correct — a suite that spends provider calls is one nobody runs — but it means **nothing in this plan proves the real provider path works**. The first real invocation will be the skill's own eval, two plans away. Name that gap in `runner`'s docstring rather than letting the green suite imply coverage it does not have, and keep the `launch` signature narrow enough that the real adapter is obviously the same shape.
