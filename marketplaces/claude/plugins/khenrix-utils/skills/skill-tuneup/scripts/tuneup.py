#!/usr/bin/env python3
"""Deterministic substrate for the skill-tuneup skill (stdlib only).

Subcommands keep the judgment-free parts of a tune-up reproducible. Run `--help` for the
current list rather than trusting an enumeration here — this header listed four of nine
for three runs, because a comment cannot fail a test when it goes stale.

  tuneup.py --self-test                hermetic logic tests, no repo/git/network needed

Judgment-shaped work (research, audit, proportionality) lives in SKILL.md and
references/ — this script only reports facts. Run memory lives in
docs/tuneups/log/<target>.jsonl (committed; outside every eval-receipt closure).
"""
from __future__ import annotations
import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Generation-agnostic model-ID shapes (never encodes a "latest", so it can't rot).
MODEL_RX = re.compile(
    r"(claude-[a-z]+-[0-9][0-9a-z.-]*"      # claude-opus-4-8, claude-fable-5, claude-haiku-4-5-20251001
    r"|gpt-[0-9][0-9a-z.+-]*"               # gpt-5.5, gpt-4o
    r"|\bo[0-9]-[a-z][a-z0-9-]*"            # o4-mini, o3-pro
    r"|gemini-[0-9][0-9a-z.-]*)"            # gemini-3.5-flash, gemini-2.5-pro
)
# Commit subjects that do NOT count as a substantive baseline.
CHORE_RX = re.compile(r"^(chore|docs|style|typo)[:(\s]", re.IGNORECASE)
SCAN_SUFFIXES = (".md", ".py", ".toml", ".json", ".sh", ".tmpl", ".txt")
# Generated / fixture / workspace paths never count as staleness evidence,
# and this script's own self-test fixtures would flag themselves.
EXCLUDE_RX = re.compile(r"(^|/)(marketplaces/|__pycache__/|workspace/|evals/_fixtures/)"
                        r"|skills/skill-tuneup/scripts/tuneup\.py/?$")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True).stdout


# Where a skill's source can live. khenrix-utils layouts first (a khenrix skill must never
# be matched by a generic layout), then the two conventions foreign repos use. This is the
# ONLY place layout is encoded — baseline/stale-models resolve through it, so teaching it a
# new layout teaches the whole engine.
KHENRIX_LAYOUTS = ("shared/skills/{s}", "shared/skill-templates/{s}")
FOREIGN_LAYOUTS = (".claude/skills/{s}", "skills/{s}")


def skill_paths(repo: Path, skill: str) -> list[Path]:
    """Source-of-truth dirs for a skill.

    Layouts are selected by REPO KIND, not tried in order: a khenrix checkout uses only
    khenrix layouts, any other repo only the foreign ones. Mixing them would let a stray
    `.claude/skills/<n>` copy inside khenrix-utils be resolved and edited alongside the real
    source. A skill matching two layouts in the same tier is ambiguous — return both so the
    caller can refuse rather than silently pick one.
    """
    pats = KHENRIX_LAYOUTS if is_khenrix_repo(repo) else FOREIGN_LAYOUTS
    return [p for p in (repo / pat.format(s=skill) for pat in pats) if p.is_dir()]


def is_khenrix_repo(repo: Path) -> bool:
    """The KHENRIX gate (evals, receipts, render.py, precommit) only exists here."""
    return (repo / "capabilities.toml").is_file() and (repo / "shared" / "skills").is_dir()


def target_info(repo: Path, skill: str) -> dict:
    """Resolve a target and say which gate tier applies — the two-tier contract in one place.

    The tier follows the LAYOUT, not a probe for gate files: outside khenrix-utils the
    khenrix receipt gate is inapplicable by construction, since a receipt is only meaningful
    against this repo's eval harness. That is a claim about the khenrix gate and NOT about
    the target repo, which may well have tests or a precommit hook of its own — run those,
    they simply cannot produce a receipt. Report the tier so the run states plainly that it
    shipped without one.
    """
    paths = skill_paths(repo, skill)
    khenrix = is_khenrix_repo(repo)
    full_gate = bool(khenrix and any(
        str(p.relative_to(repo)).startswith(("shared/skills", "shared/skill-templates"))
        for p in paths))
    # Two same-tier matches (e.g. .claude/skills/x AND skills/x) is ambiguous: baseline,
    # scanning and editing would each silently pick one. Refused at the resolver the run
    # consults FIRST — not centrally. ALL FOUR other callers of skill_paths() (baseline,
    # scan_stale_models, and both triage helpers) still union the matches if invoked
    # directly, so the guarantee is "the documented flow fails safe", not "the ambiguity
    # is unrepresentable".
    ambiguous = len(paths) > 1
    return {
        "ambiguous": ambiguous,
        "repo": str(repo),
        "repo_name": repo.name,
        "skill": skill,
        "paths": [str(p.relative_to(repo)) for p in paths],
        "found": bool(paths),
        "khenrix_repo": khenrix,
        "tier": "full-gate" if full_gate else "council-only",
        "gate": ("evals + receipt + make precommit"
                 if full_gate else
                 "research + both council reviews + audit + convergence — NO khenrix "
                 "receipt (run any gate the target repo has of its own, and report it "
                 "separately); say so plainly in the run's output"),
        "log_target": log_target_key(repo, skill),
    }


def pick_baseline(commits: list[dict]) -> dict | None:
    """Newest commit whose subject isn't a chore/docs/style tweak (commits newest-first).
    Falls back to the newest commit at all if every subject looks like a chore."""
    for c in commits:
        if not CHORE_RX.match(c["subject"]):
            return c
    return commits[0] if commits else None


def baseline(repo: Path, skill: str) -> dict | None:
    paths = skill_paths(repo, skill)
    if not paths:
        looked = ", ".join(p.format(s=skill) for p in KHENRIX_LAYOUTS + FOREIGN_LAYOUTS)
        raise FileNotFoundError(f"no such skill: {skill} (looked in {looked})")
    fmt = "--format=%H%x00%aI%x00%s"
    lines = []
    lines += _git(repo, "log", "--no-merges", fmt, "--",
                  *[str(p.relative_to(repo)) for p in paths]).splitlines()
    if (repo / "shared" / "skill-templates" / skill).is_dir():
        # templated skills also live in capabilities.toml [skill_facts.<s>.*]
        lines += _git(repo, "log", "--no-merges", fmt,
                      "-G", rf"skill_facts\.{re.escape(skill)}", "--",
                      "capabilities.toml").splitlines()
    commits, seen = [], set()
    for ln in lines:
        sha, date, subject = ln.split("\0", 2)
        if sha not in seen:
            seen.add(sha)
            commits.append({"sha": sha, "date": date, "subject": subject})
    commits.sort(key=lambda c: c["date"], reverse=True)
    picked = pick_baseline(commits)
    if picked:
        picked = {**picked, "skipped_as_chore": sum(1 for c in commits
                                                    if c["date"] > picked["date"])}
    return picked


def _slug(label: str) -> str:
    """Display label -> id shape: 'Gemini 3.5 Flash (High)' -> 'gemini-3.5-flash'."""
    return re.sub(r"\s+", "-", re.sub(r"\s*\(.*?\)", "", label).strip().lower())


def registry_repo(repo: Path) -> Path:
    """The khenrix-utils checkout: model registry + run-log home, whichever repo is TARGET.

    Derived from where THIS FILE lives rather than $HOME/git/khenrix-utils — the engine is
    always run out of the checkout, so this is correct on any machine and can't silently
    bind to a second, stale clone at the conventional path.

    Raises rather than falling back to the target repo: a foreign repo has no
    capabilities.toml, so `approved_models` would come back empty and `tag_model` would
    degrade every hit to "found" — silently disabling the staleness check in exactly the
    situation nobody is watching it.
    """
    if is_khenrix_repo(repo):
        return repo
    here = Path(__file__).resolve()
    for cand in here.parents:  # .../shared/skills/skill-tuneup/scripts/tuneup.py
        if is_khenrix_repo(cand):
            return cand
    raise FileNotFoundError(
        "cannot locate the khenrix-utils checkout from "
        f"{here} — it holds the approved-model registry and the run log. "
        "Run tuneup.py from the checkout (not a copied file).")


def approved_models(repo: Path, extra_csv: str = "") -> set[str]:
    """Approved set = every string in capabilities.toml [models] lists + --approved extras.
    Entries are also slugged, since agy's entry is a display label, not an id."""
    import tomllib
    ids: set[str] = set()
    caps_path = registry_repo(repo) / "capabilities.toml"
    if caps_path.is_file():
        with open(caps_path, "rb") as f:
            caps = tomllib.load(f)
        for v in caps.get("models", {}).values():
            if isinstance(v, list):
                for x in v:
                    ids.update((x.lower(), _slug(x)))
    ids.update(x.strip().lower() for x in extra_csv.split(",") if x.strip())
    return ids


def tag_model(mid: str, approved: set[str]) -> str:
    """current if the id equals an approved id or is a dated variant of one
    (claude-haiku-4-5-20251001 startswith claude-haiku-4-5 + '-')."""
    if not approved:
        return "found"
    low = mid.lower()
    if low in approved or any(low.startswith(a + "-") for a in approved):
        return "current"
    return "stale-candidate"


def _facts_lines(caps_text: str, skill: str) -> list[tuple[int, str]]:
    """(lineno, line) pairs inside [skill_facts.<skill>...] sections of capabilities.toml."""
    out, active = [], False
    for i, line in enumerate(caps_text.splitlines(), 1):
        m = re.match(r"\s*\[+([^\]]+)\]+", line)
        if m:
            active = m.group(1).startswith(f"skill_facts.{skill}")
        elif active:
            out.append((i, line))
    return out


def scan_stale_models(repo: Path, skill: str | None, approved: set[str]) -> list[dict]:
    hits = []
    if skill:
        roots = skill_paths(repo, skill)
        if not roots:
            raise FileNotFoundError(f"no such skill: {skill}")
    else:
        roots = [repo / "shared", repo / "capabilities.toml", repo / "docs"]
    for root in roots:
        files = [root] if root.is_file() else sorted(root.rglob("*"))
        for p in files:
            rel = str(p.relative_to(repo))
            if not p.is_file() or p.suffix not in SCAN_SUFFIXES or EXCLUDE_RX.search(rel + "/"):
                continue
            for i, line in enumerate(p.read_text(errors="ignore").splitlines(), 1):
                for m in MODEL_RX.finditer(line):
                    hits.append({"file": rel, "line": i, "id": m.group(0),
                                 "status": tag_model(m.group(0), approved)})
    if skill and (repo / "shared" / "skill-templates" / skill).is_dir():
        caps = repo / "capabilities.toml"
        if caps.is_file():
            for i, line in _facts_lines(caps.read_text(errors="ignore"), skill):
                for m in MODEL_RX.finditer(line):
                    hits.append({"file": "capabilities.toml", "line": i, "id": m.group(0),
                                 "status": tag_model(m.group(0), approved)})
    return hits


