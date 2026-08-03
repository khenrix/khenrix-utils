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
    """Some part of the tree could not be read, so no inventory of it would be honest."""


def walk_error(raise_as):
    """An `os.walk` `onerror` that RAISES `raise_as`, for a walk whose short answer would be
    read as an answer.

    os.walk's DEFAULT is to swallow the error and yield nothing for that directory, which is
    fail-open twice over in `take`: an unreadable root returns a clean `({}, [])` that `diff`
    reads as "the agent deleted the tree", and an unreadable subdirectory returns a partial
    inventory reported as complete. Raising also covers the mid-walk races — a directory
    removed between the top-level yield and the descent — which take the same swallowed path.

    A FACTORY over the caller's own error class, because the rule is one and only the name it
    raises under varies. `taskbundle._walk_error` was this argument and this f-string copied,
    saying so in its own prose ("separate functions only because each raises its own module's
    class"), and `baseline` would have been the third copy — two spellings of one rule that
    agree today is the defect this package keeps finding.

    NOT the answer at every walk. A walk whose caller is contracted to RETURN refusal lines
    must record the error in that vocabulary instead of raising past the contract:
    `inspect._escaping_links_under` emits a rejection line and `screen._walk` a breach, and
    both stop the run through `preflight.refusals`. What all four share is only that the
    listing error is never dropped.
    """
    def onerror(err: OSError):
        raise raise_as(f"cannot walk {err.filename}: {err.strerror}") from err
    return onerror


_walk_error = walk_error(SnapshotError)


@dataclass(frozen=True)
class Entry:
    """One inventoried path.

    `kind` is "file", "symlink" or "special" (FIFO, socket, device node). "dir" is
    RESERVED AND NEVER PRODUCED: directories are not inventoried, so an empty directory
    an agent creates or removes is invisible to `diff` — only its contents are seen.

    The other three fields are NOT uniform across kinds, and only "file" reads content:

    - file    — `digest` is the sha256 of the bytes; `mode` and `size` are the real ones.
    - symlink — `digest` is the sha256 of the TARGET TEXT; `mode` and `size` are ALWAYS 0.
                That 0 mode is fabricated, not read, so a consumer rendering mode changes
                must not report "000" for every symlink it touches; the size is not the
                target's length either.
    - special — `digest` stands for the file TYPE, nothing was opened; `mode` is real,
                `size` is ALWAYS 0.
    """
    path: str
    digest: str
    mode: int
    size: int
    kind: str


def digest_fd(fd: int) -> str:
    """The same digest, over an ALREADY-OPEN descriptor.

    Split out for `coverage._hash`, which must open the file `O_NOFOLLOW` relative to a
    directory descriptor — a path-taking digest would re-resolve the name it was handed and
    undo that. One spelling, so the two routes cannot drift: `_digest` is this function with
    the open in front of it.
    """
    h = hashlib.sha256()
    while chunk := os.read(fd, 1 << 16):
        h.update(chunk)
    return h.hexdigest()


def _digest(p: Path) -> str:
    """Mirrors `baseline._sha256_file` and `fleet._sha256_file`. All three must agree
    byte-for-byte on how a file is digested: a snapshot digest is compared against a
    baseline manifest hash of the same file, and a chunk size or mode difference would
    make identical bytes disagree."""
    fd = os.open(p, os.O_RDONLY)
    try:
        return digest_fd(fd)
    finally:
        os.close(fd)


def _symlink_entry(p: Path, rel: str) -> Entry:
    """Digest the target text, not the target's contents — the link is the artefact.

    surrogateescape, matching `baseline._sha256_link`, `fleet._sha256_link` and the payload
    `bundle` carries for a link: a link target is a filesystem NAME, not text, so a plain
    `.encode()` raises UnicodeEncodeError on the surrogates `os.readlink` puts there for a
    non-UTF-8 one. That is not a defensive nicety — it took `baseline.materialize` down
    with a `UnicodeEncodeError` on an ordinary tracked link to `caf\\xe9.txt`, in a class
    neither `BaselineError` nor anything `harvest` enumerates. For a valid-UTF-8 target the
    two encodings are byte-identical, so no digest moves.
    """
    target = os.readlink(p).encode("utf-8", "surrogateescape")
    return Entry(rel, hashlib.sha256(target).hexdigest(), 0, 0, "symlink")


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

    Raises SnapshotError if any directory in the tree cannot be read — missing, not a
    directory, or unreadable, at the root or below it (see `_walk_error`) — and
    PermissionError (from `_digest`) on a file it may not read. Both are deliberately
    unignorable, and for one reason: an inventory nobody can distinguish from a complete
    one must not be returned. A breach line would still return the empty dict that is
    itself the ambiguity, so raising is what the caller cannot accidentally ignore, and it
    matches how `baseline` and `fleet` reject what they cannot vouch for. Silence would be
    worst for a DIRECTORY, which drops a whole subtree rather than one file.

    `skip_dirs` MATCHES A NAME AT EVERY DEPTH AND PRUNES REAL DIRECTORIES ONLY. A symlink
    carrying one of those names is recorded as the symlink it is and not descended into — see
    the loop below for the hole the other order left.
    """
    quota = quota or Quota.default()
    root = Path(root)
    entries, total, count = {}, 0, 0

    # onerror subsumes an is_dir() precondition check: every way the root can fail that
    # check reaches scandir and routes here, and unreadability reaches only here.
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False,
                                                onerror=_walk_error):
        here = Path(dirpath)
        # Sorted for the reason screen.py sorts: the running totals below decide WHICH breach
        # is reported first, so an unsorted walk makes a tree over two caps report a different
        # line run to run.
        #
        # THE SYMLINK TEST COMES BEFORE THE `skip_dirs` TEST, and the other order was a hole.
        # A symlink to a directory arrives in `dirnames`, so it would otherwise be neither
        # recorded nor counted; filtering by NAME first meant a link *named* like a skipped
        # directory never reached the branch that records it, and left NO entry at all.
        # Measured: `tree/__pycache__ -> /elsewhere` and `tree/.git -> /elsewhere` both
        # inventoried as nothing, so creating or removing one moved the tree and left
        # byte-for-byte the inventory an untouched tree leaves. `review`'s bracket reads two
        # such inventories as a checkout that stood still. A skipped name says "do not descend
        # into this directory"; it cannot say "this path does not exist", and a link is not the
        # directory whose name it borrowed.
        kept = []
        for d in sorted(dirnames):
            p = here / d
            if p.is_symlink():
                rel = str(p.relative_to(root))
                count += 1
                if (b := quota.breach(files=count, file_bytes=0, total_bytes=total)):
                    return {}, [b]
                entries[rel] = _symlink_entry(p, rel)
                continue          # recorded, and never descended into, whatever its name
            if d not in skip_dirs:
                kept.append(d)
        dirnames[:] = kept
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
