#!/usr/bin/env python3
"""khenrix-utils reconcile engine.

Reads the bundled capabilities.toml and reconciles one CLI's live config
(MCP servers, baseline settings, base instructions) toward it — ADDITIVELY.

Guarantees:
  * Only entries declared in capabilities.toml are ever written. Anything else
    in the live config (machine-specific extras) is reported but never touched.
  * ADD-only by default: missing declared entries are added. Drift on an entry
    that already exists is reported but NOT overwritten unless --update-drift.
  * Every file is backed up to *.khenrix-backup before being modified.

Runs both from the repo (scripts/lib/reconcile.py, capabilities.toml at repo
root) and bundled inside a plugin (…/skills/khenrix-setup/scripts/reconcile.py,
capabilities.toml copied next to the plugin root) — it locates the manifest by
walking upwards.

Usage:
  reconcile.py --cli claude                 # read-only review (default)
  reconcile.py --cli claude --apply         # add missing declared entries
  reconcile.py --cli codex --apply --update-drift
  reconcile.py --status --all               # review every CLI
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

CLIS = ("claude", "codex", "agy")
MANAGED_BEGIN = "<!-- khenrix-managed:begin house-style -->"
MANAGED_END = "<!-- khenrix-managed:end house-style -->"

# ----- pretty status markers -------------------------------------------------
MARK = {"MATCH": "✅", "ADD": "➕", "UPDATE": "✏️ ", "EXTRA": "⏭️ ", "INFO": "•"}


# ----- discovery / helpers ---------------------------------------------------
def find_upwards(name: str, start: Path) -> Path | None:
    d = Path(start).resolve()
    for p in [d, *d.parents]:
        cand = p / name
        if cand.exists():
            return cand
    return None


def load_caps() -> dict:
    here = Path(__file__).resolve().parent
    caps_path = find_upwards("capabilities.toml", here)
    if not caps_path:
        sys.exit("error: capabilities.toml not found (looked upward from %s)" % here)
    with open(caps_path, "rb") as f:
        data = tomllib.load(f)
    data["_dir"] = caps_path.parent
    return data


def expand(v):
    if isinstance(v, str):
        return os.path.expandvars(os.path.expanduser(v))
    if isinstance(v, list):
        return [expand(x) for x in v]
    if isinstance(v, dict):
        return {k: expand(x) for k, x in v.items()}
    return v


def current_os() -> str:
    return {"linux": "linux", "darwin": "darwin", "windows": "windows"}.get(
        platform.system().lower(), platform.system().lower()
    )


def run(cmd: list[str]):
    return subprocess.run(cmd, capture_output=True, text=True)


def backup(path) -> Path | None:
    p = Path(path)
    if p.exists():
        b = p.with_suffix(p.suffix + ".khenrix-backup")
        shutil.copy2(p, b)
        return b
    return None


# ----- desired state ---------------------------------------------------------
def desired_mcp(caps: dict, cli: str) -> dict:
    """name -> spec, platform-filtered, including the provider docs server."""
    out = {}
    osname = current_os()
    for name, spec in caps.get("mcp_servers", {}).items():
        plat = spec.get("platform")
        if plat and plat != osname:
            continue
        out[name] = spec
    docs = caps.get("docs_mcp", {}).get(cli)
    if docs:
        out[docs["name"]] = {k: v for k, v in docs.items() if k != "name"}
    return out


def is_http(spec: dict) -> bool:
    return spec.get("transport") == "http" or (spec.get("url") and not spec.get("command"))


# ----- live state: MCP -------------------------------------------------------
def claude_mcp_current() -> dict:
    res = run(["claude", "mcp", "list"])
    cur = {}
    for line in res.stdout.splitlines():
        line = line.rstrip()
        if not line or line.startswith("Checking") or ": " not in line:
            continue
        name, rest = line.split(": ", 1)
        endpoint = rest.rsplit(" - ", 1)[0].strip() if " - " in rest else rest.strip()
        # `claude mcp list` appends a transport annotation like " (HTTP)"/" (SSE)".
        endpoint = re.sub(r"\s*\((?:HTTP|SSE|STDIO|stdio)\)\s*$", "", endpoint)
        cur[name.strip()] = {"endpoint": endpoint}
    return cur


def codex_config_path() -> Path:
    return Path(expand("${HOME}/.codex/config.toml"))


def codex_load() -> dict:
    p = codex_config_path()
    if not p.exists():
        return {}
    with open(p, "rb") as f:
        return tomllib.load(f)


def codex_mcp_current() -> dict:
    return codex_load().get("mcp_servers", {})


def agy_mcp_path() -> Path:
    return Path(expand("${HOME}/.gemini/config/mcp_config.json"))


def agy_mcp_load() -> dict:
    p = agy_mcp_path()
    if not p.exists() or p.stat().st_size == 0:
        return {}
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return {}


def agy_mcp_current() -> dict:
    return agy_mcp_load().get("mcpServers", {})


def mcp_current(cli: str) -> dict:
    return {"claude": claude_mcp_current, "codex": codex_mcp_current, "agy": agy_mcp_current}[cli]()


def mcp_drift(cli: str, spec: dict, cur: dict) -> str | None:
    spec = expand(spec)
    if is_http(spec):
        want = spec.get("url")
        have = {"claude": cur.get("endpoint"), "codex": cur.get("url"),
                "agy": cur.get("httpUrl") or cur.get("url")}[cli]
        if want and have and want != have:
            return f"url: {have} → {want}"
        return None
    if cli in ("codex", "agy"):
        wc, wa = spec.get("command"), list(spec.get("args", []))
        hc, ha = cur.get("command"), list(cur.get("args", []))
        if wc and hc and (wc != hc or wa != ha):
            return "command/args differ"
    return None


def classify_mcp(cli: str, desired: dict, current: dict):
    rows, extras = [], [n for n in current if n not in desired]
    for name, spec in desired.items():
        if name in current:
            d = mcp_drift(cli, spec, current[name])
            rows.append([name, "UPDATE" if d else "MATCH", d or ""])
        else:
            rows.append([name, "ADD", "will add"])
    return rows, extras


# ----- live state: settings --------------------------------------------------
def settings_report(cli: str, caps: dict):
    """Return (rows, apply_fn). apply_fn(update_drift) -> list[str] of actions."""
    want = caps.get("settings", {})
    if cli == "codex":
        return codex_settings(want)
    if cli == "agy":
        return agy_settings(want)
    return claude_settings(want)


def codex_settings(want: dict):
    cfg = codex_load()
    rows, todo = [], []
    checks = [("approval_policy", want.get("approval_policy")),
              ("sandbox_mode", want.get("sandbox"))]
    for key, val in checks:
        if val is None:
            continue
        have = cfg.get(key)
        if have == val:
            rows.append([key, "MATCH", val])
        elif have is None:
            rows.append([key, "ADD", val]); todo.append((key, val))
        else:
            rows.append([key, "UPDATE", f"{have} → {val}"])
    projects = cfg.get("projects", {})
    for path in expand(want.get("trusted_paths", [])):
        trusted = projects.get(path, {}).get("trust_level") == "trusted"
        if trusted:
            rows.append([f"trust {path}", "MATCH", "trusted"])
        else:
            rows.append([f"trust {path}", "ADD", "trusted"]); todo.append(("trust", path))

    def apply(update_drift):
        if not todo:
            return []
        p = codex_config_path()
        backup(p)
        chunks = []
        for kind, a in todo:
            if kind == "trust":
                chunks.append(f'\n[projects."{a}"]\ntrust_level = "trusted"\n')
            else:
                chunks.append(f'{kind} = "{a}"\n')
        # top-level keys must precede any table; prepend simple keys, append tables.
        simple = "".join(c for c in chunks if not c.lstrip().startswith("["))
        tables = "".join(c for c in chunks if c.lstrip().startswith("["))
        text = p.read_text() if p.exists() else ""
        if simple:
            text = simple + text
        text = text + tables
        p.write_text(text)
        return [f"codex settings: wrote {len(todo)} key(s) to {p}"]

    return rows, apply


def agy_settings_path() -> Path:
    return Path(expand("${HOME}/.gemini/antigravity-cli/settings.json"))


def agy_settings(want: dict):
    p = agy_settings_path()
    data = json.loads(p.read_text()) if p.exists() and p.stat().st_size else {}
    rows, missing = [], []
    tw = data.get("trustedWorkspaces", [])
    for path in expand(want.get("trusted_paths", [])):
        if path in tw:
            rows.append([f"trust {path}", "MATCH", "trusted"])
        else:
            rows.append([f"trust {path}", "ADD", "trusted"]); missing.append(path)
    rows.append(["approval/sandbox", "INFO", "agy prompts per-action; no static key"])

    def apply(update_drift):
        if not missing:
            return []
        backup(p)
        p.parent.mkdir(parents=True, exist_ok=True)
        data.setdefault("trustedWorkspaces", [])
        data["trustedWorkspaces"].extend(m for m in missing if m not in data["trustedWorkspaces"])
        p.write_text(json.dumps(data, indent=2) + "\n")
        return [f"agy settings: added {len(missing)} trusted workspace(s) to {p}"]

    return rows, apply


def claude_settings(want: dict):
    rows = [["approval/sandbox", "INFO", "Claude uses permissions/--permission-mode, not static keys"]]
    if want.get("trusted_paths"):
        rows.append(["trusted_paths", "INFO", "Claude trusts on first run; not reconciled here"])
    return rows, (lambda update_drift: [])


# ----- live state: instructions ----------------------------------------------
def managed_block(caps: dict) -> str:
    src = caps["_dir"] / caps["instructions"]["source"]
    text = src.read_text()
    i, j = text.find(MANAGED_BEGIN), text.find(MANAGED_END)
    if i == -1 or j == -1:
        return text.strip() + "\n"
    return text[i:j + len(MANAGED_END)]


def instructions_report(cli: str, caps: dict):
    target = Path(expand(caps["instructions"]["targets"][cli]))
    block = managed_block(caps)
    cur = target.read_text() if target.exists() else ""
    i, j = cur.find(MANAGED_BEGIN), cur.find(MANAGED_END)
    present = i != -1 and j != -1
    if present and cur[i:j + len(MANAGED_END)] == block:
        rows = [[str(target), "MATCH", "house-style block up to date"]]
    elif present:
        rows = [[str(target), "UPDATE", "house-style block drifted"]]
    else:
        rows = [[str(target), "ADD", "insert house-style block"]]

    def apply(update_drift):
        new = cur
        ni, nj = new.find(MANAGED_BEGIN), new.find(MANAGED_END)
        if ni != -1 and nj != -1:
            if new[ni:nj + len(MANAGED_END)] == block:
                return []
            if not update_drift:
                return [f"instructions: {target} block drifted (skipped; use --update-drift)"]
            new = new[:ni] + block + new[nj + len(MANAGED_END):]
        else:
            sep = "" if new == "" else ("\n" if new.endswith("\n") else "\n\n")
            new = new + sep + block + "\n"
        backup(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new)
        return [f"instructions: updated house-style block in {target}"]

    return rows, apply


# ----- MCP apply -------------------------------------------------------------
def toml_scalar(s: str) -> str:
    if "\\" in s or '"' in s:
        return "'''" + s + "'''"
    return '"' + s + '"'


def toml_array(a) -> str:
    return "[" + ", ".join(toml_scalar(x) for x in a) + "]"


def apply_mcp(cli: str, name: str, spec: dict) -> str:
    spec = expand(spec)
    if cli == "claude":
        if is_http(spec):
            cmd = ["claude", "mcp", "add", "--transport", "http", "--scope", "user", name, spec["url"]]
        else:
            cmd = ["claude", "mcp", "add", "--scope", "user", name]
            for k, v in (spec.get("env") or {}).items():
                cmd += ["-e", f"{k}={v}"]
            cmd += ["--", spec["command"], *spec.get("args", [])]
        res = run(cmd)
        return f"claude mcp add {name}: {'ok' if res.returncode == 0 else res.stderr.strip()}"
    if cli == "codex":
        p = codex_config_path()
        backup(p)
        lines = [f"\n[mcp_servers.{name}]"]
        if is_http(spec):
            lines.append(f"url = {toml_scalar(spec['url'])}")
        else:
            lines.append(f"command = {toml_scalar(spec['command'])}")
            if spec.get("args"):
                lines.append(f"args = {toml_array(spec['args'])}")
        env = spec.get("env") or {}
        if env:
            lines.append(f"\n[mcp_servers.{name}.env]")
            for k, v in env.items():
                lines.append(f"{k} = {toml_scalar(v)}")
        with open(p, "a") as f:
            f.write("\n".join(lines) + "\n")
        return f"codex: appended [mcp_servers.{name}] to {p}"
    # agy
    p = agy_mcp_path()
    backup(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = agy_mcp_load()
    if is_http(spec):
        entry = {"httpUrl": spec["url"]}
    else:
        entry = {"command": spec["command"]}
        if spec.get("args"):
            entry["args"] = spec["args"]
        if spec.get("env"):
            entry["env"] = spec["env"]
    data.setdefault("mcpServers", {})[name] = entry
    p.write_text(json.dumps(data, indent=2) + "\n")
    return f"agy: added mcpServers.{name} to {p}"


# ----- reporting -------------------------------------------------------------
def print_rows(title: str, rows, extras=None):
    print(f"\n{title}")
    if not rows:
        print("  (nothing declared)")
    for name, status, detail in rows:
        print(f"  {MARK.get(status, status):<3} {status:<7} {name}" + (f"  — {detail}" if detail else ""))
    for e in (extras or []):
        print(f"  {MARK['EXTRA']} EXTRA   {e}  — not managed by khenrix (left untouched)")


def reconcile(cli: str, caps: dict, apply: bool, update_drift: bool):
    print(f"\n=== khenrix-setup · {cli} ===")
    desired = desired_mcp(caps, cli)
    cur = mcp_current(cli)
    mcp_rows, extras = classify_mcp(cli, desired, cur)
    print_rows("MCP servers:", mcp_rows, extras)

    set_rows, set_apply = settings_report(cli, caps)
    print_rows("Settings:", set_rows)

    ins_rows, ins_apply = instructions_report(cli, caps)
    print_rows("Base instructions:", ins_rows)

    if not apply:
        adds = sum(1 for r in mcp_rows + set_rows + ins_rows if r[1] == "ADD")
        drift = sum(1 for r in mcp_rows + set_rows + ins_rows if r[1] == "UPDATE")
        print(f"\nReview only. {adds} to add, {drift} drifted. "
              f"Re-run with --apply to add missing entries.")
        return

    print("\nApplying (additive)…")
    actions = []
    for name, status, _ in mcp_rows:
        if status == "ADD":
            actions.append(apply_mcp(cli, name, desired[name]))
        elif status == "UPDATE" and update_drift:
            actions.append("drift update for MCP not auto-applied; edit manually: " + name)
    actions += set_apply(update_drift)
    actions += ins_apply(update_drift)
    if actions:
        for a in actions:
            print(f"  • {a}")
    else:
        print("  • nothing to do — already in sync")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Reconcile a CLI's config toward capabilities.toml")
    ap.add_argument("--cli", choices=CLIS)
    ap.add_argument("--all", action="store_true", help="review every CLI (read-only)")
    ap.add_argument("--status", action="store_true", help="read-only review (default)")
    ap.add_argument("--apply", action="store_true", help="add missing declared entries")
    ap.add_argument("--update-drift", action="store_true", help="also re-apply drifted managed entries")
    args = ap.parse_args(argv)

    caps = load_caps()
    if args.all or not args.cli:
        for c in CLIS:
            reconcile(c, caps, apply=False, update_drift=False)
        return 0
    reconcile(args.cli, caps, apply=args.apply and not args.status, update_drift=args.update_drift)
    return 0


if __name__ == "__main__":
    sys.exit(main())
