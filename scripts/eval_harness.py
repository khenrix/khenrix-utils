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
  eval_harness.py --self-test          # hermetic unit tests of the harness logic (no tokens)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FANOUT_DIR = ROOT / "shared" / "skills" / "llm-council" / "scripts"
sys.path.insert(0, str(FANOUT_DIR))
import fanout  # noqa: E402  (maintainer dev tool: reach into the council engine)

_REAL_SUBPROCESS_RUN = subprocess.run


def _checks():
    sys.path.insert(0, str(ROOT / "scripts" / "lib"))
    import checks  # noqa: E402
    return checks

EVALS_ROOT = ROOT / "evals"

_MAKE_ENV_ARGUMENTS = (
    ("KHENRIX_EVAL_SKILL_RAW", "--skill", True),
    ("KHENRIX_EVAL_PROVIDERS_RAW", "--providers", False),
    ("KHENRIX_EVAL_MODE_RAW", "--mode", False),
    ("KHENRIX_EVAL_TIMEOUT_RAW", "--timeout", False),
    ("KHENRIX_EVAL_RETRIES_RAW", "--retries", False),
    ("KHENRIX_EVAL_MODEL_CLAUDE_RAW", "--model-claude", False),
    ("KHENRIX_EVAL_MODEL_CODEX_RAW", "--model-codex", False),
    ("KHENRIX_EVAL_MODEL_AGY_RAW", "--model-agy", False),
)
_MAKE_ORIGINAL_ARGUMENTS = (
    "SKILL", "PROVIDERS", "MODE", "TIMEOUT", "RETRIES",
    "MODELCLAUDE", "MODELCODEX", "MODELAGY",
)
_MAKE_CONTROL_ENV = ("MAKEFLAGS", "MFLAGS", "MAKEOVERRIDES")


def _argv_from_make_env(env: dict[str, str]) -> list[str]:
    """Convert raw target-private Make exports into argv without a shell expansion seam."""
    argv = []
    for env_name, flag, required in _MAKE_ENV_ARGUMENTS:
        value = env.pop(env_name, "")
        if value or required:
            argv.append(f"{flag}={value}")
    for name in (*_MAKE_ORIGINAL_ARGUMENTS, *_MAKE_CONTROL_ENV):
        env.pop(name, None)
    return argv


def _argv_from_make_process(pid: str, env: dict[str, str],
                            *, proc_root: Path = Path("/proc")) -> list[str]:
    """Recover literal command-line assignments before GNU Make can expand them."""
    if not pid.isascii() or not pid.isdigit() or int(pid) <= 0:
        raise ValueError("--from-make-process requires a positive decimal parent pid")
    path = proc_root / pid / "cmdline"
    try:
        tokens = [os.fsdecode(raw) for raw in path.read_bytes().split(b"\0") if raw]
    except OSError as exc:
        raise ValueError(
            f"cannot read raw GNU Make argv from {path}; invoke eval_harness.py directly: {exc}") from exc
    names = {name.removeprefix("KHENRIX_EVAL_").removesuffix("_RAW"): (flag, required)
             for name, flag, required in _MAKE_ENV_ARGUMENTS}
    # The model variables use Make's historical spellings without underscores.
    names.update({
        "MODELCLAUDE": ("--model-claude", False),
        "MODELCODEX": ("--model-codex", False),
        "MODELAGY": ("--model-agy", False),
    })
    names.pop("MODEL_CLAUDE", None)
    names.pop("MODEL_CODEX", None)
    names.pop("MODEL_AGY", None)
    values = {}
    for token in tokens[1:]:
        name, separator, value = token.partition("=")
        if separator and name in names:
            values[name] = value
    argv = []
    ordered = (
        ("SKILL", "--skill", True),
        ("PROVIDERS", "--providers", False),
        ("MODE", "--mode", False),
        ("TIMEOUT", "--timeout", False),
        ("RETRIES", "--retries", False),
        ("MODELCLAUDE", "--model-claude", False),
        ("MODELCODEX", "--model-codex", False),
        ("MODELAGY", "--model-agy", False),
    )
    for name, flag, required in ordered:
        value = values.get(name, "")
        if value or required:
            argv.append(f"{flag}={value}")
    for name in (*_MAKE_ORIGINAL_ARGUMENTS, *_MAKE_CONTROL_ENV):
        env.pop(name, None)
    return argv


def _normalize_entry_argv(argv: list[str] | None) -> list[str]:
    raw = list(sys.argv[1:] if argv is None else argv)
    make_process = [arg for arg in raw if arg.startswith("--from-make-process=")]
    if make_process:
        if len(raw) != 1:
            raise ValueError("--from-make-process must be the only command-line argument")
        return _argv_from_make_process(make_process[0].split("=", 1)[1], os.environ)
    if "--from-make-env" not in raw:
        return raw
    if raw != ["--from-make-env"]:
        raise ValueError("--from-make-env must be the only command-line argument")
    return _argv_from_make_env(os.environ)

# Compatibility/readability view only. checks.py owns command construction and receipt
# validation; execution calls it again at candidate-capture time so this import-time view
# cannot become the command that earns a receipt after the test roster changes.
DETERMINISTIC_GATED = {
    skill: _checks().deterministic_gate_command(ROOT, skill)
    for skill in _checks().GATE_EVIDENCE_SKILLS
}


