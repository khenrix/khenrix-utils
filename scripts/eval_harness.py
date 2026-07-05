#!/usr/bin/env python3
"""Portable skill-eval harness — provider-agnostic with-skill vs baseline + LLM-judge.

The cross-provider counterpart to Claude's skill-creator eval loop: for a skill it
runs each executor (claude/codex/agy) headlessly twice per eval — once with the
skill's rendered body injected (with_skill) and once on the bare prompt (baseline) —
then has an LLM judge grade each output against the eval's assertions and pick a
winner in a BLIND A/B (it doesn't know which output is which). It emits the same
artifact schema skill-creator uses (grading.json / benchmark.json / comparison.json),
so the two interoperate.

It reuses the llm-council fan-out engine (fanout.py) for the hard part — the exact
per-provider headless invocation, retry/validation, and parallelism. This harness
adds the two conditions, the judge, the blind comparison, and the aggregation.

Stdlib only — runs on any Python 3.11+, no install step.

Model:
  - INSTRUCTION/KNOWLEDGE skills (khenrix-setup, khenrix-upgrade, …): the executor
    reads the injected SKILL.md and should behave per its instructions — exactly what
    with_skill-vs-baseline measures.
  - ORCHESTRATOR skills (llm-council): injecting its body makes the executor try to
    fan out a nested council, which the LLM_COUNCIL_DEPTH guard blocks. Its mode/model
    wiring is verified deterministically by `fanout.py --self-test` / `--smoke`, not here.

Baseline semantics (important): `without_skill` is the executor's AMBIENT environment on
the bare prompt — it is only truly skill-free if the skill is NOT already installed on
that CLI. If the skill is installed (e.g. via a prior `make khenrix-refresh`), it can
auto-trigger and the baseline becomes the *installed/old* version — so the comparison is
then effectively new-body-vs-old-version, not with-vs-without. Cleanest signal: run the
harness while iterating on a skill BEFORE installing/refreshing it. Either way the blind
A/B and delta stay meaningful; just read them with this in mind.

Usage:
  eval_harness.py --skill khenrix-setup [--providers claude,codex,agy] [--mode deep]
  eval_harness.py --skill khenrix-setup --grade-only --iteration 2
  eval_harness.py --self-test          # hermetic unit tests of the harness logic (no tokens)
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FANOUT_DIR = ROOT / "shared" / "skills" / "llm-council" / "scripts"
sys.path.insert(0, str(FANOUT_DIR))
import fanout  # noqa: E402  (maintainer dev tool: reach into the council engine)

EVALS_ROOT = ROOT / "evals"
DEFAULT_JUDGE = "claude"


# --------------------------------------------------------------------------- #
# Pure logic (unit-tested by --self-test; no subprocess / token cost).
# --------------------------------------------------------------------------- #
def strip_frontmatter(skill_md: str) -> str:
    """Return the SKILL.md body without its YAML frontmatter block."""
    if skill_md.startswith("---"):
        end = skill_md.find("\n---", 3)
        if end != -1:
            return skill_md[skill_md.find("\n", end + 1) + 1:].lstrip("\n")
    return skill_md


def build_condition_prompt(skill_body: str, eval_prompt: str, condition: str) -> str:
    """with_skill prepends the skill body as an available, to-follow skill;
    baseline is the bare prompt (what the model does with no skill)."""
    if condition == "with_skill":
        return ("You have the following skill available; follow it when relevant.\n\n"
                "<SKILL>\n" + skill_body.strip() + "\n</SKILL>\n\n"
                "---\n\nUser request:\n" + eval_prompt)
    return eval_prompt


def extract_json(text: str):
    """Best-effort: parse a JSON object from a model's answer (tolerates a fenced
    block or surrounding prose). Returns the dict, or None."""
    s = (text or "").strip()
    if not s:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", s, re.DOTALL)
    if fence:
        s = fence.group(1)
    for candidate in (s, s[s.find("{"): s.rfind("}") + 1] if "{" in s and "}" in s else ""):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None


def parse_grading(raw: str, assertions: list, eval_name: str, condition: str) -> dict:
    """Turn the judge's JSON into the grading.json schema, aligned to the canonical
    assertions by index (so a missing/extra/garbled expectation can't desync counts)."""
    obj = extract_json(raw) or {}
    got = obj.get("expectations") or []
    exps = []
    for i, assertion in enumerate(assertions):
        g = got[i] if i < len(got) and isinstance(got[i], dict) else {}
        exps.append({
            "text": assertion,                          # canonical, not the judge's echo
            "passed": bool(g.get("passed", False)),
            "evidence": str(g.get("evidence", "") or ("no verdict returned" if not g else "")),
        })
    passed = sum(1 for e in exps if e["passed"])
    return {"eval_name": eval_name, "condition": condition,
            "passed": passed, "total": len(assertions), "expectations": exps}


def blind_pair(with_text: str, without_text: str, idx: int):
    """Assign the two outputs to A/B deterministically (no RNG — alternate by eval
    index so neither condition sits in a fixed slot across the set). Returns
    (a_text, b_text, key) where key maps each slot back to its condition."""
    if idx % 2 == 0:
        return with_text, without_text, {"A": "with_skill", "B": "without_skill"}
    return without_text, with_text, {"A": "without_skill", "B": "with_skill"}


def parse_comparison(raw: str, key: dict) -> dict:
    """Judge's blind verdict → comparison.json, de-anonymized via the key."""
    obj = extract_json(raw) or {}
    winner_slot = str(obj.get("winner", "")).strip().upper()[:1]
    winner_condition = key.get(winner_slot)  # None on 'tie'/garbage
    return {
        "winner_slot": winner_slot or "?",
        "winner_condition": winner_condition or "tie",
        "reasoning": str(obj.get("reasoning", "")),
        "A": {**({"condition": key.get("A")}), **(obj.get("A") or {})},
        "B": {**({"condition": key.get("B")}), **(obj.get("B") or {})},
        "_key": key,
    }


def _stats(values: list) -> dict:
    nums = [v for v in values if isinstance(v, (int, float))]
    if not nums:
        return {}
    return {"mean": round(statistics.mean(nums), 4),
            "stddev": round(statistics.pstdev(nums), 4) if len(nums) > 1 else 0.0,
            "min": min(nums), "max": max(nums)}


def aggregate(runs: list) -> dict:
    """runs → run_summary {with_skill, without_skill, delta} over pass_rate/time/tokens,
    matching skill-creator's benchmark.json schema (skips metrics with no data)."""
    summary = {}
    for cond in ("with_skill", "without_skill"):
        rs = [r["result"] for r in runs if r["configuration"] == cond]
        block = {}
        for metric in ("pass_rate", "time_seconds", "tokens"):
            st = _stats([r.get(metric) for r in rs])
            if st:
                block[metric] = st
        summary[cond] = block
    delta = {}
    for metric in ("pass_rate", "time_seconds", "tokens"):
        w = summary["with_skill"].get(metric, {}).get("mean")
        b = summary["without_skill"].get(metric, {}).get("mean")
        if w is not None and b is not None:
            delta[metric] = round(w - b, 4)
    summary["delta"] = delta
    return summary


# --------------------------------------------------------------------------- #
# Execution layer (uses fanout for the real headless runs).
# --------------------------------------------------------------------------- #
def run_text(provider: str, prompt: str, cfg: dict, workdir: Path, *,
             timeout: int, retries: int, readonly: bool):
    """Run one provider headlessly via the fan-out engine; return (text, record).
    `readonly` swaps the provider's bypass flag for a read-and-plan-only posture
    (`make_readonly`) so a skill that mutates config (khenrix-setup/upgrade) can't
    touch the real machine during an eval — while keeping the real HOME so auth still
    resolves (sandboxing HOME instead would hide credentials and every run would fail)."""
    spec = fanout.build_real_spec(provider, prompt, timeout, cfg, workdir)
    if readonly:
        fanout.make_readonly(spec)
    m = fanout.run_council([spec], retries=retries, timeout=timeout, backoff=2.0,
                           workdir=workdir, prompt=prompt)
    rec = m["providers"][0]
    text = Path(rec["result_file"]).read_text() if rec.get("valid") else ""
    return text, rec


GRADE_TMPL = """You are grading whether a candidate answer satisfies a set of assertions.

TASK THE ANSWER WAS RESPONDING TO:
{prompt}

ASSERTIONS (each is an independent pass/fail criterion):
{assertions}

CANDIDATE ANSWER:
<<<BEGIN
{answer}
END>>>

For EACH assertion in order, decide passed (true/false) using ONLY the candidate
answer, with one short sentence of specific evidence (quote or cite). Distinguish
genuine satisfaction from a surface mention. Output ONLY a JSON object, no prose:
{{"expectations": [{{"text": "<assertion>", "passed": true, "evidence": "<one sentence>"}}]}}"""

COMPARE_TMPL = """Two answers (A and B) responded to the SAME task. Judge which is better:
correctness first, then signal-to-noise (a tighter correct answer beats a padded one).
You do NOT know which system produced which — judge blind.

TASK:
{prompt}

ASSERTIONS the answer should satisfy:
{assertions}

ANSWER A:
<<<BEGIN
{a}
END>>>

ANSWER B:
<<<BEGIN
{b}
END>>>

Output ONLY JSON, no prose:
{{"winner": "A" or "B", "reasoning": "<2-3 sentences>", "A": {{"score_1_to_10": <n>}}, "B": {{"score_1_to_10": <n>}}}}"""


def _numbered(items: list) -> str:
    return "\n".join(f"{i + 1}. {a}" for i, a in enumerate(items))


def grade(answer: str, ev: dict, condition: str, judge: str, cfg: dict, workdir: Path,
          *, timeout: int) -> dict:
    prompt = GRADE_TMPL.format(prompt=ev["prompt"], assertions=_numbered(ev["assertions"]),
                               answer=answer or "(no answer produced)")
    text, _ = run_text(judge, prompt, cfg, workdir / "judge", timeout=timeout, retries=2,
                       readonly=False)  # retries=2: a transient empty judge call → false 0/4 ("no verdict")
    return parse_grading(text, ev["assertions"], f"eval-{ev['id']}-{ev['name']}", condition)


def compare(with_text: str, without_text: str, ev: dict, judge: str, cfg: dict,
            workdir: Path, *, timeout: int) -> dict:
    a, b, key = blind_pair(with_text, without_text, ev["id"])
    prompt = COMPARE_TMPL.format(prompt=ev["prompt"], assertions=_numbered(ev["assertions"]),
                                 a=a or "(empty)", b=b or "(empty)")
    text, _ = run_text(judge, prompt, cfg, workdir / "compare", timeout=timeout, retries=2,
                       readonly=False)  # retries=2: transient judge failure → false tie
    return parse_comparison(text, key)


# --------------------------------------------------------------------------- #
# Orchestration.
# --------------------------------------------------------------------------- #
def load_evals(skill: str) -> dict:
    path = EVALS_ROOT / skill / "evals.json"
    if not path.exists():
        sys.exit(f"no evals at {path.relative_to(ROOT)} — create it first")
    return json.loads(path.read_text())


def load_skill_body(skill: str, provider: str) -> str:
    path = (ROOT / "marketplaces" / provider / "plugins" / "khenrix-utils"
            / "skills" / skill / "SKILL.md")
    if not path.exists():
        sys.exit(f"rendered skill body missing: {path.relative_to(ROOT)} (run render.py)")
    return strip_frontmatter(path.read_text())


def run_eval_for_provider(skill: str, provider: str, ev: dict, judge: str, cfg: dict,
                          itdir: Path, *, timeout: int, retries: int,
                          readonly: bool) -> list:
    body = load_skill_body(skill, provider)
    base = itdir / f"eval-{ev['id']}-{ev['name']}"
    runs = []
    outputs = {}
    for condition in ("with_skill", "without_skill"):
        wd = base / f"{provider}__{condition}"
        wd.mkdir(parents=True, exist_ok=True)
        prompt = build_condition_prompt(body, ev["prompt"], condition)
        (wd / "prompt.txt").write_text(prompt)
        text, rec = run_text(provider, prompt, cfg, wd, timeout=timeout, retries=retries,
                             readonly=readonly)
        (wd / "answer.md").write_text(text)
        outputs[condition] = text
        g = grade(text, ev, condition, judge, cfg, wd, timeout=timeout)
        (wd / "grading.json").write_text(json.dumps(g, indent=2))
        runs.append({
            "eval_id": ev["id"], "eval_name": f"eval-{ev['id']}-{ev['name']}",
            "executor": provider, "configuration": condition, "run_number": 1,
            "result": {
                "pass_rate": round(g["passed"] / g["total"], 4) if g["total"] else 0.0,
                "passed": g["passed"], "failed": g["total"] - g["passed"], "total": g["total"],
                "time_seconds": rec.get("duration_sec"), "tokens": None,
                "tool_calls": 0, "errors": 0 if rec.get("valid") else 1,
                "reason": rec.get("reason"),
            },
            "expectations": g["expectations"],
        })
    cmp = compare(outputs["with_skill"], outputs["without_skill"], ev, judge, cfg, base,
                  timeout=timeout)
    (base / "comparison.json").write_text(json.dumps(cmp, indent=2))
    return runs


def _checks():
    sys.path.insert(0, str(ROOT / "scripts" / "lib"))
    import checks  # noqa: E402
    return checks


def _write_receipt(skill, *, providers, mode, judge, delta, seeded):
    """Write evals/<skill>/receipt.json stamping the current source/eval-set hashes.
    For llm-council (orchestrator) gate on fanout --self-test, not a judge benchmark."""
    c = _checks()
    rec = {
        "skill": skill,
        "source_hash": c.source_hash(ROOT, skill),
        "eval_set_hash": c.eval_set_hash(ROOT, skill),
        "providers": providers, "mode": mode, "judge": judge,
        "delta_pass_rate": delta,
        "provenance": "seeded: blessed current committed state" if seeded else "eval",
    }
    if skill == "llm-council":
        rc = subprocess.run([sys.executable, str(FANOUT_DIR / "fanout.py"), "--self-test"])
        if rc.returncode != 0:  # never bless a failing engine with a green receipt
            raise SystemExit("llm-council self-test failed; not writing receipt")
        rec.update(self_test=True, synthesis_review="manual-attested")
    (EVALS_ROOT / skill / "receipt.json").write_text(json.dumps(rec, indent=2))


def seed_receipts(args) -> int:
    """Stamp a receipt for every eval'd skill at its current committed state."""
    for skill in _checks()._evald_skills(ROOT):
        _write_receipt(skill, providers=args.providers.split(","), mode=args.mode,
                       judge=args.judge, delta=None, seeded=True)
        print(f"  seeded receipt: {skill}")
    return 0


def run(args) -> int:
    spec = load_evals(args.skill)
    evals = spec["evals"]
    providers = [p.strip() for p in args.providers.split(",") if p.strip()]
    cfg = fanout.resolve_mode_config(_mode_args(args))
    timeout = fanout.effective_timeout(_mode_args(args))
    itdir = EVALS_ROOT / args.skill / "workspace" / f"iteration-{args.iteration}"
    itdir.mkdir(parents=True, exist_ok=True)

    all_runs = []
    for provider in providers:
        for ev in evals:
            print(f"  · {provider} / eval-{ev['id']}-{ev['name']} …", flush=True)
            all_runs.extend(run_eval_for_provider(
                args.skill, provider, ev, args.judge, cfg, itdir,
                timeout=timeout, retries=args.retries, readonly=args.readonly))

    benchmark = {
        "metadata": {"skill_name": args.skill, "judge": args.judge,
                     "providers": providers, "mode": args.mode,
                     "evals_run": [f"eval-{e['id']}-{e['name']}" for e in evals],
                     "runs_per_configuration": 1},
        "runs": all_runs,
        "run_summary": aggregate(all_runs),
        "notes": ["Portable harness: time_seconds from fan-out duration; token "
                  "accounting not captured cross-provider (tokens=null)."],
    }
    (itdir / "benchmark.json").write_text(json.dumps(benchmark, indent=2))
    _print_summary(benchmark, itdir)
    d = benchmark["run_summary"]["delta"].get("pass_rate")
    gate_ok = d is None or d >= 0
    if args.skill == "llm-council":
        # Orchestrator exception (docs/skill-eval-process.md): harness executors run
        # under LLM_COUNCIL_DEPTH=1, so an injected llm-council body can never convene
        # a real nested council — the judged delta here measures solo answers, i.e.
        # noise. The benchmark stays as advisory signal; the receipt gate is fanout
        # --self-test (enforced inside _write_receipt), never this delta.
        gate_ok = True
    if gate_ok:  # passing run → refresh the receipt
        _write_receipt(args.skill, providers=providers, mode=args.mode,
                       judge=args.judge, delta=d, seeded=False)
    return 0 if gate_ok else 1


def _mode_args(args):
    """Adapt our args into the shape fanout.resolve_mode_config/effective_timeout read."""
    ns = argparse.Namespace(mode=args.mode, timeout=args.timeout,
                            model_claude=None, model_codex=None, model_agy=None)
    return ns


def _print_summary(benchmark: dict, itdir: Path) -> None:
    s = benchmark["run_summary"]
    w = s["with_skill"].get("pass_rate", {}).get("mean")
    b = s["without_skill"].get("pass_rate", {}).get("mean")
    print(f"\n  with_skill pass_rate mean: {w}   baseline: {b}   "
          f"delta: {s['delta'].get('pass_rate')}")
    print(f"  artifacts: {itdir}")


# --------------------------------------------------------------------------- #
# Self-test — hermetic checks of the harness logic (no tokens, no subprocess).
# Live execution is covered by fanout.py --self-test and a real --run smoke.
# --------------------------------------------------------------------------- #
def self_test() -> int:
    results = []

    def check(label, cond, detail=""):
        results.append((label, bool(cond), detail))

    # frontmatter stripping
    body = strip_frontmatter("---\nname: x\ndescription: y\n---\n\n# Title\nbody")
    check("strip_frontmatter drops frontmatter", body.startswith("# Title"))
    check("strip_frontmatter no-op without frontmatter",
          strip_frontmatter("# Title\nb") == "# Title\nb")

    # condition prompts
    wp = build_condition_prompt("SKILLTEXT", "do X", "with_skill")
    check("with_skill injects body", "SKILLTEXT" in wp and "do X" in wp)
    check("baseline is bare prompt", build_condition_prompt("S", "do X", "without_skill") == "do X")

    # JSON extraction robustness
    check("extract plain json", extract_json('{"a":1}') == {"a": 1})
    check("extract fenced json", extract_json('text\n```json\n{"a":2}\n```\n') == {"a": 2})
    check("extract embedded json", extract_json('blah {"a":3} trailing') == {"a": 3})
    check("extract garbage -> None", extract_json("no json here") is None)

    # grading alignment (judge returns fewer/garbled expectations)
    asserts = ["A1", "A2", "A3"]
    raw = '{"expectations":[{"passed":true,"evidence":"e1"},{"passed":false,"evidence":"e2"}]}'
    g = parse_grading(raw, asserts, "eval-0-x", "with_skill")
    check("grading counts passed", g["passed"] == 1 and g["total"] == 3)
    check("grading aligns to canonical assertions",
          [e["text"] for e in g["expectations"]] == asserts)
    check("grading fills missing 3rd as fail", g["expectations"][2]["passed"] is False)

    # blind pairing + de-anonymization
    a, b, key = blind_pair("W", "O", 0)
    check("blind even: A=with", a == "W" and key["A"] == "with_skill")
    a, b, key = blind_pair("W", "O", 1)
    check("blind odd: A=without", a == "O" and key["A"] == "without_skill")
    cmp = parse_comparison('{"winner":"A","reasoning":"r","A":{"score_1_to_10":9},"B":{"score_1_to_10":5}}',
                           {"A": "without_skill", "B": "with_skill"})
    check("comparison de-anonymizes winner", cmp["winner_condition"] == "without_skill")

    # aggregation math + delta
    runs = [
        {"configuration": "with_skill", "result": {"pass_rate": 1.0, "time_seconds": 10, "tokens": None}},
        {"configuration": "with_skill", "result": {"pass_rate": 0.5, "time_seconds": 20, "tokens": None}},
        {"configuration": "without_skill", "result": {"pass_rate": 0.0, "time_seconds": 5, "tokens": None}},
    ]
    agg = aggregate(runs)
    check("aggregate with_skill mean", agg["with_skill"]["pass_rate"]["mean"] == 0.75)
    check("aggregate stddev present", "stddev" in agg["with_skill"]["pass_rate"])
    check("aggregate delta", agg["delta"]["pass_rate"] == 0.75)
    check("aggregate skips all-null tokens", "tokens" not in agg["with_skill"])

    passed = sum(1 for _, ok, _ in results if ok)
    for label, ok, detail in results:
        line = f"  {'PASS' if ok else 'FAIL'}  {label}"
        if detail and not ok:
            line += f"   [{detail}]"
        print(line)
    print(f"\nself-test: {passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Portable skill-eval harness")
    ap.add_argument("--skill", help="skill name under evals/<skill>/evals.json")
    ap.add_argument("--providers", default="claude",
                    help="executors to run the eval on (default: claude)")
    ap.add_argument("--judge", default=DEFAULT_JUDGE, help="grading/comparison model")
    ap.add_argument("--mode", choices=list(fanout.MODES), default="normal",
                    help="thinking mode for executors + judge (fanout MODES)")
    ap.add_argument("--iteration", type=int, default=1, help="workspace iteration-N")
    ap.add_argument("--retries", type=int, default=1)
    ap.add_argument("--timeout", type=int, default=None, help="per-attempt seconds (per-mode default)")
    sb = ap.add_mutually_exclusive_group()
    sb.add_argument("--readonly", dest="readonly", action="store_true", default=True,
                    help="run executors read-only / plan-only (claude/codex mechanically, agy best-effort) so an eval can't mutate config (default: on)")
    sb.add_argument("--no-readonly", dest="readonly", action="store_false",
                    help="run executors with full permissions (only for skills that must write)")
    ap.add_argument("--self-test", action="store_true", help="hermetic logic tests, no tokens")
    ap.add_argument("--seed-receipt", action="store_true",
                    help="stamp receipt.json for every eval'd skill at its current committed state")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.self_test:
        return self_test()
    if args.seed_receipt:
        return seed_receipts(args)
    if not args.skill:
        sys.exit("--skill is required (or use --self-test / --seed-receipt)")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
