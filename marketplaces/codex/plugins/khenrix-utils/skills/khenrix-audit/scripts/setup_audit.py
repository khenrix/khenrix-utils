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
# Declared-server platform gate (capabilities.toml `platform =`) vs the machine
# actually running the audit — e.g. [mcp_servers.1password] platform="windows"
# must not be flagged declared-not-live on Linux. ctx["platform"] overrides for tests.
_PLATFORM = {"linux": "linux", "darwin": "darwin", "win32": "windows"}.get(sys.platform, sys.platform)
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
_FM_VENDORED = re.compile(r"^vendored_from:\s*(.+)$", re.M)


def read_frontmatter(p: Path) -> dict:
    # Total function: every caller does fm["name"] unconditionally, so both
    # early-return paths (unreadable file, no frontmatter block) must still
    # supply a usable dict — an empty {} here raises KeyError deep in a
    # walker's try/except Exception, which swallows it into a discovery error
    # and silently blanks the whole CLI's inventory while phases report "complete".
    no_frontmatter = {"name": p.parent.name, "description": "", "body_lines": 0}
    try:
        t = p.read_text(errors="replace")
    except OSError:
        return no_frontmatter
    if not t.startswith("---"):
        return no_frontmatter
    head = t[3:t.find("\n---", 3)]
    name = _FM_NAME.search(head)
    desc = _FM_DESC.search(head)
    vendored = _FM_VENDORED.search(head)
    out = {"name": name.group(1).strip() if name else p.parent.name,
           "description": re.sub(r"\s+", " ", desc.group(1)).strip() if desc else "",
           "body_lines": t.count("\n")}
    if vendored:
        out["vendored_from"] = vendored.group(1).strip()
    return out


def _endpoint_hash(entry: dict) -> str:
    if entry.get("url"):
        return vhash(redact_url(entry["url"]))
    return vhash(canonical_json([entry.get("command", "")] + [str(a) for a in entry.get("args", [])]))


