---
name: khenrix-upgrade
description: Researches the latest Claude Code CLI changes, models, experimental features and best practices, then reviews and improves how this machine uses Claude Code — updating the khenrix-utils repo (skill wording, MCP, settings, house style) and writing a report of recommended live-config tuning. Use when the user wants to modernize, tune up, upgrade, or refresh their Claude Code setup, pick a newer/better model, try experimental features, or review/improve the khenrix skills. Does NOT change what the skills do — only how the CLI and models are used.
allowed-tools: Bash, Read, Edit, WebSearch, WebFetch
---

# khenrix-upgrade (Claude Code)

Modernize how this machine uses Claude Code. You research the latest changes and
best practices, review the khenrix skills/plugins with Claude's own tooling, then improve the
**khenrix-utils repo** and write a report of recommended live-config tuning. The
**purpose** of each skill must not change — only *how* we use the CLI and models to
get better results.

## Ground rules
- **Edit the repo, not the installed copy.** Locate the khenrix-utils repo
  (default `~/.../git/khenrix-utils` — the directory containing `capabilities.toml`
  and `.git`). All edits and the report go there. Validate there, then use the
  delivery path for the surface that changed; do not treat a plugin refresh as
  delivery for the direct-copy skills.
- **Repo edits are applied with confirmation; live-config tuning is only
  recommended** (written to the report with exact commands), never auto-applied.
- **Every proposed or applied repo edit names its eventual delivery path by changed
  surface**, including read-only runs where nothing can be applied. A proposal's gate
  block must not stop at verify, eval, or precommit: state whether approval would be
  followed by selective `skills:plan` / `skills:apply` / doctor / Maka smoke, optional
  `make khenrix-refresh`, or both.
- **Preserve purpose.** Improve descriptions/triggering, structure, instructions,
  model usage — never the behavior a skill is meant to deliver.
- **Never assert model or CLI facts from memory.** Names, tiers, pricing, and "the
  best current model" MUST come from this run's research or a live probe — even
  when you are confident you already know. Say how each fact was verified.
- **A model switch is two artifacts, never one action**: a repo-side guidance edit
  (applied with confirmation) plus the exact live-config command in the report
  (recommend-only — never run it yourself).
- **A model id is not free text — `[models]` in `capabilities.toml` is an ALLOW-LIST
  and `make verify` fails on anything outside it.** `checks.py`'s model-crosscheck
  refuses a model that any engine names but the manifest does not, so introducing
  one means registering it there in the same change, not just editing prose.
- **Registering it is still not enough: price it, or the cost tooling reports $0.**
  Every registered Claude ID needs an exact `scripts/pricing.toml` row.
  Session pricing accepts only exact IDs and date-suffixed variants, so a new
  minor version cannot inherit an older version's rate. `pricing-coverage`
  catches missing rows at `make verify`; bump `last_reviewed` in both files.
- **Every run ends with the dated report** at `docs/upgrades/claude-<YYYY-MM-DD>.md` —
  even a single-question run records its recommendation and commands there.

## Steps

1. **Inventory.** Show the current snapshot:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/skills/khenrix-upgrade/scripts/inventory.py" --cli claude
   ```

2. **Research (deep).** Run a thorough, multi-source, citation-checked pass using
   the **deep-research** skill (invoke it via the Skill tool), plus `WebSearch` /
   `WebFetch` on:
   - the latest Claude Code version + notable `CHANGELOG.md` entries since the
     installed version (`anthropics/claude-code`),
   - the newest recommended Claude model(s) and when to use which,
   - experimental features / flags worth trying,
   - current best practices from `code.claude.com/docs` and the community.
   Capture concrete, dated findings with sources.

3. **Review the khenrix skills.** For each skill listed in the inventory, use
   Claude's native tooling:
   - the **skill-creator** skill (invoke via the Skill tool) to assess and improve
     a skill's description/structure/triggering,
   - the **plugin-dev** `skill-reviewer` agent (launch via the Agent/Task tool) for
     a quality review, and `plugin-validator` for the plugin manifest.
   Collect concrete improvement suggestions. Keep each skill's purpose intact.

4. **Synthesize into two buckets.**
   - **Repo edits** (apply with confirmation): SKILL.md wording/structure, new
     genuinely-useful MCP servers / settings / house-style in `capabilities.toml`
     and `house-style.md`.
   - **Live-config recommendations** (report only): model choice (`/model` or
     `settings.json` `model`), and any new settings — with exact commands.

5. **Apply repo edits.** Show each change as a diff, get approval, edit the repo,
   and run `mise run verify` from the **repo root** (the directory with the
   `Makefile` / `capabilities.toml`, not an installed copy). Then
   choose delivery by changed surface:
   - If `khenrix-quality`, `khenrix-writing`, their provenance, selective-delivery
     settings, or `house-style.md` changed, run `mise run skills:test` and `mise
     run skills:upstream-status`. Show `mise run skills:plan`, then apply that exact
     content-addressed plan with `mise run skills:apply -- --expect <plan-id>`.
     Finish with `mise run skills:doctor` and `mise run skills:maka-smoke`.
   - If any optional plugin, broader reconcile capability, or other rendered skill
     changed, run `mise exec -- make khenrix-refresh`. If broader
     `capabilities.toml` entries changed, remind the user to run `/khenrix-setup` to
     review and apply them.
   A change can require both paths. Record the commands and results in the report,
   then offer to commit.

6. **Write the report** to `docs/upgrades/claude-<YYYY-MM-DD>.md` in the repo
   (use today's date): findings per dimension, repo changes applied, and the
   deferred live-config recommendations with copy-paste commands.

## Notes
- Use the deep-research skill for step 2 unless the user asks for a quick check.
- For settings changes, the built-in **update-config** skill can apply
  `settings.json` edits — but per the design, surface those as recommendations.
