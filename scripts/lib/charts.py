#!/usr/bin/env python3
"""Skill flowchart lint — every skill has a chart; every gate in it carries evidence.

A flowchart is a prose surface that can lie, and this repo's most persistent defect
class is documentation asserting behaviour the code does not have. So a chart may only
draw a decision gate (a G_* diamond) if its Gate-evidence table names what proves it:
a `path::label` test reference for code-enforced gates (resolved by this lint), an
eval assertion / audit item for agent-enforced ones (labeled honestly as such).
"""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
CHART_DIR = "docs/skill-charts"


GATE_RE = re.compile(r"\b(G_[A-Z0-9_]+)\s*\{")
# Tolerates a backticked gate ID — authors WILL write | `G_X` | and a strict regex
# would report the gate as row-less, a confusing failure for a cosmetic cause.
ROW_RE = re.compile(r"^\|\s*`?\s*(G_[A-Z0-9_]+)\s*`?\s*\|\s*(\S+)\s*\|\s*(.+?)\s*\|\s*$", re.M)
REF_RE = re.compile(r"`([^`:]+?)::([^`]+?)`")
KINDS = {"code", "agent"}


def chart_skills(root: Path) -> list[str]:
    """Every skill that owes a chart: shared skills + templated skills."""
    out = []
    for base in ("shared/skills", "shared/skill-templates"):
        for p in sorted((root / base).glob("*/")):
            if (p / "SKILL.md").is_file() or (p / "SKILL.md.tmpl").is_file():
                out.append(p.name)
    return out


def check_charts(root: Path) -> list[str]:
    """The chart honesty contract. Syntactic only — whether the drawn flow matches the
    SKILL.md is skill-tuneup's audit job; what this proves is that every drawn gate
    names live evidence, so a chart cannot outlive the test that backs it."""
    problems = []
    skills = chart_skills(root)
    if not skills and (root / "capabilities.toml").is_file():
        # Fail LOUD, never open: an empty scan in a real khenrix checkout means the
        # skill dirs moved, not that nothing owes a chart — the same vacuous-green
        # defect forge_packaging and the bats runner already refuse.
        return ["charts: found ZERO skills in a tree that has capabilities.toml — "
                "the scan is looking in the wrong place; refusing to pass vacuously"]
    makefile = root / "Makefile"
    mk_text = makefile.read_text() if makefile.is_file() else ""
    for skill in skills:
        chart = root / CHART_DIR / f"{skill}.md"
        if not chart.is_file():
            problems.append(f"charts: {skill} has no flowchart at {CHART_DIR}/{skill}.md")
            continue
        text = chart.read_text()
        blocks = re.findall(r"```mermaid\n(.*?)```", text, re.S)
        if len(blocks) != 1:
            problems.append(f"charts: {skill}.md must contain exactly one mermaid "
                            f"block (found {len(blocks)})")
            continue
        if not blocks[0].lstrip().startswith("flowchart"):
            problems.append(f"charts: {skill}.md mermaid block is not a flowchart")
        gates = set(GATE_RE.findall(blocks[0]))
        rows = {}
        for m in ROW_RE.findall(text):
            if m[0] in rows:  # a dict comprehension would silently keep one of them
                problems.append(f"charts: {skill}.md has DUPLICATE evidence rows for "
                                f"{m[0]} — the table must decide, not the parser")
            rows[m[0]] = (m[1], m[2])
        for g in sorted(gates - rows.keys()):
            problems.append(f"charts: {skill}.md gate {g} has no row in the "
                            f"Gate evidence table")
        for g in sorted(rows.keys() - gates):
            problems.append(f"charts: {skill}.md evidence row {g} matches no gate "
                            f"in the chart")
        for g, (kind, ev) in sorted(rows.items()):
            if kind not in KINDS:
                problems.append(f"charts: {skill}.md {g} has unknown kind {kind!r} "
                                f"(code|agent)")
                continue
            refs = list(REF_RE.finditer(ev))
            if kind == "code" and not refs:
                problems.append(f"charts: {skill}.md {g} is a code gate but its "
                                f"evidence has no `path::label` reference")
                continue
            for ref in refs:  # resolve EVERY reference, whichever kind carries it
                path, label = ref.group(1).strip(), ref.group(2).strip()
                f = root / path
                if not f.is_file():
                    problems.append(f"charts: {skill}.md {g} evidence file {path} "
                                    f"does not exist")
                    continue
                if label not in f.read_text():
                    problems.append(f"charts: {skill}.md {g} evidence label {label!r} "
                                    f"not found in {path}")
                if kind == "code" and path.endswith(".py") and path not in mk_text:
                    # A test on disk that no make target runs is a suite that rots —
                    # the Makefile says so itself. Proof of PRESENCE is not proof the
                    # gate's evidence is ever executed.
                    problems.append(f"charts: {skill}.md {g} cites {path} but no "
                                    f"Makefile target runs it — a suite that is "
                                    f"never run proves nothing")
    return problems


