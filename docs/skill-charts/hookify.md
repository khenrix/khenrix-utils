# hookify — flow

```mermaid
flowchart TD
    accTitle: hookify flow
    START([a repeated correction worth automating]) --> G_DETERMINISTIC{can a script decide it?}
    G_DETERMINISTIC -- no --> SKILL([write a skill or a rule instead])
    G_DETERMINISTIC -- yes --> WRITE[write the hook + its matcher]
    WRITE --> G_TESTED{does it fire on the real case and not otherwise?}
    G_TESTED -- no --> WRITE
    G_TESTED -- yes --> INSTALL[install into settings]
    INSTALL --> DONE([the correction is now mechanical])
```

## Gate evidence

| Gate | Kind | Evidence |
|---|---|---|
| G_DETERMINISTIC | agent | SKILL.md: a hook is for what a script can decide; judgement stays in a skill |
| G_TESTED | agent | SKILL.md requires firing the hook on a real case before installing it |
