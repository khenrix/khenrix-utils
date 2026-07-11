"""SQLite ledger — sync state for wiki-add / wiki-sync.

Why SQLite (stdlib) and not a JSON file: the model is many-to-many. One canonical
URL can be saved in several bookmark folders and Instagram collections and arrive
from more than one channel, so identity has to separate *occurrences* (channel +
native id) from *pages* (one wiki file per canonical URL). Transactions give
crash-consistency and let the CLI serialize commits from parallel fetch workers
without hand-rolling a lock.

Tables:
  sync_run     — one enumeration; carries completeness so a partial run can't remove.
  source_item  — one saved occurrence; identity (channel, native_id).
  membership   — which folders/collections an occurrence belongs to.
  page         — one wiki file per canonical URL; owned by ≥1 source_item.
  capture      — metadata for a raw capture (bytes live in the capture cache).

The load-bearing invariant lives in `plan_diff`: only a `complete` snapshot may mark
items removable, and a page is removable only when ALL owning occurrences are inactive.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .sources import SourceItem

JOB_STATES = ("prepared", "fetched", "committed", "failed", "deferred")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_run(
  run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
  channel         TEXT NOT NULL,
  scope           TEXT,
  status          TEXT NOT NULL,
  cursor          TEXT,
  adapter_version INTEGER,
  started_at      TEXT,
  finished_at     TEXT
);
CREATE TABLE IF NOT EXISTS source_item(
  item_id       INTEGER PRIMARY KEY AUTOINCREMENT,
  channel       TEXT NOT NULL,
  native_id     TEXT NOT NULL,
  canonical_url TEXT NOT NULL,
  title         TEXT DEFAULT '',
  first_seen    TEXT,
  last_seen     TEXT,
  active        INTEGER NOT NULL DEFAULT 1,
  job_state     TEXT,
  UNIQUE(channel, native_id)
);
CREATE TABLE IF NOT EXISTS membership(
  item_id    INTEGER NOT NULL,
  collection TEXT NOT NULL,
  active     INTEGER NOT NULL DEFAULT 1,
  seen_run   INTEGER,
  UNIQUE(item_id, collection)
);
CREATE TABLE IF NOT EXISTS page(
  page_id        INTEGER PRIMARY KEY AUTOINCREMENT,
  path           TEXT NOT NULL,
  source_url     TEXT NOT NULL UNIQUE,
  generated_hash TEXT,
  schema_version INTEGER,
  status         TEXT DEFAULT 'current',
  created_at     TEXT,
  updated_at     TEXT
);
CREATE TABLE IF NOT EXISTS capture(
  capture_id       TEXT PRIMARY KEY,
  item_id          INTEGER,
  kind             TEXT,
  capture_hash     TEXT,
  fetcher_version  INTEGER,
  extractor_version INTEGER,
  taxonomy_version INTEGER,
  captured_at      TEXT,
  raw_path         TEXT
);
CREATE INDEX IF NOT EXISTS idx_item_channel ON source_item(channel);
CREATE INDEX IF NOT EXISTS idx_item_url ON source_item(canonical_url);
"""


@dataclass
class Diff:
    new: list = field(default_factory=list)        # in snapshot, unknown to ledger
    updated: list = field(default_factory=list)    # active in both
    reappeared: list = field(default_factory=list)  # was inactive, back in snapshot
    removable: list = field(default_factory=list)   # active, absent from COMPLETE snapshot


