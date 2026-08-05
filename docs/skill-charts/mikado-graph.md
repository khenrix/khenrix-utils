# mikado-graph — flow

```mermaid
flowchart TD
    accTitle: mikado-graph flow
    START([a change that keeps breaking other things]) --> TRY[attempt the naive change]
    TRY --> G_WORKS{does it work?}
    G_WORKS -- yes --> DONE([done; revert nothing])
    G_WORKS -- no --> RECORD[record the prerequisite it revealed]
    RECORD --> REVERT[revert to green]
    REVERT --> G_LEAF{is there a prerequisite with no blockers?}
    G_LEAF -- yes --> TRY
    G_LEAF -- no --> DEEPEN[explore one prerequisite deeper] --> TRY
```

## Gate evidence

| Gate | Kind | Evidence |
|---|---|---|
| G_WORKS | agent | SKILL.md: the build/test result decides, and the revert is unconditional on failure |
| G_LEAF | agent | SKILL.md's graph rule — always work a leaf, never a blocked node |
