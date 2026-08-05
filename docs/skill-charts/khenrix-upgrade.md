# khenrix-upgrade — flow

```mermaid
flowchart TD
    accTitle: khenrix-upgrade flow
    START([modernize this CLI setup]) --> RESEARCH[research current CLI features and models]
    RESEARCH --> G_CHANGED{has anything actually changed?}
    G_CHANGED -- no --> SAY([report no change is warranted])
    G_CHANGED -- yes --> PROPOSE[propose repo edits + live-config tuning]
    PROPOSE --> G_GATED{does make verify still pass?}
    G_GATED -- no --> PROPOSE
    G_GATED -- yes --> DONE([commit the repo half; report the live half])
```

## Gate evidence

| Gate | Kind | Evidence |
|---|---|---|
| G_CHANGED | agent | SKILL.md: research first; an unchanged upstream means no edit |
| G_GATED | code | `scripts/render.py::def check` |
