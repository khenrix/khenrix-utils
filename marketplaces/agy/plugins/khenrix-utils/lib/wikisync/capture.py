"""Content-addressed raw-capture cache.

The resync promise depends on this: pages can be *reprocessed* when extraction
improves without re-hitting the live source (which may be deleted, private, or
rate-limited). Each fetched artifact (caption/comments JSON, article HTML,
transcript, frame manifest) is stored keyed by its own SHA-256, so identical
content is written once. Raw bytes live here under the XDG state dir — never in the
vault, which auto-commits and would leak private comments / work URLs / copyrighted
text into git.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

# kind → file extension for the on-disk artifact (cosmetic; get() globs by stem).
_EXT = {
    "caption": "json", "comments": "json", "metadata": "json",
    "frames": "json", "transcript": "txt", "article": "html",
    "html": "html", "readme": "md",
}


@dataclass(frozen=True)
class Capture:
    capture_id: str        # "<item_id>/<kind>-<sha12>" — stable, locates the file
    capture_hash: str      # full sha256 of the payload
    raw_path: str          # absolute path to the cached bytes
    kind: str
    item_id: str


class CaptureStore:
    def __init__(self, state_dir):
        self.root = Path(state_dir) / "raw_cache"

    def _dir(self, item_id: str) -> Path:
        return self.root / str(item_id)

    def put(self, item_id: str, kind: str, payload: bytes) -> Capture:
        """Write payload content-addressed; identical content is a no-op re-write."""
        full = hashlib.sha256(payload).hexdigest()
        sha12 = full[:12]
        ext = _EXT.get(kind, "bin")
        d = self._dir(item_id)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{kind}-{sha12}.{ext}"
        if not path.exists():                      # content-addressed dedupe
            path.write_bytes(payload)
        return Capture(f"{item_id}/{kind}-{sha12}", full, str(path), kind, str(item_id))

    def get(self, capture_id: str) -> bytes | None:
        """Return the cached bytes for a capture_id, or None if absent."""
        if "/" not in capture_id:
            return None
        item_id, stem = capture_id.split("/", 1)
        matches = sorted(self._dir(item_id).glob(f"{stem}.*"))
        return matches[0].read_bytes() if matches else None

    def latest(self, item_id: str, kind: str) -> Capture | None:
        """Most recently written capture of `kind` for an item, or None."""
        d = self._dir(item_id)
        if not d.is_dir():
            return None
        matches = list(d.glob(f"{kind}-*.*"))
        if not matches:
            return None
        newest = max(matches, key=lambda p: p.stat().st_mtime)
        sha12 = newest.stem.split("-", 1)[1] if "-" in newest.stem else newest.stem
        return Capture(f"{item_id}/{kind}-{sha12}", "", str(newest), kind, str(item_id))
