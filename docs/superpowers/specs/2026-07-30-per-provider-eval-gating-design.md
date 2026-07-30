# Per-provider eval measurement, gating, and cross-CLI portability checks — design

**Date:** 2026-07-30  **Status:** design (pre-plan)  **Repo:** khenrix-utils

## Goal

Make a skill's quality **attributable to the CLI it runs on**. Today the eval harness
runs the full claude/codex/agy panel but collapses it into one pooled mean, so a
regression on any single provider can be averaged away by gains on the others. This
design splits the measurement per provider, then — only once the measurement proves
trustworthy — gates on it, and adds cross-CLI structural checks that the current
`make verify` does not perform.

## Scope

**In:** `scripts/eval_harness.py` aggregation, printing, receipt schema and gate;
`scripts/lib/checks.py` receipt validation and a new portability module;
`scripts/eval_trigger.py` (revival); `shared/skills/skill-tuneup/` (SKILL.md +
`verify_final_receipt`); `docs/skill-eval-process.md`; `CLAUDE.md`.

**Out:** changing what `with_skill` *means* (running against the natively-installed
plugin instead of a pasted body) — see Rejected alternatives; judge-bias calibration;
a `skill_smoke.py` sentinel harness.

## Evidence base

Every number below was measured against this checkout on 2026-07-30, not estimated.

**The pooled mean hides per-provider regressions.** Replaying the ten stored
`benchmark.json` artifacts per executor, one skill's pooled pass masks a negative
provider:

```
khenrix-upgrade   pooled +0.0972  ->  claude -0.1250  codex +0.1250  agy +0.2917
```

**No codex-only or agy-only regression exists in the retained data.** Every codex and
agy delta across all ten skills is >= 0. The one negative on a delta-gated skill is on
claude. The motivating hypothesis — that skills are quietly worse on codex/agy — is
**not supported**; the gate's value is catching *any* provider's regression, whichever
provider it lands on.

**Run-to-run drift exceeds the proposed threshold.** Comparing `HEAD` receipts against
the working tree's re-earned ones, with zero body files changed and identical panel,
models, mode and judge:

| skill | delta @HEAD | delta @re-run | drift | body files changed |
|---|---|---|---|---|
| chunk-map | +0.1042 | +0.0417 | **-0.0625** | 0 |
| khenrix-upgrade | +0.1805 | +0.0972 | **-0.0833** | 0 |
| hookify | +0.25 | +0.25 | 0 | 0 |
| khenrix-setup | 0.0 | 0.0 | 0 | 0 |

**Splitting triples single-flip sensitivity.** `aggregate()` means over per-eval
`pass_rate`. One assertion flip moves the pooled mean by `1/(n_providers · n_evals)` and
a provider mean by `1/n_evals` — exactly 3x. `runs_per_configuration` is hardcoded to 1
(`eval_harness.py:492`) with no `--runs` flag. Per-provider quantum by eval-set size:

| skill | evals | quantum (one assertion flip) |
|---|---|---|
| khenrix-setup, khenrix-upgrade | 2 | ~0.146 |
| khenrix-wiki-add, khenrix-wiki-sync | 3 | ~0.102 |
| chunk-map | 4 | ~0.063 |
| skill-tuneup | 7 | ~0.034 |

Genuine skill effects run 0.32–0.55 per provider (markitdown, skill-tuneup) — an order
of magnitude above the noise floor. `khenrix-upgrade`'s -0.125 is **one assertion on one
of two evals**, on a skill whose body did not change.

**Conclusion:** a hard per-provider gate at threshold 0 would false-red on noise. The
measurement is sound and free; the gate needs a noise floor and a confirmation step.

## Constraints discovered (these shape the design)

1. **`scripts/render.py` is in every skill's source closure.** `checks.py:180` sets
   `GLOBAL_INPUTS = ["scripts/render.py"]`, folded in at `:210`. Editing `render.py`
   re-hashes all ten receipts simultaneously. Precedent: commit `a9fd6bb`, *"evals:
   re-seed receipts staled by the render.py wikisync-bundling change"*. Therefore **no
   phase of this work may edit `render.py`.** New checks hook into `checks.run_all()`,
   which `render.check()` already calls (`render.py:260-263`).
2. **`--seed-receipt` writes no per-eval data** (`eval_harness.py:456-465`). A schema
   that requires `per_provider` on every v2 receipt would make seeding structurally
   incapable of producing a valid receipt, destroying the only remedy for a mass-stale
   event.
3. **The default run is single-provider.** `--providers` defaults to `claude`;
   `docs/skill-eval-process.md:24` and `SKILL.md:283` both instruct claude-only
   iteration before a full-panel final gate. Any gate expression must be defined when a
   gated provider was not executed.
