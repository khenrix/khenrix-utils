#!/usr/bin/env python3
"""expense-fetch engine — staging, collision-safe dedup, pending→booked promotion, Wint upsert, and
charge↔reimbursement reconciliation. Deterministic + eval-testable; the SKILL.md (Claude) drives the
browser captures and hands normalized rows here. Stdlib only; imports sibling `db.py` / `normalize.py`.

`python3 fetch.py --selftest` runs a full live round-trip (ingest w/ same-day dupes + idempotent re-run +
pending→booked promotion → Wint upsert → reconcile charge+reimbursement → verify → clean up).
"""
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta, timezone, date as _date

import db as _db
import normalize

_XL_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _as_date(s):
    if not s:
        return None
    if isinstance(s, _date):
        return s
    return _date.fromisoformat(str(s)[:10])


# ── import-run staging / audit ─────────────────────────────────────────────────
def start_import(database, account_id, *, method, window_from=None, window_to=None):
    return database.insert("import_run", {
        "account_id": account_id, "method": method,
        "window_from": window_from, "window_to": window_to, "status": "running",
    })[0]["id"]


def finish_import(database, import_run_id, counts, status="ok"):
    database.update("import_run", {"id": "eq." + import_run_id}, {
        "status": status, "finished_at": _now_iso(),
        "n_fetched": counts.get("fetched", 0), "n_inserted": counts.get("inserted", 0),
        "n_updated": counts.get("promoted", 0), "n_skipped": counts.get("duplicate", 0),
        "n_flagged": counts.get("flagged", 0),
    })


def store_raw(database, import_run_id, account_id, provider_ref, payload, transaction_id=None, status="raw"):
    database.insert("raw_observation", {
        "import_run_id": import_run_id, "account_id": account_id, "provider_ref": provider_ref,
        "payload": payload, "transaction_id": transaction_id, "status": status,
    }, prefer="return=minimal")


# ── ingest with collision-safe dedup ───────────────────────────────────────────
def _occurrences(rows):
    """Per (date, amount, normalized) group, assign a stable 0-based index in batch order, so two
    identical same-day purchases get DISTINCT fingerprints (both kept) yet re-fetch is idempotent."""
    seen, out = {}, []
    for r in rows:
        key = (str(r.get("value_date") or r.get("booked_date")), r["charged_amount_minor"],
               (r.get("normalized_descriptor") or r["raw_descriptor"] or "").upper())
        n = seen.get(key, 0)
        out.append(n)
        seen[key] = n + 1
    return out


def ingest_batch(database, account, rows, import_run_id=None):
    """rows: normalized dicts (external_id?, booked_date, value_date, charged_amount_minor, currency,
    raw_descriptor, normalized_descriptor?, kind?, booking_status?, mcc?, fx...). Returns action counts."""
    counts = {"fetched": len(rows), "inserted": 0, "promoted": 0, "duplicate": 0}
    for r, occ in zip(rows, _occurrences(rows)):
        anchor = r.get("value_date") or r.get("booked_date")
        fp = normalize.fingerprint(account["id"], anchor, r["charged_amount_minor"],
                                   r.get("currency", "SEK"),
                                   r.get("normalized_descriptor") or r["raw_descriptor"], occ,
                                   source_seq=r.get("source_seq"))
        ext = r.get("external_id")
        existing = None
        if ext:
            hit = database.select("transaction", {"account_id": "eq." + account["id"],
                                                  "external_id": "eq." + ext, "limit": 1})
            existing = hit[0] if hit else None
        if existing is None:
            hit = database.select("transaction", {"account_id": "eq." + account["id"],
                                                  "fingerprint": "eq." + fp, "limit": 1})
            existing = hit[0] if hit else None
        body = {
            "account_id": account["id"], "external_id": ext, "fingerprint": fp,
            "booked_date": r.get("booked_date"), "value_date": r.get("value_date"),
            "kind": r.get("kind", "purchase"), "booking_status": r.get("booking_status", "booked"),
            "charged_amount_minor": r["charged_amount_minor"], "currency": r.get("currency", "SEK"),
            "original_amount_minor": r.get("original_amount_minor"),
            "original_currency": r.get("original_currency"), "fx_rate": r.get("fx_rate"),
            "raw_descriptor": r["raw_descriptor"], "normalized_descriptor": r.get("normalized_descriptor"),
            "mcc": r.get("mcc"),
        }
        if existing is None:
            row = database.insert("transaction", body)[0]
            counts["inserted"] += 1
            if import_run_id:
                store_raw(database, import_run_id, account["id"], ext, r.get("raw_payload", body),
                          row["id"], status="inserted")
        elif existing["review_status"] == "new" and (
                existing["booking_status"] != body["booking_status"]
                or existing["charged_amount_minor"] != body["charged_amount_minor"]):
            # pending→booked promotion / amount correction (only touch un-reviewed rows)
            database.update("transaction", {"id": "eq." + existing["id"]}, {
                "booking_status": body["booking_status"],
                "charged_amount_minor": body["charged_amount_minor"],
                "booked_date": body["booked_date"], "observed_last_at": _now_iso(), "updated_at": _now_iso(),
            })
            counts["promoted"] += 1
            if import_run_id:
                store_raw(database, import_run_id, account["id"], ext, r.get("raw_payload", body),
                          existing["id"], status="promoted")
        else:
            database.update("transaction", {"id": "eq." + existing["id"]}, {"observed_last_at": _now_iso()})
            counts["duplicate"] += 1
            if import_run_id:
                store_raw(database, import_run_id, account["id"], ext, r.get("raw_payload", body),
                          existing["id"], status="duplicate")
    return counts


