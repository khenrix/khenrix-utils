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

# NUL-framed git log format: a leading NUL (%x00) marks each commit record, %H is the
# hash. Paired with `-z`, pathnames are NUL-terminated and never quoted, so the parse is
# hash-length-agnostic (SHA-1 AND SHA-256) and immune to paths containing spaces,
# newlines, or a name that happens to look like a hash. NUL is the only byte a git path
# cannot contain — a hash-length or RS/US-sentinel heuristic is NOT collision-safe.
LOG_FORMAT = "%x00%H"


def _key(rel: str, depth: int) -> str:
    parts = Path(rel).parts
    return "/".join(parts[:depth]) if len(parts) > 1 else parts[0]


def parse_commits(raw: str) -> list[list[str]]:
    """Parse `git log -z --name-only --format=%x00%H` into a list of file-lists.
    Splitting the raw output on NUL yields: '' (leading), HASH, '\\n'+file1, file2, …,
    '' (next record marker), HASH2, …. A record boundary (empty token) means the next
    token is a hash; everything after it up to the next boundary is that commit's files."""
    commits: list[list[str]] = []
    cur: list[str] = []
    in_commit = False       # true once this record's hash is seen (a commit exists)
    after_marker = False    # the next non-empty token is a hash
    first_file = False      # the first file token carries git's format newline; later ones don't
    for tok in raw.split("\0"):
        if tok == "":                       # record marker: leading, between commits, trailing
            if in_commit:
                commits.append(cur)
            cur, in_commit, after_marker, first_file = [], False, True, False
        elif after_marker:                   # the hash — not needed for co-change, skip it
            after_marker, in_commit, first_file = False, True, True
        else:
            # Strip the header newline ONLY from the first file — stripping every token
            # would corrupt a later path that legitimately begins with '\n'.
            f = tok[1:] if (first_file and tok.startswith("\n")) else tok
            first_file = False
            if f:
                cur.append(f)
    if in_commit:                            # a final record not closed by a trailing NUL
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


def _git(root: Path, *args, check: bool = True) -> str:
    """Run a git command under `root`. With check=True, surface failures loudly and exit:
    a non-repo/bad-root otherwise returns empty stdout that reads as a legitimate 'nothing
    found', silently voiding the analysis. With check=False, a nonzero exit is tolerated
    (empty stdout returned) — used for `git log`, which fails on a valid but commit-less
    repo where LOC still works and co-change simply doesn't exist yet."""
    # surrogateescape: a valid git path can contain non-UTF-8 bytes; round-trip them
    # instead of crashing the strict UTF-8 decode (the `-z` framing is byte-exact).
    cp = subprocess.run(["git", "-C", str(root), *args],
                        capture_output=True, text=True, errors="surrogateescape")
    if cp.returncode != 0 and check:
        tail = (cp.stderr or "").strip().splitlines()[-1:] or ["(no stderr)"]
        sys.stderr.write(f"chunk-map: `git {args[0]}` failed under {root} — {tail[0]}\n")
        sys.exit(cp.returncode)   # a non-repo/bad-root empty result must NOT read as "nothing found"
    return cp.stdout


def loc_by_dir(root: Path, depth: int) -> Counter:
    rollup: Counter = Counter()
    # -z: NUL-separated, unquoted paths — survives spaces/newlines/unicode in filenames.
    for rel in _git(root, "ls-files", "-z").split("\0"):
        if not rel:
            continue
        try:
            text = (root / rel).read_text(errors="ignore")
            # O(1)-memory line count (no splitlines() list): trailing-newline-aware,
            # empty file → 0 lines.
            rollup[_key(rel, depth)] += text.count("\n") + (1 if text and not text.endswith("\n") else 0)
        except OSError:
            continue
    return rollup


def _rec(h: str, *files: str) -> str:
    """Build one NUL-framed record as `git log -z --format=%x00%H` emits it:
    \\0 + hash + \\0 + \\n<file1> + \\0 + <file2> + \\0 …"""
    out = "\0" + h + "\0"
    for i, f in enumerate(files):
        out += ("\n" + f if i == 0 else f) + "\0"
    return out


def _self_test() -> int:
    ok = []
    # SHA-1 (40-hex), SHA-256 (64-hex), a hash-shaped filename, and a path with a space —
    # all of which the old hash-length heuristic mis-parsed.
    raw = (_rec("a" * 40, "src/ingest/a.py", "src/dedupe/b.py")
           + _rec("b" * 64, "src/ingest/a.py", "src/dedupe/c.py")   # SHA-256 commit
           + _rec("c" * 40, "src/ingest/" + "d" * 40 + ".bin")      # 40-hex FILENAME
           + _rec("d" * 40, "src/my docs/e.py"))                    # space in path
    commits = parse_commits(raw)
    ok.append(("parse_commits counts 4 (SHA-1 + SHA-256 + hex-name + spaced)", len(commits) == 4))
    ok.append(("parse_commits files per commit", commits[0] == ["src/ingest/a.py", "src/dedupe/b.py"]))
    ok.append(("SHA-256 (64-hex) commit parsed, not dropped", commits[1] == ["src/ingest/a.py", "src/dedupe/c.py"]))
    ok.append(("40-hex FILENAME kept as a file, not read as a header",
               commits[2] == ["src/ingest/" + "d" * 40 + ".bin"]))
    ok.append(("path with a space preserved intact", commits[3] == ["src/my docs/e.py"]))
    # A non-first file whose path begins with a newline must NOT lose that byte (only the
    # header newline before file #1 is git's, and only it is stripped).
    nlc = parse_commits(_rec("f" * 40, "src/first.py", "\nsrc/weird.py"))
    ok.append(("non-first newline-prefixed path preserved", nlc == [["src/first.py", "\nsrc/weird.py"]]))
    pairs = cochange_pairs(commits, depth=2)
    ok.append(("co-change pairs ingest~dedupe = 2", pairs[("src/dedupe", "src/ingest")] == 2))
    ok.append(("single-dir commits yield no pair (only the 2 ingest~dedupe)", sum(pairs.values()) == 2))
    ok.append(("empty commit yields no pair", cochange_pairs(parse_commits(_rec("e" * 40)), 2) == Counter()))
    ok.append(("_key depth-2", _key("src/ingest/a.py", 2) == "src/ingest"))
    ok.append(("_key shallow file", _key("README.md", 2) == "README.md"))
    # A git failure (non-repo) must exit nonzero, not emit an empty "nothing found" result.
    exited = None
    try:
        main(["--root", "/nonexistent-xyz-not-a-repo", "--commits", "1"])
    except SystemExit as e:
        exited = e.code
    ok.append(("git failure exits nonzero (no silent empty result)", bool(exited)))
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
    loc = loc_by_dir(args.root, args.depth)   # ls-files (check=True) → non-repo is fatal here
    # Distinguish "no commits yet" (valid — LOC stands, co-change simply doesn't exist)
    # from a real `git log` failure (corruption, permissions), which must stay fatal like
    # ls-files. A commit-less repo has an unborn HEAD; only then skip log.
    has_head = _git(args.root, "rev-parse", "--verify", "-q", "HEAD", check=False).strip()
    if has_head:
        log = _git(args.root, "log", "-z", f"-n{args.commits}", "--name-only",
                   f"--format={LOG_FORMAT}")   # check=True: a real log error is fatal
    else:
        log = ""                               # unborn HEAD → no history, not an error
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
