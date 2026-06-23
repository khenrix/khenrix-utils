#!/usr/bin/env python3
"""Mikado graph helper — read .mikado/plan.md, report which nodes are READY vs BLOCKED.

A node is READY when its status != "done" and every dependency is "done". Reports cycles.
Stdlib only; the graph is a fenced ```json block inside the plan file.

  mikado.py .mikado/plan.md
  mikado.py --self-test     # hermetic logic tests
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

FENCE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def parse_graph(text: str) -> dict:
    """Extract the first ```json fenced block. Raises ValueError if absent/invalid."""
    m = FENCE.search(text)
    if not m:
        raise ValueError("no ```json graph block found in plan")
    return json.loads(m.group(1))


def _status(nodes: dict, nid: str) -> str:
    return nodes.get(nid, {}).get("status", "todo")


def classify(graph: dict) -> dict:
    """Return {ready, blocked, done, cycle}. ready = not done + all deps done."""
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    # cycle detection (DFS)
    WHITE, GREY, BLACK = 0, 1, 2
    color = {nid: WHITE for nid in nodes}
    cycle = []

    def visit(nid, stack):
        if color.get(nid) == GREY:
            cycle.append(stack[stack.index(nid):] + [nid])
            return
        if color.get(nid) == BLACK or nid not in nodes:
            return
        color[nid] = GREY
        for d in nodes[nid].get("deps", []):
            visit(d, stack + [nid])
        color[nid] = BLACK

    for nid in nodes:
        visit(nid, [])

    ready, blocked, done = [], [], []
    for nid, n in nodes.items():
        if _status(nodes, nid) == "done":
            done.append(nid)
            continue
        unmet = [d for d in n.get("deps", []) if _status(nodes, d) != "done"]
        (blocked if unmet else ready).append((nid, unmet) if unmet else nid)
    return {"ready": sorted(ready), "blocked": sorted(blocked),
            "done": sorted(done), "cycle": cycle}


def _self_test() -> int:
    ok = []
    g = {"nodes": [
        {"id": "goal", "status": "todo", "deps": ["a", "b"]},
        {"id": "a", "status": "todo", "deps": ["c"]},
        {"id": "b", "status": "done", "deps": []},
        {"id": "c", "status": "done", "deps": []},
    ]}
    r = classify(g)
    ok.append(("ready = leaf with all deps done (a)", r["ready"] == ["a"]))
    ok.append(("goal blocked on a", any(nid == "goal" and "a" in unmet for nid, unmet in r["blocked"])))
    ok.append(("done lists b,c", r["done"] == ["b", "c"]))
    ok.append(("no false cycle", r["cycle"] == []))
    # all-done parent becomes ready
    g2 = {"nodes": [{"id": "p", "status": "todo", "deps": ["x"]}, {"id": "x", "status": "done", "deps": []}]}
    ok.append(("parent ready when dep done", classify(g2)["ready"] == ["p"]))
    # cycle
    gc = {"nodes": [{"id": "u", "status": "todo", "deps": ["v"]}, {"id": "v", "status": "todo", "deps": ["u"]}]}
    ok.append(("cycle detected", len(classify(gc)["cycle"]) >= 1))
    # parse fence
    txt = "# x\n\n```json\n{\"nodes\": [{\"id\": \"z\", \"status\": \"todo\", \"deps\": []}]}\n```\n## z\nnote"
    ok.append(("parse_graph reads fence", parse_graph(txt)["nodes"][0]["id"] == "z"))
    for label, passed in ok:
        print(f"  {'PASS' if passed else 'FAIL'}  {label}")
    return 0 if all(p for _, p in ok) else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Mikado graph ready/blocked reporter")
    ap.add_argument("plan", nargs="?", help="path to .mikado/plan.md")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        return _self_test()
    if not args.plan:
        sys.exit("usage: mikado.py .mikado/plan.md  (or --self-test)")
    graph = parse_graph(Path(args.plan).read_text())
    r = classify(graph)
    if r["cycle"]:
        print("✗ CYCLE detected:", " -> ".join(r["cycle"][0]))
        return 1
    print("READY (do now — independent):")
    for nid in r["ready"]:
        print(f"  • {nid}")
    print("BLOCKED:")
    for nid, unmet in r["blocked"]:
        print(f"  • {nid}  ⟵ needs {', '.join(unmet)}")
    print(f"done: {len(r['done'])}/{len(graph.get('nodes', []))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
