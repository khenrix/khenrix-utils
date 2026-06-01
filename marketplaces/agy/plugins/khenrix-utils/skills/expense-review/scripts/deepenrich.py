#!/usr/bin/env python3
"""Merchant deep-enrichment for expense-review — match a merchant's own orders (Amazon/Google/PayPal …)
to the card charge(s) they produced, and store the line items. Stdlib only.

Two halves:
  • a PURE, conservative matcher (`match_orders_to_charges`) — links ONLY unambiguous matches, flags the
    rest for manual (same stance as fetch.reconcile_wint). Handles Amazon's reality where one order ships +
    charges in pieces (one order → N charges) via per-shipment exact match, falling back to an order-total
    subset-sum over the date window.
  • thin DB write helpers (`upsert_order` / `set_lines` / `link_charge` / `attach`) writing the
    merchant_order + merchant_order_line + order_charge_link tables (migration 010). Reference data → review
    still owns categorize/split; commit the linked charge with enrichment_source='merchant' (migration 009).

The per-merchant SCRAPE adapters (browser, human login) live in the SKILL — this module is the deterministic,
eval-testable core they feed. `python3 deepenrich.py --selftest` runs the matcher fixtures (no network/DB).
"""
import sys
from datetime import date

# Merchant → the source key + descriptor/captured-name substrings that mark a row as deep-enrichable.
# Order/amounts elsewhere are POSITIVE minor units; `transaction.charged_amount_minor` is negative.
REGISTRY = {
    "amazon":      ["AMAZON", "AMZN"],
    "paypal":      ["PAYPAL", "PP*", "PAYPAL *"],
    "google-play": ["GOOGLE *GOOGLE PLAY", "GOOGLE PLAY"],
    "google-pay":  ["GOOGLE PAY", "GOOGLE *"],          # last-resort: only the bare/opaque Google wrapper
    "apple":       ["APPLE.COM/BILL", "ITUNES", "APPLE.COM BILL"],
    "klarna":      ["KLARNA"],
}


def classify(text):
    """Which deep-enrich source a descriptor / captured merchant name belongs to, or None. More-specific
    keys win (google-play before google-pay) so 'GOOGLE *GOOGLE PLAY' isn't swallowed by the bare wrapper."""
    s = (text or "").upper()
    order = ["amazon", "paypal", "apple", "klarna", "google-play", "google-pay"]
    for src in order:
        if any(pat in s for pat in REGISTRY[src]):
            return src
    return None


# ── pure matcher ────────────────────────────────────────────────────────────────
def _within(charge_date, target_date, window_days):
    if not target_date:
        return True
    try:
        a = date.fromisoformat(str(charge_date)[:10]); b = date.fromisoformat(str(target_date)[:10])
    except ValueError:
        return False
    return abs((a - b).days) <= window_days


def _amount_candidates(charges, target_minor, target_date, window_days, claimed):
    """Charges whose MAGNITUDE equals target_minor, within the date window, not already claimed."""
    return [c for c in charges if c["id"] not in claimed
            and abs(c["charged_amount_minor"]) == target_minor
            and _within(c.get("booked_date"), target_date, window_days)]


def _subset_sum(charges, target_minor, max_size=4):
    """The UNIQUE subset of charges summing (by magnitude) to target_minor → list; 'ambiguous' if >1 such
    subset; None if none. Bounded by max_size to stay cheap on a day's pool."""
    import itertools
    found = []
    for k in range(1, min(max_size, len(charges)) + 1):
        for combo in itertools.combinations(charges, k):
            if sum(abs(c["charged_amount_minor"]) for c in combo) == target_minor:
                found.append(combo)
                if len(found) > 1:
                    return "ambiguous"
    return list(found[0]) if len(found) == 1 else None


