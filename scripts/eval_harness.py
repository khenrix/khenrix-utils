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
  - ORCHESTRATOR skills (llm-council): executors run under LLM_COUNCIL_DEPTH=1, so an
    injected body cannot convene a real nested council — the benchmark runs but is
    advisory only (see the gate exception in run()). Its mode/model wiring is verified
    deterministically by `fanout.py --self-test` / `--smoke`, which gates its receipt.

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
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FANOUT_DIR = ROOT / "shared" / "skills" / "llm-council" / "scripts"
sys.path.insert(0, str(FANOUT_DIR))
import fanout  # noqa: E402  (maintainer dev tool: reach into the council engine)

EVALS_ROOT = ROOT / "evals"
DEFAULT_JUDGE = "claude"

# Skills the LLM-judge harness cannot fairly gate, so their receipt is blessed by a
# deterministic test suite instead (analogous to llm-council's fanout --self-test).
# The wiki skills wrap the in-repo `wikisync` engine: a read-only executor can READ the
# skill source + engine from the repo cwd, so the "skill-free" baseline is contaminated
# and the with-vs-without delta is meaningless. The 70 wikisync unit tests are the real
# correctness gate; the judge run (if any) stays advisory.
DETERMINISTIC_GATED = {
    "khenrix-wiki-add":  ["python3", "-m", "unittest", "discover", "-s",
                          str(ROOT / "shared" / "lib" / "wikisync" / "tests")],
    "khenrix-wiki-sync": ["python3", "-m", "unittest", "discover", "-s",
                          str(ROOT / "shared" / "lib" / "wikisync" / "tests")],
    # A read-only with-skill/baseline harness cannot exercise forge's defining behaviour —
    # a clone fleet, three providers, a fresh verifier — and an ordinary judge receipt would
    # certify prose while leaving the dangerous mechanics untouched. The gate is the hermetic
    # forge suite, which `_write_receipt` runs and refuses to write on. It is a REAL suite,
    # not a `--help`: handover's record and provenance rules, the CLI front end, and every
    # refusal `--gc` makes before it deletes anything.
    #
    # NOTE the judge run still EXECUTES: `gate_ok = True` is applied AFTER it in `run()`, so
    # routing makes the delta advisory, not the run free. The cost control is the cheap eval
    # set beside this file.
    #
    # `uvx`, NOT `sys.executable` and not a hardcoded "python3", and that is a MEASUREMENT
    # rather than a preference: this machine's interpreter is 3.14.4 against a stated 3.11
    # floor and cannot import pytest at all (`python3 -c "import pytest"` → ModuleNotFoundError,
    # rc=1), so both spellings would fail here for a reason that has nothing to do with forge.
    # The two entries above survive on `python3` only because `unittest` is stdlib. This is
    # the same fallback `RUN_PYTEST` in the Makefile takes for the same reason.
    # DERIVED FROM DISK, NOT RESTATED. This named three suites while the Makefile named
    # thirty-one, and the omitted set included `test_forge_packaging.py` — the module that
    # checks rendered-facade resolution and the quote prose — so breaking the facade left the
    # certifying gate green. A hand-kept list is a second place to be right about what
    # "certified" means, and it was already wrong.
    "llm-forge": ["uvx", "--with", "pytest", "pytest", "-q"] + [
        str(p) for p in sorted((ROOT / "tests").glob("test_forge_*.py"))],
}


_COUNT = re.compile(r"\b(\d+)\s+(passed|failed|skipped|error|errors|xfailed|xpassed)\b")
# unittest's summary is a DIFFERENT SHAPE and two of the three gated skills use it. Reading
# only pytest's meant `tests_run: 0` for a run of 83 real tests, so the counts check refused a
# receipt it should have written — fail-closed, and wrong about which runner it was looking at.
_UNITTEST_RAN = re.compile(r"^Ran (\d+) tests? in ", re.M)
_UNITTEST_SKIP = re.compile(r"\bskipped=(\d+)")
_UNITTEST_BAD = re.compile(r"\b(?:failures|errors)=(\d+)")


def _pytest_counts(text: str) -> dict:
    """The runner's own summary, as numbers rather than as an exit code.

    A receipt written on `returncode == 0` says a PROCESS finished. It does not say anything
    ran: an all-skipped run exits 0, a run that collects nothing exits 5 but a wrapper can
    swallow it, and `true` exits 0 having tested the empty set. The counts are what turn "the
    command succeeded" into "these many tests executed and none was skipped".
    """
    text = text or ""
    out = {"tests_run": 0, "skipped": 0, "failed": 0}
    if (m := _UNITTEST_RAN.search(text)):
        # `Ran N tests` counts every test INCLUDING skips, where pytest's `N passed` excludes
        # them — so the skips come back out to keep both runners' `tests_run` meaning the same
        # thing: tests that actually executed.
        skipped = sum(int(x) for x in _UNITTEST_SKIP.findall(text))
        out["skipped"] = skipped
        out["failed"] = sum(int(x) for x in _UNITTEST_BAD.findall(text))
        out["tests_run"] = int(m.group(1)) - skipped
        return out
    for n, word in _COUNT.findall(text):
        n = int(n)
        if word in ("passed", "xfailed", "xpassed"):
            out["tests_run"] += n
        elif word == "skipped":
            out["skipped"] += n
        elif word in ("failed", "error", "errors"):
            out["failed"] += n
            out["tests_run"] += n
    return out


