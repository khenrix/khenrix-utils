#!/usr/bin/env python3
"""Validate local preparation recipes; execution requires an external owner handoff."""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared/lib"))
from fanout.container_source import ReleaseSourceBinding, host_runtime_identity, release_source_binding
from fanout.preparation import MAX_RECIPE_BYTES, PreparationError, PreparationRecipe, _json, _safe_root


def _recipe_bytes(path: Path) -> bytes:
    _safe_root(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_RECIPE_BYTES:
            raise PreparationError("preparation recipe file is unsafe or oversized")
        raw = os.read(fd, MAX_RECIPE_BYTES + 1)
        after = os.fstat(fd)
        if (len(raw) != before.st_size or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
            raise PreparationError("preparation recipe changed during reading")
        return raw
    finally:
        os.close(fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recipe", type=Path)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--internal-stage", choices=("source-binding", "host-runtime"), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        source = _safe_root(args.source_root)
        binding = release_source_binding(source)
        if args.internal_stage:
            if args.recipe or args.validate_only:
                parser.error("internal stage accepts only a source root")
            expected = ReleaseSourceBinding.from_dict(_json(sys.stdin.buffer.read(MAX_RECIPE_BYTES + 1)))
            if binding != expected:
                raise PreparationError("preparation source binding changed")
            value = binding.to_dict() if args.internal_stage == "source-binding" else dataclasses.asdict(host_runtime_identity())
            print(json.dumps(value, sort_keys=True, separators=(",", ":")))
            return 0
        if args.recipe is None:
            parser.error("--recipe is required")
        recipe = PreparationRecipe.from_bytes(_recipe_bytes(args.recipe))
        if recipe.release_binding.to_dict() != binding.to_dict():
            raise PreparationError("preparation recipe source binding is stale")
        if not args.validate_only:
            raise PreparationError("preparation launch requires an authenticated owner capability handoff; use the reusable API")
        print(json.dumps({"status": "validated", "certification": False,
                          "release_binding": binding.to_dict(), "recipe_sha256": recipe.digest}, sort_keys=True))
        return 0
    except PreparationError as error:
        print(f"local preparation refused: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError):
        print("local preparation refused: invalid or unavailable input/source", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
