---
name: khenrix-upgrade
description: Researches the latest Claude Code version changes, models, experimental features and best practices, then reviews and improves how this setup uses the CLI — updating the khenrix-utils repo (skills, MCP, settings, house style) and writing a report of recommended live-config tuning. Use when the user wants to modernize, tune up, upgrade, or refresh their Claude Code setup, pick a newer/better model, try new features, or review and improve the khenrix skills/plugins. Does NOT change what the skills do — only how the CLI and models are used.
allowed-tools: Bash, Read, Edit, WebSearch, WebFetch
---

# khenrix-upgrade (Claude Code)

Modernize how this machine uses Claude Code. You research the latest changes and
best practices, review the khenrix skills/plugins with Claude's own tooling, then
improve the **khenrix-utils repo** and write a report of recommended live-config
tuning. The **purpose** of each skill must not change — only *how* we use the CLI
and models to get better results.

## Ground rules
- **Edit the repo, not the installed copy.** Locate the khenrix-utils repo
  (default `~/.../git/khenrix-utils` — the directory containing `capabilities.toml`
  and `.git`). All edits + the report go there. Then `make khenrix-refresh`.
- **Repo edits are applied with confirmation; live-config tuning is only
  recommended** (written to the report with exact commands), never auto-applied.
- **Preserve purpose.** Improve descriptions/triggering, structure, instructions,
  model usage — never the behavior a skill is meant to deliver.

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
   then from the repo run `make khenrix-refresh`. If `capabilities.toml` changed,
   remind the user to run `/khenrix-setup` to apply it to the live config. Offer to
   commit.

6. **Write the report** to `docs/upgrades/claude-<YYYY-MM-DD>.md` in the repo
   (use today's date): findings per dimension, repo changes applied, and the
   deferred live-config recommendations with copy-paste commands.

## Notes
- Use the deep-research skill for step 2 unless the user asks for a quick check.
- For settings changes, the built-in **update-config** skill can apply
  `settings.json` edits — but per the design, surface those as recommendations.
