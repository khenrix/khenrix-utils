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

   This adds MCP servers via `claude mcp add --scope user`, ensures the
   managed alias block in `~/.bash_aliases`, ensures the house-style block in
   `~/.claude/CLAUDE.md`, and reports each action taken.

5. **Verify.** Run `claude mcp list` and confirm the newly added servers appear.
   Note that MCP changes take effect in a new session.

## Notes

- If the user explicitly wants drifted managed entries re-aligned to the source
  of truth (not just additions), add `--update-drift` to the apply command.
  Default behaviour leaves existing managed entries as-is to avoid clobbering
  local tweaks.
- Claude's approval/sandbox model differs from Codex's, so those baseline
  settings are reported as informational only and are not written here.
- To inspect the source of truth, read `capabilities.toml` at the plugin root.