def _mcp_item(cli: str, scope: str, name: str, entry: dict, path: str) -> dict:
    return item(cli, scope, "mcp", name, path, "loaded",
                endpoint_hash=_endpoint_hash(entry),
                transport=entry.get("type", "stdio" if entry.get("command") else "http"),
                env_keys=sorted((entry.get("env") or {}).keys()),
                # vhash-only (never the raw value) so two configs can be compared
                # for equality without either leaking — same contract as env_keys.
                env=redact_map(entry.get("env") or {}),
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
    # enabledPlugins gate: settings.local.json wins over settings.json per-key.
    # Read once, before the plugin loop, so a disabled plugin's install entry
    # still surfaces (for B9 hygiene) but contributes nothing else.
    enabled_plugins: dict = {}
    for sfile in ("settings.json", "settings.local.json"):
        sp = cdir / sfile
        if sp.exists():
            enabled_plugins.update(json.loads(sp.read_text()).get("enabledPlugins") or {})
    # plugins + their components (installed registry is the authority, not the cache)
    reg = cdir / "plugins" / "installed_plugins.json"
    if reg.exists():
        plugins = json.loads(reg.read_text()).get("plugins", {})
        for key, installs in plugins.items():
            pname = key.split("@", 1)[0]
            disabled = enabled_plugins.get(key) is False
            for inst in installs:
                ipath = Path(inst.get("installPath", ""))
                st = "disabled" if disabled else ("enabled" if ipath.exists() else "load_failed")
                items.append(item("claude", inst.get("scope", "user"), "plugin", pname,
                                  str(ipath), "loaded", effective_state=st,
                                  version=inst.get("version", "unknown")))
                if disabled or not ipath.exists():
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
                # Plugin-bundled MCP servers register as plugin:<plugin>:<server>
                # (verified via `claude mcp list` + live plugin cache, 2026-07-30).
                # Two declaration sites, checked BOTH: .mcp.json at the plugin root
                # — real shape observed on this machine is a FLAT {name: entry} map
                # (no "mcpServers" wrapper), so accept that and the wrapped form —
                # and an "mcpServers" key in .claude-plugin/plugin.json / plugin.json.
                mcp_p = ipath / ".mcp.json"
                if mcp_p.exists():
                    cfg = json.loads(mcp_p.read_text())
                    servers = cfg["mcpServers"] if isinstance(cfg.get("mcpServers"), dict) else cfg
                    for sname, entry in servers.items():
                        if isinstance(entry, dict):
                            items.append(_mcp_item("claude", "user", f"plugin:{pname}:{sname}",
                                                   entry, str(mcp_p)))
                for pj_rel in (".claude-plugin/plugin.json", "plugin.json"):
                    pj = ipath / pj_rel
                    if pj.exists():
                        cfg = json.loads(pj.read_text())
                        for sname, entry in (cfg.get("mcpServers") or {}).items():
                            if isinstance(entry, dict):
                                items.append(_mcp_item("claude", "user",
                                                       f"plugin:{pname}:{sname}",
                                                       entry, str(pj)))
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


# --- walkers: codex + agy -------------------------------------------------
def _skill_items_under(cli: str, root: Path, provenance: str, name_prefix: str = "") -> list:
    out = []
    for smd in sorted(root.rglob("SKILL.md")):
        fm = read_frontmatter(smd)
        out.append(item(cli, "user", "skill", name_prefix + fm["name"], str(smd),
                        provenance, **{k: v for k, v in fm.items() if k != "name"}))
    return out


def walk_codex(home: Path, repo, git_root) -> list:
    import tomllib
    items: list = []
    cdir = home / ".codex"
    if not cdir.exists():
        return items
    # curated CATALOG (available, never loaded) — startup_sync.rs CURATED_PLUGINS_RELATIVE_DIR
    cat = cdir / ".tmp" / "plugins"
    if cat.exists():
        items.extend(_skill_items_under("codex", cat, "catalog"))
    # installed plugin cache + user/system skills = loaded surface
    cache = cdir / "plugins" / "cache"
    if cache.exists():
        items.extend(_skill_items_under("codex", cache, "loaded"))
    skills = cdir / "skills"
    if skills.exists():
        items.extend(_skill_items_under("codex", skills, "loaded"))
    cfg_p = cdir / "config.toml"
    if cfg_p.exists():
        cfg = tomllib.loads(cfg_p.read_text())
        for n, e in (cfg.get("mcp_servers") or {}).items():
            items.append(_mcp_item("codex", "user", n, e, str(cfg_p)))
        for key in (cfg.get("plugins") or {}):
            items.append(item("codex", "user", "plugin", key.split("@", 1)[0],
                              str(cfg_p), "loaded"))
    return items


def walk_agy(home: Path, repo, git_root) -> list:
    items: list = []
    gdir = home / ".gemini" / "config"
    if not gdir.exists():
        return items
    plugs = gdir / "plugins"
    if plugs.exists():
        for pdir in sorted(p for p in plugs.iterdir() if p.is_dir()):
            items.append(item("agy", "user", "plugin", pdir.name, str(pdir), "loaded"))
            items.extend(_skill_items_under("agy", pdir, "loaded", f"{pdir.name}:"))
    mcp_p = gdir / "mcp_config.json"
    if mcp_p.exists():
        cfg = json.loads(mcp_p.read_text())
        for n, e in (cfg.get("mcpServers", cfg) or {}).items():
            if isinstance(e, dict):
                items.append(_mcp_item("agy", "user", n, e, str(mcp_p)))
    return items


WALKERS.append(walk_codex)
WALKERS.append(walk_agy)


# --- walkers: projects + canonical repo -----------------------------------
MANAGED_BLOCK = re.compile(r"<!-- khenrix-managed:begin -->(.*?)<!-- khenrix-managed:end -->",
                           re.S)
INSTRUCTION_FILES = ("CLAUDE.md", "AGENTS.md", "GEMINI.md")


def resolve_repo_root(candidate) -> Path | None:
    """A repo write target must be the CANONICAL checkout — never a rendered or
    installed copy (render.py copies capabilities.toml into every plugin)."""
    if candidate is None:
        return None
    c = Path(candidate)
    if all((c / m).exists() for m in (".git", "capabilities.toml", "shared/skills")):
        return c
    return None


def walk_projects(home: Path, repo, git_root) -> list:
    items: list = []
    groot = Path(git_root) if git_root else home / "git"
    if not groot.exists():
        return items
    for proj in sorted(p for p in groot.iterdir() if p.is_dir() and not p.is_symlink()):
        scope = f"project:{proj.name}"
        is_ku = resolve_repo_root(proj) is not None
        if is_ku:  # canonical checkout: sources + rendered copies, tagged, never "loaded"
            for smd in sorted((proj / "shared/skills").rglob("SKILL.md")):
                fm = read_frontmatter(smd)
                items.append(item("all", scope, "skill",
                                  f"{proj.name}:shared:{fm['name']}", str(smd), "source", **{
                                      k: v for k, v in fm.items() if k != "name"}))
            for smd in sorted((proj / "marketplaces").rglob("SKILL.md")):
                fm = read_frontmatter(smd)
                cli = smd.relative_to(proj / "marketplaces").parts[0]
                items.append(item("all", scope, "skill",
                                  f"{proj.name}:rendered:{cli}:{fm['name']}", str(smd),
                                  "rendered-artifact"))
        mcp_p = proj / ".mcp.json"
        if mcp_p.exists():
            cfg = json.loads(mcp_p.read_text())
            for n, e in (cfg.get("mcpServers") or {}).items():
                mi = _mcp_item("claude", scope, n, e, str(mcp_p))
                items.append(mi)
        cdir = proj / ".claude"
        if cdir.exists() and not is_ku:
            skills_dir = cdir / "skills"
            if skills_dir.exists():
                for smd in sorted(skills_dir.rglob("SKILL.md")):
                    fm = read_frontmatter(smd)
                    items.append(item("claude", scope, "skill", f"{proj.name}:{fm['name']}",
                                      str(smd), "loaded",
                                      **{k: v for k, v in fm.items() if k != "name"}))
        for name in INSTRUCTION_FILES:
            f = proj / name
            if f.exists():
                text = f.read_text(errors="replace")
                m = MANAGED_BLOCK.search(text)
                items.append(item("all", scope, "instruction-file", f"{proj.name}:{name}",
                                  str(f), "loaded", chars=len(text),
                                  managed_block_hash=vhash(m.group(1)) if m else None))
    return items


WALKERS.append(walk_projects)


# --- findings framework ---------------------------------------------------
CONSEQUENCE_RANK = {"silent-capability-loss": 5, "wrong-tool-fires": 4,
                    "state-divergence": 3, "cost": 2, "hygiene": 1}
CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}
# Which SEMANTICS axis each rule leans on; absent = portable (no gating).
RULE_NEEDS = {"B1": "precedence_verified", "B2": "namespacing", "B3": "dedupe_rule"}


def finding(rule, rule_version, cli, scope, kind, subjects, consequence,
            confidence, justification, evidence, remediation, note="",
            not_evaluated: bool = False) -> dict:
    if kind == "mcp" and justification == "cost":
        raise ValueError("MCP findings may never be cost-justified (spec constraint 1)")
    subjects = sorted(subjects)
    ident = canonical_json({"rule": rule, "rule_version": rule_version, "cli": cli,
                            "scope": scope, "kind": kind, "subjects": subjects})
    # not_evaluated=True means the engine had no input to judge this axis
    # (missing --repo-root / --tokens-file) — not a confirmed problem, so --check
    # must not gate a build on it any more than it gates on unverified-semantics
    # findings below. Explicit flag, not a note-text sniff: a crashed check's note
    # also mentions "NOT EVALUATED" but must still gate --check (see run_checks).
    informational = not_evaluated
    need = RULE_NEEDS.get(rule)
    if need and cli in SEMANTICS and not SEMANTICS[cli][need]:
        informational = True
        note = (note + " " if note else "") + f"semantics unverified for {cli}"
    return {"id": sid(ident), "slug": f"{rule.lower()}.{kind}." + "--".join(
                s.replace(":", "-").lower()[:40] for s in subjects[:2]),
            "rule": rule, "rule_version": rule_version, "cli": cli, "scope": scope,
            "kind": kind, "subjects": subjects,
            "consequence": consequence, "confidence": confidence,
            "severity": CONSEQUENCE_RANK[consequence] * 10 + CONFIDENCE_RANK[confidence],
            "justification": justification, "evidence": evidence,
            "fingerprint": vhash(canonical_json(evidence)),
            "remediation": remediation, "informational": informational, "note": note}


