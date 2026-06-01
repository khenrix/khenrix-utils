#!/usr/bin/env python3
"""Stdlib-only PostgREST client for the expense skills (no pip deps).

Reads SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY from ~/.config/khenrix-utils/expenses.env
(process env overrides the file). Never prints secrets. Uses one keep-alive HTTPS
connection (urllib opens a fresh TLS handshake per call, which is slow over many rows).

Run `python3 db.py --selftest` for an end-to-end round-trip against the live DB.
"""
import http.client
import json
import os
import ssl
import sys
import urllib.parse
from pathlib import Path

ENV_PATH = Path.home() / ".config" / "khenrix-utils" / "expenses.env"
# EXPENSES_-prefixed names take precedence so they don't collide with other Supabase projects' keys
# in the user's shell; the unprefixed names remain as fallbacks.
_ENV_KEYS = ("EXPENSES_SUPABASE_URL", "SUPABASE_URL",
             "EXPENSES_SUPABASE_SECRET_KEY", "SUPABASE_SECRET_KEY", "SUPABASE_SERVICE_ROLE_KEY",
             "EXPENSES_SUPABASE_PUBLISHABLE_KEY", "SUPABASE_PUBLISHABLE_KEY",
             "EXPENSES_GOOGLE_PLACES_API_KEY", "GOOGLE_PLACES_API_KEY")


def load_env():
    cfg = {}
    if ENV_PATH.exists():
        if ENV_PATH.stat().st_mode & 0o077:   # service key must not be group/world readable
            raise SystemExit(f"{ENV_PATH} is group/world-accessible; it holds a service key. "
                             f"Run: chmod 600 {ENV_PATH}")
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip('"').strip("'")
    for k in _ENV_KEYS:                       # process env wins
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    return cfg


class PostgRESTError(RuntimeError):
    """Carries the PostgREST/Postgres error body (status + detail) instead of a bare HTTP code."""

    def __init__(self, status, method, path, detail):
        self.status, self.method, self.path, self.detail = status, method, path, detail
        super().__init__(f"PostgREST {status} on {method} {path}: {detail}")


