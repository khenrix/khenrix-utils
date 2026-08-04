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


def parse_verdict(raw: str):
    """True/False if the judge said whether the skill should activate; None if it did not.

    THREE ANSWERS, BECAUSE THERE ARE THREE STATES. This returned `False` for text it could
    not read, and `False` is also a real verdict — so a judge that timed out, hit a quota
    wall or answered in prose was recorded as having said "do not activate". Every
    `near_miss` case expects exactly that, so a judge that never ran scored 100% on the
    near-miss axis and the run wrote a receipt over it.
    """
    s = (raw or "").strip()
    if not s:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", s, re.DOTALL)
    if fence:
        s = fence.group(1)
    cand = s[s.find("{"): s.rfind("}") + 1] if "{" in s and "}" in s else s
    try:
        payload = json.loads(cand)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or "activate" not in payload:
        return None
    return bool(payload["activate"])


def score(cases: list) -> dict:
    """cases: list of {kind, expected, got, readable}. Accuracy over the cases that WERE read.

    AN UNREADABLE CASE IS IN NEITHER NUMERATOR NOR DENOMINATOR, and it is reported. Counting
    it correct is the fail-open this function was written with; counting it INCORRECT is the
    mirror error, which manufactures a triggering failure out of a judge that never spoke.
    The caller decides what a run with unreadable cases is worth — `run` below refuses to
    report an accuracy at all when every case is unreadable.
    """
    readable = [c for c in cases if c.get("readable", c.get("got") is not None)]
    unreadable = len(cases) - len(readable)
    fires = [c for c in readable if c["kind"] == "should_trigger"]
    misses = [c for c in readable if c["kind"] == "near_miss"]
    tp = sum(1 for c in fires if c["got"] is True)
    tn = sum(1 for c in misses if c["got"] is False)
    total = len(readable)
    return {
        "should_trigger": {"correct": tp, "total": len(fires)},
        "near_miss": {"correct": tn, "total": len(misses)},
        "unreadable": unreadable,
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
            spec_.min_chars = 0   # judge verdicts are intentionally compact JSON (~100-200
                                  # chars) — fanout's MIN_SUBSTANTIVE_CHARS=400 floor exists
                                  # for council-seat answers, not judge verdicts; without this
                                  # every verdict scores invalid ("non_substantive") regardless
                                  # of correctness and run() reads "" for every case (mirrors
                                  # fanout.py's own --smoke precedent at s.min_chars = 0)
            m = fanout.run_council([spec_], retries=1, timeout=timeout, backoff=2.0,
                                   workdir=workdir / f"{kind}-{i}", prompt=jp)
            rec = m["providers"][0]
            raw = Path(rec["result_file"]).read_text() if rec.get("valid") else ""
            got = parse_verdict(raw)
            # `valid` and a readable verdict are TWO conditions and the second was assumed
            # from the first: a valid run whose text is prose parses to no verdict at all.
            readable = got is not None
            cases.append({"kind": kind, "prompt": prompt, "expected": kind == "should_trigger",
                          "got": got, "readable": readable,
                          "why": ("" if readable else
                                  ("the judge run was invalid: "
                                   + str(rec.get("reason", "no reason recorded"))
                                   if not rec.get("valid")
                                   else "the judge answered, and no activate verdict could be "
                                        "read from what it said"))})
            mark = "✓" if readable and got == (kind == "should_trigger") else \
                   ("?" if not readable else "✗")
            print(f"  {mark} [{kind}] {prompt[:60]}")
    result = score(cases)
    if result["unreadable"]:
        print(f"  ⚠ {result['unreadable']} of {len(cases)} case(s) produced no readable "
              "verdict and are counted in neither axis")
    if result["unreadable"] == len(cases):
        print("  ✗ no case produced a readable verdict — there is no measurement here")
        return 1
    (workdir / "triggers-result.json").write_text(json.dumps({"skill": args.skill,
        "result": result, "cases": cases}, indent=2))
    print(f"\n  accuracy: {result['accuracy']}  "
          f"(should_trigger {result['should_trigger']['correct']}/{result['should_trigger']['total']}, "
          f"near_miss {result['near_miss']['correct']}/{result['near_miss']['total']})")
    return 0 if result["accuracy"] >= 0.8 else 1


ARENA_TMPL = """A coding agent has these skills available (name + description):

{roster}

The user sends this message:
<<<BEGIN
{prompt}
END>>>

Judging ONLY from the names + descriptions, which ONE skill (if any) should
activate? Output ONLY JSON, no prose:
{{"winner": "<exact skill name, or none>", "why": "<one short sentence>"}}"""


OFF_ROSTER = "off-roster:"   # prefix for a winner the judge named that is on no roster


def _canonical(w: str, competitors: list):
    """The roster spelling of `w`, or None. Case-insensitive: SKILL.md `name` is
    lowercase by frontmatter rule, so folding can never merge two real competitors,
    and without it the fail-closed branch below would score a judge's "None" as a
    hallucination — the mirror error, manufacturing a routing failure out of a
    correct abstain."""
    for c in competitors:
        if w.lower() == str(c).lower():
            return c
    return None


def parse_arena_verdict(raw: str, competitors: list):
    """The roster name the judge picked, "none" if it picked none, None if it never answered.

    THREE OUTCOMES ON THE SAME AXIS AS `parse_verdict`, PLUS A FOURTH FOR AN ANSWER THAT IS
    NOT ON THE ROSTER. This returned the string "none" for text it could not read, and
    "none" is also a real verdict that `arena.json` really expects (2 of the 6 cases in
    evals/khenrix-audit/arena.json expect exactly it) — so a judge that timed out, hit a
    quota wall or answered in prose was recorded as having correctly abstained, and scored
    0.3333 on that file with a confusion matrix reading `none → none` for every one of them.
    Unreadable is now None and is counted in neither axis (see `score_arena`).

    A NAME THAT IS ON NO ROSTER IS ALSO NOT "none", and that was the same collapse one
    branch over: the judge was told "exact skill name, or none", so naming something else
    is a readable answer AND a routing failure, and rounding it to "none" scored it correct
    against every `expected: "none"` case. It now returns an `off-roster:` string, which is
    readable (it counts) and equals no `expected` (it counts as wrong), and which puts the
    name the judge actually said into the confusion matrix where someone can see it.
    """
    s = (raw or "").strip()
    if not s:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", s, re.DOTALL)
    if fence:
        s = fence.group(1)
    cand = s[s.find("{"): s.rfind("}") + 1] if "{" in s and "}" in s else s
    try:
        payload = json.loads(cand)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or "winner" not in payload:
        return None
    w = str(payload["winner"]).strip()
    if not w:
        return None            # the key arrived empty: a shape, not an answer
    hit = _canonical(w, competitors)
    if hit is not None:
        return hit
    # The judge subprocess is a REAL claude CLI session on this machine, so when the
    # skill under judgment is actually installed here it can answer with its own
    # environment's plugin-qualified display form (e.g. "khenrix-utils:khenrix-audit")
    # instead of the roster's bare NAME it was told to use — verified live 2026-07-31:
    # two genuinely-correct khenrix-audit arena wins were silently scored "none" by an
    # exact-match miss on exactly this prefix. Accept the bare suffix after the last ':'
    # as the same skill.
    hit = _canonical(w.rsplit(":", 1)[-1], competitors)
    return hit if hit is not None else f"{OFF_ROSTER}{w[:60]}"


def score_arena(cases: list) -> dict:
    """cases: list of {expected, got, readable}. Accuracy over the cases that WERE read.

    Same contract as `score`: an unreadable case is in NEITHER numerator nor denominator
    and is reported separately. Counting it correct is the fail-open this function was
    written with (every `expected: "none"` case scored right off a judge that never spoke);
    counting it incorrect is the mirror error. `run_arena` refuses to report an accuracy at
    all when nothing was readable.
    """
    readable = [c for c in cases if c.get("readable", c.get("got") is not None)]
    confusion: dict = {}
    correct = 0
    for c in readable:
        confusion.setdefault(c["expected"], {})
        confusion[c["expected"]][c["got"]] = confusion[c["expected"]].get(c["got"], 0) + 1
        correct += c["expected"] == c["got"]
    return {"accuracy": round(correct / len(readable), 4) if readable else 0.0,
            "confusion": confusion, "unreadable": len(cases) - len(readable)}


def run_arena(args) -> int:
    skills = [s.strip() for s in args.arena.split(",") if s.strip()]
    path = EVALS_ROOT / skills[0] / "arena.json"
    if not path.exists():
        sys.exit(f"no arena prompts at {path.relative_to(ROOT)} — create it "
                 '({"prompts": [{"prompt": "...", "expected": "<name-or-none>"}]})')
    spec = json.loads(path.read_text())
    roster = "\n".join(f"- NAME: {n}\n  DESCRIPTION: {d}"
                       for n, d in (load_skill_meta(s, args.judge) for s in skills))
    cfg = fanout.resolve_mode_config(argparse.Namespace(
        mode=args.mode, timeout=args.timeout, model_claude=None, model_codex=None, model_agy=None))
    timeout = fanout.effective_timeout(argparse.Namespace(mode=args.mode, timeout=args.timeout))
    workdir = EVALS_ROOT / skills[0] / "workspace" / "arena"
    workdir.mkdir(parents=True, exist_ok=True)
    names = [load_skill_meta(s, args.judge)[0] for s in skills]
    cases = []
    for i, case in enumerate(spec["prompts"]):
        jp = ARENA_TMPL.format(roster=roster, prompt=case["prompt"])
        spec_ = fanout.build_real_spec(args.judge, jp, timeout, cfg, workdir)
        spec_.min_chars = 0   # see run(): judge verdicts are compact JSON, not council answers
        m = fanout.run_council([spec_], retries=1, timeout=timeout, backoff=2.0,
                               workdir=workdir / f"p-{i}", prompt=jp)
        rec = m["providers"][0]
        raw = Path(rec["result_file"]).read_text() if rec.get("valid") else ""
        got = parse_arena_verdict(raw, names + ["none"])
        # `valid` and a readable verdict are TWO conditions — see run(): a valid run whose
        # text is prose names no winner at all.
        readable = got is not None
        cases.append({"prompt": case["prompt"], "expected": case["expected"], "got": got,
                      "readable": readable,
                      "why": ("" if readable else
                              ("the judge run was invalid: "
                               + str(rec.get("reason", "no reason recorded"))
                               if not rec.get("valid")
                               else "the judge answered, and no winner could be read from "
                                    "what it said"))})
        mark = "✓" if readable and got == case["expected"] else ("?" if not readable else "✗")
        print(f"  {mark} {case['prompt'][:60]} → {got}")
    result = score_arena(cases)
    if result["unreadable"]:
        print(f"  ⚠ {result['unreadable']} of {len(cases)} case(s) produced no readable "
              "winner and are counted in neither axis")
    if result["unreadable"] == len(cases):
        print("  ✗ no case produced a readable winner — there is no measurement here")
        return 1
    (workdir / "arena-result.json").write_text(json.dumps(
        {"skills": skills, "result": result, "cases": cases}, indent=2))
    print(f"\n  arena accuracy: {result['accuracy']}")
    return 0 if result["accuracy"] >= 0.8 else 1


def _self_test() -> int:
    ok = []
    fm = '---\nname: x\ndescription: >-\n  line one\n  line two\nallowed-tools: Bash\n---\nbody'
    ok.append(("folded description parsed", parse_frontmatter_field(fm, "description") == "line one line two"))
    ok.append(("plain name parsed", parse_frontmatter_field(fm, "name") == "x"))
    ok.append(("verdict true", parse_verdict('{"activate": true, "why": "y"}') is True))
    ok.append(("verdict fenced false", parse_verdict('```json\n{"activate": false}\n```') is False))
    # Not "false" — garbage with no JSON at all is unreadable, not a verdict of "don't
    # activate" (the collapse the new three-state contract two lines below exists to fix).
    ok.append(("verdict garbage → unreadable, not false", parse_verdict("nope") is None))
    r = score([{"kind": "should_trigger", "expected": True, "got": True},
               {"kind": "should_trigger", "expected": True, "got": False},
               {"kind": "near_miss", "expected": False, "got": False}])
    ok.append(("score accuracy 2/3", r["accuracy"] == round(2 / 3, 4)))
    ok.append(("score splits", r["should_trigger"]["correct"] == 1 and r["near_miss"]["correct"] == 1))
    # A judge that did not answer is not a judge that answered "no". Before this, an
    # invalid run read "" -> parse_verdict("") -> False, and every near_miss case
    # (expected False) scored CORRECT — a dead judge measured 100% on that axis.
    ok.append(("no verdict text is None, not False", parse_verdict("") is None))
    ok.append(("prose with no JSON is None", parse_verdict("Sorry, I can't help.") is None))
    ok.append(("a real verdict still reads", parse_verdict('{"activate": true}') is True))
    ok.append(("a real negative still reads", parse_verdict('{"activate": false}') is False))
    ok.append(("a fenced verdict still reads",
               parse_verdict('```json\n{"activate": true}\n```') is True))
    # Valid JSON that is still not a verdict: an array has no "activate" key to be missing
    # from, and an object can omit the key outright. Both must read as unreadable, not crash
    # `bool(payload["activate"])` and not silently resolve to `False`.
    ok.append(("valid JSON that is not an object is None", parse_verdict("[1, 2, 3]") is None))
    ok.append(("a JSON object without 'activate' is None",
               parse_verdict('{"why": "no verdict given"}') is None))
    dead = score([{"kind": "near_miss", "expected": False, "got": None, "readable": False},
                  {"kind": "near_miss", "expected": False, "got": None, "readable": False}])
    ok.append(("a dead judge scores 0, not 1.0", dead["accuracy"] == 0.0))
    ok.append(("a dead judge's cases are counted unreadable", dead["unreadable"] == 2))
    ok.append(("an unreadable case is in no denominator",
               dead["near_miss"]["total"] == 0 and dead["should_trigger"]["total"] == 0))
    mixed = score([{"kind": "near_miss", "expected": False, "got": False, "readable": True},
                   {"kind": "should_trigger", "expected": True, "got": None, "readable": False}])
    ok.append(("a mixed run scores only what it read",
               mixed["accuracy"] == 1.0 and mixed["unreadable"] == 1
               and mixed["should_trigger"]["total"] == 0))
    # --- arena mode (Task 14) ---
    ok.append(("arena verdict picks named winner",
               parse_arena_verdict('{"winner": "khenrix-wiki-add"}',
                                   ["khenrix-wiki-add", "save"]) == "khenrix-wiki-add"))
    ok.append(("arena verdict accepts plugin-qualified form of a real competitor",
               parse_arena_verdict('{"winner": "khenrix-utils:khenrix-audit"}',
                                   ["khenrix-audit", "khenrix-setup"]) == "khenrix-audit"))
    ok.append(("arena verdict says none when the judge says none",
               parse_arena_verdict('{"winner": "none"}', ["a", "none"]) == "none"))
    ar = score_arena([{"expected": "a", "got": "a"}, {"expected": "a", "got": "b"},
                      {"expected": "none", "got": "none"}])
    ok.append(("arena accuracy 2/3", ar["accuracy"] == round(2 / 3, 4)))
    ok.append(("arena confusion", ar["confusion"]["a"]["b"] == 1))
    # --- arena: a judge that never answered is not a judge that abstained -----------
    # The same collapse as parse_verdict's, one function over and one release later. "none"
    # is a REAL expected answer here (2 of the 6 cases in evals/khenrix-audit/arena.json),
    # so every unreadable case scored CORRECT against it and a dead judge measured 0.3333
    # with a `none → none` confusion row rather than 0.0 with an explicit refusal.
    ok.append(("arena: no text is None, not none", parse_arena_verdict("", ["a", "none"]) is None))
    ok.append(("arena: prose with no JSON is None",
               parse_arena_verdict("Sorry, I can't help.", ["a", "none"]) is None))
    ok.append(("arena: valid JSON that is not an object is None",
               parse_arena_verdict("[1, 2, 3]", ["a", "none"]) is None))
    ok.append(("arena: a JSON object without 'winner' is None",
               parse_arena_verdict('{"why": "no winner given"}', ["a", "none"]) is None))
    ok.append(("arena: an empty winner is a shape, not an answer",
               parse_arena_verdict('{"winner": "   "}', ["a", "none"]) is None))
    ok.append(("arena: a fenced winner still reads",
               parse_arena_verdict('```json\n{"winner": "a"}\n```', ["a", "none"]) == "a"))
    dead_a = score_arena([{"expected": "none", "got": None, "readable": False},
                          {"expected": "none", "got": None, "readable": False}])
    ok.append(("arena: a dead judge scores 0, not 1.0", dead_a["accuracy"] == 0.0))
    ok.append(("arena: a dead judge's cases are counted unreadable", dead_a["unreadable"] == 2))
    ok.append(("arena: an unreadable case is in no confusion row", dead_a["confusion"] == {}))
    mixed_a = score_arena([{"expected": "a", "got": "a", "readable": True},
                           {"expected": "none", "got": None, "readable": False}])
    ok.append(("arena: a mixed run scores only what it read",
               mixed_a["accuracy"] == 1.0 and mixed_a["unreadable"] == 1))
    # --- arena: a name on no roster is not "none" either ----------------------------
    # Readable (the judge spoke, in the right shape) but NOT an abstain, so it must score
    # wrong against `expected: "none"` instead of right, and it must carry the name it said.
    off = parse_arena_verdict('{"winner": "bogus"}', ["a", "b", "none"])
    ok.append(("arena: an unrostered winner is off-roster, not none",
               off.startswith(OFF_ROSTER) and "bogus" in off))
    ok.append(("arena: plugin-qualified but NOT a competitor is off-roster too",
               parse_arena_verdict('{"winner": "claude-obsidian:wiki-lint"}',
                                   ["khenrix-audit", "none"]).startswith(OFF_ROSTER)))
    ok.append(("arena: off-roster scores WRONG against an expected none",
               score_arena([{"expected": "none", "got": off, "readable": True}])["accuracy"]
               == 0.0))
    # ...and the mirror error this change could have introduced: `name` is lowercase by
    # frontmatter rule, so a judge answering "None" is a correct abstain in different case,
    # NOT a hallucination. Folding is what keeps the fail-closed branch above honest.
    ok.append(("arena: a capitalised None is still an abstain, not off-roster",
               parse_arena_verdict('{"winner": "None"}', ["a", "none"]) == "none"))
    ok.append(("arena: a capitalised competitor still resolves to its roster spelling",
               parse_arena_verdict('{"winner": "Khenrix-Audit"}',
                                   ["khenrix-audit", "none"]) == "khenrix-audit"))
    # --- min_chars regression (2026-07-31): a real judge verdict is short compact JSON
    # (~100-200 chars), well under fanout's council-answer floor. Pins the exact failure
    # this override fixes — without spec_.min_chars = 0 in run()/run_arena(), fanout scores
    # every correct verdict "non_substantive" and eval_trigger reads "" for every case
    # (observed live: accuracy 0.4545, should_trigger 0/6, near_miss 5/5 — every "false"
    # verdict was spuriously right and every "true" one spuriously wrong).
    short_verdict = '{"activate": true, "why": "matches the description directly"}'
    default_spec = fanout.build_real_spec("claude", "prompt", 60, {}, Path("/tmp"))
    ok.append(("build_real_spec defaults to the council floor (why the override is needed)",
               default_spec.min_chars == fanout.MIN_SUBSTANTIVE_CHARS
               and len(short_verdict) < fanout.MIN_SUBSTANTIVE_CHARS))
    ok.append(("judge verdict scores non_substantive at the default floor",
               fanout.score_seat(short_verdict, None, fanout.MIN_SUBSTANTIVE_CHARS)["cause"]
               == "non_substantive"))
    ok.append(("judge verdict scores ok once min_chars=0 (the fix)",
               fanout.score_seat(short_verdict, None, 0)["status"] == "ok"))
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
    ap.add_argument("--arena", default=None,
                    help="comma-separated skill names — pairwise routing eval")
    args = ap.parse_args(argv)
    if args.self_test:
        return _self_test()
    if args.arena:
        return run_arena(args)
    if not args.skill:
        sys.exit("--skill is required (or use --self-test)")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