def _counts_are_evidence(counts: dict) -> bool:
    """Whether these counts can support a receipt.

    A SKIP IN THE CERTIFYING SUITE IS A TEST THAT DID NOT RUN, and the receipt would say the
    suite passed. That is stricter than `make verify`, deliberately: `verify` runs a suite,
    this decides whether a run may be recorded as certification.
    """
    return counts["tests_run"] > 0 and counts["skipped"] == 0 and counts["failed"] == 0

# WHICH gate earned this receipt, as a name a reader can check against the command above. A
# single literal here was a false provenance string the moment a third skill was routed through
# that dict — and a receipt exists to say what ran, so being wrong about that is worse than
# recording nothing. A `KeyError` is the right failure for a skill routed through
# DETERMINISTIC_GATED with no name: `.get(skill, "unknown")` would write the receipt anyway.
DETERMINISTIC_GATE_NAMES = {
    "khenrix-wiki-add":  "wikisync-unittests",
    "khenrix-wiki-sync": "wikisync-unittests",
    # NOT "forge-handover-cli-gc-suites" ANY MORE. That named three modules, and the command
    # above is now derived from disk and runs every one — so the string was a provenance claim
    # about what earned the receipt that stopped being true the moment the gate widened. A
    # receipt exists to say what ran; being wrong about that is worse than saying less.
    "llm-forge":         "forge-suite-all",
}


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


def materialize_fixtures(ev: dict, src_dir: Path, dest: Path) -> Path:
    """Copy every fixture named in ev['files'] from src_dir into dest (created), so
    both conditions read identical local files. A name may be a file or a subdir
    (copied recursively). Missing sources are skipped silently — the eval author
    owns evals/<skill>/fixtures/. Returns dest (what {fixture_dir} resolves to)."""
    dest.mkdir(parents=True, exist_ok=True)
    for name in ev.get("files") or []:
        src = src_dir / name
        if src.is_file():
            (dest / name).parent.mkdir(parents=True, exist_ok=True)
            (dest / name).write_bytes(src.read_bytes())
        elif src.is_dir():
            for p in src.rglob("*"):
                if p.is_file():
                    d = dest / name / p.relative_to(src)
                    d.parent.mkdir(parents=True, exist_ok=True)
                    d.write_bytes(p.read_bytes())
    return dest


def render_prompt(ev: dict, fixture_dir: Path) -> str:
    """Substitute the {fixture_dir} placeholder in the eval prompt with the
    materialized workspace path (identical for both conditions)."""
    return ev["prompt"].replace("{fixture_dir}", str(fixture_dir))


def blind_winner(comparisons: list) -> str:
    """Aggregate the per-eval blind A/B verdicts into one winner: whichever
    condition won strictly more evals, else 'tie'. RECORDED in the receipt but
    ADVISORY — the commit gate is the assertion delta (see run()); the blind A/B
    rewards concision on strong executors, so it must not gate.

    UNREADABLE VERDICTS ARE EXCLUDED rather than counted as ties, because the tie column is
    the one that decides the winner: a dead judge inflated it and looked like agreement.

    A CONSTANT SLOT IS NOT A TIE, and this is the check the recorded field was waiting for.
    `blind_pair` alternates which condition sits in slot A by eval-id parity, so a judge with
    a fixed slot preference maps to with, without, with, without… — a clean N-N that reads as
    N genuinely matched pairs. `winner_slot` was written to disk by every run and read by
    nothing, which is what made position bias invisible.
    """
    readable = [c for c in comparisons if (c or {}).get("winner_condition") is not None]
    if not readable:
        return "unreadable"
    # ONLY OVER SLOTS THAT WERE RECORDED. An ABSENT slot and a REPEATED slot are different
    # facts, and reading the first as the second made every comparison built without the
    # field — the self-test's own cases, and any caller that constructs one by hand — look
    # like a position-biased judge. That is this project's own "nothing leaves the same record
    # as nobody", written into the check for it.
    slots = [(c or {}).get("winner_slot") for c in readable]
    recorded = [x for x in slots if x not in (None, "", "?")]
    if len(recorded) == len(readable) and len(recorded) > 1 and len(set(recorded)) == 1:
        return "slot_degenerate"
    tally = {"with_skill": 0, "without_skill": 0, "tie": 0}
    for c in readable:
        cond = c.get("winner_condition")
        tally[cond] = tally.get(cond, 0) + 1
    if tally["with_skill"] > tally["without_skill"]:
        return "with_skill"
    if tally["without_skill"] > tally["with_skill"]:
        return "without_skill"
    return "tie"


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


