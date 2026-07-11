---
name: khenrix-setup
description: Reconciles this Claude Code installation with the shared khenrix source of truth — reviews the live MCP servers, settings, shell aliases and base instructions, shows what differs, and additively adds only what is missing without removing anything machine-specific. Use when the user wants to set up, sync, audit, or update their Claude Code environment to match the khenrix-utils capabilities, or asks to install the shared MCP servers / house style.
allowed-tools: Bash, Read
---

# khenrix-setup (Claude Code)

Reconciles Claude Code with the shared khenrix capabilities defined in
`capabilities.toml` (bundled at the plugin root). The heavy lifting is done by a
deterministic engine — `scripts/reconcile.py` — so the merge logic is reliable
and **non-destructive**. Your job is to run it, present the review, and apply
only after the user confirms.

## What "reconcile" means here

- **Additive only.** Missing declared entries are added. Entries the user added
  outside khenrix (machine-specific MCP servers, hand-tuned settings, aliases) are
  reported as `EXTRA` and **never removed**.
- **Review before write.** The default run is read-only. Nothing is written
  until the user approves an `--apply` run.
- **Backups.** Every file the engine modifies is copied to `*.khenrix-backup`
  first.

## Non-negotiables

- **The read-only review always happens — even when the user waives it.** "Just
  apply it, I trust it" waives the confirmation pause, not the review: the review
  run is instant and free, so run it anyway and show the ➕ ADD list before (or
  alongside) the apply. If this session cannot execute commands, state the exact
  read-only command and what its report will show — never endorse a blind apply.
- **State the contract out loud in your answer**: additive and non-destructive —
  missing entries are added, EXTRA (machine-specific) entries are never removed,
  and every touched file gets a `*.khenrix-backup`.
- **Name what will change.** Before any apply, enumerate what the review found to
  add — a bare "applied it" with no list is never acceptable.

## Steps

1. **Review (read-only).** Run the engine and show the user its full output:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/skills/khenrix-setup/scripts/reconcile.py" --cli claude
   ```

   The report lists each MCP server, setting, and the base-instructions file as
   one of: ✅ MATCH, ➕ ADD (missing), ✏️ UPDATE (drifted), ⏭️ EXTRA (unmanaged,
   left untouched).

2. **Summarize** the diff for the user in plain language: what will be added,
   what is already in sync, and confirm that EXTRA entries will be left alone.
   Call out anything notable (e.g. a Windows-only server skipped on this host).

3. **Confirm.** Ask the user to approve applying the additions. Do not proceed
   without an explicit yes.

4. **Apply.** On approval, run:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/skills/khenrix-setup/scripts/reconcile.py" --cli claude --apply
   ```

   This adds MCP servers via `claude mcp add --scope user`; writes any MISSING
   baseline settings (model, effort, tui, theme, voice, skip-prompts) to
   `~/.claude/settings.json`; installs + registers the Stop hook and the statusline renderer;
   ensures the managed alias block in `~/.bash_aliases` and the house-style block in
   `~/.claude/CLAUDE.md`; and reports each action taken.

5. **Verify.** Run `claude mcp list` and confirm the newly added servers appear.
   Note that MCP changes take effect in a new session.

## Notes

- If the user explicitly wants drifted managed entries re-aligned to the source
  of truth (not just additions), add `--update-drift` to the apply command.
  Default behaviour leaves existing managed entries as-is to avoid clobbering
  local tweaks.
- Baseline settings (model, effort, tui, theme, voice, skip-prompts) and the Stop
  hook are applied ONLY when absent — your existing values are kept, never overridden. Only
  approval/sandbox stay informational (Claude uses permissions/--permission-mode, not static keys).
- To inspect the source of truth, read `capabilities.toml` at the plugin root.
