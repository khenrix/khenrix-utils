---
name: khenrix-setup
description: Reconciles this Codex installation with the shared khenrix source of truth — reviews the live MCP servers in config.toml, the approval/sandbox/trust settings, shell aliases, and AGENTS.md, shows what differs, and additively adds only what is missing without removing anything machine-specific. Use when the user wants to set up, sync, audit, or update their Codex environment to match the khenrix-utils capabilities, or asks to install the shared MCP servers / house style.
---

# khenrix-setup (Codex)

Reconciles Codex with the shared khenrix capabilities defined in
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
   python3 "$PLUGIN_ROOT/skills/khenrix-setup/scripts/reconcile.py" --cli codex
   ```

   The report lists each MCP server, setting, and the base-instructions file as
   one of: ✅ MATCH, ➕ ADD (missing), ✏️ UPDATE (drifted), ⏭️ EXTRA (unmanaged,
   left untouched).

2. **Summarize** the diff: which `[mcp_servers.*]` tables will be appended,
   which settings/trust entries are added, and confirm EXTRA entries (e.g. a
   Windows-only `chrome-devtools` on a non-Windows host) are left untouched.

3. **Confirm.** Ask the user to approve applying the additions. Do not proceed
   without an explicit yes.

4. **Apply.** On approval, run:

   ```bash
   python3 "$PLUGIN_ROOT/skills/khenrix-setup/scripts/reconcile.py" --cli codex --apply
   ```

   New MCP servers are appended as TOML tables, absent `approval_policy` /
   `sandbox_mode` / `[projects."…"]` trust keys are added, the managed alias
   block is ensured in `~/.bash_aliases`, and the house-style block is ensured in
   `~/.codex/AGENTS.md`.

5. **Verify.** Show the relevant `[mcp_servers.*]` sections of
   `~/.codex/config.toml` so the user can confirm. Changes apply on the next
   Codex session.

## Notes

- If the user explicitly wants drifted managed entries re-aligned to the source
  of truth (not just additions), add `--update-drift` to the apply command.
  Default behaviour leaves existing managed entries as-is to avoid clobbering
  local tweaks.
- To inspect the source of truth, read `capabilities.toml` at the plugin root.
