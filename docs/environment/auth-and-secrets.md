# Auth & secrets checklist (cross-CLI)

Provisioning prerequisites for a new machine. **No secret values live here** — only the
names, the env var / on-disk path each is referenced by, and how to provision it. This
extends [machine-setup.md](../machine-setup.md) (which covers the Claude-only baseline) to
Codex + agy. Reproduce the capability set with [`scripts/bootstrap-machine.sh`](../../scripts/bootstrap-machine.sh),
then work this checklist. Verify completeness with
`python3 scripts/env_inventory.py --check-secrets-doc`.

## Stored secrets (files / env)

| Secret | Referenced by | Provision |
|---|---|---|
| google-drive OAuth client | `GOOGLE_DRIVE_OAUTH_CREDENTIALS` = `~/.config/google-drive-mcp/gcp-oauth.keys.json` | download the OAuth client JSON from the GCP console |
| google-drive token | `GOOGLE_DRIVE_MCP_TOKEN_PATH` = `~/.config/google-drive-mcp/tokens.json` | generated on first interactive google-drive auth |

## Interactive per-machine logins (no stored value)

None of these is a file you copy — each is an interactive login performed once per machine:

- **CLI logins** — `claude`, `codex`, `agy` each authenticate per machine.
- **`gh` auth** — needed for GitHub marketplace / private-repo access.
- **slack MCP** — its session tokens (the XOXC / XOXD pair) are per-machine, session-bound,
  and rotate; this is why reconcile deliberately leaves slack unmanaged. Re-auth per
  session. Reports `awaiting-auth` in the parity smoke until done.
- **linkedin MCP** — interactive per-machine login; no stored credential. (`UV_HTTP_TIMEOUT`
  below is tuning, not a secret.)
- **claude.ai OAuth connectors** (Gmail / Calendar / Drive) — interactive `/mcp` login inside
  Claude Code; Claude-only.
- **codex connector authorization** — for the codex-native plugins (slack, github).

## Non-secret configuration (tuning)

| Name | Value | Note |
|---|---|---|
| `UV_HTTP_TIMEOUT` | `300` | linkedin `uvx` launch timeout — tuning, not a secret |
| `BROWSER` | machine-local | browser launcher used by the vercel `mcp-remote` OAuth flow — tuning, not a secret |

## Local content provenance (required paths)

- `~/git/obsidian-vault` — the `claude-obsidian` vault. Clone: `git@github.com:khenrix/obsidian-vault.git` (private). Behavior-when-absent: the plugin's non-wiki skills still load; wiki features need the vault.
- `codebase-memory-mcp` store — a local on-disk index/DB. Document its path per machine; behavior-when-absent: re-index on first use.
