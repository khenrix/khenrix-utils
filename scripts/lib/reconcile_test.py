#!/usr/bin/env python3
"""Hermetic tests for reconcile.py overlay/instruction logic (no CLI, no tokens)."""
from __future__ import annotations
import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import reconcile  # noqa: E402


def _caps(tmp: Path, overlays: dict) -> dict:
    (tmp / "house-style.md").write_text(
        f"{reconcile.MANAGED_BEGIN}\nHOUSE\n{reconcile.MANAGED_END}\n")
    for cli, fn in overlays.items():
        p = tmp / fn
        p.parent.mkdir(parents=True, exist_ok=True)   # fn may be "overlays/claude.md"
        p.write_text(f"OVERLAY-{cli.upper()}\n")
    return {"_dir": tmp,
            "instructions": {"source": "house-style.md",
                             "overlays": overlays,
                             "targets": {"claude": str(tmp / "CLAUDE.md"),
                                         "codex": str(tmp / "AGENTS.md")}}}


def run() -> int:
    ok = []
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        caps = _caps(tmp, {"claude": "overlays/claude.md"})
        bc = reconcile.managed_block(caps, "claude")
        bx = reconcile.managed_block(caps, "codex")
        ok.append(("overlay injected for claude", "OVERLAY-CLAUDE" in bc and "HOUSE" in bc))
        ok.append(("no overlay for codex", "OVERLAY" not in bx and "HOUSE" in bx))
        ok.append(("overlay inside markers",
                   bc.startswith(reconcile.MANAGED_BEGIN) and bc.rstrip().endswith(reconcile.MANAGED_END)))
        ok.append(("codex block != claude block", bc != bx))
        ok.append(("no cli arg == no overlay", reconcile.managed_block(caps) == bx))
        # no-marker source → markers injected (idempotency contract)
        (tmp / "nomarker.md").write_text("RAW\n")
        caps2 = {"_dir": tmp, "instructions": {"source": "nomarker.md", "overlays": {}, "targets": {}}}
        bn = reconcile.managed_block(caps2, "claude")
        ok.append(("no-marker injects markers",
                   bn.startswith(reconcile.MANAGED_BEGIN) and "RAW" in bn
                   and bn.rstrip().endswith(reconcile.MANAGED_END)))
    for label, passed in ok:
        print(f"  {'PASS' if passed else 'FAIL'}  {label}")
    return 0 if all(p for _, p in ok) else 1


if __name__ == "__main__":
    sys.exit(run())
