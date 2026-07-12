# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

khenrix-utils is the single source of truth for the agentic CLIs on this machine
(Claude Code, Codex, Antigravity/`agy`): shared MCP servers, baseline settings,
shell aliases, base instructions, a status line, and the reconcile skills.
`scripts/render.py` bundles the source of truth into a self-contained plugin per
CLI under `marketplaces/<cli>/plugins/khenrix-utils/`; the `khenrix-setup` skill
then reconciles each CLI's live config additively. See `README.md` for the full flow.

## Edit the source of truth, never the generated copies

`render.py` regenerates these into every plugin — edits to them are silently overwritten:

- `marketplaces/<cli>/plugins/khenrix-utils/{capabilities.toml,house-style.md,statusline/}`
- any `marketplaces/.../skills/*/scripts/{reconcile.py,inventory.py}`
- `marketplaces/.../skills/<name>/` for skills sourced from `shared/skills/`
- `marketplaces/.../skills/{khenrix-setup,khenrix-upgrade}/SKILL.md` — generated from
  `shared/skill-templates/<skill>/SKILL.md.tmpl` + the per-CLI `[skill_facts.*]` tables

Edit the originals instead:

- Capabilities (MCP servers, settings, aliases, instruction targets): `capabilities.toml`
- Shared base instructions: `house-style.md` — keep it provider-agnostic; CLI-specific
  guidance belongs in that CLI's own config, not here.
- Shared skills: `shared/skills/<name>/SKILL.md`
- Reconcile/inventory engine: `scripts/lib/reconcile.py`, `scripts/lib/inventory.py`
- The per-CLI `khenrix-setup` / `khenrix-upgrade` skills are **generated** from a shared
  template + per-CLI facts: edit the shared prose in
  `shared/skill-templates/<skill>/SKILL.md.tmpl` and the provider-specific values
  (paths, commands, config terms, per-CLI procedure) in the `[skill_facts.<skill>.<cli>]`
  tables in `capabilities.toml`. `render.py` fills the template via `string.Template` and
  fails loudly on any missing `$token`. Never edit the rendered `marketplaces/.../SKILL.md`.

## After editing any source-of-truth file

Run `make verify` (validates manifests + skills, and that `capabilities.toml` parses),
then `make khenrix-refresh` (re-renders and pushes the plugin into every installed CLI —
required because Claude/Codex cache plugins by version, so plain edits aren't picked up
otherwise). `make status` diffs each CLI's live config against the source of truth (read-only).

## Skill changes require evals (hard gate)

Any change to a skill (shared, templated, or its facts) MUST be eval-tested and
blind-reviewed before commit — for every provider, not just Claude. The full process is
`docs/skill-eval-process.md`; the loop runs through the portable harness
(`scripts/eval_harness.py`). Pre-commit ritual for a skill change:

```
make verify          # render + validate
make eval-test       # hermetic harness logic tests (no token cost)
make eval SKILL=<changed-skill>   # with-skill vs baseline + LLM-judge + blind A/B
```

Commit only when `run_summary.delta.pass_rate >= 0`. The blind A/B winner is recorded but
advisory (not a gate) — on a strong executor it rewards the tighter baseline over a
correct-but-thorough skill answer, so a non-negative-delta run isn't failed on a blind tie/loss.
`skill-creator` (Claude) and Codex's native creator are optional accelerators on top; the
harness is the baseline that also covers agy. `llm-council` is the exception — its
model/mode wiring is gated by `fanout.py --self-test` + `--smoke`, not the judge harness
(see the process doc).

`make eval` writes `evals/<skill>/receipt.json` (skill source-closure hash + eval-set hash)
on a passing run. **`make precommit`** is the commit-boundary gate: it checks render is in
sync AND that every eval'd skill's receipt matches its current source (a changed skill with a
stale/missing receipt fails). `make verify` only warns about stale receipts — run
`make precommit` before committing a skill change. The source closure includes the bundled
`scripts/lib/*` and `scripts/render.py`, so editing the reconcile engine correctly stales
every skill. Seed receipts for the current blessed state with `eval_harness.py --seed-receipt`.

## Constraints

- Python is stdlib-only (no pip dependencies; `tomllib`, `json`, `subprocess`, …). Don't
  add third-party deps — it must run on any Python 3.11+ machine with no install step.
- SKILL.md frontmatter: `name` is lowercase letters/numbers/hyphens (≤64 chars),
  `description` ≤1024 chars, body <500 lines (enforced by `render.py --check`).
- Reconcile is non-destructive by design: it only adds missing entries or updates ones
  tagged `khenrix-managed`, and never removes machine-specific config. Preserve this invariant.

## Etiquette

- Commit directly to `main` (solo repo); branch only when asked. No CI — `make verify` is the gate.
