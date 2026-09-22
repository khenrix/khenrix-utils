# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

khenrix-utils is the source of truth for Claude Code, Codex, Antigravity/`agy`,
and Maka. `components/skills/skillctl.py` directly copies 17 skills into their
native skill roots: the Khenrix-authored `khenrix-quality` and `khenrix-writing`
from `shared/skills/`, plus 15 vendored Superpowers skills from
`shared/superpowers/`. It also reconciles a bounded house-style block in all four
instruction files. This selective path does not use a marketplace. The repository
also owns shared MCP servers, portable settings, shell aliases, a status line, and
optional per-CLI plugin bundles for the broader reconcile flow. See `README.md` for
both paths.

## Edit the source of truth, never the generated copies

`render.py` regenerates these into every plugin — edits to them are silently overwritten:

- `marketplaces/<cli>/plugins/khenrix-utils/{capabilities.toml,house-style.md,statusline/}`
- any `marketplaces/.../skills/*/scripts/{reconcile.py,inventory.py}`
- `marketplaces/.../skills/<name>/` for plugin-delivered skills sourced from
  `shared/skills/`. The two Khenrix direct-copy skills and all 15 skills from
  `shared/superpowers/` are excluded.
- `marketplaces/.../skills/{khenrix-setup,khenrix-upgrade,khenrix-audit}/SKILL.md` — generated from
  `shared/skill-templates/<skill>/SKILL.md.tmpl` + the per-CLI `[skill_facts.*]` tables

Edit the originals instead:

- Capabilities (MCP servers, settings, aliases, instruction targets): `capabilities.toml`
- Shared base instructions: `house-style.md` — keep it provider-agnostic; CLI-specific
  guidance belongs in that CLI's own config, not here.
- Shared Khenrix/plugin skills: `shared/skills/<name>/SKILL.md`. The two
  Khenrix-authored direct-copy skills are `khenrix-quality` and `khenrix-writing`.
- Vendored Superpowers skills: `shared/superpowers/<name>/`. Their bodies are
  immutable reviewed upstream material; never edit one as a generic per-skill
  change. Review the central bundle provenance at
  `shared/superpowers/using-superpowers/upstreams.toml`, then update all 15 skills,
  their pin, notice, license, and declared privacy overlay atomically with
  `mise run skills:upstream-sync -- superpowers FULL_40_CHARACTER_COMMIT`.
- Selective skill delivery, restore, provenance checks, and Maka smoke:
  `components/skills/`; its allowlist and targets live under `[skill_delivery]`
  in `capabilities.toml`.
- Composed Khenrix-skill provenance: `shared/skills/<name>/upstreams.toml` plus that
  skill's notices and exact license copies. `upstreamctl.py record` keeps the pin,
  notice revision, and declared license bytes in one rollback-safe update.
- Reconcile/inventory engine: `scripts/lib/reconcile.py`, `scripts/lib/inventory.py`
- The per-CLI `khenrix-setup` / `khenrix-upgrade` / `khenrix-audit` skills are **generated** from a shared
  template + per-CLI facts: edit the shared prose in
  `shared/skill-templates/<skill>/SKILL.md.tmpl` and the provider-specific values
  (paths, commands, config terms, per-CLI procedure) in the `[skill_facts.<skill>.<cli>]`
  tables in `capabilities.toml`. `render.py` fills the template via `string.Template` and
  fails loudly on any missing `$token`. Never edit the rendered `marketplaces/.../SKILL.md`.

## After editing any source-of-truth file

Run `mise run verify` to validate manifests, skills, charts, and
`capabilities.toml`. For either Khenrix direct-copy skill or an atomic Superpowers
bundle update, also run `mise run skills:test`, `mise run
skills:upstream-status`, and `mise run skills:plan`.
Apply a reviewed plan with `mise run skills:apply -- --expect sha256:PLAN_ID`,
then run `mise run skills:doctor` and `mise run skills:maka-smoke`.

For optional plugin content, run `mise exec -- make khenrix-refresh`. Claude and
Codex cache plugins by version, so plain edits are not picked up until refresh.
`mise exec -- make status` diffs broader live CLI config read-only.

## Skill changes require evals (hard gate)

The vendored Superpowers bodies are the exception to generic per-skill authoring:
their immutable bundle is verified by provenance, delivery, and routing tests and
is updated only through the atomic bundle-sync command above. Do not tune, rewrite,
or evaluate one copied Superpowers skill as an independent local skill.

Any change to a Khenrix-authored skill (shared, templated, or its facts) MUST be eval-tested and
blind-reviewed before commit — for every provider, not just Claude. The full process is
`docs/skill-eval-process.md`; the loop runs through the portable harness
(`scripts/eval_harness.py`). Pre-commit ritual for a skill change:

```bash
mise run verify
mise exec -- make eval-test
mise exec -- make eval SKILL=<changed-skill> PROVIDERS=claude,codex,agy
```

