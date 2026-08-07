# mikado-graph — flow

Naive attempt, revert on discovery: each break becomes a prerequisite node, not a
patched-around obstacle. The engine (`mikado.py`) refuses a malformed graph outright -
a bad fence, an invalid node, a dangling dependency, or a cycle - so the agent is never
routed to work a phantom or blocked node. Only ready leaves are ever attempted, on a
clean base, and each is re-implemented fresh rather than salvaged from the reverted
spike. Source: `shared/skills/mikado-graph/SKILL.md`; engine:
`shared/skills/mikado-graph/scripts/mikado.py`.

```mermaid
flowchart TD
    accTitle: mikado-graph flow
    accDescr: The agent sketches or factors a change into a dependency graph and runs the bundled engine, which refuses a malformed fence, an invalid node, a dangling dependency, or a cycle before ever classifying readiness. Only ready leaves are attempted, on a clean base; a break becomes a new prerequisite node and the attempt is reverted rather than pushed through. The loop repeats until the goal node itself is done.

    START([a change that keeps snowballing,<br/>or a branch too big to review]) --> G_MODE{proactive - before code -<br/>or reactive - branch sprawled?}
    G_MODE -- "proactive" --> SKETCH[sketch goal + prerequisites<br/>before touching code]
    G_MODE -- "reactive" --> FACTOR[read the sprawled diff - factor into<br/>nodes: what merges alone, what blocks]
    SKETCH --> WRITE_PLAN[write .mikado/plan.md:<br/>json graph + prose notes]
    FACTOR --> WRITE_PLAN
    WRITE_PLAN --> RUN_ENGINE[run mikado.py<br/>on the plan]

    subgraph ENGINE [mikado.py classifies the graph]
        G_PARSE{fenced json block<br/>present and valid?}
        G_PARSE -- "no" --> FIX_PARSE[fix plan.md - restore<br/>the fenced json block]
        G_PARSE -- "yes" --> G_VALID{validate: ids present + unique,<br/>status known, every dep names a real node?}
        G_VALID -- "no" --> FIX_INVALID[repoint the dangling dep, dedupe id,<br/>or fix status - never chase the phantom]
        G_VALID -- "yes" --> G_CYCLE{DFS revisits a node<br/>still on the stack?}
        G_CYCLE -- "yes" --> FIX_CYCLE[break the cycle - remove<br/>or repoint the mis-modeled edge]
        G_CYCLE -- "no" --> CLASSIFY[per undone node:<br/>all deps done? ready : blocked]
    end

    RUN_ENGINE --> G_PARSE
    FIX_PARSE --> WRITE_PLAN
    FIX_INVALID --> WRITE_PLAN
    FIX_CYCLE --> WRITE_PLAN
    CLASSIFY --> REPORT[print READY - any order -<br/>and BLOCKED with unmet deps]
    REPORT --> PICK[pick a READY node -<br/>never start a BLOCKED one]
    PICK --> ATTEMPT[attempt it fresh on a clean base,<br/>not the reverted spike]
    ATTEMPT --> G_BREAKS{breaks something, or<br/>needs something first?}
    G_BREAKS -- "yes" --> ADD_CHILD[add the missing thing as a new<br/>prerequisite - child of this node]
    ADD_CHILD --> REVERT[revert the attempt - keep the<br/>learning, not the half-done code]
    REVERT --> WRITE_PLAN
    G_BREAKS -- "no" --> MERGE[small, independently-mergeable<br/>commit - mark node done in plan.md]
    MERGE --> G_MORE{goal - root -<br/>node done yet?}
    G_MORE -- "no" --> RUN_ENGINE
    G_MORE -- "yes" --> DONE([big change shipped as safe,<br/>reviewed increments])
```

## Gate evidence

| Gate | Kind | Evidence |
|---|---|---|
| G_MODE | agent | no eval covers this; SKILL.md's `## Proactive vs reactive` section names the two entry points |
| G_PARSE | code | `shared/skills/mikado-graph/scripts/mikado.py::def parse_graph` |
| G_VALID | code | `shared/skills/mikado-graph/scripts/mikado.py::def validate` |
| G_CYCLE | code | `shared/skills/mikado-graph/scripts/mikado.py::def classify` - the cycle branch is exercised by its `shared/skills/mikado-graph/scripts/mikado.py::cycle detected` self-test case |
| G_BREAKS | agent | `evals/mikado-graph/evals.json::REVERTS the half-done attempt (the Mikado move) rather than pushing through the breakage` |
| G_MORE | agent | no eval covers this; SKILL.md's leaf-first loop - "Repeat to the root." |
