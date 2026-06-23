---
name: hookify
description: >-
  Turn a recurring correction or frustration from THIS session into a durable Claude Code hook so the
  agent stops repeating the mistake. Identify the instruction the user keeps re-issuing, classify it
  into a hook EVENT (PreToolUse / PostToolUse / UserPromptSubmit / Stop / …) + a trigger condition +
  an action (warn vs block), then author the exact `hooks` entry for settings.json (or a small hook
  script) and confirm before writing. Emits CLAUDE-CODE hooks only — Codex and agy have their own
  mechanisms, so it points their users to that config instead. Use when a guardrail should outlive the
  conversation rather than be repeated by hand. Triggers: "make a hook so you stop doing X", "turn this
  correction into a rule", "hookify this", "stop reminding you to Y", "block yourself from running Z",
  "always use rg not grep — enforce it", "don't edit generated files again".
allowed-tools: Bash, Read, Edit
---

# hookify

Promote a recurring in-session correction into a **Claude Code hook** — a deterministic guardrail the
harness enforces, so the agent can't drift back into the mistake once the conversation moves on. The
user keeps saying the same thing ("run only the changed test, not the whole suite"; "don't touch the
generated files under `marketplaces/`"; "use `rg`, never `grep`"). A hook makes that stick.

**Claude-Code-specific.** Hooks live in Claude Code's `settings.json` and are run by the Claude
harness. **Codex and agy do not read these** — they have their own automation (Codex `config.toml`
/ AGENTS.md, agy settings). If the user is on Codex or agy, say so and point them at that CLI's config
instead of writing a Claude hook that won't fire for them.

Keep every guardrail **minimal and reviewable** — one tight matcher, one short command, a clear
reason string. Prefer **warn** over **block** unless the mistake is genuinely costly/irreversible.
**Always show the JSON and get a yes before writing to settings.json** (hand the write to the
`update-config` skill, which owns settings.json edits).

## Step 1 — name the recurring correction

Pull the actual pattern from this session — don't invent one. Pin down three things:

- **What** the agent did wrong (the bad action), and **when** (which tool / phase).
- **What** the user wants instead (the rule).
- **Cost of the mistake**: annoying-but-cheap → *warn*; wrong/expensive/irreversible → *block*.

State it back in one line and confirm you have it right before authoring anything.

## Step 2 — classify into EVENT + trigger + action

Map the correction to a Claude Code hook **event**. The full event set:
`PreToolUse`, `PostToolUse`, `UserPromptSubmit`, `Stop`, `SubagentStop`, `SessionStart`, `Notification`
(plus `PreCompact` and others — these cover almost every guardrail).

| Correction shape | Event | Matcher | Action |
|---|---|---|---|
| "stop running command/tool X" ("don't run the full suite", "no `grep`, use `rg`") | **PreToolUse** | tool name regex (`Bash`, `Edit\|Write`) | inspect `tool_input`, **block** the bad invocation |
| "don't edit generated/protected files" (e.g. `marketplaces/`) | **PreToolUse** | `Edit\|Write\|MultiEdit` | block when `tool_input.file_path` matches the protected path |
| "after editing X, also do Y" (lint, regenerate) | **PostToolUse** | `Edit\|Write` | warn / run the follow-up (can't undo the edit) |
| "when I ask for X, first remember Z" | **UserPromptSubmit** | none | inject context to stdout, or **block** the prompt (exit 2) |
| "don't finish without updating the changelog / running `make verify`" | **Stop** | none | **block stop** until the condition holds |
| "load context every session" | **SessionStart** | `startup\|resume` | print context to stdout |

Rules of thumb:
- **Tool events** (`PreToolUse`/`PostToolUse`) take a `matcher` = tool-name regex; **`UserPromptSubmit`
  and `Stop` take no matcher**.
- A "don't *start* doing X" guardrail is almost always **PreToolUse** (it can prevent the action).
  A "don't *finish* before Y" guardrail is **Stop** (it gates completion).
- `PostToolUse` fires *after* the action — use it to warn or to run a follow-up, never to "prevent"
  the edit that already happened.

## Step 3 — the hook contract (so the command is correct)

A hook **command** receives the event as **JSON on stdin** and signals its decision via **exit code**
and/or **stdout JSON**. Key stdin fields:

- All events: `session_id`, `cwd`, `hook_event_name`, `transcript_path`, `permission_mode`.
- Tool events: `tool_name`, `tool_input` (e.g. `tool_input.command` for Bash, `tool_input.file_path`
  for Edit/Write).
- `UserPromptSubmit`: `prompt`. `Stop`: `stop_hook_active`.

**Blocking a PreToolUse action — two ways:**

1. **Exit 2 + stderr** — simplest. The action is prevented and stderr is fed back to the agent as the
   reason.
2. **Exit 0 + stdout JSON** — structured decision:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "Run only the changed test file; the full suite is too slow here."
  }
}
```
`permissionDecision` is `"deny"` (block), `"allow"` (auto-approve), or `"ask"` (prompt the user).

Exit-code meaning: **0** = success (stdout JSON parsed; for `UserPromptSubmit`/`SessionStart`, stdout
is added to context); **2** = blocking error (stderr shown to the agent, action prevented); **any
other** = non-blocking error (logged, action continues). For **`UserPromptSubmit`**, exit 2 blocks the
prompt with the stderr message. For **`Stop`**, block with stdout JSON `{"decision":"block","reason":"…"}`
(or exit 2 + stderr) to force more work before finishing.

Use `${CLAUDE_PROJECT_DIR}` in commands to reference the project root portably.

## Step 4 — author the hook

Hooks live in `settings.json` under `hooks.<EventName>` → an array of groups, each `{ "matcher": …,
"hooks": [ { "type": "command", "command": "…" } ] }`. Keep the command tiny; if it needs more than a
one-liner, write a small script under `${CLAUDE_PROJECT_DIR}/.claude/hooks/` and call it.

**Example A — block the full test suite, allow a single file (PreToolUse / Bash, block):**

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "jq -e '.tool_input.command | test(\"(pytest|npm test|make test)\\\\b(?!.*[/.]\\\\w)\")' >/dev/null && { echo 'Run only the changed test file, not the whole suite.' >&2; exit 2; } || exit 0"
          }
        ]
      }
    ]
  }
}
```