def blind_pair(with_text: str, without_text: str, idx):
    """Assign the two outputs to A/B deterministically (no RNG — alternate by eval
    id so neither condition sits in a fixed slot across the set). Returns
    (a_text, b_text, key) where key maps each slot back to its condition.
    Eval ids may be ints OR descriptive string slugs — derive a stable parity for
    both (byte-sum is deterministic; `str % int` on a slug is a TypeError)."""
    if not isinstance(idx, int):
        idx = sum(str(idx).encode())
    if idx % 2 == 0:
        return with_text, without_text, {"A": "with_skill", "B": "without_skill"}
    return without_text, with_text, {"A": "without_skill", "B": "with_skill"}


def parse_comparison(raw: str, key: dict) -> dict:
    """Judge's blind verdict → comparison.json, de-anonymized via the key."""
    obj = extract_json(raw) or {}
    winner_slot = str(obj.get("winner", "")).strip().upper()[:1]
    # `None`, NOT `"tie"`. COMPARE_TMPL asks for "A" or "B" and never offers a tie, so every
    # tie this produced was a parse failure, an empty answer or an off-slot response wearing a
    # verdict's clothes — a judge that timed out yields raw="" -> {} -> "tie". This is
    # `eval_trigger.parse_verdict`'s already-fixed bug one module over, and that function's
    # docstring names it: "a judge that timed out, hit a quota wall or answered in prose was
    # recorded as having said 'do not activate'". Unreadable is its own state.
    winner_condition = key.get(winner_slot)
    return {
        "winner_slot": winner_slot or "?",
        "winner_condition": winner_condition,
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


def _summarize(runs: list) -> dict:
    """Per-condition stats + delta for ONE set of runs (the whole pool, or one
    provider's slice). Extracted so the pooled and per-provider blocks are computed by
    identical code and cannot drift."""
    out = {}
    for cond in ("with_skill", "without_skill"):
        rs = [r["result"] for r in runs if r["configuration"] == cond]
        block = {}
        for metric in ("pass_rate", "time_seconds", "tokens"):
            st = _stats([r.get(metric) for r in rs])
            if st:
                block[metric] = st
        out[cond] = block
    delta = {}
    for metric in ("pass_rate", "time_seconds", "tokens"):
        w = out["with_skill"].get(metric, {}).get("mean")
        b = out["without_skill"].get(metric, {}).get("mean")
        if w is not None and b is not None:
            delta[metric] = round(w - b, 4)
    out["delta"] = delta
    return out


def quantum(runs: list) -> float:
    """The harness's noise floor: the largest shift in a provider's mean pass_rate that
    ONE assertion flip can produce — 1 / (n_evals * smallest assertion count).

    A per-provider mean is over n_evals cases, so one flip moves it by 1/(n_evals*total),
    three times more than it moves a pooled mean over three providers. Measured
    run-to-run drift on UNCHANGED skill bodies is 0.06-0.08 (chunk-map +0.1042→+0.0417,
    khenrix-upgrade +0.1805→+0.0972, same panel/models/mode/judge, 2026-07-30), so a
    delta smaller than one quantum is a judge verdict, not a measurement. Floored at
    0.05. REPORTED here; it becomes the gate band in the follow-up plan.
    """
    totals = [r["result"]["total"] for r in runs if r["result"].get("total")]
    n_evals = len({r["eval_id"] for r in runs})
    if not totals or not n_evals:
        return 0.05
    return max(0.05, round(1.0 / (n_evals * min(totals)), 4))


def aggregate(runs: list) -> dict:
    """runs → run_summary {with_skill, without_skill, delta, by_provider}.

    The pooled with_skill/without_skill/delta blocks are BYTE-COMPATIBLE with the
    pre-split schema — skill-creator interop and historical receipt comparison both
    depend on them. `by_provider` is purely additive.

    Pooling across executors is what let a per-provider regression hide: khenrix-upgrade
    pooled to +0.0972 while claude sat at -0.1250. Splitting is the MEASUREMENT; the
    gate stays pooled (docs/superpowers/specs/2026-07-30-per-provider-eval-gating-design.md).
    """
    summary = _summarize(runs)
    by_provider = {}
    for p in sorted({r["executor"] for r in runs if r.get("executor")}):
        slice_ = [r for r in runs if r.get("executor") == p]
        block = _summarize(slice_)
        block["n_evals"] = len({r["eval_id"] for r in slice_})
        block["quantum"] = quantum(slice_)
        # Only an EXECUTOR failure invalidates a provider. A judge failure is a failure
        # of the shared instrument (build_run_result) and invalidates the run, not the
        # executor — blaming agy for a claude-judge failure would be simply wrong.
        block["status"] = ("invalid" if any(r["result"].get("executor_error")
                                            for r in slice_) else "ok")
        by_provider[p] = block
    summary["by_provider"] = by_provider
    invalid = [r for r in runs if r["result"].get("errors")]
    if invalid:
        # An invalid run is graded 0/N and folded into its own side's mean, so the
        # pooled delta is an artifact rather than a measurement. Mark it so the number
        # cannot be silently reused.
        summary["valid"] = False
        summary["invalid_runs"] = len(invalid)
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
    if readonly:
        prompt = fanout.apply_readonly_posture(prompt)  # same soft layer as the council
    spec = fanout.build_real_spec(provider, prompt, timeout, cfg, workdir)
    # The council's substantive-answer floor and proof-of-read sentinel are COUNCIL
    # policy, not a property of running a provider: an executor's correct answer here
    # may legitimately be two lines, and no sentinel is injected into eval prompts. Opt
    # out explicitly so the with-vs-without benchmark keeps its historical semantics —
    # same reason build_real_spec never bakes in the council member note.
    spec.min_chars = 0
    agy_wt = None
    if readonly:
        fanout.make_readonly(spec)
        if spec.name == "agy":  # and the same worktree containment as the council
            agy_wt = fanout.isolate_agy_worktree(spec, workdir)
    try:
        m = fanout.run_council([spec], retries=retries, timeout=timeout, backoff=2.0,
                               workdir=workdir, prompt=prompt)
    finally:
        fanout.remove_agy_worktree(agy_wt)
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
    text, jrec = run_text(judge, prompt, cfg, workdir / "judge", timeout=timeout, retries=2,
                          readonly=False)  # retries=2: a transient empty judge call → false 0/4 ("no verdict")
    g = parse_grading(text, ev["assertions"], f"eval-{ev['id']}-{ev['name']}", condition)
    # A dead judge fails EVERY assertion with "no verdict returned" — a 0/N that is averaged
    # into this condition's mean exactly like a dead executor, and biases the delta the same
    # way. retries=2 only lowers the odds; the whole point of failing closed is that a
    # BIASING failure can't be managed by probability. Surface it so the caller can veto.
    # Require a verdict PER assertion, not merely well-formed JSON: `{}` parses fine while
    # parse_grading scores every assertion "no verdict returned" — the exact 0/N artifact
    # this signal exists to reject.
    # Require a real verdict per assertion. Length alone is not enough:
    # {"expectations":[{},{},{}]} is well-formed and correctly-sized, yet parse_grading
    # scores every assertion "no verdict returned" — the exact 0/N artifact this rejects.
    obj = extract_json(text)
    exps = obj.get("expectations") if isinstance(obj, dict) else None
    g["judge_ok"] = (bool(jrec.get("valid")) and isinstance(exps, list)
                     and len(exps) >= len(ev["assertions"])
                     and all(isinstance(e, dict) and isinstance(e.get("passed"), bool)
                             for e in exps[:len(ev["assertions"])]))
    # Keep the judge's own failure cause when it has one — "no verdict" would send the
    # reader to the wrong remedy for a judge that actually timed out.
    # Keep the transport cause only when the transport actually failed — a valid record
    # carries reason "ok", which would otherwise label a malformed verdict as fine.
    g["judge_reason"] = (None if g["judge_ok"]
                         else (jrec.get("reason") if not jrec.get("valid")
                               else "judge returned no verdict"))
    return g


def compare(with_text: str, without_text: str, ev: dict, judge: str, cfg: dict,
            workdir: Path, *, timeout: int) -> dict:
    a, b, key = blind_pair(with_text, without_text, ev["id"])
    prompt = COMPARE_TMPL.format(prompt=ev["prompt"], assertions=_numbered(ev["assertions"]),
                                 a=a or "(empty)", b=b or "(empty)")
    text, jrec = run_text(judge, prompt, cfg, workdir / "compare", timeout=timeout, retries=2,
                          readonly=False)  # retries=2: transient judge failure → false tie
    c = parse_comparison(text, key)
    # THE JUDGE RECORD IS CARRIED OUT, on `grade`'s precedent. `compare` discarded it, so
    # unlike `grade` — which has judge_ok/judge_reason — this path had no channel to report
    # that its judge never spoke, and an unreadable verdict was indistinguishable from a
    # considered one.
    c["judge_ok"] = bool((jrec or {}).get("valid")) and c["winner_condition"] is not None
    c["judge_reason"] = (None if c["judge_ok"]
                         else ((jrec or {}).get("reason") or "no readable winner in the reply"))
    return c


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


def build_run_result(rec: dict, g: dict) -> dict:
    """The `runs[].result` record for one (provider, condition) run.

    executor_error and judge_error are SEPARATE fields because the judge is a shared
    instrument: it is always DEFAULT_JUDGE, so a judge failure while grading agy's
    answer says nothing about agy. Attributing it per-provider would blame the wrong
    executor once the summary is split by executor (aggregate's by_provider block).
    `errors` is retained as the OR of the two — the pooled gate in run() and every
    existing consumer still read it.
    """
    executor_error = 0 if rec.get("valid") else 1
    judge_error = 0 if g.get("judge_ok") else 1
    return {
        "pass_rate": round(g["passed"] / g["total"], 4) if g["total"] else 0.0,
        "passed": g["passed"], "failed": g["total"] - g["passed"], "total": g["total"],
        "time_seconds": rec.get("duration_sec"), "tokens": None,
        "tool_calls": 0,
        "executor_error": executor_error,
        "judge_error": judge_error,
        "errors": 1 if (executor_error or judge_error) else 0,
        # Keep the transport cause when the transport failed; otherwise the judge's.
        "reason": (rec.get("reason") if executor_error else g.get("judge_reason")),
    }


def run_eval_for_provider(skill: str, provider: str, ev: dict, judge: str, cfg: dict,
                          itdir: Path, *, timeout: int, retries: int,
                          readonly: bool) -> list:
    body = load_skill_body(skill, provider)
    base = itdir / f"eval-{ev['id']}-{ev['name']}"
    fixtures_src = EVALS_ROOT / skill / "fixtures"
    runs = []
    outputs = {}
    for condition in ("with_skill", "without_skill"):
        wd = base / f"{provider}__{condition}"
        wd.mkdir(parents=True, exist_ok=True)
        fx = materialize_fixtures(ev, fixtures_src, wd / "fixtures")
        eval_prompt = render_prompt(ev, fx)
        prompt = build_condition_prompt(body, eval_prompt, condition)
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
            "result": build_run_result(rec, g),
            "expectations": g["expectations"],
        })
    cmp = compare(outputs["with_skill"], outputs["without_skill"], ev, judge, cfg, base,
                  timeout=timeout)
    (base / "comparison.json").write_text(json.dumps(cmp, indent=2))
    return runs, cmp


