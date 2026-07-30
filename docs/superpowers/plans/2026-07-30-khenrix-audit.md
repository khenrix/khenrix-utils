# khenrix-audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `khenrix-audit` skill — a cross-CLI setup conflict & redundancy audit (spec: `docs/superpowers/specs/2026-07-30-khenrix-audit-design.md`).

**Architecture:** A stdlib-only deterministic engine (`setup_audit.py`, single file per repo idiom) walks Claude/Codex/agy + project scopes into a sanitized inventory, runs mechanical checks B1–B16 into fingerprinted findings, and owns the decision ledger. A thin per-CLI SKILL.md (templated, like khenrix-setup) drives model phases: adjudication, arena trigger evidence, TTL-cached ecosystem research, deterministic report, guided apply.

**Tech Stack:** Python 3.11+ stdlib only (`tomllib`, `json`, `hashlib`, `pathlib`). pytest for repo tests (via the Makefile `RUN_PYTEST` macro). No new dependencies.

## Global Constraints

- Python stdlib only — no pip installs anywhere (CLAUDE.md).
- SKILL.md frontmatter: `name` lowercase `[a-z0-9-]{1,64}`, `description` ≤1024 chars, body <500 lines (`render.py --check`).
- Engine is read-only EXCEPT `ledger-add` / `ledger-expire` subcommands (atomic `os.replace`).
- Redaction is an inventory property: secret VALUES never stored; artifact writes secret-scan and fail closed.
- Only `provenance == "loaded"` items enter collision/overlap/budget checks; `catalog` / `source` / `rendered-artifact` never do.
- Checks gated by the `SEMANTICS` table: unverified CLI semantics → `informational` findings only, never a clean pass; unsupported surface → `NOT EVALUATED`.
- Never justify an MCP finding with context cost (`justification == "cost"` + `kind == "mcp"` is forbidden by construction).
- The engine never writes to `marketplaces/**` or plugin caches; repo writes require a validated canonical checkout.
- Never edit `marketplaces/.../SKILL.md` by hand — always the shared template + `make render`.
- Skill changes require eval receipts across all three providers before commit (`make precommit` gate).
- Bash steps in SKILL.md bodies: single commands only, no `&&`/`||`/`;` chaining (house style).
- Commit messages end with: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

**File map (whole plan):**

| Path | Role |
|------|------|
| `shared/skill-templates/khenrix-audit/SKILL.md.tmpl` | Thin dispatcher skill body (per-CLI via `[skill_facts.khenrix-audit.*]`) |
| `shared/skill-templates/khenrix-audit/scripts/setup_audit.py` | The engine (single file, sections in this order: constants → util → redaction → walkers → findings framework → checks → ledger → report → CLI) |
| `shared/skill-templates/khenrix-audit/references/{checks,remediation-ladder,probe-protocol,ecosystem-evidence}.md` | Progressive-disclosure docs the SKILL.md points at |
| `tests/test_setup_audit.py` | Hermetic pytest suite (fixture homes under `tmp_path`) |
| `scripts/eval_trigger.py` | Gains `--arena` mode |
| `capabilities.toml` | `[[skills]] khenrix-audit per_cli`, `[skill_facts.khenrix-audit.*]`, khenrix-setup description fix |
| `scripts/render.py` | Templated-skill sibling-dir copying (scripts/, references/) |
| `Makefile` | `audit-test` target wired into `verify` |
| `.gitignore` | `docs/setup-audit/runs/` |
| `evals/khenrix-audit/{evals.json,triggers.json}` | Eval gate inputs |
| `docs/setup-audit/ledger.json` | Seeded ledger (google-drive `managed-absent`) |

---

### Task 1: Engine scaffold, inventory schema, test harness, Makefile wiring

**Files:**
- Create: `shared/skill-templates/khenrix-audit/scripts/setup_audit.py`
- Create: `tests/test_setup_audit.py`
- Modify: `Makefile` (add `audit-test`, wire into `verify`)

