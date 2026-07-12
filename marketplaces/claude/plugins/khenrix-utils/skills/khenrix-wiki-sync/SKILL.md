---
name: khenrix-wiki-sync
description: >-
  Batch-reconcile saved content into the Obsidian wiki from every enabled source — Chrome
  bookmarks (read directly from the live profile, no export) and Instagram saved posts (via
  Meta's official export by default, or an opt-in capped live browser pass) — and, in future,
  more sources. Enumerates each source into a completeness-aware snapshot, diffs it against the
  SQLite ledger, ingests new items through the wiki-add pipeline, runs a capped deep pass for
  videos whose recipe wasn't in the caption, and reports created/updated/deferred/removed —
  never deleting a page. Also resyncs: re-render every page from cached captures when extraction
  improves. Use when the user says "sync my wiki", "import my bookmarks / Instagram saves",
  "resync the wiki", "pull in my saved posts", or wants the whole saved corpus filed at once.
  For a single link use khenrix-wiki-add.
allowed-tools: Bash, Read, WebFetch
---

# khenrix-wiki-sync — reconcile all saved sources into the wiki

Enumerate every enabled source, diff against the ledger, and ingest what's new. The
deterministic `wikisync` engine owns enumeration, the completeness-aware diff, rendering, and
state; you own the per-item fetch/extract (via the wiki-add flow) and, for Instagram-live, the
browser enumeration. Nothing is ever deleted — a source removal is reported, not applied.

## 1. Locate the engine + probe

```bash
WS=""
for c in "${CLAUDE_PLUGIN_ROOT:-}/lib" "${PLUGIN_ROOT:-}/lib" \
         "$HOME/.gemini/config/plugins/khenrix-utils/lib" \
         "$HOME/git/khenrix-utils/shared/lib"; do
  [ -d "$c/wikisync" ] && WS="$c" && break
done
[ -z "$WS" ] && echo "wikisync engine not found" && exit 1
wk() { PYTHONPATH="$WS" python3 -m wikisync "$@"; }
wk probe          # {bookmarks, instagram_export, instagram_live, watch, wiki_plugin}
```

Sync only the sources `probe` says are available. A source you can't reach is **deferred**,
never a silent empty success.

## 2. First run: adopt existing pages

Before the first sync, adopt any wiki pages that already have a `source_url` so they join the
ledger instead of being duplicated (e.g. the hand-made Bò lá lốt recipe):

```bash
wk adopt --path "$HOME/git/obsidian-vault/wiki/sources/bo-la-lot-vietnamese-grilled-beef.md"
```

## 3. Enumerate + plan each source

`wk plan` reads a snapshot, diffs the ledger, records new items as `prepared`, and returns the
job list. It marks removals only from a **complete** snapshot — a truncated/failed enumeration
never removes anything.

### Chrome bookmarks (all CLIs)

```bash
wk plan --channel chrome-bookmarks > /tmp/wk-bm-plan.json
```

Self-serves from the live profile JSON in config. Output: `{jobs[], removable[], deferred}`.

### Instagram — export path (default, zero account risk)

If the user has an unzipped Meta "Download your information" export configured
(`instagram_export_dir`):

```bash
wk plan --channel instagram-export > /tmp/wk-ig-plan.json
```

### Instagram — live accelerator (opt-in, Claude only)

Only when `instagram_live` is true AND the user opted in. **Meta's terms restrict automated
collection even while logged in** — so this pass is conservative and stops at the first sign of
friction. Open `https://www.instagram.com/<user>/saved/all-posts/` with chrome-devtools, then
scroll-and-collect with `evaluate_script` using stable anchor selectors (never replayed private
GraphQL):

```js
() => {
  const seen = new Map();
  document.querySelectorAll('a[href*="/p/"], a[href*="/reel/"]').forEach(a => {
    const href = a.href.split('?')[0];
    seen.set(href, {href, collection: "All Posts"});
  });
  return JSON.stringify([...seen.values()]);
}
```

Loop: collect → scroll one viewport → wait a **randomized 1.5–4 s** → collect again. Stop when
either (a) no new hrefs appear across 3 consecutive scrolls **and** the page shows the end of the
list → the run is **complete**; or (b) you hit any login/challenge/verification screen, a 429, or
a rate-limit notice → **stop immediately, do not retry or evade**, and mark the run **partial**.
Optionally stop early once you match a run of already-ledgered shortcodes (incremental sync).

Write the collected array into a snapshot file and plan from it — pass the honest run status:

```bash
cat > /tmp/wk-ig-live.json <<JSON
{"channel":"instagram-saved","scope":"all","status":"partial",
 "items":[ {"native_id":"...","canonical_url":"https://www.instagram.com/reel/.../","collection":"Instagram/Saved/All Posts"} ]}
JSON
wk plan --channel instagram-live --snapshot-file /tmp/wk-ig-live.json > /tmp/wk-ig-plan.json
```

(`status` is `complete` only if you truly reached the end; otherwise `partial` so nothing is
removed on a short scroll.)

## 4. Ingest the jobs (standard pass)

For each job in a plan's `jobs[]`, run the **wiki-add** flow: fetch the item by kind, build the
extraction JSON (with raw `captures[]`), and `wk commit --job @file`. See the `khenrix-wiki-add`
skill for the per-item procedure and the extraction schema.

- **Parallelize the FETCH, serialize the COMMIT.** Fetching many pages at once is fine; commits
  go through the engine one at a time (single-writer SQLite + vault lock). Don't run concurrent
  `wk commit` calls.
- **Volume:** for a large backfill, dispatch a batch of fetch subagents, collect their extraction
  JSONs, then commit them sequentially. Cap standard-pass fetches per host (`per_host_cap`) so you
  don't hammer one site.
- **Bot-blocked ≠ deferred.** Lightweight fetchers (WebFetch/markitdown) are 403/503-blocked by
  many real recipe sites (Serious Eats, Simply Recipes, AllRecipes, BBC Good Food, food.com, …).
  A subagent should mark those `deferred: bot-blocked` (a distinct reason), NOT `unavailable`.
  After the batch, collect the `bot-blocked` items and re-fetch them on Claude through the
  **chrome-devtools browser fallback** (see khenrix-wiki-add §4) before they stay deferred — the
  browser renders past the block. For items that are genuinely dead (404/dead-DNS/SSL), try the
  **Wayback Machine** next (archive.org availability → browser fetch of the `id_` snapshot; see
  khenrix-wiki-add §4). Only what has no snapshot — and auth-gated dashboards — stays deferred.
- Update the wiki index / `log.md` / `hot.md` **once per batch**, not per item.

## 5. Deep pass (capped)

Some recipe reels have no recipe in the caption. Those jobs come back with
`target_capabilities` lacking `transcript`. Collect them into a review queue and run the deep
capture (`/watch` → frames + transcript) on at most `deep_cap` per run (default 10); commit
those, and leave the rest queued in the ledger for a later run. Deep is costly — don't run it on
everything.

## 6. Removals + report

- `removable[]` from a **complete** plan lists items no longer in the source. **Never delete a
  page.** Report them; the ledger keeps them. (Pages persist as the durable record; a removed
  save just means it left your source list.)
- Finish with `wk report` → job-state counts (committed / prepared / deferred / failed). Give the
  user a one-paragraph summary: how many pages created/updated, how many deep-queued, what was
  deferred (and why), and any source that failed enumeration.

## 7. Resync (when fetch/extraction improves)

When the renderer or taxonomy improves, re-render every page from its **cached** capture — no
network, no re-fetch:

```bash
wk reprocess --now "$(date -u +%Y-%m-%dT%H:%M:%SZ)"      # or --only-stale for taxonomy bumps
```

Hand-written notes outside the managed region survive. When the *extractor* (the LLM edge)
improves, re-run the fetch/extract for the affected items instead (a fresh capture), then commit.

## Cross-CLI summary

| Source                     | Claude | Codex / agy |
|----------------------------|:------:|:-----------:|
| Chrome bookmarks           | ✅ | ✅ |
| Instagram export           | ✅ | ✅ (if export configured) |
| Instagram live accelerator | ✅ opt-in | ❌ deferred |
| Deep video (`/watch`)      | ✅ | ❌ (queued) |
| commit / reprocess / report| ✅ | ✅ |

On a CLI missing a capability, sync the sources you can and report the rest as
`deferred: capability_unavailable`.