_COUNT = re.compile(
    r"\b(\d+)\s+(passed|failed|skipped|deselected|error|errors|xfailed|xpassed)\b")
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
        if word == "passed":
            out["tests_run"] += n
        elif word in ("skipped", "deselected", "xfailed", "xpassed"):
            # Expected failures and unexpected passes are both unresolved suite states,
            # not clean certification. The receipt schema has one non-clean count, so keep
            # them with skips/deselections and make `_counts_are_evidence` fail closed.
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
    skill: _checks().SELF_TEST_CERTIFIERS[skill]
    for skill in _checks().GATE_EVIDENCE_SKILLS
}


def _deterministic_gate_env(skill: str) -> dict[str, str] | None:
    """Remove ambient pytest selectors that a receipt cannot hash or attest."""
    env = os.environ.copy()
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    if skill != "llm-forge":
        return env
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("PYTEST_PLUGINS", None)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    return env


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


def _no_symlink_components(path: Path, label: str) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        if current.is_symlink():
            raise ValueError(f"{label} must not traverse a symlink ({current})")
        if not current.exists():
            break


def _contained(anchor: Path, candidate: Path, label: str) -> None:
    _no_symlink_components(anchor, f"{label} anchor")
    _no_symlink_components(candidate, label)
    try:
        candidate.resolve(strict=False).relative_to(anchor.resolve(strict=False))
    except ValueError as exc:
        raise ValueError(f"{label} escapes its anchor") from exc


def _eval_workspace_base(ev: dict, itdir: Path) -> Path:
    """Construct one eval workspace from the manifest's unique rendered ID."""
    checks = _checks()
    eval_id = checks._safe_eval_component(Path("<eval>"), 0, "id", ev.get("id"))
    checks._safe_eval_component(Path("<eval>"), 0, "name", ev.get("name"))
    base = itdir / f"eval-{eval_id}"
    _contained(itdir, base, "eval workspace")
    return base


def _prepare_condition_workspace(itdir: Path, workdir: Path) -> None:
    """Create one clean condition workspace, anchored beneath the iteration."""
    _contained(itdir, workdir, "eval condition workspace")
    if workdir.exists():
        if not workdir.is_dir():
            raise ValueError(f"eval condition workspace is not a directory: {workdir}")
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)


def materialize_fixtures(ev: dict, src_dir: Path, dest: Path) -> Path:
    """Copy validated fixtures while proving both paths stay inside their anchors.

    This is intentionally independent of manifest parsing: a caller can hand this helper a
    synthetic eval, so containment and symlink checks must run before it creates, reads, or
    writes anything. Requested fixtures are required; silently skipping one makes two eval
    conditions look comparable while evaluating different inputs.
    """
    checks = _checks()

    names = ev.get("files")
    if names is None:
        names = []
    if not isinstance(names, list):
        raise ValueError("eval files must be a list")

    if not names:
        _no_symlink_components(dest, "fixture destination")
        dest.mkdir(parents=True, exist_ok=True)
        return dest
    if src_dir.is_symlink() or not src_dir.is_dir():
        raise ValueError(f"fixture source directory is not a regular directory: {src_dir}")

    copy_plan: list[tuple[Path, Path]] = []
    for index, raw_name in enumerate(names):
        name = checks._safe_fixture_path(Path("<eval>"), index, raw_name)
        relative = Path(*name.split("/"))
        src = src_dir / relative
        target = dest / relative
        _contained(src_dir, src, f"fixture source {name!r}")
        _contained(dest, target, f"fixture destination {name!r}")
        if src.is_symlink():
            raise ValueError(f"fixture source {name!r} must not be a symlink")
        if src.is_file():
            copy_plan.append((src, target))
            continue
        if not src.is_dir():
            raise ValueError(f"requested fixture is missing or not a regular file/directory: {name}")
        stack = [src]
        while stack:
            directory = stack.pop()
            for child in sorted(directory.iterdir(), reverse=True):
                child_relative = child.relative_to(src_dir)
                child_target = dest / child_relative
                _contained(src_dir, child, f"fixture source {child_relative.as_posix()!r}")
                _contained(dest, child_target,
                           f"fixture destination {child_relative.as_posix()!r}")
                if child.is_symlink():
                    raise ValueError(f"fixture source {child_relative.as_posix()!r} must not be a symlink")
                if child.is_file():
                    copy_plan.append((child, child_target))
                elif child.is_dir():
                    stack.append(child)
                else:
                    raise ValueError(
                        f"fixture source {child_relative.as_posix()!r} is not a regular file/directory")

    dest.mkdir(parents=True, exist_ok=True)
    for src, target in copy_plan:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(src.read_bytes())
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
            workdir: Path, *, timeout: int, provider: str = "") -> dict:
    a, b, key = blind_pair(with_text, without_text, ev["id"])
    prompt = COMPARE_TMPL.format(prompt=ev["prompt"], assertions=_numbered(ev["assertions"]),
                                 a=a or "(empty)", b=b or "(empty)")
    # Sub-workdir is PER PROVIDER: all three providers compare the same eval, so a
    # shared `compare/` dir had the last one overwrite the others' judge artifacts.
    sub = f"compare-{provider}" if provider else "compare"
    text, jrec = run_text(judge, prompt, cfg, workdir / sub, timeout=timeout, retries=2,
                          readonly=False)  # retries=2: transient judge failure → false tie
    c = parse_comparison(text, key)
    # THE JUDGE RECORD IS CARRIED OUT, on `grade`'s precedent. `compare` discarded it, so
    # unlike `grade` — which has judge_ok/judge_reason — this path had no channel to report
    # that its judge never spoke, and an unreadable verdict was indistinguishable from a
    # considered one.
    c["judge_ok"] = bool((jrec or {}).get("valid")) and c["winner_condition"] is not None
    c["judge_reason"] = (None if c["judge_ok"]
                         else ((jrec or {}).get("reason") or "no readable winner in the reply"))
    # Identity, so a per-provider tally is reconstructible from the artifact alone.
    c["executor"] = provider
    c["eval_id"] = ev["id"]
    c["judge"] = judge
    c["judge_model"] = (cfg.get(judge) or {}).get("model")
    return c