**Interfaces:**
- Produces: `item(cli, scope, kind, name, source_path, provenance, effective_state="enabled", **meta) -> dict`; `canonical_json(obj) -> str`; `sid(*parts) -> str` (stable 12-hex id); `SCHEMA_VERSION = 1`; CLI entrypoint `main(argv) -> int` with subcommands `inventory`, `findings`, `ledger-add`, `ledger-expire` and flags `--home-root`, `--repo-root`, `--now` (ISO timestamp injection), `--self-test`.
- Consumes: nothing.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_setup_audit.py
"""Hermetic tests for the khenrix-audit engine. No real HOME, no network."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "shared" / "skill-templates" / "khenrix-audit" / "scripts" / "setup_audit.py"

spec = importlib.util.spec_from_file_location("setup_audit", ENGINE)
sa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sa)


def test_item_shape_and_defaults():
    it = sa.item("claude", "user", "skill", "chunk-map",
                 "/x/skills/chunk-map/SKILL.md", "loaded", description="d")
    assert it["cli"] == "claude" and it["scope"] == "user"
    assert it["kind"] == "skill" and it["provenance"] == "loaded"
    assert it["effective_state"] == "enabled"
    assert it["meta"]["description"] == "d"
    assert it["id"] == sa.sid("claude", "user", "skill", "chunk-map")


def test_sid_stable_and_order_sensitive():
    assert sa.sid("a", "b") == sa.sid("a", "b")
    assert sa.sid("a", "b") != sa.sid("b", "a")
    assert len(sa.sid("a")) == 12


def test_canonical_json_sorts_keys():
    assert sa.canonical_json({"b": 1, "a": [2, 1]}) == '{"a": [2, 1], "b": 1}'


def test_cli_inventory_runs_on_empty_home(tmp_path, capsys):
    (tmp_path / "home").mkdir()
    rc = sa.main(["inventory", "--home-root", str(tmp_path / "home"),
                  "--out", str(tmp_path / "inv.json")])
    assert rc == 0
    inv = json.loads((tmp_path / "inv.json").read_text())
    assert inv["schema_version"] == 1
    assert inv["items"] == []
    assert isinstance(inv["errors"], list)
```

- [ ] **Step 2: Run it to verify failure**

Run: `python3 -m pytest -q tests/test_setup_audit.py`
Expected: FAIL (engine file does not exist → `spec.loader` error).

- [ ] **Step 3: Write the engine scaffold**

```python
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
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m pytest -q tests/test_setup_audit.py`
Expected: 4 passed.

- [ ] **Step 5: Wire `audit-test` into the Makefile**

In `Makefile`: add `audit-test` to the `.PHONY` line, add near `DOCTOR_TESTS`:

```make
AUDIT_TESTS := tests/test_setup_audit.py
```

Add the target after `doctor-test` and add `audit-test` to the `verify` prerequisite list (`verify: render doctor-test audit-test bats-test council-test eval-test`):

```make
# Hermetic engine tests for the khenrix-audit skill — same stance as doctor-test:
# a verifier whose own tests never run decays into a false assurance.
audit-test: ## Hermetic tests for the khenrix-audit engine (no token cost)
	$(call RUN_PYTEST,$(AUDIT_TESTS))
```

Run: `make audit-test`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add shared/skill-templates/khenrix-audit/scripts/setup_audit.py tests/test_setup_audit.py Makefile
git commit -m "feat(khenrix-audit): engine scaffold + inventory schema + audit-test gate

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Redaction layer

**Files:**
- Modify: `shared/skill-templates/khenrix-audit/scripts/setup_audit.py` (add a `# --- redaction ---` section after the util section)
- Test: `tests/test_setup_audit.py`

**Interfaces:**
- Produces: `redact_map(d: dict) -> dict` (values → `{"redacted": True, "vhash": <8hex>}`), `redact_url(u) -> str`, `redact_argv(args: list[str]) -> list[str]`, `looks_secret(value: str) -> bool`, `scan_artifact_text(text: str) -> list[str]` (offending matches; used by every artifact writer to fail closed), `vhash(value) -> str` (equality-comparable truncated hash).
- Consumes: Task 1 helpers.

Adapted from `scripts/env_inventory.py` (`_redact_url`, `_sanitize_map`) — copied rather than imported because the engine must be self-contained inside a rendered plugin; keep the regexes textually identical to env_inventory's where they overlap.

- [ ] **Step 1: Write the failing tests**

```python
def test_redact_map_keeps_names_drops_values():
    r = sa.redact_map({"GITHUB_TOKEN": "ghp_" + "a" * 36, "PORT": "8080"})
    assert set(r) == {"GITHUB_TOKEN", "PORT"}
    assert r["GITHUB_TOKEN"]["redacted"] is True
    assert "ghp_" not in json.dumps(r)
    # vhash supports equality comparison without the value
    assert r["GITHUB_TOKEN"]["vhash"] == sa.vhash("ghp_" + "a" * 36)


def test_redact_url_strips_userinfo_and_query():
    u = sa.redact_url("https://user:pw@h.example/p?token=abc")
    assert "pw" not in u and "abc" not in u and "h.example/p" in u


def test_redact_argv_masks_secret_shaped_values():
    argv = ["serve", "--token", "xoxb-123456789012-abcdefghij", "--port", "80"]
    red = sa.redact_argv(argv)
    assert "xoxb-123456789012-abcdefghij" not in json.dumps(red)
    assert "--port" in red and "80" in red


def test_scan_artifact_text_fails_closed_on_fake_token():
    hits = sa.scan_artifact_text("cmd ghp_" + "b" * 36 + " end")
    assert hits, "artifact scan must flag a token-shaped string"
    assert sa.scan_artifact_text("plain text, no secrets") == []
```

- [ ] **Step 2: Run to verify failure** — `python3 -m pytest -q tests/test_setup_audit.py` → FAIL (`redact_map` missing).

- [ ] **Step 3: Implement**

```python
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
```

- [ ] **Step 4: Run to verify pass** — `python3 -m pytest -q tests/test_setup_audit.py` → 8 passed. Also `make audit-test`.

- [ ] **Step 5: Commit**

```bash
git add shared/skill-templates/khenrix-audit/scripts/setup_audit.py tests/test_setup_audit.py
git commit -m "feat(khenrix-audit): redaction layer — names+vhash only, fail-closed artifact scan

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Claude walker

**Files:**
- Modify: `shared/skill-templates/khenrix-audit/scripts/setup_audit.py` (add `# --- walkers: claude ---`; register in `WALKERS`)
- Test: `tests/test_setup_audit.py` (add a `make_claude_home` fixture builder)

**Interfaces:**
- Produces: `walk_claude(home, repo, git_root) -> list[dict]` emitting kinds: `plugin`, `skill`, `agent`, `command`, `hook`, `mcp`, `setting-skilloverride`, `permission-rule`. Skill names are namespaced `<plugin>:<name>`. MCP items carry `meta.endpoint_hash` (vhash of normalized command+args or URL), `meta.env_keys`. Hook items carry `meta.event`, `meta.matcher`, `meta.body_hash`.
- Consumes: `item()`, redaction functions.

- [ ] **Step 1: Write failing tests with a fixture home**

```python
def make_claude_home(tmp_path):
    h = tmp_path / "home"
    cache = h / ".claude/plugins/cache/mkt/plug/1.0.0"
    (cache / "skills/alpha").mkdir(parents=True)
    (cache / "skills/alpha" / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: >-\n  Does alpha things. "
        'Triggers: "run alpha".\n---\nbody\n')
    (cache / "hooks").mkdir()
    (cache / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {
        "Stop": [{"matcher": "*", "hooks": [{"type": "command", "command": "echo hi"}]}]}}))
    (h / ".claude/plugins").mkdir(parents=True, exist_ok=True)
    (h / ".claude/plugins/installed_plugins.json").write_text(json.dumps({
        "version": 2, "plugins": {"plug@mkt": [{
            "scope": "user", "installPath": str(cache), "version": "1.0.0"}]}}))
    (h / ".claude").mkdir(exist_ok=True)
    (h / ".claude/settings.json").write_text(json.dumps({
        "hooks": {"Stop": [{"matcher": "*", "hooks": [
            {"type": "command", "command": "echo hi"}]}]}}))
    (h / ".claude.json").write_text(json.dumps({
        "mcpServers": {"ctx": {"type": "stdio", "command": "npx",
                               "args": ["-y", "ctx"], "env": {"CTX_TOKEN": "ghp_" + "c" * 36}}},
        "projects": {}}))
    return h


def test_walk_claude_inventories_plugin_skill_hook_mcp(tmp_path):
    h = make_claude_home(tmp_path)
    items = sa.walk_claude(h, None, None)
    kinds = {(i["kind"], i["name"]) for i in items}
    assert ("plugin", "plug") in kinds
    assert ("skill", "plug:alpha") in kinds
    assert ("mcp", "ctx") in kinds
    hooks = [i for i in items if i["kind"] == "hook"]
    assert len(hooks) == 2  # plugin Stop hook + user settings Stop hook
    assert hooks[0]["meta"]["body_hash"] == hooks[1]["meta"]["body_hash"]


def test_walk_claude_redacts_mcp_env(tmp_path):
    h = make_claude_home(tmp_path)
    blob = json.dumps(sa.walk_claude(h, None, None))
    assert "ghp_" not in blob
    mcp = next(i for i in json.loads(blob) if i["kind"] == "mcp")
    assert mcp["meta"]["env_keys"] == ["CTX_TOKEN"]
    assert "endpoint_hash" in mcp["meta"]


def test_walk_claude_skill_carries_description(tmp_path):
    h = make_claude_home(tmp_path)
    skill = next(i for i in sa.walk_claude(h, None, None) if i["kind"] == "skill")
    assert "Does alpha things" in skill["meta"]["description"]
```

- [ ] **Step 2: Run to verify failure** — FAIL (`walk_claude` missing).

- [ ] **Step 3: Implement**

```python
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
                                      str(smd), "loaded", plugin=pname, **fm))
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
```

- [ ] **Step 4: Run to verify pass** — `make audit-test` → all pass (incl. earlier tasks).

- [ ] **Step 5: Commit**

```bash
git add shared/skill-templates/khenrix-audit/scripts/setup_audit.py tests/test_setup_audit.py
git commit -m "feat(khenrix-audit): claude walker — plugins, skills, hooks, MCP, settings

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Codex + agy walkers (catalog exclusion)

**Files:**
- Modify: `shared/skill-templates/khenrix-audit/scripts/setup_audit.py`
- Test: `tests/test_setup_audit.py`

**Interfaces:**
- Produces: `walk_codex(home, repo, git_root)`, `walk_agy(home, repo, git_root)`. Codex `~/.codex/.tmp/plugins/**` SKILL.md → `provenance="catalog"`; installed plugin cache + `skills/` (incl. `.system`) → `loaded`. agy: `~/.gemini/config/plugins/**` skills → `loaded`; `mcp_config.json` servers → `mcp` items.
- Consumes: `item()`, `read_frontmatter`, `_mcp_item`.

- [ ] **Step 1: Failing tests**

```python
def make_codex_home(tmp_path):
    h = tmp_path / "home"
    (h / ".codex/skills/.system/sys1").mkdir(parents=True)
    (h / ".codex/skills/.system/sys1/SKILL.md").write_text("---\nname: sys1\ndescription: s\n---\n")
    inst = h / ".codex/plugins/cache/mkt/plug/skills/beta"
    inst.mkdir(parents=True)
    (inst / "SKILL.md").write_text("---\nname: beta\ndescription: b\n---\n")
    cat = h / ".codex/.tmp/plugins/plugins/zoom/skills/z1"
    cat.mkdir(parents=True)
    (cat / "SKILL.md").write_text("---\nname: z1\ndescription: catalog only\n---\n")
    (h / ".codex/config.toml").write_text(
        '[mcp_servers.ctx]\ncommand = "npx"\nargs = ["-y", "ctx"]\n')
    return h


def test_walk_codex_separates_catalog_from_loaded(tmp_path):
    items = sa.walk_codex(make_codex_home(tmp_path), None, None)
    by_prov = {}
    for i in items:
        by_prov.setdefault(i["provenance"], set()).add(i["name"])
    assert any("z1" in n for n in by_prov.get("catalog", set()))
    loaded = by_prov.get("loaded", set())
    assert any("beta" in n for n in loaded) and any("sys1" in n for n in loaded)
    assert not any("z1" in n for n in loaded)


def test_walk_codex_reads_mcp_from_toml(tmp_path):
    items = sa.walk_codex(make_codex_home(tmp_path), None, None)
    assert any(i["kind"] == "mcp" and i["name"] == "ctx" for i in items)


def test_walk_agy(tmp_path):
    h = tmp_path / "home"
    sk = h / ".gemini/config/plugins/khenrix-utils/skills/gamma"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text("---\nname: gamma\ndescription: g\n---\n")
    (h / ".gemini/config/mcp_config.json").write_text(json.dumps(
        {"mcpServers": {"ctx": {"command": "npx", "args": ["-y", "ctx"]}}}))
    items = sa.walk_agy(h, None, None)
    assert any(i["kind"] == "skill" and "gamma" in i["name"] for i in items)
    assert any(i["kind"] == "mcp" and i["name"] == "ctx" for i in items)
```

- [ ] **Step 2: Run to verify failure** — FAIL (`walk_codex` missing).

- [ ] **Step 3: Implement**

```python
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
```

- [ ] **Step 4: Run to verify pass** — `make audit-test`.

- [ ] **Step 5: Commit**

```bash
git add shared/skill-templates/khenrix-audit/scripts/setup_audit.py tests/test_setup_audit.py
git commit -m "feat(khenrix-audit): codex + agy walkers with curated-catalog exclusion

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Project scope + canonical-repo walker (rendered-artifact provenance)

**Files:**
- Modify: `shared/skill-templates/khenrix-audit/scripts/setup_audit.py`
- Test: `tests/test_setup_audit.py`

**Interfaces:**
- Produces: `walk_projects(home, repo, git_root)` — bounded scan of `<git_root or home/git>/*`: only `.mcp.json`, `.claude/{skills,agents,commands,settings*.json}`, `CLAUDE.md`/`AGENTS.md`/`GEMINI.md` (managed-block presence recorded as `meta.managed_block_hash`); any path under a `marketplaces/` dir → `provenance="rendered-artifact"`; `shared/skills/**` in the khenrix-utils checkout → `provenance="source"`. Also `resolve_repo_root(candidate: Path|None) -> Path|None` validating `.git` + `capabilities.toml` + `shared/skills/`.
- Consumes: prior helpers.

- [ ] **Step 1: Failing tests**

```python
MANAGED_BEGIN = "<!-- khenrix-managed:begin -->"


def make_git_root(tmp_path):
    g = tmp_path / "git"
    ku = g / "khenrix-utils"
    (ku / ".git").mkdir(parents=True)
    (ku / "shared/skills/alpha").mkdir(parents=True)
    (ku / "shared/skills/alpha/SKILL.md").write_text("---\nname: alpha\ndescription: a\n---\n")
    r = ku / "marketplaces/claude/plugins/khenrix-utils/skills/alpha"
    r.mkdir(parents=True)
    (r / "SKILL.md").write_text("---\nname: alpha\ndescription: a\n---\n")
    (ku / "capabilities.toml").write_text("version = 1\n")
    (ku / "CLAUDE.md").write_text("x\n" + MANAGED_BEGIN + "\nstyle\n<!-- khenrix-managed:end -->\n")
    other = g / "app"
    (other / ".claude/skills/local1").mkdir(parents=True)
    (other / ".claude/skills/local1/SKILL.md").write_text("---\nname: local1\ndescription: l\n---\n")
    (other / ".mcp.json").write_text(json.dumps({"mcpServers": {"p1": {"command": "x"}}}))
    return g


def test_walk_projects_provenance_split(tmp_path):
    items = sa.walk_projects(tmp_path / "home", None, make_git_root(tmp_path))
    prov = {i["name"]: i["provenance"] for i in items if i["kind"] == "skill"}
    # same skill name, three provenances — only project-local one is "loaded"
    assert prov["khenrix-utils:shared:alpha"] == "source"
    assert prov["khenrix-utils:rendered:claude:alpha"] == "rendered-artifact"
    assert prov["app:local1"] == "loaded"


def test_walk_projects_finds_project_mcp_and_instruction_files(tmp_path):
    items = sa.walk_projects(tmp_path / "home", None, make_git_root(tmp_path))
    assert any(i["kind"] == "mcp" and i["scope"] == "project:app" for i in items)
    inst = [i for i in items if i["kind"] == "instruction-file"]
    assert any(i["meta"].get("managed_block_hash") for i in inst)


def test_resolve_repo_root(tmp_path):
    g = make_git_root(tmp_path)
    assert sa.resolve_repo_root(g / "khenrix-utils") == g / "khenrix-utils"
    assert sa.resolve_repo_root(g / "app") is None
    assert sa.resolve_repo_root(None) is None
```

- [ ] **Step 2: Run to verify failure** — FAIL.

- [ ] **Step 3: Implement**

```python
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
            for smd in sorted((cdir / "skills").rglob("SKILL.md")) if (cdir / "skills").exists() else []:
                fm = read_frontmatter(smd)
                items.append(item("claude", scope, "skill", f"{proj.name}:{fm['name']}",
                                  str(smd), "loaded", **{k: v for k, v in fm.items() if k != "name"}))
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
```

- [ ] **Step 4: Run to verify pass** — `make audit-test`. Also confirm the fixture-repo tree yields ZERO `loaded` duplicates of `alpha` (regression for the rendered-copy flood): add assertion to the provenance test if not already covered.

- [ ] **Step 5: Commit**

```bash
git add shared/skill-templates/khenrix-audit/scripts/setup_audit.py tests/test_setup_audit.py
git commit -m "feat(khenrix-audit): project walker — provenance split, canonical-root resolution

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Findings framework + `findings` subcommand

**Files:**
- Modify: `shared/skill-templates/khenrix-audit/scripts/setup_audit.py`
- Test: `tests/test_setup_audit.py`

**Interfaces:**
- Produces: `finding(rule, rule_version, cli, scope, kind, subjects: list[str], consequence, confidence, justification, evidence: dict, remediation: list[str], severity_hint=None) -> dict` — computes `id` = sid over canonical identity (subjects SORTED), `fingerprint` = vhash(canonical evidence), `severity` from `CONSEQUENCE_RANK`; `informational=True` forced when `SEMANTICS[cli]` lacks the axis the rule declares in `RULE_NEEDS`; forbids `justification=="cost"` for `kind=="mcp"` (raises `ValueError`). `run_checks(inv, ctx) -> list[dict]`; `CHECKS: list` registry; `write_findings(findings, inv, path, ctx)` (adds `capabilities` block; secret-scans, fail-closed). `cmd_findings(args)` wiring: inventory → checks → suppression (Task 12 no-op until then) → write.
- Consumes: Tasks 1–5.

- [ ] **Step 1: Failing tests**

```python
def test_finding_id_order_insensitive_fingerprint_evidence_sensitive():
    f1 = sa.finding("B1", 1, "claude", "user", "skill", ["b", "a"],
                    "wrong-tool-fires", "high", "correctness", {"x": 1}, ["r1"])
    f2 = sa.finding("B1", 1, "claude", "user", "skill", ["a", "b"],
                    "wrong-tool-fires", "high", "correctness", {"x": 2}, ["r1"])
    assert f1["id"] == f2["id"]
    assert f1["fingerprint"] != f2["fingerprint"]


def test_finding_severity_ranks_silent_loss_top():
    hi = sa.finding("B7", 1, "claude", "user", "skill", ["s"],
                    "silent-capability-loss", "high", "correctness", {}, [])
    lo = sa.finding("B9", 1, "claude", "user", "plugin", ["p"],
                    "hygiene", "high", "correctness", {}, [])
    assert hi["severity"] > lo["severity"]


def test_finding_cost_mcp_forbidden():
    import pytest
    with pytest.raises(ValueError):
        sa.finding("B7", 1, "claude", "user", "mcp", ["m"],
                   "cost", "high", "cost", {}, [])


def test_finding_unverified_cli_is_informational():
    f = sa.finding("B1", 1, "codex", "user", "skill", ["x"],
                   "wrong-tool-fires", "high", "correctness", {}, [])
    assert f["informational"] is True and "semantics unverified" in f["note"]


def test_write_findings_fails_closed_on_secret(tmp_path):
    import pytest
    bad = sa.finding("B2", 1, "claude", "user", "hook", ["h"],
                     "silent-capability-loss", "high", "correctness",
                     {"cmd": "ghp_" + "d" * 36}, [])
    with pytest.raises(SystemExit):
        sa.write_findings([bad], {"items": [], "errors": []},
                          tmp_path / "f.json", {"now": "2026-07-30T00:00:00Z"})
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement**

```python
# --- findings framework ---------------------------------------------------
CONSEQUENCE_RANK = {"silent-capability-loss": 5, "wrong-tool-fires": 4,
                    "state-divergence": 3, "cost": 2, "hygiene": 1}
CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}
# Which SEMANTICS axis each rule leans on; absent = portable (no gating).
RULE_NEEDS = {"B1": "precedence_verified", "B2": "namespacing", "B3": "dedupe_rule"}


def finding(rule, rule_version, cli, scope, kind, subjects, consequence,
            confidence, justification, evidence, remediation, note="") -> dict:
    if kind == "mcp" and justification == "cost":
        raise ValueError("MCP findings may never be cost-justified (spec constraint 1)")
    subjects = sorted(subjects)
    ident = canonical_json({"rule": rule, "rule_version": rule_version, "cli": cli,
                            "scope": scope, "kind": kind, "subjects": subjects})
    informational = False
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


def write_findings(findings, inv, path, ctx) -> None:
    doc = {"schema_version": SCHEMA_VERSION, "generated": ctx.get("now"),
           "capabilities": engine_capabilities(ctx),
           "inventory_hash": vhash(canonical_json(inv["items"])),
           "counts": {"items": len(inv["items"]), "findings": len(findings),
                      "errors": len(inv["errors"])},
           "errors": inv["errors"], "findings": findings}
    text = json.dumps(doc, indent=1, sort_keys=True)
    leaked = scan_artifact_text(text)
    if leaked:
        sys.exit(f"REFUSING to write {path}: secret-shaped strings survived "
                 f"sanitization: {leaked}")
    Path(path).write_text(text)


def cmd_findings(args) -> int:
    home = Path(args.home_root)
    repo = resolve_repo_root(args.repo_root)
    inv = build_inventory(home, repo, Path(args.git_root) if args.git_root else None)
    ctx = {"now": now_utc(args), "repo_root": repo, "home": home}
    fnd = run_checks(inv, ctx)
    fnd = apply_ledger(fnd, ctx)   # Task 12; define a pass-through until then:
    write_findings(fnd, inv, args.out or "findings.json", ctx)
    print(f"{len(fnd)} finding(s) → {args.out or 'findings.json'}")
    return 0


def apply_ledger(findings, ctx):  # replaced in Task 12
    return findings
```

Wire in `main()`: replace the `findings` placeholder branch with `if args.cmd == "findings": return cmd_findings(args)`.

- [ ] **Step 4: Run to verify pass** — `make audit-test`.

- [ ] **Step 5: Commit**

```bash
git add shared/skill-templates/khenrix-audit/scripts/setup_audit.py tests/test_setup_audit.py
git commit -m "feat(khenrix-audit): findings framework — stable ids, consequence severity, fail-closed writer

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Checks B1–B3 (collisions, bare-key refs, endpoint dedupe)

**Files:**
- Modify: `shared/skill-templates/khenrix-audit/scripts/setup_audit.py` (add `# --- checks: B1-B3 ---`)
- Test: `tests/test_setup_audit.py`

**Interfaces:**
- Produces: `check_b1_name_collisions`, `check_b2_bare_key_refs`, `check_b3_endpoint_dupes` registered in `CHECKS`. All operate ONLY on `provenance=="loaded"` items.
- Consumes: findings framework.

- [ ] **Step 1: Failing tests**

```python
def _mk_inv(items):
    return {"schema_version": 1, "items": items, "errors": []}


def test_b1_flags_same_bare_skill_name_two_plugins():
    inv = _mk_inv([
        sa.item("claude", "user", "skill", "pa:save", "/a", "loaded", description="x"),
        sa.item("claude", "user", "skill", "pb:save", "/b", "loaded", description="y"),
        sa.item("claude", "user", "skill", "pa:other", "/c", "loaded", description="z")])
    hits = sa.check_b1_name_collisions(inv, {})
    assert len(hits) == 1 and hits[0]["rule"] == "B1"
    assert set(hits[0]["subjects"]) == {"pa:save", "pb:save"}


def test_b1_ignores_catalog_and_rendered():
    inv = _mk_inv([
        sa.item("claude", "user", "skill", "pa:save", "/a", "loaded"),
        sa.item("codex", "user", "skill", "zoom:save", "/b", "catalog")])
    assert sa.check_b1_name_collisions(inv, {}) == []


def test_b2_flags_bare_key_reference_to_namespaced_plugin_server():
    inv = _mk_inv([
        sa.item("claude", "user", "mcp", "plugin:pw:pw", "/m", "loaded", endpoint_hash="e1"),
        sa.item("claude", "user", "permission-rule", "mcp__pw__click", "/s", "loaded"),
        sa.item("claude", "user", "hook", "<settings.json>:PostToolUse", "/s", "loaded",
                event="PostToolUse", matcher="mcp__pw__.*", owner="<settings.json>",
                body_hash="h")])
    hits = sa.check_b2_bare_key_refs(inv, {})
    assert len(hits) == 2
    assert all(h["consequence"] == "silent-capability-loss" for h in hits)


def test_b3_flags_same_endpoint_two_scopes():
    inv = _mk_inv([
        sa.item("claude", "user", "mcp", "cdt", "/u", "loaded", endpoint_hash="same"),
        sa.item("claude", "project:app", "mcp", "cdt2", "/p", "loaded", endpoint_hash="same"),
        sa.item("claude", "user", "mcp", "other", "/u", "loaded", endpoint_hash="diff")])
    hits = sa.check_b3_endpoint_dupes(inv, {})
    assert len(hits) == 1 and hits[0]["kind"] == "mcp"
    assert hits[0]["justification"] == "correctness"
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement**

```python
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
                # bare mcp__<server>__ style reference without the plugin_ prefix
                if re.search(rf"mcp__{re.escape(bare)}(__|\b)", probe) and \
                        f"mcp__plugin_" not in probe:
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
    for (cli, ep), group in sorted(by_ep.items()):
        if ep and len(group) > 1:
            out.append(finding("B3", 1, cli, "multi", "mcp",
                               sorted(f'{g["scope"]}/{g["name"]}' for g in group),
                               "silent-capability-loss", "medium", "correctness",
                               {"endpoint_hash": ep,
                                "configs": sorted(g["source_path"] for g in group)},
                               ["remove the lower-precedence duplicate (confirmed)"]))
    return out


CHECKS.extend([check_b1_name_collisions, check_b2_bare_key_refs, check_b3_endpoint_dupes])
```

- [ ] **Step 4: Run to verify pass** — `make audit-test`.

- [ ] **Step 5: Commit**

```bash
git add shared/skill-templates/khenrix-audit/scripts/setup_audit.py tests/test_setup_audit.py
git commit -m "feat(khenrix-audit): checks B1-B3 — collisions, bare-key refs, endpoint dedupe

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: Checks B4–B5 (declared↔live drift with ownership, cross-CLI drift)

**Files:**
- Modify: `shared/skill-templates/khenrix-audit/scripts/setup_audit.py`
- Test: `tests/test_setup_audit.py`

**Interfaces:**
- Produces: `load_declared(repo_root) -> dict` (parses canonical `capabilities.toml` with `tomllib`: declared MCP names + platform gates); `check_b4_drift(inv, ctx)` — three states: `drift` (declared∧¬live, or live∧`managed-absent`-policy), `unmanaged` (live∧¬declared → single INFO finding, `confidence="low"`, never waivable), `in-sync` (no finding); `check_b5_cross_cli(inv, ctx)` — declared server missing on one CLI, respecting `platform` gates and `docs_mcp` per-CLI applicability. `ctx["policies"]` supplies `desired_state` entries from the ledger (Task 12 loads them; until then tests inject).
- Consumes: walkers, findings, ledger policies via ctx.

- [ ] **Step 1: Failing tests**

```python
def _repo_with_caps(tmp_path, caps_text):
    ku = tmp_path / "ku"
    (ku / ".git").mkdir(parents=True)
    (ku / "shared/skills").mkdir(parents=True)
    (ku / "capabilities.toml").write_text(caps_text)
    return ku


CAPS = 'version = 1\n[mcp_servers.ctx]\ncommand = "npx"\n[mcp_servers.vercel]\nurl = "https://v"\n'


def test_b4_declared_but_not_live_is_drift(tmp_path):
    repo = _repo_with_caps(tmp_path, CAPS)
    inv = _mk_inv([sa.item("claude", "user", "mcp", "ctx", "/c", "loaded", endpoint_hash="e")])
    hits = sa.check_b4_drift(inv, {"repo_root": repo, "policies": {}})
    drift = [h for h in hits if h["evidence"]["direction"] == "declared-not-live"]
    assert any("vercel" in h["subjects"][0] for h in drift)


def test_b4_live_managed_absent_keeps_firing(tmp_path):
    repo = _repo_with_caps(tmp_path, CAPS)
    inv = _mk_inv([sa.item("claude", "user", "mcp", "gdrive", "/c", "loaded", endpoint_hash="e")])
    pol = {"mcp:gdrive": {"desired_state": "managed-absent", "reason": "native connector"}}
    hits = sa.check_b4_drift(inv, {"repo_root": repo, "policies": pol})
    gone = [h for h in hits if "gdrive" in h["subjects"][0]]
    assert gone and gone[0]["consequence"] == "state-divergence"
    assert gone[0]["confidence"] == "high"


def test_b4_live_unknown_is_info_only(tmp_path):
    repo = _repo_with_caps(tmp_path, CAPS)
    inv = _mk_inv([sa.item("claude", "user", "mcp", "personal", "/c", "loaded", endpoint_hash="e"),
                   sa.item("claude", "user", "mcp", "ctx", "/c", "loaded", endpoint_hash="e2")])
    hits = sa.check_b4_drift(inv, {"repo_root": repo, "policies": {}})
    um = [h for h in hits if "personal" in h["subjects"][0]]
    assert um and um[0]["confidence"] == "low" and um[0]["evidence"]["direction"] == "unmanaged"


def test_b5_cross_cli_missing_server(tmp_path):
    repo = _repo_with_caps(tmp_path, CAPS)
    inv = _mk_inv([sa.item("claude", "user", "mcp", "ctx", "/c", "loaded", endpoint_hash="e"),
                   sa.item("codex", "user", "mcp", "ctx", "/c", "loaded", endpoint_hash="e")])
    hits = sa.check_b5_cross_cli(inv, {"repo_root": repo, "policies": {}})
    assert any("agy" in h["cli"] and "ctx" in h["subjects"][0] for h in hits)
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement**

```python
# --- checks: B4-B5 (drift) ------------------------------------------------
def load_declared(repo_root) -> dict:
    """Declared capabilities from the CANONICAL capabilities.toml. Vocabulary kept
    aligned with reconcile.py's status output (EXTRA / missing)."""
    import tomllib
    if not repo_root:
        return {}
    with open(Path(repo_root) / "capabilities.toml", "rb") as f:
        caps = tomllib.load(f)
    return {"mcp": {n: {"platform": e.get("platform")}
                    for n, e in (caps.get("mcp_servers") or {}).items()},
            "docs_mcp": {cli: list(v) if isinstance(v, dict) else v
                         for cli, v in (caps.get("docs_mcp") or {}).items()}}


