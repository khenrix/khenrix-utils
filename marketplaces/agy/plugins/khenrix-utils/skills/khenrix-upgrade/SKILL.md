---
name: khenrix-upgrade
description: Researches the latest Antigravity (agy) / Gemini version changes, models, experimental features and best practices, then reviews and improves how this setup uses agy — updating the khenrix-utils repo (skills, MCP, settings, house style) and writing a report of recommended live-config tuning. Use when the user wants to modernize, tune up, upgrade, or refresh their agy setup, adopt a newer/better Gemini model, try new features, or review and improve the khenrix skills/plugins. Does NOT change what the skills do — only how the CLI and models are used.
---

# khenrix-upgrade (Antigravity / agy)

Modernize how this machine uses agy. You research the latest changes and best
practices, review the khenrix skills/plugins, then improve the **khenrix-utils
repo** and write a report of recommended live-config tuning. The **purpose** of each
skill must not change — only *how* we use the CLI and models.

## Ground rules
- **Edit the repo, not the installed copy.** Locate the khenrix-utils repo
  (default `~/.../git/khenrix-utils` — the dir with `capabilities.toml` and `.git`).
  All edits + the report go there, then `make khenrix-refresh`.
- **Repo edits are applied with confirmation; live-config tuning is only
  recommended**, never auto-applied. agy's model is chosen at runtime, so model
  guidance is recommendation-only.
- **Preserve purpose.** Improve description/structure/instructions/model usage,
  never a skill's behavior.

## Steps

1. **Inventory.** Show the current snapshot (run from this skill's `scripts/` dir):
   ```bash
   python3 scripts/inventory.py --cli agy
   ```

2. **Research (deep).** Run a thorough, multi-source pass:
   - the latest agy changes via `agy changelog`,
   - the newest recommended Gemini model(s) and when to use which (Gemini docs),
   - experimental features / capabilities worth trying,
   - current best practices for getting better results with agy/Gemini.
   Capture concrete, dated findings with sources.

3. **Review the khenrix skills/plugins.** agy has no skill-creator; use its native
   validator for each plugin:
   ```bash
   agy plugin validate <plugin-dir>
   ```
   For skill *quality* (description/triggering/structure), apply the general Agent
   Skills best practices from research. Keep each skill's purpose intact.

4. **Synthesize into two buckets.**
   - **Repo edits** (apply with confirmation): SKILL.md improvements, new useful MCP
     servers / settings / house-style in `capabilities.toml` + `house-style.md`.
   - **Live-config recommendations** (report only): trusted workspaces, MCP, and
     any agy/Gemini usage guidance — with exact steps.

5. **Apply repo edits.** Show diffs, confirm, edit the repo, run
   `make khenrix-refresh` (for agy this re-installs the plugin). If
   `capabilities.toml` changed, remind the user to run the `khenrix-setup` skill.
   Offer to commit.

6. **Write the report** to `docs/upgrades/agy-<YYYY-MM-DD>.md` (today's date):
   findings per dimension, repo changes applied, deferred recommendations.

## Notes
- agy lacks bundled skill/plugin creation tooling, so this variant leans more on
  research + `agy plugin validate` than on a native skill reviewer.