def match_orders_to_charges(orders, charges, window_days=3):
    """orders: [{external_order_id, order_date, total_minor(+), shipments?:[{amount_minor(+), date?}]}].
    charges: [{id, booked_date, charged_amount_minor(-)}] — unenriched candidates for this merchant.
    Returns {links:[{order_id, transaction_id, amount_minor, confidence, match}], ambiguous:[...], unmatched:[...]}.
    Conservative: a charge is never reused across orders; anything not uniquely resolved is flagged, not linked."""
    from collections import Counter
    claimed, links, ambiguous, unmatched = set(), [], [], []
    for o in orders:
        oid, odate = o.get("external_order_id"), o.get("order_date")
        matched, match_kind = [], None

        if o.get("shipments"):                                  # prefer per-shipment exact match (by amount multiset)
            want = Counter(abs(s["amount_minor"]) for s in o["shipments"])
            chosen, status = [], "ok"
            for amt, m in want.items():
                cand = _amount_candidates(charges, amt, odate, window_days, claimed | {c["id"] for c in chosen})
                if len(cand) == m:
                    chosen.extend(cand)                         # forced 1:1 — interchangeable equals are fine
                elif len(cand) > m:
                    status = "ambiguous"; break                 # more equal charges than shipments → which ones?
                else:
                    status = "short"; break                     # not enough → let order-total subset-sum try
            if status == "ambiguous":
                ambiguous.append({"order_id": oid, "reason": "more equal-amount charges than shipments"})
                continue
            if status == "ok" and chosen:
                matched, match_kind = chosen, "shipment"

        if not matched and o.get("total_minor"):                # fall back to order-total subset-sum
            pool = [c for c in charges if c["id"] not in claimed and _within(c.get("booked_date"), odate, window_days)]
            res = _subset_sum(pool, abs(o["total_minor"]))
            if res == "ambiguous":
                ambiguous.append({"order_id": oid, "reason": "multiple charge subsets match the order total"})
                continue
            if res:
                matched, match_kind = res, ("single" if len(res) == 1 else "subset")

        if not matched:
            unmatched.append({"order_id": oid, "reason": "no unambiguous charge match — link manually"})
            continue
        conf = 0.95 if match_kind in ("shipment", "single") else 0.8
        for c in matched:
            claimed.add(c["id"])
            links.append({"order_id": oid, "transaction_id": c["id"],
                          "amount_minor": abs(c["charged_amount_minor"]), "confidence": conf, "match": match_kind})
    return {"links": links, "ambiguous": ambiguous, "unmatched": unmatched}


# ── DB writes (migration 010) — idempotent; reference data, so a partial write self-heals on re-run ─────
def upsert_order(db, *, source, external_order_id, merchant_id=None, order_date=None,
                 total_minor=None, currency="SEK", status=None, raw=None):
    return db.upsert("merchant_order", {
        "source": source, "external_order_id": external_order_id, "merchant_id": merchant_id,
        "order_date": order_date, "total_minor": total_minor, "currency": currency,
        "status": status, "raw": raw, "updated_at": _now(),
    }, on_conflict="source,external_order_id")[0]


def set_lines(db, order_id, lines):
    """Replace an order's line items (idempotent). lines: [{description, qty?, unit_amount_minor?,
    amount_minor?, currency?, category_hint?, raw?}]."""
    db.delete("merchant_order_line", {"order_id": "eq." + order_id})
    if lines:
        db.insert("merchant_order_line", [{
            "order_id": order_id, "line_seq": i,
            "description": l["description"], "qty": l.get("qty"),
            "unit_amount_minor": l.get("unit_amount_minor"), "amount_minor": l.get("amount_minor"),
            "currency": l.get("currency", "SEK"), "category_hint": l.get("category_hint"), "raw": l.get("raw"),
        } for i, l in enumerate(lines)], prefer="return=minimal")


def link_charge(db, order_id, transaction_id, amount_minor=None):
    return db.upsert("order_charge_link", {
        "order_id": order_id, "transaction_id": transaction_id, "amount_minor": amount_minor,
    }, on_conflict="order_id,transaction_id")[0]