CHECKS: list = []  # populated by check tasks


# --- checks: B1-B3 --------------------------------------------------------
def _loaded(inv, kind=None, cli=None):
    return [i for i in inv["items"] if i["provenance"] == "loaded"
            and (kind is None or i["kind"] == kind)
            and (cli is None or i["cli"] == cli)]


def check_b1_name_collisions(inv, ctx) -> list:
    """Same bare skill name reachable from two owners (plugin skills are reachable
    at their bare name too — verified Claude semantics)."""
    out = []
    by_bare: dict = {}
    for s in _loaded(inv, "skill"):
        bare = s["name"].split(":")[-1]
        by_bare.setdefault((s["cli"], bare), []).append(s)
    for (cli, bare), group in sorted(by_bare.items()):
        owners = sorted({g["name"] for g in group})
        if len(owners) > 1:
            out.append(finding("B1", 1, cli, "user", "skill", owners,
                               "wrong-tool-fires", "high", "correctness",
                               {"bare_name": bare,
                                "paths": sorted(g["source_path"] for g in group)},
                               ["narrow one description (rung 1)",
                                "disable one plugin (rung 2)"]))
    return out


def check_b2_bare_key_refs(inv, ctx) -> list:
    """A permission rule / hook matcher naming a plugin-namespaced MCP server by its
    BARE key silently never matches (verified Claude semantics)."""
    out = []
    for cli in CLIS:
        plugin_servers = [m for m in _loaded(inv, "mcp", cli)
                          if m["name"].startswith("plugin:")]
        if not plugin_servers:
            continue
        bare_names = {m["name"].split(":")[-1] for m in plugin_servers}
        refs = _loaded(inv, "permission-rule", cli) + _loaded(inv, "hook", cli)
        for r in refs:
            probe = r["meta"].get("matcher", "") if r["kind"] == "hook" else r["name"]
            for bare in sorted(bare_names):
                # bare mcp__<server>__ style reference without the plugin_ prefix.
                # Per-match, not whole-string: an unrelated namespaced reference
                # elsewhere in the same probe must not suppress a genuine bare hit.
                if re.search(rf"mcp__(?!plugin_){re.escape(bare)}(__|\b)", probe):
                    out.append(finding("B2", 1, cli, r["scope"], r["kind"],
                                       [r["name"], f"server:{bare}"],
                                       "silent-capability-loss", "high", "correctness",
                                       {"reference": probe, "config": r["source_path"],
                                        "expected_prefix": "mcp__plugin_<plugin>_<server>__"},
                                       ["rewrite the matcher to the namespaced form"]))
    return out