# --------------------------------------------------------------------------- #
# Orchestration.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CandidateSnapshot:
    """The exact source, eval inputs, rendered bodies, and certifier being evaluated."""

    skill: str
    source_hash: str
    source_inputs: object
    eval_inputs: object
    gate_tree: object | None
    rendered_bodies: tuple[tuple[str, str], ...]
    gate_command: tuple[str, ...] | None


def _capture_candidate_once(skill: str, providers: list[str] | tuple[str, ...], *,
                            include_bodies: bool) -> CandidateSnapshot:
    c = _checks()
    skill = c.validate_skill_name(skill)
    unknown = [provider for provider in providers if provider not in c.CLIS]
    if unknown:
        raise ValueError(f"unsupported eval provider(s): {unknown}; expected {list(c.CLIS)}")
    source_inputs = c.source_input_snapshot(ROOT, skill)
    inputs = c.eval_input_snapshot(ROOT, skill)
    bodies = tuple(
        (provider, load_skill_body(skill, provider))
        for provider in providers
    ) if include_bodies else ()
    command = c.deterministic_gate_command(ROOT, skill)
    gate_tree = c.gate_tree_snapshot(ROOT) if command is not None else None
    return CandidateSnapshot(
        skill=skill,
        source_hash=source_inputs.source_hash,
        source_inputs=source_inputs,
        eval_inputs=inputs,
        gate_tree=gate_tree,
        rendered_bodies=bodies,
        gate_command=tuple(command) if command is not None else None,
    )


def _candidate_drift(before: CandidateSnapshot, after: CandidateSnapshot) -> list[str]:
    drift = []
    if before.source_hash != after.source_hash:
        drift.append("source")
    if before.eval_inputs.eval_set_hash != after.eval_inputs.eval_set_hash:
        drift.append("eval set")
    if (before.eval_inputs.manifest != after.eval_inputs.manifest
            or before.eval_inputs.roster != after.eval_inputs.roster
            or before.eval_inputs.fixture_dirs != after.eval_inputs.fixture_dirs
            or before.eval_inputs.fixture_files != after.eval_inputs.fixture_files):
        drift.append("eval manifest/fixtures")
    if before.rendered_bodies != after.rendered_bodies:
        drift.append("rendered skill body")
    if before.gate_command != after.gate_command:
        drift.append("deterministic gate command")
    if ((before.gate_tree is None) != (after.gate_tree is None)
            or (before.gate_tree is not None and after.gate_tree is not None
                and (before.gate_tree.gate_tree_hash != after.gate_tree.gate_tree_hash
                     or before.gate_tree.directories != after.gate_tree.directories
                     or before.gate_tree.files != after.gate_tree.files))):
        drift.append("deterministic gate tree")
    return drift


def capture_candidate(skill: str, providers: list[str] | tuple[str, ...], *,
                      include_bodies: bool = True) -> CandidateSnapshot:
    """Capture a stable candidate; refuse an edit that lands during the capture itself."""
    first = _capture_candidate_once(skill, providers, include_bodies=include_bodies)
    second = _capture_candidate_once(skill, providers, include_bodies=include_bodies)
    drift = _candidate_drift(first, second)
    if drift:
        raise SystemExit(
            f"{skill} changed while the eval candidate was being captured ({', '.join(drift)}); "
            "not starting or writing a receipt")
    return first


def _assert_candidate_current(candidate: CandidateSnapshot) -> None:
    providers = [provider for provider, _body in candidate.rendered_bodies]
    first = _capture_candidate_once(
        candidate.skill,
        providers,
        include_bodies=bool(candidate.rendered_bodies),
    )
    second = _capture_candidate_once(
        candidate.skill, providers, include_bodies=bool(candidate.rendered_bodies))
    drift = sorted(set(_candidate_drift(candidate, first)
                       + _candidate_drift(candidate, second)
                       + _candidate_drift(first, second)))
    if drift:
        raise SystemExit(
            f"{candidate.skill} changed during the eval ({', '.join(drift)}); "
            "not writing a receipt")


def _materialize_input_snapshot(candidate: CandidateSnapshot, dest: Path) -> Path:
    """Write captured fixture bytes to an isolated source tree used by every condition."""
    _contained(dest.parent, dest, "captured fixture directory")
    if dest.exists():
        if dest.is_symlink() or not dest.is_dir():
            raise ValueError(f"captured fixture directory is unsafe: {dest}")
        shutil.rmtree(dest)
    if not candidate.eval_inputs.fixture_dirs and not candidate.eval_inputs.fixture_files:
        return dest
    dest.mkdir(parents=True)
    prefix = Path("fixtures")
    directory_modes = []
    for rel, mode in candidate.eval_inputs.fixture_dirs:
        directory = dest / Path(rel).relative_to(prefix)
        directory.mkdir(parents=True, exist_ok=True)
        directory_modes.append((directory, mode))
    for rel, mode, content in candidate.eval_inputs.fixture_files:
        target = dest / Path(rel).relative_to(prefix)
        _contained(dest, target, "captured fixture file")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        target.chmod(mode)
    for directory, mode in reversed(directory_modes):
        directory.chmod(mode)
    return dest


