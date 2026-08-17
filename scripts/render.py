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

# checks.py owns the CLI list and this module borrows it, not the reverse: render already
# imports checks (check(), below), so defining it here and importing it there would be a
# cycle. Restating it would be worse — the restatements are what let a fourth CLI be
# rendered while a check quietly skipped it.
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
import checks  # noqa: E402

CLIS = checks.CLIS
BUNDLED = ["capabilities.toml", "house-style.md", "headless-invocation.md"]
BUNDLED_DIRS = ["statusline", "overlays", "hooks"]
# Shared engine/helper scripts bundled into every skill's scripts/ dir so each
# skill is self-contained after a marketplace copies the plugin.
LIB_SCRIPTS = [ROOT / "scripts" / "lib" / "reconcile.py",
               ROOT / "scripts" / "lib" / "inventory.py"]
# Shared stdlib engines under shared/lib/<name>/, bundled once per plugin at lib/<name>/
# so skills can `PYTHONPATH=<plugin>/lib python3 -m <name>` after a marketplace copy.
# Runtime code only — tests are excluded to keep the plugin lean.
SHARED_LIBS = ["wikisync", "council", "forge"]
# Single MODULES (not packages) that land beside those engines at lib/<file>.py, because
# an engine imports them at runtime. forge/screen.py loads checks.py by path to get the
# secret patterns rather than forking them; once the marketplace copies a plugin out of
# this repo, lib/checks.py is the only candidate its resolver can still reach.
SHARED_LIB_FILES = [ROOT / "scripts" / "lib" / "checks.py"]
NAME_RE = re.compile(r"^[a-z0-9-]{1,64}$")
# Per-CLI skills whose SHARED body is one template + per-CLI [skill_facts.*] in
# capabilities.toml; render.py generates each plugin's SKILL.md from them.
TEMPLATED_SKILLS = ("khenrix-setup", "khenrix-upgrade", "khenrix-audit")
TMPL_ROOT = ROOT / "shared" / "skill-templates"


def plugin_dir(cli: str) -> Path:
    return ROOT / "marketplaces" / cli / "plugins" / "khenrix-utils"


