#!/usr/bin/env python3
"""Facade: the llm-council engine now lives at <root>/lib/council/engine.py (repo:
shared/lib/council/engine.py; rendered plugin: <plugin>/lib/council/engine.py — the
same relative expression from this file either way).

sys.modules-swap, NOT `from ... import *`: consumers monkeypatch `fanout.run_member`
and expect `fanout.run_provider` to see it, `--self-test` reads underscore state, and
`_write_receipt` shells this file as a program. Only module identity preserves all
three (spec 2026-07-30-llm-forge-design.md §17).
"""
import sys
from pathlib import Path

_LIB = Path(__file__).resolve().parents[3] / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import council.engine as _engine          # noqa: E402

sys.modules[__name__] = _engine

if __name__ == "__main__":
    sys.exit(_engine.main())
