#!/usr/bin/env python3
"""expense-review engine — deterministic Supabase writes + split math for the one-by-one loop.

The SKILL.md (Claude) does the *reasoning* — which merchant, which category, how to split — and the
user interaction. This module does the idempotent DB writes so that logic stays out of prose and is
eval-testable. Stdlib only; imports the sibling `db.py` / `normalize.py`.

`python3 review.py --selftest` runs a full live round-trip (seed a `new` row → review it → ignore one
→ reopen → verify balance → clean up).
"""
import sys
from datetime import datetime, timezone

import db as _db


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


# ── reads ──────────────────────────────────────────────────────────────────────
def pending(database, limit=200):
    """The work queue: oldest unhandled transactions first."""
    return _db.unreviewed(database, limit=limit)


def category_id(database, slug):
    rows = database.select("category", {"slug": "eq." + slug, "select": "id", "limit": 1})
    return rows[0]["id"] if rows else None


# ── split math (materialized rows; payer absorbs the öre remainder) ─────────────
def even_split(shareable_minor, person_ids, payer_id):
    """Even split that sums EXACTLY to shareable_minor; the PAYER absorbs the öre remainder.
    Uses sign-aware integer math (divmod on the magnitude) so the remainder direction is correct for
    negative expenses too — plain `//` floors toward -inf and would hand the öre to the debtor."""
    n = len(person_ids)
    sign = -1 if shareable_minor < 0 else 1
    base, rem = divmod(abs(shareable_minor), n)
    shares = {pid: sign * base for pid in person_ids}
    shares[payer_id] = shares.get(payer_id, 0) + sign * rem      # payer takes the leftover öre
    return [(pid, shares[pid]) for pid in person_ids]


# ── writes ─────────────────────────────────────────────────────────────────────
def upsert_merchant(database, *, canonical_name, slug, default_category_id=None,
                    website=None, org_number=None):
    return database.upsert("merchant", {
        "canonical_name": canonical_name, "slug": slug,
        "default_category_id": default_category_id, "website": website, "org_number": org_number,
    }, on_conflict="slug")[0]


def upsert_location(database, *, merchant_id, name, city=None, address=None,
                    lat=None, lng=None, google_place_id=None):
    return database.insert("merchant_location", {
        "merchant_id": merchant_id, "name": name, "city": city, "address": address,
        "lat": lat, "lng": lng, "google_place_id": google_place_id,
    })[0]


def cache_alias(database, *, provider, normalized_descriptor, merchant_id,
                merchant_location_id=None, confidence=1.0):
    """Remember raw→merchant so this vendor is never re-researched (provider-scoped)."""
    return database.upsert("descriptor_alias", {
        "provider": provider, "normalized_descriptor": normalized_descriptor,
        "merchant_id": merchant_id, "merchant_location_id": merchant_location_id,
        "confidence": confidence,
    }, on_conflict="provider,normalized_descriptor")[0]


def commit(database, tx_id, *, shareable_minor, split_type, splits,
           merchant_id=None, merchant_location_id=None, category_id=None):
    """Finalize a transaction: set fields, replace split rows, mark reviewed.
    Invariant (caller-enforced): sum(amt for _, amt in splits) == shareable_minor."""
    if shareable_minor == 0 and splits:
        # a personal / zero-shareable row must carry NO split rows (else a bad zero-sum split hits the balance)
        raise ValueError("zero shareable (personal) must have no split rows")
    if splits:
        if sum(a for _, a in splits) != shareable_minor:
            raise ValueError(f"splits sum {sum(a for _, a in splits)} != shareable {shareable_minor}")
    elif shareable_minor:
        # nonzero shareable with no split rows would silently leave the balance empty — reject it
        raise ValueError(f"shareable {shareable_minor} requires split rows (use shareable 0 for personal)")
    # Single transactional RPC (migration 005) — update + replace-splits atomically, so a partial
    # failure can't leave a 'reviewed' row with zero/partial splits silently dropped from the balance.
    database.request("POST", "/rpc/review_commit", body={
        "p_tx": tx_id, "p_shareable": shareable_minor, "p_split_type": split_type,
        "p_merchant": merchant_id, "p_location": merchant_location_id, "p_category": category_id,
        "p_splits": [{"person_id": pid, "share_amount_minor": amt} for pid, amt in splits],
    }, prefer="return=minimal")


