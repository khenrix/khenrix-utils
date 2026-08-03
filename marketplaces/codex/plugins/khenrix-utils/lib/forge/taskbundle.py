"""§20: the task bundle — what a seat was actually given, as a record it can be checked against.

Resolving a task into a portable instruction is a CLOSURE, NOT A BODY. Inlining only the
Markdown hands a seat prose referencing a `scripts/tool.sh` it does not have; copying
without modes hands it a script it cannot execute. So the unit is a manifest — canonical
relative path, kind, mode, content hash, size — plus an entrypoint, plus the caps that were
actually applied, and the bytes are materialized from it and then RE-DERIVED FROM THE SEAT
and compared against this manifest, by the module that owns materialization.

WHY NOT `fleet.clone_seat(template_dir=...)`, WHICH IS THE OBVIOUS HOOK. Measured on git
2.53.0, three independent ways it cannot satisfy §20:

  1. EVERY DOT-NAME IS SILENTLY DROPPED, AT EVERY LEVEL. A template holding
     `bundle/.claude/skills/S.md` and `bundle/.envrc` produced a clone containing neither,
     rc 0, no output. A skill closure is precisely the shape that has dot directories, and
     this is the worst available failure: the manifest lists the file, the hash covers it,
     the seat does not have it, and nothing says so.
  2. MODES ARE NORMALIZED, NOT PRESERVED. A 0600 template file arrives 0644 (git applies
     0666/0777 by the executable bit, masked by umask). Only +x survives.
  3. IT DISARMS AN EXISTING DEFENCE. `clone_seat` computes `engine_owned = not
     template_dir` (`fleet.py:177`); passing a directory flips it False, skipping the
     pre-clean and installing the directory as a real git template — git READS a template
     `config` (a malformed one aborted a clone outright) and installs a template `hooks/`
     that then runs for the agent's own commits.

The bundle is therefore copied AFTER the clone, into the seat's own git directory, and the
manifest is recomputed from the seat's filesystem and compared. That recomputation is the
only thing that turns the three silent losses above into a refusal, and it is the rule
`clone_seat` already applies to itself: the trusted parent recomputes readiness from primary
evidence rather than trusting the operation that produced it.

WHAT THIS MODULE DOES NOT CLAIM. §20 says "bar ambient invocation of the same skill". The
only mechanism in reach is a sentence in the prompt, so `ambient_note()` returns that
sentence and the record calls it an INSTRUCTION ISSUED. It is not a mechanical bar and the
report must never say it is; if a per-CLI settings toggle exists, it has to be measured
before it is claimed.

EMPTY DIRECTORIES ARE NOT CARRIED, and that is the same ceiling `snapshot.Entry` declares:
directories are not inventoried, so a bundle that means to hand a seat an empty `output/`
cannot. Say so to the author rather than materializing something the manifest cannot check.
"""
import hashlib
import json
import os
import stat
from dataclasses import dataclass, fields
from pathlib import Path, PurePosixPath

from . import bundle as bundlemod
from . import snapshot, storage

VERSION = 1


class TaskBundleError(RuntimeError):
    """This bundle cannot be described honestly, or cannot be laid down where it was asked."""


@dataclass(frozen=True)
class BundleEntry:
    """One path the bundle carries.

    `kind` is "file" or "symlink" — the two shapes with an honest payload. A FIFO, socket
    or device node is REFUSED, never given a fabricated one: `snapshot` records a special
    file by type because it is inventorying a tree it did not choose, while a bundle is
    authored, and an author who put a FIFO in one has made a mistake worth hearing about.

    `mode` is the real `st_mode & 0o777` for a file and a FABRICATED 0 for a symlink,
    matching `snapshot.Entry` and `bundle.SidecarEntry`. `size` is 0 for a symlink for the
    same reason — it is not the target's length.

    `sha256` is the content for a file and the sha256 of the TARGET TEXT, encoded with
    surrogateescape, for a symlink: a link target is a filesystem name, not text, and a
    strict `.encode()` took `baseline.materialize` down on an ordinary link to `café.txt`.
    """
    path: str
    kind: str
    mode: int
    sha256: str
    size: int


