"""Source adapters + the normalized snapshot envelope every adapter emits.

The envelope is the contract between fetch (adapters) and state (ledger). Crucially
it carries a completeness `status`: a `partial` enumeration (a truncated Instagram
scroll, an unavailable channel on a non-Claude CLI) may add or update observations
but must NEVER cause the ledger to mark anything removed. Only a `complete` snapshot
is authoritative about absence. The ledger enforces this in `plan_diff`.
"""
from __future__ import annotations

from dataclasses import dataclass, field

VALID_SNAPSHOT_STATUS = ("complete", "partial", "failed", "unavailable")


@dataclass(frozen=True)
class SourceItem:
    """One saved occurrence: a bookmark node or an Instagram shortcode. Identity is
    (channel, native_id) — NOT the URL, which can appear in many folders/collections."""
    native_id: str
    canonical_url: str
    original_url: str = ""
    title: str = ""
    collection: str = ""      # one folder path / IG collection name ("" = none)
    added_at: str = ""        # ISO-8601 or ""


@dataclass
class Snapshot:
    """A single enumeration of one source channel + scope."""
    channel: str
    scope: str
    status: str               # complete | partial | failed | unavailable
    items: list = field(default_factory=list)
    cursor: str = ""
    adapter_version: int = 1
    errors: list = field(default_factory=list)

    def __post_init__(self):
        if self.status not in VALID_SNAPSHOT_STATUS:
            raise ValueError(f"invalid snapshot status: {self.status!r}")


# --- adapter registry (adapters register themselves; the CLI looks them up) ------
_ADAPTERS: dict = {}


def register(channel: str):
    def deco(fn):
        _ADAPTERS[channel] = fn
        return fn
    return deco


def get_adapter(channel: str):
    return _ADAPTERS.get(channel)


def adapters() -> dict:
    return dict(_ADAPTERS)