# ── Wint receipts + reconciliation ─────────────────────────────────────────────
def upsert_wint(database, receipt):
    """receipt: dict mapped from api.wint.se Receipt (keeps full payload in `raw`)."""
    return database.upsert("wint_expense", receipt, on_conflict="wint_id",
                           prefer="resolution=merge-duplicates,return=representation")[0]


def _within(cands, target, window_days):
    """Candidates within ±window_days of target, by booked/value date."""
    t = _as_date(target)
    if not t:
        return []
    out = []
    for c in cands:
        cd = _as_date(c.get("booked_date") or c.get("value_date"))
        if cd and abs((cd - t).days) <= window_days:
            out.append(c)
    return out


def reconcile_wint(database, window_days=5):
    """Conservative linking — auto-links ONLY when exactly one unclaimed candidate matches (else flags
    ambiguous for manual linking, never force-matches). charge: an unclaimed purchase whose magnitude ==
    receipt amount near receipt_date. reimbursement: an unclaimed positive deposit == amount near
    payment_date. Returns {charge, reimbursement, ambiguous}. Batched payouts (one deposit for several
    receipts) won't 1:1-match and are left ambiguous → manual."""
    wints = database.select("wint_expense", {})
    claimed_charge = {w["charge_transaction_id"] for w in wints if w["charge_transaction_id"]}
    claimed_reimb = {w["reimbursed_transaction_id"] for w in wints if w["reimbursed_transaction_id"]}
    out = {"charge": 0, "reimbursement": 0, "ambiguous": 0}
    for w in wints:
        amt = w.get("amount_sek_minor")
        if not amt:
            continue
        if not w["charge_transaction_id"]:
            cand = [c for c in database.select("transaction", {
                        "charged_amount_minor": "eq." + str(-abs(amt)), "kind": "eq.purchase",
                        "currency": "eq.SEK", "is_reimbursable": "eq.false",
                        "select": "id,booked_date,value_date,review_status"})
                    if c["id"] not in claimed_charge]
            near = _within(cand, w["receipt_date"], window_days)
            if len(near) == 1:                            # link only on a single unambiguous unclaimed match
                tid = near[0]["id"]
                database.request("POST", "/rpc/reconcile_charge",   # atomic: link + flag reimbursable + reopen
                                 body={"p_wint": w["id"], "p_tx": tid}, prefer="return=minimal")
                claimed_charge.add(tid); out["charge"] += 1
            elif len(near) > 1:
                out["ambiguous"] += 1
        if not w["reimbursed_transaction_id"] and w.get("paid_out") and w.get("payment_date"):
            cand = [c for c in database.select("transaction", {
                        "charged_amount_minor": "eq." + str(abs(amt)), "is_transfer": "eq.false",
                        "currency": "eq.SEK", "review_status": "eq.new",   # don't grab an already-reviewed deposit
                        "select": "id,booked_date,value_date"})
                    if c["id"] not in claimed_reimb]
            near = _within(cand, w["payment_date"], window_days)
            if len(near) == 1:
                tid = near[0]["id"]
                database.request("POST", "/rpc/reconcile_reimbursement",   # atomic: link + kind/transfer
                                 body={"p_wint": w["id"], "p_tx": tid}, prefer="return=minimal")
                claimed_reimb.add(tid); out["reimbursement"] += 1
            elif len(near) > 1:
                out["ambiguous"] += 1
    return out


