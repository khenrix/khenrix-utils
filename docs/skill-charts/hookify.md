# hookify — flow

Claude-Code-only, and it says so before it writes anything: a Codex or agy user gets a
plain-language rule instead of a hook that won't fire for them. The correction's shape
picks the event (start-blocking is PreToolUse, finish-gating is Stop, follow-up is
PostToolUse), its cost picks warn vs block, and every command is hardened so a missing
or erroring predicate blocks rather than silently falls open. Nothing is written to
settings.json without an explicit yes. Source: `shared/skills/hookify/SKILL.md`.

```mermaid
flowchart TD
    accTitle: hookify flow
    accDescr: A recurring correction is named, then routed to plain-language config on Codex or agy, or classified by shape into a Claude Code hook event and by cost into warn or block, with the warn path further split between a nudge aimed at the agent and a note aimed at the user. Every command is hardened so a predicate error blocks rather than silently allows, and nothing is written to settings.json without an explicit yes.

    START([a correction the user<br/>keeps re-issuing this session]) --> NAME[pin down: bad action + phase,<br/>the wanted rule, cost if repeated]
    NAME --> G_ONPLATFORM{user is on<br/>Claude Code?}
    G_ONPLATFORM -- "Codex or agy" --> OTHER_CLI([stop: hooks are Claude-Code-only -<br/>describe the rule for that CLI's own config])
    G_ONPLATFORM -- "yes" --> G_SHAPE{what shape is<br/>the correction?}
    G_SHAPE -- "stop starting X" --> PRETOOL[PreToolUse -<br/>matcher = tool-name regex]
    G_SHAPE -- "stop finishing before Y" --> STOPEVT[Stop -<br/>no matcher]
    G_SHAPE -- "after X also do Y" --> POSTTOOL[PostToolUse -<br/>matcher = tool-name regex]
    G_SHAPE -- "remember Z up front" --> PROMPTSUB[UserPromptSubmit -<br/>no matcher]
    PRETOOL --> G_COST{annoying but cheap, or<br/>wrong, expensive, irreversible?}
    STOPEVT --> G_COST
    POSTTOOL --> G_COST
    PROMPTSUB --> G_COST
    G_COST -- "cheap" --> WARN_PATH[warn - exit 0,<br/>the action proceeds]
    G_COST -- "costly" --> BLOCK_PATH[block - exit 2 + stderr, or<br/>exit 0 + permissionDecision deny]
    WARN_PATH --> G_AUDIENCE{nudge aimed at the agent,<br/>or a note for the user?}
    BLOCK_PATH --> G_HEADLESS{external approval needed<br/>while running headless -p?}
    G_HEADLESS -- "yes" --> DEFER[permissionDecision defer -<br/>needs Claude Code 2.1.89+]

    subgraph HARDEN [author and harden the command - repeats until fail-closed and approved]
        AUTHOR[write the hooks.Event entry -<br/>matcher + command]
        AUTHOR --> G_FAILOPEN{a predicate error falls<br/>through to allow?}
        G_FAILOPEN -- "yes" --> INVERT[invert the predicate - block from<br/>the failure branch instead]
        INVERT --> AUTHOR
        G_FAILOPEN -- "no" --> G_CONFIRM{explicit yes from the user<br/>on the final JSON?}
        G_CONFIRM -- "declines / wants changes" --> AUTHOR
    end

    G_AUDIENCE -- "agent" --> USE_CTX[hookSpecificOutput.additionalContext -<br/>nested]
    G_AUDIENCE -- "user" --> USE_SYS[systemMessage -<br/>top-level only]
    G_HEADLESS -- "no" --> AUTHOR
    USE_CTX --> AUTHOR
    USE_SYS --> AUTHOR
    DEFER --> AUTHOR
    G_CONFIRM -- "yes" --> WRITE[hand the settings.json edit<br/>to update-config - merges additively]
    WRITE --> DONE([guardrail is live on<br/>the next matching event])
```

## Gate evidence

| Gate | Kind | Evidence |
|---|---|---|
| G_ONPLATFORM | agent | no eval covers this; SKILL.md frontmatter — "Emits CLAUDE-CODE hooks only — Codex and agy have their own mechanisms, so it points their users to that config instead". The eval that graded it was dropped 2026-08-08: it scored 10/12 on BOTH conditions, i.e. a baseline already refuses to hand a Claude hook to a Codex user |
| G_SHAPE | agent | `evals/hookify/evals.json::Maps 'don't FINISH before doing Y' to a Stop hook (gates completion), NOT a PreToolUse tool gate and not PostToolUse` |
| G_COST | agent | `evals/hookify/evals.json::Recognizes this is a WARN (not block) guardrail and that the natural event (PostToolUse — the edit already happened, or a non-blocking PreToolUse) cannot/should not prevent the action` |
| G_AUDIENCE | agent | `evals/hookify/evals.json::distinguishes it from systemMessage, which surfaces a note to the USER` |
| G_HEADLESS | agent | `evals/hookify/evals.json::ask prompts interactively and does not exit the process with the call preserved` |
| G_FAILOPEN | agent | no eval covers the jq-error path; SKILL.md §"Fail closed, not open" — a naive `jq -e … && {block} || exit 0` blocks only when jq succeeds, so invert the predicate and deny from the `||` branch. Dropped 2026-08-08 at 12/12 on BOTH conditions. The SIBLING fail-open mechanism is now graded instead: `evals/hookify/evals.json::Identifies exit code 1 as the cause: only exit 2 is the BLOCKING exit code` |
| G_CONFIRM | agent | `evals/hookify/evals.json::Confirms before writing the hook to settings.json` |
