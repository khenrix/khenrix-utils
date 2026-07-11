"""Instagram live-enumeration normalizer — opt-in accelerator only.

The SKILL.md drives a chrome-devtools `evaluate_script` payload over the logged-in
saved page, collecting anchors matching `a[href*="/p/"]` / `a[href*="/reel/"]` (stable
selectors, NOT replayed private GraphQL). This module only NORMALIZES the resulting
JSON array into a snapshot. It never fabricates completeness: the caller passes the
`run_status` it actually observed (`complete` only when it hit a true end of the list;
`partial` otherwise), and a malformed or EMPTY array is `failed` — never an
authoritative "you saved nothing", which would wrongly mark everything removed.
"""
from __future__ import annotations

from ..canonurl import canonicalize
from . import Snapshot, SourceItem

CHANNEL = "instagram-saved"


def normalize_live(json_array, run_status: str) -> Snapshot:
    if not isinstance(json_array, list) or not json_array:
        return Snapshot(channel=CHANNEL, scope="saved", status="failed",
                        errors=["live enumeration returned no usable array"])
    status = run_status if run_status in ("complete", "partial") else "partial"
    items = []
    for entry in json_array:
        if not isinstance(entry, dict):
            continue
        href = entry.get("href") or entry.get("url") or ""
        if not href:
            continue
        c = canonicalize(href)
        if not c.native_id:
            continue
        coll = entry.get("collection") or "All Posts"
        cap = (entry.get("caption") or "").strip().replace("\n", " ")
        items.append(SourceItem(
            native_id=c.native_id,
            canonical_url=c.canonical,
            original_url=c.original,
            title=cap[:80],
            collection=f"Instagram/Saved/{coll}",
            added_at="",
        ))
    if not items:
        return Snapshot(channel=CHANNEL, scope="saved", status="failed",
                        errors=["no recognizable post/reel links in live array"])
    return Snapshot(channel=CHANNEL, scope="saved", status=status, items=items)
