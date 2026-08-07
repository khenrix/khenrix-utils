# Skill-eval process (provider-agnostic)

**Every change to a skill in this repo must be eval-tested and blind-reviewed before it
is committed.** This is a hard gate, not a suggestion. The point is the same rigor the
`llm-council` work proved out — with-skill vs baseline, judged against assertions, then a
blind A/B — applied to *every* skill and *every* provider, not just Claude.

This repo ships a portable, stdlib-only harness (`scripts/eval_harness.py`) that delivers
that loop for all three CLIs. Claude's `skill-creator` and Codex's native skill tooling
are optional accelerators on top (see below) — the harness is the baseline that also
covers agy, which has no native skill tooling at all.

## The loop

1. **Edit the skill.** For shared skills, edit `shared/skills/<name>/SKILL.md`. For the
   templated per-CLI skills, edit `shared/skill-templates/<skill>/SKILL.md.tmpl` (shared
   prose) and/or the `[skill_facts.<skill>.<cli>]` facts in `capabilities.toml`. Then
   `python3 scripts/render.py` so the rendered bodies the harness runs are current.
2. **Write/refresh evals.** `evals/<skill>/evals.json` — 2-7 cases (prefer 2-5; go higher only for a genuinely
   separate contract, never to pad), each with `prompt`,
   optional `files`, and `assertions`. Make assertions **discriminating**: things a
   no-skill baseline would structurally fail (that gap is the skill's measured value).
   Objective and answer-only — "runs the engine read-only first", not "is well written".
3. **Run with-skill vs baseline, per provider.** `make eval SKILL=<name>` (defaults to
   the `claude` executor; add `PROVIDERS=claude,codex,agy` for the full panel). The
   harness injects the rendered SKILL.md for the with_skill run and uses the bare prompt
   for the baseline.

   **Baseline caveat:** `without_skill` is the executor's *ambient* environment — truly
   skill-free only if the skill isn't already installed on that CLI. If it is installed
   (a prior `make khenrix-refresh`), it can auto-trigger and the baseline becomes the
   *old installed version*, so the comparison is new-body-vs-old, not with-vs-without.
   For the cleanest signal, iterate with the harness BEFORE refreshing/installing the
   change. The blind A/B and delta stay meaningful either way.
4. **Grade.** An LLM judge scores each output against the assertions → `grading.json`
   (`text`/`passed`/`evidence`).
5. **Blind A/B.** The two outputs are shuffled into A/B (with a hidden key) and the judge
   picks the better one blind → `comparison.json`, then de-anonymized.
6. **Iterate** until with_skill matches or beats baseline on the discriminating assertions
   (`run_summary.delta.pass_rate >= 0` — the commit gate; zero passes, negative fails).
   The blind A/B winner is **recorded but advisory** (not a gate): on a strong executor it
   rewards the tighter baseline over a correct-but-more-thorough skill answer, so a
   non-negative-delta run is never failed on a blind tie/loss. Use the recorded winner to
   triage a weak/zero delta.
7. **Only then** `make verify && make eval-test && make eval SKILL=<name>` → commit.

## Layout

```
evals/<skill>/evals.json          # committed: id / name / prompt / files / assertions
evals/<skill>/workspace/          # gitignored (evals/*/workspace/)
  iteration-N/
    eval-<id>-<name>/
      <provider>__with_skill/     {prompt.txt, answer.md, grading.json, <fanout artifacts>}
      <provider>__without_skill/  {prompt.txt, answer.md, grading.json, …}
      compare-<provider>/         # judge artifacts for that provider's blind A/B
      comparison.<provider>.json  # blind A/B verdict, de-anonymized, PER EXECUTOR
    benchmark.json                # metadata + runs[] + run_summary{…,by_provider} + blind_winner
```

The comparison file is per-provider because this directory is shared by all three: a
single `comparison.json` had each provider silently overwrite the previous one's verdict,
so a per-provider blind tally was not reconstructible from the artifacts at all.

The artifact schema matches `skill-creator`'s (`grading.json` / `benchmark.json` /
`comparison.json`), so the two interoperate — you can grade with one and aggregate with
the other.

## Commands

```bash
make eval-test                              # hermetic harness logic tests (no tokens)
make eval SKILL=khenrix-setup               # claude executor, normal mode
make eval SKILL=khenrix-setup PROVIDERS=claude,codex,agy MODE=deep
```