**Example B — protect generated files (PreToolUse / Edit·Write, block via JSON):**

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit",
        "hooks": [
          {
            "type": "command",
            "command": "jq -e '.tool_input.file_path | test(\"marketplaces/\")' >/dev/null && printf '%s' '{\"hookSpecificOutput\":{\"hookEventName\":\"PreToolUse\",\"permissionDecision\":\"deny\",\"permissionDecisionReason\":\"marketplaces/ is generated — edit the source of truth instead.\"}}' || true"
          }
        ]
      }
    ]
  }
}
```

**Example C — don't finish without `make verify` (Stop, block):** a `Stop` hook (no matcher) whose
command checks the condition and, if unmet, prints `{"decision":"block","reason":"Run make verify before finishing."}`
to stdout (or exits 2 with that message on stderr). Guard against loops by honoring `stop_hook_active`.

Keep matchers narrow (a precise tool/path regex beats a broad one), reasons short and actionable, and
the predicate cheap. Prefer `jq` to read `tool_input`; interpret exit codes, don't echo-and-grep.

## Step 5 — confirm, then write

Show the user the final JSON (or script + the `settings.json` stanza), the event, and warn-vs-block.
**Get an explicit yes before writing.** Then hand the settings.json edit to the **`update-config`**
skill (it owns hook/permission/env edits to settings.json and merges additively without clobbering
existing hooks). After writing, tell the user the hook is live on the next matching event and how to
remove it (delete that `hooks.<EventName>` group).

If the user is on **Codex or agy**, stop before writing a Claude hook: explain hooks are Claude-Code
only and the equivalent guardrail lives in that CLI's own config — describe the rule in plain terms so
they can encode it there.
