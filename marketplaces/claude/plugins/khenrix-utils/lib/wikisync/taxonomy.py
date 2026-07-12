"""Collection → (page type, target dir, namespaced tags) routing.

The bookmark folder path and IG collection name are provenance, not final truth — but
they're a strong classification hint, so this maps them to a page type and a controlled,
namespaced tag vocabulary (course/*, cuisine/*, diet/*, method/*, protein/*) that Bases
dashboards group on. An explicit `type` in the LLM extraction overrides the folder guess.
Bump TAXONOMY_VERSION when the vocabulary changes so `reclassify` can find stale pages.
"""
from __future__ import annotations

from dataclasses import dataclass

TAXONOMY_VERSION = 1

_TARGET_DIR = {
    "recipe": "wiki/recipes",
    "product": "wiki/products",
    "inspiration": "wiki/inspiration",
    "source": "wiki/sources",
}

# substring (in a lowercased folder segment) → namespaced tag. First match wins.
_COURSE = [
    ("starter", "course/starter"), ("appetiz", "course/starter"),
    ("main", "course/main"), ("side", "course/side"),
    ("soup", "course/soup"), ("stew", "course/soup"),
    ("sauce", "course/sauce"), ("rub", "course/sauce"), ("marinade", "course/sauce"),
    ("dessert", "course/dessert"), ("baking", "course/dessert"),
    ("drink", "course/drink"), ("cocktail", "course/drink"),
    ("snack", "course/snack"), ("technique", "course/technique"),
    ("breakfast", "course/breakfast"),
]
_CUISINE = [
    ("italian", "cuisine/italian"), ("japanese", "cuisine/japanese"),
    ("swedish", "cuisine/swedish"), ("tex-mex", "cuisine/mexican"),
    ("mexican", "cuisine/mexican"), ("sichuan", "cuisine/chinese"),
    ("chinese", "cuisine/chinese"), ("american", "cuisine/american"),
    ("bbq", "cuisine/american"), ("middle eastern", "cuisine/middle-eastern"),
    ("spanish", "cuisine/spanish"), ("vietnamese", "cuisine/vietnamese"),
    ("thai", "cuisine/thai"), ("korean", "cuisine/korean"),
    ("indian", "cuisine/indian"), ("french", "cuisine/french"),
    ("greek", "cuisine/greek"),
]
# collection keyword → page type (checked in order; FIRST hit wins). Order matters:
# shopping intent (a "Köpa?" folder and its subfolders like "Kitchen & Cooking") must
# beat food/recipe keywords, so product is checked before recipe. "kitchen" is NOT a
# recipe trigger — in this vault it only appears under the shopping tree.
_TYPE_HINTS = [
    (("köpa", "kopa", "gift", "wishlist", "home & garden", "furniture", "decor",
      "beauty", "clothing", "sports & outdoor"), "product"),
    (("food", "foodapp", "recipe", "treats"), "recipe"),
    (("github", "inspo", "tech"), "inspiration"),
]


@dataclass
class Route:
    kind: str
    target_dir: str
    tags: list


def _segments(collection: str) -> list[str]:
    return [s.strip().lower() for s in (collection or "").split("/") if s.strip()]


def _first_match(segments: list[str], table) -> str | None:
    for seg in segments:
        for needle, tag in table:
            if needle in seg:
                return tag
    return None


def _kind_from_collection(collection: str) -> str:
    low = (collection or "").lower()
    for keywords, kind in _TYPE_HINTS:
        if any(k in low for k in keywords):
            return kind
    return "source"


def route(item, extraction: dict) -> Route:
    """Classify an item into a page type + target dir + namespaced tags. An explicit
    extraction['type'] wins over the folder-derived guess."""
    extraction = extraction or {}
    kind = extraction.get("type") or _kind_from_collection(getattr(item, "collection", ""))
    if kind not in _TARGET_DIR:
        kind = "source"

    tags = {kind}
    segs = _segments(getattr(item, "collection", ""))
    for tbl in (_COURSE, _CUISINE):
        t = _first_match(segs, tbl)
        if t:
            tags.add(t)
    # facets the LLM extraction supplies, namespaced
    for facet in ("diet", "method", "protein", "meal", "occasion"):
        for v in extraction.get(facet, []) or []:
            tags.add(f"{facet}/{_tagslug(v)}")
    # pre-namespaced tags passed straight through
    for t in extraction.get("tags", []) or []:
        tags.add(t)
    return Route(kind=kind, target_dir=_TARGET_DIR[kind], tags=sorted(tags))


def _tagslug(v: str) -> str:
    return "-".join(str(v).strip().lower().split())
