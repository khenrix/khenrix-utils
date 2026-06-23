---
name: mikado-graph
description: >-
  Decompose a big, scary change into a dependency graph of small, independently-mergeable steps
  using the Mikado Method, so each commit is safe and reviewable instead of one giant tangled
  branch. The goal is the graph's root; prerequisites hang beneath it; only LEAF nodes (all their
  prerequisites already done) are actionable now. Persists a resumable .mikado/plan.md (a JSON graph
  block + per-node notes) that survives across sessions, and the engine (scripts/mikado.py) tells you
  which node is ready vs blocked. Works proactively (design the boundaries before you start) or
  reactively (split an existing too-big branch). Use before a large refactor/migration, when a change
  keeps snowballing, or when a branch has grown unmergeable. Triggers: "plan this refactor", "this
  change is too big", "break this into mergeable steps", "mikado", "what should I do first here",
  "split this branch", "dependency graph for this migration".
allowed-tools: Bash, Read, Edit
---

# mikado-graph

The Mikado Method, as a planning tool: instead of a heroic branch, you find the **prerequisites** of
a big change and do them leaf-first, each as its own safe, mergeable commit. The engine
(`scripts/mikado.py`) computes which nodes are ready; **you** discover prerequisites and re-implement
each node cleanly.

## The core move (revert-on-discovery)
1. State the **goal** (graph root). Attempt it naively.
2. It breaks something / needs something first → that's a **prerequisite**. Add it as a child of the
   node you were attempting. **Revert your attempt** (this is the Mikado move — you keep the *learning*,
   not the half-done code).
3. Recurse on each prerequisite until you reach **leaves** — changes with no unmet prerequisite, each
   small + independently mergeable + behavior-preserving (or a tiny, safe behavior change).
4. Implement leaves first, merge each, mark done; their parents become the new leaves. Repeat to the root.

**Re-implement, don't cherry-pick.** When a node becomes ready, implement it *fresh* on a clean base —
don't try to salvage the reverted exploratory code. The graph is the durable artifact; the spike code is not.

## The plan — `.mikado/plan.md` (gitignored, resumable)
One file: a fenced `json` block holds the graph (machine-readable); the prose below holds per-node notes.

````markdown
# Mikado: <goal>

```json
{
  "goal": "extract auth into its own module",
  "nodes": [
    {"id": "goal",        "status": "todo", "deps": ["move-session", "decouple-db"]},
    {"id": "move-session","status": "todo", "deps": ["decouple-db"]},
    {"id": "decouple-db", "status": "done", "deps": []}
  ]
}
```

## decouple-db
Why it's a prerequisite; how it was done; the PR link.
````

`status` ∈ `todo|in_progress|done`. Add `.mikado/` to `.gitignore` (per-developer working state).

## Find the next actionable node
A node is **ready** when its status isn't `done` and **every** dep is `done`. Don't eyeball it — ask the engine:

```bash
python3 scripts/mikado.py .mikado/plan.md
```

It prints READY (do these now, in any order — they're independent) and BLOCKED (with the unmet deps),
and errors on a cycle. Work only READY nodes; never start a node with an unmet prerequisite.

## Proactive vs reactive
- **Proactive** — before touching code, sketch the goal + prerequisites into the graph, then implement
  leaf-first. Boundaries are designed, not discovered painfully.
- **Reactive** — a branch already sprawled. Read its diff, factor it into the graph (what could merge
  on its own? what blocks what?), then re-implement each node cleanly on `main`, abandoning the sprawl.

## Invariants
- Only leaf/ready nodes are actionable; the graph enforces order.
- Each node is independently mergeable and (ideally) behavior-preserving — if a node can't merge alone,
  it has a hidden prerequisite; add it.
- Re-implement ready nodes fresh; the reverted spike is throwaway.
- `.mikado/plan.md` is gitignored, per-developer, resumable; re-running is safe.