def check_b4_drift(inv, ctx) -> list:
    out = []
    decl = load_declared(ctx.get("repo_root"))
    if not decl:
        return [finding("B4", 1, "all", "repo", "check-error", ["capabilities.toml"],
                        "state-divergence", "low", "drift",
                        {"error": "no canonical repo root"}, [],
                        note="NOT EVALUATED — pass --repo-root")]
    for cli in CLIS:
        live = {m["name"]: m for m in _loaded(inv, "mcp", cli) if m["scope"] == "user"}
        if not live and not any(i["cli"] == cli for i in inv["items"]):
            continue  # CLI absent on this machine — not drift
        for name in sorted(decl["mcp"]):
            if name not in live:
                out.append(finding("B4", 1, cli, "user", "mcp", [f"{cli}/{name}"],
                                   "state-divergence", "medium", "drift",
                                   {"direction": "declared-not-live"},
                                   ["run khenrix-setup to add it"]))
        for name in sorted(set(live) - set(decl["mcp"])):
            pol = ctx.get("policies", {}).get(f"mcp:{name}")
            if pol and pol.get("desired_state") == "managed-absent":
                out.append(finding("B4", 2, cli, "user", "mcp", [f"{cli}/{name}"],
                                   "state-divergence", "high", "drift",
                                   {"direction": "managed-absent-but-live",
                                    "reason": pol.get("reason", "")},
                                   ["remove from live config (confirmed, restore bundle first)"]))
            else:
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
        return []
    present_clis = [c for c in CLIS if any(i["cli"] == c for i in inv["items"])]
    for name in sorted(decl["mcp"]):
        have = {c for c in present_clis
                if any(m["name"] == name for m in _loaded(inv, "mcp", c))}
        missing = set(present_clis) - have
        if have and missing:
            for cli in sorted(missing):
                out.append(finding("B5", 1, cli, "user", "mcp", [f"{cli}/{name}"],
                                   "state-divergence", "medium", "drift",
                                   {"present_on": sorted(have)},
                                   [f"add {name} on {cli} via khenrix-setup"]))
    return out


