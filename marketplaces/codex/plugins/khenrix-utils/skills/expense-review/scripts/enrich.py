#!/usr/bin/env python3
"""Stdlib-only, local-first merchant enrichment for expense-review (no pip deps).

Resolution order (cheapest first):
  1. exact `descriptor_alias` hit (provider-scoped)  → zero external calls
  2. Google Places Text Search (New Places API, field-masked Essentials fields) → a *candidate*
The candidate is a suggestion the review loop shows for confirmation — it is NOT auto-committed.
If no GOOGLE_PLACES_API_KEY is set, step 2 is skipped and the loop falls back to LLM + manual.

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


def resolve_merchant(db, provider, raw_descriptor):
    """Local-first: alias cache → Places candidate. Returns a dict the review loop confirms.

    {source: 'cache'|'places'|'none', merchant_id?, merchant_location_id?, candidate?, parsed}
    """
    import normalize  # local helper (same skill dir)
    parsed = normalize.parse_descriptor(raw_descriptor)
    rows = db.select("descriptor_alias", {
        "provider": "eq." + provider,
        "normalized_descriptor": "eq." + parsed["normalized"],
        "select": "merchant_id,merchant_location_id,confidence", "limit": 1,
    })
    if rows:
        hit = rows[0]
        return {"source": "cache", "merchant_id": hit["merchant_id"],
                "merchant_location_id": hit.get("merchant_location_id"), "parsed": parsed}
    query = " ".join(t for t in [parsed.get("brand"), parsed.get("subbrand"), parsed.get("town"), "Sverige"] if t)
    try:
        candidates = places_search(query)
    except RuntimeError:
        candidates = []                                      # degrade to LLM + manual on any Places error
    return {"source": "places" if candidates else "none",
            "candidate": candidates[0] if candidates else None,
            "candidates": candidates, "parsed": parsed}


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
