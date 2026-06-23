#!/usr/bin/env python3
"""LOC rollups + git temporal co-change coupling — raw signals for chunk boundaries.

Stdlib only. Co-change (files that change together) is the boundary signal an import
graph misses; this surfaces the most-coupled directory pairs over recent history.

  codebase_stats.py [--root .] [--commits 400] [--depth 2] [--json]
  codebase_stats.py --self-test     # hermetic logic tests, no git
"""
from __future__ import annotations
import argparse, json, subprocess, sys
from collections import Counter
from itertools import combinations
from pathlib import Path

HEX = set("0123456789abcdef")


def _key(rel: str, depth: int) -> str:
    parts = Path(rel).parts
    return "/".join(parts[:depth]) if len(parts) > 1 else parts[0]


def parse_commits(log: str) -> list[list[str]]:
    """`git log --name-only --pretty=format:%H` → list of file-lists, one per commit.
    Layout: a 40-hex hash line, then file lines, blank lines between commits."""
    commits, cur, started = [], [], False
    for line in log.splitlines():
        s = line.strip()
        if len(s) == 40 and all(c in HEX for c in s):       # new commit header
            if started:
                commits.append(cur)
            cur, started = [], True
        elif s and started:
            cur.append(s)
    if started:
        commits.append(cur)
    return commits


def cochange_pairs(commits: list[list[str]], depth: int) -> Counter:
    """Count how often two dirs (at `depth`) appear in the same commit."""
    pairs: Counter = Counter()
    for files in commits:
        dirs = sorted({_key(f, depth) for f in files})
        for a, b in combinations(dirs, 2):
            pairs[(a, b)] += 1
    return pairs


def _git(root: Path, *args) -> str:
    return subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True).stdout


def loc_by_dir(root: Path, depth: int) -> Counter:
    rollup: Counter = Counter()
    for rel in _git(root, "ls-files").split():
        try:
            rollup[_key(rel, depth)] += (root / rel).read_text(errors="ignore").count("\n") + 1
        except OSError:
            continue
    return rollup


def _self_test() -> int:
    ok = []
    log = ("a" * 40 + "\n\nsrc/ingest/a.py\nsrc/dedupe/b.py\n\n"
           + "b" * 40 + "\n\nsrc/ingest/a.py\nsrc/dedupe/c.py\n\n"
           + "c" * 40 + "\n\nsrc/ingest/a.py\n")
    commits = parse_commits(log)
    ok.append(("parse_commits counts 3", len(commits) == 3))
    ok.append(("parse_commits files per commit", commits[0] == ["src/ingest/a.py", "src/dedupe/b.py"]))
    pairs = cochange_pairs(commits, depth=2)
    ok.append(("co-change pairs ingest~dedupe = 2", pairs[("src/dedupe", "src/ingest")] == 2))
    ok.append(("single-dir commit yields no pair", sum(pairs.values()) == 2))
    ok.append(("_key depth-2", _key("src/ingest/a.py", 2) == "src/ingest"))
    ok.append(("_key shallow file", _key("README.md", 2) == "README.md"))
    for label, passed in ok:
        print(f"  {'PASS' if passed else 'FAIL'}  {label}")
    return 0 if all(p for _, p in ok) else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="LOC + git co-change signals for chunk boundaries")
    ap.add_argument("--root", type=Path, default=Path("."))
    ap.add_argument("--commits", type=int, default=400)
    ap.add_argument("--depth", type=int, default=2)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        return _self_test()
    loc = loc_by_dir(args.root, args.depth)
    log = _git(args.root, "log", f"-n{args.commits}", "--name-only", "--pretty=format:%H")
    pairs = cochange_pairs(parse_commits(log), args.depth)
    top_loc = loc.most_common(25)
    top_pairs = pairs.most_common(20)
    if args.json:
        print(json.dumps({"loc": dict(top_loc),
                          "cochange": [{"a": a, "b": b, "n": n} for (a, b), n in top_pairs]}, indent=2))
    else:
        print("LOC by dir (top 25):")
        for d, n in top_loc:
            print(f"  {n:>7}  {d}")
        print("\nMost co-changed dir pairs (candidate chunk couplings):")
        for (a, b), n in top_pairs:
            print(f"  {n:>4}×  {a}  ~  {b}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
