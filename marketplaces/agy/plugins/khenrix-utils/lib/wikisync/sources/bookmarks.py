"""Chrome bookmarks adapter — reads the live profile JSON directly.

No manual HTML export: the adapter reads Chrome's on-disk `Bookmarks` JSON straight
from the WSL filesystem. Each url node is one occurrence (identity = its Chrome GUID),
carrying the full folder path as its collection. A URL saved in two folders is two
GUID nodes with the same canonical_url — the ledger collapses them to one page.

A clean read is always `complete` (the file is authoritative and local), so bookmark
removals are honored. The stable-read guards the rare case where Chrome rewrites the
file mid-read.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ..canonurl import canonicalize
from . import Snapshot, SourceItem, register

CHANNEL = "chrome-bookmarks"
_WEBKIT_EPOCH_OFFSET = 11_644_473_600  # seconds between 1601-01-01 and 1970-01-01


def _webkit_to_iso(date_added: str) -> str:
    """Chrome stores date_added as microseconds since 1601-01-01 UTC. '0'/'' → ''."""
    try:
        micros = int(date_added)
    except (TypeError, ValueError):
        return ""
    if micros <= 0:
        return ""
    unix = micros / 1_000_000 - _WEBKIT_EPOCH_OFFSET
    if unix <= 0:
        return ""
    return datetime.fromtimestamp(unix, tz=timezone.utc).isoformat()


def _stable_read(path: Path, attempts: int = 4) -> dict:
    """Read the whole file, verifying the stat didn't change under us; retry a JSON
    decode if Chrome replaced the file concurrently."""
    last_err = None
    for _ in range(attempts):
        try:
            st1 = path.stat()
            raw = path.read_bytes()
            st2 = path.stat()
            if (st1.st_mtime_ns, st1.st_size) != (st2.st_mtime_ns, st2.st_size):
                continue  # changed mid-read; retry
            return json.loads(raw)
        except (json.JSONDecodeError, OSError) as e:  # concurrent replace / transient
            last_err = e
            continue
    raise RuntimeError(f"could not stably read {path}: {last_err}")


def _walk(node: dict, path_parts: list[str]):
    """Yield (folder_path, url_node) for every url node under `node`."""
    ntype = node.get("type")
    if ntype == "folder":
        here = path_parts + [node.get("name", "")] if node.get("name") else path_parts
        for child in node.get("children", []):
            yield from _walk(child, here)
    elif ntype == "url":
        yield "/".join(p for p in path_parts if p), node


@register(CHANNEL)
def read_bookmarks(path) -> Snapshot:
    p = Path(path)
    if not p.is_file():
        return Snapshot(channel=CHANNEL, scope="all", status="unavailable",
                        errors=[f"bookmarks file not found: {p}"])
    try:
        data = _stable_read(p)
    except RuntimeError as e:
        return Snapshot(channel=CHANNEL, scope="all", status="failed", errors=[str(e)])

    items = []
    for root in (data.get("roots") or {}).values():
        if not isinstance(root, dict):
            continue
        for folder, node in _walk(root, []):
            url = node.get("url") or ""
            if not url:
                continue
            c = canonicalize(url)
            items.append(SourceItem(
                native_id=node.get("guid") or c.canonical,   # GUID identity; URL fallback
                canonical_url=c.canonical,
                original_url=c.original,
                title=node.get("name", ""),
                collection=folder,
                added_at=_webkit_to_iso(node.get("date_added", "")),
            ))
    return Snapshot(channel=CHANNEL, scope="all", status="complete", items=items)
