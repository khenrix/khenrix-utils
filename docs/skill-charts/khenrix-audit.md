# khenrix-audit — flow

```mermaid
flowchart TD
    accTitle: khenrix-audit flow
    START([is my multi-CLI setup coherent?]) --> INV[inventory every CLI's live config]
    INV --> G_DRIFT{does anything differ from the source of truth?}
    G_DRIFT -- no --> CLEAN([report clean, with what was checked])
    G_DRIFT -- yes --> REPORT[report each difference and its cause]
    REPORT --> G_FIXABLE{is the fix additive?}
    G_FIXABLE -- yes --> SUGGEST([suggest khenrix-setup])
    G_FIXABLE -- no --> ESCALATE([name it as a machine-specific decision])
```

## Gate evidence

| Gate | Kind | Evidence |
|---|---|---|
| G_DRIFT | code | `scripts/env_inventory.py::def _self_test` |
| G_FIXABLE | agent | SKILL.md: reconcile is additive; anything destructive is the user's call |
