"""Instagram saved-posts adapter — the default, zero-risk path.

Parses Meta's "Download your information" export (`saved_posts.json`), whose canonical
shape is `saved_saved_media[].string_map_data["Saved on"].{href, timestamp}` with the
author username in `title`. An export is authoritative and complete — no browser
automation, no ToS/account risk — so it yields a `complete` snapshot and removals are
honored. The live accelerator (instagram_live) is opt-in on top of this.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ..canonurl import canonicalize
from . import Snapshot, SourceItem, register

CHANNEL = "instagram-saved"


def _iso(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return ""


@register("instagram-export")
def read_export(path) -> Snapshot:
    p = Path(path)
    if not p.is_file():
        return Snapshot(channel=CHANNEL, scope="saved", status="unavailable",
                        errors=[f"instagram export not found: {p}"])
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError) as e:
        return Snapshot(channel=CHANNEL, scope="saved", status="failed", errors=[str(e)])

    items = []
    for entry in data.get("saved_saved_media", []) or []:
        smd = (entry.get("string_map_data") or {}).get("Saved on") or {}
        href = smd.get("href") or ""
        if not href:
            continue
        c = canonicalize(href)
        if not c.native_id:            # not a recognizable post/reel link
            continue
        items.append(SourceItem(
            native_id=c.native_id,
            canonical_url=c.canonical,
            original_url=c.original,
            title="",                  # author (entry['title']) captured at fetch time
            collection="Instagram/Saved",
            added_at=_iso(smd.get("timestamp")),
        ))
    return Snapshot(channel=CHANNEL, scope="saved", status="complete", items=items)