# ── SAS / SEB Kort Excel export reader (stdlib OOXML; column-letter aware) ──────
def _col_index(ref):
    letters = re.match(r"[A-Z]+", ref or "A").group(0)
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def parse_xlsx(path):
    """Minimal stdlib XLSX → list of rows (each a list of cell text), gaps preserved by column letter.
    For SAS 'Kontoutdrag → Exportera till Excel'. Handles shared strings, inline strings, numbers."""
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        shared = []
        if "xl/sharedStrings.xml" in names:
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall(f"{_XL_NS}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{_XL_NS}t")))
        sheet = "xl/worksheets/sheet1.xml" if "xl/worksheets/sheet1.xml" in names \
            else next(n for n in names if n.startswith("xl/worksheets/sheet"))
        rows = []
        for row in ET.fromstring(z.read(sheet)).iter(f"{_XL_NS}row"):
            cells = {}
            for c in row.findall(f"{_XL_NS}c"):
                idx = _col_index(c.get("r"))
                v = c.find(f"{_XL_NS}v")
                if c.get("t") == "s" and v is not None:
                    cells[idx] = shared[int(v.text)]
                elif v is not None:
                    cells[idx] = v.text or ""
                else:
                    is_ = c.find(f"{_XL_NS}is")
                    cells[idx] = "".join(x.text or "" for x in is_.iter(f"{_XL_NS}t")) if is_ is not None else ""
            width = (max(cells) + 1) if cells else 0
            rows.append([cells.get(i, "") for i in range(width)])
        return rows


def xlsx_date(serial):
    """Excel serial number → ISO date string. The 1899-12-30 epoch absorbs Excel's 1900-leap-year bug for
    serials >= 61 (the only range real statements use). The SAS adapter must run date cells through this —
    parse_xlsx returns them as raw serial text, which `_as_date` would otherwise reject."""
    return (_date(1899, 12, 30) + timedelta(days=int(float(serial)))).isoformat()


# ── self-test ──────────────────────────────────────────────────────────────────
def _selftest():
    d = _db.PostgREST()
    acct = _db.account_by_slug(d, "swedbank")
    run = start_import(d, acct["id"], method="selftest")
    try:
        rows = [
            {"external_id": "_ft_charge", "booked_date": "2026-02-10", "value_date": "2026-02-10",
             "charged_amount_minor": -50000, "raw_descriptor": "_FT WEBHALLEN", "normalized_descriptor": "_FT WEBHALLEN"},
            {"booked_date": "2026-02-12", "value_date": "2026-02-12", "charged_amount_minor": -3900,
             "raw_descriptor": "_FT PRESSBYRAN", "normalized_descriptor": "_FT PRESSBYRAN"},
            {"booked_date": "2026-02-12", "value_date": "2026-02-12", "charged_amount_minor": -3900,
             "raw_descriptor": "_FT PRESSBYRAN", "normalized_descriptor": "_FT PRESSBYRAN"},  # same-day dupe
            {"external_id": "_ft_deposit", "booked_date": "2026-02-20", "value_date": "2026-02-20",
             "charged_amount_minor": 50000, "raw_descriptor": "_FT WINT PAYOUT", "normalized_descriptor": "_FT WINT PAYOUT"},
        ]
        c1 = ingest_batch(d, acct, rows, import_run_id=run)
        assert c1["inserted"] == 4, c1                              # both same-day Pressbyrån kept
        c2 = ingest_batch(d, acct, rows, import_run_id=run)
        assert c2["inserted"] == 0 and c2["duplicate"] == 4, c2     # idempotent re-run
        # pending→booked promotion via external_id
        rows[0]["charged_amount_minor"] = -50500; rows[0]["booking_status"] = "booked"
        c3 = ingest_batch(d, acct, [rows[0]])
        assert c3["promoted"] == 1, c3
        d.update("transaction", {"external_id": "eq._ft_charge"}, {"charged_amount_minor": -50000})  # reset for recon

        w = upsert_wint(d, {"wint_id": "_ft_r1", "serial_number": 999, "receipt_date": "2026-02-10",
                            "amount_minor": 50000, "currency": "SEK", "amount_sek_minor": 50000,
                            "supplier": "Webhallen", "payment_method": "Eget utlägg", "paid_out": True,
                            "payment_date": "2026-02-20", "raw": {"selftest": True}})
        rec = reconcile_wint(d)
        assert rec["charge"] == 1 and rec["reimbursement"] == 1, rec
        rv = d.select("v_wint_reconciliation", {"id": "eq." + w["id"]})[0]
        assert rv["recon_status"] == "settled", rv
        charge = d.select("transaction", {"external_id": "eq._ft_charge", "select": "is_reimbursable"})[0]
        dep = d.select("transaction", {"external_id": "eq._ft_deposit", "select": "kind,is_transfer"})[0]
        assert charge["is_reimbursable"] and dep["kind"] == "reimbursement" and dep["is_transfer"], (charge, dep)
        print(f"OK  ingest {c1}; re-run idempotent; promote {c3['promoted']}; reconcile {rec} → settled")
    finally:
        d.delete("wint_expense", {"wint_id": "eq._ft_r1"})
        d.delete("transaction", {"raw_descriptor": "like._FT%"})
        d.delete("import_run", {"id": "eq." + run})
    print("OK  cleanup verified")
    d.close()


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        print(__doc__)
