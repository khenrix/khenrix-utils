#!/usr/bin/env python3
"""Render shared assets into each per-CLI plugin, and validate skills.

Makes every plugin self-contained so it works after being installed/copied by a
marketplace:
  * copies capabilities.toml + house-style.md + statusline/ to the plugin root
  * copies shared/skills/<name>/ into the plugin's skills/
  * copies scripts/lib/reconcile.py into each khenrix-setup skill's scripts/
  * validates every SKILL.md (name + description, length/char rules)

Modes:
  render.py            render + validate
  render.py --check    validate only (non-zero exit on any problem)
  render.py --clean    remove rendered copies (incl. generated templated skills)
"""
from __future__ import annotations

import argparse
import re
import shutil
import string
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLIS = ("claude", "codex", "agy")
BUNDLED = ["capabilities.toml", "house-style.md", "headless-invocation.md"]
BUNDLED_DIRS = ["statusline", "overlays"]
# Shared engine/helper scripts bundled into every skill's scripts/ dir so each
# skill is self-contained after a marketplace copies the plugin.
LIB_SCRIPTS = [ROOT / "scripts" / "lib" / "reconcile.py",
               ROOT / "scripts" / "lib" / "inventory.py"]
NAME_RE = re.compile(r"^[a-z0-9-]{1,64}$")
# Per-CLI skills whose SHARED body is one template + per-CLI [skill_facts.*] in
# capabilities.toml; render.py generates each plugin's SKILL.md from them.
TEMPLATED_SKILLS = ("khenrix-setup", "khenrix-upgrade")
TMPL_ROOT = ROOT / "shared" / "skill-templates"


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


def load_caps() -> dict:
    with open(ROOT / "capabilities.toml", "rb") as f:
        return tomllib.load(f)


def render_templated_skill(skill: str, cli: str, caps: dict, problems: list):
    """Fill shared/skill-templates/<skill>/SKILL.md.tmpl with the per-CLI facts
    from [skill_facts.<skill>.<cli>]. Returns the body, or None (recording a
    problem) if the template or any token is missing."""
    tmpl_path = TMPL_ROOT / skill / "SKILL.md.tmpl"
    if not tmpl_path.exists():
        problems.append(f"{skill}: template missing at {tmpl_path.relative_to(ROOT)}")
        return None
    facts = caps.get("skill_facts", {}).get(skill, {}).get(cli)
    if facts is None:
        problems.append(f"{skill}: no [skill_facts.{skill}.{cli}] in capabilities.toml")
        return None
    tmpl = string.Template(tmpl_path.read_text())
    missing = set(tmpl.get_identifiers()) - set(facts)  # get_identifiers: py3.11+
    if missing:
        problems.append(f"{skill}/{cli}: facts missing tokens {sorted(missing)}")
        return None
    try:
        return tmpl.substitute(facts)
    except (KeyError, ValueError) as e:  # stray/invalid $placeholder in the template
        problems.append(f"{skill}/{cli}: substitution failed: {e}")
        return None


def render():
    caps = load_caps()
    problems: list[str] = []
    shared_skills = sorted((ROOT / "shared" / "skills").glob("*/"))
    for cli in CLIS:
        pdir = plugin_dir(cli)
        pdir.mkdir(parents=True, exist_ok=True)
        # 0. generate the templated per-CLI skill bodies from shared template + facts
        for skill in TEMPLATED_SKILLS:
            body = render_templated_skill(skill, cli, caps, problems)
            if body is not None:
                dst = pdir / "skills" / skill
                dst.mkdir(parents=True, exist_ok=True)
                (dst / "SKILL.md").write_text(body)
        # 1. bundle the source of truth
        for f in BUNDLED:
            shutil.copy2(ROOT / f, pdir / f)
        for d in BUNDLED_DIRS:
            dst = pdir / d
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(ROOT / d, dst,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        # 2. copy shared skills (canonical bodies) into the plugin
        for s in shared_skills:
            dst = pdir / "skills" / s.name
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(s, dst)
        # 3. bundle the shared engine/helper scripts into every skill's scripts/
        skills_root = pdir / "skills"
        if skills_root.exists():
            for skill in (d for d in skills_root.iterdir() if (d / "SKILL.md").exists()):
                (skill / "scripts").mkdir(parents=True, exist_ok=True)
                for lib in LIB_SCRIPTS:
                    shutil.copy2(lib, skill / "scripts" / lib.name)
    if problems:
        print("RENDER FAILED:")
        for p in problems:
            print(f"  ✗ {p}")
        raise SystemExit(1)
    libs = ", ".join(p.name for p in LIB_SCRIPTS)
    print(f"rendered: bundled {BUNDLED} + {BUNDLED_DIRS} + [{libs}] into {len(CLIS)} plugins; "
          f"{len(shared_skills)} shared skill(s); {len(TEMPLATED_SKILLS)} templated skill(s)")


def clean():
    removed = 0
    for cli in CLIS:
        pdir = plugin_dir(cli)
        for f in BUNDLED:
            (pdir / f).unlink(missing_ok=True)
            removed += 1
        for d in BUNDLED_DIRS:
            shutil.rmtree(pdir / d, ignore_errors=True)
        skills_root = pdir / "skills"
        if skills_root.exists():
            for skill in skills_root.iterdir():
                for lib in LIB_SCRIPTS:
                    (skill / "scripts" / lib.name).unlink(missing_ok=True)
        # generated templated skill bodies are regenerable — drop them too
        for skill in TEMPLATED_SKILLS:
            (pdir / "skills" / skill / "SKILL.md").unlink(missing_ok=True)
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
    # deterministic source-of-truth checks — skip if capabilities.toml itself failed to parse
    if not any("capabilities.toml" in p for p in problems):
        sys.path.insert(0, str(ROOT / "scripts" / "lib"))
        import checks  # noqa: E402
        problems.extend(checks.run_all(ROOT))
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
