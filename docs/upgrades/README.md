# Upgrade reports

The `khenrix-upgrade` skill writes a dated report here each time it runs, one per
CLI: `<cli>-<YYYY-MM-DD>.md` (e.g. `claude-2026-05-30.md`).

Each report captures:
- **Findings** per dimension (models & reasoning, experimental features,
  skill/plugin quality, MCP / settings / house-style) with sources.
- **Repo changes applied** in that run (SKILL.md / `capabilities.toml` / house-style).
- **Deferred live-config recommendations** — model / reasoning / experimental-flag
  tuning that is *not* auto-applied, with exact copy-paste commands to apply it.

These reports are the audit trail of how our usage of each CLI has been modernized
over time. They never contain secrets.