def self_test() -> int:
    ok = []

    def _tree(td: Path, chart_text: str | None, evidence_target: str = "def converge",
              makefile: str = "eval-test:\n\t$(PY) scripts/engine.py --self-test\n"):
        """Minimal repo: one shared skill `alpha`, one evidence file, a Makefile that
        runs it (rule 5 needs the cited .py to appear in a make recipe), optional chart."""
        (td / "shared" / "skills" / "alpha").mkdir(parents=True)
        (td / "shared" / "skills" / "alpha" / "SKILL.md").write_text("# a\n")
        (td / "scripts").mkdir(exist_ok=True)
        (td / "scripts" / "engine.py").write_text(f"{evidence_target}\n")
        (td / "Makefile").write_text(makefile)
        if chart_text is not None:
            d = td / CHART_DIR
            d.mkdir(parents=True)
            (d / "alpha.md").write_text(chart_text)
        return td

    GOOD = """# alpha — flow
```mermaid
flowchart TD
    accTitle: alpha flow
    START([start]) --> G_READY{engine ready?}
    G_READY -- yes --> WORK[do the work] --> DONE([done])
    G_READY -- no --> HALT([stop])
```
## Gate evidence
| Gate | Kind | Evidence |
|---|---|---|
| G_READY | code | `scripts/engine.py::def converge` |
"""
    with tempfile.TemporaryDirectory() as t:
        ok.append(("clean chart passes", check_charts(_tree(Path(t), GOOD)) == []))
    with tempfile.TemporaryDirectory() as t:
        probs = check_charts(_tree(Path(t), None))
        ok.append(("missing chart is flagged",
                   len(probs) == 1 and "no flowchart" in probs[0]))
    with tempfile.TemporaryDirectory() as t:
        probs = check_charts(_tree(Path(t), GOOD + "\n```mermaid\nflowchart LR\nA-->B\n```\n"))
        ok.append(("two mermaid blocks are flagged",
                   any("exactly one" in p for p in probs)))
    with tempfile.TemporaryDirectory() as t:
        probs = check_charts(_tree(Path(t), GOOD.replace("flowchart TD", "sequenceDiagram")))
        ok.append(("non-flowchart block is flagged",
                   any("not a flowchart" in p for p in probs)))
    with tempfile.TemporaryDirectory() as t:
        # gate drawn but no evidence row
        text = GOOD.replace("| G_READY | code | `scripts/engine.py::def converge` |\n", "")
        probs = check_charts(_tree(Path(t), text))
        ok.append(("gate without evidence row is flagged",
                   any("G_READY" in p and "no row" in p for p in probs)))
    with tempfile.TemporaryDirectory() as t:
        # evidence row for a gate that is not in the chart
        text = GOOD + "| G_GHOST | code | `scripts/engine.py::def converge` |\n"
        probs = check_charts(_tree(Path(t), text))
        ok.append(("row without gate is flagged",
                   any("G_GHOST" in p and "matches no gate" in p for p in probs)))
    with tempfile.TemporaryDirectory() as t:
        text = GOOD.replace("def converge", "def nonexistent_label")
        probs = check_charts(_tree(Path(t), text))
        ok.append(("unresolvable code evidence is flagged",
                   any("not found in" in p for p in probs)))
    with tempfile.TemporaryDirectory() as t:
        text = GOOD.replace("| G_READY | code | `scripts/engine.py::def converge` |",
                            "| G_READY | agent | SKILL.md checkpoint rule; eval assertion 'stops for approval' |")
        ok.append(("agent row with prose evidence passes",
                   check_charts(_tree(Path(t), text)) == []))
    with tempfile.TemporaryDirectory() as t:
        text = GOOD.replace("| G_READY | code |", "| G_READY | vibes |")
        probs = check_charts(_tree(Path(t), text))
        ok.append(("unknown kind is flagged", any("kind" in p for p in probs)))
    with tempfile.TemporaryDirectory() as t:
        # agent row WITH a path::label reference still gets it resolved
        text = GOOD.replace("| G_READY | code | `scripts/engine.py::def converge` |",
                            "| G_READY | agent | covered by `scripts/engine.py::def missing_thing` |")
        probs = check_charts(_tree(Path(t), text))
        ok.append(("agent row's dangling reference is still resolved",
                   any("not found in" in p for p in probs)))
    with tempfile.TemporaryDirectory() as t:
        # duplicate rows for one gate must be an error, not a silent overwrite
        text = GOOD + "| G_READY | agent | second opinion |\n"
        probs = check_charts(_tree(Path(t), text))
        ok.append(("duplicate evidence rows are flagged",
                   any("DUPLICATE" in p for p in probs)))
    with tempfile.TemporaryDirectory() as t:
        # a backticked gate ID in the table still matches its diamond
        text = GOOD.replace("| G_READY | code |", "| `G_READY` | code |")
        ok.append(("backticked gate ID is tolerated",
                   check_charts(_tree(Path(t), text)) == []))
    with tempfile.TemporaryDirectory() as t:
        # EVERY reference in a row is resolved, not just the first
        text = GOOD.replace(
            "| G_READY | code | `scripts/engine.py::def converge` |",
            "| G_READY | code | `scripts/engine.py::def converge` and `scripts/engine.py::def gone` |")
        probs = check_charts(_tree(Path(t), text))
        ok.append(("second reference in a row is resolved too",
                   any("'def gone'" in p and "not found" in p for p in probs)))
    with tempfile.TemporaryDirectory() as t:
        # a cited .py that no make target runs proves nothing
        probs = check_charts(_tree(Path(t), GOOD, makefile="eval-test:\n\ttrue\n"))
        ok.append(("code evidence outside every make recipe is flagged",
                   any("never run" in p for p in probs)))
    with tempfile.TemporaryDirectory() as t:
        # zero skills + a capabilities.toml = the scan is wrong, not the repo clean
        td = Path(t)
        (td / "capabilities.toml").write_text("[models]\n")
        probs = check_charts(td)
        ok.append(("empty scan with capabilities.toml present fails loud",
                   any("vacuously" in p for p in probs)))

    passed = sum(1 for _, v in ok if v)
    for label, v in ok:
        print(f"  {'PASS' if v else 'FAIL'}  {label}")
    print(f"\ncharts self-test: {passed}/{len(ok)} checks passed")
    return 0 if passed == len(ok) else 1


if __name__ == "__main__":
    sys.exit(self_test() if "--self-test" in sys.argv else 0)