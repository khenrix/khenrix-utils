#!/usr/bin/env python3
"""mcp_merge.py — stdlib merge for agy's ~/.gemini/config/mcp_config.json.

Adds missing MCP servers without clobbering; preserves unknown top-level keys;
stops loudly on a same-name-but-different-command collision. Atomic write.
Tests: `--self-test`.
"""
from __future__ import annotations
import argparse, json, os, sys, tempfile
from pathlib import Path

SERVERS_KEY = "mcpServers"


class MergeConflict(Exception):
    pass


def merge(existing: dict, additions: dict) -> dict:
    if not isinstance(existing, dict):
        raise ValueError("existing config must be a JSON object")
    out = json.loads(json.dumps(existing))          # deep copy, preserves unknown keys
    dst = out.setdefault(SERVERS_KEY, {})
    for name, spec in additions.get(SERVERS_KEY, {}).items():
        if name in dst:
            if dst[name] != spec:
                raise MergeConflict(
                    f"server '{name}' already exists with a different definition; refusing to clobber")
            continue                                 # identical → idempotent no-op
        dst[name] = spec
    return out


def write_merged(path: Path, additions: dict) -> dict:
    existing = json.loads(path.read_text()) if path.exists() else {}
    merged = merge(existing, additions)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(merged, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)                        # atomic
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return merged


def _self_test() -> int:
    ok = []
    base = {"mcpServers": {"a": {"command": "x"}}, "theme": "dark"}
    add = {"mcpServers": {"b": {"command": "y"}}}
    m = merge(base, add)
    ok.append(("adds new server", "b" in m["mcpServers"]))
    ok.append(("keeps existing server", "a" in m["mcpServers"]))
    ok.append(("preserves unknown top-level key", m.get("theme") == "dark"))
    ok.append(("does not mutate the input", "b" not in base["mcpServers"]))
    m2 = merge(base, {"mcpServers": {"a": {"command": "x"}}})
    ok.append(("identical re-add is idempotent (no error)", m2["mcpServers"]["a"]["command"] == "x"))
    conflict = False
    try:
        merge(base, {"mcpServers": {"a": {"command": "DIFFERENT"}}})
    except MergeConflict:
        conflict = True
    ok.append(("different command on same name -> MergeConflict", conflict))
    malformed = False
    try:
        merge("not a dict", add)  # type: ignore[arg-type]
    except (TypeError, ValueError, AttributeError):
        malformed = True
    ok.append(("malformed existing -> raises", malformed))
    # atomic write round-trip
    _d = Path(tempfile.mkdtemp())
    _p = _d / "mcp_config.json"
    write_merged(_p, {"mcpServers": {"z": {"command": "q"}}})
    rt = json.loads(_p.read_text())
    ok.append(("write_merged creates file with the server", rt["mcpServers"]["z"]["command"] == "q"))
    write_merged(_p, {"mcpServers": {"z": {"command": "q"}}})   # idempotent second write
    ok.append(("second identical write_merged is a no-op", json.loads(_p.read_text()) == rt))
    _p.unlink()
    _d.rmdir()
    failed = [n for n, p in ok if not p]
    for n, p in ok:
        print(f"  {'ok' if p else 'FAIL'}  {n}")
    print(f"mcp_merge self-test: {len(ok) - len(failed)}/{len(ok)} passed")
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return _self_test()
    ap.error("no action given")
    return 2


if __name__ == "__main__":
    sys.exit(main())