def _checks():
    sys.path.insert(0, str(ROOT / "scripts" / "lib"))
    import checks  # noqa: E402
    return checks


def _write_receipt(skill, *, providers, mode, judge, delta, seeded, blind_winner=None,
                   models=None):
    """Write evals/<skill>/receipt.json stamping the current source/eval-set hashes.
    For llm-council (orchestrator) gate on fanout --self-test, not a judge benchmark.
    `blind_winner` is the aggregated blind A/B verdict of the run (None when seeded).
    `models` records the resolved executor/judge model(s) actually used, so a run on a
    non-default model (e.g. --model-claude claude-opus-4-8 while Fable-5 is walled) is
    provable from the receipt, not silently attributed to the MODES default."""
    c = _checks()
    rec = {
        "skill": skill,
        "source_hash": c.source_hash(ROOT, skill),
        "eval_set_hash": c.eval_set_hash(ROOT, skill),
        "providers": providers, "mode": mode, "judge": judge,
        "delta_pass_rate": delta,
        "blind_winner": blind_winner,
        "provenance": "seeded: blessed current committed state" if seeded else "eval",
    }
    if models:
        rec["models"] = models
    if not seeded:
        # WHAT CERTIFIED THIS RUN, SAID BY THE RECEIPT RATHER THAN INFERRED AT THE GATE.
        # MEASURED: an ordinary skill's real eval wrote `provenance: "eval"` and no
        # `self_test` — a field only the llm-council and deterministic-gated branches below
        # set — and `checks._receipt_is_certified` then refused it, because "absent" was
        # treated as the SEEDED shape. So `make eval SKILL=khenrix-setup` produced a receipt
        # that `make precommit` rejected, and the only way past was to seed over the real
        # result with a weaker one. Reproduced on khenrix-setup and khenrix-upgrade.
        #
        # `delta-gate` IS THE HONEST NAME. `_write_receipt` is reached only when `gate_ok`,
        # which for an ordinary skill is `delta is not None and delta >= 0 and not invalid` —
        # so writing this field is recording the gate that already passed, not asserting a
        # second one. The two branches below overwrite it with the stronger thing they ran.
        rec["certified_by"] = "delta-gate"
    if skill == "llm-council":
        rc = subprocess.run([sys.executable, str(FANOUT_DIR / "fanout.py"), "--self-test"])
        if rc.returncode != 0:  # never bless a failing engine with a green receipt
            raise SystemExit("llm-council self-test failed; not writing receipt")
        rec.update(self_test=True, certified_by="fanout --self-test",
                   synthesis_review="manual-attested")
    elif skill in DETERMINISTIC_GATED:
        cmd = DETERMINISTIC_GATED[skill]
        rc = subprocess.run(cmd, capture_output=True, text=True)
        print(rc.stdout[-2000:] if rc.stdout else "", end="")
        if rc.returncode != 0:  # unit tests are the gate — never bless a failing engine
            raise SystemExit(f"{skill} deterministic tests failed; not writing receipt")
        counts = _pytest_counts((rc.stdout or "") + (rc.stderr or ""))
        if not _counts_are_evidence(counts):
            # AN EXIT CODE IS NOT A TEST COUNT. An all-skipped run exits 0, and so does a
            # command that runs nothing at all — both would have written a green receipt.
            raise SystemExit(
                f"{skill} deterministic gate exited 0 but its counts are not evidence "
                f"({counts}); not writing receipt")
        rec.update(deterministic_gate=DETERMINISTIC_GATE_NAMES[skill], self_test=True,
                   certified_by=DETERMINISTIC_GATE_NAMES[skill],
                   gate_command=cmd, gate_counts=counts)
    (EVALS_ROOT / skill / "receipt.json").write_text(json.dumps(rec, indent=2))


