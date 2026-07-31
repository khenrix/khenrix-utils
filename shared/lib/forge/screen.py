"""Bounded secret screen over the selected baseline, run BEFORE any provider starts.

Spec §3: whatever the baseline contains is what N cloud-backed full-permission agents
read. Scanning the OUTPUT is too late — the exposure already happened. This is a
high-confidence screen, NOT proof the baseline is secret-free, and it does not contain
agents that keep access to the real $HOME.

The patterns are IMPORTED from scripts/lib/checks.py, never copied: one definition of what
a secret looks like. checks.scan_path is deliberately not reused — it is a single-file
reader with no size cap, no binary guard and no walk, so pointing it at a tree would decode
a 400 MB build log into memory.
"""
import hashlib
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path

from .storage import Quota

# High-risk by NAME regardless of content: a credential file whose value shape we do not
# recognise is still a credential file.
BLOCKED_NAMES = (".env", ".envrc", ".netrc", ".pgpass", "credentials", "id_rsa",
                 "id_ed25519", ".npmrc", ".pypirc")

# Bytes read before deciding a file is binary. Read as a separate chunk from a still-open
# handle rather than sliced off a full read(), so a 30 MB object file costs 8 KiB here
# instead of being pulled into memory and then discarded.
_PROBE_BYTES = 8192


def _checks():
    """Import scripts/lib/checks.py by path — it is not an installed package."""
    if "khenrix_checks" in sys.modules:
        return sys.modules["khenrix_checks"]
    here = Path(__file__).resolve()
    # parents[3] is the repo root: forge -> shared/lib -> shared -> <root>. The second
    # candidate is this package's own lib/ dir (<plugin>/lib/checks.py), which is where a
    # rendered plugin would have to carry checks.py — render.py does not put it there
    # today, hence the raise below rather than a silent empty pattern set.
    for cand in (here.parents[3] / "scripts" / "lib" / "checks.py",     # repo layout
                 here.parents[2] / "lib" / "checks.py"):                # bundled plugin
        if cand.is_file():
            spec = importlib.util.spec_from_file_location("khenrix_checks", cand)
            mod = importlib.util.module_from_spec(spec)
            sys.modules["khenrix_checks"] = mod
            spec.loader.exec_module(mod)
            return mod
    raise RuntimeError("checks.py not found; forge cannot screen without the patterns")


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    pattern: str


def _is_high_risk_name(rel: str) -> bool:
    base = Path(rel).name
    return any(base == n or base.startswith(n + ".") for n in BLOCKED_NAMES)


def screen_tree(root, rel_paths, quota: Quota = None):
    """Scan the given repo-relative paths (files or directories).

    Returns (findings, breaches). A non-empty `breaches` means the caller must FAIL the
    run closed with that message — never silently scan less than it claimed to.
    """
    c = _checks()
    quota = quota or Quota.default()
    root = Path(root)

    targets = []
    for rel in rel_paths:
        p = root / rel
        # is_dir()/is_file() both follow symlinks, so the link check comes first: a
        # selected `linkdir -> /home/user/.ssh` would otherwise be walked as a directory
        # and pull host files the baseline never contained into the report.
        if p.is_symlink():
            continue
        if p.is_dir():
            # rglob does not descend into symlinked subdirectories (3.11+, and explicit
            # since recurse_symlinks landed in 3.13), so this only has to drop links
            # that appear as leaves.
            targets += [q for q in sorted(p.rglob("*")) if q.is_file() and not q.is_symlink()]
        elif p.is_file():
            targets.append(p)

    breaches, findings, total = [], [], 0
    if (b := quota.breach(files=len(targets), file_bytes=0, total_bytes=0)):
        return [], [b]

    for p in targets:
        rel = str(p.relative_to(root))
        size = p.stat().st_size
        total += size
        if (b := quota.breach(files=0, file_bytes=size, total_bytes=total)):
            breaches.append(f"{rel}: {b}")
            continue
        if _is_high_risk_name(rel):
            findings.append(Finding(rel, 0, "high-risk-filename"))
        if rel.endswith(c.SCAN_SKIP_SUFFIX):
            continue
        with p.open("rb") as fh:
            head = fh.read(_PROBE_BYTES)
            if b"\x00" in head:        # binary guard: do not read on, do not decode
                continue
            raw = head + fh.read()
        for i, line in enumerate(raw.decode("utf-8", "replace").splitlines(), 1):
            for rx in c.SECRET_FAIL:
                m = rx.search(line)
                if m and hashlib.sha256(m.group(0).encode()).hexdigest() not in c.SECRET_ALLOW_SHA:
                    findings.append(Finding(rel, i, rx.pattern))
                    break
    return findings, breaches
