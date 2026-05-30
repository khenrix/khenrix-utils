---
name: khenrix-setup
description: Reconciles this Codex installation with the shared khenrix source of truth — reviews the live MCP servers in config.toml, the approval/sandbox/trust settings, shell aliases, and AGENTS.md, shows what differs, and additively adds only what is missing without removing anything machine-specific. Use when the user wants to set up, sync, audit, or update their Codex environment to match the khenrix-utils capabilities, or asks to install the shared MCP servers / house style.
---

# khenrix-setup (Codex)

Reconciles Codex with the shared khenrix capabilities in `capabilities.toml`
(bundled at the plugin root). A deterministic engine — `scripts/reconcile.py` —
does the diff and the **non-destructive** merge. Run it, present the review,
then apply only after the user confirms.

## What "reconcile" means here

- **Additive only.** Missing declared `[mcp_servers.*]` tables, settings keys,
  trusted projects and shell aliases are added. Anything you configured outside
  khenrix (e.g. a machine-only MCP server) is reported as `EXTRA` and **never removed**.
- **Review before write.** The default run is read-only.
- **Backups.** `~/.codex/config.toml` and `AGENTS.md` are copied to
  `*.khenrix-backup` before any change.

## Steps

1. **Review (read-only).** Run the bundled engine and show its full output. The
   script lives in this skill's `scripts/` directory:

   ```bash
   python3 scripts/reconcile.py --cli codex
   ```

   (If the working directory isn't the skill root, use the absolute path to this
   skill's `scripts/reconcile.py`.)

2. **Summarize** the diff: which `[mcp_servers.*]` tables will be appended,
   which settings/trust entries are added, and confirm EXTRA entries (e.g. a
   Windows-only `chrome-devtools` on a non-Windows host) are left untouched.

3. **Confirm.** Ask the user to approve. Do not proceed without an explicit yes.

4. **Apply.** On approval:

   ```bash
   python3 scripts/reconcile.py --cli codex --apply
   ```

   New MCP servers are appended as TOML tables, absent `approval_policy` /
   `sandbox_mode` / `[projects."…"]` trust keys are added, the managed alias
   block is ensured in `~/.bashrc`, and the house-style block is ensured in
   `~/.codex/AGENTS.md`.

5. **Verify.** Show the relevant `[mcp_servers.*]` sections of
   `~/.codex/config.toml` so the user can confirm. Changes apply on the next
   Codex session.

## Notes

- Drift on an entry that already exists is reported but not overwritten unless
  the user asks; add `--update-drift` to the apply command only then.
- To inspect the source of truth, read `capabilities.toml` at the plugin root.
