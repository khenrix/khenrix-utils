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
    if "HOME" in source:
        home = Path(source["HOME"])
    elif os.name == "nt":
        if "USERPROFILE" in source:
            home = Path(source["USERPROFILE"])
        elif "HOMEPATH" in source:
            home = Path(source.get("HOMEDRIVE", "")) / source["HOMEPATH"]
        else:
            raise RuntimeError("native home is unavailable in the supplied environment")
    else:
        # Path.home() would consult this process's HOME, not the child mapping.
        import pwd
        try:
            home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        except KeyError as error:
            raise RuntimeError("native account home is unavailable") from error
    return NativeRoots(home, Path(source.get("CODEX_HOME", home / ".codex")),
                       Path(source.get("CLAUDE_CONFIG_DIR", home / ".claude")),
                       home / ".gemini/antigravity-cli")
