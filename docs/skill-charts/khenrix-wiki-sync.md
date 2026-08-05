# khenrix-wiki-sync — flow

```mermaid
flowchart TD
    accTitle: khenrix-wiki-sync flow
    START([vault and remote may have diverged]) --> SCAN[scan both sides]
    SCAN --> G_CONFLICT{do both sides have changes?}
    G_CONFLICT -- no --> APPLY[apply the one-sided change]
    G_CONFLICT -- yes --> STOP([report the conflict; never merge silently])
    APPLY --> DONE([in sync])
```

## Gate evidence

| Gate | Kind | Evidence |
|---|---|---|
| G_CONFLICT | agent | covered by the wikisync unittests, which `make eval SKILL=khenrix-wiki-sync` runs as its deterministic gate (`eval_harness.DETERMINISTIC_GATED`) |
