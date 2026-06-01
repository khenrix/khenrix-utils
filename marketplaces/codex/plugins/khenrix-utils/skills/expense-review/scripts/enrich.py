#!/usr/bin/env python3
"""Stdlib-only, local-first merchant enrichment for expense-review (no pip deps).

Resolution order (cheapest + most-trusted first) — `resolve_merchant` returns a `source`:
  1. `cache`   — exact `descriptor_alias` hit (provider-scoped)            → zero external calls
  2. `details` — the merchant name the SOURCE already captured in `raw_observation`
                 (Amex `mn` / `extended_details.merchant`; Swedbank details pass when present).
                 We trust the source's own merchant name; Places (if keyed) only corroborates address.
  3. `mcc`     — no name, but a captured MCC → category hint (map via `category_for_mcc`)
  4. `places`  — Google Places Text Search (New API, field-masked) → a *candidate*
  5. `none`    — nothing; the loop falls back to LLM + manual
Steps 2–4 are suggestions the review loop confirms — never auto-committed. With no GOOGLE_PLACES_API_KEY
the Places step is skipped (a `details`/`mcc` hit still works; otherwise it degrades to LLM + manual).

`python3 enrich.py --selftest "COOP NÄRA FINSPÅNG"` does a live Places lookup (needs the key).
"""
import http.client
import json
import os
import sys
import urllib.parse
from pathlib import Path

_ENV_PATH = Path.home() / ".config" / "khenrix-utils" / "expenses.env"
_PLACES_HOST = "places.googleapis.com"
_FIELD_MASK = ",".join([
    "places.id", "places.displayName", "places.formattedAddress",
    "places.location", "places.websiteUri", "places.primaryType", "places.types",
])

# MCC → category slug, mirroring references/taxonomy.md's "MCC hints" column. A captured MCC is an
# opportunistic category hint (present on card-rail feeds, usually absent on bank lists). Exact codes
# first; `resa` (travel) also covers airline/hotel ranges. Keep in sync with taxonomy.md + 002_seed.sql.
_MCC_EXACT = {
    "5411": "dagligvaror", "5499": "dagligvaror",
    "5812": "restaurang", "5814": "restaurang", "5811": "restaurang",
    "4111": "transport", "4121": "transport", "4131": "transport", "7523": "transport",
    "5541": "drivmedel", "5542": "drivmedel", "5552": "drivmedel",
    "5200": "boende", "5211": "boende", "5712": "boende",
    "4900": "el-internet", "4814": "el-internet",
    "5912": "halsa", "8062": "halsa", "7997": "halsa", "8011": "halsa",
    "5651": "shopping", "5691": "shopping", "5732": "shopping", "5999": "shopping",
    "5815": "noje", "7832": "noje", "7922": "noje", "5942": "noje",
    "5921": "systembolaget",
    "4899": "prenumerationer", "5968": "prenumerationer", "7372": "prenumerationer",
    "6012": "avgifter", "6051": "avgifter",
    "4511": "resa", "7011": "resa",
}


def category_for_mcc(mcc):
    """Map a merchant category code to a taxonomy slug, or None. Ranges: 3000–3299 airlines,
    3501–3999 hotels → `resa`. Opportunistic — the review loop still confirms."""
    if not mcc:
        return None
    code = str(mcc).strip()
    if code in _MCC_EXACT:
        return _MCC_EXACT[code]
    if code.isdigit():
        n = int(code)
        if 3000 <= n <= 3299 or 3501 <= n <= 3999:
            return "resa"
    return None


def captured_hint(db, tx_id, provider):
    """Merchant name / MCC / city the SOURCE already captured (read from `raw_observation.payload`),
    so we can resolve WITHOUT an external Places call. Returns {name?, mcc?, city?, address?} ({} if none).
    Payload shapes differ per provider: Amex stores a clean `mn` (compact) or `extended_details.merchant`
    (full); Swedbank's list capture carries no merchant (a future details pass may add merchant/MCC, read
    if present); SAS export gives only `Ort` (city)."""
    if not tx_id:
        return {}
    rows = db.select("raw_observation", {
        "transaction_id": "eq." + tx_id, "select": "payload,observed_at",
        "order": "observed_at.desc", "limit": 1})
    p = rows[0].get("payload") if rows else None
    if not isinstance(p, dict):
        return {}
    if provider == "amex":
        m = (p.get("extended_details") or {}).get("merchant") or {}
        if m.get("name"):                                    # full shape
            addr = m.get("address") or {}
            lines = addr.get("address_lines") or []
            return {"name": m["name"], "mcc": m.get("merchant_category_code"),
                    "city": lines[1] if len(lines) > 1 else None,
                    "address": ", ".join(lines) or None}
        if p.get("mn"):                                      # compact shape (`mn` = merchant name)
            return {"name": p["mn"], "mcc": None, "city": None, "address": None}
        return {}
    if provider == "swedbank":                               # list has none; details pass (future) may
        name = p.get("merchant") or p.get("merchantName")
        mcc = p.get("mcc") or p.get("merchantCategoryCode")
        hint = {}
        if name:
            hint["name"] = name
        if mcc:
            hint["mcc"] = mcc
        return hint
    if provider == "sas":
        ort = (p.get("Ort") or "").strip()
        return {"city": ort} if ort else {}
    return {}