Notes: executors run **read-only / plan-only** by default (`make_readonly` swaps each
provider's bypass flag — claude `--permission-mode plan` plus plan-file suppression
(`--disallowedTools ExitPlanMode` + an appended system prompt), codex `--sandbox read-only`;
agy `--mode plan` — a mechanical read-only mode since agy 1.1.1, since its `--sandbox` hangs
headless (see `make_readonly`'s docstring) — plus two SOFT layers as defense in depth: a
READONLY_POSTURE line prepended to every executor's prompt and a throwaway git-worktree cwd,
so cwd-relative writes are discarded —
so a skill that mutates config (`khenrix-setup`/`khenrix-upgrade`) is
mechanically constrained on all three during an eval, while the real HOME is kept so auth still resolves
(sandboxing HOME instead hid credentials and every run failed `auth_or_quota`). Full
three-provider runs are token-expensive (~3-4×); use the single-provider `claude` loop for
iteration and the full panel for the final gate. `--no-readonly` opts out when a skill
genuinely must write. agy's plan mode is a mechanical write barrier but not an OS sandbox —
still less sealed than codex's, so lower-risk rather than sealed.

**Invalid runs.** Each entry in `benchmark.json`'s `runs[]` carries `result.errors` (1 when
the executor timed out or died, **or the judge returned no verdict** — `result.reason`
distinguishes them) and `result.reason`. An invalid run is graded 0/N — on an empty answer
when the executor died, on a missing verdict when the judge did — and averaged into its own
side's mean, so it BIASES the delta — a `with_skill` error
sinks it, a `without_skill` error inflates it and would otherwise earn a receipt off a
baseline that never answered. The gate therefore fails closed whenever any run is invalid,
for every skill whose gate IS the delta; `llm-council` and every `DETERMINISTIC_GATED` skill
are exempt because their receipts are earned by a self-test / unit suite instead. The
run summary prints a `⚠ INVALID RUN` line per occurrence. If one recurs with nothing else in
flight, the eval is under-timed — raise the per-attempt cap with `make eval … TIMEOUT=<secs>`
rather than `MODE=deep`, which would also change reasoning depth.

## Per-provider measurement

`run_summary.by_provider` reports each executor's own `with_skill` / `without_skill` /
`delta`, plus `n_evals`, `quantum` and `status`; the receipt carries the same as
`per_provider`. Pooling across executors hid real regressions — `khenrix-upgrade` pooled
to `+0.0972` while claude sat at `-0.1250`.

**The pooled `delta.pass_rate` is still the gate.** The per-provider numbers are a
measurement, not yet a gate. Why: measured run-to-run drift on *unchanged* skill bodies is
0.06–0.08, and splitting by provider triples single-flip sensitivity, so a gate at
threshold 0 would fire on noise. Demonstrated live — the same unchanged `khenrix-upgrade`
gave `claude -0.125 / codex +0.125 / agy +0.292` one week and
`claude +0.25 / codex -0.042 / agy -0.083` the next. **When pooled and per-provider
disagree, the pooled number is authoritative for the gate and the per-provider numbers are
authoritative for diagnosis.**

`quantum` is the noise floor: `1 / (n_evals × smallest assertion count)`, floored at 0.05 —
the largest mean shift one assertion flip can produce. A per-provider delta smaller than
one quantum is a judge verdict, not a signal, and the summary annotates it as such. Eval
sets with 2 cases have a quantum near 0.15, nearly their whole measurable range; grow them
before reading their per-provider numbers as anything.

`status` is `invalid` when that **executor** failed. A **judge** failure invalidates the
whole run instead — the judge is a shared instrument (always `DEFAULT_JUDGE`), so blaming
one executor for it would be wrong. `runs[].result` carries `executor_error` and
`judge_error` separately for exactly this reason; `errors` remains the OR of the two.

## Trigger and routing evals (the other axis)

`eval_harness.py` injects a skill body and grades the output — it assumes the skill
already triggered. `scripts/eval_trigger.py` covers the complementary axis: given only a
skill's name and description, would the agent pick it?

```bash
make eval-trigger SKILL=khenrix-setup          # fires / abstains, needs >= 0.8
make eval-arena   SKILLS=khenrix-audit,khenrix-setup,skill-tuneup   # cross-skill routing
```

`eval-trigger` reads `evals/<skill>/triggers.json`
(`{"should_trigger": [...], "near_miss": [...]}`) and scores correct fires plus correct
abstains. Near-misses should be prompts belonging to an *adjacent* skill:
`khenrix-setup` and `khenrix-upgrade` are deliberately **mirrors** — each one's
`should_trigger` cases are the other's `near_miss` set — so a description edit that blurs
the boundary fails on both sides rather than silently passing one.

`eval-arena` reads `evals/<first-skill>/arena.json` and asks which skill on a roster wins
each prompt. Neither is a receipt gate; run them when editing a skill's `description`,
which the behaviour harness cannot evaluate at all.

**Two live findings recorded 2026-08-07, both for a future tuneup rather than a quick fix:**

- `khenrix-upgrade` scores 0.909 (passes) but misses `"which model should this machine
  default to now?"` — a *question-shaped* prompt inside its stated "pick a newer/better
  model" territory. Its description may under-cover interrogative phrasing.
- The arena scores 0.667 on the committed roster because the judge answers
  `off-roster:khenrix-upgrade` and `off-roster:claude-obsidian:wiki-lint` where
  `arena.json` expects `none`. That is `parse_arena_verdict` behaving exactly as its
  docstring specifies (an off-roster name is a readable answer *and* a routing failure),
  and the judge is arguably being *more* informative than the contract permits — but it
  makes the committed arena set unpassable as authored. Either widen the roster or
  re-express those cases; do not "fix" it by rounding off-roster back to `none`, which is
  the exact collapse that docstring exists to prevent.

## Per-provider tooling (accelerators, not the gate)

- **Claude** — `skill-creator` (installed via `claude-plugins-official`) has the richest
  loop: grader/comparator/analyzer subagents, an eval viewer, benchmark variance, and
  trigger-description optimization. Prefer it when authoring on Claude. We **reference** it;
  we do not vendor it. Its artifact schema is the one this harness emits.
- **Codex** — `~/.codex/skills/.system/skill-creator/` scaffolds + validates
  (`quick_validate.py`), but has **no** structured evals/blind-review. Use it for
  scaffolding/validation; use this harness for the eval loop.
- **agy** — no native skill tooling. The harness is the only eval path; `agy plugin
  validate` covers manifest validation.

## Orchestrator skills (llm-council)

`llm-council` is special: harness executors run under `LLM_COUNCIL_DEPTH=1`, so an
injected body cannot convene a real nested council — the with-skill/baseline benchmark
still runs but its delta measures solo answers and is **advisory only**, never the
receipt gate. What earns the receipt is the model/mode wiring verified
**deterministically** by `python3 shared/skills/llm-council/scripts/fanout.py
--self-test` and a live `--smoke` (inspect the manifest's `model`/`thinking` and
`[mode: …]`). Its synthesis quality has a bespoke blind-review workspace under
`evals/llm-council/` (authored with skill-creator).

## Deterministically-gated skills (`DETERMINISTIC_GATED`)

Some skills the judge harness cannot fairly gate route their receipt through a real test
suite instead. `eval_harness.DETERMINISTIC_GATED` maps the skill to the command, and
`DETERMINISTIC_GATE_NAMES` maps it to the NAME the receipt records — two tables rather than
one, because a single hardcoded gate name became a false provenance string the moment a
third skill was routed through the dict, on the one artifact whose whole job is to say what
ran. A skill in the first table and not the second raises a `KeyError` rather than writing a
receipt that cannot name its gate; `make eval-test` catches that before a paid run does.

| Skill | Why the delta cannot gate it | Gate |
|---|---|---|
| `khenrix-wiki-add` / `khenrix-wiki-sync` | the read-only baseline can read the in-repo skill source and engine, so "skill-free" is contaminated | the wikisync unit suite |
| `llm-forge` | a read-only with-skill/baseline harness cannot drive a clone fleet, three providers and a fresh verifier — a judge receipt would certify prose and leave the dangerous mechanics untouched | the hermetic forge handover/CLI/`--gc` suites |

**The judge run still executes.** `gate_ok = True` is applied *after* it in `run()`, so
routing makes the delta **advisory, not free** — the cost control is a cheap eval set. Read
the recorded delta when triaging; do not treat it as the evidence in the receipt. The
evidence is the suite named in `deterministic_gate`, which `_write_receipt` runs and refuses
to write on.

## Maintenance runs (skill-tuneup)

The `skill-tuneup` skill automates this loop for periodic maintenance of an existing
skill: it researches upstream drift since the target's last substantive commit, audits,
applies user-approved fixes, scaffolds a missing eval set per this doc, and iterates
`make eval` to a fresh receipt before committing. Its per-target decisions live in
`docs/tuneups/log/`.