def check_b3_endpoint_dupes(inv, ctx) -> list:
    """Same normalized endpoint reachable at two scopes — the lower-precedence copy
    is silently dropped (plugin servers dedupe BY ENDPOINT)."""
    out = []
    by_ep: dict = {}
    for m in _loaded(inv, "mcp"):
        by_ep.setdefault((m["cli"], m["meta"].get("endpoint_hash")), []).append(m)
    for (cli, ep), group in sorted(by_ep.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
        if ep and len(group) > 1:
            out.append(finding("B3", 1, cli, "multi", "mcp",
                               sorted(f'{g["scope"]}/{g["name"]}' for g in group),
                               "silent-capability-loss", "medium", "correctness",
                               {"endpoint_hash": ep,
                                "configs": sorted(g["source_path"] for g in group)},
                               ["remove the lower-precedence duplicate (confirmed)"]))
    return out


CHECKS.extend([check_b1_name_collisions, check_b2_bare_key_refs, check_b3_endpoint_dupes])


# --- checks: B4-B5 (drift) ------------------------------------------------
def load_declared(repo_root) -> dict:
    """Declared capabilities from the CANONICAL capabilities.toml. Vocabulary kept
    aligned with reconcile.py's status output (EXTRA / missing)."""
    import tomllib
    if not repo_root:
        return {}
    with open(Path(repo_root) / "capabilities.toml", "rb") as f:
        caps = tomllib.load(f)
    return {"mcp": {n: {"platform": e.get("platform"), "clis": e.get("clis")}
                    for n, e in (caps.get("mcp_servers") or {}).items()},
            "docs_mcp": {cli: list(v) if isinstance(v, dict) else v
                         for cli, v in (caps.get("docs_mcp") or {}).items()}}


def _withheld(entry: dict, cli: str, platform: str) -> bool:
    """True when capabilities.toml declares this server but not for THIS cli/host.

    Two allow-lists read the same way: `platform` (the machine cannot launch it) and
    `clis` (this CLI's MCP client cannot run it — see [mcp_servers.vercel]). Under either,
    "declared and not live here" is the intended state, so reporting it as drift would be
    a permanent false positive whose remediation ("run khenrix-setup to add it") is the
    action the gate exists to prevent.

    A MALFORMED `clis` IS TREATED AS NO GATE, deliberately unlike reconcile.py, which
    refuses outright. This engine only reports — it never writes a live config — and a
    check that raises takes the other nine down with it. Ignoring the gate errs toward
    SAYING something ("declared-not-live"), which is visible; honouring an unreadable gate
    would silently suppress a finding. reconcile.py still refuses at apply time, so a typo
    cannot reach a live config through either path.
    """
    if entry.get("platform") and entry["platform"] != platform:
        return True
    gate = entry.get("clis")
    return isinstance(gate, list) and cli not in gate


def check_b4_drift(inv, ctx) -> list:
    out = []
    decl = load_declared(ctx.get("repo_root"))
    if not decl:
        return [finding("B4", 1, "all", "repo", "check-error", ["capabilities.toml"],
                        "state-divergence", "low", "drift",
                        {"error": "no canonical repo root"}, [],
                        note="NOT EVALUATED — pass --repo-root", not_evaluated=True)]
    platform = ctx.get("platform", _PLATFORM)
    for cli in CLIS:
        live = {m["name"]: m for m in _loaded(inv, "mcp", cli) if m["scope"] == "user"}
        if not live and not any(i["cli"] == cli for i in inv["items"]):
            continue  # CLI absent on this machine — not drift
        for name in sorted(decl["mcp"]):
            withheld = _withheld(decl["mcp"][name], cli, platform)
            if name not in live:
                # The gates apply ONLY to the declared-not-live direction: a windows-only
                # server absent on Linux, or one withheld from this CLI, isn't drift. A
                # live server that is undeclared/managed-absent is still reportable
                # regardless (see the loop below, which never consults `decl`).
                if withheld:
                    continue
                out.append(finding("B4", 1, cli, "user", "mcp", [f"{cli}/{name}"],
                                   "state-divergence", "medium", "drift",
                                   {"direction": "declared-not-live"},
                                   ["run khenrix-setup to add it"]))
            elif withheld:
                # The state this gate exists to end, and the one the loop below cannot
                # see: `set(live) - set(decl)` never contains a DECLARED name, so a server
                # withheld from this CLI yet still live here was reported by nothing.
                # That is precisely how agy ran for weeks with a vercel entry that stopped
                # every headless run from reaching a model.
                out.append(finding("B4", 2, cli, "user", "mcp", [f"{cli}/{name}"],
                                   "state-divergence", "high", "drift",
                                   {"direction": "withheld-but-live"},
                                   ["remove from live config (khenrix-setup is additive "
                                    "and will not); see the gate's comment in "
                                    "capabilities.toml for the measurement"]))
        for name in sorted(set(live) - set(decl["mcp"])):
            pol = ctx.get("policies", {}).get(f"mcp:{name}")
            if pol and pol.get("desired_state") == "managed-absent":
                out.append(finding("B4", 2, cli, "user", "mcp", [f"{cli}/{name}"],
                                   "state-divergence", "high", "drift",
                                   {"direction": "managed-absent-but-live",
                                    "reason": pol.get("reason", "")},
                                   ["remove from live config (confirmed, restore bundle first)"]))
            elif not name.startswith("plugin:"):
                # A plugin-bundled MCP server ("plugin:<plugin>:<server>") is
                # structurally undeclarable in capabilities.toml, which only
                # names top-level servers — flagging it "unmanaged" would be a
                # permanent false positive, not a drift to fix.
                out.append(finding("B4", 1, cli, "user", "mcp", [f"{cli}/{name}"],
                                   "state-divergence", "low", "drift",
                                   {"direction": "unmanaged"},
                                   ["declare it in capabilities.toml, or leave as machine-specific"],
                                   note="unmanaged extra — reported once, deliberately preserved"))
    return out


def check_b5_cross_cli(inv, ctx) -> list:
    out = []
    decl = load_declared(ctx.get("repo_root"))
    if not decl:
        return [finding("B5", 1, "all", "repo", "check-error", ["capabilities.toml"],
                        "state-divergence", "low", "drift",
                        {"error": "no canonical repo root"}, [],
                        note="NOT EVALUATED — pass --repo-root", not_evaluated=True)]
    platform = ctx.get("platform", _PLATFORM)
    present_clis = [c for c in CLIS if any(i["cli"] == c for i in inv["items"])]
    for name in sorted(decl["mcp"]):
        have = {c for c in present_clis
                if any(m["name"] == name for m in _loaded(inv, "mcp", c))}
        # A CLI the server is withheld from is not "missing" it — cross-CLI parity is
        # measured over the CLIs the declaration actually covers, or B5 would report
        # every gated server as a hole on exactly the CLI that must not have it.
        eligible = [c for c in present_clis if not _withheld(decl["mcp"][name], c, platform)]
        missing = set(eligible) - have
        if have and missing:
            for cli in sorted(missing):
                out.append(finding("B5", 1, cli, "user", "mcp", [f"{cli}/{name}"],
                                   "state-divergence", "medium", "drift",
                                   {"present_on": sorted(have)},
                                   [f"add {name} on {cli} via khenrix-setup"]))
    return out


CHECKS.extend([check_b4_drift, check_b5_cross_cli])


# --- check: B6 trigger-surface overlap ------------------------------------
_STOP = set("""a an the and or of to in for on with when use used using this that it its is are
be as by from at into if then than you your we our not do does doing what which who how why can
could should may might will would about over under before after each per any all more most other
some such only own same so also very just too s t skill skills claude code user users want wants
asks ask repo file files codebase check tool tools""".split())


def trigger_surface(desc: str) -> str:
    out = []
    m = re.search(r"triggers?\s*(?:on)?\s*:(.*)$", desc, re.I | re.S)
    if m:
        out.append(m.group(1))
    out += re.findall(r'"([^"]{3,60})"', desc)
    out += re.findall(r"[Uu]se (?:this )?(?:skill )?when ([^.]{5,160})", desc)
    return " ".join(out)


def _toks(s: str) -> list:
    return [w for w in re.findall(r"[a-z][a-z0-9-]{2,}", s.lower()) if w not in _STOP]


def overlap_pairs(skills: list, top_k: int = 15) -> list:
    import itertools
    import math
    from collections import Counter
    docs = {}
    phrases = {}
    for s in skills:
        ts = trigger_surface(s["meta"].get("description", ""))
        if ts.strip():
            docs[s["name"]] = Counter(_toks(ts))
            phrases[s["name"]] = set(re.findall(r'"([^"]{3,60})"',
                                                s["meta"].get("description", "")))
    docs = {k: v for k, v in docs.items() if v}
    n = len(docs)
    if n < 2:
        return []
    df = Counter()
    for c in docs.values():
        df.update(c.keys())
    idf = {w: math.log(n / (1 + d)) + 1 for w, d in df.items()}
    vec = {}
    for k, c in docs.items():
        v = {w: (1 + math.log(cnt)) * idf[w] for w, cnt in c.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        vec[k] = {w: x / norm for w, x in v.items()}
    pairs = []
    for x, y in itertools.combinations(sorted(vec), 2):
        shared = set(vec[x]) & set(vec[y])
        if len(shared) < 2:      # require ≥2 distinct non-stopword tokens
            continue
        cos = sum(vec[x][w] * vec[y][w] for w in shared)
        exact = sorted(phrases.get(x, set()) & phrases.get(y, set()))
        if exact:                # exact shared quoted phrase: high-precision boost
            cos += 0.25
        top_shared = sorted(shared, key=lambda w: -(vec[x][w] * vec[y][w]))[:6]
        pairs.append((round(cos, 3), x, y, top_shared, exact))
    pairs.sort(reverse=True)
    return pairs[:top_k]


def check_b6_trigger_overlap(inv, ctx) -> list:
    """Nominate overlapping skill trigger surfaces WITHIN each CLI's own loaded
    corpus. Pooling all CLIs into one name-keyed corpus (the original bug) lets a
    same-named skill on a different CLI silently overwrite a genuine pair via dict
    collision in overlap_pairs — partitioning first makes overlap_pairs' name-keyed
    dicts safe."""
    out = []
    for cli in CLIS:
        skills = [s for s in _loaded(inv, "skill", cli) if s["meta"].get("description")]
        pairs = overlap_pairs(skills)
        for cos, x, y, shared, exact in pairs:
            same_plugin = x.split(":")[0] == y.split(":")[0]
            out.append(finding("B6", 1, cli, "user", "skill", [x, y],
                               "wrong-tool-fires", "low", "correctness",
                               {"cosine": cos, "shared_tokens": shared,
                                "shared_phrases": exact, "same_plugin": same_plugin,
                                "corpus_size": len(skills), "nominated": len(pairs)},
                               ["Phase C adjudication → Phase D arena/probes",
                                "rung 1 if DUPLICATE and one side is ours"],
                               note="heuristic nomination — adjudicate before acting"))
    return out


CHECKS.append(check_b6_trigger_overlap)


# --- checks: B7-B9 --------------------------------------------------------
def check_b7_budget(inv, ctx) -> list:
    tokens = ctx.get("tokens")
    window = ctx.get("context_window", 200_000)
    budget = int(window * 0.01)
    if tokens is None:
        return [finding("B7", 1, "claude", "user", "skill", ["<listing-budget>"],
                        "silent-capability-loss", "low", "correctness", {"budget": budget},
                        ["produce tokens.json via `claude plugin details` and re-run"],
                        note="NOT EVALUATED — no --tokens-file", not_evaluated=True)]
    total = sum(tokens.values())
    instr = {i["name"]: i["meta"].get("chars", 0) // 4
             for i in inv["items"] if i["kind"] == "instruction-file"}
    if total <= budget:
        return []
    payers = sorted(tokens.items(), key=lambda kv: -kv[1])
    return [finding("B7", 1, "claude", "user", "skill", ["<listing-budget>"],
                    "silent-capability-loss", "high", "correctness",
                    {"total_always_on": total, "budget": budget,
                     "over_by_pct": round(100 * (total - budget) / budget),
                     "estimator": "claude plugin details (count_tokens)",
                     "biggest_payers": payers[:5],
                     "instruction_file_estimates_chars4": instr,
                     "note": "drop order is least-invoked-first and unobservable — "
                             "reduce the total; do not predict victims"},
                    ["disable the biggest-payer plugin you use least (rung 2)",
                     "shorten khenrix-utils descriptions (rung 1, arena-gated)"])]


def check_b8_hook_collisions(inv, ctx) -> list:
    out = []
    hooks = _loaded(inv, "hook")
    by_body: dict = {}
    for h in hooks:
        by_body.setdefault((h["cli"], h["meta"]["body_hash"]), []).append(h)
    for (cli, bh), group in sorted(by_body.items()):
        owners = sorted({g["meta"]["owner"] for g in group})
        if len(owners) > 1:
            out.append(finding("B8", 1, cli, "user", "hook",
                               [f'{o}:{group[0]["meta"]["event"]}' for o in owners],
                               "state-divergence", "high", "correctness",
                               {"duplicate_body": True, "event": group[0]["meta"]["event"],
                                "configs": sorted(g["source_path"] for g in group)},
                               ["remove one copy (same command registered twice fires twice)"]))
    by_slot: dict = {}
    for h in hooks:
        by_slot.setdefault((h["cli"], h["meta"]["event"], h["meta"]["matcher"]), set()).add(
            h["meta"]["owner"])
    for (cli, event, matcher), owners in sorted(by_slot.items()):
        if len(owners) > 1:
            out.append(finding("B8", 1, cli, "user", "hook",
                               sorted(f"{o}:{event}" for o in owners),
                               "hygiene", "low", "correctness",
                               {"duplicate_body": False, "event": event, "matcher": matcher},
                               [], note="shared event slot — usually intentional composition"))
    return out


def check_b9_hygiene(inv, ctx) -> list:
    out = []
    for p in _loaded(inv, "plugin"):
        if p["meta"].get("version") == "unknown":
            out.append(finding("B9", 1, p["cli"], p["scope"], "plugin", [p["name"]],
                               "hygiene", "low", "correctness",
                               {"issue": "version unknown"}, ["reinstall pinned"]))
        if p["effective_state"] == "load_failed":
            out.append(finding("B9", 1, p["cli"], p["scope"], "plugin", [p["name"]],
                               "hygiene", "medium", "correctness",
                               {"issue": "installPath missing"}, ["reinstall or remove entry"]))
    return out


CHECKS.extend([check_b7_budget, check_b8_hook_collisions, check_b9_hygiene])


# --- checks: B10-B16 (cheap file checks) ----------------------------------
def check_b10_parse_validity(inv, ctx) -> list:
    return [finding("B10", 1, "all", "engine", "config", [err.split(":")[0]],
                    "silent-capability-loss", "high", "correctness", {"error": err},
                    ["fix or quarantine the malformed file"])
            for err in inv["errors"]]


def check_b11_dangling_refs(inv, ctx) -> list:
    out = []
    for h in _loaded(inv, "hook"):
        head = h["meta"].get("command_head") or []
        exe = head[0] if head and isinstance(head[0], str) else ""
        if exe.startswith("/") and not Path(exe).exists():
            out.append(finding("B11", 1, h["cli"], h["scope"], "hook", [h["name"]],
                               "silent-capability-loss", "high", "correctness",
                               {"missing": exe, "config": h["source_path"]},
                               ["fix the path or remove the hook"]))
    return out


def check_b12_frontmatter(inv, ctx) -> list:
    out = []
    for s in _loaded(inv, "skill"):
        desc = s["meta"].get("description", "")
        issues = []
        if not desc:
            issues.append("missing description")
        elif len(desc) > 1024:
            issues.append(f"description {len(desc)} chars (>1024)")
        if s["meta"].get("body_lines", 0) >= 500:
            issues.append(f"body {s['meta']['body_lines']} lines (>=500)")
        if issues:
            out.append(finding("B12", 1, s["cli"], s["scope"], "skill", [s["name"]],
                               "hygiene", "medium", "correctness", {"issues": issues},
                               ["fix frontmatter (repo constraint)"]))
    return out


def check_b13_managed_block_divergence(inv, ctx) -> list:
    out = []
    by_proj: dict = {}
    for i in inv["items"]:
        if i["kind"] == "instruction-file" and i["meta"].get("managed_block_hash"):
            by_proj.setdefault(i["scope"], {})[i["name"]] = i["meta"]["managed_block_hash"]
    for scope, files in sorted(by_proj.items()):
        if len(set(files.values())) > 1:
            out.append(finding("B13", 1, "all", scope, "instruction-file",
                               sorted(files), "state-divergence", "medium", "drift",
                               {"hashes": files},
                               ["re-run khenrix-setup to re-sync managed blocks"]))
    return out


def check_b14_claude_json_health(inv, ctx) -> list:
    out = []
    cj = (ctx.get("home") or Path("/nonexistent")) / ".claude.json"
    if not cj.exists():
        return out
    size = cj.stat().st_size
    stale = [p for p in (json.loads(cj.read_text()).get("projects") or {})
             if not Path(p).exists()]
    if size > 1_048_576 or stale:
        out.append(finding("B14", 1, "claude", "user", "config", ["~/.claude.json"],
                           "hygiene", "low", "correctness",
                           {"bytes": size, "stale_projects": stale[:10]},
                           ["prune stale project entries (confirmed)"]))
    return out


def check_b15_dual_path(inv, ctx) -> list:
    out = []
    by_key: dict = {}
    for s in _loaded(inv, "skill"):
        key = (s["cli"], s["name"].split(":")[-1], vhash(s["meta"].get("description", "")))
        by_key.setdefault(key, []).append(s)
    for (cli, bare, _), group in sorted(by_key.items()):
        paths = sorted({g["source_path"] for g in group})
        if len(paths) > 1:
            out.append(finding("B15", 1, cli, "user", "skill",
                               sorted(g["name"] for g in group),
                               "cost", "medium", "correctness",
                               {"paths": paths},
                               ["remove one path (double-counts the listing budget)"]))
    return out


def check_b16_vendored_source_enabled(inv, ctx) -> list:
    out = []
    plugins = {p["name"] for p in _loaded(inv, "plugin")
               if p["effective_state"] == "enabled"}
    for s in _loaded(inv, "skill"):
        src = s["meta"].get("vendored_from")
        if src and src in plugins:
            out.append(finding("B16", 1, s["cli"], s["scope"], "skill", [s["name"]],
                               "wrong-tool-fires", "high", "correctness",
                               {"vendored_from": src},
                               [f"disable {src} or drop the vendored copy"]))
    return out


CHECKS.extend([check_b10_parse_validity, check_b11_dangling_refs, check_b12_frontmatter,
               check_b13_managed_block_divergence, check_b14_claude_json_health,
               check_b15_dual_path, check_b16_vendored_source_enabled])


def run_checks(inv: dict, ctx: dict) -> list:
    out: list = []
    for check in CHECKS:
        try:
            out.extend(check(inv, ctx))
        except Exception as e:  # noqa: BLE001
            out.append(finding("ENGINE", 1, "all", "engine", "check-error",
                               [check.__name__], "silent-capability-loss", "high",
                               "correctness", {"error": f"{type(e).__name__}: {e}"},
                               ["fix the engine"], note="check crashed — NOT EVALUATED"))
    return sorted(out, key=lambda f: -f["severity"])


def engine_capabilities(ctx: dict) -> dict:
    import shutil as _sh
    return {"can_probe": _sh.which("claude") is not None,
            "can_token_count": _sh.which("claude") is not None,
            "semantics_verified_for": [c for c in CLIS if SEMANTICS[c]["precedence_verified"]],
            "writable_ledger": ctx.get("repo_root") is not None}


def write_findings(findings, inv, path, ctx) -> dict:
    doc = {"schema_version": SCHEMA_VERSION, "generated": ctx.get("now"),
           "capabilities": engine_capabilities(ctx),
           "inventory_hash": vhash(canonical_json(inv["items"])),
           "counts": {"items": len(inv["items"]), "findings": len(findings),
                      "errors": len(inv["errors"])},
           "errors": inv["errors"], "findings": findings,
           "waived": ctx.get("waived", []), "local_waivers": ctx.get("local_waivers", 0)}
    text = json.dumps(doc, indent=1, sort_keys=True)
    leaked = scan_artifact_text(text)
    if leaked:
        sys.exit(f"REFUSING to write {path}: secret-shaped strings survived "
                 f"sanitization: {leaked}")
    Path(path).write_text(text)
    return doc


# --- report ---------------------------------------------------------------
def derive_status(doc: dict) -> str:
    """Overall run status — a single word phases/counts can't lie about. `fatal`
    beats `incomplete` beats `complete-with-findings` beats `complete`: a run
    that discovered nothing AND errored is worse than one that merely errored."""
    items = doc["counts"]["items"]
    errors = doc["counts"]["errors"]
    findings = doc["findings"]
    if items == 0 and errors > 0:
        return "fatal"
    if errors > 0 or any(f["rule"] == "ENGINE" for f in findings):
        return "incomplete"
    if any(not f.get("informational") for f in findings):
        return "complete-with-findings"
    return "complete"


def render_report(doc: dict, phases: dict) -> str:
    L = [f"# Setup audit — {doc['generated']}", "",
         f"Status: {derive_status(doc)}", "",
         f"Inventory {doc['counts']['items']} items (hash {doc['inventory_hash']}), "
         f"{doc['counts']['findings']} finding(s), {doc['counts']['errors']} discovery error(s).",
         f"{doc.get('local_waivers', 0)} local waiver(s) active.", "", "## Phase coverage", ""]
    for phase, status in sorted(phases.items()):
        L.append(f"- {phase}: {status}")
    L += ["", "## Findings (by severity)", ""]
    for f in doc["findings"]:
        tag = " (informational)" if f.get("informational") else ""
        L.append(f"### [{f['severity']}] {f['slug']}{tag}")
        L.append(f"- rule {f['rule']} · {f['cli']}/{f['scope']} · {f['consequence']} "
                 f"· confidence {f['confidence']} · id `{f['id']}` fp `{f['fingerprint']}`")
        if f.get("note"):
            L.append(f"- note: {f['note']}")
        # A backtick inside evidence (e.g. a shell command finding) would close
        # the code span early and corrupt the rest of the line — swap it for a
        # single quote before embedding, since evidence is diagnostic text, not
        # something a reader needs byte-exact.
        # Short scalars first, long prose last, THEN truncate — canonical_json's
        # alphabetical key order let a long "note" push actionable scalars (e.g.
        # B7's over_by_pct/total_always_on) past the [:400] cut.
        ev = f["evidence"]
        ordered = dict(sorted(ev.items(),
                              key=lambda kv: (len(str(kv[1])) > 80, kv[0])))
        evidence_str = json.dumps(ordered, ensure_ascii=False)[:400].replace("`", "'")
        L.append(f"- evidence: `{evidence_str}`")
        for r in f["remediation"]:
            L.append(f"- rung: {r}")
        L.append("")
    L += ["## Waived (collapsed)", ""]
    for w in doc.get("waived", []):
        L.append(f"- {w['slug']} — {w['reason']}")
    return "\n".join(L) + "\n"


def write_report(doc: dict, phases: dict, report_dir: Path, machine: str) -> None:
    md = render_report(doc, phases)
    leaked = scan_artifact_text(md)
    if leaked:
        sys.exit(f"REFUSING to write report: secret-shaped strings leaked: {leaked}")
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "latest.md").write_text(md)
    runs = report_dir / "runs" / re.sub(r"[^A-Za-z0-9._-]", "-", machine)
    runs.mkdir(parents=True, exist_ok=True)
    stamp = doc["generated"].replace(":", "") + "-" + doc["inventory_hash"]
    # Never overwrite history: same generated+inventory_hash within one second
    # collides on `stamp` (e.g. two runs from a scripted loop) — walk suffixes
    # -2, -3, ... to the first free name instead of silently clobbering the
    # earlier run's .md/.json.
    if (runs / f"{stamp}.md").exists():
        n = 2
        while (runs / f"{stamp}-{n}.md").exists():
            n += 1
        stamp = f"{stamp}-{n}"
    (runs / f"{stamp}.md").write_text(md)
    (runs / f"{stamp}.json").write_text(json.dumps(doc, indent=1, sort_keys=True))
    history = sorted(runs.glob("*.md"))
    if len(history) > 10:
        print(f"note: {len(history)} run reports in {runs} — prune manually "
              f"(the engine never deletes history unasked)")


# --- ledger -----------------------------------------------------------------
def ledger_paths(ctx):
    repo = ctx.get("repo_root")
    repo_l = Path(repo) / "docs" / "setup-audit" / "ledger.json" if repo else None
    local_l = Path(ctx["home"]) / ".local" / "state" / "khenrix" / "ledger.local.json"
    return repo_l, local_l


def _read_ledger(p: Path | None) -> dict:
    if p and p.exists():
        return json.loads(p.read_text())
    return {"schema_version": SCHEMA_VERSION, "entries": {}, "policies": {}}


def load_ledger(ctx) -> dict:
    repo_l, local_l = ledger_paths(ctx)
    merged = {"entries": {}, "policies": {}}
    n_local = 0
    for p in (repo_l, local_l):
        d = _read_ledger(p)
        merged["entries"].update(d.get("entries", {}))
        merged["policies"].update(d.get("policies", {}))
        if p == local_l:
            n_local = len(d.get("entries", {}))
    merged["local_waivers"] = n_local
    merged["waived"] = []
    return merged


def apply_ledger(findings, ctx):
    entries = ctx.get("entries", {})
    now = ctx.get("now", "")
    kept = []
    for f in findings:
        e = entries.get(f["id"])
        if not e or e.get("disposition") not in ("waived", "wontfix"):
            kept.append(f)
            continue
        if e.get("fingerprint") != f["fingerprint"]:
            f["note"] = (f["note"] + " " if f["note"] else "") + \
                "waiver stale — situation changed"
            kept.append(f)
        elif e["disposition"] == "waived" and e.get("until") and e["until"] <= now:
            f["note"] = (f["note"] + " " if f["note"] else "") + \
                f"waiver expired {e['until']}"
            kept.append(f)
        else:
            ctx.setdefault("waived", []).append(
                {"id": f["id"], "slug": f["slug"], "reason": e.get("reason", "")})
    return kept


def ledger_write(path: Path, doc: dict) -> None:
    """Atomic, lock-guarded: the engine is the ONLY ledger writer across all CLIs."""
    import os
    text = json.dumps(doc, indent=1, sort_keys=True)
    leaked = scan_artifact_text(text)
    if leaked:
        sys.exit(f"REFUSING to write {path}: secret-shaped strings survived "
                 f"sanitization: {leaked}")
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(".lock")
    if lock.exists():
        sys.exit(f"ledger locked by another writer: {lock} (remove if stale)")
    lock.write_text(str(os.getpid()))
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text)
        os.replace(tmp, path)
    finally:
        lock.unlink(missing_ok=True)


