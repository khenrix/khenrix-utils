#!/usr/bin/env python3
"""Keep a local, always-current checkout of the upstream sources we reason about.

WHY THIS EXISTS
    Three convergence cycles on `skill-tuneup` found that nearly every blocking defect was
    documentation asserting behaviour the code does not have. The cure is not more careful
    prose — it is reading the thing itself. Where a CLI's source is public, an answer about
    its flags, exit codes or output shape is a LOOKUP, not an inference.

    Pulling is part of the ritual, not a setup step: a stale checkout is a confident wrong
    answer with a plausible citation, which is worse than admitting you do not know.

WHAT IS IN SCOPE
    Public repositories, fetched from their canonical upstream. Nothing else. Leaked or
    mirrored copies of proprietary source are out of scope no matter how convenient — for
    any vendor. When a CLI is closed (agy, and claude's compiled binary), the licensed
    install on this machine plus a live probe IS the primary source; see PROBE_FIRST below.

USAGE
    python3 scripts/cli_sources.py           # clone-or-pull everything, then report
    python3 scripts/cli_sources.py --status  # report only, never touch the network
    python3 scripts/cli_sources.py --self-test
"""
from __future__ import annotations
import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path.home() / ".cache" / "khenrix-utils" / "cli-sources"

# name -> (url, why we read it). Canonical upstreams only.
SOURCES: dict[str, tuple[str, str]] = {
    "codex": (
        "https://github.com/openai/codex.git",
        "FULL Rust source. codex-rs/exec/src/exec_events.rs is the authoritative "
        "definition of the JSONL events `codex exec --json` emits — the ground truth for "
        "fanout.py's extract_codex_json.",
    ),
    "claude-code": (
        "https://github.com/anthropics/claude-code.git",
        "NOT the CLI source (the binary is compiled and not published). Carries the "
        "CHANGELOG, docs and examples — the authoritative record of behaviour changes "
        "per version. Pair it with the licensed local install's sdk-tools.d.ts.",
    ),
    "antigravity-for-claude-code": (
        "https://github.com/yuting0624/antigravity-for-claude-code.git",
        "No public agy source exists, so this actively-maintained wrapper is the best "
        "available written record of agy's headless behaviour — its documented quirks are "
        "dated and version-stamped. Corroborate against a live probe before trusting one.",
    ),
}

# Closed-source CLIs: what to read INSTEAD of guessing. Paths are resolved at read time.
PROBE_FIRST = {
    "claude": "`claude --help`, the CHANGELOG above, and the licensed package's "
              "sdk-tools.d.ts (ships real type declarations for the SDK/tool surface).",
    "agy": "`agy --help` and a live `--output-format json` run. agy writes its real "
           "failure reason to --log-file; in JSON mode stderr is EMPTY (measured), so "
           "there is nothing there to scan.",
}


def _git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def sync(name: str, url: str, root: Path = ROOT) -> tuple[bool, str]:
    """Clone if absent, otherwise fetch+reset to the remote head. Returns (ok, detail)."""
    dest = root / name
    if not (dest / ".git").is_dir():
        root.mkdir(parents=True, exist_ok=True)
        # --filter=blob:none: we read a handful of files out of repos up to ~100 MB;
        # fetching every blob of history to grep one enum is pure waste.
        r = _git("clone", "--filter=blob:none", url, str(dest))
        return (r.returncode == 0, "cloned" if r.returncode == 0 else r.stderr.strip()[:200])
    r = _git("fetch", "--quiet", "origin", cwd=dest)
    if r.returncode != 0:
        return False, f"fetch failed: {r.stderr.strip()[:200]}"
    head = _git("rev-parse", "HEAD", cwd=dest).stdout.strip()
    branch = _git("symbolic-ref", "--short", "refs/remotes/origin/HEAD", cwd=dest).stdout.strip()
    ref = branch or "origin/main"
    r = _git("reset", "--hard", "--quiet", ref, cwd=dest)
    if r.returncode != 0:
        return False, f"reset failed: {r.stderr.strip()[:200]}"
    new = _git("rev-parse", "HEAD", cwd=dest).stdout.strip()
    return True, "already current" if new == head else f"updated {head[:8]}..{new[:8]}"


def status(root: Path = ROOT) -> list[str]:
    lines = []
    for name in SOURCES:
        d = root / name
        if not (d / ".git").is_dir():
            lines.append(f"  {name:30} MISSING — run `make cli-sources`")
            continue
        when = _git("log", "-1", "--format=%ci", cwd=d).stdout.strip()
        sha = _git("rev-parse", "--short", "HEAD", cwd=d).stdout.strip()
        lines.append(f"  {name:30} {sha}  upstream commit {when}")
    return lines


def _self_test() -> int:
    ok = []
    ok.append(("every source has a url and a stated reason to read it",
               all(u.startswith("https://") and len(w) > 40 for u, w in SOURCES.values())))
    ok.append(("sources are canonical github upstreams, never mirrors",
               all(u.startswith("https://github.com/") for u, _ in SOURCES.values())))
    # The cache lives OUTSIDE the repo: these checkouts are tens of MB and are not ours to
    # version. A path inside the repo would land in `git add -A` and render.py's sweep.
    ok.append(("the cache is outside the khenrix-utils checkout",
               ".cache" in str(ROOT) and "khenrix-utils/scripts" not in str(ROOT)))
    ok.append(("closed-source CLIs name a probe-first alternative",
               set(PROBE_FIRST) == {"claude", "agy"} and all(PROBE_FIRST.values())))
    ok.append(("status() never touches the network on a missing checkout",
               "MISSING" in "\n".join(status(Path("/nonexistent-cli-sources")))))
    for label, good in ok:
        print(f"  {'PASS' if good else 'FAIL'}  {label}")
    bad = sum(1 for _, g in ok if not g)
    print(f"cli_sources self-test: {len(ok) - bad}/{len(ok)} passed")
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--status", action="store_true", help="report only; no network")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return _self_test()
    if a.status:
        print(f"cli sources @ {ROOT}")
        print("\n".join(status()))
        return 0
    print(f"syncing upstream sources -> {ROOT}")
    failed = 0
    for name, (url, _why) in SOURCES.items():
        ok, detail = sync(name, url)
        print(f"  {'✓' if ok else '✗'} {name:30} {detail}")
        failed += 0 if ok else 1
    print("\n".join([""] + status()))
    print("\nclosed source — probe, don't guess:")
    for cli, how in PROBE_FIRST.items():
        print(f"  {cli}: {how}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
