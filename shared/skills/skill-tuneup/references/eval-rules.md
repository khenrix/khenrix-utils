# Eval rules — running the gate and iterating to green

The repo's eval harness IS the benchmark (`docs/skill-eval-process.md` is the authority;
read it before scaffolding). Key invariants:

- Executors and judges use fresh working directories with normal installed skills,
  plugins, MCPs, HOME and authentication. Executors use medium effort; grading and review
  use xhigh; Gemini uses high throughout. Native controls deny known writers where
  supported, but trusted shell access is not OS confinement. No messages or other writes
  are authorized by evaluation.
- `with_skill` receives a bounded explicit source closure. `without_skill` receives no
  explicit copy, but installed skills remain discoverable in both conditions. The delta
  measures explicit guidance under native configuration, not target absence or causal
  uplift. Keep source hashes, commands, actual tool evidence and observed CLI versions.
- A run with `delta.pass_rate >= 0` (the skill doesn't make answers worse) passes the
  gate and writes `evals/<t>/receipt.json` — the exact artifact `make precommit` gates
  on. The blind A/B winner is **recorded but advisory**, not a gate: on a strong executor
  it rewards the tighter baseline over a correct-but-more-thorough skill answer (a
  concision bias, not correctness — observed 2026-07-12 on hookify: a clearly positive
  assertion delta yet a blind loss to the tighter baseline). Read it when triaging a weak
  delta; never fail a non-negative-delta run on it. The assertion delta is the "does it
  help" signal.
- Fanout plan/execute receipts also require their named deterministic contracts. Native
  tool discovery, actual service reads, collaboration and owner application need their
  own scoped evidence; an answer-quality benchmark alone cannot establish them.
- Current executors and judges share the native profile registry, parsers and process
  supervisor. Automatic transport retries are zero.
  Malformed successful judge JSON receives one fresh semantic retry and both raw attempts
  remain available. Timeouts, quota/errors and uncertain spend are terminal transport
  evidence, not an automatic whole-eval replay.

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
fine; a fanout review is not. Raise the per-attempt cap with `TIMEOUT=` rather than
`MODE=deep`, which would also change reasoning depth.

Classify every failure before touching anything:

| Class | Signal | Action |
|---|---|---|
| Real regression | deterministic fail tied to a specific edit | fix the edit; re-run |
| Assertion regression | behavior intentionally changed; assertion now wrong | update the assertion; re-run |
| Invalid run — executor | `errors == 1` with a timeout/crash reason — the executor died and was graded on an empty answer | The delta is unmeasured. Inspect retained evidence; a new paid run requires the existing owner spend authority, never automatic replay. Adjust `TIMEOUT=` only with evidence of an inadequate cap. Does not consume the fix cap |
| Invalid run — judge | `errors == 1` with `reason: "judge returned no verdict"` — the answer was fine; the grade is the artifact | Inspect raw attempts and model/quota. One malformed-success semantic retry is already bounded; do not automatically replay a failed or uncertain paid turn. Same cap exemption |
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
