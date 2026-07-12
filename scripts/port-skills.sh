#!/usr/bin/env bash
# port-skills.sh — mirror portable third-party Claude skill BODIES onto codex + agy.
#
# The skills are NOT vendored into this repo (they are third-party, some licensed) — this
# script copies them from THIS machine's Claude plugin caches at run time into a machine-local
# "khenrix-ported" plugin, then installs that plugin via each CLI's own mechanism:
#   - agy:   `agy plugin install <dir>`            (skills copied into ~/.gemini/config/plugins)
#   - codex: local marketplace + `codex plugin add` (root kept under ~/.local/share)
#
# Prereq: `claude plugin install` has populated ~/.claude/plugins/cache (bootstrap-machine.sh
# does this). Idempotent + reversible (agy: `agy plugin uninstall khenrix-ported`;
# codex: `codex plugin remove khenrix-ported@khenrix-ported-marketplace`
#        + `codex plugin marketplace remove khenrix-ported-marketplace`).
#
# Curation (design rule: port what resolves; keep Claude-mechanism / hook-and-script skills
# Claude-only). superpowers is NATIVE on codex, so its methodology skills go to agy only;
# skill-creator is native on both codex (.system) — not ported. using-superpowers /
# subagent-driven-development / dispatching-parallel-agents (Claude subagent mechanics),
# last30days / watch (hook+script+API setup) stay Claude-only.
set -euo pipefail

CC="$HOME/.claude/plugins/cache"
OBS_SKILLS="${OBSIDIAN_VAULT:-$HOME/git/obsidian-vault}/skills"

# Skills mirrored to BOTH codex and agy (harness-agnostic, dependency-light).
SHARED_SKILLS=(frontend-design claude-md-improver autoresearch canvas defuddle obsidian-bases obsidian-markdown think)
# superpowers methodology skills — agy ONLY (codex has superpowers native).
SUPERPOWERS_SKILLS=(brainstorming writing-plans executing-plans systematic-debugging test-driven-development verification-before-completion requesting-code-review receiving-code-review finishing-a-development-branch using-git-worktrees)

# Resolve a source skill dir by name (searches the Claude caches + the obsidian vault).
# Prints the path, or nothing (caller warns) — takes the highest-sorted version dir.
resolve_src() {
  local name="$1" hit
  hit="$(find "$CC" -maxdepth 5 -type d -path "*/skills/$name" 2>/dev/null | sort | tail -1)"
  [ -z "$hit" ] && [ -d "$OBS_SKILLS/$name" ] && hit="$OBS_SKILLS/$name"
  printf '%s' "$hit"
}

stage_skills() {  # $1=dest skills dir ; rest=skill names
  local dest="$1"; shift
  local n src
  for n in "$@"; do
    src="$(resolve_src "$n")"
    if [ -n "$src" ] && [ -f "$src/SKILL.md" ]; then
      cp -r "$src" "$dest/$n"
    else
      echo "  WARN: source for skill '$n' not found — skipped (install its Claude plugin first)"
    fi
  done
}

echo "== agy: build + install khenrix-ported (superpowers methodology + shared) =="
AGY_BUILD="$(mktemp -d)"
mkdir -p "$AGY_BUILD/skills"
cat > "$AGY_BUILD/plugin.json" <<'JSON'
{
  "name": "khenrix-ported",
  "version": "0.1.0",
  "description": "Portable third-party Claude skills mirrored to agy (machine-local copy from Claude caches; not vendored into khenrix-utils).",
  "author": { "name": "khenrix" },
  "homepage": "https://github.com/khenrix/khenrix-utils",
  "skills": "./skills/"
}
JSON
stage_skills "$AGY_BUILD/skills" "${SUPERPOWERS_SKILLS[@]}" "${SHARED_SKILLS[@]}"
if command -v agy >/dev/null 2>&1; then
  agy plugin validate "$AGY_BUILD" >/dev/null && agy plugin install "$AGY_BUILD"
  echo "  agy skills installed: $(ls "$HOME/.gemini/config/plugins/khenrix-ported/skills/" 2>/dev/null | wc -l)"
else
  echo "  agy not on PATH — skipped"
fi
rm -rf "$AGY_BUILD"

echo "== codex: build local marketplace + install khenrix-ported (shared only; superpowers/skill-creator native) =="
CODEX_ROOT="$HOME/.local/share/khenrix-ported-codex"
rm -rf "$CODEX_ROOT"
mkdir -p "$CODEX_ROOT/.agents/plugins" "$CODEX_ROOT/plugins/khenrix-ported/.codex-plugin" "$CODEX_ROOT/plugins/khenrix-ported/skills"
cat > "$CODEX_ROOT/.agents/plugins/marketplace.json" <<'JSON'
{
  "name": "khenrix-ported-marketplace",
  "interface": { "displayName": "khenrix ported (codex)" },
  "plugins": [
    { "name": "khenrix-ported",
      "source": { "source": "local", "path": "./plugins/khenrix-ported" },
      "policy": { "installation": "AVAILABLE", "authentication": "ON_INSTALL" },
      "category": "Productivity" }
  ]
}
JSON
cat > "$CODEX_ROOT/plugins/khenrix-ported/.codex-plugin/plugin.json" <<'JSON'
{
  "name": "khenrix-ported",
  "version": "0.1.0",
  "description": "Portable third-party Claude skills mirrored to Codex (machine-local copy from Claude caches; not vendored into khenrix-utils).",
  "author": { "name": "khenrix" },
  "homepage": "https://github.com/khenrix/khenrix-utils",
  "skills": "./skills/",
  "interface": { "displayName": "khenrix ported skills", "shortDescription": "Portable Claude skills mirrored to Codex", "category": "Productivity" }
}
JSON
stage_skills "$CODEX_ROOT/plugins/khenrix-ported/skills" "${SHARED_SKILLS[@]}"
if command -v codex >/dev/null 2>&1; then
  codex plugin marketplace list 2>/dev/null | grep -q khenrix-ported-marketplace || codex plugin marketplace add "$CODEX_ROOT"
  codex plugin list 2>/dev/null | grep -q "khenrix-ported@.*installed" || codex plugin add "khenrix-ported@khenrix-ported-marketplace"
  echo "  codex skills installed: $(ls "$HOME/.codex/plugins/cache/khenrix-ported-marketplace/khenrix-ported/0.1.0/skills/" 2>/dev/null | wc -l)"
else
  echo "  codex not on PATH — skipped"
fi

echo "== done =="
