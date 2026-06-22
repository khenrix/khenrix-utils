#!/usr/bin/env python3
"""Self-contained HTML usage report for Claude Code sessions (stdlib only).

Reuses claude_session_stats parsing/pricing (DRY) and renders a single static HTML
file — token + cost breakdown by day / model / project, no external assets, no deps.

  session_report.py [--root DIR] [--out PATH]
  session_report.py --self-test     # hermetic, no real data
"""
from __future__ import annotations
import argparse, html, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import claude_session_stats as css  # noqa: E402

PRICING = Path(__file__).resolve().parent / "pricing.toml"


def build_report(root: Path, pricing: dict) -> dict:
    events = css.dedupe(list(css.iter_events(root)))
    total = round(sum(css.price(e, pricing) for e in events), 4)
    return {
        "total_cost_usd": total,
        "messages": len(events),
        "by_day": css.aggregate(events, pricing, by="day"),
        "by_model": css.aggregate(events, pricing, by="model"),
        "by_project": css.aggregate(events, pricing, by="project"),
    }


def _table(title: str, rows: dict) -> str:
    head = ("<tr><th>{}</th><th>cost $</th><th>msgs</th><th>input</th>"
            "<th>output</th><th>cache rd</th><th>cache cr</th></tr>").format(html.escape(title))
    body = []
    for k, v in sorted(rows.items(), key=lambda kv: -kv[1]["cost_usd"]):
        body.append("<tr><td>{}</td><td class=n>{:.4f}</td><td class=n>{}</td>"
                    "<td class=n>{}</td><td class=n>{}</td><td class=n>{}</td>"
                    "<td class=n>{}</td></tr>".format(
                        html.escape(str(k)), v["cost_usd"], v["msgs"],
                        v["input"], v["output"], v["cache_read"], v["cache_creation"]))
    return f"<h2>{html.escape(title)}</h2><table>{head}{''.join(body)}</table>"


def render_html(report: dict) -> str:
    css_style = ("body{font:14px/1.5 system-ui,sans-serif;margin:2rem;color:#16150f;background:#f7f5f1}"
                 "h1{font-size:1.5rem}h2{margin-top:2rem;font-size:1.1rem}"
                 "table{border-collapse:collapse;width:100%;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.1)}"
                 "th,td{padding:6px 10px;border-bottom:1px solid #ede9e1;text-align:left}"
                 "th{background:#16150f;color:#fff;font-size:12px}td.n{text-align:right;font-variant-numeric:tabular-nums}")
    summary = (f"<h1>Claude session usage</h1><p><b>${report['total_cost_usd']:.2f}</b> "
               f"across <b>{report['messages']}</b> assistant messages.</p>")
    parts = [summary, _table("By day", report["by_day"]),
             _table("By model", report["by_model"]), _table("By project", report["by_project"])]
    return ("<!doctype html><html><head><meta charset=utf-8>"
            f"<title>Claude session usage</title><style>{css_style}</style></head>"
            f"<body>{''.join(parts)}</body></html>")


def _self_test() -> int:
    ok = []
    report = {"total_cost_usd": 12.5, "messages": 3,
              "by_day": {"2026-06-01": {"cost_usd": 12.5, "msgs": 3, "input": 100,
                                        "output": 50, "cache_read": 0, "cache_creation": 0}},
              "by_model": {"claude-opus-4-8<x>": {"cost_usd": 12.5, "msgs": 3, "input": 100,
                                                  "output": 50, "cache_read": 0, "cache_creation": 0}},
              "by_project": {}}
    h = render_html(report)
    ok.append(("renders valid html doc", h.startswith("<!doctype html>") and h.endswith("</html>")))
    ok.append(("includes total", "$12.50" in h))
    ok.append(("escapes html in keys", "&lt;x&gt;" in h and "<x>" not in h.split("<body>")[1].replace("&lt;x&gt;", "")))
    ok.append(("empty section renders", "By project" in h))
    for label, passed in ok:
        print(f"  {'PASS' if passed else 'FAIL'}  {label}")
    return 0 if all(p for _, p in ok) else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Claude session HTML usage report (stdlib)")
    ap.add_argument("--root", type=Path, default=css.DEFAULT_ROOT)
    ap.add_argument("--out", type=Path, default=Path.home() / ".claude" / "session-report.html")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        return _self_test()
    report = build_report(args.root, css.load_pricing(PRICING))
    args.out.write_text(render_html(report))
    print(f"wrote {args.out}  (${report['total_cost_usd']:.2f}, {report['messages']} msgs)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