CHECKS.extend([check_b4_drift, check_b5_cross_cli])
```

- [ ] **Step 4: Run to verify pass.** — `make audit-test`.

- [ ] **Step 5: Commit**

```bash
git add shared/skill-templates/khenrix-audit/scripts/setup_audit.py tests/test_setup_audit.py
git commit -m "feat(khenrix-audit): B4-B5 drift with ownership tiers and managed-absent policy

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 9: Check B6 — trigger-surface overlap nomination

**Files:**
- Modify: `shared/skill-templates/khenrix-audit/scripts/setup_audit.py`
- Test: `tests/test_setup_audit.py`

**Interfaces:**
- Produces: `trigger_surface(description) -> str`, `overlap_pairs(skills: list[dict], top_k=15) -> list[tuple]` (cosine, nameA, nameB, shared tokens, exact shared quoted phrases), `check_b6_trigger_overlap(inv, ctx)` — emits top-K nominations as findings (`consequence="wrong-tool-fires"`, `confidence="low"` — heuristic NOMINATES; Phase C/D adjudicate) plus `meta.same_plugin` flag; reports nomination coverage in evidence.
- Consumes: findings framework. This ports the session-validated prototype: trigger surface only (whole-description scoring buried the known-bad pair; trigger-surface ranked it #1 at 0.452).

- [ ] **Step 1: Failing tests**

```python
def test_trigger_surface_extracts_quotes_triggers_usewhen():
    d = ('Does X. Use when the user wants to file a link. '
         'Triggers: "add this to the wiki", "save this link".')
    ts = sa.trigger_surface(d)
    assert "add this to the wiki" in ts and "file a link" in ts
    assert "Does X" not in ts


def test_overlap_pairs_ranks_shared_trigger_vocab_first():
    skills = [
        {"name": "a:save", "meta": {"description": 'Triggers: "save this", "add this to the wiki", "keep this".'}},
        {"name": "b:wiki-add", "meta": {"description": 'Triggers: "add this to the wiki", "save this link".'}},
        {"name": "c:chart", "meta": {"description": 'Triggers: "plot a chart", "visualize data".'}},
    ]
    pairs = sa.overlap_pairs(skills, top_k=3)
    assert pairs, "must nominate at least one pair"
    top = pairs[0]
    assert {top[1], top[2]} == {"a:save", "b:wiki-add"}
    assert "add this to the wiki" in top[4]  # exact shared quoted phrase


def test_b6_emits_low_confidence_nominations():
    inv = _mk_inv([
        sa.item("claude", "user", "skill", "a:save", "/a", "loaded",
                description='Triggers: "add this to the wiki".'),
        sa.item("claude", "user", "skill", "b:wiki-add", "/b", "loaded",
                description='Triggers: "add this to the wiki".')])
    hits = sa.check_b6_trigger_overlap(inv, {})
    assert hits and hits[0]["confidence"] == "low"
    assert hits[0]["evidence"]["shared_phrases"] == ["add this to the wiki"]
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement**

```python
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
    skills = [s for s in _loaded(inv, "skill") if s["meta"].get("description")]
    out = []
    pairs = overlap_pairs(skills)
    for cos, x, y, shared, exact in pairs:
        same_plugin = x.split(":")[0] == y.split(":")[0]
        out.append(finding("B6", 1, "claude", "user", "skill", [x, y],
                           "wrong-tool-fires", "low", "correctness",
                           {"cosine": cos, "shared_tokens": shared,
                            "shared_phrases": exact, "same_plugin": same_plugin,
                            "corpus_size": len(skills), "nominated": len(pairs)},
                           ["Phase C adjudication → Phase D arena/probes",
                            "rung 1 if DUPLICATE and one side is ours"],
                           note="heuristic nomination — adjudicate before acting"))
    return out


CHECKS.append(check_b6_trigger_overlap)
```

- [ ] **Step 4: Run to verify pass.** — `make audit-test`.

- [ ] **Step 5: Commit**

```bash
git add shared/skill-templates/khenrix-audit/scripts/setup_audit.py tests/test_setup_audit.py
git commit -m "feat(khenrix-audit): B6 trigger-surface overlap nomination (top-K + exact phrases)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 10: Checks B7–B9 (budget, hook collisions, hygiene)

**Files:**
- Modify: `shared/skill-templates/khenrix-audit/scripts/setup_audit.py`
- Test: `tests/test_setup_audit.py`

**Interfaces:**
- Produces: `check_b7_budget(inv, ctx)` — reads `ctx["tokens"]` (dict `{plugin: always_on_tokens}` loaded from `--tokens-file`, produced by the model via `claude plugin details`; absent file → `NOT EVALUATED` finding), compares vs `ctx["context_window"] * 0.01` (default 200_000), includes instruction-file `chars/4` estimates SEPARATELY (never mixed into the token total; both reported); `check_b8_hook_collisions` — same `body_hash` registered twice → finding; same event+matcher from different owners → informational; `check_b9_hygiene` — version `unknown`, `load_failed` state. Add `--tokens-file` and `--context-window` to `add_common`.
- Consumes: walkers, findings.

- [ ] **Step 1: Failing tests**

```python
def test_b7_over_budget_flags_silent_capability_loss():
    inv = _mk_inv([])
    ctx = {"tokens": {"claude-obsidian": 3678, "khenrix-utils": 2918},
           "context_window": 200_000}
    hits = sa.check_b7_budget(inv, ctx)
    assert hits and hits[0]["consequence"] == "silent-capability-loss"
    assert hits[0]["evidence"]["total_always_on"] == 6596
    assert hits[0]["evidence"]["budget"] == 2000
    assert hits[0]["justification"] == "correctness"  # NOT "cost" — overflow drops descriptions


def test_b7_without_tokens_file_is_not_evaluated():
    hits = sa.check_b7_budget(_mk_inv([]), {"context_window": 200_000})
    assert hits and "NOT EVALUATED" in hits[0]["note"]


def test_b8_duplicate_hook_bodies_flagged():
    inv = _mk_inv([
        sa.item("claude", "user", "hook", "<settings.json>:Stop", "/s", "loaded",
                event="Stop", matcher="*", owner="<settings.json>", body_hash="same"),
        sa.item("claude", "user", "hook", "plug:Stop", "/p", "loaded",
                event="Stop", matcher="*", owner="plug", body_hash="same")])
    hits = sa.check_b8_hook_collisions(inv, {})
    dup = [h for h in hits if h["evidence"].get("duplicate_body")]
    assert len(dup) == 1


def test_b9_flags_unknown_version_and_load_failed():
    inv = _mk_inv([
        sa.item("claude", "user", "plugin", "p1", "/x", "loaded", version="unknown"),
        sa.item("claude", "user", "plugin", "p2", "/y", "loaded",
                effective_state="load_failed", version="1.0")])
    hits = sa.check_b9_hygiene(inv, {})
    assert len(hits) == 2 and all(h["consequence"] == "hygiene" for h in hits)
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement**

```python
# --- checks: B7-B9 --------------------------------------------------------
def check_b7_budget(inv, ctx) -> list:
    tokens = ctx.get("tokens")
    window = ctx.get("context_window", 200_000)
    budget = int(window * 0.01)
    if tokens is None:
        return [finding("B7", 1, "claude", "user", "skill", ["<listing-budget>"],
                        "silent-capability-loss", "low", "correctness", {"budget": budget},
                        ["produce tokens.json via `claude plugin details` and re-run"],
                        note="NOT EVALUATED — no --tokens-file")]
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
```

Also in `cmd_findings`, before `run_checks`: load `ctx["tokens"]` from `args.tokens_file` if given (`json.loads(Path(args.tokens_file).read_text())`), and `ctx["context_window"] = args.context_window`. Add both flags in `add_common` (`--tokens-file`, `--context-window` type=int default=200000).

- [ ] **Step 4: Run to verify pass.** — `make audit-test`.

- [ ] **Step 5: Commit**

```bash
git add shared/skill-templates/khenrix-audit/scripts/setup_audit.py tests/test_setup_audit.py
git commit -m "feat(khenrix-audit): B7 budget (hermetic tokens input), B8 hook dupes, B9 hygiene

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 11: Checks B10–B16 (cheap file checks)

**Files:**
- Modify: `shared/skill-templates/khenrix-audit/scripts/setup_audit.py`
- Test: `tests/test_setup_audit.py`

**Interfaces:**
- Produces: `check_b10_parse_validity` (walkers already record parse crashes in `inv["errors"]` — this converts each error into a finding, `consequence="silent-capability-loss"`); `check_b11_dangling_refs` (hook `command_head[0]` path missing on disk when absolute; MCP `env_keys` naming vars unset in the probe environment is Phase-G advisory, NOT checked here — hermeticity); `check_b12_frontmatter` (missing description / >1024 chars / body ≥500 lines on `loaded` skills); `check_b13_managed_block_divergence` (instruction files in one project whose `managed_block_hash` differ); `check_b14_claude_json_health` (size > 1 MiB, or project entries whose dir is gone — pass `home` via ctx); `check_b15_dual_path` (two `loaded` skill items, same bare name, same `body hash` → same skill twice); `check_b16_vendored_source_enabled` (skill `meta.vendored_from` plugin still enabled).
- Consumes: everything prior.

- [ ] **Step 1: Failing tests**

```python
def test_b10_converts_walker_errors_to_findings():
    inv = {"schema_version": 1, "items": [], "errors": ["walk_claude: ValueError: bad json"]}
    hits = sa.check_b10_parse_validity(inv, {})
    assert hits and hits[0]["consequence"] == "silent-capability-loss"


def test_b11_flags_missing_hook_executable(tmp_path):
    inv = _mk_inv([sa.item("claude", "user", "hook", "o:Stop", "/s", "loaded",
                           event="Stop", matcher="*", owner="o", body_hash="h",
                           command_head=[str(tmp_path / "gone.sh")])])
    hits = sa.check_b11_dangling_refs(inv, {})
    assert len(hits) == 1


def test_b12_flags_oversized_description():
    inv = _mk_inv([sa.item("claude", "user", "skill", "p:big", "/x", "loaded",
                           description="d" * 1100, body_lines=10)])
    assert len(sa.check_b12_frontmatter(inv, {})) == 1


def test_b13_flags_diverged_managed_blocks():
    inv = _mk_inv([
        sa.item("all", "project:p", "instruction-file", "p:CLAUDE.md", "/1", "loaded",
                chars=10, managed_block_hash="aaa"),
        sa.item("all", "project:p", "instruction-file", "p:AGENTS.md", "/2", "loaded",
                chars=10, managed_block_hash="bbb")])
    assert len(sa.check_b13_managed_block_divergence(inv, {})) == 1


def test_b14_flags_stale_project_entries(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    (h / ".claude.json").write_text(json.dumps(
        {"mcpServers": {}, "projects": {str(tmp_path / "gone-dir"): {}}}))
    hits = sa.check_b14_claude_json_health(_mk_inv([]), {"home": h})
    assert hits and "gone-dir" in json.dumps(hits[0]["evidence"])


def test_b15_flags_same_skill_two_paths():
    inv = _mk_inv([
        sa.item("claude", "user", "skill", "pa:dup", "/a", "loaded", description="same text"),
        sa.item("claude", "user", "skill", "dup", "/b", "loaded", description="same text")])
    assert len(sa.check_b15_dual_path(inv, {})) == 1
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement**

```python
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
```

Also pass `ctx["home"] = home` in `cmd_findings` (already present from Task 6 — verify).

- [ ] **Step 4: Run to verify pass.** — `make audit-test`.

- [ ] **Step 5: Commit**

```bash
git add shared/skill-templates/khenrix-audit/scripts/setup_audit.py tests/test_setup_audit.py
git commit -m "feat(khenrix-audit): checks B10-B16 — parse validity, dangling refs, hygiene sweep

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 12: Ledger (sole writer, fingerprint waivers, suppression)

**Files:**
- Modify: `shared/skill-templates/khenrix-audit/scripts/setup_audit.py`
- Modify: `.gitignore` (add `docs/setup-audit/runs/`)
- Create: `docs/setup-audit/ledger.json` (seeded)
- Test: `tests/test_setup_audit.py`

**Interfaces:**
- Produces: `ledger_paths(ctx) -> (repo_ledger: Path|None, local_ledger: Path)` (local = `<home>/.local/state/khenrix/ledger.local.json`); `load_ledger(ctx) -> dict` (merge both; entries keyed by finding id; also returns `policies` keyed `"<kind>:<name>"` for `desired_state` entries); `ledger_write(path, entries)` atomic + `.lock` file guard; `cmd_ledger_add(args)` (flags: `--id --state --reason --until --desired-state --subject --local`); `cmd_ledger_expire(args)`; real `apply_ledger(findings, ctx)` replacing the Task 6 pass-through: `disposition=="waived"` + fingerprint match + not expired → drop to a `waived` list in the doc (not deleted); fingerprint mismatch → keep, note `"waiver stale — situation changed"`; expired → keep, note `"waiver expired <until>"`; `wontfix` → waived list permanently.
- Consumes: findings framework; `cmd_findings` gains `waived` section in output doc.

- [ ] **Step 1: Failing tests**

```python
def _ledger_ctx(tmp_path, entries=None, policies=None):
    repo = tmp_path / "ku"
    (repo / ".git").mkdir(parents=True)
    (repo / "shared/skills").mkdir(parents=True)
    (repo / "capabilities.toml").write_text("version = 1\n")
    (repo / "docs/setup-audit").mkdir(parents=True)
    (repo / "docs/setup-audit/ledger.json").write_text(json.dumps(
        {"schema_version": 1, "entries": entries or {}, "policies": policies or {}}))
    home = tmp_path / "home"
    home.mkdir()
    return {"repo_root": repo, "home": home, "now": "2026-08-01T00:00:00Z"}


def _wf(**kw):
    base = dict(rule="B6", rule_version=1, cli="claude", scope="user", kind="skill",
                subjects=["a", "b"], consequence="wrong-tool-fires", confidence="low",
                justification="correctness", evidence={"cosine": 0.4}, remediation=[])
    base.update(kw)
    return sa.finding(**base)


def test_waiver_suppresses_matching_fingerprint(tmp_path):
    f = _wf()
    ctx = _ledger_ctx(tmp_path, entries={f["id"]: {
        "disposition": "waived", "fingerprint": f["fingerprint"],
        "until": "2026-12-31T00:00:00Z", "reason": "deliberate"}})
    ctx.update(sa.load_ledger(ctx))
    kept = sa.apply_ledger([f], ctx)
    assert kept == [] and ctx["waived"][0]["id"] == f["id"]


def test_stale_fingerprint_reraises(tmp_path):
    f = _wf(evidence={"cosine": 0.9})
    ctx = _ledger_ctx(tmp_path, entries={f["id"]: {
        "disposition": "waived", "fingerprint": "outdated0", "until": "2026-12-31T00:00:00Z",
        "reason": "old"}})
    ctx.update(sa.load_ledger(ctx))
    kept = sa.apply_ledger([f], ctx)
    assert kept and "situation changed" in kept[0]["note"]


def test_expired_waiver_reraises(tmp_path):
    f = _wf()
    ctx = _ledger_ctx(tmp_path, entries={f["id"]: {
        "disposition": "waived", "fingerprint": f["fingerprint"],
        "until": "2026-07-01T00:00:00Z", "reason": "was deferred"}})
    ctx.update(sa.load_ledger(ctx))
    kept = sa.apply_ledger([f], ctx)
    assert kept and "waiver expired" in kept[0]["note"]


def test_ledger_add_is_atomic_and_loadable(tmp_path):
    ctx = _ledger_ctx(tmp_path)
    rc = sa.main(["ledger-add", "--home-root", str(ctx["home"]),
                  "--repo-root", str(ctx["repo_root"]), "--id", "abc123def456",
                  "--state", "waived", "--reason", "test", "--fingerprint", "ff00ff00",
                  "--until", "2026-12-31T00:00:00Z"])
    assert rc == 0
    led = json.loads((ctx["repo_root"] / "docs/setup-audit/ledger.json").read_text())
    assert led["entries"]["abc123def456"]["reason"] == "test"


def test_policies_flow_from_ledger(tmp_path):
    ctx = _ledger_ctx(tmp_path, policies={
        "mcp:gdrive": {"desired_state": "managed-absent", "reason": "native connector"}})
    loaded = sa.load_ledger(ctx)
    assert loaded["policies"]["mcp:gdrive"]["desired_state"] == "managed-absent"
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement**

```python
# --- ledger ---------------------------------------------------------------
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
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(".lock")
    if lock.exists():
        sys.exit(f"ledger locked by another writer: {lock} (remove if stale)")
    lock.write_text(str(os.getpid()))
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=1, sort_keys=True))
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
    doc = _read_ledger(target)
    e = doc.get("entries", {}).get(args.id)
    if not e:
        sys.exit(f"no ledger entry {args.id}")
    e["until"] = now_utc(args)   # never delete — history is the audit trail
    ledger_write(target, doc)
    print(f"ledger: {args.id} expired")
    return 0
