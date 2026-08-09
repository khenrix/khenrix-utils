"""Runtime configuration for wiki-sync.

Environment-specific values (vault path, Chrome profile, caps) live in a JSON file
under the XDG state dir — NOT hardcoded in adapters, so the same code runs on a
backup machine or a renamed Chrome profile by editing config, not source. Missing
file → sane defaults for this machine.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path


def _default_state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "khenrix-wiki-sync"


def _default_chrome_profile() -> str:
    """WSL path to the live Chrome Bookmarks JSON (read directly — no manual export).

    DISCOVERED, NOT HARDCODED. This returned one machine's literal
    `/mnt/c/Users/<name>/.../Profile 3/Bookmarks`, so on every other machine — including
    this one, whose Windows user and profile are both different — `probe` reported
    `bookmarks: false` and the whole channel silently went unavailable. A default that is
    right on exactly one machine is a default that is wrong.

    Prefers `Default` because that is the profile Chrome creates first, then falls back to
    the largest `Profile N` Bookmarks file: size is the best available proxy for "the one
    actually in use" without opening any of them. Returns the conventional path unchanged
    when nothing is found, so the caller's `is_file()` probe still reports honestly rather
    than this raising at import time.
    """
    candidates = sorted(Path("/mnt/c/Users").glob(
        "*/AppData/Local/Google/Chrome/User Data/*/Bookmarks"))
    if not candidates:
        return "/mnt/c/Users/Default/AppData/Local/Google/Chrome/User Data/Default/Bookmarks"
    default_profiles = [c for c in candidates if c.parent.name == "Default"]
    pick = max(default_profiles or candidates, key=lambda c: c.stat().st_size)
    return str(pick)


@dataclass
class Config:
    state_dir: Path = field(default_factory=_default_state_dir)
    vault: Path = field(default_factory=lambda: Path.home() / "git" / "obsidian-vault")
    chrome_profile: str = field(default_factory=_default_chrome_profile)
    instagram_export_dir: str = ""     # path to an unzipped Meta "Download your info" export
    enabled_sources: list = field(default_factory=lambda: ["chrome-bookmarks",
                                                           "instagram-export"])
    instagram_live_optin: bool = False  # live enumeration is off unless explicitly enabled
    per_host_cap: int = 5               # standard-pass fetches per host per run
    deep_cap: int = 10                  # deep (/watch) fetches per run

    @property
    def ledger_path(self) -> Path:
        return Path(self.state_dir) / "ledger.db"


_PATH_FIELDS = {"state_dir", "vault"}


def load_config(path) -> Config:
    """Load config from `path`, overlaying any present keys on the defaults. A missing
    file yields all-defaults (this machine's values)."""
    cfg = Config()
    p = Path(path)
    if p.is_file():
        data = json.loads(p.read_text())
        known = {f.name for f in fields(Config)}
        for k, v in data.items():
            if k in known:
                setattr(cfg, k, Path(v) if k in _PATH_FIELDS else v)
    return cfg
