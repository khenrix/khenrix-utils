# Eval rules — running the gate and iterating to green

The repo's eval harness IS the benchmark (`docs/skill-eval-process.md` is the authority;
read it before scaffolding). Key invariants:

- Executors run **read-only by default** — mechanical on claude (plan mode, plan-file
  writes suppressed) and codex (read-only sandbox); best-effort on agy (its sandbox
  hangs headless; see `docs/skill-eval-process.md`) — safe to run mid-tuneup.
- **Baseline caveat**: `without_skill` is the executor's ambient env; if the old skill
  version is installed (a prior `make khenrix-refresh`), the comparison is new-vs-old,
  not with-vs-without. Iterate BEFORE refreshing for the cleanest signal.
- A run with `delta.pass_rate >= 0` writes `evals/<t>/receipt.json` — the exact
  artifact `make precommit` gates on. The blind winner being `with_skill` is the
  commit gate YOU verify (per the process doc); the harness does not enforce it.
- **llm-council is special**: its receipt is gated by `fanout.py --self-test` (+ a live
  `--smoke`), not the judge harness — executors run under `LLM_COUNCIL_DEPTH=1`, so the
  judged delta never exercises a real council and is advisory only.

## Scaffolding a missing eval set

If the target has no `evals/<t>/evals.json`: author 2-5 cases per the process doc —
`id`/`name`/`prompt`/optional `files`/`assertions`, plus a `notes` field explaining the
discriminating signal. Prefer inline-answer prompts ("Answer inline in prose — do NOT
enter plan mode or run tools") for decision-shaped skills; they are cheap, provider-safe,
and non-recursive. Checkpoint the proposed prompts with the user before running them.

## The loop

```bash
make eval SKILL=<t> PROVIDERS=claude     # iterate here (cheap)
make eval SKILL=<t> PROVIDERS=claude,codex,agy   # final gate (~3-4x tokens)
```

Classify every failure before touching anything:

| Class | Signal | Action |
|---|---|---|
| Real regression | deterministic fail tied to a specific edit | fix the edit; re-run |
| Assertion regression | behavior intentionally changed; assertion now wrong | update the assertion; re-run |
| Flaky / judge noise | same input passes sometimes | re-run ONCE; if it passes, accept and note it — do NOT edit the skill to chase a noisy grader |

## Cap

Hard cap: **5 iterations** (or the user's stated cap). On cap-reached-not-green: STOP,
record the unresolved failures (assertion + class + last result) in the run summary and
the run log, and hand the decision to the user. Never loop past the cap.

## Cost honesty

Say up front: any source change to the target re-arms its receipt, so even a one-line fix
costs an eval run before it can be committed. Fold that into the proportionality call at
the checkpoint — sometimes the right answer is to batch small fixes.
