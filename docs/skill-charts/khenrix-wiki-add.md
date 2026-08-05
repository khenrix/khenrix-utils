# khenrix-wiki-add — flow

```mermaid
flowchart TD
    accTitle: khenrix-wiki-add flow
    START([something worth remembering]) --> G_VAULT{is a vault configured?}
    G_VAULT -- no --> SETUP([ask the user to run /wiki first])
    G_VAULT -- yes --> G_EXISTS{does a page already cover it?}
    G_EXISTS -- yes --> UPDATE[update that page]
    G_EXISTS -- no --> CREATE[create the page + index entry]
    UPDATE --> DONE([vault updated])
    CREATE --> DONE
```

## Gate evidence

| Gate | Kind | Evidence |
|---|---|---|
| G_VAULT | agent | covered by the wikisync unittests, which `make eval SKILL=khenrix-wiki-add` runs as its deterministic gate (`eval_harness.DETERMINISTIC_GATED`) |
| G_EXISTS | agent | SKILL.md: check for an existing page and offer to update rather than duplicate |