def _gate_git_env(skill: str) -> dict[str, str]:
    return _checks().sanitized_git_env(_deterministic_gate_env(skill))


def _rebase_gate_command(command: tuple[str, ...], live_root: Path,
                         snapshot_root: Path) -> list[str]:
    """Rebase standalone absolute argv paths under live_root into the snapshot."""
    rebased = []
    for arg in command:
        path = Path(arg)
        if path.is_absolute():
            try:
                arg = str(snapshot_root / path.relative_to(live_root))
            except ValueError:
                pass
        rebased.append(arg)
    return rebased


def _materialize_gate_tree(candidate: CandidateSnapshot, dest: Path) -> Path:
    """Create and stage the exact Git-visible tree a deterministic certifier executes."""
    if candidate.gate_tree is None:
        raise ValueError(f"{candidate.skill} has no captured deterministic gate tree")
    _contained(dest.parent, dest, "deterministic candidate snapshot")
    if dest.exists():
        raise ValueError(f"deterministic candidate snapshot already exists: {dest}")
    dest.mkdir(parents=True)
    directory_modes = []
    for rel, mode in candidate.gate_tree.directories:
        directory = dest / rel
        _contained(dest, directory, "deterministic candidate directory")
        directory.mkdir(parents=True, exist_ok=True)
        directory_modes.append((directory, mode))
    for rel, mode, content in candidate.gate_tree.files:
        target = dest / rel
        _contained(dest, target, "deterministic candidate source")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        target.chmod(mode)
    for directory, mode in reversed(directory_modes):
        directory.chmod(mode)
    command = _checks().deterministic_gate_command(dest, candidate.skill)
    if tuple(command or ()) != tuple(candidate.gate_command or ()):
        raise SystemExit(
            f"{candidate.skill} deterministic snapshot does not reproduce its captured "
            "gate command; not writing a receipt")
    git_env = _gate_git_env(candidate.skill)
    git_authority = _checks().git_authority
    for git_command in (["init", "-q"], ["add", "-f", "-A"]):
        result = git_authority.run(
            git_command, cwd=dest, env=git_env, capture_output=True, text=True,
            runner=_REAL_SUBPROCESS_RUN)
        if result.returncode != 0:
            raise SystemExit(
                f"cannot prepare deterministic gate snapshot Git identity: "
                f"{result.stderr.strip()}; not writing a receipt")
    materialized = _checks().gate_tree_snapshot(dest)
    if (materialized.gate_tree_hash != candidate.gate_tree.gate_tree_hash
            or materialized.directories != candidate.gate_tree.directories
            or materialized.files != candidate.gate_tree.files):
        raise SystemExit(
            f"{candidate.skill} deterministic candidate changed while its private Git "
            "snapshot was materialized; not writing a receipt")
    return dest


def load_evals(skill: str) -> dict:
    skill = _checks().validate_skill_name(skill)
    try:
        spec, _raw = _checks().load_eval_manifest(ROOT, skill)
    except FileNotFoundError:
        path = EVALS_ROOT / skill / "evals.json"
        sys.exit(f"no evals at {path.relative_to(ROOT)} — create it first")
    return spec


def load_skill_body(skill: str, provider: str) -> str:
    c = _checks()
    skill = c.validate_skill_name(skill)
    provider = c.validate_provider_name(provider)
    path = (ROOT / "marketplaces" / provider / "plugins" / "khenrix-utils"
            / "skills" / skill / "SKILL.md")
    if not path.exists():
        sys.exit(f"rendered skill body missing: {path.relative_to(ROOT)} (run render.py)")
    _mode, body = c._regular_file_state(path, ROOT)
    expected = c.expected_rendered_skill(ROOT, skill, provider)
    if body != expected:
        sys.exit(
            f"rendered skill body is stale: {path.relative_to(ROOT)} "
            "(run render.py before eval)")
    return strip_frontmatter(body.decode("utf-8"))


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
                          readonly: bool, skill_body: str | None = None,
                          fixtures_src: Path | None = None) -> list:
    c = _checks()
    skill = c.validate_skill_name(skill)
    provider = c.validate_provider_name(provider)
    base = _eval_workspace_base(ev, itdir)
    body = load_skill_body(skill, provider) if skill_body is None else skill_body
    fixtures_src = (EVALS_ROOT / skill / "fixtures"
                    if fixtures_src is None else fixtures_src)
    runs = []
    outputs = {}
    for condition in ("with_skill", "without_skill"):
        wd = base / f"{provider}__{condition}"
        _prepare_condition_workspace(itdir, wd)
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
                  timeout=timeout, provider=provider)
    # PER-PROVIDER FILENAME: this dir is shared by all three providers, so a single
    # comparison.json had each provider silently overwrite the previous one's verdict —
    # a per-provider blind tally was not reconstructible from the artifacts at all.
    (base / f"comparison.{provider}.json").write_text(json.dumps(cmp, indent=2))
    return runs, cmp