def seed_receipts(args) -> int:
    """Stamp a receipt at the current committed state. With --skill, seed just that one
    (e.g. re-blessing a skill whose only change is a mechanical render.py bundling, or a
    deterministic-gated skill); otherwise seed every eval'd skill."""
    skills = [args.skill] if args.skill else _checks()._evald_skills(ROOT)
    for skill in skills:
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
    comparisons = []
    for provider in providers:
        for ev in evals:
            print(f"  · {provider} / eval-{ev['id']}-{ev['name']} …", flush=True)
            runs, cmp = run_eval_for_provider(
                args.skill, provider, ev, args.judge, cfg, itdir,
                timeout=timeout, retries=args.retries, readonly=args.readonly)
            all_runs.extend(runs)
            comparisons.append(cmp)

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
    bw = blind_winner(comparisons)
    print(f"  blind A/B winner: {bw}   ({_blind_tally(comparisons)})  [advisory]")
    # Gate: a non-negative assertion delta — the skill must not make answers worse.
    # The blind A/B winner is RECORDED but ADVISORY: on a strong executor it rewards the
    # tighter baseline over a correct-but-more-thorough skill answer (a concision bias,
    # not a correctness signal — observed 2026-07-12 on hookify: a clearly positive
    # assertion delta, incl. a case the baseline failed and the skill passed, yet the
    # blind A/B still went to the tighter baseline). The assertion delta is the "does it
    # help" signal; a non-negative one must not be vetoed by
    # a concision-driven blind tie/loss. Read the recorded blind_winner when triaging, but
    # don't gate on it. `d is not None` guards the degenerate case (empty eval set / empty
    # providers → no runs → no delta) so a receipt is never earned with zero evidence;
    # the llm-council + DETERMINISTIC_GATED overrides below set gate_ok=True regardless.
    # An executor that timed out or died is graded 0/4 on an empty answer, then averaged
    # into its side's mean — so an invalid run doesn't just add noise, it BIASES the delta:
    # a with_skill error sinks it (looks like a regression), a baseline error inflates it
    # (looks like a pass and earns a receipt). Observed 2026-07-25 on khenrix-upgrade: the
    # with_skill side timed out for -0.29, the serial re-run's BASELINE timed out for
    # +0.375, and the second one silently wrote a receipt. Where the delta IS the gate, an
    # invalid run means there is no measurement — fail closed rather than bless it. The two
    # overrides below gate on a self-test/unit suite instead, so a flaky executor there
    # costs an advisory number, not the gate; they deliberately stay unaffected.
    invalid = [r for r in all_runs if r["result"].get("errors")]
    gate_ok = (d is not None and d >= 0 and not invalid)
    if args.skill == "llm-council":
        # Orchestrator exception (docs/skill-eval-process.md): harness executors run
        # under LLM_COUNCIL_DEPTH=1, so an injected llm-council body can never convene
        # a real nested council — the judged delta AND blind A/B measure solo answers,
        # i.e. noise. The benchmark stays advisory; the receipt gate is fanout
        # --self-test (enforced inside _write_receipt), never this delta/winner.
        gate_ok = True
        bw = "n/a-orchestrator"
    elif args.skill in DETERMINISTIC_GATED:
        # For the wiki skills the read-only baseline reads the in-repo skill source, so the
        # with-vs-without delta is meaningless; for llm-forge a read-only harness cannot drive
        # a clone fleet at all. Either way the judge run is advisory and the receipt gate is
        # the suite named in DETERMINISTIC_GATE_NAMES (enforced inside _write_receipt).
        gate_ok = True
        bw = "n/a-deterministic"
    if gate_ok:  # passing run → refresh the receipt
        models = {p: cfg.get(p, {}).get("model") for p in providers}
        models["judge"] = cfg.get(args.judge, {}).get("model")
        _write_receipt(args.skill, providers=providers, mode=args.mode,
                       judge=args.judge, delta=d, blind_winner=bw, seeded=False, models=models)
    return 0 if gate_ok else 1