def cmd_ledger_add(args) -> int:
    ctx = {"repo_root": resolve_repo_root(args.repo_root), "home": Path(args.home_root)}
    repo_l, local_l = ledger_paths(ctx)
    target = local_l if args.local else repo_l
    if target is None:
        sys.exit("ledger-add needs --repo-root (canonical checkout) or --local")
    doc = _read_ledger(target)
    if args.subject:  # a desired-state policy, keyed kind:name
        doc.setdefault("policies", {})[args.subject] = {
            "desired_state": args.desired_state, "reason": args.reason,
            "created": now_utc(args)}
    else:
        # A WAIVER THAT NAMES NO FINDING IS NOT A WAIVER. Both of these are optional at the
        # argparse layer because the policy branch above uses neither, so an empty `--id`
        # walked straight through and wrote an entry keyed by "" into a TRACKED ledger —
        # unmatchable against any finding, invisible in review as anything but noise, and
        # silently overwritten by the next one. The fingerprint is required with it because
        # that is what ties the waiver to the finding's CONTENT: an id alone waives whatever
        # later happens to carry that id.
        missing = [f for f, v in (("--id", args.id), ("--fingerprint", args.fingerprint))
                   if not (v or "").strip()]
        if missing:
            sys.exit(f"ledger-add needs {' and '.join(missing)} to record a waiver — an "
                     "entry naming no finding can never match one. Take both from the "
                     "finding you mean to waive in the `findings` output.")
        doc.setdefault("entries", {})[args.id] = {
            "disposition": args.state, "fingerprint": args.fingerprint,
            "reason": args.reason, "until": args.until,
            "created": now_utc(args), "machine": args.machine}
    ledger_write(target, doc)
    print(f"ledger: recorded in {target}")
    return 0