Commit only when `run_summary.delta.pass_rate >= 0`. The blind A/B winner is recorded but
advisory (not a gate) — on a strong executor it rewards the tighter baseline over a
correct-but-thorough skill answer, so a non-negative-delta run isn't failed on a blind tie/loss.

The harness also records `run_summary.by_provider` — each executor's own delta plus the
noise `quantum` — and writes it into the receipt as `per_provider`. **That is a diagnostic,
not a gate:** the pooled delta remains the commit gate, because measured run-to-run drift on
unchanged bodies (0.06–0.08) exceeds a threshold of 0 and splitting triples the sensitivity.
Read the per-provider numbers when a delta is weak or surprising — a pooled pass can hide a
provider-sized regression.
`skill-creator` (Claude) and Codex's native creator are optional accelerators on top; the
harness is the baseline that also covers agy. `llm-council` is one exception — its
model/mode wiring is gated by `fanout.py --self-test` + `--smoke`, not the judge harness.
The `DETERMINISTIC_GATED` skills (`khenrix-wiki-add`, `khenrix-wiki-sync`, `llm-forge`) are
the other: their receipt is earned by a real test suite because a read-only harness cannot
exercise what they do. Their judge run still costs tokens and is recorded as advisory — the
receipt names which suite gated it (see the process doc).

`make eval` writes `evals/<skill>/receipt.json` (skill source-closure hash + eval-set hash)
on a passing run. **`make precommit`** is the commit-boundary gate: it checks render is in
sync AND that every eval'd skill's receipt matches its current source (a changed skill with a
stale/missing receipt fails). `make verify` only warns about stale receipts — run
`make precommit` before committing a skill change. The source closure includes the bundled
`scripts/lib/*` and `scripts/render.py`, so editing the reconcile engine correctly stales
every skill. Seed receipts for the current blessed state with `eval_harness.py --seed-receipt`.

`make precommit` also depends on **`make forge-test-slow`**, and that is load-bearing rather
than incidental. The forge suite is split by weight: `FORGE_TESTS` (the fast half) runs inside
`make verify`, while `FORGE_SLOW_TESTS` (the clone- and process-heavy half) deliberately does
not — `make verify` is this repository's own obvious confirmed verify command, so a forge run
would otherwise spawn clone fleets inside its own verifier clones. `precommit` is where those
nine suites land instead; nothing runs `precommit` inside a verifier. Adding a `tests/test_forge_*.py`
to neither variable is a suite nothing runs, and `test_forge_packaging.py` fails on it.

The provider harness covers Claude, Codex, and agy. Maka consumes the same
installed skill body, but it is not a fourth `make eval` provider. Run `mise run
skills:maka-smoke` after applying the skills. It verifies explicit and natural
loading from Maka's recorded events. Non-bootstrap natural cases first load the
root-session router and continue in the same bounded session, matching the shared house
rule without widening Maka's sandbox. The smoke also checks bounded ADHD activation,
action-first continuation, and opt-out acknowledgement. It does not replace behavior
evals or earn their receipts.

## Skill flowcharts

Every Khenrix-authored skill covered by the chart gate has a mermaid flowchart at
`docs/skill-charts/<skill>.md` — maintainer docs,
deliberately OUTSIDE the plugins and every receipt closure (a chart under
`shared/skills/` would stale that skill's receipt on every edit). Each decision diamond
(`G_*`) carries a Gate-evidence row: `code` gates cite a `path::label` reference that
`make verify` resolves by grep; `agent` gates are LLM-enforced process rules (a
checkpoint, a scope rule) that cite the eval assertion or audit item covering them — no
test can prove a model will stop, and claiming one would be the defect class this repo
keeps finding.

If a change alters a Khenrix-authored skill's flow, update its chart in the same
commit. A NEW Khenrix skill owes a chart in the same commit that adds it, exactly as it owes an eval set — the lint
(`scripts/lib/charts.py`, wired through `checks.run_all`) fails `make verify` on a
missing chart or a dangling evidence reference.

## Constraints

- Python is stdlib-only (no pip dependencies; `tomllib`, `json`, `subprocess`, …). Don't
  add third-party deps — it must run on any Python 3.11+ machine with no install step.
- SKILL.md frontmatter: `name` is lowercase letters/numbers/hyphens (≤64 chars),
  `description` ≤1024 chars, body <500 lines (enforced by `render.py --check`).
- Reconcile is non-destructive by design: it only adds missing entries or updates ones
  tagged `khenrix-managed`, and never removes machine-specific config. Preserve this invariant.
- Selective delivery owns only the 17 names declared in `[skill_delivery].skills`
  (two Khenrix skills from `shared/skills/` and 15 vendored Superpowers skills from
  `shared/superpowers/`), its private state directory, and the bounded house-style
  block. Preserve unrelated skills, parent-directory modes, and instruction text
  outside the markers. Refuse symlinked managed paths rather than following them.

## Etiquette

- Commit directly to `main` (solo repo); branch only when asked. No CI — `make verify` is the gate.