def _blind_tally(comparisons: list) -> dict:
    """The counts behind `blind_winner`, with unreadable verdicts REPORTED rather than
    absorbed into `tie`. A run whose judge died half the time and one whose judge genuinely
    split are different runs, and the receipt should not spell them the same way."""
    t = {"with_skill": 0, "without_skill": 0, "tie": 0, "unreadable": 0}
    for c in comparisons:
        cond = (c or {}).get("winner_condition")
        if cond is None:
            t["unreadable"] += 1
        else:
            t[cond] = t.get(cond, 0) + 1
    return t


def _mode_args(args):
    """Adapt our args into the shape fanout.resolve_mode_config/effective_timeout read.
    The --model-* overrides let evals run when a MODES-default model is walled (e.g. a
    Fable-5 credit wall → --model-claude claude-opus-4-8); record the substitution."""
    ns = argparse.Namespace(mode=args.mode, timeout=args.timeout,
                            model_claude=getattr(args, "model_claude", None),
                            model_codex=getattr(args, "model_codex", None),
                            model_agy=getattr(args, "model_agy", None))
    return ns


def _print_summary(benchmark: dict, itdir: Path) -> None:
    s = benchmark["run_summary"]
    print()
    # Per-provider FIRST: the pooled number is the gate, but a pooled pass can hide a
    # provider-sized regression, and nobody can triage what is never printed.
    for p, blk in sorted(s.get("by_provider", {}).items()):
        d = blk["delta"].get("pass_rate")
        q = blk.get("quantum")
        pw = blk["with_skill"].get("pass_rate", {}).get("mean")
        pb = blk["without_skill"].get("pass_rate", {}).get("mean")
        note = ""
        if d is not None and q is not None:
            # Naming the band now means an operator can already tell a one-assertion
            # wobble from a real regression, ahead of it becoming the gate.
            if d < -q:
                note = f"   ⚠ below the noise floor (-{q}) — a real regression"
            elif d < 0:
                note = f"   · negative but inside the noise floor (±{q})"
        if blk.get("status") != "ok":
            note = f"   ⚠ {str(blk['status']).upper()} — executor failed; score is an artifact"
        print(f"  {p:8} with {pw}  base {pb}  delta {d}{note}")
    w = s["with_skill"].get("pass_rate", {}).get("mean")
    b = s["without_skill"].get("pass_rate", {}).get("mean")
    print(f"\n  POOLED  with {w}  base {b}  delta {s['delta'].get('pass_rate')}   [THE GATE]")
    if s.get("valid") is False:
        print(f"  ⚠ pooled delta is an ARTIFACT — {s.get('invalid_runs')} invalid run(s)")
    # An errored condition is graded 0/4 ("No answer was produced") and averaged in like
    # any other score, so it moves the delta in whichever direction it lands on — down if
    # with_skill errored, UP if the baseline did. Print it: without this line a contended
    # run and a real regression are indistinguishable at the console.
    for r in benchmark.get("runs", []):
        if r["result"].get("errors"):
            print(f"  ⚠ INVALID RUN  {r['eval_name']} / {r.get('executor')} / "
                  f"{r['configuration']}  reason={r['result'].get('reason')} — "
                  f"scored 0 and folded into the delta")
    print(f"  artifacts: {itdir}")


