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

import argparse
import hashlib
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


def _tree_identity(root: Path, roster: list[str] | None = None) -> tuple[str, list[str]]:
    """Hash regular bytes + executable bits for a fixed relative-path roster."""
    problems = []
    if root.is_symlink() or not root.is_dir():
        return "", [f"unsafe or missing plugin root: {root}"]
    if roster is None:
        roster = []
        for path in sorted(root.rglob("*")):
            if "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            if path.is_symlink():
                problems.append(f"plugin tree contains a symlink: {path}")
            elif path.is_file():
                roster.append(path.relative_to(root).as_posix())
    framed = []
    for rel in roster:
        path = root / rel
        current = root
        for part in Path(rel).parts:
            current /= part
            if current.is_symlink():
                problems.append(f"plugin path crosses a symlink: {current}")
                break
        else:
            if not path.is_file():
                problems.append(f"plugin file missing after refresh: {path}")
                continue
            mode = path.stat().st_mode & 0o111
            framed.append((rel, mode, hashlib.sha256(path.read_bytes()).hexdigest()))
    payload = repr(framed).encode()
    return hashlib.sha256(payload).hexdigest(), problems


def verify_install(src: Path, dest: Path) -> str:
    source_hash, source_problems = _tree_identity(src)
    if source_problems:
        raise RuntimeError("; ".join(source_problems))
    roster = [p.relative_to(src).as_posix() for p in sorted(src.rglob("*"))
              if p.is_file() and not p.is_symlink()
              and "__pycache__" not in p.parts and p.suffix != ".pyc"]
    installed_hash, installed_problems = _tree_identity(dest, roster)
    if installed_problems:
        raise RuntimeError("; ".join(installed_problems))
    if installed_hash != source_hash:
        raise RuntimeError(
            f"installed plugin hash mismatch for {dest}: "
            f"source={source_hash} installed={installed_hash}")
    return source_hash


def sync(cli: str) -> list[str]:
    src = ROOT / "marketplaces" / cli / "plugins" / "khenrix-utils"
    notes = []
    dests = installed_dirs(cli)
    if not dests:
        return [f"{cli}: not installed (run `make setup-{cli}`)"]
    for d in dests:
        _before, unsafe = _tree_identity(d)
        if unsafe:
            raise RuntimeError("; ".join(unsafe))
        shutil.copytree(src, d, dirs_exist_ok=True)
        # `dirs_exist_ok=True` merges and never deletes, so a clean source cannot remove
        # what an earlier sync already put here — and bytecode also appears in place when a
        # CLI imports these modules from the install. Closing the tap in render.py leaves
        # the puddle: measured 2026-08-16, the three live installs still held 25 .pyc after
        # the source tree was clean. Sweep the destination too, for the same reason and with
        # the same idiom (see render.py: `list()` before deleting, rglob is lazy).
        for cache in list(Path(d).rglob("__pycache__")):
            shutil.rmtree(cache, ignore_errors=True)
        digest = verify_install(src, d)
        notes.append(f"{cli}: synced + hash-verified {digest[:12]} → {d}")
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


def _selected_clis(raw: str) -> tuple[str, ...]:
    selected = tuple(item.strip() for item in raw.split(",") if item.strip())
    if not selected or len(selected) != len(set(selected)):
        raise ValueError("--clis must be a non-empty comma list without duplicates")
    bad = [item for item in selected if item not in CLIS]
    if bad:
        raise ValueError(f"unknown CLI(s) {bad}; expected values from {list(CLIS)}")
    return selected


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--clis", default=",".join(CLIS),
                    help="exact CLIs to refresh, comma-separated (default: all)")
    args = ap.parse_args(argv)
    try:
        selected = _selected_clis(args.clis)
    except ValueError as exc:
        ap.error(str(exc))
    print("Rendering…")
    render()
    print("\nSyncing installed plugins…")
    for cli in selected:
        for note in sync(cli):
            print(f"  • {note}")
        m = meta_refresh(cli)
        if m:
            print(f"  • {m}")
    print("\n✅ Refresh complete. Restart the selected CLI sessions to pick up changes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