# --------------------------------------------------------------------------- #
# Triage — rank all skills by staleness. Read-only by construction.
# --------------------------------------------------------------------------- #
def receipt_state(repo: Path, skill: str) -> str:
    """fresh | stale-source | stale-evalset | missing | no-evals | unknown."""
    if not (repo / "evals" / skill / "evals.json").exists():
        return "no-evals"
    rp = repo / "evals" / skill / "receipt.json"
    if not rp.exists():
        return "missing"
    try:
        sys.path.insert(0, str(repo / "scripts" / "lib"))
        import checks  # noqa: PLC0415
        rec = json.loads(rp.read_text())
        if rec.get("source_hash") != checks.source_hash(repo, skill):
            return "stale-source"
        if rec.get("eval_set_hash") != checks.eval_set_hash(repo, skill):
            return "stale-evalset"
        return "fresh"
    except Exception:  # noqa: BLE001 — plugin copy has no scripts/lib; degrade
        return "unknown"


RECEIPT_SCORE = {"no-evals": 40, "missing": 30, "stale-source": 20,
                 "stale-evalset": 20, "unknown": 5, "fresh": 0}


def triage_score(receipt: str, age_days: float | None, stale_hits: int, md_lines: int) -> int:
    score = RECEIPT_SCORE.get(receipt, 5)
    score += min(stale_hits * 10, 30)
    if age_days is not None:
        score += min(int(age_days / 30) * 2, 24)   # ~2 pts per month unmaintained, cap 24
    # An UNKNOWN age deliberately scores NOTHING. It is missing evidence, not staleness:
    # awarding points made a git failure outrank 70 days of real neglect and, because every
    # row got the same bonus, turned the board into an alphabetical tiebreak that
    # triage_recommendation then reported as a winner. The all-unknown case is a DIAGNOSIS,
    # not a ranking — triage_recommendation says so instead.
    if md_lines > 450:
        score += 10                                # near the 500-line hard cap
    return score


def triage_recommendation(rows: list[dict]) -> str:
    """The one-line verdict under the triage table.

    A recommendation needs a SIGNAL, not just a first row. Rows sort by (-score, skill),
    so once every score is 0 the top is whichever skill sorts first ALPHABETICALLY — and a
    tool whose entire product is "which skill needs work" would recommend a multi-hour run
    on the exact evidence that nothing needs work. Reachable as soon as the last scoring
    skill drops off the board.
    """
    if not rows:
        return "no skills found."
    # Missing AGE is not missing EVIDENCE. Most of the score (receipt state, stale model
    # ids, the line budget) never touches git, so a board whose ages all failed to resolve
    # can still hold a decisive signal — suppressing the recommendation there withheld an
    # answer the tool had good grounds for. The age gap becomes a NOTE on the answer, and
    # only an all-unknown board with nothing else to say degrades to the bare diagnosis.
    # `"age_days" in r`, not `.get(...) is None`: an ABSENT key is a caller that did not
    # report an age, not a checkout whose age is unknown. triage() always sets the key.
    unknown = [r for r in rows if "age_days" in r and r["age_days"] is None]
    if rows[0]["score"] > 0:
        note = ""
        if len(unknown) == len(rows):
            note = ("  (note: baseline age is UNKNOWN for every skill — git failed, so the "
                    "age component of the ranking is missing; the rest of the score stands)")
        elif unknown:
            note = f"  (note: baseline age is unknown for {len(unknown)} of {len(rows)} skills)"
        return f"recommend: deep tune-up of '{rows[0]['skill']}' first{note}"
    if len(unknown) == len(rows):
        return ("baseline age is UNKNOWN for every skill — git failed or this is not a "
                "checkout with history. No other signal fired either, but the ranking is "
                "incomplete: fix that before concluding there is nothing to do.")
    if unknown:
        # No signal fired, but some evidence never arrived — an all-clear would overclaim.
        return (f"no staleness signal fired, but baseline age is unknown for "
                f"{len(unknown)} of {len(rows)} skills — the all-clear is INCOMPLETE.")
    return "no skill shows a staleness signal — nothing to tune up."


def triage(repo: Path) -> list[dict]:
    # Step 3 documents triage for "a target repo", but the ranking only knows khenrix
    # layouts — on a foreign repo it produced an EMPTY table, which reads as "nothing to
    # tune" rather than "I cannot rank this". Refuse instead of answering wrongly.
    if not is_khenrix_repo(repo):
        raise ValueError(
            f"triage ranks khenrix-utils skills only; {repo} is not that checkout. "
            "For a skill in another repo use `target-info --skill <name>` to resolve its "
            "tier, then run the deep pass directly.")
    # A set, not concatenation: a name present in BOTH source dirs is one skill with two
    # layouts (skill_paths already treats that as ambiguous), not two rows on the board.
    names = {p.name for p in (repo / "shared" / "skills").glob("*/") if p.is_dir()}
    names |= {p.name for p in (repo / "shared" / "skill-templates").glob("*/") if p.is_dir()}
    skills = sorted(names)
    approved = approved_models(repo)
    now = datetime.now(timezone.utc)
    rows = []
    for s in skills:
        try:
            b = baseline(repo, s)
        except (subprocess.CalledProcessError, FileNotFoundError, OSError):
            b = None
        age = (now - datetime.fromisoformat(b["date"])).days if b else None
        stale = sum(1 for h in scan_stale_models(repo, s, approved)
                    if h["status"] == "stale-candidate")
        md = next((p / f for p in skill_paths(repo, s)
                   for f in ("SKILL.md", "SKILL.md.tmpl") if (p / f).is_file()), None)
        lines = len(md.read_text(errors="ignore").splitlines()) if md else 0
        receipt = receipt_state(repo, s)
        rows.append({"skill": s, "score": triage_score(receipt, age, stale, lines),
                     "receipt": receipt, "age_days": age, "stale_model_hits": stale,
                     "skill_md_lines": lines,
                     "baseline": (b or {}).get("sha", "")[:9] or None})
    rows.sort(key=lambda r: (-r["score"], r["skill"]))
    return rows


# --------------------------------------------------------------------------- #
# Run memory — docs/tuneups/log/<target>.jsonl (committed, append-only).
# --------------------------------------------------------------------------- #
REQUIRED_LOG_KEYS = {"target", "finding_id", "decision"}
DECISIONS = {"applied", "rejected", "deferred"}


# Machine-global and OUT OF TREE. Not TMPDIR (per-process on many setups, so two runs would
# each make their own "mutex" and never see each other), and not beside the engine either:
# `_skill_source_files` rglobs the skill dir and pathlib's rglob matches dotfiles, so an
# in-tree lock puts a random per-run token into skill-tuneup's own source_hash — `make
# precommit` would then fail the receipt check on every run, at the ship step, while the
# lock is held. It is also untracked, so `git add -A` would commit it and render.py would
# copy it into all three marketplaces.
LOCK_DIR = Path.home() / ".cache" / "khenrix-utils" / "skill-tuneup.lock.d"
# Must strictly exceed the longest step a run can take BETWEEN REFRESHES, or a LIVE run's
# lock becomes stealable. It does NOT cover Step 7's CHECKPOINT: that is a human wait, so
# it is unbounded and no window can. Step 7 therefore refreshes immediately before
# presenting the checkpoint AND immediately on resume, which is what turns an unbounded
# wait back into a bounded gap. `lock status` samples the age without acquiring.
# RAISED 90 -> 135 ON 2026-08-15, because the window had silently stopped satisfying the
# invariant in the first line of this comment. MODE_TIMEOUT["deep"] is 1800s (it was 1200
# when 90 was chosen; the council engine recalibrated it 2026-08-13 after six real deep
# councils measured 753-1238s), and --retries defaults to 2, so ONE fan-out is now
# 3 x 1800 = 90 min plus backoff — i.e. exactly the old window, with no margin at all. A
# legitimate deep run could therefore have its own lock stolen mid-fan-out. 135 restores
# the original ~1.5x margin (90 was 1.5x the then-longest 60 min step), derived rather
# than rounded so the next MODE_TIMEOUT change can redo the arithmetic. Step 6's own
# "deep + retries 1" guidance for tuning this machinery is 60 min.
# The eval run is LONGER than this window — eval_harness iterates cases
# serially across providers and both conditions — and is deliberately not covered by it:
# Step 6 mandates backgrounding the run and Step 1 mandates a `lock refresh` between polls,
# so an eval is an attended step whose lock never goes 135 min untouched. Kept a constant rather than derived: lock_acquire runs in a DIFFERENT
# process with no knowledge of the holder's --timeout/--retries, so deriving it would mean
# writing a deadline into the lock — a new refresh contract, and a crashed deep run
# blocking the next for an hour. The two stay linked by reading this comment.
LOCK_STALE_MIN = 135


def lock_acquire(stale_min: int = LOCK_STALE_MIN) -> tuple[bool, str]:
    """mkdir-based mutex carrying an ownership token.

    The token is what makes a steal *detectable*: `touch -c` on a lock another run already
    removed silently succeeds, so the previous heartbeat could not distinguish "still mine"
    from "gone and re-taken". refresh() compares the token before bumping the mtime.
    """
    tok = LOCK_DIR / "owner"
    if LOCK_DIR.is_dir():
        age_min = (time.time() - LOCK_DIR.stat().st_mtime) / 60
        if age_min <= stale_min:
            held = tok.read_text().strip() if tok.is_file() else "unknown"
            return False, f"held by {held} ({age_min:.0f} min old)"
        shutil.rmtree(LOCK_DIR, ignore_errors=True)  # stale: a crashed run
    try:
        LOCK_DIR.parent.mkdir(parents=True, exist_ok=True)
        LOCK_DIR.mkdir(parents=False, exist_ok=False)
    except FileExistsError:
        return False, "raced with another run"
    owner = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    tok.write_text(owner + "\n")
    return True, owner


def _norm_owner(owner: str) -> str:
    """Accept the token in the shape `lock acquire` PRINTS it, not just the internal one.

    acquire emits `OWNER=<token>` — a KEY=VALUE line an operator naturally copies whole,
    especially since shell state does not survive between the orchestrator's Bash calls.
    Comparing that literal against the stored bare token made refresh report
    "lock was STOLEN — STOP" to a run that still held its own lock, and made release
    refuse, leaking the lock until it aged out. A false steal alarm is worse than a
    missed one: it aborts correct work.
    """
    return owner.strip().removeprefix("OWNER=").strip()


