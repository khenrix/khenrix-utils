#!/usr/bin/env bash
# wiki-autosave-gate.sh — Stop hook (global): once per session, if the session
# looks substantive, block the stop once and ask Claude to consider filing
# durable insights into the Obsidian vault via the claude-obsidian /save skill.
# Remove by deleting this file and the Stop stanza in ~/.claude/settings.json.
set -euo pipefail

IN=$(cat)
command -v jq >/dev/null 2>&1 || exit 0

# Loop guard: we already blocked once this turn-chain.
[ "$(jq -r '.stop_hook_active // false' <<<"$IN")" = "true" ] && exit 0

SID=$(jq -r '.session_id // empty' <<<"$IN")
TP=$(jq -r '.transcript_path // empty' <<<"$IN")
[ -n "$SID" ] && [ -f "$TP" ] || exit 0

# Only when the vault exists on this machine.
[ -d "$HOME/git/obsidian-vault/wiki" ] || exit 0

# Once per session (marker resets on reboot, which is fine).
MARK="/tmp/claude-wiki-autosave-$SID"
[ -e "$MARK" ] && exit 0

# Substantive session = enough real user turns (tool_results also log as
# type:user in the transcript, so exclude them — this filters headless
# one-shot runs like llm-council fanouts) and enough content overall.
TURNS=$(grep '"type":"user"' "$TP" 2>/dev/null | grep -vc tool_result || true)
SIZE=$(stat -c %s "$TP" 2>/dev/null || echo 0)
[ "${TURNS:-0}" -ge 6 ] && [ "${SIZE:-0}" -ge 80000 ] || exit 0

touch "$MARK"
printf '%s' '{"decision":"block","reason":"Once-per-session wiki capture check: if this session has produced durable insights, decisions, or knowledge worth keeping, file them into the Obsidian vault at ~/git/obsidian-vault using the claude-obsidian /save skill (one concise note; follow its transport policy). If nothing is wiki-worthy, or it was already saved, just finish normally. Do not ask the user for permission; if you do save, mention it in one line."}'
