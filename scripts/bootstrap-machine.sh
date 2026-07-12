#!/usr/bin/env bash
# bootstrap-machine.sh — reproduce the khenrix agentic-CLI capability SET on a new machine.
#
# Idempotent: every step is check-before-act. `--dry-run` prints planned actions and
# performs no mutation. Reproduces the capability SET, not exact versions (third-party
# plugins install at marketplace HEAD). Secrets/auth are provisioned by hand — see
# docs/environment/auth-and-secrets.md. This script NEVER prints or stores a secret value.
#
# Ownership: the 5 shared MCPs (context7, vercel, google-drive, chrome-devtools, linkedin)
# are owned by khenrix reconcile (via `khenrix-setup`), NOT by this script. Bootstrap only
# adds the parity additions (playwright / slack / codebase-memory-mcp on codex+agy) and the
# one-time marketplace/plugin setup.
set -euo pipefail

DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1
run()  { if [ "$DRY" = 1 ]; then echo "DRY:  $*"; else echo "RUN:  $*"; "$@"; fi; }
skip() { echo "SKIP: $* (already present)"; }
have() { command -v "$1" >/dev/null 2>&1; }

REPO="${KHENRIX_REPO:-$HOME/git/khenrix-utils}"
REPO_URL="git@github.com:khenrix/khenrix-utils.git"
VAULT="${OBSIDIAN_VAULT:-$HOME/git/obsidian-vault}"
VAULT_URL="git@github.com:khenrix/obsidian-vault.git"

echo "== Prereqs =="
# Install per docs/machine-setup.md (manual, versioned). Node MUST match the version the
# reconcile-owned MCP commands hardcode (capabilities.toml pins an absolute asdf node path,
# e.g. nodejs/26.2.0) — install that exact version or ensure reconcile's PATH fallback works.
for bin in claude codex agy uv gh node git; do
  have "$bin" && echo "  ok: $bin" || echo "  MISSING: $bin — see docs/machine-setup.md"
done

echo "== Claude marketplaces (add if absent) =="
mkt_has() { claude plugin marketplace list 2>/dev/null | grep -q "$1"; }
add_mkt() { mkt_has "$1" && skip "marketplace $1" || run claude plugin marketplace add "$2"; }
add_mkt "claude-plugins-official"       "anthropics/claude-plugins-official"
add_mkt "last30days-skill"              "mvanhorn/last30days-skill"
add_mkt "claude-video"                  "bradautomates/claude-video"
add_mkt "openai-codex"                  "openai/codex-plugin-cc"
add_mkt "khenrix-claude-marketplace"    "$REPO/marketplaces/claude"
add_mkt "agricidaniel-claude-obsidian"  "$VAULT"

echo "== Claude plugins (install if absent; name@marketplace) =="
plug_has() { claude plugin list 2>/dev/null | grep -q "$1@"; }
add_plug() { plug_has "$1" && skip "plugin $1" || run claude plugin install "$1@$2"; }
add_plug "claude-md-management" "claude-plugins-official"
add_plug "code-review"          "claude-plugins-official"
add_plug "code-simplifier"      "claude-plugins-official"
add_plug "frontend-design"      "claude-plugins-official"
add_plug "playwright"           "claude-plugins-official"
add_plug "pyright-lsp"          "claude-plugins-official"
add_plug "security-guidance"    "claude-plugins-official"
add_plug "superpowers"          "claude-plugins-official"
add_plug "typescript-lsp"       "claude-plugins-official"
add_plug "last30days"           "last30days-skill"
add_plug "watch"                "claude-video"
add_plug "codex"                "openai-codex"
add_plug "claude-obsidian"      "agricidaniel-claude-obsidian"   # local-dir plugin (vault below)
# Postcondition: `claude plugin list` shows each enabled.

echo "== Claude MCP =="
# The 5 shared MCPs + slack + codebase-memory-mcp are reconcile-owned / already present on
# Claude — NOT re-added here. The claude.ai OAuth connectors (Gmail/Calendar/Drive) are an
# interactive `/mcp` login inside Claude Code — MANUAL, not scripted.

echo "== Codex (native XOR shared-MCP) =="
# Native-preferred ONLY where reconcile does NOT own the capability: slack, github,
# superpowers@openai-curated. google-drive stays the reconcile MCP (NO native drive plugin).
# Parity MCP additions on codex: playwright, codebase-memory-mcp (NOT slack — slack is native).
# Reproduce the active openaiDeveloperDocs HTTP MCP.
# NOTE: confirm exact `codex mcp add` / `codex plugin install` non-interactive syntax at
# install time (T11); these mutate, so they run in the parity-install step, not the dry-run.

echo "== agy MCP config (stdlib merge, never clobber) =="
AGY_CFG="$HOME/.gemini/config/mcp_config.json"
# Parity additions on agy: playwright, slack, codebase-memory-mcp — merged via the helper
# (atomic; stops on a same-name/different-command collision). Example (run at T11):
#   run python3 "$REPO/scripts/lib/mcp_merge.py" --apply "$AGY_CFG" <additions.json>
# (add a small --apply CLI mode to mcp_merge.py when wiring T11 if not already present.)

echo "== khenrix-utils (source of truth) =="
if [ -d "$REPO/.git" ]; then skip "clone $REPO"; else run git clone "$REPO_URL" "$REPO"; fi
if [ -d "$VAULT/.git" ]; then skip "clone $VAULT"; else run git clone "$VAULT_URL" "$VAULT"; fi
run make -C "$REPO" khenrix-refresh
# khenrix-setup per CLI = the deterministic reconcile step (NOT the agent skill). Confirm the
# exact headless invocation + success check at T11, e.g.:
#   run python3 "$REPO/scripts/lib/reconcile.py" --apply --all   # then assert exit 0

echo "== Ported third-party skills (codex + agy) =="
# Mirrors portable Claude skill bodies onto codex/agy from THIS machine's Claude caches
# (not vendored — must run AFTER the Claude plugins above are installed). Idempotent.
run bash "$REPO/scripts/port-skills.sh"

echo "== Done (dry-run=$DRY) =="