def _write_receipt(skill, *, providers, mode, judge, delta, seeded, blind_winner=None,
                   models=None, summary=None, candidate: CandidateSnapshot):
    """Write a receipt for the captured candidate, never for whatever is live at the end.

    For llm-council (orchestrator) gate on fanout --self-test, not a judge benchmark.
    `blind_winner` is the aggregated blind A/B verdict of the run (None when seeded).
    `models` records the resolved executor/judge model(s) actually used, so a run on a
    non-default model (e.g. --model-claude claude-opus-4-8 while Fable-5 is walled) is
    provable from the receipt, not silently attributed to the MODES default."""
    c = _checks()
    skill = c.validate_skill_name(skill)
    if candidate.skill != skill:
        raise ValueError(
            f"receipt candidate is for {candidate.skill!r}, not requested skill {skill!r}")
    deterministic = c.requires_deterministic_gate(skill)
    rec = {
        "schema_version": c.CURRENT_RECEIPT_SCHEMA,
        "skill": skill,
        "source_hash": candidate.source_hash,
        "eval_set_hash": candidate.eval_inputs.eval_set_hash,
        "eval_policy_hash": c.eval_policy_hash(ROOT),
        "providers": providers, "mode": mode, "judge": judge,
        "delta_pass_rate": delta,
        "blind_winner": blind_winner,
        # A deterministic target really runs its certifier below even through the legacy
        # --seed-receipt entry point. Record what happened, not which flag reached it.
        "provenance": ("seeded: blessed current committed state"
                       if seeded and not deterministic else "eval"),
    }
    if summary:
        # WHICH EXECUTOR CARRIED THE DELTA, readable from the receipt alone. The pooled
        # delta_pass_rate above is the gate; this is the diagnosis, and without it a
        # reader of a green receipt cannot tell a uniform win from one provider's gain
        # masking another's regression.
        rec["per_provider"] = {
            p: {"delta": b["delta"].get("pass_rate"), "quantum": b.get("quantum"),
                "n_evals": b.get("n_evals"), "status": b.get("status")}
            for p, b in (summary.get("by_provider") or {}).items()
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
    if deterministic:
        if candidate.gate_command is None:
            raise SystemExit(f"{skill} has no deterministic gate command; not writing receipt")
        with tempfile.TemporaryDirectory(prefix="khenrix-eval-gate-") as gate_td:
            gate_root = _materialize_gate_tree(candidate, Path(gate_td) / "candidate")
            cmd = _rebase_gate_command(candidate.gate_command, ROOT, gate_root)
            rc = subprocess.run(
                cmd, capture_output=(skill != "llm-council"),
                text=(skill != "llm-council"), env=_gate_git_env(skill), cwd=gate_root)
        canonical_command = list(candidate.gate_command)
        rec.update(gate_command=canonical_command,
                   gate_tree_hash=candidate.gate_tree.gate_tree_hash)
        if skill == "llm-council":
            if rc.returncode != 0:
                raise SystemExit("llm-council self-test failed; not writing receipt")
            rec.update(self_test=True, certified_by="fanout --self-test")
        else:
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
            gate_name = c.SELF_TEST_CERTIFIERS[skill]
            rec.update(deterministic_gate=gate_name, self_test=True,
                       certified_by=gate_name, gate_counts=counts)
    receipt_dir = EVALS_ROOT / skill
    _contained(ROOT, receipt_dir, "receipt directory")
    if receipt_dir.is_symlink() or not receipt_dir.is_dir():
        raise SystemExit(f"unsafe or missing receipt directory: {receipt_dir}")
    receipt_path = receipt_dir / "receipt.json"
    _contained(receipt_dir, receipt_path, "receipt path")
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=receipt_dir,
                prefix=".receipt.", suffix=".tmp", delete=False) as tmp:
            tmp.write(json.dumps(rec, indent=2))
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = Path(tmp.name)
        # The certifier may run for minutes. Two identical captures after the complete temp
        # file close the recapture race immediately before publication.
        _assert_candidate_current(candidate)
        os.replace(tmp_path, receipt_path)
        tmp_path = None
        # A mutation triggered at the replacement seam must not leave even a stale receipt.
        _assert_candidate_current(candidate)
        problems = c.validate_receipt(ROOT, skill, final=False)
        if problems:
            raise SystemExit(
                f"published receipt failed its own validator ({'; '.join(problems)}); removing it")
    except BaseException:
        receipt_path.unlink(missing_ok=True)
        raise
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def seed_receipts(args) -> int:
    """Stamp a receipt at the current committed state. With --skill, seed just that one
    (e.g. re-blessing a skill whose only change is mechanical rendering); otherwise seed
    every eval'd skill. Panel-exempt targets still run and record their real certifier."""
    c = _checks()
    skills = [c.validate_skill_name(args.skill)] if args.skill else c._evald_skills(ROOT)
    providers = [provider.strip() for provider in args.providers.split(",")
                 if provider.strip()]
    for skill in skills:
        candidate = capture_candidate(skill, providers, include_bodies=False)
        _write_receipt(skill, providers=providers, mode=args.mode,
                       judge=args.judge, delta=None, seeded=True, candidate=candidate)
        action = ("earned deterministic receipt" if c.requires_deterministic_gate(skill)
                  else "seeded receipt")
        print(f"  {action}: {skill}")
    return 0