def attach(db, order, lines, charge_ids):
    """Persist one matched order: upsert header → replace lines → link each charge. Returns the order row.
    Idempotent (unique keys). Does NOT finalize review — the loop still categorizes/splits each linked
    charge and commits it with enrichment_source='merchant'."""
    row = upsert_order(db, source=order["source"], external_order_id=order["external_order_id"],
                       merchant_id=order.get("merchant_id"), order_date=order.get("order_date"),
                       total_minor=order.get("total_minor"), currency=order.get("currency", "SEK"),
                       status=order.get("status"), raw=order.get("raw"))
    set_lines(db, row["id"], lines)
    for tid in charge_ids:
        link_charge(db, row["id"], tid)
    return row


# brand substrings pushed to PostgREST as an ilike pre-filter so we scan ALL matching charges server-side
# (not just a global oldest-N slice); `classify` then assigns the precise source.
_BRAND_ILIKE = {
    "amazon": ["AMAZON", "AMZN"], "paypal": ["PAYPAL"], "google-play": ["GOOGLE"],
    "google-pay": ["GOOGLE"], "apple": ["APPLE.COM", "ITUNES"], "klarna": ["KLARNA"],
}


def needs_deep_enrich(db, source=None, limit=500):
    """Purchase charges that look like a deep-enrich merchant and have NO order linked yet — the queue the
    review loop offers to pull order detail for. Filters by the merchant's brand substrings SERVER-SIDE
    (`ilike`) so a real Amazon charge isn't missed behind a global row cap. Pass `source` to batch one."""
    srcs = [source] if source else list(REGISTRY)
    subs = sorted({s for src in srcs for s in _BRAND_ILIKE.get(src, [])})
    if not subs:
        return []
    rows = db.select("transaction", {
        "select": "id,account_id,booked_date,charged_amount_minor,currency,raw_descriptor,normalized_descriptor,merchant_id",
        "kind": "eq.purchase", "is_transfer": "eq.false",
        "or": "(" + ",".join("raw_descriptor.ilike.*%s*" % s for s in subs) + ")",
        "order": "booked_date.asc.nullslast", "limit": str(limit),
    })
    linked = {l["transaction_id"] for l in db.select("order_charge_link", {"select": "transaction_id"})}
    out = []
    for r in rows:
        src = classify(r.get("raw_descriptor") or r.get("normalized_descriptor"))
        if src and r["id"] not in linked and (source is None or src == source):
            out.append({**r, "deep_enrich_source": src})
    return out


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ── hermetic self-test (no DB / no network) ─────────────────────────────────────
def _selftest():
    assert classify("AMAZONRETAIL*NN79L8KZ4 WWW.AMAZON.SE") == "amazon"
    assert classify("GOOGLE *GOOGLE PLAY AP") == "google-play"   # specific beats the bare wrapper
    assert classify("GOOGLE PAY") == "google-pay"
    assert classify("COOP ALVSJO") is None

    C = lambda i, d, a: {"id": i, "booked_date": d, "charged_amount_minor": a}
    charges = [C("t1", "2026-03-01", -21104), C("t2", "2026-03-02", -5000),
               C("t3", "2026-03-02", -5000), C("t4", "2026-03-10", -9999)]

    # 1) single exact match by amount+date
    r = match_orders_to_charges([{"external_order_id": "o1", "order_date": "2026-03-01", "total_minor": 21104}], charges)
    assert r["links"] == [{"order_id": "o1", "transaction_id": "t1", "amount_minor": 21104,
                           "confidence": 0.95, "match": "single"}], r

    # 2) per-shipment: one order, two shipments → two distinct charges
    r = match_orders_to_charges([{"external_order_id": "o2", "order_date": "2026-03-02", "total_minor": 10000,
                                  "shipments": [{"amount_minor": 5000}, {"amount_minor": 5000}]}], charges)
    tids = sorted(l["transaction_id"] for l in r["links"])
    assert tids == ["t2", "t3"] and all(l["match"] == "shipment" for l in r["links"]), r

    # 3) ambiguous shipment (one 5000 shipment, two equal candidates) → flagged, NOT linked
    r = match_orders_to_charges([{"external_order_id": "o3", "order_date": "2026-03-02", "total_minor": 5000,
                                  "shipments": [{"amount_minor": 5000}]}], charges)
    # total-subset fallback also finds two single-charge subsets (t2, t3) → ambiguous
    assert r["links"] == [] and r["ambiguous"] and not r["unmatched"], r

    # 4) order-total subset-sum: total = t2+t4 uniquely (5000+9999)
    r = match_orders_to_charges([{"external_order_id": "o4", "order_date": "2026-03-05", "total_minor": 14999}],
                                [C("t2", "2026-03-04", -5000), C("t4", "2026-03-06", -9999)], window_days=3)
    assert sorted(l["transaction_id"] for l in r["links"]) == ["t2", "t4"] \
        and all(l["match"] == "subset" for l in r["links"]), r

    # 5) no match → unmatched (left for manual), nothing linked
    r = match_orders_to_charges([{"external_order_id": "o5", "order_date": "2026-03-01", "total_minor": 777}], charges)
    assert r["links"] == [] and r["unmatched"] and not r["ambiguous"], r

    # 6) a claimed charge is never reused across orders
    r = match_orders_to_charges([{"external_order_id": "a", "order_date": "2026-03-01", "total_minor": 21104},
                                 {"external_order_id": "b", "order_date": "2026-03-01", "total_minor": 21104}], charges)
    assert len(r["links"]) == 1 and r["unmatched"], r  # only the first order claims t1; second has nothing left
    print("OK  deepenrich matcher self-test passed (single, shipment, ambiguous, subset, no-match, no-reuse)")
    _livetest()


