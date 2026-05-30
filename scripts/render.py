#!/usr/bin/env python3
"""Render shared assets into each per-CLI plugin, and validate skills.

Makes every plugin self-contained so it works after being installed/copied by a
marketplace:
  * copies capabilities.toml + house-style.md to the plugin root
  * copies shared/skills/<name>/ into the plugin's skills/
  * copies scripts/lib/reconcile.py into each khenrix-setup skill's scripts/
  * validates every SKILL.md (name + description, length/char rules)

Modes:
  render.py            render + validate
  render.py --check    validate only (non-zero exit on any problem)
  render.py --clean    remove rendered copies (keeps per-CLI khenrix-setup body)
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLIS = ("claude", "codex", "agy")
BUNDLED = ["capabilities.toml", "house-style.md"]
RECONCILE = ROOT / "scripts" / "lib" / "reconcile.py"
NAME_RE = re.compile(r"^[a-z0-9-]{1,64}$")


def plugin_dir(cli: str) -> Path:
    return ROOT / "marketplaces" / cli / "plugins" / "khenrix-utils"


def parse_frontmatter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    fm = {}
    for line in text[3:end].splitlines():
        if ":" in line and not line.startswith(" "):
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip().strip('"').strip("'")
    return fm


def validate_skill(skill_md: Path, problems: list):
    fm = parse_frontmatter(skill_md.read_text())
    rel = skill_md.relative_to(ROOT)
    name, desc = fm.get("name"), fm.get("description")
    if not name:
        problems.append(f"{rel}: missing 'name'")
    elif not NAME_RE.match(name):
        problems.append(f"{rel}: name '{name}' must be lowercase letters/numbers/hyphens, ≤64 chars")
    if not desc:
        problems.append(f"{rel}: missing 'description'")
    elif len(desc) > 1024:
        problems.append(f"{rel}: description >1024 chars ({len(desc)})")
    body_lines = skill_md.read_text().count("\n")
    if body_lines > 500:
        problems.append(f"{rel}: SKILL.md is {body_lines} lines (recommended <500)")


def iter_skills():
    for cli in CLIS:
        sk = plugin_dir(cli) / "skills"
        if sk.exists():
            yield from sk.glob("*/SKILL.md")


def render():
    shared_skills = sorted((ROOT / "shared" / "skills").glob("*/"))
    for cli in CLIS:
        pdir = plugin_dir(cli)
        pdir.mkdir(parents=True, exist_ok=True)
        # 1. bundle the source of truth
        for f in BUNDLED:
            shutil.copy2(ROOT / f, pdir / f)
        # 2. copy shared skills (canonical bodies) into the plugin
        for s in shared_skills:
            dst = pdir / "skills" / s.name
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(s, dst)
        # 3. bundle the reconcile engine next to the per-CLI khenrix-setup skill
        setup = pdir / "skills" / "khenrix-setup"
        if setup.exists():
            (setup / "scripts").mkdir(parents=True, exist_ok=True)
            shutil.copy2(RECONCILE, setup / "scripts" / "reconcile.py")
    print(f"rendered: bundled {BUNDLED} + reconcile.py into {len(CLIS)} plugins; "
          f"{len(shared_skills)} shared skill(s)")


def clean():
    removed = 0
    for cli in CLIS:
        pdir = plugin_dir(cli)
        for f in BUNDLED:
            (pdir / f).unlink(missing_ok=True)
            removed += 1
        recon = pdir / "skills" / "khenrix-setup" / "scripts" / "reconcile.py"
        recon.unlink(missing_ok=True)
    print(f"cleaned rendered copies ({removed} files targeted)")


def check() -> int:
    problems: list[str] = []
    skills = list(iter_skills())
    for s in skills:
        validate_skill(s, problems)
    # capabilities.toml must parse
    try:
        with open(ROOT / "capabilities.toml", "rb") as f:
            tomllib.load(f)
    except Exception as e:  # noqa: BLE001
        problems.append(f"capabilities.toml: {e}")
    if problems:
        print("VALIDATION FAILED:")
        for p in problems:
            print(f"  ✗ {p}")
        return 1
    print(f"validation ok: {len(skills)} skill(s), capabilities.toml parses")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Render + validate khenrix-utils plugins")
    ap.add_argument("--check", action="store_true", help="validate only")
    ap.add_argument("--clean", action="store_true", help="remove rendered copies")
    args = ap.parse_args(argv)
    if args.clean:
        clean()
        return 0
    if args.check:
        return check()
    render()
    return check()


if __name__ == "__main__":
    sys.exit(main())
