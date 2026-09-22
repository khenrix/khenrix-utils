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
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import reconcile  # bundled alongside this script

CLIS = ("claude", "codex", "agy")
DIRECT_TARGET = {
    "claude": "claude",
    "codex": "codex_maka",
    "agy": "agy",
}
SHA256_ID = re.compile(r"^sha256:[0-9a-f]{64}$")

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
        "model_discovery": "The default model label is stored in ~/.gemini/antigravity-cli/settings.json; "
                           "see `agy changelog` + Gemini docs before changing it.",
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
        agents = cfg.get("agents", {})
        if not isinstance(agents, dict):
            agents = {}
        return {
            "model": cfg.get("model"),
            "model_reasoning_effort": cfg.get("model_reasoning_effort"),
            "plan_mode_reasoning_effort": cfg.get("plan_mode_reasoning_effort"),
            "default_subagent_reasoning_effort": agents.get(
                "default_subagent_reasoning_effort"),
            "personality": cfg.get("personality"),
            "features": sorted(feats) if isinstance(feats, dict) else feats,
        }
    if cli == "claude":
        p = Path(reconcile.expand("${HOME}/.claude/settings.json"))
        data = json.loads(p.read_text()) if p.exists() and p.stat().st_size else {}
        return {
            "model": data.get("model", "(default — account/global)"),
            "effortLevel": data.get("effortLevel"),
            "ultracode": data.get("ultracode"),
            "permissions": "set" if data.get("permissions") else "(none)",
            "note": "Portable model/effort/ultracode defaults are declared in capabilities.toml.",
        }
    p = reconcile.agy_settings_path()
    data = reconcile.read_json_object(p)
    return {"model": data.get("model", "(default — account/global)"),
            "note": "The High effort tier is encoded in agy's selected model label."}


def _read_capabilities(path: Path) -> dict:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _plugin_skills(caps_path: Path) -> set[str]:
    skills_dir = caps_path.parent / "skills"
    if not skills_dir.is_dir() or skills_dir.is_symlink():
        return set()
    return {
        item.name
        for item in skills_dir.iterdir()
        if item.is_dir()
        and not item.is_symlink()
        and (item / "SKILL.md").is_file()
        and not (item / "SKILL.md").is_symlink()
    }


def _direct_skills(cli: str, caps: dict) -> set[str]:
    """Return only receipt-backed skills owned by selective delivery for ``cli``."""

    delivery = caps.get("skill_delivery")
    if not isinstance(delivery, dict):
        return set()
    declared = delivery.get("skills")
    targets = delivery.get("targets")
    state_dir = delivery.get("state_dir")
    target_name = DIRECT_TARGET[cli]
    if (
        not isinstance(declared, list)
        or not all(isinstance(name, str) and name for name in declared)
        or not isinstance(targets, dict)
        or not isinstance(targets.get(target_name), str)
        or not isinstance(state_dir, str)
    ):
        return set()

    receipt_path = Path(reconcile.expand(state_dir)) / "install-receipt.json"
    if not receipt_path.is_file() or receipt_path.is_symlink():
        return set()
    try:
        receipt = json.loads(receipt_path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return set()
    if not isinstance(receipt, dict):
        return set()
    records = receipt.get("skills")
    if receipt.get("schema_version") != 1 or not isinstance(records, dict):
        return set()

    expected_root = Path(reconcile.expand(targets[target_name])).absolute()
    installed: set[str] = set()
    # The declaration is the authority boundary. Extra receipt or native-root
    # entries are deliberately ignored, so another installer cannot become
    # Khenrix-owned merely by writing beside these two skills.
    for name in sorted(set(declared)):
        record = records.get(name)
        target_records = record.get("targets") if isinstance(record, dict) else None
        target = target_records.get(target_name) if isinstance(target_records, dict) else None
        expected_path = expected_root / name
        skill_file = expected_path / "SKILL.md"
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("source_hash"), str)
            or not SHA256_ID.fullmatch(record["source_hash"])
            or not isinstance(target, dict)
            or target.get("path") != str(expected_path)
            or target.get("hash") != record["source_hash"]
            or expected_path.is_symlink()
            or not expected_path.is_dir()
            or skill_file.is_symlink()
            or not skill_file.is_file()
        ):
            continue
        installed.add(name)
    return installed


def installed_skills(cli: str) -> list[str]:
    caps = reconcile.find_upwards("capabilities.toml", Path(__file__).resolve().parent)
    if not caps:
        return []
    capabilities = _read_capabilities(caps)
    return sorted(_plugin_skills(caps) | _direct_skills(cli, capabilities))


def snapshot(cli: str) -> dict:
    return {
        "cli": cli,
        "version": version(cli),
        "model_settings": model_settings(cli),
        "mcp_servers": sorted(reconcile.mcp_current(cli)),
        "installed_khenrix_skills": installed_skills(cli),
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
