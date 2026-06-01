#!/usr/bin/env python3
"""Stdlib-only descriptor normalization + dedup fingerprinting for the expense skills.

Turns a noisy Swedish card/bank descriptor into:
  - cleaned            : uppercased, processor/terminal/ref noise stripped
  - brand / subbrand / town : best-effort tokens for fuzzy merchant + location matching
  - normalized         : stable key for the descriptor_alias cache (cleaned, town kept)

And builds a collision-safe transaction fingerprint that *includes a same-day occurrence
index*, so two identical 39 kr Pressbyrån buys on one day get distinct fingerprints and are
both kept (never silently deduped). LLM-in-the-loop handles whatever the heuristics miss.

`python3 normalize.py --selftest` runs fixture assertions (no network).
"""
import hashlib
import re
import sys

# Payment processors / aggregators that mask the real merchant. Strip the prefix BEFORE
# fuzzy matching or a Places lookup — otherwise "KLARNA *SYSTEMBOLAGET" resolves to Klarna HQ.
_PROCESSORS = [
    "KLARNA", "PAYPAL", "PP", "IZ", "IZETTLE", "ZETTLE", "SUMUP", "STRIPE",
    "SQUARE", "SQ", "ADYEN", "NETS", "VIVA", "MOLLIE", "SHOPIFY", "WWW.",
]
# Prefix forms like "KLARNA*", "KLARNA *", "PAYPAL *", "IZ *", "PP*".
_PROC_RE = re.compile(r"^(?:%s)\s*\*\s*" % "|".join(re.escape(p) for p in _PROCESSORS))

# City abbreviations seen in Swedish descriptors.
_CITY_ABBR = {"STHLM": "STOCKHOLM", "STH": "STOCKHOLM", "GBG": "GÖTEBORG",
              "GTBG": "GÖTEBORG", "MLM": "MALMÖ", "MMA": "MALMÖ"}

# Brands whose name legitimately contains a city — do NOT treat the trailing token as a location.
_BRAND_WITH_CITY = {"GÖTEBORGS SPÅRVÄGAR", "STOCKHOLMS LOKALTRAFIK", "MALMÖ STAD"}

# Known Swedish brand tokens (helps decide what's brand vs town); not exhaustive — just high-leverage.
_KNOWN_BRANDS = {"COOP", "ICA", "WILLYS", "HEMKÖP", "LIDL", "CITY GROSS", "SYSTEMBOLAGET",
                 "PRESSBYRÅN", "APOTEKET", "CIRCLE K", "OKQ8", "PREEM", "MCDONALDS",
                 "MAX", "ESPRESSO HOUSE", "SL", "SJ", "SAS", "SPOTIFY", "NETFLIX"}

# Trailing noise: terminal ids, store numbers, reference numbers, dates, card-tail markers.
_NOISE_RE = re.compile(
    r"\b(?:KORTKÖP|KÖP|PURCHASE|REF\.?\s*\w+|TERM\.?\s*\w+|NR\.?\s*\d+|"
    r"\d{2}[./-]\d{2}(?:[./-]\d{2,4})?|K\d{6,}|\d{4,})\b"
)
_WS_RE = re.compile(r"\s+")
_SUBBRANDS = ("NÄRA", "KVANTUM", "STORA", "SUPERMARKET", "EXTRA", "MAXI", "EXPRESS", "TO GO")


def clean_descriptor(raw: str) -> str:
    s = (raw or "").upper().strip()
    s = _PROC_RE.sub("", s)
    s = s.replace("*", " ")
    s = _NOISE_RE.sub(" ", s)
    s = s.replace(",", " ").replace("/", " ")
    s = _WS_RE.sub(" ", s).strip()
    # expand city abbreviations as standalone tokens
    s = " ".join(_CITY_ABBR.get(tok, tok) for tok in s.split())
    return s


def parse_descriptor(raw: str) -> dict:
    """Best-effort split into brand / subbrand / town. Heuristic, not authoritative."""
    cleaned = clean_descriptor(raw)
    brand = subbrand = town = None

    for known in _BRAND_WITH_CITY:                      # protect brands that contain a city
        if cleaned.startswith(known):
            return {"cleaned": cleaned, "brand": known, "subbrand": None,
                    "town": None, "normalized": cleaned}

    tokens = cleaned.split()
    if tokens:
        # brand = leading known multiword brand, else first token
        for known in sorted(_KNOWN_BRANDS, key=len, reverse=True):
            if cleaned.startswith(known):
                brand = known
                break
        if brand is None:
            brand = tokens[0]
        rest = cleaned[len(brand):].strip().split()
        if rest and rest[0] in _SUBBRANDS:
            subbrand = rest[0]
            rest = rest[1:]
        if rest:
            town = rest[-1]                             # trailing token most often the location
    return {"cleaned": cleaned, "brand": brand, "subbrand": subbrand,
            "town": town, "normalized": cleaned}


FINGERPRINT_VERSION = "1"   # bump (with a migration) when the normalized component's meaning changes

def fingerprint(account_id, booked_date, amount_minor, currency, normalized,
                occurrence=0, source_seq="") -> str:
    """Collision-safe dedup key — but PREFER a stable provider id (external_id) over this whenever the
    source gives one. `source_seq` (a per-row running balance / statement sequence the adapter supplies)
    makes genuinely-identical same-day rows distinct WITHOUT relying on batch order — the robust path for
    id-less export feeds like SAS. `occurrence` is the last-resort batch-order fallback (only stable if the
    SAME full window is re-fetched). FINGERPRINT_VERSION pins the normalizer so a future normalize change
    re-keys deliberately instead of silently re-inserting all history on the next fetch."""
    parts = [FINGERPRINT_VERSION, account_id, str(booked_date), str(amount_minor),
             (currency or "").upper(), normalized or "", str(source_seq or ""), str(occurrence)]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


def _selftest():
    # processor stripping
    assert parse_descriptor("KLARNA *SYSTEMBOLAGET")["brand"] == "SYSTEMBOLAGET", "processor not stripped"
    assert parse_descriptor("IZ *KAFFEBAR STHLM")["town"] == "STOCKHOLM", "city abbr not expanded"
    # brand/location split — same brand, two towns, same merchant brand token
    a, b = parse_descriptor("COOP NÄRA FINSPÅNG"), parse_descriptor("COOP NÄRA STOCKHOLM")
    assert a["brand"] == b["brand"] == "COOP" and a["subbrand"] == "NÄRA", a
    assert a["town"] == "FINSPÅNG" and b["town"] == "STOCKHOLM", (a, b)
    # brand-with-city protected
    assert parse_descriptor("GÖTEBORGS SPÅRVÄGAR")["town"] is None
    # same-day identical purchases get distinct fingerprints via occurrence
    f0 = fingerprint("acc", "2026-01-02", -3900, "SEK", "PRESSBYRÅN", 0)
    f1 = fingerprint("acc", "2026-01-02", -3900, "SEK", "PRESSBYRÅN", 1)
    assert f0 != f1, "same-day repeats must not collide"
    # but a re-fetch of the *same* row (same occurrence) is stable → idempotent
    assert f0 == fingerprint("acc", "2026-01-02", -3900, "SEK", "PRESSBYRÅN", 0)
    print("OK  normalize.py self-test passed")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        print(__doc__)
