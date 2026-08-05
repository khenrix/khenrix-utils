#!/usr/bin/env python3
"""Cross-CLI structural checks for the rendered plugins."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
CLIS = ("claude", "codex", "agy")


# Matched with the `${` prefix on purpose: the bare string "PLUGIN_ROOT" is a SUBSTRING
# of "CLAUDE_PLUGIN_ROOT", so a skill probing only the Claude root would falsely satisfy
# the Codex token and the check would never fire.
PLUGIN_ROOT_TOKENS = ("${CLAUDE_PLUGIN_ROOT", "${PLUGIN_ROOT", ".gemini/config/plugins")
CHAIN_OPS = ("&&", "||", ";")


def _shared_skills(root: Path):
    return sorted((root / "shared" / "skills").glob("*/"))


def _rendered(root: Path, cli: str, skill: str) -> Path:
    return root / "marketplaces" / cli / "plugins" / "khenrix-utils" / "skills" / skill


def script_tree_parity(root: Path) -> list[str]:
    """Every file under a shared skill's scripts/ must exist in ALL THREE rendered
    plugins. This is the defect the eval harness structurally cannot see: it pastes the
    SKILL.md body into a prompt and never executes a bundled script, so a script that
    resolves on claude but was never copied into the agy plugin scores a clean agy
    delta and breaks the moment a real agy session runs the skill.
    """
    problems = []
    for skill_dir in _shared_skills(root):
        src = skill_dir / "scripts"
        if not src.is_dir():
            continue
        want = sorted(p.relative_to(src) for p in src.rglob("*")
                      if p.is_file() and "__pycache__" not in p.parts
                      and p.suffix != ".pyc")
        for cli in CLIS:
            dst = _rendered(root, cli, skill_dir.name) / "scripts"
            for rel in want:
                if not (dst / rel).is_file():
                    problems.append(
                        f"portability: {skill_dir.name}/scripts/{rel} is missing from "
                        f"the {cli} plugin — re-run `python3 scripts/render.py`")
    return problems


def plugin_root_probes(root: Path) -> list[str]:
    """A SKILL.md that resolves its own bundled script does it with a probe loop over
    each CLI's plugin root (see shared/skills/llm-council/SKILL.md). Assert every such
    loop names ALL THREE roots — omitting the agy root is exactly the silent
    single-CLI breakage this module exists to catch.

    Deliberately does NOT scan prose for bare `scripts/*.py` strings. Skills
    legitimately cite $KU/scripts/render.py, llm-council's fanout.py and codex's
    ~/.codex/skills/.system tooling; that rule was implemented during design review and
    produced 9 false positives out of 9 hits.
    """
    problems = []
    for skill_dir in _shared_skills(root):
        md = skill_dir / "SKILL.md"
        if not md.is_file():
            continue
        text = md.read_text()
        if "${CLAUDE_PLUGIN_ROOT" not in text:
            continue  # not a probe-loop skill
        missing = [t for t in PLUGIN_ROOT_TOKENS if t not in text]
        if missing:
            problems.append(
                f"portability: {skill_dir.name}/SKILL.md probes for a plugin root but "
                f"never names {', '.join(missing)} — it cannot resolve on that CLI")
    return problems


def _allowed_tools(text: str) -> list[str]:
    """Pull `allowed-tools` out of YAML frontmatter without a YAML parser (stdlib-only).
    Handles the inline form (`allowed-tools: A, B`) and the block-list form."""
    if not text.startswith("---"):
        return []
    end = text.find("\n---", 3)
    if end == -1:
        return []
    entries, in_block = [], False
    for line in text[3:end].splitlines():
        if in_block:
            if line.startswith("  - ") or line.startswith("- "):
                entries.append(line.split("- ", 1)[1].strip())
                continue
            in_block = False
        if line.startswith("allowed-tools:"):
            rest = line.split(":", 1)[1].strip()
            if rest:
                # Split on commas OUTSIDE parentheses: `Bash(git add:*), Read`.
                depth, cur = 0, ""
                for ch in rest:
                    if ch == "(":
                        depth += 1
                    elif ch == ")":
                        depth -= 1
                    if ch == "," and depth == 0:
                        entries.append(cur.strip())
                        cur = ""
                    else:
                        cur += ch
                if cur.strip():
                    entries.append(cur.strip())
            else:
                in_block = True
    return [e for e in entries if e]


def allowed_tools_single_command(root: Path) -> list[str]:
    """House rule (house-style.md): each Bash entry in `allowed-tools` must be ONE
    command. Chaining with && / || / ; defeats allow-list prefix matching and forces a
    permission prompt — the exact failure the rule exists to prevent, and until now
    documented but unenforced.
    """
    problems = []
    for skill_dir in _shared_skills(root):
        md = skill_dir / "SKILL.md"
        if not md.is_file():
            continue
        for entry in _allowed_tools(md.read_text()):
            if entry.startswith("Bash(") and any(op in entry for op in CHAIN_OPS):
                problems.append(
                    f"portability: {skill_dir.name}/SKILL.md allowed-tools entry "
                    f"{entry!r} chains commands — split it into separate entries")
    return problems


def run(root: Path) -> list[str]:
    """Every hermetic check, in one call — what `checks.run_all()` hooks.

    NOT DEFINED BY THE PLAN. Its Interfaces block promises `run(root) -> list[str]` and its
    code blocks give only the three checks, so the aggregator is written here. It concatenates
    rather than short-circuiting: three independent structural facts, and stopping at the
    first would hide the other two behind whichever happened to be alphabetically unlucky.
    """
    problems = []
    for fn in (script_tree_parity, plugin_root_probes, allowed_tools_single_command):
        problems.extend(fn(root))
    return problems


def self_test() -> int:
    ok = []

    def _skel(root: Path, *, scripts=("tool.py",), clis=CLIS, body="# x\n"):
        """Minimal render tree: one shared skill with scripts/, mirrored into `clis`."""
        src = root / "shared" / "skills" / "alpha"
        (src / "scripts").mkdir(parents=True)
        (src / "SKILL.md").write_text(body)
        for s in scripts:
            (src / "scripts" / s).write_text("print(1)")
        for cli in clis:
            dst = root / "marketplaces" / cli / "plugins" / "khenrix-utils" / "skills" / "alpha"
            (dst / "scripts").mkdir(parents=True)
            (dst / "SKILL.md").write_text(body)
            for s in scripts:
                (dst / "scripts" / s).write_text("print(1)")
        return root

    with tempfile.TemporaryDirectory() as td:
        r = _skel(Path(td) / "full")
        ok.append(("parity clean when all three CLIs have the script",
                   script_tree_parity(r) == []))
    with tempfile.TemporaryDirectory() as td:
        r = _skel(Path(td) / "partial", clis=("claude", "codex"))
        probs = script_tree_parity(r)
        ok.append(("parity flags the missing agy copy",
                   len(probs) == 1 and "agy" in probs[0] and "tool.py" in probs[0]))

    PROBE_ALL = ("${CLAUDE_PLUGIN_ROOT:-}/x ${PLUGIN_ROOT:-}/x "
                 "$HOME/.gemini/config/plugins/khenrix-utils/x\n")
    with tempfile.TemporaryDirectory() as td:
        r = _skel(Path(td) / "probe-ok", body=PROBE_ALL)
        ok.append(("probe list naming all three roots is clean",
                   plugin_root_probes(r) == []))
    with tempfile.TemporaryDirectory() as td:
        r = _skel(Path(td) / "probe-bad",
                  body="${CLAUDE_PLUGIN_ROOT:-}/x ${PLUGIN_ROOT:-}/x\n")
        probs = plugin_root_probes(r)
        ok.append(("probe list omitting the agy root is flagged",
                   len(probs) == 1 and ".gemini" in probs[0]))
    with tempfile.TemporaryDirectory() as td:
        r = _skel(Path(td) / "no-probe", body="no plugin root here\n")
        ok.append(("a SKILL.md with no probe loop is not flagged",
                   plugin_root_probes(r) == []))

    FM_OK = "---\nname: alpha\nallowed-tools: Bash(git status:*), Read\n---\nbody\n"
    FM_BAD = "---\nname: alpha\nallowed-tools: Bash(cd x && ls:*), Read\n---\nbody\n"
    with tempfile.TemporaryDirectory() as td:
        r = _skel(Path(td) / "fm-ok", body=FM_OK)
        ok.append(("single-command Bash entries are clean",
                   allowed_tools_single_command(r) == []))
    with tempfile.TemporaryDirectory() as td:
        r = _skel(Path(td) / "fm-bad", body=FM_BAD)
        probs = allowed_tools_single_command(r)
        ok.append(("a chained Bash entry is flagged",
                   len(probs) == 1 and "chains commands" in probs[0]))

    with tempfile.TemporaryDirectory() as td:
        r = _skel(Path(td) / "run-clean")
        ok.append(("run() aggregates the three checks", run(r) == []))
    with tempfile.TemporaryDirectory() as td:
        r = _skel(Path(td) / "run-two", clis=("claude",), body=FM_BAD)
        # A parity problem AND a chained Bash entry: `run` reports BOTH, because stopping at
        # the first would hide a real defect behind an unrelated one.
        probs = run(r)
        ok.append(("run() does not stop at the first problem",
                   len(probs) >= 3 and any("chains commands" in x for x in probs)
                   and any("missing from" in x for x in probs)))

    passed = sum(1 for _, v in ok if v)
    for label, v in ok:
        print(f"  {'PASS' if v else 'FAIL'}  {label}")
    print(f"\nportability self-test: {passed}/{len(ok)} checks passed")
    return 0 if passed == len(ok) else 1


if __name__ == "__main__":
    sys.exit(self_test() if "--self-test" in sys.argv else 0)