@dataclass(frozen=True)
class TaskBundle:
    """The manifest §20 asks for, plus the caps that were applied to produce it.

    THE CAPS ARE RECORDED, NOT ASSUMED. A bundle that fit under `Quota.for_task_bundle`
    and a bundle nobody measured are different records, and only one of them can say what
    it was measured against. `storage.Quota` is not stored as a nested object because JSON
    has one sequence type and the round-trip check below is cheaper to keep honest over
    three plain ints.
    """
    version: int
    entrypoint: str
    entries: tuple[BundleEntry, ...]
    max_files: int
    max_file_bytes: int
    max_total_bytes: int


def _rel(root: Path, p: Path) -> str:
    """The canonical POSIX relative path, refused if it could name anything but a bundle path."""
    rel = PurePosixPath(p.relative_to(root)).as_posix()
    # One spelling of the rule, imported rather than re-inlined: `bundle.py:191` re-inlined
    # `harvest._literal` and that divergence route is on this project's open-defect list.
    #
    # Wrapped rather than left to propagate: `_assert_contained` raises `bundle.BundleError`,
    # a type this module's own contract does not promise. UNREACHABLE through `_rel`'s two
    # call sites today — `p` always comes from `os.walk` under `root` (see `_walk`), and
    # `os.walk` never yields a literal `..` component, which is the only way a successful
    # `relative_to` above could still leave one for `_assert_contained` to catch. The wrap
    # costs nothing and means that invariant is enforced here rather than leaned on by every
    # future caller of `_rel`.
    try:
        bundlemod._assert_contained(rel, "a task bundle path")
    except bundlemod.BundleError as e:
        raise TaskBundleError(str(e)) from e
    if bundlemod._names_dotgit(rel):
        raise TaskBundleError(
            f"a task bundle may not carry git's own directory: {rel!r}. A `.git/config` "
            "laid into a clone takes its hooks pin and its identity, and both are §4.1's.")
    return rel


def _entry(root: Path, p: Path, quota: storage.Quota) -> BundleEntry:
    st = p.lstat()
    if stat.S_ISLNK(st.st_mode):
        rel = _rel(root, p)
        target = os.readlink(p)
        # The escaping-link rule, imported rather than re-derived: `bundle._escapes` is the
        # one spelling this project already trusts for "does this link's target leave the
        # tree it is joined onto", and re-deriving it here risks the same divergence `_rel`'s
        # comment above warns against — a hand-rolled `joined.startswith("..")` also matches
        # a legitimate path segment like `..cache` that is not a `..` traversal at all.
        # Materializing an escaping link points a seat at a host path nobody authored.
        if bundlemod._escapes(rel, target):
            raise TaskBundleError(
                f"a task bundle symlink escapes the bundle: {rel!r} -> {target!r}")
        digest = hashlib.sha256(target.encode("utf-8", "surrogateescape")).hexdigest()
        return BundleEntry(rel, "symlink", 0, digest, 0)
    if not stat.S_ISREG(st.st_mode):
        raise TaskBundleError(
            f"a task bundle may not carry a special file (FIFO, socket, device): "
            f"{_rel(root, p)!r}. It was NOT opened — a read-open on a FIFO blocks.")
    rel = _rel(root, p)
    # Checked BEFORE `_digest` opens and reads the file — `snapshot.take`'s own precedent
    # (`st.st_size` against the cap ahead of the read, `snapshot.py:154`). A file that
    # breaches the per-file cap is refused for the bytes it would have cost to hash, not
    # after they were spent: measured before this fix, a 300 MB file under a 1 KB-per-file
    # quota was fully SHA-256'd (~0.6s of I/O) before `scan()` ever raised. This is the read
    # `Quota.for_task_bundle`'s docstring says these caps exist to prevent.
    if (breach := quota.breach(files=0, file_bytes=st.st_size, total_bytes=0)):
        raise TaskBundleError(f"this task bundle exceeds §20's caps — {rel}: {breach}")
    return BundleEntry(rel, "file", st.st_mode & 0o777, snapshot._digest(p), st.st_size)