def cmd_ledger_expire(args) -> int:
    ctx = {"repo_root": resolve_repo_root(args.repo_root), "home": Path(args.home_root)}
    repo_l, local_l = ledger_paths(ctx)
    target = local_l if args.local else repo_l
    if target is None:
        sys.exit("ledger-expire needs --repo-root (canonical checkout) or --local")
    doc = _read_ledger(target)
    e = doc.get("entries", {}).get(args.id)
    if not e:
        sys.exit(f"no ledger entry {args.id}")
    e["until"] = now_utc(args)   # never delete — history is the audit trail
    ledger_write(target, doc)
    print(f"ledger: {args.id} expired")
    return 0


def cmd_findings(args) -> int:
    home = Path(args.home_root)
    repo = resolve_repo_root(args.repo_root)
    inv = build_inventory(home, repo, Path(args.git_root) if args.git_root else None)
    ctx = {"now": now_utc(args), "repo_root": repo, "home": home,
           "context_window": args.context_window}
    if args.tokens_file:
        ctx["tokens"] = json.loads(Path(args.tokens_file).read_text())
    ctx.update(load_ledger(ctx))
    fnd = run_checks(inv, ctx)
    fnd = apply_ledger(fnd, ctx)
    doc = write_findings(fnd, inv, args.out or "findings.json", ctx)
    print(f"{len(fnd)} finding(s) → {args.out or 'findings.json'}")
    if args.report_dir:
        n_crashed = sum(1 for f in doc["findings"] if f["rule"] == "ENGINE")
        write_report(doc, phases={
            "inventory": (f"incomplete — {len(inv['errors'])} discovery error(s)"
                          if inv["errors"] else "complete"),
            "checks": (f"incomplete — {n_crashed} check(s) crashed"
                      if n_crashed else "complete"),
            "probes": "engine does not run probes — SKILL.md Phase D",
            "ecosystem": "engine does not run discovery — SKILL.md Phase E"},
            report_dir=Path(args.report_dir), machine=args.machine)
    if args.check is not None and any(
            f["severity"] >= args.check and not f["informational"] for f in doc["findings"]):
        return 1
    return 0


