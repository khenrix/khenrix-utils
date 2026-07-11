---
name: khenrix-upgrade
description: Researches the latest Codex CLI changes, models, reasoning settings, experimental features and best practices, then reviews and improves how this machine uses Codex — updating the khenrix-utils repo (skill wording, MCP, settings, house style) and writing a report of recommended live-config tuning. Use when the user wants to modernize, tune up, upgrade, or refresh their Codex setup, pick a newer/better model or reasoning effort, try experimental features, or review/improve the khenrix skills. Does NOT change what the skills do — only how the CLI and models are used.
---

# khenrix-upgrade (Codex)

Modernize how this machine uses Codex. You research the latest changes and
best practices, review the khenrix skills with Codex's own tooling, then improve the
**khenrix-utils repo** and write a report of recommended live-config tuning. The
**purpose** of each skill must not change — only *how* we use the CLI and models to
get better results.

## Ground rules
- **Edit the repo, not the installed copy.** Locate the khenrix-utils repo
  (default `~/.../git/khenrix-utils` — the directory containing `capabilities.toml`
  and `.git`). All edits + the report go there. Then `make khenrix-refresh`.
- **Repo edits are applied with confirmation; live-config tuning is only
  recommended** (model, reasoning effort, experimental flags), never auto-applied.
- **Preserve purpose.** Improve descriptions/triggering, structure, instructions,
  model usage — never the behavior a skill is meant to deliver.
- **Never assert model or CLI facts from memory.** Names, tiers, pricing, and "the
  best current model" MUST come from this run's research or a live probe — even
  when you are confident you already know. Say how each fact was verified.
- **A model switch is two artifacts, never one action**: a repo-side guidance edit
  (applied with confirmation) plus the exact live-config command in the report
  (recommend-only — never run it yourself).
- **Every run ends with the dated report** at `docs/upgrades/codex-<YYYY-MM-DD>.md` —
  even a single-question run records its recommendation and commands there.

## Steps

1. **Inventory.** Show the current snapshot (Codex provides `$PLUGIN_ROOT` for the
   installed plugin):
   ```bash
   python3 "$PLUGIN_ROOT/skills/khenrix-upgrade/scripts/inventory.py" --cli codex
   ```

2. **Research (deep).** Run a thorough, multi-source pass:
   - the latest Codex version + changelog highlights (`codex --version`,
     developers.openai.com),
   - the newest recommended model + `model_reasoning_effort` — use the
     `openaiDeveloperDocs` MCP (`search_openai_docs` / `fetch_openai_doc`) and the
     `openai-docs` system skill's `scripts/resolve-latest-model-info.js`
     (`~/.codex/skills/.system/openai-docs/`),
   - experimental `[features.*]` flags and config keys worth trying,
   - current best practices for prompting/using Codex well.
   Capture concrete, dated findings with sources.

3. **Review the khenrix skills.** For each skill in the inventory, use Codex's
   native tooling under `~/.codex/skills/.system/`:
   - `skill-creator/scripts/quick_validate.py <skill-dir>` to validate frontmatter
     and structure, plus the skill-creator workflow for description/quality,
   - `plugin-creator/scripts/validate_plugin.py <plugin-dir>` for the plugin.
   - If either validator fails because Python cannot import `yaml`, run it from a
     temporary venv with `PyYAML` instead of installing packages globally.
   Collect concrete improvements; keep each skill's purpose intact.

4. **Synthesize into two buckets.**
   - **Repo edits** (apply with confirmation): SKILL.md wording/structure, new
     genuinely-useful MCP servers / settings / house-style in `capabilities.toml`
     and `house-style.md`.
   - **Live-config recommendations** (report only): `model`,
     `model_reasoning_effort`, `plan_mode_reasoning_effort`, `[features.*]` in
     `~/.codex/config.toml` — with exact `codex -c key=value` or edit commands.

5. **Apply repo edits.** Show each change as a diff, get approval, edit the repo,
   then run `make khenrix-refresh` from the **repo root** (the directory with the
   `Makefile` / `capabilities.toml`, not the installed plugin dir). If
   `capabilities.toml` changed, remind the user to run the `khenrix-setup` skill to apply it to the
   live config. Offer to commit.

6. **Write the report** to `docs/upgrades/codex-<YYYY-MM-DD>.md` in the repo
   (use today's date): findings per dimension, repo changes applied, and the
   deferred live-config recommendations with copy-paste commands.

## Notes
- Codex caches plugins by version, so `make khenrix-refresh` is required for the
  CLI to pick up skill edits.
- Prefer the bundled fallback `~/.codex/skills/.system/openai-docs/references/latest-model.md`
  if the model-discovery script can't reach the network.