class Ledger:
    def __init__(self, db_path):
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        # WAL for crash-safety + concurrent readers (no-op harmless on :memory:).
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            pass
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # -- writes ------------------------------------------------------------- #
    def upsert_item(self, *, chan: str, native_id: str, url: str, title: str = "",
                    now: str = "") -> int:
        """Insert or reactivate a source occurrence; returns its item_id."""
        with self.conn:
            self.conn.execute(
                """INSERT INTO source_item(channel, native_id, canonical_url, title,
                                           first_seen, last_seen, active)
                   VALUES(?,?,?,?,?,?,1)
                   ON CONFLICT(channel, native_id) DO UPDATE SET
                     canonical_url=excluded.canonical_url,
                     title=CASE WHEN excluded.title != '' THEN excluded.title
                                ELSE source_item.title END,
                     last_seen=excluded.last_seen,
                     active=1""",
                (chan, native_id, url, title, now, now))
        return self._item_id(chan, native_id)

    def set_membership(self, item_id: int, collection: str, *, run_id=None) -> None:
        if not collection:
            return
        with self.conn:
            self.conn.execute(
                """INSERT INTO membership(item_id, collection, active, seen_run)
                   VALUES(?,?,1,?)
                   ON CONFLICT(item_id, collection) DO UPDATE SET active=1, seen_run=excluded.seen_run""",
                (item_id, collection, run_id))

    def observe(self, *, chan: str, native_id: str, url: str, collection: str = "",
                title: str = "", now: str = "") -> int:
        """Convenience: upsert an occurrence and record one membership."""
        iid = self.upsert_item(chan=chan, native_id=native_id, url=url, title=title, now=now)
        if collection:
            self.set_membership(iid, collection)
        return iid

    def deactivate_item(self, *, chan: str, native_id: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE source_item SET active=0 WHERE channel=? AND native_id=?",
                (chan, native_id))

    def record_page(self, *, path: str, source_url: str, generated_hash: str = "",
                    schema_version: int = 1, now: str = "") -> int:
        with self.conn:
            self.conn.execute(
                """INSERT INTO page(path, source_url, generated_hash, schema_version,
                                    status, created_at, updated_at)
                   VALUES(?,?,?,?, 'current', ?, ?)
                   ON CONFLICT(source_url) DO UPDATE SET
                     path=excluded.path, generated_hash=excluded.generated_hash,
                     schema_version=excluded.schema_version, updated_at=excluded.updated_at""",
                (path, source_url, generated_hash, schema_version, now, now))
        row = self.conn.execute("SELECT page_id FROM page WHERE source_url=?",
                                (source_url,)).fetchone()
        return int(row["page_id"])

    def mark_page_status(self, page_id: int, status: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE page SET status=? WHERE page_id=?", (status, page_id))

    def record_capture(self, *, capture_id: str, item_id: int, kind: str,
                       capture_hash: str, raw_path: str = "", fetcher_version: int = 1,
                       extractor_version: int = 1, taxonomy_version: int = 1,
                       now: str = "") -> None:
        with self.conn:
            self.conn.execute(
                """INSERT OR REPLACE INTO capture(capture_id, item_id, kind, capture_hash,
                     fetcher_version, extractor_version, taxonomy_version, captured_at, raw_path)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (capture_id, item_id, kind, capture_hash, fetcher_version,
                 extractor_version, taxonomy_version, now, raw_path))

    def job_transition(self, item_id: int, state: str) -> None:
        if state not in JOB_STATES:
            raise ValueError(f"invalid job state: {state!r} (valid: {JOB_STATES})")
        with self.conn:
            self.conn.execute("UPDATE source_item SET job_state=? WHERE item_id=?",
                              (state, item_id))

    # -- reads -------------------------------------------------------------- #
    def _item_id(self, chan: str, native_id: str) -> int:
        row = self.conn.execute(
            "SELECT item_id FROM source_item WHERE channel=? AND native_id=?",
            (chan, native_id)).fetchone()
        return int(row["item_id"]) if row else -1

    def item_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) c FROM source_item").fetchone()["c"])

    def memberships(self, native_id: str, chan: str | None = None) -> list[str]:
        q = ("SELECT m.collection FROM membership m JOIN source_item s ON s.item_id=m.item_id "
             "WHERE s.native_id=? AND m.active=1")
        args = [native_id]
        if chan is not None:
            q += " AND s.channel=?"
            args.append(chan)
        return [r["collection"] for r in self.conn.execute(q, args).fetchall()]

    def job_state(self, item_id: int) -> str | None:
        row = self.conn.execute("SELECT job_state FROM source_item WHERE item_id=?",
                                (item_id,)).fetchone()
        return row["job_state"] if row else None

    def find_page_by_url(self, source_url: str):
        return self.conn.execute("SELECT * FROM page WHERE source_url=?",
                                 (source_url,)).fetchone()

    def page_removable(self, page_id: int) -> bool:
        """A page is removable only when every source_item owning its URL is inactive."""
        row = self.conn.execute("SELECT source_url FROM page WHERE page_id=?",
                                (page_id,)).fetchone()
        if row is None:
            return False
        active = self.conn.execute(
            "SELECT COUNT(*) c FROM source_item WHERE canonical_url=? AND active=1",
            (row["source_url"],)).fetchone()["c"]
        return active == 0

    def plan_diff(self, snapshot) -> Diff:
        """Classify a snapshot against ledger state. `removable` is populated ONLY for a
        `complete` snapshot — a partial/failed/unavailable enumeration can add and update
        but never remove."""
        rows = self.conn.execute(
            "SELECT native_id, canonical_url, active FROM source_item WHERE channel=?",
            (snapshot.channel,)).fetchall()
        known = {r["native_id"]: r for r in rows}
        diff = Diff()
        seen = set()
        for it in snapshot.items:
            seen.add(it.native_id)
            row = known.get(it.native_id)
            if row is None:
                diff.new.append(it)
            elif not row["active"]:
                diff.reappeared.append(it)
            else:
                diff.updated.append(it)
        if snapshot.status == "complete":
            for nid, row in known.items():
                if row["active"] and nid not in seen:
                    diff.removable.append(SourceItem(native_id=nid,
                                                     canonical_url=row["canonical_url"]))
        return diff

    def close(self) -> None:
        self.conn.close()