def _livetest():
    """Live DB round-trip of the write path (queue → match → attach → v_transaction_detail → dequeue)."""
    import db as _db
    d = _db.PostgREST()
    acct = _db.account_by_slug(d, "amex-se")
    tx = d.insert("transaction", {
        "account_id": acct["id"], "fingerprint": "_de_smoke", "booked_date": "2026-03-15",
        "value_date": "2026-03-15", "charged_amount_minor": -21104, "currency": "SEK",
        "raw_descriptor": "AMAZONRETAIL*_DE_SMOKE WWW.AMAZON.SE", "normalized_descriptor": "AMAZONRETAIL _DE_SMOKE",
        "kind": "purchase", "review_status": "new"})[0]
    try:
        assert [r for r in needs_deep_enrich(d, source="amazon") if r["id"] == tx["id"]], "queue should surface it"
        order = {"source": "amazon", "external_order_id": "_DE_ORDER1", "order_date": "2026-03-15",
                 "total_minor": 21104, "currency": "SEK", "status": "delivered", "raw": {"smoke": True}}
        m = match_orders_to_charges([order], [{"id": tx["id"], "booked_date": tx["booked_date"],
                                               "charged_amount_minor": tx["charged_amount_minor"]}])
        assert len(m["links"]) == 1 and m["links"][0]["transaction_id"] == tx["id"], m
        attach(d, order, [{"description": "USB-C cable 2m", "amount_minor": 9904, "category_hint": "shopping"},
                          {"description": "AA batteries 8-pack", "amount_minor": 11200, "category_hint": "shopping"}],
               [tx["id"]])
        det = d.select("v_transaction_detail", {"transaction_id": "eq." + tx["id"], "select": "description,order_total_minor"})
        assert len(det) == 2 and all(x["order_total_minor"] == 21104 for x in det), det
        assert not [r for r in needs_deep_enrich(d, source="amazon") if r["id"] == tx["id"]], "linked → dequeued"
        print("OK  deepenrich live round-trip: queue→match→attach→v_transaction_detail(2 lines)→dequeued")
    finally:
        d.delete("order_charge_link", {"transaction_id": "eq." + tx["id"]})
        d.delete("merchant_order", {"source": "eq.amazon", "external_order_id": "eq._DE_ORDER1"})
        d.delete("transaction", {"fingerprint": "eq._de_smoke"})
    d.close()
    print("OK  cleanup verified")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        print(__doc__)
