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
import stat
from dataclasses import dataclass
from pathlib import Path

from .storage import Quota


class SnapshotError(RuntimeError):
    """A precondition of the whole call is violated, so no inventory can be taken."""


@dataclass(frozen=True)
class Entry:
    """One inventoried path.

    `kind` is "file", "symlink" or "special" (FIFO, socket, device node). "dir" is
    RESERVED AND NEVER PRODUCED: directories are not inventoried, so an empty directory
    an agent creates or removes is invisible to `diff` — only its contents are seen.

    `digest`/`mode`/`size` mean different things per kind, and only `_digest` reads
    content; see the branches in `take`.
    """
    path: str
    digest: str
    mode: int
    size: int
    kind: str


def _digest(p: Path) -> str:
    """Mirrors `baseline._sha256_file` and `fleet._sha256_file`. All three must agree
    byte-for-byte on how a file is digested: a snapshot digest is compared against a
    baseline manifest hash of the same file, and a chunk size or mode difference would
    make identical bytes disagree."""
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _symlink_entry(p: Path, rel: str) -> Entry:
    """Digest the target text, not the target's contents — the link is the artefact."""
    return Entry(rel, hashlib.sha256(os.readlink(p).encode()).hexdigest(), 0, 0, "symlink")


def _special_entry(st: os.stat_result, rel: str) -> Entry:
    """A FIFO, socket or device node — recorded WITHOUT being opened.

    Opening one is not merely unhelpful, it is unsafe: a read-open on a FIFO blocks until
    a writer appears, which for an inventory is an unbounded hang with no timeout anywhere
    in the call path, and a socket raises ENXIO. Dropping it silently is the other wrong
    answer (`screen.py` refuses to do that for the same reason), so its existence is
    recorded and the file TYPE stands in for the content it has none of — which also makes
    a FIFO replaced by a socket at the same path read as modified.
    """
    kind_bits = f"special:{stat.S_IFMT(st.st_mode)}".encode()
    return Entry(rel, hashlib.sha256(kind_bits).hexdigest(), st.st_mode & 0o777, 0, "special")


def take(root, *, quota: Quota | None = None, skip_dirs=(".git",)):
    """Inventory `root`. Returns (entries, breaches); a breach means FAIL CLOSED — the
    entries dict is empty rather than a partial inventory reported as complete.

    Raises SnapshotError if `root` is not an existing directory, and PermissionError (from
    `_digest`) on a file it may not read. Both are deliberately unignorable. os.walk's
    default `onerror=None` SWALLOWS ENOENT/ENOTDIR and yields nothing, so a mistyped path
    or a torn-down seat would otherwise return a clean `({}, [])` — and an empty inventory
    that means "I read nothing" is indistinguishable from one that means "nothing is here",
    so `diff(before, {})` would report every file in the tree as removed and hand the
    agent the blame for a mass deletion. A breach line would still return that empty dict;
    raising is what the caller cannot accidentally ignore, and matches how `baseline` and
    `fleet` reject a violated precondition. PermissionError is left to propagate for the
    same reason: a file whose content could not be read has no honest digest, and a
    snapshot that quietly substitutes one would report an agent's edit to it as unchanged.
    """
    quota = quota or Quota.default()
    root = Path(root)
    if not root.is_dir():
        raise SnapshotError(f"snapshot root is not an existing directory: {root}")
    entries, total, count = {}, 0, 0

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Sorted for the reason screen.py sorts: the running totals below decide WHICH
        # breach is reported first, so an unsorted walk makes a tree over two caps report
        # a different line run to run.
        dirnames[:] = sorted(d for d in dirnames if d not in skip_dirs)
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
        for name in sorted(filenames):
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
            # Before open(): a FIFO here would block the walk forever. stat() does not.
            if not stat.S_ISREG(st.st_mode):
                entries[rel] = _special_entry(st, rel)
                continue
            total += st.st_size
            if (b := quota.breach(files=0, file_bytes=st.st_size, total_bytes=total)):
                return {}, [f"{rel}: {b}"]
            entries[rel] = Entry(rel, _digest(p), st.st_mode & 0o777, st.st_size, "file")
    return entries, []


def diff(before: dict, after: dict) -> dict:
    """path -> added | removed | modified. Compares content, mode and size only.

    `kind` is deliberately NOT compared, so a file replaced by a symlink at the same path
    is caught only incidentally — via the digest, and via the mode 0 / size 0 the symlink
    branch records. That is reliable in practice but it is a side effect, not a rule: a
    consumer that needs the type change itself must compare `kind` for itself.
    """
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
