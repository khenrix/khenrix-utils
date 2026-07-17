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
allowed-tools: Bash, Read, Edit, Write
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
# Locate the bundled engine — cwd is the target repo, not the plugin, so resolve $MIKADO first:
MIKADO=""
for c in \
  "${CLAUDE_PLUGIN_ROOT:-}/skills/mikado-graph/scripts/mikado.py" \
  "${PLUGIN_ROOT:-}/skills/mikado-graph/scripts/mikado.py" \
  "$HOME/git/khenrix-utils/shared/skills/mikado-graph/scripts/mikado.py" \
  "$HOME/.gemini/config/plugins/khenrix-utils/skills/mikado-graph/scripts/mikado.py"; do
  if [ -f "$c" ]; then MIKADO="$c"; break; fi
done
# Codex sets no PLUGIN_ROOT for a skill's Bash calls. If nothing above matched, fall back to the
# Codex plugin cache — taking the NEWEST version. The sort key is the path AFTER `khenrix-utils/`
# (i.e. "<version>/skills/…"), so sort -V orders by the version segment itself, not by the
# cache-parent wildcard that precedes it — deterministic across multiple cache parents.
# (The repo-dev source-of-truth above already wins on a dev box, so a stale cache can't shadow it.)
if [ -z "$MIKADO" ]; then
  MIKADO=$(for f in "$HOME"/.codex/plugins/cache/*/khenrix-utils/*/skills/mikado-graph/scripts/mikado.py; do
    [ -e "$f" ] && printf '%s\t%s\n' "${f#*/khenrix-utils/}" "$f"
  done | sort -V | tail -1 | cut -f2-)
fi
if [ -z "$MIKADO" ]; then echo "mikado.py not found — is khenrix-utils installed?"; exit 1; fi
python3 "$MIKADO" .mikado/plan.md
```

It prints READY (do these now, in any order — they're independent) and BLOCKED (with the unmet deps).
It **refuses the plan with a nonzero error** on a malformed graph — a cycle, a dep naming an
**undefined node** (a dangling reference, almost always a rename typo — not a real prerequisite to go
do), a duplicate id, or an unknown `status` value. Fix the graph; don't act on the phantom node.
Work only READY nodes; never start a node with an unmet prerequisite.

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
