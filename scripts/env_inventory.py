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


PROBE_ERROR = object()   # sentinel: a source could not be observed


def _run(cmd: list[str], timeout: int = 20) -> str | None:
    """Non-interactive, time-bounded. Returns stdout, or None on any failure."""
    env = {**os.environ, "TERM": "dumb", "PAGER": "cat", "NO_COLOR": "1"}
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def _parse_json_list(text: str):
    """Parse a JSON array (codex --json) or config dict ({mcpServers:{...}}) → list.
    PROBE_ERROR on unusable input."""
    if not text or not text.strip():
        return PROBE_ERROR
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return PROBE_ERROR
    if isinstance(data, dict):
        for key in ("mcpServers", "servers", "plugins"):
            if key in data and isinstance(data[key], dict):
                return list(data[key])
            if key in data and isinstance(data[key], list):
                return data[key]
        return list(data)
    return data if isinstance(data, list) else PROBE_ERROR


def _names_from(parsed):
    """Normalize a parsed list into a set of names (str elements or dict-with-name)."""
    if parsed is PROBE_ERROR:
        return PROBE_ERROR
    names = set()
    for e in parsed:
        if isinstance(e, str):
            names.add(e)
        elif isinstance(e, dict) and "name" in e:
            names.add(e["name"])
    return names


def _read_config(path: Path):
    return _parse_json_list(path.read_text()) if path.exists() else PROBE_ERROR


def probe_all() -> dict:
    """Observe live MCP state per CLI (prefer config files / --json; never scrape text).
    Live codex-plugin / ported-skill probing is a documented follow-up — those sets are
    empty here, so the XOR live check only fires on data actually observed."""
    out: dict = {}
    claude_mcp = _read_config(Path.home() / ".claude.json")
    out["claude"] = {"mcp": _names_from(claude_mcp)}
    codex_json = _run(["codex", "mcp", "list", "--json"])
    out["codex"] = {
        "mcp": _names_from(_parse_json_list(codex_json)) if codex_json is not None else PROBE_ERROR,
        "native_plugins": set(), "ported_skills": set(),
    }
    agy_mcp = _read_config(Path.home() / ".gemini/config/mcp_config.json")
    out["agy"] = {"mcp": _names_from(agy_mcp)}
    return out


def _report_view(obs: dict) -> dict:
    """JSON-serializable, human-readable view (sets→sorted lists, sentinel→string)."""
    v: dict = {}
    for cli, d in obs.items():
        row = {}
        for k, val in d.items():
            row[k] = "PROBE_ERROR" if val is PROBE_ERROR else sorted(val)
        v[cli] = row
    return v


def write_report() -> Path:
    REPORT.write_text(json.dumps(sanitize(_report_view(probe_all())), indent=2) + "\n")
    return REPORT


def render_md(m: dict) -> str:
    lines = [
        "<!-- GENERATED by scripts/env_inventory.py --render from inventory.toml. DO NOT EDIT. -->",
        "# Environment inventory (cross-CLI)", "",
        f"Snapshot: {m.get('meta', {}).get('snapshot_date', 'n/a')}. "
        "Desired-state — the reproduction target. Versions are best-effort references, not pins.",
        "", "## Plugins", "",
        "| Plugin | Source | Version | Components | Claude | Codex | agy | Portability |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for p in sanitize(m["plugins"]):
        lines.append("| {name} | {source} | {version} | {comp} | {claude} | {codex} | {agy} | {portability} |".format(
            comp=", ".join(p.get("components", [])), **{**p, "portability": p.get("portability", "")}))
    lines += ["", "## MCP servers", "",
              "| MCP | Transport | Owner | Secret | Claude | Codex | agy | Notes |",
              "|---|---|---|---|---|---|---|---|"]
    for s in sanitize(m["mcp"]):
        note = s.get("portability", "")
        if s.get("xor_exempt"):
            note = ("XOR-exempt; " + note).strip("; ")
        lines.append("| {name} | {transport} | {owner} | {secret} | {claude} | {codex} | {agy} | {note} |".format(
            note=note, **s))
    return "\n".join(lines) + "\n"


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
    md = render_md(m)
    ok.append(("render includes a known plugin", "superpowers" in md))
    ok.append(("render includes a known mcp", "google-drive" in md))
    ok.append(("render is deterministic", render_md(m) == md))
    ok.append(("render carries no sentinel", SENTINEL not in md))
    ok.append(("render marks github xor-exempt", "xor-exempt" in md.lower()))
    FIX = ROOT / "scripts/fixtures/env_inventory"
    ok.append(("json-array fixture parses to names",
               _names_from(_parse_json_list((FIX / "codex_mcp.json").read_text())) >= {"chrome-devtools", "context7"}))
    ok.append(("config-dict fixture parses to names",
               _names_from(_parse_json_list((FIX / "agy_config.json").read_text())) >= {"context7", "vercel"}))
    ok.append(("malformed input -> PROBE_ERROR (no crash)",
               _parse_json_list((FIX / "malformed.json").read_text()) is PROBE_ERROR))
    ok.append(("empty input -> PROBE_ERROR", _parse_json_list("") is PROBE_ERROR))
    ok.append(("_names_from passes PROBE_ERROR through", _names_from(PROBE_ERROR) is PROBE_ERROR))
    ok.append(("report view carries no sentinel object",
               "PROBE_ERROR" == _report_view({"x": {"mcp": PROBE_ERROR}})["x"]["mcp"]))
    failed = [n for n, p in ok if not p]
    for n, p in ok:
        print(f"  {'ok' if p else 'FAIL'}  {n}")
    print(f"env_inventory self-test: {len(ok) - len(failed)}/{len(ok)} passed")
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--render", action="store_true", help="write inventory.md from the manifest")
    ap.add_argument("--check-render", action="store_true", help="fail if inventory.md is stale")
    ap.add_argument("--probe", action="store_true", help="write the sanitized observed-state report")
    args = ap.parse_args()
    if args.self_test:
        return _self_test()
    if args.probe:
        p = write_report()
        print(f"wrote {p}")
        return 0
    m = load_manifest()
    if args.render:
        DOC.write_text(render_md(m))
        print(f"wrote {DOC}")
        return 0
    if args.check_render:
        fresh = render_md(m)
        cur = DOC.read_text() if DOC.exists() else ""
        if fresh != cur:
            print("✗ inventory.md is stale — run: python3 scripts/env_inventory.py --render")
            return 1
        print("✓ inventory.md in sync")
        return 0
    ap.error("no action given")
    return 2


if __name__ == "__main__":
    sys.exit(main())
