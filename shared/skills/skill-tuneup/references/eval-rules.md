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
- A run with `delta.pass_rate >= 0` (the skill doesn't make answers worse) passes the
  gate and writes `evals/<t>/receipt.json` — the exact artifact `make precommit` gates
  on. The blind A/B winner is **recorded but advisory**, not a gate: on a strong executor
  it rewards the tighter baseline over a correct-but-more-thorough skill answer (a
  concision bias, not correctness — observed 2026-07-12 on hookify: a clearly positive
  assertion delta yet a blind loss to the tighter baseline). Read it when triaging a weak
  delta; never fail a non-negative-delta run on it. The assertion delta is the "does it
  help" signal.
- **llm-council is special**: its receipt is WRITTEN by `make eval SKILL=llm-council`,
  which gates on `fanout.py --self-test` alone, not the judge harness — executors run under
  `LLM_COUNCIL_DEPTH=1`, so the judged delta never exercises a real council and is advisory
  only. A live `--smoke` and `make council-test` are additional REQUIRED checks, not what
  earns the receipt; `council-test` runs inside `verify`/`precommit`.

## Scaffolding a missing eval set

If the target has no `evals/<t>/evals.json`: author 2-7 cases per the process doc
(prefer 2-5; a case earns its place by covering a contract nothing else does) —
`id`/`name`/`prompt`/optional `files`/`assertions`, plus a `notes` field explaining the
discriminating signal. Prefer inline-answer prompts ("Answer inline in prose — do NOT
enter plan mode or run tools") for decision-shaped skills; they are cheap, provider-safe,
and non-recursive. Checkpoint the proposed prompts with the user before running them.

## The loop

```bash
make eval SKILL=<t> PROVIDERS=claude     # iterate here (cheap)
make eval SKILL=<t> PROVIDERS=claude,codex,agy   # final gate (~3-4x tokens)
```

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
