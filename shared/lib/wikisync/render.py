"""Deterministic page rendering — frontmatter, slug/collision filenames, managed merge.

Everything an LLM would drift on across runs and CLIs lives here as pure code: the
frontmatter schema, collision-safe filenames, and — crucially — the managed/manual
boundary. Only the region between the khenrix:managed markers is regenerated; anything
the user hand-wrote outside it survives a refetch/reprocess untouched. Credential-shaped
query params are redacted from the wiki-visible source URL. Given a fixed `now`, output
is byte-identical, so `reprocess` doesn't churn pages that didn't actually change.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass

from . import SCHEMA_VERSION
from .canonurl import redact_credentials
from .taxonomy import TAXONOMY_VERSION, Route

MANAGED_START = "<!-- khenrix:managed:start -->"
MANAGED_END = "<!-- khenrix:managed:end -->"

_YAML_SPECIAL = set(":#\"'{}[]|>&*!%@`,")

_URL_RE = re.compile(r"https?://[^\s)\]<>\"']+")

# how each fetch-capability reads in the human "provenance" line
_CAP_LABEL = {
    "caption": "post caption",
    "comments": "top comments",
    "video_frames": "video (frames)",
    "video_note": "video (frames)",
    "transcript": "video transcript",
    "original-source": "linked original recipe",
    "archive": "web-archive snapshot",
    "metadata": "page metadata",
    "article_body": "article text",
    "readme": "repo README",
}


def _first_url(text: str) -> str | None:
    m = _URL_RE.search(text or "")
    return m.group(0).rstrip(".,;")  if m else None


def _provides_text(capabilities) -> str:
    seen, out = set(), []
    for c in capabilities or []:
        lbl = _CAP_LABEL.get(c, c)
        if lbl not in seen:
            seen.add(lbl)
            out.append(lbl)
    return ", ".join(out)


def _collect_sources(source_url, channel, author, capabilities, extraction):
    """Ordered [(label, url, provides)] for the Sources section. Primary = the saved
    item; additional = declared extraction['sources'] plus any URL found in
    original-source / archive captures (so resync knows exactly where each fact lives).
    De-duped by url."""
    primary_caps = [c for c in (capabilities or []) if c not in ("original-source", "archive")]
    label = (channel or "source").replace("-", " ")
    if author:
        label = f"{label} · {author}"
    out = [(label, source_url, _provides_text(primary_caps) or "the saved item")]
    seen = {source_url}
    for s in extraction.get("sources", []) or []:
        u = (s.get("url") or "").strip()
        if u and u not in seen:
            out.append((s.get("label") or "original source", u, s.get("provides") or ""))
            seen.add(u)
    for cap in extraction.get("captures", []) or []:
        kind = cap.get("kind", "")
        if kind in ("original-source", "archive"):
            u = _first_url(cap.get("text", ""))
            if u and u not in seen:
                lbl = "original recipe" if kind == "original-source" else "web archive"
                prov = "full recipe" if kind == "original-source" else "archived page"
                out.append((lbl, u, prov))
                seen.add(u)
    return out


@dataclass(frozen=True)
class PageDoc:
    path: str            # vault-relative, e.g. wiki/recipes/carbonara-chef-ab12cd.md
    text: str
    generated_hash: str


def _slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def filename(title: str, author: str, item_id: str) -> str:
    """Deterministic, collision-safe: <title-slug>-<author-slug>-<h6> (or -<h8> with no
    author). The item_id hash guarantees uniqueness even for same title+author."""
    base = _slug(title) or "untitled"
    h = hashlib.sha256(str(item_id).encode()).hexdigest()
    asl = _slug(author) if author else ""
    if asl:
        return f"{base}-{asl}-{h[:6]}.md"
    return f"{base}-{h[:8]}.md"


def _yaml_scalar(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    s = str(v)
    if s == "" or s.strip() != s or any(ch in s for ch in _YAML_SPECIAL):
        return json.dumps(s, ensure_ascii=False)
    return s


def _yaml_frontmatter(fields: list[tuple[str, object]]) -> str:
    lines = ["---"]
    for key, val in fields:
        if isinstance(val, list):
            if not val:
                lines.append(f"{key}: []")
            else:
                lines.append(f"{key}:")
                lines += [f"  - {_yaml_scalar(v)}" for v in val]
        else:
            lines.append(f"{key}: {_yaml_scalar(val)}")
    lines.append("---")
    return "\n".join(lines)


def _managed_body(title, source_url, author, channel, capabilities, fetched_at,
                  extraction, sources) -> str:
    lines = [f"# {title}", ""]
    src = f"> {source_url}" + (f" — {author}" if author else "")
    lines += ["> [!info] Source", src]
    meta = []
    if channel:
        meta.append(channel)
    if capabilities:
        meta.append("depth: " + ", ".join(capabilities))
    if fetched_at:
        meta.append(f"captured {fetched_at}")
    if meta:
        lines.append("> " + " · ".join(meta))
    lines.append("")
    if extraction.get("summary"):
        lines += ["## Summary", str(extraction["summary"]), ""]
    if extraction.get("ingredients"):
        lines += ["## Ingredients"] + [f"- {x}" for x in extraction["ingredients"]] + [""]
    if extraction.get("method"):
        lines += ["## Method"] + [f"{i}. {s}" for i, s in enumerate(extraction["method"], 1)] + [""]
    if extraction.get("notes"):
        lines += ["## Notes", str(extraction["notes"]), ""]
    if extraction.get("caveats"):
        lines += ["> [!warning] Extraction caveats", f"> {extraction['caveats']}", ""]
    # Sources & provenance — every place a fact came from, so a resync is unambiguous.
    if sources:
        lines.append("## Sources")
        for label, url, provides in sources:
            tail = f" — {provides}" if provides else ""
            lines.append(f"- **{label}:** {url}{tail}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_page(item, extraction: dict, route: Route, existing_text: str | None = None,
                now: str = "") -> PageDoc:
    extraction = extraction or {}
    source_url = redact_credentials(item.canonical_url)
    title = extraction.get("title") or getattr(item, "title", "") or "Untitled"
    author = extraction.get("author") or ""
    channel = extraction.get("source_channel") or ""
    collections = extraction.get("collections") or (
        [item.collection] if getattr(item, "collection", "") else [])
    capabilities = extraction.get("fetch_capabilities") or extraction.get("capabilities") or []
    fetched_at = extraction.get("fetched_at") or now
    created = extraction.get("created") or now

    sources = _collect_sources(source_url, channel, author, capabilities, extraction)
    additional_sources = [u for (_lbl, u, _p) in sources[1:]]

    frontmatter = _yaml_frontmatter([
        ("schema_version", SCHEMA_VERSION),
        ("type", route.kind),
        ("title", title),
        ("created", created),
        ("updated", now),
        ("status", extraction.get("status") or "current"),
        ("tags", list(route.tags)),
        ("source_url", source_url),
        ("source_author", author),
        ("source_channel", channel),
        ("source_collection", collections),
        ("native_id", item.native_id),
        ("capture_id", extraction.get("capture_id") or ""),
        ("fetcher_version", extraction.get("fetcher_version", 1)),
        ("extractor_version", extraction.get("extractor_version", 1)),
        ("taxonomy_version", TAXONOMY_VERSION),
        ("fetch_capabilities", list(capabilities)),
        ("additional_sources", additional_sources),
        ("fetched_at", fetched_at),
    ])

    body = _managed_body(title, source_url, author, channel, capabilities, fetched_at,
                         extraction, sources)
    managed = f"{MANAGED_START}\n{body}{MANAGED_END}"

    # Preserve anything the user hand-wrote after the managed region.
    manual_tail = ""
    if existing_text and MANAGED_END in existing_text:
        manual_tail = existing_text.split(MANAGED_END, 1)[1]

    text = f"{frontmatter}\n\n{managed}{manual_tail}"
    if not manual_tail.endswith("\n"):
        text += "\n"
    gen_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    path = f"{route.target_dir}/{filename(title, author, item.native_id)}"
    return PageDoc(path=path, text=text, generated_hash=gen_hash)
