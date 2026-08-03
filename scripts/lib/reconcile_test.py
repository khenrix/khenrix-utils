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


def _read_json_object_checks(tmp: Path) -> list:
    """A config file that exists and cannot be read as an object must REFUSE, not answer {}.

    {} is what an ABSENT file returns, and every caller turns "absent" into a full ADD list
    that the apply path then writes over the whole file. Only the first input below is a
    decode error; the rest used to reach the same {} by a different line, or by no line at
    all (`[1, 2]` and `"hello"` PARSE — the old isinstance fallback flattened them).
    """
    ok = []

    def reads(p: Path):
        """read_json_object(p), or a marker string — so a refusal fails a check cleanly
        rather than aborting the whole run with a traceback."""
        try:
            return reconcile.read_json_object(p)
        except OSError as e:
            return f"UNREADABLE: {e}"
        except reconcile.ReconcileReadError as e:
            return f"REFUSED: {e}"

    for name, body in (("truncated", '{"a": 1'),
                       ("array", "[1, 2]"),
                       ("string", '"hello"'),
                       ("null", "null"),
                       ("whitespace-only", "  \n\t\n"),
                       # A BOM is bytes, so this file is NOT the empty one below. Refusing it
                       # is the deliberate answer: tolerating the encoding would be a separate
                       # change, and until someone makes it the file must not read as absent.
                       ("bom", "﻿" + '{"a": 1}')):
        p = tmp / f"{name}.json"
        p.write_text(body)
        res = reads(p)
        ok.append((f"{name} refuses",
                   isinstance(res, str) and res.startswith("REFUSED:") and str(p) in res))
    # The two states that are genuinely "nothing here" still answer {}.
    ok.append(("absent is {}", reads(tmp / "nope.json") == {}))
    empty = tmp / "empty.json"
    empty.write_text("")
    ok.append(("zero-length is {}", reads(empty) == {}))
    # A path that exists and cannot be read is not an absent one either. A directory is the
    # root-proof stand-in for a permission error (root reads a chmod-000 file; nobody
    # read_text()s a directory) and both arrive as OSError.
    unreadable = tmp / "adir.json"
    unreadable.mkdir()
    ok.append(("unreadable path is not read as absent", reads(unreadable) != {}))
    # And a real object still round-trips.
    good = tmp / "good.json"
    good.write_text('{"mcpServers": {}}')
    ok.append(("object reads", reads(good) == {"mcpServers": {}}))

    # agy's MCP config had a second copy of the same reader, and apply_mcp rebuilds the whole
    # file from it — the identical destructive path one function over, so it refuses too.
    bad = tmp / "mcp_config.json"
    bad.write_text('{"mcpServers": {')
    orig_path = reconcile.agy_mcp_path
    reconcile.agy_mcp_path = lambda: bad
    try:
        try:
            agy = reconcile.agy_mcp_load()
        except reconcile.ReconcileReadError:
            agy = "REFUSED"
        ok.append(("agy_mcp_load refuses an unparseable config", agy == "REFUSED"))
        bad.write_text('{"mcpServers": {"a": {"command": "x"}}}')
        ok.append(("agy_mcp_load still reads a good config",
                   reconcile.agy_mcp_load() == {"mcpServers": {"a": {"command": "x"}}}))
    finally:
        reconcile.agy_mcp_path = orig_path
    return ok


def run() -> int:
    ok = []
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        ok.extend(_read_json_object_checks(tmp))
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