def _places_key():
    names = ("EXPENSES_GOOGLE_PLACES_API_KEY", "GOOGLE_PLACES_API_KEY")   # namespaced name preferred
    for n in names:
        if os.environ.get(n):
            return os.environ[n]
    if _ENV_PATH.exists():                                   # fallback to the shared env file
        for line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() in names:
                return v.strip().strip('"').strip("'") or None
    return None


def places_search(query, region="SE", language="sv", limit=3):
    """Google Places (New) Text Search. Returns a list of normalized candidates, or [] on miss/no-key."""
    key = _places_key()
    if not key:
        return []
    body = json.dumps({
        "textQuery": query, "regionCode": region,
        "languageCode": language, "maxResultCount": limit,
    }).encode("utf-8")
    conn = http.client.HTTPSConnection(_PLACES_HOST, timeout=20)
    try:
        conn.request("POST", "/v1/places:searchText", body=body, headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": key,
            "X-Goog-FieldMask": _FIELD_MASK,
        })
        resp = conn.getresponse()
        raw = resp.read()
    finally:
        conn.close()
    if resp.status >= 400:
        # Surface the Places error (quota, API-not-enabled, bad key) instead of a bare code.
        raise RuntimeError(f"Places {resp.status}: {raw.decode('utf-8', 'replace')[:300]}")
    data = json.loads(raw or b"{}")
    out = []
    for p in data.get("places", []):
        loc = p.get("location") or {}
        out.append({
            "google_place_id": p.get("id"),
            "name": (p.get("displayName") or {}).get("text"),
            "address": p.get("formattedAddress"),
            "lat": loc.get("latitude"),
            "lng": loc.get("longitude"),
            "website": p.get("websiteUri"),
            "primary_type": p.get("primaryType"),
            "types": p.get("types", []),
        })
    return out


def resolve_merchant(db, provider, raw_descriptor, tx_id=None):
    """Local-first, captured-details-before-Places. Returns a dict the review loop confirms — never
    auto-committed (except a `cache` hit, which is already a confirmed alias).

    {source: 'cache'|'details'|'mcc'|'places'|'none',
     merchant_id?, merchant_location_id?,   # cache hits only
     captured?,                             # {name?, mcc?, city?, address?} from the source
     mcc_category?,                         # taxonomy slug inferred from a captured MCC
     candidate?, candidates?,               # Google Places suggestion(s), if any
     parsed}                                # normalize.parse_descriptor output

    Pass `tx_id` so we can read what the SOURCE captured (Amex merchant name, a Swedbank details MCC, …)
    BEFORE spending a Places lookup. The loop's job: cache → auto-fill; details → use captured name as the
    canonical merchant (Places corroborates address/website); mcc → category hint, merchant still decided;
    places → propose candidate; none → LLM + manual.
    """
    import normalize  # local helper (same skill dir)
    parsed = normalize.parse_descriptor(raw_descriptor)

    # 1. alias cache — already a confirmed mapping
    rows = db.select("descriptor_alias", {
        "provider": "eq." + provider,
        "normalized_descriptor": "eq." + parsed["normalized"],
        "select": "merchant_id,merchant_location_id,confidence", "limit": 1,
    })
    if rows:
        hit = rows[0]
        return {"source": "cache", "merchant_id": hit["merchant_id"],
                "merchant_location_id": hit.get("merchant_location_id"),
                "confidence": hit.get("confidence"), "parsed": parsed}

    # 2/3. what the source already captured (merchant name and/or MCC)
    hint = captured_hint(db, tx_id, provider)
    mcc_cat = category_for_mcc(hint.get("mcc"))

    # 4. Places — query with the captured merchant NAME when we have one (far better than the raw
    #    descriptor), else the parsed brand; prefer a captured city over the parsed trailing token.
    query_name = hint.get("name") or " ".join(t for t in [parsed.get("brand"), parsed.get("subbrand")] if t)
    town = hint.get("city") or parsed.get("town")
    query = " ".join(t for t in [query_name, town, "Sverige"] if t)
    try:
        candidates = places_search(query) if query_name else []
    except RuntimeError:
        candidates = []                                      # degrade gracefully on any Places error

    base = {"captured": hint or None, "mcc_category": mcc_cat,
            "candidate": candidates[0] if candidates else None,
            "candidates": candidates, "parsed": parsed}
    if hint.get("name"):                                     # source gave us the merchant name → trust it
        return {"source": "details", **base}
    if mcc_cat:                                              # no name, but a category from the MCC
        return {"source": "mcc", **base}
    return {"source": "places" if candidates else "none", **base}


def _selftest(query):
    key = _places_key()
    if not key:
        print("SKIP  no GOOGLE_PLACES_API_KEY set — enrichment will fall back to LLM + manual")
        return
    res = places_search(query)
    if not res:
        print(f"OK    Places reachable, no match for {query!r} (would fall back to manual)")
        return
    top = res[0]
    print(f"OK    Places resolved {query!r} → {top['name']} | {top['address']} | "
          f"type={top['primary_type']} | place_id={top['google_place_id'][:18]}…")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        q = next((a for a in sys.argv[1:] if not a.startswith("--")), "COOP NÄRA FINSPÅNG")
        _selftest(q)
    else:
        print(__doc__)
