#!/usr/bin/env python3
"""Trigger/near-miss description eval — does a skill's DESCRIPTION fire correctly?

The behavior harness (eval_harness.py) injects a skill and grades the output; it
assumes the skill already triggered. This complements it on the other axis: given
ONLY the skill's name+description, would the agent pick it for a prompt? Reads
evals/<skill>/triggers.json {should_trigger:[…], near_miss:[…]}, asks the judge
per prompt, and scores correct fires + correct abstains.

Stdlib only; reuses the llm-council fan-out engine for the judge call.
  eval_trigger.py --skill <name> [--judge claude] [--mode normal]
  eval_trigger.py --self-test     # hermetic logic, no tokens
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FANOUT_DIR = ROOT / "shared" / "skills" / "llm-council" / "scripts"
sys.path.insert(0, str(FANOUT_DIR))
import fanout  # noqa: E402

EVALS_ROOT = ROOT / "evals"
DEFAULT_JUDGE = "claude"

JUDGE_TMPL = """A coding agent has exactly one skill available:

NAME: {name}
DESCRIPTION: {description}

The user sends this message:
<<<BEGIN
{prompt}
END>>>

Judging ONLY from the name + description above, is this skill the right tool to
activate for that message? Output ONLY JSON, no prose:
{{"activate": true or false, "why": "<one short sentence>"}}"""


def parse_frontmatter_field(skill_md: str, field: str) -> str:
    """Pull a top-level frontmatter scalar (handles `key: >- ` folded blocks)."""
    if not skill_md.startswith("---"):
        return ""
    end = skill_md.find("\n---", 3)
    fm = skill_md[3:end] if end != -1 else skill_md
    lines = fm.splitlines()
    for i, line in enumerate(lines):
        m = re.match(rf"^{re.escape(field)}:\s*(.*)$", line)
        if not m:
            continue
        val = m.group(1).strip()
        if val in (">", "|", ">-", "|-", ">+", "|+"):  # folded/literal block scalar
            block = []
            for cont in lines[i + 1:]:
                if cont and not cont.startswith((" ", "\t")):
                    break
                block.append(cont.strip())
            return " ".join(b for b in block if b).strip()
        return val.strip().strip('"').strip("'")
    return ""


def load_skill_meta(skill: str, provider: str = "claude") -> tuple[str, str]:
    p = (ROOT / "marketplaces" / provider / "plugins" / "khenrix-utils"
         / "skills" / skill / "SKILL.md")
    if not p.exists():
        sys.exit(f"rendered skill missing: {p.relative_to(ROOT)} (run render.py)")
    text = p.read_text()
    return (parse_frontmatter_field(text, "name") or skill,
            parse_frontmatter_field(text, "description"))


def parse_verdict(raw: str) -> bool:
    """True if the judge says the skill should activate."""
    s = (raw or "").strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", s, re.DOTALL)
    if fence:
        s = fence.group(1)
    cand = s[s.find("{"): s.rfind("}") + 1] if "{" in s and "}" in s else s
    try:
        return bool(json.loads(cand).get("activate"))
    except (json.JSONDecodeError, AttributeError):
        return False


def score(cases: list) -> dict:
    """cases: list of {kind, expected, got}. Returns precision-ish accuracy split."""
    fires = [c for c in cases if c["kind"] == "should_trigger"]
    misses = [c for c in cases if c["kind"] == "near_miss"]
    tp = sum(1 for c in fires if c["got"])
    tn = sum(1 for c in misses if not c["got"])
    total = len(cases)
    return {
        "should_trigger": {"correct": tp, "total": len(fires)},
        "near_miss": {"correct": tn, "total": len(misses)},
        "accuracy": round((tp + tn) / total, 4) if total else 0.0,
    }


def run(args) -> int:
    path = EVALS_ROOT / args.skill / "triggers.json"
    if not path.exists():
        sys.exit(f"no triggers at {path.relative_to(ROOT)} — create it first "
                 '({"should_trigger": [...], "near_miss": [...]})')
    spec = json.loads(path.read_text())
    name, desc = load_skill_meta(args.skill, args.judge)
    cfg = fanout.resolve_mode_config(argparse.Namespace(
        mode=args.mode, timeout=args.timeout, model_claude=None, model_codex=None, model_agy=None))
    timeout = fanout.effective_timeout(argparse.Namespace(mode=args.mode, timeout=args.timeout))
    workdir = EVALS_ROOT / args.skill / "workspace" / "triggers"
    workdir.mkdir(parents=True, exist_ok=True)

    cases = []
    for kind in ("should_trigger", "near_miss"):
        for i, prompt in enumerate(spec.get(kind, [])):
            jp = JUDGE_TMPL.format(name=name, description=desc, prompt=prompt)
            spec_ = fanout.build_real_spec(args.judge, jp, timeout, cfg, workdir)
            m = fanout.run_council([spec_], retries=1, timeout=timeout, backoff=2.0,
                                   workdir=workdir / f"{kind}-{i}", prompt=jp)
            rec = m["providers"][0]
            raw = Path(rec["result_file"]).read_text() if rec.get("valid") else ""
            got = parse_verdict(raw)
            cases.append({"kind": kind, "prompt": prompt, "expected": kind == "should_trigger",
                          "got": got})
            print(f"  {'✓' if got == (kind == 'should_trigger') else '✗'} [{kind}] {prompt[:60]}")
    result = score(cases)
    (workdir / "triggers-result.json").write_text(json.dumps({"skill": args.skill,
        "result": result, "cases": cases}, indent=2))
    print(f"\n  accuracy: {result['accuracy']}  "
          f"(should_trigger {result['should_trigger']['correct']}/{result['should_trigger']['total']}, "
          f"near_miss {result['near_miss']['correct']}/{result['near_miss']['total']})")
    return 0 if result["accuracy"] >= 0.8 else 1


def _self_test() -> int:
    ok = []
    fm = '---\nname: x\ndescription: >-\n  line one\n  line two\nallowed-tools: Bash\n---\nbody'
    ok.append(("folded description parsed", parse_frontmatter_field(fm, "description") == "line one line two"))
    ok.append(("plain name parsed", parse_frontmatter_field(fm, "name") == "x"))
    ok.append(("verdict true", parse_verdict('{"activate": true, "why": "y"}') is True))
    ok.append(("verdict fenced false", parse_verdict('```json\n{"activate": false}\n```') is False))
    ok.append(("verdict garbage → false", parse_verdict("nope") is False))
    r = score([{"kind": "should_trigger", "expected": True, "got": True},
               {"kind": "should_trigger", "expected": True, "got": False},
               {"kind": "near_miss", "expected": False, "got": False}])
    ok.append(("score accuracy 2/3", r["accuracy"] == round(2 / 3, 4)))
    ok.append(("score splits", r["should_trigger"]["correct"] == 1 and r["near_miss"]["correct"] == 1))
    for label, passed in ok:
        print(f"  {'PASS' if passed else 'FAIL'}  {label}")
    return 0 if all(p for _, p in ok) else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Skill description trigger/near-miss eval")
    ap.add_argument("--skill")
    ap.add_argument("--judge", default=DEFAULT_JUDGE)
    ap.add_argument("--mode", choices=list(fanout.MODES), default="normal")
    ap.add_argument("--timeout", type=int, default=None)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        return _self_test()
    if not args.skill:
        sys.exit("--skill is required (or use --self-test)")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
