---
name: khenrix-setup
description: Reconciles this Antigravity (agy) installation with the shared khenrix source of truth — reviews the live MCP servers in ~/.gemini/config/mcp_config.json, the trusted workspaces, shell aliases, and GEMINI.md, shows what differs, and additively adds only what is missing without removing anything machine-specific. Use when the user wants to set up, sync, audit, or update their agy environment to match the khenrix-utils capabilities, or asks to install the shared MCP servers / house style.
---

# khenrix-setup (Antigravity / agy)

Reconciles agy with the shared khenrix capabilities in `capabilities.toml`
(bundled at the plugin root). A deterministic engine — `scripts/reconcile.py` —
does the diff and the **non-destructive** merge. Run it, present the review,
then apply only after the user confirms.

## What "reconcile" means here

- **Additive only.** Missing declared MCP servers (added to
  `~/.gemini/config/mcp_config.json`), trusted workspaces and shell aliases are
  added. Anything configured outside khenrix is reported as `EXTRA` and **never
  removed**.
- **Review before write.** The default run is read-only.
- **Backups.** Modified files are copied to `*.khenrix-backup` first.

## Steps

1. **Review (read-only).** Run the bundled engine and show its full output (agy
   installs this plugin at a fixed path):

   ```bash
   python3 "$HOME/.gemini/config/plugins/khenrix-utils/skills/khenrix-setup/scripts/reconcile.py" --cli agy
   ```

2. **Summarize** the diff: which MCP servers will be added to
   `mcp_config.json`, which trusted workspaces are added to
   `~/.gemini/antigravity-cli/settings.json`, and that EXTRA entries are left
   untouched. Note that http servers are written with `httpUrl` and stdio
   servers with `command`/`args`/`env`.

3. **Confirm.** Ask the user to approve. Do not proceed without an explicit yes.

4. **Apply.** On approval:

   ```bash
   python3 "$HOME/.gemini/config/plugins/khenrix-utils/skills/khenrix-setup/scripts/reconcile.py" --cli agy --apply
   ```

   This merges MCP servers into `mcp_config.json`, adds trusted workspaces,
   ensures the managed alias block in `~/.bash_aliases`, and ensures the house-style
   block in `~/.gemini/GEMINI.md`.

5. **Verify.** Show `~/.gemini/config/mcp_config.json` so the user can confirm.
   Restart agy to pick up new servers.

## Notes

- agy prompts per-action for approvals, so there is no static approval/sandbox
  key to reconcile (reported as informational).
- To inspect the source of truth, read `capabilities.toml` at the plugin root.