def parse_frontmatter(text: str) -> dict:
    """Minimal YAML front-matter reader — including FOLDED/LITERAL block scalars.

    Block scalars are not a nicety here: every skill writes its `description` as
    `description: >-` plus indented continuation lines, because the descriptions are long
    prose. The original reader kept only the text after the colon and skipped every
    indented line, so it stored the literal ">-" and `validate_skill` then measured
    len(">-") == 2. The documented "description <=1024 chars, enforced by render.py
    --check" was therefore inert for every skill that used a block scalar — 7 of 8 — while
    appearing to pass. A check that cannot fail is worse than no check: it is a false
    assurance someone will rely on.

    Implements the block-scalar subset YAML actually specifies, because a "close enough"
    fold silently changes the number this gate measures:
      `|` literal - lines joined with newlines.
      `>` folded  - lines joined with spaces, but a BLANK line is a paragraph break and
                    becomes a newline. Collapsing it to a space (the first version of this
                    function) shortens the value, which is the wrong direction for a limit.
      chomp `-` strip the trailing newline; `+` keep all; absent (clip) keep exactly one.
    Anything else after `|`/`>` (an explicit indentation indicator) is unsupported and
    raises rather than being silently mis-parsed.
    """
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    fm = {}
    lines = text[3:end].splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if ":" not in line or line.startswith((" ", "\t", "-", "#")):
            continue
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip()
        if v and v[0] in "|>":
            literal, chomp = v[0] == "|", v[1:].strip()
            if chomp not in ("", "-", "+"):
                raise ValueError(f"unsupported block scalar '{k}: {v}' — only |, >, "
                                 f"and the -/+ chomp suffixes are handled")
            raw = []
            while i < len(lines) and (not lines[i].strip() or lines[i].startswith((" ", "\t"))):
                raw.append(lines[i].strip())
                i += 1
            while raw and not raw[-1]:
                raw.pop()
            if literal:
                v = "\n".join(raw)
            else:
                # n blank lines fold to n newlines (NOT n+1): the blank is the separator
                # itself, so it must not also trigger a newline for the line following it.
                out, blanks, started = "", 0, False
                for ln in raw:
                    if not ln:
                        blanks += 1
                        continue
                    if not started:
                        out, started = ln, True
                    else:
                        out += ("\n" * blanks) if blanks else " "
                        out += ln
                    blanks = 0
                v = out
            v = v + ("" if chomp == "-" else "\n")
        fm[k] = v.strip('"').strip("'")
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
                # templated skills may ship engine/reference dirs next to the template
                for sub in ("scripts", "references"):
                    src_dir = TMPL_ROOT / skill / sub
                    if src_dir.is_dir():
                        sub_dst = dst / sub
                        if sub_dst.exists():
                            shutil.rmtree(sub_dst)
                        shutil.copytree(src_dir, sub_dst,
                                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        # 1. bundle the source of truth
        for f in BUNDLED:
            shutil.copy2(ROOT / f, pdir / f)
        for d in BUNDLED_DIRS:
            dst = pdir / d
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(ROOT / d, dst,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        # 2. copy shared skills (canonical bodies) into the plugin.
        # The ignore list is defence in depth: it keeps bytecode out of the copy in the first
        # place, independently of the sweep at the end of this loop — which is the mechanism
        # `tests/test_render_packaging.py` actually pins, and which would clean up after this
        # line even if it were dropped. This was the ONE copytree here
        # without it, so running any bundled script before a render (a `--self-test`, an
        # eval) left a `scripts/__pycache__/` that got copied into all three plugins and
        # then shipped into every live CLI install by refresh.py. The bytecode is gitignored
        # at both ends, so git never showed it and the render looked clean. It also made the
        # rendered tree depend on which interpreters had happened to run — this machine
        # carried both cpython-313 and cpython-314 copies. Same patterns as the other four
        # calls, and the same exclusion `checks.py` already applies to the receipt closure.
        for s in shared_skills:
            dst = pdir / "skills" / s.name
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(s, dst,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        # 3. bundle the shared engine/helper scripts into every skill's scripts/
        skills_root = pdir / "skills"
        if skills_root.exists():
            for skill in (d for d in skills_root.iterdir() if (d / "SKILL.md").exists()):
                (skill / "scripts").mkdir(parents=True, exist_ok=True)
                for lib in LIB_SCRIPTS:
                    shutil.copy2(lib, skill / "scripts" / lib.name)
        # 4. bundle shared stdlib engines (runtime only) at the plugin's lib/<name>/
        (pdir / "lib").mkdir(parents=True, exist_ok=True)
        for f in SHARED_LIB_FILES:
            shutil.copy2(f, pdir / "lib" / f.name)
        for name in SHARED_LIBS:
            src = ROOT / "shared" / "lib" / name
            if src.is_dir():
                dst = pdir / "lib" / name
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(src, dst,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests"))
        # Belt to the ignore-patterns' braces. Those only filter what a copy BRINGS IN;
        # bytecode also APPEARS IN PLACE whenever anything imports a module from the
        # rendered tree, and the paths built with copy2 into an existing dir (lib/) are
        # never rmtree'd, so that residue survives every later render.
        #
        # WHAT THIS GUARANTEES, PRECISELY: the render -> sync window. `refresh.py` renders
        # and then copies with `copytree(..., dirs_exist_ok=True)` and no ignore list, so
        # whatever is here at render time is what reaches the live CLI installs. refresh.py
        # sweeps `__pycache__` at the destination too — that is a SECOND, independent guard
        # covering the residue a merge cannot remove, not this one.
        # WHAT IT DOES NOT: hold afterwards. Any later import re-dirties this tree within
        # seconds, and NOTHING detects that — `render.py --check` runs validate_skill +
        # a tomllib parse + checks.run_all and compares nothing, while precommit's real
        # drift check (`git diff --quiet -- marketplaces/`, Makefile) is structurally blind
        # here because .gitignore excludes `__pycache__/` and `*.pyc` at both ends. Measured
        # 2026-08-16: 106 .pyc reappeared under marketplaces/ within the hour after a clean
        # render. Treat this as hygiene at the shipping boundary, not an invariant.
        #
        # `list()` is load-bearing: rglob is lazy, so deleting a directory the walker has
        # not finished descending is a mutation-during-iteration. 3.13's glob wraps scandir
        # in `except OSError` and tolerates it; this repo supports 3.11+, whose older
        # selector catches only PermissionError. Materialize before deleting.
        for cache in list(pdir.rglob("__pycache__")):
            shutil.rmtree(cache, ignore_errors=True)
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
        shutil.rmtree(pdir / "lib", ignore_errors=True)   # bundled shared engines
        # generated templated skill bodies are regenerable — drop them too
        for skill in TEMPLATED_SKILLS:
            (pdir / "skills" / skill / "SKILL.md").unlink(missing_ok=True)
            for sub in ("scripts", "references"):
                shutil.rmtree(pdir / "skills" / skill / sub, ignore_errors=True)
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