4. **`comparison.json` has no provider component.** It is written to
   `eval-<id>-<name>/comparison.json` while the six `<provider>__<condition>/` dirs sit
   beside it (`eval_harness.py:411-413`), so each provider overwrites the previous
   provider's blind verdict. Per-provider blind A/B is not reconstructible until this is
   fixed.
5. **The judge is a shared instrument.** `result.errors` is set when the executor *or*
   the judge failed (`eval_harness.py:405`), and the judge is always claude. Narrowing
   invalidity per provider without splitting these would blame agy for a claude-judge
   failure.
6. **Special gate kinds already exist.** `llm-council` gates on `fanout.py --self-test`
   and `DETERMINISTIC_GATED` skills on the wikisync unit suite (`eval_harness.py:525-538`);
   `khenrix-wiki-sync`'s receipt is legitimately earned with a negative advisory delta.
   A blanket "every non-waived provider non-negative" rule breaks both.
7. **`scripts/eval_trigger.py` already exists** — 170 lines implementing the
   description-triggering axis. Only its `--self-test` is wired in (`Makefile:144`);
   there are zero `triggers.json` files and no make target runs it. It is referenced in
   neither `docs/skill-eval-process.md` nor `SKILL.md`.

## Phase 1 — Per-provider measurement (no gate change)

The load-bearing, zero-risk half. Ships first and alone.

**Aggregation.** `aggregate(runs)` gains a `by_provider` block keyed on the `executor`
field every run record already carries (`eval_harness.py:397`):

```
by_provider: { <provider>: { with_skill: {...}, without_skill: {...},
                             delta: {...}, n_evals: N, quantum: q,
                             status: "ok" | "invalid" } }
```

The existing top-level `with_skill` / `without_skill` / `delta` blocks stay
**byte-compatible** so skill-creator interop and historical comparison are preserved.
Per codex's finding 10, the pooled block gains `valid: false` + `invalid_runs` when any
constituent run is invalid, so its number cannot be silently reused as a measurement.

**Error attribution.** Split `result.errors` into `executor_error` and `judge_error`.
An executor error marks only that provider `status: invalid`; a judge error invalidates
the run. `result.errors` is retained as the OR of the two for back-compatibility.

**Printing.** `_print_summary` prints one line per provider with its delta, quantum and
status, plus the pooled line. A provider whose delta is negative prints a visible marker
even in Phase 1, where it does not gate.

**Comparison artifact identity.** Write to `eval-<id>-<name>/comparison.<provider>.json`
and include `executor`, `eval_id`, `judge` and the resolved model in the JSON.
`blind_winner` tallies per provider. `docs/skill-eval-process.md`'s layout section is
updated to match. No migration needed — `evals/*/workspace/` is gitignored.

