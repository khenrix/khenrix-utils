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
import os
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


def _walk(base: Path, root: Path, skip_prefixes) -> list[Path]:
    """Enumerate regular files under `base`, PRUNING rather than post-filtering.

    `.git` is the object store the baseline was built from, so scanning it is pure cost —
    and on a repo with a large loose-object store, merely enumerating it is too. Pruning
    dirnames in place stops os.walk descending; filtering a completed rglob would still
    have stat'd every object, and those objects would count against `max_files` and turn a
    normal repo-root selection into a confusing quota breach.

    os.walk does not descend into symlinked directories by default, so links only have to
    be dropped where they appear as leaves. Names are sorted so the scan order — and any
    running-total breach that depends on it — is deterministic.
    """
    out = []
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        d = Path(dirpath)
        dirnames[:] = sorted(n for n in dirnames if n != ".git")
        for n in sorted(filenames):
            q = d / n
            if q.is_symlink():
                continue
            if any(q.relative_to(root).as_posix().startswith(s) for s in skip_prefixes):
                continue
            out.append(q)
    return out


def screen_tree(root, rel_paths, quota: Quota = None):
    """Scan the given repo-relative paths (files or directories).

    Returns (findings, breaches). A non-empty `breaches` means the caller must FAIL the
    run closed with that message — never silently scan less than it claimed to.
    """
    c = _checks()
    quota = quota or Quota.default()
    root = Path(root)

    targets, breaches, findings, total = [], [], [], 0
    for rel in rel_paths:
        p = root / rel
        # is_dir()/is_file() both follow symlinks, so the link check comes first: a
        # selected `linkdir -> /home/user/.ssh` would otherwise be walked as a directory
        # and pull host files the baseline never contained into the report. Not following
        # it is right; dropping it silently is not — a selected `.env -> creds` would then
        # come back clean having never been opened, which is exactly the verdict this
        # module exists to prevent. "We did not read through the link" is a breach.
        if p.is_symlink():
            breaches.append(f"{rel}: not screened — symlink; links are never followed")
        elif p.is_dir():
            targets += _walk(p, root, c.SCAN_SKIP_DIRS)
        elif p.is_file():
            targets.append(p)
        elif not p.exists():
            breaches.append(f"{rel}: not screened — selected path does not exist")
        else:
            breaches.append(f"{rel}: not screened — not a regular file or directory")

    if (b := quota.breach(files=len(targets), file_bytes=0, total_bytes=0)):
        breaches.append(b)
        return [], breaches

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
                # Every match on the line, not just the first: with `search`, an
                # allowlisted decoy earlier on the line consumes the only look this
                # pattern gets and a live token after it is never seen.
                if any(hashlib.sha256(m.group(0).encode()).hexdigest() not in c.SECRET_ALLOW_SHA
                       for m in rx.finditer(line)):
                    findings.append(Finding(rel, i, rx.pattern))
                    break
    return findings, breaches