def run(args) -> int:
    c = _checks()
    args.skill = c.validate_skill_name(args.skill)
    providers = [p.strip() for p in args.providers.split(",") if p.strip()]
    candidate = capture_candidate(args.skill, providers)
    evals = candidate.eval_inputs.spec["evals"]
    cfg = fanout.resolve_mode_config(_mode_args(args))
    timeout = fanout.effective_timeout(_mode_args(args))
    itdir = EVALS_ROOT / args.skill / "workspace" / f"iteration-{args.iteration}"
    _contained(ROOT, itdir, "eval iteration workspace")
    itdir.mkdir(parents=True, exist_ok=True)
    fixtures_snapshot = _materialize_input_snapshot(
        candidate, itdir / "_candidate-fixtures")
    rendered_bodies = dict(candidate.rendered_bodies)

    all_runs = []
    comparisons = []
    for provider in providers:
        for ev in evals:
            print(f"  · {provider} / eval-{ev['id']}-{ev['name']} …", flush=True)
            runs, cmp = run_eval_for_provider(
                args.skill, provider, ev, args.judge, cfg, itdir,
                timeout=timeout, retries=args.retries, readonly=args.readonly,
                skill_body=rendered_bodies[provider], fixtures_src=fixtures_snapshot)
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
    bw_by_provider = blind_winner_by_provider(comparisons)
    benchmark["blind_winner"] = {"pooled": bw, "by_provider": bw_by_provider}
    (itdir / "benchmark.json").write_text(json.dumps(benchmark, indent=2))
    print(f"  blind A/B winner: {bw}   ({_blind_tally(comparisons)})  [advisory]")
    for p, v in sorted(bw_by_provider.items()):
        print(f"    {p:8} {v}")
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
    elif c.requires_deterministic_gate(args.skill):
        # For the wiki skills the read-only baseline reads the in-repo skill source, so the
        # with-vs-without delta is meaningless; for llm-forge a read-only harness cannot drive
        # a clone fleet at all. Either way the judge run is advisory and the receipt gate is
        # the suite named in DETERMINISTIC_GATE_NAMES (enforced inside _write_receipt).
        gate_ok = True
        bw = "n/a-deterministic"
    policy = c.eval_policy(ROOT)
    canonical = (providers == list(policy.required_providers)
                 and args.judge == policy.judge and args.mode == policy.mode)
    if gate_ok and canonical:  # only the canonical run may refresh a shipping receipt
        models = {p: cfg.get(p, {}).get("model") for p in providers}
        models["judge"] = cfg.get(args.judge, {}).get("model")
        _write_receipt(args.skill, providers=providers, mode=args.mode,
                       judge=args.judge, delta=d, blind_winner=bw, seeded=False,
                       models=models, summary=benchmark["run_summary"],
                       candidate=candidate)
    elif gate_ok:
        print(
            "  advisory-only: result is green, but its providers/mode/judge do not "
            "exactly match capabilities.toml [eval]; receipt left untouched")
    return 0 if gate_ok else 1