def _walk(root: Path, quota: storage.Quota) -> list:
    """Every file and symlink under `root`, sorted, never following a directory symlink."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        d = Path(dirpath)
        # A symlink TO a directory is an entry, not a directory to descend: os.walk lists
        # it in dirnames and followlinks=False stops the descent but not the listing.
        for name in list(dirnames):
            if (d / name).is_symlink():
                dirnames.remove(name)
                out.append(_entry(root, d / name, quota))
        for name in filenames:
            out.append(_entry(root, d / name, quota))
    return sorted(out, key=lambda e: e.path)


def scan(root, *, entrypoint: str, quota: storage.Quota | None = None) -> TaskBundle:
    """The manifest for the closure rooted at `root`, or a refusal.

    Refused rather than truncated on every count, because every one of them makes a bundle
    that CLAIMS more than it carries: no entries at all, an entrypoint no entry names, a
    `.git` component, an escaping link, a special file, a breached cap.
    """
    root = Path(root)
    quota = quota or storage.Quota.for_task_bundle()
    entries = _walk(root, quota)
    if not entries:
        raise TaskBundleError(
            f"{root} has no entries. An empty bundle hashes to a stable value and makes "
            "every later comparison vacuous while still answering True.")
    # `file_bytes` is not re-checked here: `_entry` already refused any regular file whose
    # size breached `quota.max_file_bytes` before it was hashed (before `entries` could ever
    # hold it), and a symlink's size is always the fabricated 0 (`BundleEntry`'s docstring).
    # So max(e.size for e in entries) could never be what raises by the time the walk has
    # finished — only the file COUNT still needs the whole walk to be known, since a count
    # breach reported mid-walk would misreport how many entries the bundle actually has.
    breach = quota.breach(files=len(entries), file_bytes=0,
                          total_bytes=sum(e.size for e in entries))
    if breach:
        raise TaskBundleError(f"this task bundle exceeds §20's caps — {breach}")
    if entrypoint not in {e.path for e in entries}:
        raise TaskBundleError(
            f"the entrypoint {entrypoint!r} is not one of this bundle's entries. The seat "
            "is told to read it, so a bundle that does not carry it is prose pointing at "
            "a file the seat does not have.")
    return TaskBundle(VERSION, entrypoint, tuple(entries),
                      quota.max_files, quota.max_file_bytes, quota.max_total_bytes)


def _rows(entries) -> list:
    """The four hashed fields per entry, sorted — the ONE spelling of "hash a closure" here."""
    return sorted([e.path, e.kind, e.mode, e.sha256] for e in entries)


def bundle_hash(b: TaskBundle) -> str:
    """§11.5.3's task/resource bundle hash.

    `checks.source_hash`'s spelling — `sha256(json.dumps(..., sort_keys=True).encode())` —
    EXTENDED in two directions, both strengthening. `source_manifest` hashes
    `(relpath, content_sha)` pairs; a bundle hash blind to `mode` cannot distinguish a
    script from a non-executable copy of the same bytes, which is §20's own named failure,
    invisible to the hash meant to prove identical materialization. `kind` is in for the
    same reason one level over. And the ENTRYPOINT is hashed because §11 compares this
    value to decide "identically prompted": two seats handed the same files and told to
    start in different places were not identically prompted.

    The caps are NOT hashed. They describe what was measured, not what was handed over,
    and two runs with different caps over the same bytes gave their seats the same bundle.
    """
    payload = {"version": b.version, "entrypoint": b.entrypoint, "entries": _rows(b.entries)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _row(b: TaskBundle) -> dict:
    return {"version": b.version, "entrypoint": b.entrypoint,
            "max_files": b.max_files, "max_file_bytes": b.max_file_bytes,
            "max_total_bytes": b.max_total_bytes,
            "entries": [{"path": e.path, "kind": e.kind, "mode": e.mode,
                         "sha256": e.sha256, "size": e.size} for e in b.entries]}


_ENTRY_TYPES = {"path": str, "kind": str, "mode": int, "sha256": str, "size": int}


def _decode(row, source) -> TaskBundle:
    """The `runstate._decode` precedent: missing refused, unknown refused, type-checked.

    NOT `write_seat`'s silence. That module argues its own case — §14.2 assigns the seat
    record's fields to the orchestrator, so `runstate` refuses to be the authority on a
    record it does not own. None of that transfers: §20 enumerates this manifest's fields
    exactly, and §11 hashes it into a comparison that decides whether three seats' agreement
    is creditable. A field a writer stopped writing changes the hash with nothing saying why.
    """
    if not isinstance(row, dict):
        raise TaskBundleError(f"{source}: a task bundle is an object, not {type(row).__name__}")
    names = [f.name for f in fields(TaskBundle)]
    missing = [n for n in names if n not in row]
    if missing:
        raise TaskBundleError(f"{source} is missing {missing}")
    unknown = sorted(set(row) - set(names))
    if unknown:
        raise TaskBundleError(f"{source} carries fields this engine does not know: {unknown}")
    for n in ("version", "max_files", "max_file_bytes", "max_total_bytes"):
        if not isinstance(row[n], int) or isinstance(row[n], bool):
            raise TaskBundleError(f"{source}: {n} is an int, not {row[n]!r}")
    if not isinstance(row["entrypoint"], str):
        raise TaskBundleError(f"{source}: entrypoint is a string, not {row['entrypoint']!r}")
    if row["version"] != VERSION:
        raise TaskBundleError(
            f"{source} was written by task-bundle version {row['version']}, and this engine "
            f"writes {VERSION}. A manifest read under the wrong version is a hash nobody can "
            "reproduce.")
    if not isinstance(row["entries"], list) or not row["entries"]:
        raise TaskBundleError(
            f"{source}: entries is a non-empty list. An empty one reads as a bundle with "
            "nothing in it, which every later check passes vacuously.")
    entries = []
    for i, e in enumerate(row["entries"]):
        if not isinstance(e, dict):
            raise TaskBundleError(f"{source}: entry {i} is an object, not {type(e).__name__}")
        emissing = sorted(set(_ENTRY_TYPES) - set(e))
        eunknown = sorted(set(e) - set(_ENTRY_TYPES))
        if emissing or eunknown:
            raise TaskBundleError(
                f"{source}: entry {i} is missing {emissing} and carries unknown {eunknown}")
        for k, t in _ENTRY_TYPES.items():
            if not isinstance(e[k], t) or (t is int and isinstance(e[k], bool)):
                raise TaskBundleError(f"{source}: entry {i}: {k} is {t.__name__}, not {e[k]!r}")
        if e["kind"] not in ("file", "symlink"):
            raise TaskBundleError(
                f"{source}: entry {i}: kind is 'file' or 'symlink', not {e['kind']!r}")
        entries.append(BundleEntry(e["path"], e["kind"], e["mode"], e["sha256"], e["size"]))
    return TaskBundle(row["version"], row["entrypoint"], tuple(entries),
                      row["max_files"], row["max_file_bytes"], row["max_total_bytes"])


def write_task_bundle(run_dir, b: TaskBundle) -> None:
    """Persist the resolved instruction so `--collect` never depends on vanished context (§20).

    `atomic_write`, not `exclusive_write`: the manifest is the run's write-once identity and
    this is not it — a resume may re-record the same bundle, and a rename-published file
    leaves a mid-write reader the previous one whole.
    """
    if not isinstance(b, TaskBundle):
        raise TaskBundleError(f"a TaskBundle is required, not {type(b).__name__}")
    blob = json.dumps(_row(b), sort_keys=True, indent=2).encode("utf-8") + b"\n"
    path = storage.task_bundle_path(run_dir)
    restored = _decode(json.loads(blob), path)
    if restored != b:
        differing = [f.name for f in fields(TaskBundle)
                     if getattr(restored, f.name) != getattr(b, f.name)]
        raise TaskBundleError(
            f"this task bundle does not survive its own round trip; {differing} come back as "
            "a different type. JSON has one sequence type: pass `entries` as a tuple of "
            "`BundleEntry`.")
    storage.atomic_write(path, blob)


def read_task_bundle(run_dir) -> TaskBundle:
    """What §20 recorded. Raises if it is absent — never an empty bundle.

    `read_manifest`'s precedent. An absent bundle defaulting to "no entries" would make
    every §11 comparison and every materialization check pass over nothing.
    """
    path = storage.task_bundle_path(run_dir)
    try:
        raw = path.read_bytes()
    except FileNotFoundError as e:
        raise TaskBundleError(
            f"{path} does not exist: this run recorded no task bundle, so there is nothing "
            "to say what the seats were given.") from e
    try:
        return _decode(json.loads(raw), path)
    except ValueError as e:
        raise TaskBundleError(f"{path} is not readable as JSON: {e}") from e
