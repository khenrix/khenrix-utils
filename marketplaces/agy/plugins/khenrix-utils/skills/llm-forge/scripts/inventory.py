#!/usr/bin/env python3
"""khenrix-upgrade inventory helper.

Prints a deterministic snapshot of a CLI's current setup — the baseline the
khenrix-upgrade skill researches against. Reuses the inspection functions in
reconcile.py (bundled in the same scripts/ dir after render).

Usage:
  inventory.py --cli claude        # snapshot for one CLI
  inventory.py --cli codex --json  # machine-readable
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import reconcile  # bundled alongside this script

CLIS = ("claude", "codex", "agy")

# Where to research changes + which native tooling reviews skills, per CLI.
DOCS = {
    "claude": {
        "docs": "https://code.claude.com/docs",
        "changelog": "https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md",
        "model_discovery": "Research latest Claude models via WebSearch / the deep-research skill; "
                           "set via ~/.claude/settings.json `model` or the /model command.",
        "review_tools": "skill-creator skill; plugin-dev `skill-reviewer` & `plugin-validator` agents; "
                        "built-in update-config skill.",
        "version_cmd": ["claude", "--version"],
    },
    "codex": {
        "docs": "https://developers.openai.com",
        "changelog": "`codex --version` + https://developers.openai.com/codex",
        "model_discovery": "openaiDeveloperDocs MCP (search_openai_docs/fetch_openai_doc) + "
                           "~/.codex/skills/.system/openai-docs/scripts/resolve-latest-model-info.js; "
                           "set `model` / `model_reasoning_effort` in ~/.codex/config.toml.",
        "review_tools": "~/.codex/skills/.system/skill-creator (quick_validate.py); "
                        "plugin-creator (validate_plugin.py).",
        "version_cmd": ["codex", "--version"],
    },
    "agy": {
        "docs": "https://ai.google.dev/gemini-api/docs",
        "changelog": "`agy changelog`",
        "model_discovery": "Gemini model is chosen at runtime via the API; see `agy changelog` + Gemini docs.",
        "review_tools": "`agy plugin validate <dir>` (no skill-creator/plugin-creator on agy).",
        "version_cmd": ["agy", "changelog"],
    },
}


def run(cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return (r.stdout or r.stderr).strip()
    except Exception as e:  # noqa: BLE001
        return f"(unavailable: {e})"


def version(cli: str) -> str:
    out = run(DOCS[cli]["version_cmd"])
    return out.splitlines()[0] if out else "(unknown)"


def model_settings(cli: str) -> dict:
    if cli == "codex":
        cfg = reconcile.codex_load()
        feats = cfg.get("features", {})
        return {
            "model": cfg.get("model"),
            "model_reasoning_effort": cfg.get("model_reasoning_effort"),
            "plan_mode_reasoning_effort": cfg.get("plan_mode_reasoning_effort"),
            "personality": cfg.get("personality"),
            "features": sorted(feats) if isinstance(feats, dict) else feats,
        }
    if cli == "claude":
        p = Path(reconcile.expand("${HOME}/.claude/settings.json"))
        data = json.loads(p.read_text()) if p.exists() and p.stat().st_size else {}
        return {
            "model": data.get("model", "(default — account/global)"),
            "permissions": "set" if data.get("permissions") else "(none)",
            "note": "Claude has no static reasoning/experimental keys; tune via /model and settings.json.",
        }
    return {"model": "(runtime — chosen by Gemini API)",
            "note": "agy exposes no static model/reasoning keys; recommendations only."}


def installed_skills() -> list[str]:
    caps = reconcile.find_upwards("capabilities.toml", Path(__file__).resolve().parent)
    if not caps:
        return []
    skills_dir = caps.parent / "skills"
    if not skills_dir.exists():
        return []
    return sorted(d.name for d in skills_dir.iterdir() if (d / "SKILL.md").exists())


def snapshot(cli: str) -> dict:
    return {
        "cli": cli,
        "version": version(cli),
        "model_settings": model_settings(cli),
        "mcp_servers": sorted(reconcile.mcp_current(cli)),
        "installed_khenrix_skills": installed_skills(),
        "research_inputs": {k: DOCS[cli][k] for k in ("docs", "changelog", "model_discovery", "review_tools")},
    }


def print_human(s: dict):
    print(f"\n=== khenrix-upgrade inventory · {s['cli']} ===")
    print(f"\nVersion: {s['version']}")
    print("\nModel / settings:")
    for k, v in s["model_settings"].items():
        print(f"  {k}: {v}")
    print(f"\nMCP servers ({len(s['mcp_servers'])}): {', '.join(s['mcp_servers']) or '(none)'}")
    print(f"\nInstalled khenrix skills: {', '.join(s['installed_khenrix_skills']) or '(none)'}")
    print("\nResearch inputs:")
    for k, v in s["research_inputs"].items():
        print(f"  {k}: {v}")
    print("\nNext: research the latest version/model/best-practices for this CLI, then review the skills above.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Snapshot a CLI's setup for khenrix-upgrade")
    ap.add_argument("--cli", choices=CLIS, required=True)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    s = snapshot(args.cli)
    if args.json:
        print(json.dumps(s, indent=2))
    else:
        print_human(s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