def lock_refresh(owner: str) -> tuple[bool, str]:
    owner = _norm_owner(owner)
    tok = LOCK_DIR / "owner"
    if not tok.is_file():
        return False, "lock is GONE — another run removed it"
    cur = tok.read_text().strip()
    if cur != owner:
        return False, f"lock was STOLEN — now held by {cur}"
    os.utime(LOCK_DIR, None)
    return True, owner


def lock_status() -> dict:
    """Read-only view of the lock. NEVER acquires, never steals, never writes.

    It exists because sampling the age used to require `lock acquire` — the one command
    that REMOVES a lock older than the stale window. So the documented way to diagnose
    "is the holder alive?" was also the way to destroy it, and past 135 minutes the
    diagnostic *was* the theft. A question must not be answerable only by an action.
    """
    if not LOCK_DIR.is_dir():
        return {"held": False}
    tok = LOCK_DIR / "owner"
    return {
        "held": True,
        "owner": tok.read_text().strip() if tok.is_file() else None,
        "age_min": round((time.time() - LOCK_DIR.stat().st_mtime) / 60, 1),
        "stale_after_min": LOCK_STALE_MIN,
    }


def lock_release(owner: str) -> tuple[bool, str]:
    owner = _norm_owner(owner)
    tok = LOCK_DIR / "owner"
    if tok.is_file() and tok.read_text().strip() != owner:
        return False, "not the owner — refusing to release someone else's lock"
    shutil.rmtree(LOCK_DIR, ignore_errors=True)
    return True, "released"


def log_path(repo: Path, target: str) -> Path:
    """Run memory always lands in the khenrix-utils checkout, never the target repo.

    `repo` is the TARGET's repo; resolving through registry_repo() is what makes that true.
    Writing under the target instead would create docs/tuneups/log/ inside a foreign repo,
    where `git add -A` would sweep it into that project's commit — and the next run, reading
    from khenrix-utils, would see no history and re-propose everything already decided.
    """
    return registry_repo(repo) / "docs" / "tuneups" / "log" / f"{target}.jsonl"


def log_target_key(repo: Path, skill: str) -> str:
    """Foreign targets are keyed <repo-name>@<hash>:<skill>.

    The basename alone collides (~/git/foo and ~/work/foo are different repos with the same
    name, and a run log that merges them would silently apply one project's decisions to
    another). The short hash of the canonical repo root disambiguates; the readable name is
    kept so the file is still greppable by a human.
    """
    if is_khenrix_repo(repo):
        return skill
    root = str(repo.resolve())
    h = hashlib.sha256(root.encode()).hexdigest()[:8]
    return f"{repo.resolve().name}@{h}:{skill}"


# Severity decides when a run STOPS, so the bar has to be objective enough that a tiring
# operator can't quietly relabel a defect to end the loop. Tests, not adjectives:
SEVERITIES = ("blocking", "serious", "minor")
SEVERITY_TESTS = {
    "blocking": "produces a wrong result, makes a gate pass/fail incorrectly, loses data, "
                "exposes a secret, or documents behaviour the code does not have",
    "serious":  "a real edge case that CAN fire in normal use, or an eval gap that would "
                "hide a genuine regression — bounded, but a correctness defect",
    "minor":    "polish, naming, hardening for a condition never observed, preference",
}
STALL_LIMIT = 2  # consecutive non-decreasing cycles => the loop is not converging


CYCLE_END = "cycle-end"      # one per CYCLE, written after that cycle's council review
RUN_START = "run-start"      # one per RUN, written at Step 1
RUN_END = "run-convergence"  # one per RUN — the run's outcome, NOT a cycle boundary


def cycle_severity_counts(entries: list) -> tuple[list[int], bool, list[str]]:
    """(per-cycle blocking+serious counts for the CURRENT run, tail_open, warnings).

    Scoped to the newest `run-start`: a previous run's history must not let a fresh run
    inherit its convergence or stall state. Cycles are delimited by `cycle-end` — NOT by
    `run-convergence`, which is written once per run (every historical entry carries
    `cycles: 3`), so counting on it silently measured runs and called them cycles.

    `tail_open` is True when applied findings sit after the last `cycle-end`: the cycle is
    still in flight and MUST NOT be read as a completed zero-serious cycle. An unsevered
    applied finding counts as serious so an omitted tag can never end the run.
    """
    start = max((i for i, e in enumerate(entries)
                 if e.get("finding_id") == RUN_START), default=None)
    if start is None:
        # FAIL CLOSED. Falling back to the whole log would scope a fresh run to every prior
        # run's history — exactly what this scoping exists to prevent — and it was the one
        # path in this engine that failed open.
        raise ValueError(
            f"no {RUN_START!r} marker in the log — write it at Step 1, before any finding. "
            "Without it the run's cycles cannot be isolated from earlier runs.")
    # Findings before the newest run-start are ADVISORY, not fatal. A backfilled marker
    # looks identical to two legitimate states — closing out a deferred finding between
    # runs (this log's established practice) and a run that died before writing its
    # run-convergence — so raising permanently locked the target for every later run, and
    # misdiagnosed it as "you wrote run-start late" when the operator had not. The hard
    # guarantees that DO hold are elsewhere: a missing run-start refuses, an unnumbered or
    # duplicate cycle-end refuses, and an open tail can never converge.
    prev_end = max((i for i, e in enumerate(entries[:start])
                    if e.get("finding_id") == RUN_END), default=-1)
    prev_start = max((i for i, e in enumerate(entries[:start])
                      if e.get("finding_id") == RUN_START), default=-1)
    warnings = []
    orphans = [e.get("finding_id") for e in entries[prev_end + 1:start]
               if e.get("decision") == "applied"
               and e.get("finding_id") not in (RUN_END, RUN_START, CYCLE_END)]
    # Ambiguous when the previous run never CLOSED — not merely when no run ever closed.
    # `prev_end == -1` scoped this to a target's FIRST-EVER run: any target with one
    # completed run behind it was permanently unguarded, so a SECOND run-start written
    # inside one run (the natural resume point — Step 1 re-acquires the lock) silently
    # dropped that run's findings and reported `converged` with zero warnings. Comparing
    # the two markers keeps legitimate post-run bookkeeping quiet (prev_end > prev_start)
    # while catching the re-opened run (prev_end <= prev_start).
    if orphans and prev_end <= prev_start:
        warnings.append(
            f"{len(orphans)} applied finding(s) sit between the last {RUN_END!r} and this "
            f"{RUN_START!r} ({orphans[:3]}…). Expected if they are inter-run bookkeeping or "
            "a run that ended without its marker; if they belong to THIS run, run-start was "
            "written late and they are not being counted.")
    cycles, cur, seen_cycles = [], [], []
    for i, e in enumerate(entries[start + 1:]):
        fid = e.get("finding_id")
        if fid == RUN_END:
            # Terminal: this run is complete. Anything after belongs to no run, so stop
            # rather than letting a later run that forgot its own run-start silently
            # inherit this run's cycle history and stall state. But do NOT drop it
            # silently: real work appended after a completed run means the log describes
            # two things at once, and reporting the finished run's verdict would report a
            # stale "converged" over unaccounted findings.
            rest = [x.get("finding_id") for x in entries[start + 1:][i + 1:]
                    if x.get("decision") == "applied"
                    and x.get("finding_id") not in (RUN_END, RUN_START)]
            if rest:
                warnings.append(
                    f"{len(rest)} record(s) follow this run's {RUN_END!r} with no newer "
                    f"{RUN_START!r} ({rest[:3]}…) — they belong to no run and are NOT "
                    "counted. Start the next run with run-start.")
            break
        if fid == CYCLE_END:
            # `cycle` is REQUIRED, not optional. Without it a duplicate marker is
            # indistinguishable from a legitimate zero-finding cycle — and a zero-finding
            # cycle IS the convergence condition, so any heuristic strict enough to catch
            # the duplicate also makes converging impossible. A monotonic number separates
            # them exactly.
            n = e.get("cycle")
            if not isinstance(n, int):
                raise ValueError(
                    f"{CYCLE_END} is missing an integer `cycle` number. It is required: "
                    "without it a duplicate marker looks identical to a clean cycle.")
            if seen_cycles and n <= seen_cycles[-1]:
                raise ValueError(
                    f"{CYCLE_END} cycle={n} is not greater than the previous "
                    f"({seen_cycles[-1]}) — duplicate or out-of-order marker")
            seen_cycles.append(n)
            cycles.append(cur)
            cur = []
        elif fid not in (RUN_END, RUN_START) and e.get("decision") == "applied":
            cur.append(e)
    # Everything that is not an explicit, valid "minor" counts as serious. Whitelisting the
    # only value that can END a run means a null, a typo or an absent key all fail CLOSED.
    counts = [sum(1 for e in c if e.get("severity") != "minor") for c in cycles]
    return counts, bool(cur), warnings


def convergence_status(entries: list) -> dict:
    """Severity-gated stop rule, replacing the old fixed cycle cap.

    - converged: the newest COMPLETED cycle applied nothing blocking or serious. Positive
      evidence there is nothing left worth finding, which a counter never gave.
    - stalled: the best (minimum) count has not improved for STALL_LIMIT cycles. Testing
      "did not decrease" was insufficient — an oscillation like [2,1,2,1,...] never
      converges and never stalls, i.e. the termination guarantee had an infinite loop in
      exactly the shape it existed to prevent. Improvement-of-best terminates because the
      minimum is a non-negative integer that must strictly fall to keep the loop alive.
    """
    counts, tail_open, warnings = cycle_severity_counts(entries)
    if not counts:
        return {"cycles": 0, "counts": [], "tail_open": tail_open, "warnings": warnings,
                "verdict": "cycle in flight" if tail_open else "no-cycles-yet",
                "converged": False}
    base = {"cycles": len(counts), "counts": counts, "tail_open": tail_open,
            "warnings": warnings}
    if tail_open:  # never converge on an unclosed cycle
        return {**base, "converged": False, "verdict": "cycle in flight — close it first"}
    if counts[-1] == 0:
        if warnings:
            # Advisory diagnostics must not lock the target — but converging ON an
            # ambiguous log would report a clean run over records the parse dropped.
            return {**base, "converged": False,
                    "verdict": "clean cycle, but the log is ambiguous — resolve the "
                               "warning(s) before declaring convergence"}
        return {**base, "converged": True, "verdict": "converged"}
    best_at = min(range(len(counts)), key=lambda i: (counts[i], i))  # first index of the min
    stalled = (len(counts) - 1 - best_at) >= STALL_LIMIT
    return {**base, "converged": False,
            "verdict": "stalled — hand over" if stalled else "keep-iterating"}


