#!/usr/bin/env python3
"""setup_audit.py — khenrix-audit engine: cross-CLI setup inventory + mechanical checks.

Read-only EXCEPT the ledger-* subcommands (atomic writes, sole ledger writer).
Stdlib only. Hermetic: every walk roots at --home-root / --repo-root; the clock
is injected via --now so checks are pure functions of their inputs.

Spec: docs/superpowers/specs/2026-07-30-khenrix-audit-design.md
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
CLIS = ("claude", "codex", "agy")
PROVENANCE = ("loaded", "catalog", "source", "rendered-artifact")
# Verified harness semantics per CLI. A check that depends on an unverified
# axis emits informational findings only for that CLI (fail closed, spec §4.1).
SEMANTICS = {
    "claude": {"precedence_verified": True, "namespacing": True, "dedupe_rule": True,
               "source_ref": "code.claude.com/docs (deep-research 2026-07-30, 16 claims)"},
    "codex":  {"precedence_verified": False, "namespacing": False, "dedupe_rule": False,
               "source_ref": "unverified — establish from ~/.cache/khenrix-utils/cli-sources/codex"},
    "agy":    {"precedence_verified": False, "namespacing": False, "dedupe_rule": False,
               "source_ref": "unverified — establish via live probes"},
}


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def sid(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:12]


def item(cli: str, scope: str, kind: str, name: str, source_path: str,
         provenance: str, effective_state: str = "enabled", **meta) -> dict:
    assert provenance in PROVENANCE, provenance
    return {"id": sid(cli, scope, kind, name), "cli": cli, "scope": scope,
            "kind": kind, "name": name, "source_path": source_path,
            "provenance": provenance, "effective_state": effective_state,
            "meta": meta}


# --- redaction ------------------------------------------------------------
# Values never leave the process unredacted; vhash() keeps equality comparable.
_SECRET_KEY = re.compile(r"(token|secret|key|cred|password|passwd|cookie|auth|session|bearer)", re.I)
_SECRET_VALUE = [  # shapes mirrored from scripts/lib/checks.py SECRET_FAIL
    re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ghp_[0-9A-Za-z]{36}"),
    re.compile(r"glpat-[0-9A-Za-z_-]{20,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\beyJ[0-9A-Za-z_-]{20,}"),          # JWT-ish
    re.compile(r"\bsk-[0-9A-Za-z_-]{20,}"),
]


def vhash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:8]


def looks_secret(value: str) -> bool:
    if any(p.search(value) for p in _SECRET_VALUE):
        return True
    # long single-token high-entropy-ish strings (no spaces, mixed classes)
    return (len(value) >= 24 and " " not in value
            and re.search(r"[0-9]", value) and re.search(r"[A-Za-z]", value)
            and re.search(r"[^A-Za-z0-9]|[A-Z].*[a-z]|[a-z].*[A-Z]", value) is not None)


def redact_map(d: dict) -> dict:
    return {k: {"redacted": True, "vhash": vhash(str(v))} for k, v in d.items()}


def redact_url(u: str) -> str:
    from urllib.parse import urlsplit, urlunsplit
    try:
        p = urlsplit(u)
    except ValueError:
        return "<redacted-url>"
    netloc = ("<redacted>@" + p.netloc.split("@", 1)[1]) if "@" in p.netloc else p.netloc
    return urlunsplit((p.scheme, netloc, p.path, "<redacted>" if p.query else "", ""))


def redact_argv(args: list) -> list:
    out = []
    for i, a in enumerate(args):
        a = str(a)
        prev = str(args[i - 1]) if i else ""
        if looks_secret(a) or (_SECRET_KEY.search(prev) and prev.startswith("-")):
            out.append({"redacted": True, "vhash": vhash(a)})
        elif "://" in a:
            out.append(redact_url(a))
        else:
            out.append(a)
    return out


def scan_artifact_text(text: str) -> list:
    """Final gate before any artifact write: token-shaped strings that survived
    sanitization. Non-empty ⇒ the writer must fail closed."""
    return sorted({m.group(0)[:12] + "…" for p in _SECRET_VALUE for m in p.finditer(text)})


def build_inventory(home: Path, repo: Path | None, git_root: Path | None) -> dict:
    """Walk every surface. Walkers are added by later tasks; each is wrapped so a
    crash records a discovery error instead of silently returning nothing."""
    items: list[dict] = []
    errors: list[str] = []
    for walker in WALKERS:
        try:
            items.extend(walker(home, repo, git_root))
        except Exception as e:  # noqa: BLE001 — a silent empty list is the worse bug
            errors.append(f"{walker.__name__}: {type(e).__name__}: {e}")
    return {"schema_version": SCHEMA_VERSION, "items": items, "errors": errors}


WALKERS: list = []  # populated by walker tasks


# --- walkers: claude ------------------------------------------------------
_FM_NAME = re.compile(r"^name:\s*(.+)$", re.M)
_FM_DESC = re.compile(r"^description:\s*(?:>-|>|\|)?\s*\n?((?:.|\n)*?)(?=\n[a-z][a-z-]*:|\Z)", re.M)


def read_frontmatter(p: Path) -> dict:
    try:
        t = p.read_text(errors="replace")
    except OSError:
        return {}
    if not t.startswith("---"):
        return {}
    head = t[3:t.find("\n---", 3)]
    name = _FM_NAME.search(head)
    desc = _FM_DESC.search(head)
    return {"name": name.group(1).strip() if name else p.parent.name,
            "description": re.sub(r"\s+", " ", desc.group(1)).strip() if desc else "",
            "body_lines": t.count("\n")}


def _endpoint_hash(entry: dict) -> str:
    if entry.get("url"):
        return vhash(redact_url(entry["url"]))
    return vhash(canonical_json([entry.get("command", "")] + [str(a) for a in entry.get("args", [])]))


def _mcp_item(cli: str, scope: str, name: str, entry: dict, path: str) -> dict:
    return item(cli, scope, "mcp", name, path, "loaded",
                endpoint_hash=_endpoint_hash(entry),
                transport=entry.get("type", "stdio" if entry.get("command") else "http"),
                env_keys=sorted((entry.get("env") or {}).keys()),
                argv=redact_argv([entry.get("command", "")] + list(entry.get("args", []))))


def _hook_items(cli: str, scope: str, owner: str, hooks_cfg: dict, path: str) -> list:
    out = []
    for event, arr in (hooks_cfg or {}).items():
        if not isinstance(arr, list):
            continue
        for e in arr:
            for h in e.get("hooks", []):
                cmd = h.get("command", "")
                out.append(item(cli, scope, "hook", f"{owner}:{event}", path, "loaded",
                                event=event, matcher=e.get("matcher", "*"),
                                owner=owner, body_hash=vhash(cmd),
                                command_head=redact_argv(cmd.split())[:4]))
    return out


def walk_claude(home: Path, repo, git_root) -> list:
    items: list = []
    cdir = home / ".claude"
    if not cdir.exists():
        return items
    # plugins + their components (installed registry is the authority, not the cache)
    reg = cdir / "plugins" / "installed_plugins.json"
    if reg.exists():
        plugins = json.loads(reg.read_text()).get("plugins", {})
        for key, installs in plugins.items():
            pname = key.split("@", 1)[0]
            for inst in installs:
                ipath = Path(inst.get("installPath", ""))
                st = "enabled" if ipath.exists() else "load_failed"
                items.append(item("claude", inst.get("scope", "user"), "plugin", pname,
                                  str(ipath), "loaded", effective_state=st,
                                  version=inst.get("version", "unknown")))
                if not ipath.exists():
                    continue
                for smd in sorted(ipath.rglob("SKILL.md")):
                    fm = read_frontmatter(smd)
                    items.append(item("claude", "user", "skill", f"{pname}:{fm['name']}",
                                      str(smd), "loaded", plugin=pname,
                                      **({k: v for k, v in fm.items() if k != "name"})))
                for sub, kind in (("agents", "agent"), ("commands", "command")):
                    d = ipath / sub
                    if d.exists():
                        for f in sorted(d.rglob("*.md")):
                            items.append(item("claude", "user", kind,
                                              f"{pname}:{f.stem}", str(f), "loaded", plugin=pname))
                hj = ipath / "hooks" / "hooks.json"
                if hj.exists():
                    cfg = json.loads(hj.read_text())
                    items.extend(_hook_items("claude", "user", pname,
                                             cfg.get("hooks", cfg), str(hj)))
    # user-level dirs
    for sub, kind in (("skills", "skill"), ("agents", "agent"), ("commands", "command")):
        d = cdir / sub
        if d.exists():
            for f in sorted(d.rglob("SKILL.md" if kind == "skill" else "*.md")):
                fm = read_frontmatter(f) if kind == "skill" else {"name": f.stem}
                items.append(item("claude", "user", kind, fm["name"], str(f), "loaded",
                                  **({k: v for k, v in fm.items() if k != "name"})))
    # settings: hooks + permissions + skillOverrides
    for sfile in ("settings.json", "settings.local.json"):
        sp = cdir / sfile
        if not sp.exists():
            continue
        cfg = json.loads(sp.read_text())
        items.extend(_hook_items("claude", "user", f"<{sfile}>", cfg.get("hooks", {}), str(sp)))
        for rule in (cfg.get("permissions", {}) or {}).get("allow", []) + \
                    (cfg.get("permissions", {}) or {}).get("deny", []):
            items.append(item("claude", "user", "permission-rule", str(rule), str(sp), "loaded"))
        for skill, state in (cfg.get("skillOverrides") or {}).items():
            items.append(item("claude", "user", "setting-skilloverride", skill, str(sp),
                              "loaded", state=state))
    # MCP: global + per-project (~/.claude.json)
    cj = home / ".claude.json"
    if cj.exists():
        cfg = json.loads(cj.read_text())
        for n, e in (cfg.get("mcpServers") or {}).items():
            items.append(_mcp_item("claude", "user", n, e, str(cj)))
        for proj, pv in (cfg.get("projects") or {}).items():
            for n, e in ((pv or {}).get("mcpServers") or {}).items():
                items.append(_mcp_item("claude", f"project:{proj}", n, e, str(cj)))
    return items


WALKERS.append(walk_claude)


def cmd_inventory(args) -> int:
    home = Path(args.home_root)
    repo = Path(args.repo_root) if args.repo_root else None
    inv = build_inventory(home, repo, Path(args.git_root) if args.git_root else None)
    out = json.dumps(inv, indent=1, sort_keys=True)
    if args.out:
        Path(args.out).write_text(out)
    else:
        print(out)
    return 0


def add_common(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--home-root", default=str(Path.home()))
    ap.add_argument("--repo-root", default=None,
                    help="canonical khenrix-utils checkout (validated before any repo write)")
    ap.add_argument("--git-root", default=None, help="projects root (default <home>/git)")
    ap.add_argument("--now", default=None, help="ISO8601 audit time (injected clock)")
    ap.add_argument("--out", default=None)


def now_utc(args) -> str:
    return args.now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="khenrix-audit engine")
    ap.add_argument("--self-test", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("inventory", "findings", "ledger-add", "ledger-expire"):
        add_common(sub.add_parser(name))
    args = ap.parse_args(argv)
    if args.self_test:
        return _self_test()
    if args.cmd == "inventory":
        return cmd_inventory(args)
    if args.cmd is None:
        ap.print_help()
        return 2
    print(f"{args.cmd}: implemented in a later task", file=sys.stderr)
    return 2


def _self_test() -> int:
    ok = [("sid stable", sid("a", "b") == sid("a", "b")),
          ("canonical sorted", canonical_json({"b": 1, "a": 2}).startswith('{"a"'))]
    for label, passed in ok:
        print(f"  {'PASS' if passed else 'FAIL'}  {label}")
    return 0 if all(p for _, p in ok) else 1


if __name__ == "__main__":
    sys.exit(main())