class PostgREST:
    def __init__(self, cfg=None):
        cfg = cfg or load_env()
        url = cfg.get("EXPENSES_SUPABASE_URL") or cfg.get("SUPABASE_URL")
        # Prefer the namespaced EXPENSES_ secret; new-format sb_secret_ key, then legacy service_role JWT.
        self.key = (cfg.get("EXPENSES_SUPABASE_SECRET_KEY") or cfg.get("SUPABASE_SECRET_KEY")
                    or cfg.get("SUPABASE_SERVICE_ROLE_KEY"))
        if not url or not self.key:
            raise SystemExit(
                "Missing SUPABASE_URL / EXPENSES_SUPABASE_SECRET_KEY "
                f"(set them in {ENV_PATH} or the environment)."
            )
        self.host = urllib.parse.urlparse(url).netloc
        self.base = "/rest/v1"
        self._ctx = ssl.create_default_context()
        self._conn = None

    # ── transport ──────────────────────────────────────────────────────────────
    def _connection(self):
        if self._conn is None:
            self._conn = http.client.HTTPSConnection(self.host, timeout=30, context=self._ctx)
        return self._conn

    def _headers(self, prefer=None):
        h = {
            "apikey": self.key,
            "Authorization": "Bearer " + self.key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if prefer:
            h["Prefer"] = prefer
        return h

    def request(self, method, path, params=None, body=None, prefer=None):
        query = ("?" + urllib.parse.urlencode(params, doseq=True)) if params else ""
        url = self.base + path + query
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = self._headers(prefer)
        attempts = 2 if method == "GET" else 1  # retry only idempotent GETs — never re-send a POST/PATCH/DELETE
        for attempt in range(1, attempts + 1):
            conn = self._connection()
            try:
                conn.request(method, url, body=data, headers=headers)
                resp = conn.getresponse()
                raw = resp.read()
                break
            except (http.client.HTTPException, OSError):
                try:
                    conn.close()
                finally:
                    self._conn = None
                if attempt == attempts:
                    raise
        if resp.status >= 400:                 # PostgREST puts the DB error in the body — surface it
            raise PostgRESTError(resp.status, method, path, raw.decode("utf-8", "replace"))
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw.decode("utf-8", "replace")

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    # ── thin verbs ─────────────────────────────────────────────────────────────
    def select(self, table, params=None):
        return self.request("GET", "/" + table, params=params)

    def insert(self, table, rows, prefer="return=representation"):
        return self.request("POST", "/" + table, body=rows, prefer=prefer)

    def upsert(self, table, rows, on_conflict, prefer="resolution=merge-duplicates,return=representation"):
        return self.request("POST", "/" + table, params={"on_conflict": on_conflict}, body=rows, prefer=prefer)

    def update(self, table, params, patch, prefer="return=representation"):
        return self.request("PATCH", "/" + table, params=params, body=patch, prefer=prefer)

    def delete(self, table, params):
        return self.request("DELETE", "/" + table, params=params)


# ── domain helpers (shared by both skills) ─────────────────────────────────────
def person_id(db, name):
    rows = db.select("person", {"name": "eq." + name, "select": "id", "limit": 1})
    return rows[0]["id"] if rows else None


def account_by_slug(db, slug):
    rows = db.select("account", {"slug": "eq." + slug, "limit": 1})
    return rows[0] if rows else None


def get_last_tx_date(db, account_slug):
    """Default fetch window anchor: the most recent booked_date stored for an account."""
    acct = account_by_slug(db, account_slug)
    if not acct:
        return None
    rows = db.select("transaction", {
        "account_id": "eq." + acct["id"],
        "select": "booked_date",
        "order": "booked_date.desc.nullslast",
        "limit": 1,
    })
    return rows[0]["booked_date"] if rows else None


def find_alias(db, provider, normalized_descriptor):
    """Local-first merchant resolution cache lookup (provider-scoped)."""
    rows = db.select("descriptor_alias", {
        "provider": "eq." + provider,
        "normalized_descriptor": "eq." + normalized_descriptor,
        "select": "merchant_id,merchant_location_id,confidence",
        "limit": 1,
    })
    return rows[0] if rows else None


def unreviewed(db, limit=200):
    """The expense-review work queue: oldest unhandled transactions first."""
    return db.select("transaction", {
        "review_status": "eq.new",
        "booking_status": "neq.pending",   # don't review a pending row — wait until it books (it may re-settle)
        "order": "booked_date.asc.nullslast,observed_first_at.asc",
        "limit": limit,
    })


# ── self-test: live round-trip proving the whole DB path with no browser ───────
def _selftest():
    db = PostgREST()
    cid, aid = person_id(db, "Christoffer Henriksson"), person_id(db, "Anna Knoph")
    acct = account_by_slug(db, "swedbank")
    assert cid and aid and acct, "seed data missing — apply 002_seed.sql"

    merch = db.insert("merchant", {"canonical_name": "Selftest AB", "slug": "_selftest"})[0]
    try:
        tx = db.insert("transaction", {
            "account_id": acct["id"],
            "fingerprint": "_selftest_fp",
            "booked_date": "2026-01-01",
            "value_date": "2026-01-01",
            "charged_amount_minor": -10000,        # -100.00 SEK expense
            "currency": "SEK",
            "raw_descriptor": "SELFTEST ROUNDTRIP",
            "normalized_descriptor": "SELFTEST",
            "merchant_id": merch["id"],
            "shareable_amount_minor": -10000,
            "split_type": "even",
            "review_status": "reviewed",
        })[0]
        db.insert("transaction_split", [
            {"transaction_id": tx["id"], "person_id": cid, "share_amount_minor": -5000},
            {"transaction_id": tx["id"], "person_id": aid, "share_amount_minor": -5000},
        ])
        bal = db.select("v_balance", {"debtor_id": "eq." + aid})
        owed = sum(r["owed_minor"] for r in bal)
        assert owed == -5000, f"expected Anna owed_minor -5000, got {owed} ({bal})"
        print(f"OK  round-trip: merchant+tx+2 splits inserted; v_balance Anna={owed} öre")
    finally:
        db.delete("transaction", {"fingerprint": "eq._selftest_fp"})   # cascades splits
        db.delete("merchant", {"slug": "eq._selftest"})
    leftover = db.select("merchant", {"slug": "eq._selftest", "select": "id"})
    assert not leftover, "cleanup failed"
    print("OK  cleanup verified — DB path is healthy")
    db.close()


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        print(__doc__)
