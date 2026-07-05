#!/usr/bin/env python3
"""Deterministic substrate for the skill-tuneup skill (stdlib only).

Four subcommands keep the judgment-free parts of a tune-up reproducible:

  baseline     --repo R --skill S      last substantive commit (skips chore/docs/style)
  stale-models --repo R [--skill S]    model-ID hits tagged current|stale-candidate
  triage       --repo R                rank ALL skills by staleness (read-only, no network)
  log          append|list --repo R --target S   per-target run memory (JSONL)

  tuneup.py --self-test                hermetic logic tests, no repo/git/network needed

Judgment-shaped work (research, audit, proportionality) lives in SKILL.md and
references/ — this script only reports facts. Run memory lives in
docs/tuneups/log/<target>.jsonl (committed; outside every eval-receipt closure).
"""
from __future__ import annotations
import argparse, json, re, subprocess, sys
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


def skill_paths(repo: Path, skill: str) -> list[Path]:
    """Source-of-truth dirs for a skill: shared/skills/<s> and/or shared/skill-templates/<s>."""
    return [p for p in (repo / "shared" / "skills" / skill,
                        repo / "shared" / "skill-templates" / skill) if p.is_dir()]


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
        raise FileNotFoundError(f"no such skill: {skill} (looked in shared/skills, shared/skill-templates)")
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


def approved_models(repo: Path, extra_csv: str = "") -> set[str]:
    """Approved set = every string in capabilities.toml [models] lists + --approved extras.
    Entries are also slugged, since agy's entry is a display label, not an id."""
    import tomllib
    ids: set[str] = set()
    caps_path = repo / "capabilities.toml"
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
    if md_lines > 450:
        score += 10                                # near the 500-line hard cap
    return score


def triage(repo: Path) -> list[dict]:
    skills = sorted(p.name for p in (repo / "shared" / "skills").glob("*/") if p.is_dir())
    skills += sorted(p.name for p in (repo / "shared" / "skill-templates").glob("*/") if p.is_dir())
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


def log_path(repo: Path, target: str) -> Path:
    return repo / "docs" / "tuneups" / "log" / f"{target}.jsonl"


def log_append(repo: Path, target: str, entry: dict) -> dict:
    missing = REQUIRED_LOG_KEYS - entry.keys()
    if missing:
        raise ValueError(f"log entry missing keys: {sorted(missing)}")
    if entry["decision"] not in DECISIONS:
        raise ValueError(f"decision must be one of {sorted(DECISIONS)}")
    if entry["target"] != target:
        raise ValueError(f"entry target {entry['target']!r} != --target {target!r}")
    entry.setdefault("ts", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    p = log_path(repo, target)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry


def log_list(repo: Path, target: str) -> list[dict]:
    """Latest decision per finding_id (later lines win)."""
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
    # log round-trip in a tempdir; latest decision per finding wins
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
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
    for label, passed in ok:
        print(f"  {'PASS' if passed else 'FAIL'}  {label}")
    return 0 if all(p for _, p in ok) else 1


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
    repo = Path(args.repo).resolve()

    if args.cmd == "baseline":
        b = baseline(repo, args.skill)
        if not b:
            print(f"no commits found for {args.skill}")
            return 1
        print(json.dumps(b, indent=2) if args.json else
              f"baseline {b['sha'][:9]}  {b['date']}  {b['subject']}"
              f"  ({b['skipped_as_chore']} newer chore/docs commit(s) skipped)")
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
        rows = triage(repo)
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            print(f"{'score':>5}  {'skill':<16} {'receipt':<13} {'age(d)':>6} "
                  f"{'stale-ids':>9} {'md-lines':>8}")
            for r in rows:
                print(f"{r['score']:>5}  {r['skill']:<16} {r['receipt']:<13} "
                      f"{r['age_days'] if r['age_days'] is not None else '-':>6} "
                      f"{r['stale_model_hits']:>9} {r['skill_md_lines']:>8}")
            if rows:
                print(f"\nrecommend: deep tune-up of '{rows[0]['skill']}' first")
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