def mark_ignored(database, tx_id, *, is_transfer=False, kind=None):
    """Not an expense to split (internal transfer, refund noise, own-account movement)."""
    patch = {"review_status": "ignored", "reviewed_at": _now_iso(), "updated_at": _now_iso(),
             "is_transfer": is_transfer, "shareable_amount_minor": 0}
    if kind:
        patch["kind"] = kind
    database.delete("transaction_split", {"transaction_id": "eq." + tx_id})
    database.update("transaction", {"id": "eq." + tx_id}, patch)


def reopen(database, tx_id):
    """Correction flow: pull a reviewed/ignored row back into the queue to redo it."""
    database.delete("transaction_split", {"transaction_id": "eq." + tx_id})
    database.update("transaction", {"id": "eq." + tx_id},
                    {"review_status": "new", "reviewed_at": None})


# ── self-test: full review write-path against the live DB ───────────────────────
def _selftest():
    d = _db.PostgREST()
    cid, aid = _db.person_id(d, "Christoffer Henriksson"), _db.person_id(d, "Anna Knoph")
    acct = _db.account_by_slug(d, "swedbank")
    cat = category_id(d, "dagligvaror")
    assert cid and aid and acct and cat, "seed data missing"

    # seed a 'new' transaction (–100.01 SEK, odd öre to exercise rounding)
    tx = d.insert("transaction", {
        "account_id": acct["id"], "fingerprint": "_rev_selftest", "booked_date": "2026-01-03",
        "charged_amount_minor": -10001, "currency": "SEK", "raw_descriptor": "COOP NARA FINSPANG TEST",
        "normalized_descriptor": "COOP NÄRA FINSPÅNG", "review_status": "new",
    })[0]
    try:
        m = upsert_merchant(d, canonical_name="Coop", slug="_t_coop", default_category_id=cat)
        loc = upsert_location(d, merchant_id=m["id"], name="Coop Nära Finspång", city="Finspång")
        import enrich
        raw_desc = tx["raw_descriptor"]                                  # the ASCII bank descriptor
        norm = enrich.resolve_merchant(d, "swedbank", raw_desc)["parsed"]["normalized"]
        cache_alias(d, provider="swedbank", normalized_descriptor=norm,  # cache the EXACT key resolve uses
                    merchant_id=m["id"], merchant_location_id=loc["id"])
        splits = even_split(-10001, [cid, aid], payer_id=cid)        # payer absorbs öre
        assert sum(a for _, a in splits) == -10001, splits
        commit(d, tx["id"], shareable_minor=-10001, split_type="even", splits=splits,
               merchant_id=m["id"], merchant_location_id=loc["id"], category_id=cat)

        row = d.select("transaction", {"id": "eq." + tx["id"],
                                       "select": "review_status,category_id,shareable_amount_minor"})[0]
        assert row["review_status"] == "reviewed" and row["shareable_amount_minor"] == -10001, row
        bal = sum(r["owed_minor"] for r in d.select("v_balance", {"debtor_id": "eq." + aid}))
        assert bal == -5000, f"Anna owed {bal}, expected -5000 (debtor gets clean half; payer absorbs the öre)"
        # H4: a fresh resolve of the SAME raw descriptor must now hit the cache (the "free next time" promise)
        hit = enrich.resolve_merchant(d, "swedbank", raw_desc)
        assert hit["source"] == "cache" and hit["merchant_id"] == m["id"], hit
        print(f"OK  review+split: reviewed, Anna owes {bal} öre, alias cached")

        # H2: a refund of a shared purchase splits symmetrically — positive shareable, payer absorbs the öre
        refund = dict(even_split(10001, [cid, aid], payer_id=cid))
        assert sum(refund.values()) == 10001 and refund[aid] == 5000 and refund[cid] == 5001, refund

        reopen(d, tx["id"])
        assert d.select("transaction", {"id": "eq." + tx["id"], "select": "review_status"})[0]["review_status"] == "new"
        mark_ignored(d, tx["id"], is_transfer=True, kind="transfer")
        ig = d.select("transaction", {"id": "eq." + tx["id"], "select": "review_status,is_transfer"})[0]
        assert ig["review_status"] == "ignored" and ig["is_transfer"] is True
        print("OK  reopen + mark_ignored(transfer) work")
    finally:
        d.delete("transaction", {"fingerprint": "eq._rev_selftest"})
        d.delete("descriptor_alias", {"provider": "eq.swedbank", "normalized_descriptor": "eq.COOP NÄRA FINSPÅNG"})
        d.delete("merchant", {"slug": "eq._t_coop"})
    assert not d.select("merchant", {"slug": "eq._t_coop", "select": "id"}), "cleanup failed"
    print("OK  cleanup verified")
    d.close()


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        print(__doc__)
