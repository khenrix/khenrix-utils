# chunk-map — flow

```mermaid
flowchart TD
    accTitle: chunk-map flow
    START([a codebase too large to hold at once]) --> STATS[codebase_stats.py: LOC + git co-change]
    STATS --> DRAW[you draw chunk boundaries]
    DRAW --> G_SIZE{each chunk reasonable in one pass?}
    G_SIZE -- no --> SPLIT[split by responsibility, never mid-responsibility] --> DRAW
    G_SIZE -- yes --> SEAMS[record exposes / consumes / couples_with]
    SEAMS --> G_LEAK{does anything reach past a seam?}
    G_LEAK -- yes --> SURFACE[surface the leak; widen or fix] --> SEAMS
    G_LEAK -- no --> WRITE[write .chunkmap/map.md with reviewed_sha]
    WRITE --> DONE([a map you can drill into])
```

## Gate evidence

| Gate | Kind | Evidence |
|---|---|---|
| G_SIZE | agent | SKILL.md: "split when you can no longer answer what this does, what it exposes, what it depends on" |
| G_LEAK | agent | SKILL.md's seam-leak verification step; the grep is the operator's, and a hit is surfaced not silently widened |
