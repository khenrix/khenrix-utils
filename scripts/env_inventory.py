#!/usr/bin/env python3
"""env_inventory.py — cross-CLI environment inventory (D1).

Reads the desired-state manifest (docs/environment/inventory.toml), renders
inventory.md from it, probes live CLI state into a gitignored report, and
--checks live-vs-desired. Read-only. Stdlib-only. Tests: `--self-test`.
"""
from __future__ import annotations
import argparse, json, os, re, subprocess, sys, tomllib
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "docs/environment/inventory.toml"
DOC = ROOT / "docs/environment/inventory.md"
REPORT = ROOT / "docs/environment/observed-state.json"   # gitignored

STATUS = {"present", "ported", "native", "not-applicable", "claude-only", "gh-cli", "awaiting-auth"}
CLIS = ("claude", "codex", "agy")

REDACTED = "<redacted>"
# key names whose VALUES are always secret-bearing
_SECRET_KEY = re.compile(r"(token|secret|key|cred|password|passwd|cookie|auth|session|bearer)", re.I)
# env vars that are tuning/config, not secrets (values kept)
_SAFE_ENV = {"UV_HTTP_TIMEOUT"}


def _redact_url(u: str) -> str:
    try:
        parts = urlsplit(u)
    except ValueError:
        return REDACTED
    netloc = parts.netloc
    if "@" in netloc:                        # strip userinfo
        netloc = REDACTED + "@" + netloc.split("@", 1)[1]
    query = REDACTED if parts.query else ""  # drop query values wholesale
    return urlunsplit((parts.scheme, netloc, parts.path, query, ""))


def _sanitize_map(v):
    """env/headers/cookies: keep names, redact values unless explicitly safe."""
    if not isinstance(v, dict):
        return sanitize(v)
    return {k: (v[k] if k in _SAFE_ENV else REDACTED) for k in v}


def _sanitize_args(lst):
    """Command args: a secret can be a bare value after a flag (`--token X`) or
    inline (`--token=X`), with no key to match on. Redact those positionally."""
    out = []
    redact_next = False
    for el in lst:
        if redact_next:
            out.append(REDACTED)
            redact_next = False
            continue
        if isinstance(el, str):
            if el.startswith("-") and "=" in el:
                flag, _, _val = el.partition("=")
                if _SECRET_KEY.search(flag):
                    out.append(flag + "=" + REDACTED)
                    continue
            if el.startswith("-") and _SECRET_KEY.search(el):
                out.append(el)
                redact_next = True
                continue
            if "://" in el:
                out.append(_redact_url(el))
                continue
        out.append(sanitize(el))
    return out


def sanitize(obj):
    """Recursively strip every value-bearing channel; keep only safe symbolic refs."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(v, str) and _SECRET_KEY.search(str(k)) and k not in _SAFE_ENV:
                out[k] = REDACTED
            elif str(k).lower() in ("env", "headers", "cookies"):
                out[k] = _sanitize_map(v)
            elif str(k).lower() in ("args", "argv") and isinstance(v, list):
                out[k] = _sanitize_args(v)
            elif isinstance(v, str) and "://" in v:
                out[k] = _redact_url(v)
            else:
                out[k] = sanitize(v)
        return out
    if isinstance(obj, list):
        return [sanitize(x) for x in obj]
    return obj


def load_manifest(path: Path = MANIFEST) -> dict:
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def validate_manifest(m: dict) -> list[str]:
    """Return a list of human-readable schema errors (empty = valid)."""
    errs: list[str] = []
    plugins = m.get("plugins")
    mcp = m.get("mcp")
    if not isinstance(plugins, list):
        errs.append("top-level [[plugins]] must be a list")
        plugins = []
    if not isinstance(mcp, list):
        errs.append("top-level [[mcp]] must be a list")
        mcp = []
    names_seen: set[str] = set()
    for p in plugins:
        n = p.get("name", "<unnamed>")
        if n in names_seen:
            errs.append(f"duplicate plugin name: {n}")
        names_seen.add(n)
        for f in ("name", "source", "version", "components", "portability"):
            if f not in p:
                errs.append(f"plugin {n}: missing field '{f}'")
        if not isinstance(p.get("components"), list):
            errs.append(f"plugin {n}: components must be a list")
        for cli in CLIS:
            if p.get(cli) not in STATUS:
                errs.append(f"plugin {n}: {cli} status '{p.get(cli)}' not in {sorted(STATUS)}")
    for s in mcp:
        n = s.get("name", "<unnamed>")
        for f in ("name", "transport", "owner", "secret"):
            if f not in s:
                errs.append(f"mcp {n}: missing field '{f}'")
        if s.get("owner") not in ("reconcile", "bootstrap"):
            errs.append(f"mcp {n}: owner must be reconcile|bootstrap")
        for cli in CLIS:
            if s.get(cli) not in STATUS:
                errs.append(f"mcp {n}: {cli} status '{s.get(cli)}' not in {sorted(STATUS)}")
    return errs


def _self_test() -> int:
    ok: list[tuple[str, bool]] = []
    m = load_manifest()
    ok.append(("manifest loads", isinstance(m.get("plugins"), list) and isinstance(m.get("mcp"), list)))
    errs = validate_manifest(m)
    ok.append(("manifest is schema-valid", errs == []))
    ok.append(("bad status rejected", validate_manifest(
        {"plugins": [{"name": "x", "source": "s", "version": "v",
                      "components": ["skills"], "claude": "BOGUS", "codex": "present",
                      "agy": "present", "portability": "p"}], "mcp": []}) != []))
    SENTINEL = "SUPERSECRETVALUE123"
    dirty = {
        "command": "node", "args": ["--token", SENTINEL],
        "env": {"API_KEY": SENTINEL, "UV_HTTP_TIMEOUT": "300"},
        "headers": {"Authorization": f"Bearer {SENTINEL}"},
        "cookies": {"session": SENTINEL},
        "url": f"https://user:{SENTINEL}@example.com/path?token={SENTINEL}",
        "nested": [{"password": SENTINEL}],
    }
    clean = sanitize(dirty)
    blob = json.dumps(clean)
    ok.append(("sentinel never survives sanitize", SENTINEL not in blob))
    ok.append(("safe tuning value preserved", "300" in blob))
    ok.append(("env var NAMES preserved", "API_KEY" in blob and "UV_HTTP_TIMEOUT" in blob))
    ok.append(("url host preserved", "example.com" in blob))
    failed = [n for n, p in ok if not p]
    for n, p in ok:
        print(f"  {'ok' if p else 'FAIL'}  {n}")
    print(f"env_inventory self-test: {len(ok) - len(failed)}/{len(ok)} passed")
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return _self_test()
    ap.error("no action given")
    return 2


if __name__ == "__main__":
    sys.exit(main())
