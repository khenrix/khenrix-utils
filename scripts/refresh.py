#!/usr/bin/env python3
"""Refresh installed khenrix-utils plugins in every CLI from this repo.

Claude and Codex cache plugins by version (e.g. .../khenrix-utils/0.1.0/), so a
plain edit to the repo is NOT picked up until you bump the version or reinstall.
This refreshes everything in one step:

  1. re-renders the plugins (bundles capabilities.toml + house-style.md + engine)
  2. syncs each per-CLI plugin directory into its installed location(s), so the
     skill + engine the CLI actually runs match the repo — no version bump needed
  3. best-effort refresh of each CLI's marketplace metadata

Only files are copied (additive overwrite); nothing in your live CLI *config*
(MCP servers, settings) is touched — that is the khenrix-setup skill's job.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOME = Path.home()
CLIS = ("claude", "codex", "agy")

# Where each CLI keeps the installed plugin (globs, ~ already expanded).
INSTALL_GLOBS = {
    "claude": ["~/.claude/plugins/cache/khenrix-claude-marketplace/khenrix-utils/*"],
    "codex": ["~/.codex/plugins/cache/khenrix-codex-marketplace/khenrix-utils/*"],
    "agy": ["~/.gemini/config/plugins/khenrix-utils"],
}

# Best-effort native metadata refresh per CLI (failures are ignored).
# Codex local-dir marketplaces aren't Git marketplaces, so `upgrade` doesn't
# apply — the file-sync above is the refresh. agy is refreshed via re-install.
META_REFRESH = {
    "claude": ["claude", "plugin", "marketplace", "update", "khenrix-claude-marketplace"],
    "codex": None,
    "agy": None,
}


def render():
    subprocess.run([sys.executable, str(ROOT / "scripts" / "render.py")], check=True)


def installed_dirs(cli: str) -> list[Path]:
    out = []
    for g in INSTALL_GLOBS[cli]:
        base = Path(g.replace("~", str(HOME)))
        if "*" in g:
            out += [p for p in base.parent.glob(base.name) if p.is_dir()]
        elif base.is_dir():
            out.append(base)
    return out


def sync(cli: str) -> list[str]:
    src = ROOT / "marketplaces" / cli / "plugins" / "khenrix-utils"
    notes = []
    dests = installed_dirs(cli)
    if not dests:
        return [f"{cli}: not installed (run `make setup-{cli}`)"]
    for d in dests:
        shutil.copytree(src, d, dirs_exist_ok=True)
        notes.append(f"{cli}: synced → {d}")
    return notes


def meta_refresh(cli: str) -> str | None:
    cmd = META_REFRESH[cli]
    if cli == "agy":
        cmd = ["agy", "plugin", "install", str(ROOT / "marketplaces" / "agy" / "plugins" / "khenrix-utils")]
    if not cmd:
        return None
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return f"{cli}: {' '.join(cmd[:3])} → {'ok' if r.returncode == 0 else r.stderr.strip()[:80]}"
    except Exception as e:  # noqa: BLE001
        return f"{cli}: metadata refresh skipped ({e})"


def main() -> int:
    print("Rendering…")
    render()
    print("\nSyncing installed plugins…")
    for cli in CLIS:
        for note in sync(cli):
            print(f"  • {note}")
        m = meta_refresh(cli)
        if m:
            print(f"  • {m}")
    print("\n✅ Refresh complete. Restart any open CLI session to pick up changes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