```

Add the flags to the two ledger subparsers in `main()` (`--id`, `--state`, `--reason`, `--fingerprint`, `--until`, `--subject`, `--desired-state`, `--machine` default `""`, `--local` action=store_true) and dispatch `cmd_ledger_add` / `cmd_ledger_expire`. In `cmd_findings`, replace `ctx = {...}` composition: `ctx.update(load_ledger(ctx))` before `run_checks`, pass `ctx["policies"]` through (B4 already reads it), and include `"waived"` + `"local_waivers"` in the `write_findings` doc.

- [ ] **Step 4: Seed the real ledger + gitignore**

Create `docs/setup-audit/ledger.json`:

```json
{
 "schema_version": 1,
 "entries": {},
 "policies": {
  "mcp:google-drive": {
   "desired_state": "managed-absent",
   "reason": "Removed from capabilities.toml 2026-07-20: redundant with the native claude.ai Drive connector. Still live in ~/.claude.json and ~/.codex/config.toml — must keep firing until removed there.",
   "created": "2026-07-30T00:00:00Z"
  }
 }
}
```

Append to `.gitignore`:

```
# khenrix-audit per-run artifacts (machine state; ledger.json IS committed)
docs/setup-audit/runs/
```

- [ ] **Step 5: Run to verify pass** — `make audit-test`.

- [ ] **Step 6: Commit**

```bash
git add shared/skill-templates/khenrix-audit/scripts/setup_audit.py tests/test_setup_audit.py docs/setup-audit/ledger.json .gitignore
git commit -m "feat(khenrix-audit): ledger — sole-writer, fingerprint waivers, managed-absent policies

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 13: Report renderer + `--check` mode

