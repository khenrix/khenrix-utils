# Eval rules — running the gate and iterating to green

The repo's eval harness IS the benchmark (`docs/skill-eval-process.md` is the authority;
read it before scaffolding). Key invariants:

- Executors run **read-only by default** — the harness calls `fanout.make_readonly`, so all
  three are mechanically constrained: claude (plan mode, plan-file writes suppressed), codex
  (read-only sandbox), agy (`--mode plan`, since 1.1.1). agy additionally gets two soft
  layers (a READONLY_POSTURE line + a throwaway git-worktree cwd) as defense in depth.
  Plan mode is a mechanical write barrier, not an OS sandbox — still less sealed than
  codex's, so lower-risk to run mid-tuneup rather than sealed.
- **Baseline caveat**: `without_skill` is the executor's ambient env; if the old skill
  version is installed (a prior `make khenrix-refresh`), the comparison is new-vs-old,
  not with-vs-without. Iterate BEFORE refreshing for the cleanest signal.
- For a non-deterministic target, `delta.pass_rate >= 0` (the skill doesn't make answers
  worse) passes the gate and writes `evals/<t>/receipt.json` — the exact artifact
  `make precommit` gates on. The blind A/B winner is **recorded but advisory**, not a gate: on a strong executor
  it rewards the tighter baseline over a correct-but-more-thorough skill answer (a
  concision bias, not correctness — observed 2026-07-12 on hookify: a clearly positive
  assertion delta yet a blind loss to the tighter baseline). Read it when triaging a weak
  delta; never fail a non-negative-delta run on it. The assertion delta is the "does it
  help" signal.
- **Gate selection is target-only**: ask
  `checks.requires_deterministic_gate(<target>)`, never infer it from a receipt. This stays
  correct when the receipt is absent, corrupt, stale, or names the wrong certifier.

## Scaffolding a missing eval set

If the target has no `evals/<t>/evals.json`: author 2-7 cases per the process doc
(prefer 2-5; a case earns its place by covering a contract nothing else does) —
`id`/`name`/`prompt`/optional `files`/`assertions`, plus a `notes` field explaining the
discriminating signal. Prefer inline-answer prompts ("Answer inline in prose — do NOT
enter plan mode or run tools") for decision-shaped skills; they are cheap, provider-safe,
and non-recursive. Checkpoint the proposed prompts with the user before running them.

## Select and run the target's gate

For `requires_deterministic_gate(<target>) == True`:

```bash
make eval SKILL=<t>
```

That command must run the certifier named for the target in `checks.SELF_TEST_CERTIFIERS`
and write matching deterministic evidence. Judge-provider output is advisory for deciding
whether the certifier passed, but the schema-3 shipping receipt also requires that advisory
run over the canonical Codex+agy panel. For llm-council, the certifier is `fanout.py --self-test`; a live `--smoke`
and `make council-test` are additional REQUIRED checks, not what earns the receipt
(`council-test` also runs inside `verify`/`precommit`).

Only when `requires_deterministic_gate(<target>) == False` use the judge loop:

```bash
make eval SKILL=<t> PROVIDERS=codex      # optional cheap iteration; advisory only
make eval SKILL=<t>                      # canonical Codex+agy shipping panel
```

Only the canonical providers/judge/mode policy can refresh a receipt; a narrowed green run
leaves the prior receipt untouched. The final panel is fixed and may not be narrowed or
reordered. A receipt's current shape
can verify evidence through `checks.is_self_test_gated(<target>, receipt)`; it never chooses
which branch applies.

**Run it in the background and with no other token-heavy agent work in flight.** An eval is
strictly serial — every case × condition × judge call in sequence — so it routinely outlives
a foreground command cap, and a kill loses the whole run. Reading files alongside it is
fine; a council fan-out is not. Raise the per-attempt cap with `TIMEOUT=` rather than
`MODE=deep`, which would also change reasoning depth.

Classify every failure before touching anything:

| Class | Signal | Action |
|---|---|---|
| Real regression | deterministic fail tied to a specific edit | fix the edit; re-run |
| Assertion regression | behavior intentionally changed; assertion now wrong | update the assertion; re-run |
| Invalid run — executor | `errors == 1` with `reason` a timeout/crash (or a `⚠ INVALID RUN` line naming one) — the executor died and was graded on an empty answer | NOT a regression: the delta is unmeasured. Re-run once serially; if it recurs, the eval is under-timed — raise `TIMEOUT=`. Does not consume the 5-iteration fix cap |
| Invalid run — judge | `errors == 1` with `reason: "judge returned no verdict"` — the *answer* was fine; the grade is the artifact | Also not a regression, and `TIMEOUT=` won't help: the judge already retries twice. Plain re-run, then check the judge model/quota. Same cap exemption |
| Flaky / judge noise | same input passes sometimes | re-run ONCE; if it passes, accept and note it — do NOT edit the skill to chase a noisy grader |

## Raw regrade for changed contracts

For every eval case mapped to a changed contract cell, open both conditions' raw
`answer.md` and `grading.json` plus the iteration's `benchmark.json`. Independently regrade
the frozen answers against the canonical assertions and recompute the affected counts; do
not accept a judge observation, summary, blind verdict, or receipt as semantic
authentication. A false green is an `Eval-gap` or real regression and must be fixed and
rerun. Label each regraded case with the counterexample protocol's evidence labels, and
carry every applicable case not regraded as `UNINSPECTED` with its ship impact. A green
receipt proves that its named harness ran, not that its semantic grades survived this check.

An invalid run **biases** the delta rather than merely adding noise: it scores 0 and is
averaged into its own side, so a with_skill error sinks the delta and a **baseline** error
inflates it. The gate now fails closed on any invalid run (where the delta is the gate), so
you cannot earn a receipt off one — but read the reason before re-running, because a
repeated with_skill-only timeout can be a genuine regression (a skill edit that makes the
executor do far more work).

## Cap

Hard cap: **5 iterations** (or the user's stated cap). On cap-reached-not-green: STOP,
record the unresolved failures (assertion + class + last result) in the run summary and
the run log, and hand the decision to the user. Never loop past the cap.

## Cost honesty

Say up front: any source change to the target re-arms its receipt, so even a one-line fix
costs an eval run before it can be committed. Fold that into the proportionality call at
the checkpoint — sometimes the right answer is to batch small fixes.
