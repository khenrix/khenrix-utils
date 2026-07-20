#!/usr/bin/env bash
# bootstrap-machine.sh — reproduce the khenrix agentic-CLI capability SET on a new machine.
#
# Idempotent: every step is check-before-act. `--dry-run` prints planned actions and
# performs no mutation. Reproduces the capability SET, not exact versions (third-party
# plugins install at marketplace HEAD). Secrets/auth are provisioned by hand — see
# docs/environment/auth-and-secrets.md. This script NEVER prints or stores a secret value.
#
# Ownership: the 4 shared MCPs (context7, vercel, chrome-devtools, linkedin)
# are owned by khenrix reconcile (via `khenrix-setup`), NOT by this script. Bootstrap only
# adds the parity additions (playwright / slack / codebase-memory-mcp on codex+agy) and the
# one-time marketplace/plugin setup.
set -euo pipefail

DRY=0
# Unknown flags must NOT fall through to a real run — same reasoning as Tier 0, and
# it matters more here: Tier 1 installs plugins and rewrites CLI config, so a
# misspelt `--dryrun` silently mutating the machine defeats shipping a dry run.
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    -h|--help) echo "Usage: bootstrap-machine.sh [--dry-run]"; exit 0 ;;
    *) echo "bootstrap-machine.sh: unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
run()  { if [ "$DRY" = 1 ]; then echo "DRY:  $*"; else echo "RUN:  $*"; "$@"; fi; }
skip() { echo "SKIP: $* (already present)"; }
have() { command -v "$1" >/dev/null 2>&1; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${KHENRIX_REPO:-$HOME/git/khenrix-utils}"
REPO_URL="git@github.com:khenrix/khenrix-utils.git"
VAULT="${OBSIDIAN_VAULT:-$HOME/git/obsidian-vault}"
VAULT_URL="git@github.com:khenrix/obsidian-vault.git"

echo "== Tier 0 (unauthenticated prerequisites) =="
# Tier 1 RUNS Tier 0 rather than assuming someone did. The two tiers split on
# authentication (see bootstrap-tier0.sh): Tier 0 provisions and tolerates, Tier 1
# assumes provisioning succeeded and fails hard. That assumption is only sound if
# Tier 0 actually ran, so this is a dependency, not a suggestion.
#
# --dry-run PROPAGATES. A dry run that provisions the machine is not a dry run,
# and Tier 0's own --dry-run mutates nothing (not even mkdir).
TIER0="$HERE/bootstrap-tier0.sh"
[ -x "$TIER0" ] || { echo "FATAL: Tier 0 missing or not executable: $TIER0" >&2; exit 1; }
if [ "$DRY" = 1 ]; then TIER0_ARGS=(--dry-run); else TIER0_ARGS=(); fi
if ! "$TIER0" "${TIER0_ARGS[@]}"; then
  echo "FATAL: Tier 0 reported missing prerequisites. Resolve them, then re-run." >&2
  exit 1
fi

echo "== Tier 1 prereqs (authenticated; MUST be present) =="
# Absence is now a REAL ERROR, not the expected bare-distro state: Tier 0 has just
# run and succeeded, so anything still missing here is a genuine install failure.
# Printing `MISSING:` and carrying on — which is what this used to do — meant every
# step below ran against a machine known not to satisfy them.
MISSING=0
for bin in claude codex agy uv gh node git; do
  if have "$bin"; then echo "  ok: $bin"; else echo "  MISSING: $bin"; MISSING=1; fi
done
if [ "$MISSING" = 1 ]; then
  echo "FATAL: install the above (docs/machine-setup.md), then re-run." >&2
  exit 1
fi

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
# superpowers@openai-curated. (google-drive was decommissioned 2026-07-20 -- the native
# claude.ai Drive MCP covers it, and the stdio one hardcoded an asdf node path.)
# Parity MCP additions on codex: playwright, codebase-memory-mcp (NOT slack — slack is native).
# Reproduce the active openaiDeveloperDocs HTTP MCP.
# NOTE: confirm exact `codex mcp add` / `codex plugin install` non-interactive syntax at
# install time (T11); these mutate, so they run in the parity-install step, not the dry-run.

echo "== agy MCP config (stdlib merge, never clobber) =="
# The 5 declared MCPs reach agy through reconcile below (reconcile.py --all covers
# claude, codex AND agy), so nothing is merged by hand here.
#
# scripts/lib/mcp_merge.py --apply is the mechanism for the parity additions
# (playwright / codebase-memory-mcp / slack) — atomic, stops on a same-name
# different-command collision. It is deliberately NOT invoked: those three are not
# declared in capabilities.toml, so there is no additions manifest to merge, and
# slack's XOXC/XOXD are per-machine rotating tokens that must never enter a shared
# source of truth (see the linkedin/slack note in capabilities.toml). Add them by
# hand per machine, or declare them first and let reconcile own them.
#   python3 "$REPO/scripts/lib/mcp_merge.py" --apply "$HOME/.gemini/config/mcp_config.json" additions.json

echo "== khenrix-utils (source of truth) =="
if [ -d "$REPO/.git" ]; then skip "clone $REPO"; else run git clone "$REPO_URL" "$REPO"; fi
if [ -d "$VAULT/.git" ]; then skip "clone $VAULT"; else run git clone "$VAULT_URL" "$VAULT"; fi
run make -C "$REPO" khenrix-refresh

echo "== Reconcile config into every CLI (deterministic; additive) =="
# The headless equivalent of the `khenrix-setup` skill, NOT the agent skill: it adds
# declared entries that are absent and leaves everything else untouched. `--all`
# genuinely applies now — it used to hardcode apply=False, so this call would have
# been a silent no-op had it been wired earlier. Non-zero here aborts (set -e).
run python3 "$REPO/scripts/lib/reconcile.py" --apply --all

echo "== Ported third-party skills (codex + agy) =="
# Mirrors portable Claude skill bodies onto codex/agy from THIS machine's Claude caches
# (not vendored — must run AFTER the Claude plugins above are installed). Idempotent.
run bash "$REPO/scripts/port-skills.sh"

echo "== Verify what was built =="
# A bootstrap that ends without proving anything is how a machine ends up with a
# chrome-devtools MCP that is configured and dead. doctor.py checks BEHAVIOUR, and
# it runs last so it sees the finished machine. set -e makes a failed check fail
# the bootstrap — the whole point is that this cannot be ignored.
run python3 "$REPO/scripts/doctor.py" --profile full

echo "== Done (dry-run=$DRY) =="
