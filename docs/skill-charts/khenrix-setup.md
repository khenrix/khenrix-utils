# khenrix-setup — flow

```mermaid
flowchart TD
    accTitle: khenrix-setup flow
    START([reconcile this CLI with the source of truth]) --> READ[read the live config]
    READ --> G_READABLE{could every config file be read?}
    G_READABLE -- no --> REFUSE([refuse; never overwrite a file it cannot read])
    G_READABLE -- yes --> DIFF[compute what is missing]
    DIFF --> G_APPROVE{operator approves the additions?}
    G_APPROVE -- no --> SHOW([show the diff and stop])
    G_APPROVE -- yes --> APPLY[add missing entries; update khenrix-managed ones]
    APPLY --> DONE([reconciled, nothing removed])
```

## Gate evidence

| Gate | Kind | Evidence |
|---|---|---|
| G_READABLE | code | `scripts/lib/reconcile_test.py::def run` |
| G_APPROVE | agent | SKILL.md's procedure shows the diff before applying |
