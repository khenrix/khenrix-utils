---
name: chunk-map
description: >-
  Decompose a codebase (or a large subtree) into reviewable "chunks" of ~200–800 LOC with
  explicit seams — the contracts each chunk exposes and consumes — so you can reason about,
  review, or refactor one piece at a time instead of holding the whole repo in context. Uses
  git temporal co-change (which files change together) as the PRIMARY boundary signal, catching
  hidden coupling the import graph misses, plus LOC/dir rollups. Writes a gitignored
  .chunkmap/map.md (a dependency graph + per-chunk notes) you can resume across sessions, and
  supports drilling into one chunk in isolation. Use when a codebase is too big to hold at once,
  before a large refactor, when onboarding to an unfamiliar repo, or to find natural module
  boundaries. Triggers: "map this codebase", "break this repo into chunks", "where are the
  natural module boundaries", "chunk analysis", "decompose this for review", "what changes
  together here".
allowed-tools: Bash, Read, Grep
---

# chunk-map

Partition a codebase into **chunks** — units small enough to hold in working memory at once
(~200–800 LOC), each with explicit **seams** (what it exposes, what it consumes). The map is a
thinking tool: review, refactor, or onboard one chunk at a time. The engine
(`scripts/codebase_stats.py`) gives you the raw signals (LOC rollups + git co-change); **you**
draw the boundaries and write the map.

## Sizing — by reasoning capacity, not file count
A chunk is "one thing you can fully reason about in a single pass." 200–800 LOC is the guide, not
a rule: a dense state machine may cap at 200; a flat list of DTOs can run to 800. Split when you
can no longer answer "what does this do, what does it expose, what does it depend on" without
re-reading. Never split mid-responsibility just to hit a line count.

## Boundaries — co-change first, imports second
The **#1 signal is git temporal co-change**: files that change together belong together, even when
no import links them (hidden coupling — a serializer and its schema, a flag and its three readers).
The import graph is the *second* signal; it misses exactly this. Get both:

```bash
# Locate the bundled engine — cwd is the target repo, not the plugin, so resolve $STATS first:
STATS=""
for c in \
  "${CLAUDE_PLUGIN_ROOT:-}/skills/chunk-map/scripts/codebase_stats.py" \
  "${PLUGIN_ROOT:-}/skills/chunk-map/scripts/codebase_stats.py" \
  "$HOME/.gemini/config/plugins/khenrix-utils/skills/chunk-map/scripts/codebase_stats.py"; do
  if [ -f "$c" ]; then STATS="$c"; break; fi
done
if [ -z "$STATS" ]; then echo "codebase_stats.py not found — is khenrix-utils installed?"; exit 1; fi
python3 "$STATS" --root . --commits 400
```

This prints LOC rolled up per top-2-level dir + the most-coupled dir pairs (co-change count). Read
co-change clusters as candidate chunks; use imports/dir structure to refine. A dir pair that
co-changes constantly but lives apart is a seam to make explicit (or a refactor target).

## The map — `.chunkmap/map.md` (gitignored, resumable)
Write a single file: YAML frontmatter holds the chunk graph; the body holds per-chunk notes.

```markdown
---
reviewed_sha: <git rev-parse HEAD at map time>
confirmed: false   # true only after a human signs off; headless runs leave false
chunks:
  - id: ingest
    paths: [hunter/backend/ingest]
    loc: 540
    exposes: [ingest_batch, _upsert_item]      # the seam OUT
    consumes: [db.withUser, normalize.city]    # the seam IN
    couples_with: [dedupe]                      # from co-change, even if no import
---
## ingest
What it does (2-3 lines). Non-obvious invariants. Open questions.
```

Add `.chunkmap/` to `.gitignore` (it's a per-developer working artifact, not shared state).

## Staleness — derive lazily, never write back
Don't store "is this stale". At read time, diff against the recorded sha:

```bash
git diff --name-status -M <reviewed_sha> HEAD
```

Any changed file maps back to its chunk(s) → those chunks are stale; re-derive just those. Use
`--name-status -M` (not bare `--name-only`): a rename prints `R<score>\t<old>\t<new>`, so you can
stale BOTH the source chunk (old path) and the destination chunk — `--name-only` shows only the new
path and silently leaves the source chunk looking current. A map is a snapshot of understanding at
`reviewed_sha`, not a live index.

## Seam-leak verification
A chunk's seam is a lie if other chunks reach *past* it into internals. After drafting boundaries,
grep for cross-chunk references to symbols a chunk does NOT list in `exposes`:

```bash
grep -rn "internal_symbol" --include=*.py path/to/other/chunk
```

A hit = either the seam is wrong (add the symbol to `exposes`) or there's a coupling to fix. Surface
leaks; don't silently widen the seam.

## Drill into one chunk
To work a single chunk in isolation: read only its `paths`, treat its `consumes` as fixed contracts
(don't open those chunks), and verify your changes don't add a new cross-seam reference. This is the
payoff — bounded context, reliable edits.

## Invariants
- The map is advisory + per-developer (gitignored); it never gates a build.
- `confirmed: false` whenever written without a human sign-off (e.g. a headless pass).
- Co-change is the primary boundary signal; record `couples_with` even when no import exists.
- Re-running is safe: it overwrites `.chunkmap/map.md` and re-stamps `reviewed_sha`.
