---
name: khenrix-setup
description: Reconciles this Antigravity (agy) installation with the shared khenrix source of truth — reviews the live MCP servers in ~/.gemini/config/mcp_config.json, the trusted workspaces, shell aliases, and GEMINI.md, shows what differs, and additively adds only what is missing without removing anything machine-specific. Use when the user wants to set up, sync, audit, or update their agy environment to match the khenrix-utils capabilities, or asks to install the shared MCP servers / house style.
---

# khenrix-setup (Antigravity / agy)

Reconciles Antigravity / agy with the shared khenrix capabilities defined in
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
   python3 "$HOME/.gemini/config/plugins/khenrix-utils/skills/khenrix-setup/scripts/reconcile.py" --cli agy
   ```

   The report lists each MCP server, setting, and the base-instructions file as
   one of: ✅ MATCH, ➕ ADD (missing), ✏️ UPDATE (drifted), ⏭️ EXTRA (unmanaged,
   left untouched).

2. **Summarize** the diff: which MCP servers will be added to
   `mcp_config.json`, which trusted workspaces are added to
   `~/.gemini/antigravity-cli/settings.json`, and that EXTRA entries are left
   untouched. Note that http servers are written with `httpUrl` and stdio
   servers with `command`/`args`/`env`.

3. **Confirm.** Ask the user to approve applying the additions. Do not proceed
   without an explicit yes.

4. **Apply.** On approval, run:

   ```bash
   python3 "$HOME/.gemini/config/plugins/khenrix-utils/skills/khenrix-setup/scripts/reconcile.py" --cli agy --apply
   ```

   This merges MCP servers into `mcp_config.json`, adds trusted workspaces,
   ensures the managed alias block in `~/.bash_aliases`, and ensures the house-style
   block in `~/.gemini/GEMINI.md`.

5. **Verify.** Show `~/.gemini/config/mcp_config.json` so the user can confirm.
   Restart agy to pick up new servers.

## Notes

- If the user explicitly wants drifted managed entries re-aligned to the source
  of truth (not just additions), add `--update-drift` to the apply command.
  Default behaviour leaves existing managed entries as-is to avoid clobbering
  local tweaks.
- agy prompts per-action for approvals, so there is no static approval/sandbox
  key to reconcile (reported as informational).
- To inspect the source of truth, read `capabilities.toml` at the plugin root.