**Receipt.** Add `schema_version: 2` and a `per_provider` block mirroring `by_provider`'s
deltas. `checks.receipt_gate` grandfathers a receipt with **no** `schema_version` key
(today's ten stay green) and distinguishes an absent key from `"schema_version": null`.
**Seeded receipts are a legal v2 shape** (`provenance: "seeded: …"`, `per_provider`
absent) for `receipt_gate`; `verify_final_receipt` keeps rejecting them, which is
already the correct split (`tuneup.py:1336-1338`) and only needs writing down.

**The gate is unchanged in Phase 1.** It remains the pooled `delta >= 0`. Nothing can
false-red.

**Cleanup.** The harness docstring advertises `--grade-only` (`eval_harness.py:37`) which
`parse_args` never implements. Remove the line; do not implement the flag (its only
consumer was the cut calibration item).

## Phase 2 — The gate

Starts only once Phase 1 has produced `per_provider` data for **at least three** skills
through real `make eval` runs, so the guard band is tuned against observed numbers
rather than the historical replay alone.

**Prerequisite.** `khenrix-setup` and `khenrix-upgrade` have 2 eval cases each, giving a
quantum of ~0.146 — nearly the whole measurable range. Per-provider gating is
meaningless there until their eval sets grow to at least 4 cases. Growing them is part
of this phase, and each addition follows the discriminating-assertion rule in
`docs/skill-eval-process.md:19-23`.

**Noise floor.** `q_skill = 1 / (n_evals · min_i(total_i))` — the largest mean shift one
assertion flip can produce — floored at 0.05. `total_i` is already stored per run
(`result.total`).

**Gate expression.** For each provider `p` in `set(providers_run) ∩ gated`:

- `delta[p] >= -q_skill` → pass.
- `delta[p] < -q_skill` → **confirm**: re-run provider `p` across the **whole eval set**
  in both conditions (the delta cannot be recomputed from the regressing cases alone),
  then compare at assertion level. Block only when the same `(eval_id, assertion_index)`
  regressions reproduce **and** the re-run delta is still `< -q_skill`. Otherwise record
  `inconclusive` — which does not block, and is surfaced in the summary and receipt.
  The confirmation re-run is capped at one attempt per provider per run; a second
  negative that does not reproduce the same assertions stays `inconclusive`.

If `set(providers_run) ∩ gated` is empty — every executed provider is waived — no
per-provider gating applies to that run and it **cannot earn a final receipt**;
`verify_final_receipt` rejects it on the panel requirement.

Panel completeness is **not** a run-time condition — it is enforced at convergence by
`verify_final_receipt`, which already does exactly this (`tuneup.py:1339-1344`). This
resolves constraint 3: claude-only iteration runs keep working; a receipt claiming
finality must be full-panel.

**Gate kinds.** Receipts carry `gate_kind: assertion_delta | self_test | deterministic`,
written from Phase 2 onward. A v2 receipt from Phase 1 has no `gate_kind`; an absent key
means `assertion_delta`, so Phase 1 receipts stay valid without rewriting. Only
`assertion_delta` is subject to per-provider deltas; the other two carry their
deterministic evidence and are exempt, preserving constraint 6.

**Waivers.** Optional top-level block in `evals.json`:

```json
"gate": {
  "providers": ["claude", "codex"],
  "waived": { "agy": { "reason": "<non-empty>", "since": "YYYY-MM-DD" } }
}
```

Absent means all three gated. A waiver with an empty `reason` is refused. Gated and
waived sets must be disjoint, known, and collectively exhaustive with a non-empty gated
side. A waived provider **still executes** and records an advisory number; its summary
line prints `[waived: <reason>]`. `receipt_gate` warns (advisory only) once a waiver is
older than 180 days, so waivers cannot ossify silently. Because `eval_set_hash` hashes
`evals.json` wholesale (`checks.py:245-258`), any waiver edit re-arms that receipt
automatically.

**Single validator.** Receipt semantics currently live in two places — `receipt_gate`
(freshness) and `verify_final_receipt` (provenance + panel). Both call one new
`checks.validate_receipt(root, skill, *, final: bool)`; `final=True` adds the panel and
provenance requirements. No duplicated rules.

## Phase 3 — Portability checks and trigger revival

**Portability module.** New `scripts/lib/portability.py`, called from
`checks.run_all()` — never from `render.py` (constraint 1).

*Hermetic checks (fail hard):*
- Rendered-vs-source script-tree parity per CLI: every file under a shared skill's
  `scripts/` exists in all three rendered plugins. This is the defect actually worth
  catching — a bundled script that resolves on claude but was never copied to agy.
- Plugin-root-anchored entrypoint resolution: for each SKILL.md probe list of the
  `${CLAUDE_PLUGIN_ROOT}` / `${PLUGIN_ROOT}` / `$HOME/.gemini/...` shape, assert every
  named root resolves **and** that all three CLIs' roots are named.
- House-style rule: every `allowed-tools` Bash entry is a single command — no `&&`,
  `||` or `;`, because chaining defeats allow-list matching. Real, checkable, currently
  unenforced.

*External validators (skip visibly when the CLI is absent, fail when it rejects):*
- `agy plugin validate <rendered agy plugin>` — measured 0.5s.
- codex `quick_validate.py` per rendered skill **plus** the `.codex-plugin/plugin.json`
  manifest validator — measured 1.0s for all ten skills.

Both memoized per process and given an explicit subprocess timeout so a wedged CLI
cannot hang the gate. The skip must print in the same visual shape as the bats runner's
warning (`Makefile:104-107`: *"a non-zero SKIP count is a failure here"*) so it can never
read as a pass. `render.check()` is reached up to three times per `make precommit`
(`Makefile:74`, `:75`, `:79`); memoization makes the repeats free.

**Explicitly not checked:** `allowed-tools` tool *names*. `WebFetch` and `Skill` are
Claude-specific and codex/agy do not expose them under those names; there is no
cross-CLI contract to assert against. Document `allowed-tools` as Claude-only metadata
that no skill procedure may depend on.

**Explicitly dropped:** scanning SKILL.md prose for `scripts/*.py` and asserting each
resolves under the rendered skill dir. Implemented and run during review: 45 cited
paths, 9 reported missing, **9 false positives** — skills legitimately cite
`$KU/scripts/render.py`, llm-council's `fanout.py`, and codex's
`~/.codex/skills/.system/...` tooling.

**Trigger revival.** Do not build a new smoke harness. Instead:
1. Author `evals/<skill>/triggers.json` for two skills with known-adjacent descriptions
   (`khenrix-setup` vs `khenrix-upgrade` are the natural near-miss pair).
2. Add `make eval-trigger SKILL=<name>` running the existing `scripts/eval_trigger.py`.
3. Reference it from `docs/skill-eval-process.md` and skill-tuneup.
4. Then decide: if it earns its keep, extend coverage; if not, delete
   `eval_trigger.py` rather than leaving two dead trigger harnesses.

## Documentation

- `CLAUDE.md:48-60` states the pooled `run_summary.delta.pass_rate >= 0` gate as active
  repo instruction. Update it in the same change as Phase 2.
- `docs/skill-eval-process.md`: per-provider reporting, the guard band and confirmation
  step, the waiver contract, gate kinds, the corrected artifact layout, and which number
  is authoritative when pooled and per-provider disagree.
- `shared/skills/skill-tuneup/SKILL.md`: Step 8.3 and Step 10 gain "per-provider green,
  waivers reviewed"; the failure-handling table gains the `inconclusive` verdict.
- `references/eval-rules.md`: waiver-authoring rule.

## Testing

All hermetic, all inside `make eval-test`, no token cost.

`eval_harness.py --self-test` gains: per-provider aggregation with opposing deltas;
byte-compatibility of the pooled block; quantum computation across eval-set sizes; the
guard band; the confirmation path (mocked); executor-error narrowing vs judge-error run
invalidation; the gate on a partial provider set; each gate kind.

`checks.py --self-test` gains: v1 grandfathering; absent vs null `schema_version`;
v2 missing `per_provider` rejected for `final=True` but accepted when seeded; waiver
partition errors (overlap, unknown provider, empty reason, empty gated side); waiver
age warning.

New `portability.py --self-test`: tree-parity detection on a temp render tree; probe-list
resolution including an agy-root-missing case; the `allowed-tools` chaining rule;
skip-vs-fail behaviour when a validator binary is absent.

## Risks

- **R1. Defect #2 is untouched.** Even per-provider, codex and agy are graded on
  comprehension of a pasted body. A skill whose description never fires on codex can
  still post a perfect codex delta. Phase 3's trigger work is a partial mitigation, not
  a fix.
- **R2. The judge remains a single correlated point of failure** across all three
  providers' scores. Cutting calibration means this stays unmeasured by choice.
- **R3. Baseline contamination becomes asymmetric.** `without_skill` is the ambient
  environment; with the plugin installed, one provider's delta may measure new-vs-old
  while another's measures with-vs-without. Per-provider gating makes this consequential
  rather than averaged away, and nothing here detects it.
- **R4. The receipt does not attest harness semantics.** `eval_harness.py` is not in the
  source closure, so a future change to aggregation or judge prompts leaves existing
  receipts looking current. The original design (`docs/archive-adoption/design.md:91-99`)
  specified a `harness_sha` for exactly this. Deferred, not solved — adding it now would
  stale every receipt, the very trap constraint 1 describes.
- **R5. agy fails all-or-nothing** (all six agy runs invalid in
  `khenrix-wiki-sync`'s stored benchmark), so per-provider narrowing may in practice
  produce "agy invalid → waive agy" — the status quo with more ceremony.
- **R6. Equal weighting of case pass-rates** gives a 3-assertion case the same weight as
  a 7-assertion one. Changing to assertion weighting would itself change gate semantics;
  out of scope, noted so it is a decision rather than an accident.
- **R7. Rounding.** Deltas are rounded to 4 dp; store raw pass/total counts alongside so
  a tiny negative cannot become a signed zero at the gate boundary.

## Rejected alternatives

- **Hard per-provider gate at threshold 0 (the original proposal).** Rejected on the
  measured evidence: run-to-run drift of 0.06–0.08 on unchanged bodies, against a
  threshold of 0, with splitting tripling sensitivity. It would red-flag `khenrix-upgrade`
  on one assertion the day it shipped.
- **Hooking portability into `render.py --check`.** Rejected per constraint 1 — it
  stales all ten receipts and, combined with a strict v2 schema, leaves no seeding
  escape hatch.
- **`skill_smoke.py` with a body sentinel.** Rejected on three grounds: skill-tuneup
  refreshes installed plugins only *after* commit (`SKILL.md:428-435`), so a smoke "at
  convergence" tests the previous version; successfully using a skill does not imply
  quoting an arbitrary body string; and instructing the body to echo one pollutes every
  production skill with test behaviour.
- **Judge-bias calibration.** Rejected: without gold labels, re-grading the same answers
  with three judges measures agreement and leniency, not bias — answer quality, model,
  condition, assertion difficulty and judge strictness are confounded. The corpus is also
  gitignored and overwritten by the next `make eval` at the same `--iteration`.
- **Running `with_skill` against the natively-installed plugin** instead of a pasted
  body. This is the approach that would actually fix defect #2, proving triggering,
  script resolution and tool permissions in one measurement, and the plumbing exists
  (`make khenrix-refresh` installs to all three; `run_text` keeps the real HOME). Not
  rejected on merit — deferred because constructing a genuinely skill-free
  `without_skill` baseline against an installed plugin is an unsolved problem and a
  larger effort than this whole design. Recorded here as the successor project.
