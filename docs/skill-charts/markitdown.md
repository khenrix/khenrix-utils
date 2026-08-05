# markitdown — flow

```mermaid
flowchart TD
    accTitle: markitdown flow
    START([a document to read as markdown]) --> G_SUPPORTED{format markitdown handles?}
    G_SUPPORTED -- no --> OTHER([use another reader])
    G_SUPPORTED -- yes --> CONVERT[markitdown converts to markdown]
    CONVERT --> G_EMPTY{did anything come back?}
    G_EMPTY -- no --> SAY([report the conversion produced nothing])
    G_EMPTY -- yes --> DONE([read the markdown])
```

## Gate evidence

| Gate | Kind | Evidence |
|---|---|---|
| G_SUPPORTED | agent | SKILL.md lists the formats; an unsupported one is declined rather than attempted |
| G_EMPTY | agent | SKILL.md: an empty conversion is reported, never presented as an empty document |