def cmd_inventory(args) -> int:
    home = Path(args.home_root)
    repo = Path(args.repo_root) if args.repo_root else None
    inv = build_inventory(home, repo, Path(args.git_root) if args.git_root else None)
    out = json.dumps(inv, indent=1, sort_keys=True)
    leaked = scan_artifact_text(out)
    if leaked:
        sys.exit(f"REFUSING to emit inventory: secret-shaped strings survived "
                 f"sanitization: {leaked}")
    if args.out:
        Path(args.out).write_text(out)
    else:
        print(out)
    return 0


def add_common(ap: argparse.ArgumentParser) -> None:
    import platform  # local import per this file's idiom (see engine_capabilities, load_declared)
    ap.add_argument("--home-root", default=str(Path.home()))
    ap.add_argument("--repo-root", default=None,
                    help="canonical khenrix-utils checkout (validated before any repo write)")
    ap.add_argument("--git-root", default=None, help="projects root (default <home>/git)")
    ap.add_argument("--now", default=None, help="ISO8601 audit time (injected clock)")
    ap.add_argument("--out", default=None)
    ap.add_argument("--tokens-file", default=None,
                    help="JSON {plugin: always_on_tokens} from `claude plugin details`")
    ap.add_argument("--context-window", type=int, default=200_000)
    ap.add_argument("--report-dir", default=None,
                    help="also render a markdown report (findings command only)")
    ap.add_argument("--machine", default=platform.node(),
                    help="machine label for report history + ledger entries (sanitized)")
    ap.add_argument("--check", type=int, default=None,
                    help="exit 1 if any non-informational finding's severity >= N")


