"""URL canonicalization, host classification, and credential redaction.

Deterministic and stdlib-only. Two jobs the ledger and renderer depend on:

- `canonicalize` collapses a saved URL to a stable identity (IG shortcode, YouTube
  id, GitHub owner/repo) and strips a *documented* tracking-param denylist while
  preserving every other param — so semantic params (`v=`, product variants, signed
  query strings) survive. Canonical form is the ledger dedup key.
- `classify_host` / `redact_credentials` are the safety boundary: they flag
  internal/local/non-http URLs and scrub credential-shaped params from anything the
  wiki will display, so a bookmarked signed link never lands in a committed page.
"""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

# Tracking params dropped from the canonical URL. Documented denylist, not a
# guess-everything heuristic: anything not listed here is preserved so semantic
# params are never lost. `utm_*` matched by prefix.
_TRACKING_PARAMS = {
    "igsh", "igshid", "fbclid", "gclid", "dclid", "msclkid", "yclid",
    "si", "feature", "spm", "ref", "ref_src", "ref_url", "source",
    "mc_cid", "mc_eid", "_hsenc", "_hsmi", "vero_id", "oly_anon_id",
    "s", "t",  # twitter/x share noise
}
# Query params whose VALUES are scrubbed before a URL is shown in the wiki.
_CREDENTIAL_PARAMS = {
    "token", "access_token", "key", "apikey", "api_key", "secret",
    "password", "pwd", "auth", "sig", "signature", "sessionid", "session",
}
_REDACTED = "REDACTED"

_IG_PATH = re.compile(r"^/(?P<seg>p|reel|reels|tv)/(?P<code>[A-Za-z0-9_-]+)/?")
_YT_SHORTS = re.compile(r"^/shorts/(?P<id>[A-Za-z0-9_-]+)")
_YT_EMBED = re.compile(r"^/embed/(?P<id>[A-Za-z0-9_-]+)")


@dataclass(frozen=True)
class CanonUrl:
    canonical: str      # stable dedup identity
    original: str       # exactly what was saved
    native_id: str | None
    kind: str           # instagram_post | instagram_reel | youtube | github | web


def _is_tracking(key: str) -> bool:
    return key in _TRACKING_PARAMS or key.startswith("utm_")


def _split_query(query: str) -> list[tuple[str, str | None]]:
    """Order-preserving split that keeps raw values (no re-encoding). A bare flag
    (`?x`) yields (x, None)."""
    out = []
    if not query:
        return out
    for part in query.split("&"):
        if not part:
            continue
        if "=" in part:
            k, v = part.split("=", 1)
            out.append((k, v))
        else:
            out.append((part, None))
    return out


def _join_query(pairs: list[tuple[str, str | None]]) -> str:
    return "&".join(k if v is None else f"{k}={v}" for k, v in pairs)


def _rebuild(url: str, pairs: list[tuple[str, str | None]]) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path, _join_query(pairs), p.fragment))


def _strip_tracking(url: str) -> str:
    p = urlsplit(url)
    kept = [(k, v) for k, v in _split_query(p.query) if not _is_tracking(k)]
    return urlunsplit((p.scheme, p.netloc, p.path, _join_query(kept), ""))


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def canonicalize(url: str) -> CanonUrl:
    """Collapse `url` to a stable canonical identity + kind. Never raises on odd
    input — unknown shapes fall through to kind='web'."""
    original = url.strip()
    host = _host(original)
    p = urlsplit(original)

    # Instagram — canonical is the bare shortcode URL (all query dropped).
    if host.endswith("instagram.com"):
        m = _IG_PATH.match(p.path)
        if m:
            seg, code = m.group("seg"), m.group("code")
            kind = "instagram_reel" if seg in ("reel", "reels") else "instagram_post"
            canon_seg = "reel" if kind == "instagram_reel" else "p"
            return CanonUrl(f"https://www.instagram.com/{canon_seg}/{code}/",
                            original, code, kind)

    # YouTube — keep the semantic v= param; strip tracking.
    if host.endswith("youtube.com") or host == "youtu.be" or host.endswith(".youtu.be"):
        vid = None
        if host == "youtu.be" or host.endswith(".youtu.be"):
            vid = p.path.lstrip("/").split("/")[0] or None
        else:
            for k, v in _split_query(p.query):
                if k == "v":
                    vid = v
                    break
            if not vid:
                for rx in (_YT_SHORTS, _YT_EMBED):
                    mm = rx.match(p.path)
                    if mm:
                        vid = mm.group("id")
                        break
        return CanonUrl(_strip_tracking(original), original, vid, "youtube")

    # GitHub — native id is owner/repo.
    if host.endswith("github.com"):
        segs = [s for s in p.path.split("/") if s]
        nid = "/".join(segs[:2]) if len(segs) >= 2 else None
        return CanonUrl(_strip_tracking(original), original, nid, "github")

    return CanonUrl(_strip_tracking(original), original, None, "web")


def classify_host(url: str) -> str:
    """Return one of public | internal | local | nonhttp. Used to gate whether a URL
    may be fetched/displayed without confirmation (work/internal links need care)."""
    p = urlsplit(url)
    if p.scheme not in ("http", "https"):
        return "nonhttp"
    host = (p.hostname or "").lower()
    if not host:
        return "nonhttp"
    if host in ("localhost", "localhost.localdomain"):
        return "local"
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_loopback:
            return "local"
        if ip.is_private or ip.is_link_local:
            return "internal"
        return "public"
    except ValueError:
        pass
    if host.endswith((".local", ".internal", ".lan", ".home", ".corp")):
        return "internal"
    if "." not in host:            # single-label hostname → intranet
        return "internal"
    return "public"


def redact_credentials(url: str) -> str:
    """Replace the VALUES of credential-shaped query params with REDACTED, preserving
    order and every other param. For URLs the wiki will display."""
    p = urlsplit(url)
    pairs = [(k, _REDACTED if k.lower() in _CREDENTIAL_PARAMS and v is not None else v)
             for k, v in _split_query(p.query)]
    return _rebuild(url, pairs)