# --------------------------------------------------------------------------- #
# Self-test — hermetic checks of the harness logic (no tokens, no subprocess).
# Live execution is covered by fanout.py --self-test and a real --run smoke.
# --------------------------------------------------------------------------- #
def self_test() -> int:
    results = []

    def check(label, cond, detail=""):
        results.append((label, bool(cond), detail))

    # Every DETERMINISTIC_GATED skill must have a gate NAME. `_write_receipt` indexes
    # DETERMINISTIC_GATE_NAMES directly — deliberately, since a receipt that cannot say what
    # gated it should not be written — but that KeyError would land at the END of a paid run.
    # This check costs nothing and moves it to `make eval-test`.
    check("every deterministic-gated skill names its gate",
          set(DETERMINISTIC_GATED) == set(DETERMINISTIC_GATE_NAMES),
          str(sorted(set(DETERMINISTIC_GATED) ^ set(DETERMINISTIC_GATE_NAMES))))

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

    # aggregation math + delta (pooled block must stay byte-compatible)
    runs = [
        {"eval_id": 0, "executor": "claude", "configuration": "with_skill",
         "result": {"pass_rate": 1.0, "time_seconds": 10, "tokens": None, "total": 4}},
        {"eval_id": 1, "executor": "claude", "configuration": "with_skill",
         "result": {"pass_rate": 0.5, "time_seconds": 20, "tokens": None, "total": 4}},
        {"eval_id": 0, "executor": "claude", "configuration": "without_skill",
         "result": {"pass_rate": 0.0, "time_seconds": 5, "tokens": None, "total": 4}},
    ]
    agg = aggregate(runs)
    check("aggregate with_skill mean", agg["with_skill"]["pass_rate"]["mean"] == 0.75)
    check("aggregate stddev present", "stddev" in agg["with_skill"]["pass_rate"])
    check("aggregate delta", agg["delta"]["pass_rate"] == 0.75)
    check("aggregate skips all-null tokens", "tokens" not in agg["with_skill"])

    # per-provider split — opposing deltas must NOT cancel
    split = [
        {"eval_id": 0, "executor": "claude", "configuration": "with_skill",
         "result": {"pass_rate": 1.0, "time_seconds": 1, "tokens": None, "total": 4}},
        {"eval_id": 0, "executor": "claude", "configuration": "without_skill",
         "result": {"pass_rate": 0.5, "time_seconds": 1, "tokens": None, "total": 4}},
        {"eval_id": 0, "executor": "codex", "configuration": "with_skill",
         "result": {"pass_rate": 0.25, "time_seconds": 1, "tokens": None, "total": 4}},
        {"eval_id": 0, "executor": "codex", "configuration": "without_skill",
         "result": {"pass_rate": 0.75, "time_seconds": 1, "tokens": None, "total": 4}},
    ]
    a = aggregate(split)
    check("pooled delta cancels the opposing providers", a["delta"]["pass_rate"] == 0.0)
    check("claude delta is positive", a["by_provider"]["claude"]["delta"]["pass_rate"] == 0.5)
    check("codex delta is negative", a["by_provider"]["codex"]["delta"]["pass_rate"] == -0.5)
    check("by_provider records n_evals", a["by_provider"]["claude"]["n_evals"] == 1)
    check("by_provider defaults to status ok", a["by_provider"]["codex"]["status"] == "ok")

    # executor_error marks ONLY its own provider invalid; judge_error does not
    ex_err = [dict(r) for r in split]
    ex_err[2] = {**ex_err[2], "result": {**ex_err[2]["result"], "executor_error": 1,
                                         "errors": 1}}
    a = aggregate(ex_err)
    check("executor_error marks that provider invalid",
          a["by_provider"]["codex"]["status"] == "invalid")
    check("executor_error leaves the other provider ok",
          a["by_provider"]["claude"]["status"] == "ok")
    check("any invalid run marks the pooled block invalid", a["valid"] is False)
    check("invalid_runs is counted", a["invalid_runs"] == 1)
    jt = [dict(r) for r in split]
    jt[2] = {**jt[2], "result": {**jt[2]["result"], "judge_error": 1, "errors": 1}}
    check("judge_error does NOT mark the provider invalid",
          aggregate(jt)["by_provider"]["codex"]["status"] == "ok")

    # quantum = the largest mean shift ONE assertion flip can cause, floored at 0.05
    check("quantum for 2 evals x 4 assertions", quantum([
        {"eval_id": 0, "result": {"total": 4}}, {"eval_id": 1, "result": {"total": 4}}]) == 0.125)
    check("quantum uses the SMALLEST assertion count", quantum([
        {"eval_id": 0, "result": {"total": 8}}, {"eval_id": 1, "result": {"total": 4}}]) == 0.125)
    check("quantum floors at 0.05", quantum(
        [{"eval_id": i, "result": {"total": 10}} for i in range(20)]) == 0.05)
    check("quantum on empty runs is the floor", quantum([]) == 0.05)

    # error attribution: the judge is a SHARED instrument
    rec_ok = {"valid": True, "reason": "ok", "duration_sec": 12}
    rec_dead = {"valid": False, "reason": "timeout", "duration_sec": 300}
    g_ok = {"passed": 2, "total": 4, "judge_ok": True, "judge_reason": None}
    g_bad = {"passed": 0, "total": 4, "judge_ok": False,
             "judge_reason": "judge returned no verdict"}
    r = build_run_result(rec_dead, g_ok)
    check("dead executor sets executor_error only",
          r["executor_error"] == 1 and r["judge_error"] == 0)
    check("dead executor keeps the transport reason", r["reason"] == "timeout")
    r = build_run_result(rec_ok, g_bad)
    check("dead judge sets judge_error only",
          r["judge_error"] == 1 and r["executor_error"] == 0)
    check("dead judge keeps the judge reason", r["reason"] == "judge returned no verdict")
    check("errors stays the OR for back-compat", r["errors"] == 1)
    check("clean run has no errors", build_run_result(rec_ok, g_ok)["errors"] == 0)
    check("pass_rate computed from the grading",
          build_run_result(rec_ok, g_ok)["pass_rate"] == 0.5)

    # fixture materialization + {fixture_dir} substitution (Task 1)
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        src = tdp / "fixtures"
        src.mkdir()
        (src / "bm.json").write_text('{"k":1}')
        ev = {"id": 0, "name": "fx", "prompt": "read {fixture_dir}/bm.json",
              "files": ["bm.json"], "assertions": ["x"]}
        ws = materialize_fixtures(ev, src_dir=src, dest=tdp / "ws")
        check("fixtures materialized into workspace", (ws / "bm.json").exists())
        rp = render_prompt(ev, ws)
        check("fixture_dir placeholder substituted", "{fixture_dir}" not in rp and str(ws) in rp)
        check("no-files eval is a no-op copy",
              materialize_fixtures({"prompt": "x"}, src_dir=src, dest=tdp / "ws2").exists())

    # blind-winner aggregation across comparisons (Task 1)
    comps = [{"winner_condition": "with_skill"}, {"winner_condition": "with_skill"},
             {"winner_condition": "without_skill"}]
    check("blind_winner picks majority with_skill", blind_winner(comps) == "with_skill")
    check("blind_winner tie on equal", blind_winner(
        [{"winner_condition": "with_skill"}, {"winner_condition": "without_skill"}]) == "tie")
    check("blind_winner without_skill when it leads", blind_winner(
        [{"winner_condition": "without_skill"}, {"winner_condition": "tie"}]) == "without_skill")

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
    ap.add_argument("--model-claude", help="override the claude executor+judge model "
                    "(e.g. claude-opus-4-8 when the MODES default is unavailable)")
    ap.add_argument("--model-codex", help="override the codex executor model")
    ap.add_argument("--model-agy", help="override the agy executor model")
    ap.add_argument("--iteration", type=int, default=1, help="workspace iteration-N")
    ap.add_argument("--retries", type=int, default=1)
    ap.add_argument("--timeout", type=int, default=None, help="per-attempt seconds (per-mode default)")
    sb = ap.add_mutually_exclusive_group()
    sb.add_argument("--readonly", dest="readonly", action="store_true", default=True,
                    help="run executors read-only / plan-only (all three mechanically: claude plan mode, codex sandbox, agy --mode plan) so an eval can't mutate config (default: on)")
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