def now_utc(args) -> str:
    return args.now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def add_ledger_flags(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--id", default=None, help="finding id (ledger entries)")
    ap.add_argument("--state", default=None, help="disposition: waived | wontfix")
    ap.add_argument("--reason", default=None)
    ap.add_argument("--fingerprint", default=None)
    ap.add_argument("--until", default=None, help="ISO8601 waiver expiry")
    ap.add_argument("--subject", default=None, help="kind:name for a desired-state policy")
    ap.add_argument("--desired-state", default=None)
    # --machine is defined by add_common (called for every subcommand, including
    # ledger-add/ledger-expire, before this function runs) — redefining it here
    # would raise argparse.ArgumentError: conflicting option string.
    ap.add_argument("--local", action="store_true",
                    help="write to the per-machine ledger instead of the repo one")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="khenrix-audit engine")
    ap.add_argument("--self-test", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("inventory", "findings", "ledger-add", "ledger-expire"):
        p = sub.add_parser(name)
        add_common(p)
        if name in ("ledger-add", "ledger-expire"):
            add_ledger_flags(p)
    args = ap.parse_args(argv)
    if args.self_test:
        return _self_test()
    if args.cmd == "inventory":
        return cmd_inventory(args)
    if args.cmd == "findings":
        return cmd_findings(args)
    if args.cmd == "ledger-add":
        return cmd_ledger_add(args)
    if args.cmd == "ledger-expire":
        return cmd_ledger_expire(args)
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