def _check_log_key(repo: Path, target: str) -> None:
    """Refuse an unqualified key for a foreign target rather than silently merging repos.

    Fails closed: an unqualified `demo` from two different projects would share one log, and
    the second run would inherit the first's decisions as if they were its own.
    """
    if not is_khenrix_repo(repo) and not re.match(r".+@[0-9a-f]{8}:", target):
        raise ValueError(
            f"target {target!r} is unqualified but {repo} is not the khenrix-utils checkout — "
            f"use the log_target from `target-info` (expected {log_target_key(repo, target)!r})")


def log_append(repo: Path, target: str, entry: dict) -> dict:
    _check_log_key(repo, target)
    missing = REQUIRED_LOG_KEYS - entry.keys()
    if missing:
        raise ValueError(f"log entry missing keys: {sorted(missing)}")
    if entry["decision"] not in DECISIONS:
        raise ValueError(f"decision must be one of {sorted(DECISIONS)}")
    # An explicit null is NOT the same as an absent key: `.get(k, default)` returns None
    # for an explicit null, so a null-severity finding counted as 0 and converged a cycle
    # that had applied work. Absent is allowed (defaults to serious); null is not.
    if "severity" in entry and entry["severity"] not in SEVERITIES:
        # Show the TESTS, not just the labels: this is the moment an operator is choosing
        # a severity, and severity is what decides when the run stops. SEVERITY_TESTS was
        # dead code duplicating SKILL.md's table until it was wired in here.
        raise ValueError(
            f"severity must be one of {sorted(SEVERITIES)} (got {entry['severity']!r}); "
            "omit the key entirely to default to 'serious'.\n"
            + "\n".join(f"  {k}: {v}" for k, v in SEVERITY_TESTS.items()))
    # `cycle` is the delimiter the whole convergence rule is built on, so validate it at
    # WRITE time for the same reason as severity — but the stakes are higher: a bad value
    # is only caught at count time, cannot be superseded (a later cycle-end shares the
    # finding_id and the bad one is still inside the scan window), and so bricks
    # convergence-status for the rest of the run. `bool` is excluded explicitly because
    # isinstance(True, int) is True, which would silently count as cycle 1.
    if entry["finding_id"] == CYCLE_END:
        n = entry.get("cycle")
        if isinstance(n, bool) or not isinstance(n, int):
            raise ValueError(
                f"{CYCLE_END!r} requires an integer `cycle` number (got {n!r}) — a bad "
                "value cannot be superseded by a later append and blocks "
                "convergence-status for the whole run")
    if entry["target"] != target:
        raise ValueError(f"entry target {entry['target']!r} != --target {target!r}")
    entry.setdefault("ts", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    p = log_path(repo, target)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry


def log_entries(repo: Path, target: str) -> list[dict]:
    """EVERY entry in write order — the raw history.

    Distinct from log_list(), which collapses to the latest decision per finding_id: cycle
    accounting needs each cycle's own applied findings, and a deduped view would drop a
    finding that was applied in one cycle and superseded in a later one.
    """
    _check_log_key(repo, target)
    p = log_path(repo, target)
    if not p.is_file():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def log_list(repo: Path, target: str) -> list[dict]:
    """Latest decision per finding_id (later lines win)."""
    _check_log_key(repo, target)  # reading the WRONG repo's log imports its frozen decisions
    p = log_path(repo, target)
    if not p.is_file():
        return []
    latest: dict[str, dict] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            e = json.loads(line)
            latest[e["finding_id"]] = e
    return sorted(latest.values(), key=lambda e: e.get("ts", ""))


# --------------------------------------------------------------------------- #
_MISSING = object()


def _raises(fn, exc) -> bool:
    try:
        fn()
    except exc:
        return True
    return False


def _self_test() -> int:
    import tempfile
    ok = []
    # model regex: must-match and must-NOT-match shapes
    for s in ("claude-opus-4-8", "claude-fable-5", "claude-haiku-4-5-20251001",
              "gpt-5.5", "gpt-4o", "o4-mini", "o3-pro", "gemini-3.5-flash"):
        ok.append((f"regex matches {s}", bool(MODEL_RX.fullmatch(s))))
    for s in ("gpt_helper.py", "solo4-mini", "clock-opus-4", "audio2-track", "claude-code"):
        ok.append((f"regex ignores {s}", not MODEL_RX.search(s)))
    # approved-set tagging incl. dated-variant prefix rule
    approved = {"claude-opus-4-8", "claude-haiku-4-5"}
    ok.append(("exact id is current", tag_model("claude-opus-4-8", approved) == "current"))
    ok.append(("dated variant is current", tag_model("claude-haiku-4-5-20251001", approved) == "current"))
    ok.append(("unknown id is stale-candidate", tag_model("claude-opus-4-6", approved) == "stale-candidate"))
    ok.append(("no approved set -> found", tag_model("gpt-5.5", set()) == "found"))
    ok.append(("display label slugs to id", _slug("Gemini 3.5 Flash (High)") == "gemini-3.5-flash"))
    ok.append(("plain id survives slugging", _slug("claude-opus-4-8") == "claude-opus-4-8"))
    ok.append(("own self-test fixtures excluded from scans",
               bool(EXCLUDE_RX.search("shared/skills/skill-tuneup/scripts/tuneup.py"))
               and not EXCLUDE_RX.search("shared/skills/skill-tuneup/scripts/other.py")))
    # baseline subject filtering (newest-first)
    commits = [{"sha": "c1", "date": "2026-07-01", "subject": "chore: bump receipts"},
               {"sha": "c2", "date": "2026-06-20", "subject": "docs: fix typo"},
               {"sha": "c3", "date": "2026-06-01", "subject": "fix(llm-council): retry judge"}]
    picked = pick_baseline(commits)
    ok.append(("baseline skips chore/docs", picked["sha"] == "c3"))
    ok.append(("skips are countable", sum(1 for c in commits if c["date"] > picked["date"]) == 2))
    ok.append(("all-chore history falls back to newest",
               pick_baseline(commits[:2])["sha"] == "c1"))
    ok.append(("empty history -> None", pick_baseline([]) is None))
    # triage scoring: monotonic in each signal
    ok.append(("no-evals outranks fresh",
               triage_score("no-evals", 10, 0, 100) > triage_score("fresh", 10, 0, 100)))
    ok.append(("stale hits raise score",
               triage_score("fresh", 10, 3, 100) > triage_score("fresh", 10, 0, 100)))
    ok.append(("age raises score, capped",
               triage_score("fresh", 400, 0, 100) > triage_score("fresh", 30, 0, 100)
               and triage_score("fresh", 4000, 0, 100) == triage_score("fresh", 400, 0, 100)))
    ok.append(("near line-cap raises score",
               triage_score("fresh", 10, 0, 480) > triage_score("fresh", 10, 0, 100)))
    # skill_facts section slicing
    caps = "[models]\nx = 1\n[skill_facts.khenrix-setup.claude]\nm = 'claude-opus-4-8'\n[skill_facts.other.claude]\nm = 'gpt-5.5'\n"
    lines = _facts_lines(caps, "khenrix-setup")
    ok.append(("facts slice finds own section", any("claude-opus-4-8" in ln for _, ln in lines)))
    ok.append(("facts slice excludes other sections", not any("gpt-5.5" in ln for _, ln in lines)))
    # Two-tier targeting + the receipt gate. These branches shipped untested once and one of
    # them was DEAD (`provenance == "seed"` vs a producer writing "seeded: …"), so assert the
    # actual strings rather than trusting the shape.
    with tempfile.TemporaryDirectory() as td:
        r = Path(td)
        (r / "shared" / "skills" / "alpha").mkdir(parents=True)
        (r / "capabilities.toml").write_text("[models]\nclaude = []\n")
        (r / ".claude" / "skills" / "beta").mkdir(parents=True)
        ti_a = target_info(r, "alpha")
        ok.append(("khenrix layout resolves to full-gate", ti_a["tier"] == "full-gate"))
        ok.append(("full-gate log target is unqualified", ti_a["log_target"] == "alpha"))
        # a stray .claude/skills copy INSIDE khenrix-utils must not resolve — layouts are
        # chosen by repo kind, so the real source can never be shadowed by a generic one
        ok.append(("foreign layout is ignored inside a khenrix repo",
                   target_info(r, "beta")["found"] is False))
        # same basename, different roots — the collision an unhashed key would merge
        c1, c2 = Path(td) / "a" / "dup", Path(td) / "b" / "dup"
        (c1 / "skills" / "x").mkdir(parents=True)
        (c2 / "skills" / "x").mkdir(parents=True)
        ok.append(("same-basename repos get distinct log keys",
                   log_target_key(c1, "x") != log_target_key(c2, "x")))
        ok.append(("unqualified key for a foreign repo is refused",
                   _raises(lambda: _check_log_key(c1, "x"), ValueError)))
        ok.append(("qualified key for a foreign repo is accepted",
                   _check_log_key(c1, log_target_key(c1, "x")) is None))
        ok.append(("run log resolves into the khenrix checkout, not the target repo",
                   is_khenrix_repo(log_path(c1, log_target_key(c1, "x")).parents[3])))
        ok.append(("missing skill reports not-found", target_info(r, "nope")["found"] is False))
        f = Path(td) / "foreign"
        (f / "skills" / "gamma").mkdir(parents=True)
        (f / ".claude" / "skills" / "delta").mkdir(parents=True)
        ti_g, ti_d = target_info(f, "gamma"), target_info(f, "delta")
        ok.append(("skills/<n> in a non-khenrix repo is council-only",
                   ti_g["tier"] == "council-only"))
        ok.append((".claude/skills/<n> in a non-khenrix repo also resolves",
                   ti_d["found"] and ti_d["tier"] == "council-only"))
        ok.append(("foreign log target is repo-qualified",
                   bool(re.match(rf"foreign@[0-9a-f]{{8}}:gamma$", ti_g["log_target"]))))
        # verify_final_receipt: assert on the REAL producer strings
        ev = r / "evals" / "alpha"
        ev.mkdir(parents=True)
        ev.joinpath("evals.json").write_text("{}")
        def _probs(rec):
            ev.joinpath("receipt.json").write_text(json.dumps(rec))
            return " ".join(verify_final_receipt(r, "alpha", ["claude", "codex", "agy"]))
        ok.append(("seeded receipt is rejected",
                   "seeded, not earned" in _probs(
                       {"providers": ["claude", "codex", "agy"],
                        "provenance": "seeded: blessed current committed state"})))
        ok.append(("single-provider receipt is rejected",
                   "FULL-PANEL" in _probs({"providers": ["claude"], "provenance": "eval"})))
        ok.append(("self-test-gated receipt skips the panel requirement",
                   "FULL-PANEL" not in _probs(
                       {"providers": ["claude"], "provenance": "eval", "self_test": True})))
        ok.append(("missing receipt is reported",
                   "no receipt" in " ".join(verify_final_receipt(r, "zeta", ["claude"]))))
    # severity-gated convergence: the rule that replaced the fixed cycle cap
    def _hist(counts, tail=0):
        e = [{"finding_id": RUN_START, "decision": "applied"}]
        for c in counts:
            e += [{"finding_id": f"f{i}", "decision": "applied", "severity": "serious"}
                  for i in range(c)]
            e.append({"finding_id": CYCLE_END, "decision": "applied",
                      "cycle": len([x for x in e if x["finding_id"] == CYCLE_END]) + 1})
        e += [{"finding_id": f"t{i}", "decision": "applied", "severity": "serious"}
              for i in range(tail)]
        return e
    ok.append(("work appended after a completed run WARNS and blocks convergence",
               convergence_status(
                   [{"finding_id": RUN_START, "decision": "applied"},
                    {"finding_id": CYCLE_END, "decision": "applied", "cycle": 1},
                    {"finding_id": RUN_END, "decision": "applied"},
                    {"finding_id": "f", "decision": "applied", "severity": "serious"},
                    {"finding_id": CYCLE_END, "decision": "applied", "cycle": 2}]
               )["converged"] is False))
    ok.append(("a properly closed run followed by a new one still converges",
               convergence_status(
                   [{"finding_id": RUN_START, "decision": "applied"},
                    {"finding_id": CYCLE_END, "decision": "applied", "cycle": 1},
                    {"finding_id": RUN_END, "decision": "applied"},
                    {"finding_id": RUN_START, "decision": "applied"},
                    {"finding_id": CYCLE_END, "decision": "applied", "cycle": 1}]
               )["converged"] is True))
    ok.append(("a clean final cycle converges",
               convergence_status(_hist([3, 1, 0]))["verdict"] == "converged"))
    ok.append(("a declining rate keeps iterating",
               convergence_status(_hist([6, 3, 1]))["verdict"] == "keep-iterating"))
    ok.append(("a flat/rising rate stalls and hands over",
               convergence_status(_hist([2, 2, 3]))["verdict"].startswith("stalled")))
    ok.append(("minor findings do not block convergence",
               convergence_status(
                   _hist([2]) + [{"finding_id": "m", "decision": "applied", "severity": "minor"},
                                 {"finding_id": CYCLE_END, "decision": "applied", "cycle": 2}]
               )["verdict"] == "converged"))
    ok.append(("an unsevered applied finding counts as serious (fail closed)",
               convergence_status(
                   [{"finding_id": RUN_START, "decision": "applied"},
                    {"finding_id": "x", "decision": "applied"},
                    {"finding_id": CYCLE_END, "decision": "applied", "cycle": 1}]
               )["verdict"] == "keep-iterating"))
    ok.append(("deferred findings never block convergence",
               convergence_status(
                   [{"finding_id": RUN_START, "decision": "applied"},
                    {"finding_id": "d", "decision": "deferred", "severity": "blocking"},
                    {"finding_id": CYCLE_END, "decision": "applied", "cycle": 1}]
               )["verdict"] == "converged"))
    ok.append(("an oscillating rate stalls (the infinite loop the old rule allowed)",
               convergence_status(_hist([2, 1, 2, 1, 2, 1]))["verdict"].startswith("stalled")))
    # A SECOND run-start inside one run (the natural resume point) used to drop that run's
    # findings and report converged — but only on a target with a completed run behind it,
    # so every real target was in the unguarded regime and the self-test's first-run
    # fixtures never saw it. Assert on the shape that actually shipped.
    _closed_run = [{"finding_id": RUN_START, "decision": "applied"},
                   {"finding_id": CYCLE_END, "decision": "applied", "cycle": 1},
                   {"finding_id": RUN_END, "decision": "applied"}]
    # The run writes its OWN run-start, does blocking work, then re-writes run-start
    # (Step 1 is the natural resume point after an interruption). The findings are then
    # before the newest marker and drop out of the count.
    _reopened = (_closed_run
                 + [{"finding_id": RUN_START, "decision": "applied"}]
                 + [{"finding_id": f"bug{i}", "decision": "applied", "severity": "blocking"}
                    for i in range(2)]
                 + [{"finding_id": RUN_START, "decision": "applied"},
                    {"finding_id": CYCLE_END, "decision": "applied", "cycle": 1}])
    ok.append(("a re-opened run cannot converge over its dropped findings",
               convergence_status(_reopened)["converged"] is False))
    ok.append(("re-opened run names the dropped findings",
               any("bug0" in w for w in cycle_severity_counts(_reopened)[2])))
    # The discriminating half: closing out a deferred finding BETWEEN runs is this log's
    # established practice and must stay silent, or no run that tidies up could converge.
    ok.append(("inter-run bookkeeping still converges",
               convergence_status(
                   _closed_run
                   + [{"finding_id": "closeout", "decision": "applied", "severity": "minor"},
                      {"finding_id": RUN_START, "decision": "applied"},
                      {"finding_id": CYCLE_END, "decision": "applied", "cycle": 1}]
               )["converged"] is True))
    ok.append(("a strictly declining rate keeps iterating however long",
               convergence_status(_hist([9, 8, 7, 6, 5]))["verdict"] == "keep-iterating"))
    ok.append(("an OPEN cycle never converges",
               convergence_status(_hist([3, 0], tail=1))["converged"] is False))
    ok.append(("a run that ends mid-cycle leaves the cycle OPEN, never converged",
               convergence_status(
                   [{"finding_id": RUN_START, "decision": "applied"},
                    {"finding_id": "a", "decision": "applied", "severity": "serious"},
                    {"finding_id": RUN_END, "decision": "applied"},
                    {"finding_id": CYCLE_END, "decision": "applied", "cycle": 1}]
               )["converged"] is False))
    ok.append(("a fresh run does not inherit the prior run's history",
               convergence_status(
                   _hist([5, 5, 5]) + [{"finding_id": RUN_END, "decision": "applied"}]
                   + _hist([0]))["verdict"] == "converged"))
    ok.append(("findings before run-start WARN but do not lock the target",
               bool(convergence_status(
                   [{"finding_id": "f", "decision": "applied", "severity": "blocking"},
                    {"finding_id": RUN_START, "decision": "applied"},
                    {"finding_id": CYCLE_END, "decision": "applied", "cycle": 1}]
               )["warnings"])))
    ok.append(("legitimate inter-run bookkeeping does not lock the target",
               convergence_status(
                   [{"finding_id": RUN_START, "decision": "applied"},
                    {"finding_id": "f", "decision": "applied", "severity": "serious"},
                    {"finding_id": CYCLE_END, "decision": "applied", "cycle": 1},
                    {"finding_id": RUN_END, "decision": "applied"},
                    {"finding_id": "closed-old", "decision": "applied", "severity": "minor"},
                    {"finding_id": RUN_START, "decision": "applied"},
                    {"finding_id": CYCLE_END, "decision": "applied", "cycle": 1}]
               )["verdict"] == "converged"))
    ok.append(("run-convergence is TERMINAL — a later run without run-start cannot inherit",
               convergence_status(
                   [{"finding_id": RUN_START, "decision": "applied"},
                    {"finding_id": "a", "decision": "applied", "severity": "serious"},
                    {"finding_id": CYCLE_END, "decision": "applied", "cycle": 1},
                    {"finding_id": RUN_END, "decision": "applied"},
                    {"finding_id": "b", "decision": "applied", "severity": "serious"},
                    {"finding_id": CYCLE_END, "decision": "applied", "cycle": 4}]
               )["counts"] == [1]))
    ok.append(("a missing run-start is REFUSED (would inherit all history)",
               _raises(lambda: convergence_status(
                   [{"finding_id": "f", "decision": "applied", "severity": "serious"},
                    {"finding_id": CYCLE_END, "decision": "applied", "cycle": 1}]), ValueError)))
    ok.append(("a duplicate cycle-end is REFUSED (would fake a clean cycle)",
               _raises(lambda: convergence_status(
                   [{"finding_id": RUN_START, "decision": "applied"},
                    {"finding_id": "f", "decision": "applied", "severity": "serious"},
                    {"finding_id": CYCLE_END, "decision": "applied", "cycle": 1},
                    {"finding_id": CYCLE_END, "decision": "applied", "cycle": 1}]), ValueError)))
    ok.append(("non-monotonic numbered cycle-end is REFUSED",
               _raises(lambda: convergence_status(
                   [{"finding_id": RUN_START, "decision": "applied"},
                    {"finding_id": "f", "decision": "applied", "severity": "serious"},
                    {"finding_id": CYCLE_END, "decision": "applied", "cycle": 2},
                    {"finding_id": CYCLE_END, "decision": "applied", "cycle": 2}]), ValueError)))
    ok.append(("a prior COMPLETED run before run-start is fine",
               convergence_status(
                   [{"finding_id": RUN_START, "decision": "applied"},
                    {"finding_id": "old", "decision": "applied", "severity": "serious"},
                    {"finding_id": CYCLE_END, "decision": "applied", "cycle": 1},
                    {"finding_id": RUN_END, "decision": "applied"},
                    {"finding_id": RUN_START, "decision": "applied"},
                    {"finding_id": CYCLE_END, "decision": "applied", "cycle": 1}]
               )["verdict"] == "converged"))
    with tempfile.TemporaryDirectory() as td:  # khenrix-shaped, or the LOG-KEY check raises
        kr = Path(td)                          # first and the assertion passes vacuously
        (kr / "shared" / "skills").mkdir(parents=True)
        (kr / "capabilities.toml").write_text("[models]\n")
        def _sev(v):
            e = {"target": "x", "finding_id": "y", "decision": "applied"}
            if v is not _MISSING:
                e["severity"] = v
            try:
                log_append(kr, "x", e)
                return None
            except ValueError as ex:
                return str(ex)
        ok.append(("a bad severity is rejected at write time — for the RIGHT reason",
                   "severity must be one of" in (_sev("P0") or "")))
        # "severity must be one of" predates the SEVERITY_TESTS wiring, so asserting only
        # that would stay green if the tests were deleted again — dead code restored.
        ok.append(("bad severity prints the objective TESTS, not just the labels",
                   all(f"  {k}: {v}" in (_sev("P0") or "")
                       for k, v in SEVERITY_TESTS.items())))
        ok.append(("an explicit null severity is rejected (absent != null)",
                   "severity must be one of" in (_sev(None) or "")))
        ok.append(("an omitted severity is accepted", _sev(_MISSING) is None))
    ok.append(("a null severity counts as serious, not zero",
               convergence_status(
                   [{"finding_id": RUN_START, "decision": "applied"},
                    {"finding_id": "f", "decision": "applied", "severity": None},
                    {"finding_id": CYCLE_END, "decision": "applied", "cycle": 1}]
               )["verdict"] == "keep-iterating"))
    # lock: the token is what makes a steal detectable — touch -c could not
    _saved = globals()["LOCK_DIR"]
    with tempfile.TemporaryDirectory() as td:
        globals()["LOCK_DIR"] = Path(td) / "lock.d"
        got, owner = lock_acquire()
        ok.append(("lock acquires", got))
        ok.append(("second acquire is refused", lock_acquire()[0] is False))
        ok.append(("refresh with the owner token succeeds", lock_refresh(owner)[0]))
        # exercise the token in the shape acquire PRINTS, not just the shape it returns:
        # the operator only ever sees `OWNER=<token>`, so testing the bare form alone
        # left the real interface broken while the suite stayed green.
        ok.append(("refresh accepts the printed OWNER= form",
                   lock_refresh(f"OWNER={owner}")[0]))
        ok.append(("refresh accepts a trailing newline",
                   lock_refresh(f"OWNER={owner}\n")[0]))
        ok.append(("refresh with a wrong token reports a steal",
                   lock_refresh("bogus")[0] is False))
        ok.append(("OWNER= prefix does not mask a wrong token",
                   lock_refresh("OWNER=bogus")[0] is False))
        ok.append(("non-owner cannot release", lock_release("bogus")[0] is False))
        ok.append(("release accepts the printed OWNER= form",
                   lock_release(f"OWNER={owner}")[0]))
        got, owner = lock_acquire()  # re-take: the line above released it
        ok.append(("re-acquire after release succeeds", got))
        ok.append(("owner releases", lock_release(owner)[0]))
        ok.append(("refresh after release reports it gone", lock_refresh(owner)[0] is False))
    globals()["LOCK_DIR"] = _saved
    # triage must not recommend work on zero evidence — the sort is (-score, skill), so an
    # all-zero board would otherwise crown whichever skill sorts first alphabetically.
    # The union, not concatenation: a name under BOTH source dirs is one skill. Reverting
    # to `sorted(a) + sorted(b)` leaves every other triage assertion green, so this is the
    # only thing standing between that revert and a duplicated board.
    with tempfile.TemporaryDirectory() as _td:
        _r = Path(_td)
        (_r / "shared" / "skills" / "dup").mkdir(parents=True)
        (_r / "shared" / "skill-templates" / "dup").mkdir(parents=True)
        (_r / "capabilities.toml").write_text("[models]\nclaude = []\n")
        try:
            _rows = triage(_r)
            ok.append(("triage: a name in BOTH source dirs yields ONE row",
                       [r["skill"] for r in _rows].count("dup") == 1))
        except Exception as _e:  # noqa: BLE001
            ok.append((f"triage: a name in BOTH source dirs yields ONE row ({_e})", False))
    ok.append(("triage: a signal-free board scores 0 for every row",
               triage_score("fresh", 0, 0, 100) == 0))
    ok.append(("triage: the line-budget rule is what lifts a fresh skill off 0",
               triage_score("fresh", 0, 0, 480) == 10))
    ok.append(("triage: a stale receipt still outranks a line budget",
               triage_score("stale-source", 0, 0, 100) > triage_score("fresh", 0, 0, 480)))
    # An UNKNOWN baseline age must not read as a fresh one. triage() swallows git errors,
    # so without this a checkout where git fails for every skill scores an all-zero board
    # and triage_recommendation reports "nothing to tune up" — a confident wrong all-clear.
    # Missing evidence must not become staleness points, and must not become a winner.
    ok.append(("triage: an unknown age scores the SAME as a known-fresh one (no points)",
               triage_score("fresh", None, 0, 100) == triage_score("fresh", 0, 0, 100)))
    ok.append(("triage: 70 days of real neglect still outranks an unknown age",
               triage_score("fresh", 70, 0, 100) > triage_score("fresh", None, 0, 100)))
    _unk = [{"skill": "aaa", "score": 0, "age_days": None},
            {"skill": "zzz", "score": 0, "age_days": None}]
    ok.append(("triage: an all-unknown board reports the DIAGNOSIS, not a winner",
               "recommend" not in triage_recommendation(_unk)
               and "UNKNOWN for every skill" in triage_recommendation(_unk)))
    ok.append(("triage: an all-unknown board is not reported as a clean all-clear",
               "nothing to tune up" not in triage_recommendation(_unk)))
    # A MIXED board must still rank. `all(...)` -> `any(...)` survives every assertion
    # above, and under `any` a single skill with no history would suppress a valid
    # recommendation for the whole board.
    _mixed = [{"skill": "hot", "score": 40, "age_days": None},
              {"skill": "cold", "score": 2, "age_days": 12.0}]
    ok.append(("triage: a MIXED board still recommends, not diagnoses",
               "recommend" in triage_recommendation(_mixed)
               and "UNKNOWN for every skill" not in triage_recommendation(_mixed)))
    # A real signal must survive a TOTAL age blackout: score 55 comes from receipt state,
    # stale model ids and the line budget, none of which touch git. Suppressing the
    # recommendation there withheld an answer the tool had good grounds for.
    _blackout = [{"skill": "stale-one", "score": 55, "age_days": None},
                 {"skill": "other", "score": 10, "age_days": None}]
    ok.append(("triage: a decisive signal SURVIVES an all-unknown age board",
               "recommend" in triage_recommendation(_blackout)
               and "stale-one" in triage_recommendation(_blackout)))
    ok.append(("triage: and it discloses that the age component is missing",
               "age component" in triage_recommendation(_blackout)))
    ok.append(("triage: an all-unknown board with NO signal is still the bare diagnosis",
               "recommend" not in triage_recommendation(
                   [{"skill": "a", "score": 0, "age_days": None}])))
    ok.append(("triage: a partial blackout with no signal refuses a clean all-clear",
               "INCOMPLETE" in triage_recommendation(
                   [{"skill": "a", "score": 0, "age_days": None},
                    {"skill": "b", "score": 0, "age_days": 3.0}])))
    ok.append(("triage: a fully-known board with no signal IS a clean all-clear",
               triage_recommendation([{"skill": "a", "score": 0, "age_days": 3.0}])
               == "no skill shows a staleness signal — nothing to tune up."))
    ok.append(("triage: a partial blackout WITH a signal names how many are unknown",
               "1 of 2" in triage_recommendation(
                   [{"skill": "a", "score": 40, "age_days": None},
                    {"skill": "b", "score": 0, "age_days": 3.0}])))
    with tempfile.TemporaryDirectory() as td:
        try:
            triage(Path(td))
            ok.append(("triage: refuses a non-khenrix repo instead of reporting nothing", False))
        except ValueError:
            ok.append(("triage: refuses a non-khenrix repo instead of reporting nothing", True))
    # Assert the DECISION, not the score: a score assertion still passes with the
    # threshold removed, which is exactly how this first shipped insensitive.
    _zero = [{"skill": "aaa-first", "score": 0}, {"skill": "zzz-last", "score": 0}]
    ok.append(("triage: an all-zero board recommends NOTHING",
               "recommend" not in triage_recommendation(_zero)))
    ok.append(("triage: an all-zero board says so explicitly",
               "nothing to tune up" in triage_recommendation(_zero)))
    ok.append(("triage: a real signal still produces a recommendation",
               "recommend: deep tune-up of 'x'" in
               triage_recommendation([{"skill": "x", "score": 10}])))
    # the staleness window must exceed the longest step this skill's own guidance produces:
    # deep timeout 1800s x (retries 1 + 1) = 60 min for a self-tuneup, 90 at the default.
    # Assert the DOCUMENTED 135, not a weaker bound. A default deep fan-out is already
    # 3 x 1800s plus backoff = 90 min 15 s before teardown and worktree setup, so `> 90`
    # still admitted values a single legal fan-out can exhaust — which is exactly how the
    # previous 90 became wrong when MODE_TIMEOUT["deep"] moved 1200 -> 1800 and nothing
    # here noticed. The number in the failure table and the number here have to be the
    # same number.
    ok.append(("lock: the staleness window is the documented 135 min", LOCK_STALE_MIN == 135))
    # AND ENFORCE THE INVARIANT MECHANICALLY, not just in the comment above the constant.
    # This is the defect that produced the 135: LOCK_STALE_MIN was coupled to
    # MODE_TIMEOUT["deep"] by PROSE ONLY, the council engine moved 1200 -> 1800 on
    # 2026-08-13, and nothing failed — the window silently stopped exceeding a single
    # legal fan-out. A comment cannot notice a number changing in another file; this can.
    # Skipped (not failed) when the engine is unreachable, because tuneup.py also runs
    # against non-khenrix repos that have no council engine.
    _eng = Path(__file__).resolve().parents[3] / "lib" / "council" / "engine.py"
    if _eng.is_file():
        _ns: dict = {}
        for _ln in _eng.read_text().splitlines():
            if _ln.startswith("MODE_TIMEOUT"):
                exec(_ln, _ns)  # noqa: S102 - a literal dict assignment from our own repo
                break
        _deep = (_ns.get("MODE_TIMEOUT") or {}).get("deep")
        if _deep:
            _longest = 3 * _deep / 60          # default --retries 2 => 3 attempts
            ok.append((f"lock: window {LOCK_STALE_MIN} min strictly exceeds one default "
                       f"deep fan-out ({_longest:.0f} min at MODE_TIMEOUT deep={_deep}s)",
                       LOCK_STALE_MIN > _longest))
    # lock status must be a QUESTION, never an action: it is the answer to "is the holder
    # alive?", and the old answer (`lock acquire`) destroyed the lock past the window.
    with tempfile.TemporaryDirectory() as _std:
        _saved = LOCK_DIR
        try:
            globals()["LOCK_DIR"] = Path(_std) / "lock.d"
            ok.append(("lock status: reports not-held without creating anything",
                       lock_status() == {"held": False} and not LOCK_DIR.exists()))
            _got, _tok = lock_acquire()
            _st = lock_status()
            ok.append(("lock status: reports the holder and an age",
                       _st["held"] and _st["owner"] == _tok and _st["age_min"] >= 0))
            _old = time.time() - 999 * 60
            os.utime(LOCK_DIR, (_old, _old))
            ok.append(("lock status: does NOT steal a lock far past the stale window",
                       lock_status()["held"] and LOCK_DIR.is_dir()
                       and (LOCK_DIR / "owner").read_text().strip() == _tok))
        finally:
            shutil.rmtree(LOCK_DIR, ignore_errors=True)
            globals()["LOCK_DIR"] = _saved
    # Exercise the boundary through lock_acquire's DEFAULT, so the failure-table's "older
    # than 135 min" and the constant cannot drift apart, and so a change to the default
    # argument is caught too. 134 -> still held; 136 -> stolen.
    with tempfile.TemporaryDirectory() as _ltd:
        _saved = LOCK_DIR
        try:
            globals()["LOCK_DIR"] = Path(_ltd) / "lock.d"
            for _age, _want_held in ((134, True), (136, False)):
                shutil.rmtree(LOCK_DIR, ignore_errors=True)
                lock_acquire()
                _old = time.time() - _age * 60
                os.utime(LOCK_DIR, (_old, _old))
                _got, _ = lock_acquire()          # default stale_min, not an override
                ok.append((f"lock: a {_age}-min-old lock is "
                           f"{'still held' if _want_held else 'stealable'}",
                           _got is not _want_held))
        finally:
            shutil.rmtree(LOCK_DIR, ignore_errors=True)
            globals()["LOCK_DIR"] = _saved

    # ambiguous target: two same-tier layouts must be REFUSED, not silently resolved
    with tempfile.TemporaryDirectory() as td:
        fr = Path(td) / "foreign"
        for layout in (".claude/skills/x", "skills/x"):
            (fr / layout).mkdir(parents=True)
            (fr / layout / "SKILL.md").write_text("---\nname: x\n---\n")
        info = target_info(fr, "x")
        ok.append(("two same-tier layouts are reported ambiguous", info["ambiguous"] is True))
        ok.append(("ambiguous target names BOTH paths", len(info["paths"]) == 2))
        # capture the refusal so the suite's own output stays readable — the message is
        # expected here, and printing it mid-suite reads like a failure
        _buf = io.StringIO()
        with contextlib.redirect_stdout(_buf):   # this refusal goes to STDOUT, not stderr
            _rc = main(["target-info", "--repo", str(fr), "--skill", "x"])
        ok.append(("target-info exits nonzero on an ambiguous target", _rc != 0))
        ok.append(("ambiguous refusal explains itself", "MORE THAN ONE layout" in _buf.getvalue()))

    # review-material: every case the shell loop it replaced got wrong, verified live
    with tempfile.TemporaryDirectory() as td:
        r = Path(td) / "repo"; r.mkdir()
        subprocess.run(["git", "-C", str(r), "init", "-q", "."], check=True)
        (r / "t.txt").write_text("tracked\n")
        subprocess.run(["git", "-C", str(r), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(r), "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-qm", "i"], check=True)
        secret = Path(td) / "outside-secret.txt"
        secret.write_text("SECRET-OUTSIDE-REPO\n")
        (r / "leak.txt").symlink_to(secret)          # points OUTSIDE the repo
        (r / "broken.txt").symlink_to(Path(td) / "nope")
        (r / "newline-only.md").write_text("\n")     # grep -Iq . called this BINARY
        (r / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00data")
        (r / "name with spaces.md").write_text("spaced\n")
        (r / "big.md").write_bytes(b"a" * 65535 + "é".encode() + b"tail")  # split codepoint
        out = review_material(r)
        ok.append(("review-material never dereferences a symlink (exfiltration guard)",
                   "SECRET-OUTSIDE-REPO" not in out))
        ok.append(("review-material names the skipped symlink", "SYMLINK — not followed" in out))
        ok.append(("review-material survives a broken symlink", "broken.txt" in out))
        ok.append(("review-material keeps a newline-only text file",
                   "newline-only.md" in out and "newline-only.md ===" in out
                   and "BINARY" not in out.split("newline-only.md")[1][:40]))
        ok.append(("review-material omits a real binary", "shot.png" in out and "BINARY" in out))
        ok.append(("review-material handles spaces in a path", "name with spaces.md" in out))
        # NOT `out.encode().decode() == out` — a tautology for any str. Assert the thing
        # that actually separates this implementation from the naive ones: a strict decode
        # would RAISE on the split codepoint, and a latin-1 fallback would surface é or
        # U+FFFD. Only dropping the partial codepoint passes.
        ok.append(("review-material drops the partial codepoint rather than splitting it",
                   "[truncated" in out and "é" not in out and "\ufffd" not in out))
        (r / "t.txt").write_text("tracked and then MODIFIED\n")
        ok.append(("review-material includes tracked modifications too",
                   "diff --git" in review_material(r)))
        subprocess.run(["git", "-C", str(r), "add", "-A"], check=True)
        ok.append(("review-material sees a STAGED tree (diff HEAD, not bare diff)",
                   "diff --git" in review_material(r)))
        # a failed git must RAISE, not return "" — an empty result is what tells Step 9
        # there is nothing to review, and a skipped review reads as a converged cycle
        try:
            review_material(Path(td) / "not-a-repo-at-all")
            ok.append(("review-material fails CLOSED on a broken repo", False))
        except RuntimeError:
            ok.append(("review-material fails CLOSED on a broken repo", True))
        for i in range(6):
            (r / f"scratch{i}.md").write_text("x" * 4000)
        capped = review_material(r, cap=2000, total_cap=6000)
        ok.append(("review-material enforces an AGGREGATE cap, not just per-file",
                   "total cap reached" in capped))
        # Bound the UNTRACKED contribution, not len(capped): by now the fixture has staged
        # files, so the diff itself legitimately carries them and would mask the check.
        ok.append(("aggregate cap bounds the untracked contribution",
                   capped.count("total cap reached") == 3
                   and sum(len(s) for s in capped.split("=== NEW FILE")[1:]) < 20000))
    # log round-trip in a tempdir; latest decision per finding wins
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        # khenrix-shaped so registry_repo() resolves to THIS tempdir — otherwise the log
        # would route to the real checkout and the test would write into the repo.
        (repo / "shared" / "skills").mkdir(parents=True)
        (repo / "capabilities.toml").write_text("[models]\n")
        e1 = {"target": "markitdown", "finding_id": "stale-flag", "decision": "deferred"}
        e2 = {"target": "markitdown", "finding_id": "stale-flag", "decision": "applied"}
        log_append(repo, "markitdown", dict(e1))
        log_append(repo, "markitdown", dict(e2))
        got = log_list(repo, "markitdown")
        ok.append(("log keeps latest decision per finding",
                   len(got) == 1 and got[0]["decision"] == "applied"))
        ok.append(("log adds a timestamp", "ts" in got[0]))
        try:
            log_append(repo, "markitdown", {"target": "markitdown", "finding_id": "x", "decision": "maybe"})
            ok.append(("bad decision rejected", False))
        except ValueError:
            ok.append(("bad decision rejected", True))
        try:
            log_append(repo, "markitdown", {"finding_id": "x", "decision": "applied"})
            ok.append(("missing keys rejected", False))
        except ValueError:
            ok.append(("missing keys rejected", True))
        # cycle is validated at WRITE time: a bad value cannot be superseded (the later
        # cycle-end shares the finding_id) and bricks convergence-status for the whole run.
        for label, bad in (("string", "1"), ("null", None), ("float", 2.0),
                           ("bool", True), ("absent", ...)):
            entry = {"target": "markitdown", "finding_id": CYCLE_END, "decision": "applied"}
            if bad is not ...:
                entry["cycle"] = bad
            try:
                log_append(repo, "markitdown", entry)
                ok.append((f"cycle-end rejects a {label} cycle", False))
            except ValueError:
                ok.append((f"cycle-end rejects a {label} cycle", True))
        log_append(repo, "markitdown",
                   {"target": "markitdown", "finding_id": CYCLE_END,
                    "decision": "applied", "cycle": 3})
        ok.append(("cycle-end accepts an integer cycle",
                   any(e["finding_id"] == CYCLE_END for e in log_list(repo, "markitdown"))))
    for label, passed in ok:
        print(f"  {'PASS' if passed else 'FAIL'}  {label}")
    return 0 if all(p for _, p in ok) else 1


def _load_checks():
    """Import the receipt validator from wherever THIS engine lives, not from the target.

    The engine always comes from the khenrix checkout (or, once rendered, the plugin
    bundle) while `repo` is whichever repo the TARGET lives in — they are the same only
    for a full-gate target. Importing from `repo/scripts` therefore failed outright in a
    foreign repo, and a failed import that returns early silently skips the provenance
    and panel checks: the command would print "proven" having verified nothing.
    """
    import importlib.util
    here = Path(__file__).resolve()
    candidates = [
        here.parents[4] / "scripts" / "lib" / "checks.py",   # khenrix checkout
        here.parents[3] / "lib" / "checks.py",               # rendered plugin bundle
    ]
    for c in candidates:
        if c.is_file():
            spec = importlib.util.spec_from_file_location("_khenrix_checks", c)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    raise RuntimeError(f"receipt validator not found; looked in {[str(c) for c in candidates]}")


def verify_final_receipt(repo: Path, skill: str, panel: list) -> list:
    """Prove Step 10's full-panel requirement instead of asserting it in prose.

    `make precommit` only compares hashes, so a receipt earned on a single provider
    satisfies it — correct for mid-run iteration but NOT for the convergence gate, which
    requires a green full-panel eval on the exact candidate. Nothing checked that, and in
    practice every receipt stayed single-provider. Returns problems; empty means proven.

    THE RULES THEMSELVES LIVE IN `checks.validate_receipt(final=True)` — one rulebook
    shared with `receipt_gate`, so this command and `make precommit` can no longer
    disagree about the same receipt. llm-council and the deterministic-gated skills stay
    exempt from the panel requirement there: their receipts are earned by a self-test /
    unit suite, so a full panel proves nothing extra about them.
    """
    return _load_checks().validate_receipt(repo, skill, final=True, panel=panel)


REVIEW_BYTE_CAP = 65536
REVIEW_TOTAL_CAP = 512 * 1024


def review_material(repo: Path, cap: int = REVIEW_BYTE_CAP,
                    total_cap: int = REVIEW_TOTAL_CAP) -> str:
    """Assemble exactly what council review #2 must see: the diff PLUS untracked files.

    This was a shell loop in SKILL.md and shell was the wrong tool — three cycles of
    review found four ways it silently mis-served the reviewer, each verified live:

    - `cat`/`wc`/`head` FOLLOW SYMLINKS, so an untracked symlink pointing outside the
      repo would send its referent to three external CLIs. That is an exfiltration path,
      not a formatting bug; symlinks are skipped outright and named in the output.
    - `head -c` cuts by BYTE, so truncating mid-codepoint yields invalid UTF-8 and
      crashes fanout's `read_text()` before any seat spawns — reintroducing the exact
      crash the cap was added to prevent. Truncation is decoded with errors='ignore'.
    - `grep -Iq .` calls a newline-only file BINARY (`.` matches no character on an
      empty line), silently dropping a legitimate text file from the review.
    - `wc -c` on a broken symlink emits nothing, so `[ "$sz" -gt 0 ]` raises.

    Binary detection is a NUL scan of the first 8 KiB — what git itself uses. The per-file
    and aggregate byte caps exist because the whole result becomes ONE prompt sent to three
    CLIs: an unbounded diff either blows a context window or silently truncates inside the
    provider, and a review of a truncated diff still reports as a review.

    The two non-obvious choices, kept here so a later 'simplification' meets them at the
    code rather than only in SKILL.md:

    - `diff HEAD`, never bare `git diff`. Bare diff is blind to the INDEX, so a fully
      staged tree returns empty — and an empty result is exactly what tells Step 9 there
      is nothing to review, silently skipping a mandatory council review.
    - untracked files are appended explicitly. `git diff` in any form cannot see them, so
      a file created during the run (an `evals.json` scaffolded in Step 8.2 is the live
      case) would never reach the reviewer while `git add -A` still ships it.
    """
    def git(*a: str, binary: bool = False):
        """A FAILED git is not an empty diff.

        Capturing stderr to decide "is there anything to review" removed the only signal
        the shell version still had: git's `fatal:` reached the terminal there. Silently
        returning "" here makes a broken repo indistinguishable from a clean one, so
        Step 9 skips the mandatory review and `convergence-status` reads the resulting
        zero-finding cycle as CONVERGED — over a candidate no council ever saw. Realistic
        triggers: dubious ownership on a /mnt/c checkout, an unborn HEAD, a mistyped $REPO.
        """
        # bytes always, decoded explicitly: `text=True` decodes with the locale codec and
        # RAISES on tracked content that is not valid UTF-8, which would abort the review.
        p = subprocess.run(["git", "-C", str(repo), *a], capture_output=True)
        if p.returncode != 0:
            raise RuntimeError(f"git {' '.join(a)} failed in {repo}: "
                               f"{p.stderr.decode('utf-8', 'replace').strip()}")
        return p.stdout if binary else p.stdout.decode("utf-8", "replace")

    parts = [git("diff", "HEAD", "--", ":(exclude)marketplaces")]
    raw = git("ls-files", "--others", "--exclude-standard", "-z",
              "--", ":(exclude)marketplaces", binary=True)
    # The per-file cap bounds what each file EMITS; without a total the review can still
    # blow the prompt out — a foreign repo with a few hundred not-yet-ignored scratch files
    # ships megabytes to three seats at deep-mode prices and fails them all on context,
    # landing back in "review skipped". Read only what can be emitted, never the whole file.
    budget = total_cap
    for name in filter(None, raw.split(b"\0")):
        rel = name.decode("utf-8", "replace")
        p = repo / rel
        if p.is_symlink():          # never dereference — the referent may be outside the repo
            parts.append(f"\n\n=== NEW FILE (untracked, SYMLINK — not followed): {rel} ===\n")
            continue
        if budget <= 0:
            parts.append(f"\n\n=== NEW FILE (untracked, OMITTED — total cap reached): {rel} ===\n")
            continue
        try:
            size = p.stat().st_size
            with open(p, "rb") as fh:
                data = fh.read(min(cap, budget) + 1)
        except OSError as e:
            parts.append(f"\n\n=== NEW FILE (untracked, UNREADABLE: {e.strerror}): {rel} ===\n")
            continue
        if b"\0" in data[:8192]:
            parts.append(f"\n\n=== NEW FILE (untracked, {size} bytes, BINARY — omitted): {rel} ===\n")
            continue
        keep = data[:min(cap, budget)]
        budget -= len(keep)
        head = f"\n\n=== NEW FILE (untracked, {size} bytes): {rel} ===\n"
        body = keep.decode("utf-8", "ignore")
        if len(keep) < size:
            body += f"\n…[truncated {size - len(keep)} bytes]\n"
        parts.append(head + body)
    return "".join(parts)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="skill-tuneup deterministic helpers")
    ap.add_argument("--self-test", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("baseline", "stale-models", "triage"):
        sp = sub.add_parser(name)
        sp.add_argument("--repo", required=True)
        if name != "triage":
            sp.add_argument("--skill", required=(name == "baseline"))
        if name == "stale-models":
            sp.add_argument("--approved", default="", help="extra approved ids, comma-separated")
        sp.add_argument("--json", action="store_true")
    kp = sub.add_parser("lock")
    kp.add_argument("action", choices=["acquire", "refresh", "release", "status"])
    kp.add_argument("--owner", default="")
    cp = sub.add_parser("convergence-status")
    cp.add_argument("--repo", required=True)
    cp.add_argument("--target", required=True)
    cp.add_argument("--json", action="store_true")
    tp = sub.add_parser("target-info")
    tp.add_argument("--repo", required=True)
    tp.add_argument("--skill", required=True)
    tp.add_argument("--json", action="store_true")
    fp = sub.add_parser("verify-final-receipt")
    fp.add_argument("--repo", required=True)
    fp.add_argument("--skill", required=True)
    fp.add_argument("--panel", default="claude,codex,agy")
    fp.add_argument("--json", action="store_true")
    rp = sub.add_parser("review-material")
    rp.add_argument("--repo", required=True)
    lp = sub.add_parser("log")
    lp.add_argument("action", choices=["append", "list"])
    lp.add_argument("--repo", required=True)
    lp.add_argument("--target", required=True)
    lp.add_argument("--entry", help="JSON object for append (or pass via stdin)")
    lp.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()
    if not args.cmd:
        ap.print_help()
        return 2
    if args.cmd == "lock":  # the only command with no --repo
        if args.action == "status":
            st = lock_status()
            if not st["held"]:
                print("no lock held")
                return 0
            print(f"held by {st['owner'] or 'unknown'} ({st['age_min']} min old; "
                  f"the next acquire steals it above {st['stale_after_min']} min)")
            return 0
        if args.action == "acquire":
            ok, info = lock_acquire()
            print(f"OWNER={info}" if ok else f"  ✗ lock not acquired: {info}")
        elif args.action == "refresh":
            if not args.owner:
                print("  ✗ --owner is required (the token from `lock acquire`)")
                return 2
            ok, info = lock_refresh(args.owner)
            print("lock refreshed" if ok else f"  ✗ {info} — STOP, do not keep working")
        else:
            ok, info = lock_release(args.owner)
            print(f"lock {info}" if ok else f"  ✗ {info}")
        return 0 if ok else 1
    repo = Path(args.repo).resolve()

    if args.cmd == "baseline":
        b = baseline(repo, args.skill)
        if not b:
            print(f"no commits found for {args.skill}")
            return 1
        print(json.dumps(b, indent=2) if args.json else
              f"baseline {b['sha'][:9]}  {b['date']}  {b['subject']}"
              f"  ({b['skipped_as_chore']} newer chore/docs commit(s) skipped)")
    if args.cmd == "review-material":
        try:
            sys.stdout.write(review_material(Path(args.repo)))
        except RuntimeError as e:   # never let a git failure read as "nothing to review"
            print(f"  ✗ {e}", file=sys.stderr)
            return 2
        return 0
    elif args.cmd == "convergence-status":
        st = convergence_status(log_entries(repo, args.target))
        if args.json:
            print(json.dumps(st, indent=2))
        else:
            print(f"cycles:   {st['cycles']}   serious-per-cycle: {st['counts']}")
            print(f"verdict:  {st['verdict']}")
            for w in st.get("warnings", []):
                print(f"  ⚠ {w}")
            if st["verdict"] == "stalled — hand over":
                print("  the serious-finding rate stopped falling — another cycle buys "
                      "another defect, not convergence. Hand the remainder to the user.")
        return 0 if st["converged"] else 1
    elif args.cmd == "target-info":
        info = target_info(repo, args.skill)
        if args.json:
            print(json.dumps(info, indent=2))
        else:
            if not info["found"]:
                looked = ", ".join(p.format(s=args.skill)
                                   for p in KHENRIX_LAYOUTS + FOREIGN_LAYOUTS)
                print(f"  ✗ no skill {args.skill!r} in {repo} (looked in {looked})")
            elif info["ambiguous"]:
                print(f"  ✗ {args.skill!r} matches MORE THAN ONE layout in {repo}: "
                      f"{', '.join(info['paths'])} — refusing; there is no single source "
                      f"of truth to tune. Remove or rename the duplicate.")
            else:
                print(f"skill:  {args.skill}  in  {info['repo']}")
                print(f"paths:  {', '.join(info['paths'])}")
                print(f"tier:   {info['tier']}")
                print(f"gate:   {info['gate']}")
                print(f"log:    docs/tuneups/log/{info['log_target']}.jsonl (in khenrix-utils)")
        return 0 if (info["found"] and not info["ambiguous"]) else 1
    elif args.cmd == "verify-final-receipt":
        problems = verify_final_receipt(repo, args.skill, args.panel.split(","))
        if args.json:
            print(json.dumps({"skill": args.skill, "problems": problems}, indent=2))
        elif problems:
            for p in problems:
                print(f"  ✗ {p}")
            print("FINAL GATE NOT PROVEN — do not record convergence")
        else:
            print(f"final gate proven: {args.skill} receipt is full-panel and matches source")
        return 1 if problems else 0
    elif args.cmd == "stale-models":
        approved = approved_models(repo, args.approved)
        hits = scan_stale_models(repo, getattr(args, "skill", None), approved)
        stale = [h for h in hits if h["status"] == "stale-candidate"]
        if args.json:
            print(json.dumps({"hits": hits, "approved": sorted(approved)}, indent=2))
        else:
            for h in hits:
                print(f"{h['file']}:{h['line']}:{h['id']}:{h['status']}")
            print(f"SUMMARY {len(hits)} hits, {len(stale)} stale-candidate, "
                  f"{len({h['id'] for h in hits})} distinct ids")
    elif args.cmd == "triage":
        try:
            rows = triage(repo)
        except ValueError as e:   # a refusal is a RESULT, not a crash — every other
            print(f"  \u2717 {e}")   # refusal in this file prints and returns a code
            return 2
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            print(f"{'score':>5}  {'skill':<16} {'receipt':<13} {'age(d)':>6} "
                  f"{'stale-ids':>9} {'md-lines':>8}")
            for r in rows:
                print(f"{r['score']:>5}  {r['skill']:<16} {r['receipt']:<13} "
                      f"{r['age_days'] if r['age_days'] is not None else '-':>6} "
                      f"{r['stale_model_hits']:>9} {r['skill_md_lines']:>8}")
            print("\n" + triage_recommendation(rows))
    elif args.cmd == "log":
        if args.action == "append":
            raw = args.entry or sys.stdin.read()
            entry = log_append(repo, args.target, json.loads(raw))
            print(json.dumps(entry, sort_keys=True))
        else:
            entries = log_list(repo, args.target)
            if args.json:
                print(json.dumps(entries, indent=2))
            else:
                for e in entries:
                    print(f"{e.get('ts','?'):<26} {e['decision']:<9} {e['finding_id']}"
                          f"  {e.get('title', '')}")
                print(f"({len(entries)} finding(s) with a recorded decision)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
