#!/usr/bin/env python3
"""Read Claude Code session JSONL and report token spend (stdlib only).

Walks ~/.claude/projects/**/*.jsonl INCLUDING nested subagents/**/agent-*.jsonl
(real spend, isSidechain). Dedupes replayed workflow agents by message.id. Token
buckets (input / cache_read / cache_creation / output) are mutually exclusive.
Prices from scripts/pricing.toml. No network, no deps, metadata only — never reads
externalized tool-result payloads.

  claude_session_stats.py [--by day|project|model] [--json] [--root DIR]
  claude_session_stats.py --self-test     # hermetic, no real data
"""
from __future__ import annotations
import argparse, json, sys, tomllib
from collections import defaultdict
from pathlib import Path

DEFAULT_ROOT = Path.home() / ".claude" / "projects"
PRICING = Path(__file__).resolve().parent / "pricing.toml"


def iter_events(root: Path):
    """Yield one dict per assistant message with usage. Tolerates schema drift
    (skips unparseable lines). sidechain = nested subagent/agent transcript."""
    for jf in root.rglob("*.jsonl"):
        sidechain = "/subagents/" in str(jf) or jf.name.startswith("agent-")
        try:
            text = jf.read_text(errors="ignore")
        except OSError:
            continue
        for line in text.splitlines():
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(row, dict):
                continue
            msg = row.get("message") or {}
            usage = msg.get("usage") or {}
            if not usage:
                continue
            yield {
                "model": msg.get("model") or row.get("model") or "unknown",
                "input": usage.get("input_tokens", 0) or 0,
                "cache_read": usage.get("cache_read_input_tokens", 0) or 0,
                "cache_creation": usage.get("cache_creation_input_tokens", 0) or 0,
                "output": usage.get("output_tokens", 0) or 0,
                "ts": (row.get("timestamp") or "")[:10],
                "project": jf.parent.name,
                "sidechain": bool(row.get("isSidechain") or sidechain),
                "msg_id": msg.get("id"),
            }


def dedupe(events):
    """Drop replayed workflow rows that share a message.id (keep first)."""
    seen, out = set(), []
    for e in events:
        mid = e.get("msg_id")
        if mid and mid in seen:
            continue
        if mid:
            seen.add(mid)
        out.append(e)
    return out


def price(e: dict, pricing: dict) -> float:
    # Live logs carry date-suffixed model ids (claude-haiku-4-5-20251001); match the
    # longest pricing key that is a prefix. Unknown models price 0 (add them to pricing.toml).
    model = e["model"]
    p = pricing.get(model)
    if p is None:
        keys = sorted((k for k in pricing if model.startswith(k)), key=len, reverse=True)
        p = pricing[keys[0]] if keys else None
    if not p:
        return 0.0
    return (e["input"] * p["input"] + e["output"] * p["output"]
            + e["cache_read"] * p["cache_read"]
            + e["cache_creation"] * p["cache_write"]) / 1_000_000


def aggregate(events, pricing, by="day"):
    key = {"day": "ts", "project": "project", "model": "model"}[by]
    agg = defaultdict(lambda: {"input": 0, "cache_read": 0, "cache_creation": 0,
                               "output": 0, "cost_usd": 0.0, "msgs": 0})
    for e in events:
        b = agg[e.get(key) or "?"]
        for k in ("input", "cache_read", "cache_creation", "output"):
            b[k] += e[k]
        b["cost_usd"] += price(e, pricing)
        b["msgs"] += 1
    return {k: {**v, "cost_usd": round(v["cost_usd"], 4)} for k, v in sorted(agg.items())}


def load_pricing(path: Path) -> dict:
    with open(path, "rb") as f:
        d = tomllib.load(f)
    d.pop("last_reviewed", None)
    return d


def _self_test() -> int:
    import tempfile
    ok = []
    pricing = {"m": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75}}
    # pure-logic: dedupe + pricing + aggregation
    evs = [
        {"model": "m", "input": 1_000_000, "cache_read": 0, "cache_creation": 0, "output": 0,
         "ts": "2026-06-01", "project": "p", "sidechain": False, "msg_id": "a"},
        {"model": "m", "input": 0, "cache_read": 0, "cache_creation": 0, "output": 1_000_000,
         "ts": "2026-06-01", "project": "p", "sidechain": True, "msg_id": "a"},   # dup id
        {"model": "m", "input": 0, "cache_read": 0, "cache_creation": 0, "output": 1_000_000,
         "ts": "2026-06-02", "project": "p", "sidechain": True, "msg_id": "b"},
    ]
    ok.append(("dedupe by msg_id", len(dedupe(evs)) == 2))
    ok.append(("price input/MTok", abs(price(evs[0], pricing) - 3.0) < 1e-9))
    ok.append(("price output/MTok", abs(price(evs[2], pricing) - 15.0) < 1e-9))
    agg = aggregate(dedupe(evs), pricing, by="day")
    ok.append(("aggregate groups by day", set(agg) == {"2026-06-01", "2026-06-02"}))
    ok.append(("aggregate sums cost", abs(agg["2026-06-01"]["cost_usd"] - 3.0) < 1e-9))
    # fixture-based: iter_events parses JSONL, tolerates a garbage line, flags sidechain,
    # dedupes the replayed message.id across main + subagent transcripts.
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "proj").mkdir()
        (root / "proj" / "main.jsonl").write_text(
            json.dumps({"message": {"id": "a", "model": "m", "usage": {"input_tokens": 10}},
                        "timestamp": "2026-06-01T00:00:00Z"}) + "\n"
            + "this is not json — drift tolerance\n"
            + json.dumps({"message": {"id": "a", "model": "m", "usage": {"output_tokens": 5}}}) + "\n")
        (root / "proj" / "subagents").mkdir()
        (root / "proj" / "subagents" / "agent-1.jsonl").write_text(
            json.dumps({"message": {"id": "b", "model": "m", "usage": {"output_tokens": 7}},
                        "isSidechain": True}) + "\n")
        evs2 = list(iter_events(root))
        ok.append(("iter_events skips garbage + reads both files", len(evs2) == 3))
        ok.append(("iter_events dedupes to 2", len(dedupe(evs2)) == 2))
        ok.append(("iter_events flags sidechain", any(e["sidechain"] for e in evs2)))
    for label, passed in ok:
        print(f"  {'PASS' if passed else 'FAIL'}  {label}")
    return 0 if all(p for _, p in ok) else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Claude session spend reader (stdlib)")
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    ap.add_argument("--by", choices=["day", "project", "model"], default="day")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        return _self_test()
    pricing = load_pricing(PRICING)
    out = aggregate(dedupe(list(iter_events(args.root))), pricing, by=args.by)
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        total = sum(v["cost_usd"] for v in out.values())
        for k, v in out.items():
            print(f"{k:24} ${v['cost_usd']:>9.4f}  {v['msgs']:>5} msgs  "
                  f"in={v['input']} out={v['output']} cr={v['cache_read']} cc={v['cache_creation']}")
        print(f"{'TOTAL':24} ${total:>9.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