def blind_winner_by_provider(comparisons: list) -> dict:
    """Per-executor blind A/B tally. Still ADVISORY — the blind comparison rewards
    concision on a strong executor, which is why it never gates (see run()). Entries
    with no `executor` predate per-provider artifacts and are skipped rather than
    lumped into an arbitrary bucket."""
    out = {}
    for p in sorted({c.get("executor") for c in comparisons
                     if c and c.get("executor")}):
        out[p] = blind_winner([c for c in comparisons if c.get("executor") == p])
    return out


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
    global ROOT, EVALS_ROOT, DETERMINISTIC_GATED, DETERMINISTIC_GATE_NAMES
    results = []

    def check(label, cond, detail=""):
        results.append((label, bool(cond), detail))

    hostile_make_env = {
        "KHENRIX_EVAL_SKILL_RAW": "$(shell touch should-not-run)",
        "KHENRIX_EVAL_PROVIDERS_RAW": "codex,agy; echo nope",
        "KHENRIX_EVAL_MODE_RAW": "normal\n--seed-receipt",
        "KHENRIX_EVAL_TIMEOUT_RAW": "$(value HOME)",
        "KHENRIX_EVAL_RETRIES_RAW": "`id`",
        "KHENRIX_EVAL_MODEL_CLAUDE_RAW": "--option-shaped",
        "KHENRIX_EVAL_MODEL_CODEX_RAW": "$$(escaped)",
        "KHENRIX_EVAL_MODEL_AGY_RAW": '"quoted value"',
        "SKILL": "expanded-skill", "PROVIDERS": "expanded-provider",
        "MODE": "deep", "TIMEOUT": "1", "RETRIES": "99",
        "MODELCLAUDE": "expanded", "MODELCODEX": "expanded", "MODELAGY": "expanded",
        "MAKEFLAGS": "--eval=bad", "MFLAGS": "-e", "MAKEOVERRIDES": "SKILL",
        "KEEP": "yes",
    }
    hostile_argv = _argv_from_make_env(hostile_make_env)
    check("Make adapter preserves hostile values as single --flag=value argv atoms",
          hostile_argv == [
              "--skill=$(shell touch should-not-run)",
              "--providers=codex,agy; echo nope",
              "--mode=normal\n--seed-receipt",
              "--timeout=$(value HOME)",
              "--retries=`id`",
              "--model-claude=--option-shaped",
              "--model-codex=$$(escaped)",
              '--model-agy="quoted value"',
          ], repr(hostile_argv))
    check("Make adapter scrubs originals and recursive Make control state",
          hostile_make_env == {"KEEP": "yes"}, repr(hostile_make_env))
    makefile = (ROOT / "Makefile").read_text()
    check("Make eval recipe contains no user-controlled expansion point",
          "python3 scripts/eval_harness.py --from-make-process=$$PPID" in makefile
          and "$(if $(PROVIDERS)" not in makefile
          and all(f"$(value {name})" not in makefile for name in _MAKE_ORIGINAL_ARGUMENTS))
    try:
        _normalize_entry_argv(["--from-make-process=1", "--self-test"])
        mixed_make_problem = False
    except ValueError:
        mixed_make_problem = True
    check("Make adapter cannot be combined with injected command-line flags",
          mixed_make_problem)

    # Every DETERMINISTIC_GATED skill must have a gate NAME. `_write_receipt` indexes
    # DETERMINISTIC_GATE_NAMES directly — deliberately, since a receipt that cannot say what
    # gated it should not be written — but that KeyError would land at the END of a paid run.
    # This check costs nothing and moves it to `make eval-test`.
    check("every deterministic-gated skill names its gate",
          set(DETERMINISTIC_GATED) == set(DETERMINISTIC_GATE_NAMES),
          str(sorted(set(DETERMINISTIC_GATED) ^ set(DETERMINISTIC_GATE_NAMES))))
    expected_self_test_certifiers = {
        "llm-council": "fanout --self-test",
        **DETERMINISTIC_GATE_NAMES,
    }
    check("receipt verifier and producer agree on every panel-exempt certifier",
          _checks().SELF_TEST_CERTIFIERS == expected_self_test_certifiers,
          str({"producer": expected_self_test_certifiers,
               "verifier": _checks().SELF_TEST_CERTIFIERS}))
    forge_command = _checks().deterministic_gate_command(ROOT, "llm-forge")
    forge_inputs = [arg for arg in forge_command if arg.endswith(".py")]
    expected_forge_inputs = [
        str(path.relative_to(ROOT))
        for pattern in _checks().DETERMINISTIC_GATE_INPUT_GLOBS["llm-forge"]
        for path in sorted(ROOT.glob(pattern))
    ]
    check("forge producer executes every source-closure certifier input exactly once",
          forge_inputs == expected_forge_inputs,
          str({"producer": forge_inputs, "closure": expected_forge_inputs}))
    check("forge certifier pins pytest and disables repository pytest configuration",
          "pytest==9.1.1" in forge_command
          and forge_command[forge_command.index("-c") + 1] == os.devnull
          and forge_command[forge_command.index("--rootdir") + 1] == ".")
    check("Makefile pytest gates share the pinned ambient-free collection contract",
          "pytest==9.1.1" in makefile
          and "env -u PYTEST_ADDOPTS -u PYTEST_PLUGINS" in makefile
          and "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1" in makefile
          and "pytest -q -c /dev/null --rootdir $(REPO)" in makefile)
    saved_addopts = os.environ.get("PYTEST_ADDOPTS")
    saved_plugins = os.environ.get("PYTEST_PLUGINS")
    os.environ["PYTEST_ADDOPTS"] = "-m not_slow"
    os.environ["PYTEST_PLUGINS"] = "outside_plugin"
    try:
        forge_env = _deterministic_gate_env("llm-forge")
    finally:
        if saved_addopts is None:
            os.environ.pop("PYTEST_ADDOPTS", None)
        else:
            os.environ["PYTEST_ADDOPTS"] = saved_addopts
        if saved_plugins is None:
            os.environ.pop("PYTEST_PLUGINS", None)
        else:
            os.environ["PYTEST_PLUGINS"] = saved_plugins
    check("forge certifier ignores unhashable ambient pytest selectors",
          forge_env is not None
          and "PYTEST_ADDOPTS" not in forge_env
          and "PYTEST_PLUGINS" not in forge_env
          and forge_env.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD") == "1")
    narrowed = _pytest_counts("1074 passed, 2 deselected in 1.0s")
    check("a deselected deterministic test makes counts non-certifying",
          narrowed["skipped"] == 2 and not _counts_are_evidence(narrowed))
    xresults = _pytest_counts("1074 passed, 1 xfailed, 1 xpassed in 1.0s")
    check("pytest expected and unexpected outcomes make counts non-certifying",
          xresults == {"tests_run": 1074, "skipped": 2, "failed": 0}
          and not _counts_are_evidence(xresults))

    # Producer-to-validator round trip for the legacy seed entry point. A deterministic
    # target really runs its suite there, so the receipt must say `eval`, not claim it was
    # merely blessed. Exercise the writer, not a hand-assembled approximation of its shape.
    _saved_root, _saved_evals = ROOT, EVALS_ROOT
    _saved_run = subprocess.run
    _receipt_checks = _checks()
    _roundtrip_ok = False
    _roundtrip_detail = ""
    try:
        with tempfile.TemporaryDirectory() as _td:
            ROOT = Path(_td)
            EVALS_ROOT = ROOT / "evals"
            (ROOT / "capabilities.toml").write_text(
                "[models]\n[eval]\nrequired_providers=['codex','agy']\n"
                "judge='codex'\nmode='normal'\n")
            for _skill in ("khenrix-wiki-add", "llm-council"):
                (ROOT / "shared" / "skills" / _skill).mkdir(parents=True)
                (ROOT / "shared" / "skills" / _skill / "SKILL.md").write_text(
                    f"# {_skill}\n")
                (EVALS_ROOT / _skill).mkdir(parents=True)
                (EVALS_ROOT / _skill / "evals.json").write_text(
                    '{"evals":[{"id":0,"name":"case","prompt":"p",'
                    '"assertions":["a"],"files":[]}]}')
            _REAL_SUBPROCESS_RUN(
                ["git", "init", "-q", str(ROOT)], check=True, capture_output=True)

            class _Completed:
                returncode = 0
                stdout = "1 passed\n"
                stderr = ""

            subprocess.run = lambda *a, **kw: _Completed()
            _wiki_candidate = capture_candidate(
                "khenrix-wiki-add", ["codex", "agy"], include_bodies=False)
            _write_receipt(
                "khenrix-wiki-add", providers=["codex", "agy"], mode="normal", judge="codex",
                delta=None, seeded=True, candidate=_wiki_candidate)
            _receipt = json.loads(
                (EVALS_ROOT / "khenrix-wiki-add" / "receipt.json").read_text())
            _roundtrip_problems = _receipt_checks.validate_receipt(
                ROOT, "khenrix-wiki-add", final=True, panel=["codex", "agy"])
            _council_candidate = capture_candidate(
                "llm-council", ["codex", "agy"], include_bodies=False)
            _write_receipt(
                "llm-council", providers=["codex", "agy"], mode="normal", judge="codex",
                delta=None, seeded=True, candidate=_council_candidate)
            _council_receipt = json.loads(
                (EVALS_ROOT / "llm-council" / "receipt.json").read_text())
            _council_problems = _receipt_checks.validate_receipt(
                ROOT, "llm-council", final=True, panel=["codex", "agy"])
            _roundtrip_ok = (
                _receipt.get("provenance") == "eval" and bool(_roundtrip_problems)
                and _council_receipt.get("provenance") == "eval"
                and _council_receipt.get("self_test") is True
                and _council_receipt.get("certified_by") == "fanout --self-test"
                and "synthesis_review" not in _council_receipt and bool(_council_problems)
                and all("evidence" in problem or "model provenance" in problem
                        or "per_provider" in problem
                        for problem in _roundtrip_problems + _council_problems))
            _roundtrip_detail = str(_roundtrip_problems + _council_problems)
    finally:
        ROOT, EVALS_ROOT = _saved_root, _saved_evals
        subprocess.run = _saved_run
    check("deterministic seeding retains its suite but cannot fabricate panel evidence",
          _roundtrip_ok, _roundtrip_detail)

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

    # per-provider blind tally
    cmps = [{"winner_condition": "with_skill", "winner_slot": "A", "executor": "claude"},
            {"winner_condition": "with_skill", "winner_slot": "B", "executor": "claude"},
            {"winner_condition": "without_skill", "winner_slot": "A", "executor": "codex"},
            {"winner_condition": "tie", "winner_slot": "B", "executor": "codex"}]
    bp = blind_winner_by_provider(cmps)
    check("blind tally splits per provider",
          bp == {"claude": "with_skill", "codex": "without_skill"})
    check("blind_winner_by_provider ignores entries with no executor",
          blind_winner_by_provider([{"winner_condition": "with_skill"}]) == {})

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
        itdir = tdp / "iteration"
        itdir.mkdir()
        empty_wd = itdir / "eval-empty" / "claude__with_skill"
        empty_dest = empty_wd / "fixtures"
        empty_dest.mkdir(parents=True)
        (empty_dest / "stale.json").write_text("stale")
        _prepare_condition_workspace(itdir, empty_wd)
        materialize_fixtures(
            {"prompt": "x", "files": []},
            src_dir=tdp / "missing-fixtures", dest=empty_dest)
        check("no-files eval needs no source and clears prior fixtures",
              empty_dest.exists() and list(empty_dest.iterdir()) == [])

        # A composite `<id>-<name>` is not injective: these two safe pairs used to share
        # one workspace, so the no-files case inherited the first eval's fixture.
        left = {"id": "alpha-beta", "name": "gamma", "files": ["bm.json"]}
        right = {"id": "alpha", "name": "beta-gamma", "files": []}
        left_base = _eval_workspace_base(left, itdir)
        right_base = _eval_workspace_base(right, itdir)
        left_wd = left_base / "claude__with_skill"
        right_wd = right_base / "claude__with_skill"
        _prepare_condition_workspace(itdir, left_wd)
        _prepare_condition_workspace(itdir, right_wd)
        materialize_fixtures(left, src, left_wd / "fixtures")
        materialize_fixtures(right, tdp / "missing-fixtures", right_wd / "fixtures")
        check("distinct safe eval IDs cannot collide or leak fixtures",
              left_base != right_base
              and (left_wd / "fixtures" / "bm.json").is_file()
              and list((right_wd / "fixtures").iterdir()) == [])

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
    ap.add_argument("--providers",
                    help="executors to run (default: canonical capabilities [eval] panel)")
    ap.add_argument("--judge", help="grading/comparison provider (default: eval policy)")
    ap.add_argument("--mode", choices=list(fanout.MODES),
                    help="thinking mode (default: canonical capabilities [eval] mode)")
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
                    help="stamp ordinary receipts; panel-exempt skills still run their certifier")
    try:
        argv = _normalize_entry_argv(argv)
    except ValueError as exc:
        ap.error(str(exc))
    return ap.parse_args(argv)


def _apply_policy_defaults(args):
    policy = _checks().eval_policy(ROOT)
    if args.providers is None:
        args.providers = ",".join(policy.required_providers)
    if args.judge is None:
        args.judge = policy.judge
    if args.mode is None:
        args.mode = policy.mode
    return args


def main(argv=None) -> int:
    args = _apply_policy_defaults(parse_args(argv))
    if args.self_test:
        return self_test()
    if args.seed_receipt:
        return seed_receipts(args)
    if not args.skill:
        sys.exit("--skill is required (or use --self-test / --seed-receipt)")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
