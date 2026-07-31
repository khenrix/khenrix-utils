"""Filesystem inventory for the harvest (spec §7.3).

The change predicate is CONTENT HASH + MODE + SIZE. Never mtime, ctime or inode: this
repo's own `render` step does rmtree-then-copytree, which replaces every inode with
byte-identical content — an lstat-keyed predicate reports the whole rendered tree as
changed and the generator fixed point never converges.

Symlinks are recorded by their target and never followed: following them would walk out
of the tree and could cycle. `os.path.realpath` does not raise on a cycle, so refusing to
descend is the only defence.
"""
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from .storage import Quota


@dataclass(frozen=True)
class Entry:
    path: str
    digest: str
    mode: int
    size: int
    kind: str


def _digest(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _symlink_entry(p: Path, rel: str) -> Entry:
    """Digest the target text, not the target's contents — the link is the artefact."""
    return Entry(rel, hashlib.sha256(os.readlink(p).encode()).hexdigest(), 0, 0, "symlink")


def take(root, *, quota: Quota | None = None, skip_dirs=(".git",)):
    """Inventory `root`. Returns (entries, breaches); a breach means FAIL CLOSED — the
    entries dict is empty rather than a partial inventory reported as complete."""
    quota = quota or Quota.default()
    root = Path(root)
    entries, total, count = {}, 0, 0

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        here = Path(dirpath)
        # A symlink to a directory arrives in dirnames, so it would otherwise be neither
        # recorded nor counted. Drop it from the walk and record the link itself.
        for d in list(dirnames):
            p = here / d
            if p.is_symlink():
                dirnames.remove(d)
                rel = str(p.relative_to(root))
                count += 1
                if (b := quota.breach(files=count, file_bytes=0, total_bytes=total)):
                    return {}, [b]
                entries[rel] = _symlink_entry(p, rel)
        for name in filenames:
            p = here / name
            rel = str(p.relative_to(root))
            count += 1
            if (b := quota.breach(files=count, file_bytes=0, total_bytes=total)):
                return {}, [b]
            # Before stat(): a dangling symlink lands in filenames and stat() would raise.
            if p.is_symlink():
                entries[rel] = _symlink_entry(p, rel)
                continue
            st = p.stat()
            total += st.st_size
            if (b := quota.breach(files=0, file_bytes=st.st_size, total_bytes=total)):
                return {}, [f"{rel}: {b}"]
            entries[rel] = Entry(rel, _digest(p), st.st_mode & 0o777, st.st_size, "file")
    return entries, []


def diff(before: dict, after: dict) -> dict:
    """path -> added | removed | modified. Compares content, mode and size only."""
    out = {}
    for path, e in after.items():
        old = before.get(path)
        if old is None:
            out[path] = "added"
        elif (old.digest, old.mode, old.size) != (e.digest, e.mode, e.size):
            out[path] = "modified"
    for path in before:
        if path not in after:
            out[path] = "removed"
    return out