**Files:**
- Modify: `shared/skill-templates/khenrix-audit/scripts/setup_audit.py`
- Test: `tests/test_setup_audit.py`

**Interfaces:**
- Produces: `render_report(doc: dict, phases: dict) -> str` (deterministic markdown skeleton: header with status/counts/coverage; findings by severity with slug, evidence, remediation rungs; collapsed waived section; explicit line per skipped phase; "N local waivers active"); `cmd_findings` gains `--report-dir` (writes `latest.md` + `runs/<machine>/<ts>-<invhash>.md/.json`, machine from `--machine` flag default `platform.node()` sanitized) and `--check <min-severity>` (exit 1 if any non-informational finding ≥ threshold; e.g. `--check 40`). Report text passes `scan_artifact_text` (fail closed).
- Consumes: everything prior.

- [ ] **Step 1: Failing tests**

```python
def test_render_report_contains_skeleton_and_skips(tmp_path):
    f = _wf()
    doc = {"generated": "2026-07-30T00:00:00Z", "capabilities": {"can_probe": False},
           "inventory_hash": "abc", "counts": {"items": 3, "findings": 1, "errors": 0},
           "errors": [], "findings": [f], "waived": [], "local_waivers": 2}
    md = sa.render_report(doc, phases={"probes": "skipped — claude not on PATH",
                                       "ecosystem": "cache, 12d old"})
    assert "probes: skipped — claude not on PATH" in md
    assert "2 local waiver(s) active" in md
    assert f["slug"] in md


def test_check_mode_exit_codes(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    rc_clean = sa.main(["findings", "--home-root", str(home),
                        "--out", str(tmp_path / "f.json"), "--check", "40"])
    assert rc_clean == 0  # empty home → no findings ≥ 40
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement**

```python
# --- report ---------------------------------------------------------------
def render_report(doc: dict, phases: dict) -> str:
    L = [f"# Setup audit — {doc['generated']}", "",
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
        L.append(f"- evidence: `{canonical_json(f['evidence'])[:400]}`")
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
    (runs / f"{stamp}.md").write_text(md)
    (runs / f"{stamp}.json").write_text(json.dumps(doc, indent=1, sort_keys=True))
    history = sorted(runs.glob("*.md"))
    if len(history) > 10:
        print(f"note: {len(history)} run reports in {runs} — prune manually "
              f"(the engine never deletes history unasked)")
```

In `cmd_findings`: add `--report-dir`, `--machine` (default `platform.node()`), `--check` (type=int, default None) to `add_common`; after `write_findings`, if `args.report_dir`: `write_report(doc, phases={"inventory": "complete", "checks": "complete", "probes": "engine does not run probes — SKILL.md Phase D", "ecosystem": "engine does not run discovery — SKILL.md Phase E"}, ...)`. Build `doc` once and reuse for both writers (refactor `write_findings` to return the doc). `--check N`: return 1 if any `f["severity"] >= N and not f["informational"]`.

- [ ] **Step 4: Run to verify pass.** — `make audit-test`.

- [ ] **Step 5: Commit**

```bash
git add shared/skill-templates/khenrix-audit/scripts/setup_audit.py tests/test_setup_audit.py
git commit -m "feat(khenrix-audit): deterministic report renderer + --check gate

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 14: `eval_trigger.py --arena` mode

**Files:**
- Modify: `scripts/eval_trigger.py`
- Test: extend `_self_test()` in the same file (repo idiom)

**Interfaces:**
- Produces: `--arena skillA,skillB[,...]` flag; `ARENA_TMPL` (judge sees ALL competitor name+description pairs, must pick exactly one or `none`); `parse_arena_verdict(raw, competitors) -> str` (winner name or `"none"`); `score_arena(cases) -> dict` (per-skill confusion matrix `{expected: {got: count}}`); reads `evals/<first-skill>/arena.json` `{"prompts": [{"prompt": ..., "expected": "<skill-name-or-none>"}]}`. Exit 0 when ≥0.8 of prompts route to their expected skill.
- Consumes: existing `fanout` plumbing and `load_skill_meta` (unchanged).

- [ ] **Step 1: Extend `_self_test()` with failing cases**

```python
    # --- arena mode (Task 14) ---
    ok.append(("arena verdict picks named winner",
               parse_arena_verdict('{"winner": "khenrix-wiki-add"}',
                                   ["khenrix-wiki-add", "save"]) == "khenrix-wiki-add"))
    ok.append(("arena verdict unknown → none",
               parse_arena_verdict('{"winner": "bogus"}', ["a", "b"]) == "none"))
    ar = score_arena([{"expected": "a", "got": "a"}, {"expected": "a", "got": "b"},
                      {"expected": "none", "got": "none"}])
    ok.append(("arena accuracy 2/3", ar["accuracy"] == round(2 / 3, 4)))
    ok.append(("arena confusion", ar["confusion"]["a"]["b"] == 1))
```

- [ ] **Step 2: Run to verify failure** — `python3 scripts/eval_trigger.py --self-test` → NameError.

- [ ] **Step 3: Implement**

```python
ARENA_TMPL = """A coding agent has these skills available (name + description):

{roster}

The user sends this message:
<<<BEGIN
{prompt}
END>>>

Judging ONLY from the names + descriptions, which ONE skill (if any) should
activate? Output ONLY JSON, no prose:
{{"winner": "<exact skill name, or none>", "why": "<one short sentence>"}}"""


def parse_arena_verdict(raw: str, competitors: list) -> str:
    s = (raw or "").strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", s, re.DOTALL)
    if fence:
        s = fence.group(1)
    cand = s[s.find("{"): s.rfind("}") + 1] if "{" in s and "}" in s else s
    try:
        w = str(json.loads(cand).get("winner", "none")).strip()
    except (json.JSONDecodeError, AttributeError):
        return "none"
    return w if w in competitors else "none"


def score_arena(cases: list) -> dict:
    confusion: dict = {}
    correct = 0
    for c in cases:
        confusion.setdefault(c["expected"], {})
        confusion[c["expected"]][c["got"]] = confusion[c["expected"]].get(c["got"], 0) + 1
        correct += c["expected"] == c["got"]
    return {"accuracy": round(correct / len(cases), 4) if cases else 0.0,
            "confusion": confusion}


def run_arena(args) -> int:
    skills = [s.strip() for s in args.arena.split(",") if s.strip()]
    path = EVALS_ROOT / skills[0] / "arena.json"
    if not path.exists():
        sys.exit(f"no arena prompts at {path.relative_to(ROOT)} — create it "
                 '({"prompts": [{"prompt": "...", "expected": "<name-or-none>"}]})')
    spec = json.loads(path.read_text())
    roster = "\n".join(f"- NAME: {n}\n  DESCRIPTION: {d}"
                       for n, d in (load_skill_meta(s, args.judge) for s in skills))
    cfg = fanout.resolve_mode_config(argparse.Namespace(
        mode=args.mode, timeout=args.timeout, model_claude=None, model_codex=None, model_agy=None))
    timeout = fanout.effective_timeout(argparse.Namespace(mode=args.mode, timeout=args.timeout))
    workdir = EVALS_ROOT / skills[0] / "workspace" / "arena"
    workdir.mkdir(parents=True, exist_ok=True)
    names = [load_skill_meta(s, args.judge)[0] for s in skills]
    cases = []
    for i, case in enumerate(spec["prompts"]):
        jp = ARENA_TMPL.format(roster=roster, prompt=case["prompt"])
        spec_ = fanout.build_real_spec(args.judge, jp, timeout, cfg, workdir)
        m = fanout.run_council([spec_], retries=1, timeout=timeout, backoff=2.0,
                               workdir=workdir / f"p-{i}", prompt=jp)
        rec = m["providers"][0]
        raw = Path(rec["result_file"]).read_text() if rec.get("valid") else ""
        got = parse_arena_verdict(raw, names + ["none"])
        cases.append({"prompt": case["prompt"], "expected": case["expected"], "got": got})
        print(f"  {'✓' if got == case['expected'] else '✗'} {case['prompt'][:60]} → {got}")
    result = score_arena(cases)
    (workdir / "arena-result.json").write_text(json.dumps(
        {"skills": skills, "result": result, "cases": cases}, indent=2))
    print(f"\n  arena accuracy: {result['accuracy']}")
    return 0 if result["accuracy"] >= 0.8 else 1
```

In `main()`: add `ap.add_argument("--arena", default=None, help="comma-separated skill names — pairwise routing eval")`; after self-test branch: `if args.arena: return run_arena(args)`.

- [ ] **Step 4: Run to verify pass** — `python3 scripts/eval_trigger.py --self-test` → all PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/eval_trigger.py
git commit -m "feat(eval): arena mode — which of N skill descriptions wins a prompt

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 15: SKILL.md template, references, capabilities.toml wiring, render support

**Files:**
- Create: `shared/skill-templates/khenrix-audit/SKILL.md.tmpl`
- Create: `shared/skill-templates/khenrix-audit/references/checks.md`
- Create: `shared/skill-templates/khenrix-audit/references/remediation-ladder.md`
- Create: `shared/skill-templates/khenrix-audit/references/probe-protocol.md`
- Create: `shared/skill-templates/khenrix-audit/references/ecosystem-evidence.md`
- Modify: `scripts/render.py` (TEMPLATED_SKILLS + sibling-dir copy)
- Modify: `capabilities.toml` (`[[skills]]` + `[skill_facts.khenrix-audit.*]` ×3 + khenrix-setup description fix)

**Interfaces:**
- Consumes: engine CLI surface from Tasks 1–13; `$description`, `$cli`, `$display_name`, `$engine_path`, `$phases_note` template tokens.
- Produces: rendered `marketplaces/<cli>/plugins/khenrix-utils/skills/khenrix-audit/{SKILL.md,scripts/setup_audit.py,references/*.md}`.

- [ ] **Step 1: render.py — templated skills ship sibling dirs**

In `scripts/render.py` change `TEMPLATED_SKILLS` (line 41) to:

```python
TEMPLATED_SKILLS = ("khenrix-setup", "khenrix-upgrade", "khenrix-audit")
```

In `render()` step 0, after `(dst / "SKILL.md").write_text(body)`, add:

```python
                # templated skills may ship engine/reference dirs next to the template
                for sub in ("scripts", "references"):
                    src_dir = TMPL_ROOT / skill / sub
                    if src_dir.is_dir():
                        sub_dst = dst / sub
                        if sub_dst.exists():
                            shutil.rmtree(sub_dst)
                        shutil.copytree(src_dir, sub_dst,
                                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
```

And in `clean()`, inside the `for skill in TEMPLATED_SKILLS:` loop, add:

```python
            for sub in ("scripts", "references"):
                shutil.rmtree(pdir / "skills" / skill / sub, ignore_errors=True)
```

- [ ] **Step 2: capabilities.toml — skills entry + facts + incumbent fix**

After the `[[skills]] name = "khenrix-upgrade"` block add:

```toml
[[skills]]
name = "khenrix-audit"
per_cli = true
```

In `[skill_facts.khenrix-setup.claude]` (line ~254) change the description's trigger sentence: replace `set up, sync, audit, or update` with `set up, sync, or update` — same edit in the codex and agy fact tables (grep: `grep -n "sync, audit" capabilities.toml`). "audit" now belongs to khenrix-audit alone.

Add the audit facts (adjacent to the other `skill_facts` tables; repeat for codex/agy with their `display_name`/`cli`/`engine_path`; agy's engine path is `${HOME}/.gemini/config/plugins/khenrix-utils/skills/khenrix-audit/scripts/setup_audit.py`):

```toml
[skill_facts.khenrix-audit.claude]
description = "Cross-CLI setup conflict finder: maps every installed plugin, skill, MCP server, hook and instruction file across Claude Code, Codex and agy, then finds duplicate or overlapping skills (which skill fires for a prompt), declared-vs-live drift, shadowed or endpoint-deduped MCP servers, conflicting hooks, and skill-listing budget overflow — with a guided per-finding apply and a synced decision ledger. NOT for one skill's content (use skill-tuneup), NOT for the wiki (use wiki-lint). Invoke as /khenrix-audit."
display_name = "Claude Code"
cli = "claude"
engine_path = "${CLAUDE_PLUGIN_ROOT}/skills/khenrix-audit/scripts/setup_audit.py"
phases_note = "All phases available: this CLI can run trigger probes (Phase D tier 2) and produce token counts via `claude plugin details`."
```

- [ ] **Step 3: Write the SKILL.md template**

`shared/skill-templates/khenrix-audit/SKILL.md.tmpl` — thin dispatcher, <200 lines. Body content:

```markdown
---
name: khenrix-audit
description: >-
  $description
allowed-tools: Bash, Read, Grep
---

# khenrix-audit — cross-CLI setup audit ($display_name)

One run: deterministic engine → model adjudication → evidence → report → guided
apply. The engine is read-only except its ledger subcommands. Full check
catalog: `references/checks.md`. Spec: docs/superpowers/specs/2026-07-30-khenrix-audit-design.md.

$phases_note

## 1. Run the engine

Locate the engine and the canonical checkout (repo writes NEVER target rendered
copies — resolve ~/git/khenrix-utils and pass it explicitly):

```bash
python3 "$engine_path" findings --repo-root "$$HOME/git/khenrix-utils" --out /tmp/audit-findings.json --report-dir "$$HOME/git/khenrix-utils/docs/setup-audit"
```

On Claude, FIRST produce token counts so B7 runs (skip elsewhere — B7 then
reports NOT EVALUATED, which is correct):

```bash
claude plugin list
```

For each installed plugin run `claude plugin details <name>`, collect the
"Always-on" numbers into `/tmp/audit-tokens.json` as `{"<plugin>": <tokens>}`,
then re-run the engine with `--tokens-file /tmp/audit-tokens.json`.

## 2. Read findings.json — branch on `capabilities`, not CLI name

- `errors` non-empty → report them first (a walker crash is a finding, not noise).
- Findings marked `informational` (semantics unverified) NEVER reach the apply loop.

## 3. Adjudicate B6 nominations (Phase C)

For each B6 finding, read ONLY the two trigger surfaces + frontmatter from the
evidence, classify DISTINCT / AMBIGUOUS / DUPLICATE with one sentence of
reasoning. Full-body reads: at most 3 pairs per run. Do not act on a
nomination without adjudication.

## 4. Evidence for AMBIGUOUS pairs (Phase D)

Tier 1 (any CLI): arena eval — see `references/probe-protocol.md` §Arena.
Tier 2 (`capabilities.can_probe` only): live probes — §Live-probes. k ≥ 3 per
prompt, report fire-rates, INCONCLUSIVE when unstable.

## 5. Ecosystem discovery (Phase E)

Follow `references/ecosystem-evidence.md`. Serve from
docs/setup-audit/ecosystem-cache.json when fresh (30-day TTL); refresh on
request. Every claim needs a citation; no citation → no finding. Web failure →
report DISCOVERY INCOMPLETE.

## 6. Report (Phase F)

The engine already wrote latest.md + runs/. Add model annotations (adjudications,
probe results, ecosystem findings) BELOW the mechanical findings — never delete
or renumber them.

## 7. Guided apply (Phase G)

Walk non-informational findings by severity. Per finding offer:
apply / not now / waive (reason + until) / accept current state / reject / detail.
Rules, all mandatory — see `references/remediation-ladder.md` for the rung table:

- Every action binds to the inventory hash in findings.json; if config changed
  since, re-run the engine first.
- Destructive ops (any removal/disable): confirm individually, write a restore
  bundle (`*.khenrix-backup` convention) BEFORE, record in the ledger AFTER.
- Repo edits: only in the canonical checkout, description-only changes eval-gated
  via arena (`references/remediation-ladder.md` §Rung-1 gate); body changes hand
  off to skill-tuneup.
- Waive → record via the engine, never by hand:

```bash
python3 "$engine_path" ledger-add --repo-root "$$HOME/git/khenrix-utils" --id <finding-id> --state waived --fingerprint <fp> --reason "<why>" --until <ISO8601>
```

- After any live mutation, re-run the engine and mark superseded queue items
  stale — do not keep applying from the old snapshot.
```

(`$$` renders a literal `$` under `string.Template`.)

- [ ] **Step 4: Write the four reference docs**

`references/checks.md` — one section per B1–B16: what it detects, the verified
semantics it relies on, evidence fields, false-positive notes. Copy the check
list from spec §4.1 table verbatim, then add per-check "evidence fields" lines
matching the engine (`bare_name`/`paths` for B1, `reference`/`config`/`expected_prefix`
for B2, etc. — keep in sync with `setup_audit.py`).

`references/remediation-ladder.md`:

```markdown
# Remediation ladder — cheapest available rung first

| Rung | Action | Available when | Gate |
|------|--------|----------------|------|
| 1 | Narrow OUR skill's description (frontmatter only) | subject skill lives in khenrix-utils shared/ or skill-templates/ | arena eval over the edited skill + every skill it was nominated against, accuracy ≥ 0.8, then `make eval SKILL=<name>` receipt |
| 2 | Disable the offending plugin | any plugin | list EVERYTHING the plugin provides first (skills, MCP, hooks, agents); user confirms the full loss |
| 3 | Vendor the one wanted skill into khenrix-utils | plugin skill worth keeping | record `vendored_from` in the copy's frontmatter meta; staleness tracking belongs to skill-tuneup; license/provenance reviewed |
| — | Anything else | — | advisory only, report text |

## Rung-1 gate detail
Description edits reroute prompts BETWEEN skills — a single-skill receipt cannot
see that. Run: `python3 scripts/eval_trigger.py --arena <edited>,<neighbor1>,...`
with prompts in `evals/<edited>/arena.json` covering both sides' trigger phrases.
Body-level changes are skill-tuneup's job — hand off, do not apply here.
```

`references/probe-protocol.md`:

```markdown
# Trigger evidence protocol

## Arena (tier 1 — any CLI, cheap, also the rung-1 gate)
1. Write evals/<skillA>/arena.json: {"prompts": [{"prompt": "...", "expected": "<name-or-none>"}]}
   — 4+ prompts per side, including near-misses that share keywords but belong elsewhere.
2. Run: python3 scripts/eval_trigger.py --arena skillA,skillB
3. Read the confusion matrix. Misrouted prompts = the collision is real.

## Live probes (tier 2 — capabilities.can_probe only, top AMBIGUOUS pairs)
Validated mechanism (2026-07-30): the stream-json transcript exposes the fired skill.
1. Isolation is mandatory: probes both perturb and are perturbed by invocation
   history (descriptions drop least-invoked-first). Create a throwaway
   CLAUDE_CONFIG_DIR containing ONLY the pair under test + two fixed decoy skills.
2. Per prompt, k >= 3 runs of:
   claude -p "<probe>" --output-format stream-json --max-turns 1 --permission-mode plan
3. Parse tool_use events for `"name": "Skill"`; record `input.skill` per run.
4. Report fire-RATES per skill. Unstable (no skill >= 2/3) → INCONCLUSIVE.
5. A model answering inline without invoking any skill is a valid outcome —
   record as "no-skill", not as a miss for either side.
6. State the probe cost up front (k × prompts × pairs turns) and get confirmation.
```

`references/ecosystem-evidence.md`:

```markdown
# Ecosystem discovery — evidence gate

Subjects: the engine's findings.json lists every loaded plugin + MCP server.
Cache: docs/setup-audit/ecosystem-cache.json {subject: {checked, verdict, citations}};
serve entries younger than 30 days; refresh on user request or expiry.

Order of evidence (stop at the first that answers):
1. Registry facts: GitHub releases / npm registry — last release date, archived flag.
2. Vendor docs / changelogs — deprecation or supersession notices.
3. Marketplace manifests — official vs community publisher.
4. Web search — only for "is there a better X", never as the sole citation.

A replacement recommendation REQUIRES all of: overlapping tool surface (named
tools), maintenance signal (release within 12 months or explicit LTS), publisher
status, and a migration cost note. Additions may be suggested ONLY from gaps the
user stated or the report demonstrates (never from mining session history).
No citation → no finding. Web failure → DISCOVERY INCOMPLETE (never "no
replacements exist"). Everything here is ADVISORY — no auto-apply.
```

- [ ] **Step 5: Render and validate**

Run: `make render`
Expected: `rendered: … 3 templated skill(s)` → check `marketplaces/claude/plugins/khenrix-utils/skills/khenrix-audit/` contains `SKILL.md`, `scripts/setup_audit.py`, `references/` (4 files). Then `python3 scripts/render.py --check` → `validation ok`.

- [ ] **Step 6: Re-seed receipts staled by the render.py edit**

`render.py` is in every skill's receipt closure, so this change correctly stales all receipts. Per repo convention, bless unchanged skills and fully re-eval the changed ones (khenrix-setup re-evals in Task 16):

Run: `python3 scripts/eval_harness.py --seed-receipt` for each skill whose behavior did NOT change (list them from the `make precommit` failure output).
Expected: `make verify` passes with only khenrix-setup + khenrix-audit receipts stale.

- [ ] **Step 7: Commit**

```bash
git add shared/skill-templates/khenrix-audit scripts/render.py capabilities.toml marketplaces/ evals/
git commit -m "feat(khenrix-audit): skill template + references, render wiring, incumbent de-audit

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 16: Eval gate — cases, receipts, precommit

**Files:**
- Create: `evals/khenrix-audit/evals.json`, `evals/khenrix-audit/triggers.json`, `evals/khenrix-audit/arena.json`
- Test: `make eval SKILL=khenrix-audit`, `make eval SKILL=khenrix-setup`

- [ ] **Step 1: Read the harness schema**

Run: `python3 -c "import json; print(json.dumps(json.load(open('evals/chunk-map/evals.json')), indent=1)[:2000])"` and read `docs/skill-eval-process.md`. Follow that exact case schema.

- [ ] **Step 2: Write the eval inputs**

`evals/khenrix-audit/triggers.json` (single-skill trigger eval):

```json
{
 "should_trigger": [
  "audit my whole claude/codex/agy setup for conflicts",
  "which of my installed skills overlap or fire for the same prompt?",
  "do I have duplicate MCP servers or plugins across my CLIs?",
  "find drift between capabilities.toml and my live CLI config",
  "is my skill listing over the context budget?",
  "/khenrix-audit"
 ],
 "near_miss": [
  "tune up the markitdown skill",
  "lint the wiki for orphan pages",
  "audit and improve my CLAUDE.md files",
  "sync my claude setup with the khenrix source of truth",
  "review this pull request for security issues"
 ]
}
```

`evals/khenrix-audit/arena.json` (routing vs the incumbents — used by rung-1 gates and the audit's own self-check):

```json
{
 "prompts": [
  {"prompt": "audit my setup for conflicting skills", "expected": "khenrix-audit"},
  {"prompt": "set up my claude environment with the shared MCP servers", "expected": "khenrix-setup"},
  {"prompt": "is chunk-map stale? give it a maintenance pass", "expected": "skill-tuneup"},
  {"prompt": "health check the wiki", "expected": "none"},
  {"prompt": "do my installed plugins duplicate each other?", "expected": "khenrix-audit"},
  {"prompt": "upgrade how this machine uses claude models", "expected": "none"}
 ]
}
```

`evals/khenrix-audit/evals.json`: 3 behavior cases following the chunk-map schema — (1) "run a read-only audit of this fixture home and list the findings" against a scaffold that recreates the Task 3–5 fixtures on disk, graded on naming the B1 collision and the B4 managed-absent hit; (2) "waive finding <id> for 60 days" graded on using `ledger-add` (not hand-editing); (3) "what would disabling plugin X cost me?" graded on enumerating the plugin's full component list before recommending. Copy grader phrasing style from `evals/chunk-map/evals.json`.

- [ ] **Step 3: Trigger + arena baselines**

Run: `python3 scripts/eval_trigger.py --skill khenrix-audit`
Expected: accuracy ≥ 0.8 (fix the description in `capabilities.toml` facts + re-render if near-misses fire).
Run: `python3 scripts/eval_trigger.py --arena khenrix-audit,khenrix-setup,skill-tuneup`
Expected: accuracy ≥ 0.8.

- [ ] **Step 4: Full eval + receipts (costs tokens)**

Run: `make eval SKILL=khenrix-audit` then `make eval SKILL=khenrix-setup` (its description changed).
Expected: `run_summary.delta.pass_rate >= 0` each; receipts written.

- [ ] **Step 5: Gate + commit**

Run: `make precommit`
Expected: `✅ precommit clean`.

```bash
git add evals/
git commit -m "eval(khenrix-audit): trigger/arena/behavior cases + fresh receipts

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 17: First real run + refresh

**Files:**
- Create (generated): `docs/setup-audit/latest.md`
- No source changes.

- [ ] **Step 1: Refresh installed plugins**

Run: `make khenrix-refresh`
Expected: plugin pushed into all installed CLIs (Claude/Codex cache by version — required for the new skill to load).

- [ ] **Step 2: Produce token counts, run the engine against the REAL machine**

```bash
for p in $(claude plugin list 2>/dev/null | grep -oP '(?<=❯ )[a-z-]+(?=@)'); do claude plugin details "$p"; done
```

Collect the Always-on numbers into `/tmp/audit-tokens.json`, then:

```bash
python3 shared/skill-templates/khenrix-audit/scripts/setup_audit.py findings --repo-root "$HOME/git/khenrix-utils" --tokens-file /tmp/audit-tokens.json --out /tmp/audit-findings.json --report-dir "$HOME/git/khenrix-utils/docs/setup-audit"
```

- [ ] **Step 3: Verify the three known-truth findings appear**

Check `/tmp/audit-findings.json` contains: (a) B4 `managed-absent-but-live` for google-drive on claude AND codex; (b) B8 duplicate-body hook for wiki-autosave-gate (user settings + khenrix plugin); (c) B6 nominating `claude-obsidian:save` ↔ `khenrix-utils:khenrix-wiki-add` in its top pairs. Also verify ZERO findings reference `marketplaces/` paths (rendered-artifact exclusion holds) and `scan_artifact_text(latest.md)` found nothing (the run would have failed otherwise).
If any of the three is missing, that is a bug — fix the responsible walker/check, add the regression to `tests/test_setup_audit.py`, re-run.

- [ ] **Step 4: Commit the first report + updated receipt state**

```bash
git add docs/setup-audit/
git commit -m "audit: first real run — known-truth findings confirmed on this machine

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 18 (deferred, optional): khenrix-setup managed-install stamping

B4's `managed-provenance` tier currently rests on declared-state + ledger policies, which covers the google-drive class. Full provenance ("khenrix-setup installed this, so its disappearance from declarations is meaningful") needs setup to stamp what it installs:

- [ ] **Step 1:** Read the apply path: `grep -n "def .*apply\|def add_" scripts/lib/reconcile.py | head -20`, then read those functions.
- [ ] **Step 2:** Add to `scripts/lib/reconcile.py` a module-level helper and call it from each function that performs an ADD action, passing the entry kind + name:

```python
def stamp_managed(kind: str, name: str) -> None:
    """Sidecar record of what khenrix-setup installed — read by khenrix-audit B4."""
    import json as _json
    from pathlib import Path as _P
    p = _P.home() / ".local/state/khenrix/managed.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    doc = _json.loads(p.read_text()) if p.exists() else {"schema_version": 1, "installed": {}}
    doc["installed"][f"{kind}:{name}"] = True
    tmp = p.with_suffix(".tmp")
    tmp.write_text(_json.dumps(doc, indent=1, sort_keys=True))
    import os as _os
    _os.replace(tmp, p)
```

- [ ] **Step 3:** In `setup_audit.py` `check_b4_drift`, upgrade an `unmanaged` finding to `drift`/`medium` when `managed.json` contains its key (load in `cmd_findings` into `ctx["managed"]`). Add a test mirroring `test_b4_live_unknown_is_info_only` but with the stamp present.
- [ ] **Step 4:** `make verify` (reconcile.py edit stales all receipts — re-seed unchanged, re-eval khenrix-setup), `make precommit`, commit.

---

## Self-review (done at plan time)

- **Spec coverage:** engine+checks (T1–11), ledger (T12), report/`--check` (T13), arena (T14), SKILL.md/references/render/per-CLI facts/incumbent fix (T15), eval gate incl. arena self-check (T16), known-truth first run (T17), setup stamping (T18). Probe/ecosystem phases are model-procedure, delivered as reference docs (T15) per spec §4.3. Not planned (spec "out of scope" or advisory-only): auto-apply, remote machines.
- **Placeholder scan:** T16 Step 2's evals.json defers to the harness schema read in Step 1 with concrete case content specified — acceptable because the schema is repo-internal and read first; everything else carries full code.
- **Type consistency:** `item()`/`finding()` signatures fixed in T1/T6 and used verbatim after; `ctx` keys (`repo_root`, `home`, `now`, `policies`, `tokens`, `context_window`, `entries`, `waived`) introduced before first consumer.
