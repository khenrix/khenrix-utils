# chunk-map — flow

Co-change first, imports second: the engine (`codebase_stats.py`) surfaces LOC rollups
and which directories change together in git history; the agent reads that as candidate
chunk boundaries, sizes each by reasoning capacity rather than a line count, records its
seams, and greps for leaks past them before writing the resumable map. Source:
`shared/skills/chunk-map/SKILL.md`; engine: `shared/skills/chunk-map/scripts/codebase_stats.py`.

```mermaid
flowchart TD
    accTitle: chunk-map flow
    accDescr: Locate and run the bundled LOC and co-change engine, which itself gates on the root being a real git repo and on whether HEAD has any commits yet. The agent reads co-change clusters as candidate chunks, sizes each by reasoning capacity rather than line count, records its seams, and greps for leaks past them before writing the resumable map. Resuming later re-derives only the chunks whose files changed since the recorded sha.

    START([codebase too big<br/>to hold at once]) --> LOCATE[resolve STATS across<br/>plugin-root candidates]
    LOCATE --> G_FOUND{engine script<br/>found on disk?}
    G_FOUND -- "no" --> HALT_MISSING([stop: codebase_stats.py<br/>not found])
    G_FOUND -- "resolved" --> G_REPO{root is a real git repo -<br/>git ls-files?}
    G_REPO -- "git command fails" --> HALT_REPO([stop: exit nonzero,<br/>never read as empty])
    G_REPO -- "yes" --> LOC[roll up LOC per<br/>top-2-level dir]
    LOC --> G_HEAD{HEAD exists -<br/>repo has a commit?}
    G_HEAD -- "unborn HEAD" --> SKIP_LOG[skip git log -<br/>co-change stays empty]
    G_HEAD -- "yes" --> COCHANGE[git log last N commits -<br/>count dir-pair co-occurrence]
    SKIP_LOG --> PRINT[print top-25 LOC +<br/>top-20 co-change pairs]
    COCHANGE --> PRINT
    PRINT --> READ[read co-change clusters as<br/>candidate chunks, refine with imports]

    subgraph DRAFT [draw boundaries - one chunk at a time]
        G_SIZE{chunk = one thing reasoned<br/>about in a single pass?}
        G_SIZE -- "no" --> SPLIT[split by responsibility,<br/>never mid-responsibility]
        SPLIT --> G_SIZE
        G_SIZE -- "yes" --> SEAMS[record exposes, consumes,<br/>couples_with]
        SEAMS --> G_LEAK{another chunk reaches a<br/>symbol not in exposes?}
        G_LEAK -- "leak found" --> SURFACE[surface it - fix the coupling,<br/>or deliberately widen exposes]
        SURFACE --> SEAMS
    end

    READ --> G_SIZE
    G_LEAK -- "clean" --> WRITE[write .chunkmap/map.md -<br/>reviewed_sha, confirmed false, notes]
    WRITE --> MAP_READY([resumable map<br/>ready to drill into])
    MAP_READY --> G_STALE{any file changed<br/>since reviewed_sha?}
    G_STALE -- "no" --> DRILL[drill into one chunk - read only<br/>its paths, consumes stays fixed]
    G_STALE -- "yes" --> RESTALE[diff --name-status -M sha HEAD -<br/>remap renames to old and new chunk]
    RESTALE --> WRITE
    DRILL --> STOP([bounded, reliable<br/>edit inside the chunk])
```

## Gate evidence

| Gate | Kind | Evidence |
|---|---|---|
| G_FOUND | agent | no eval covers this; SKILL.md's engine-resolution loop exits 1 when codebase_stats.py isn't found on any candidate path |
| G_REPO | code | `shared/skills/chunk-map/scripts/codebase_stats.py::def _git` - the check=True fatal exit, exercised by `shared/skills/chunk-map/scripts/codebase_stats.py::_self_test` |
| G_HEAD | code | `shared/skills/chunk-map/scripts/codebase_stats.py::has_head` |
| G_SIZE | agent | `evals/chunk-map/evals.json::Sizes by reasoning capacity / one coherent responsibility, NOT a fixed line count` |
| G_LEAK | agent | `evals/chunk-map/evals.json::SURFACES it (either fix the coupling, or add the symbol to exposes deliberately) rather than silently widening the seam` |
| G_STALE | agent | `evals/chunk-map/evals.json::Does NOT store an is_stale flag; derives staleness lazily at read time` |
