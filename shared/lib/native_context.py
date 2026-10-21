"""Effective native discovery roots; safe to retain without configuration contents."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True, slots=True)
class NativeRoots:
    home: Path
    codex: Path
    claude: Path
    agy: Path

    def to_dict(self) -> dict[str, str]:
        return {name: str(getattr(self, name)) for name in ("home", "codex", "claude", "agy")}


def resolve_native_roots(environment: Mapping[str, str] | None = None) -> NativeRoots:
    """Resolve overrides from the supplied environment, never ambient fallbacks."""
    source = os.environ if environment is None else environment
    home = Path(source["HOME"]) if "HOME" in source else Path.home()
    return NativeRoots(home, Path(source.get("CODEX_HOME", home / ".codex")),
                       Path(source.get("CLAUDE_CONFIG_DIR", home / ".claude")),
                       home / ".gemini/antigravity-cli")
