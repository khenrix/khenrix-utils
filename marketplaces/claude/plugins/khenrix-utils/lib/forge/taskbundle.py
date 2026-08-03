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
     template_dir` (`fleet.py:188`); passing a directory flips it False, skipping the
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

MEASURED (2026-08-03): CLI ACCESS TO THE TASK DIRECTORY. Whether a CLI's file-reading tools
will open a path under the git directory was an open question this module could not answer by
construction, so it was probed once, by hand, outside the suite: `git init`, a sentinel at
`.git/khenrix-forge/task/SKILL.md`, and each CLI asked headlessly to read it and quote the
word.

  * claude (Claude Code, `-p --output-format json`): READS IT. Quoted the sentinel,
    `is_error: false`, `permission_denials: []`.
  * codex (`exec --dangerously-bypass-approvals-and-sandbox`, v0.145.0): READS IT. Quoted the
    sentinel; the transcript shows it opening the path directly.
  * agy (v1.1.10): **NOT MEASURED, and NOT a refusal.** Both the plan-mode and JSON forms
    answered `{"status":"ERROR","error":"timeout waiting for response"}` with `total_tokens: 0`
    — it never reached a model. The CONTROL is what makes that readable rather than a guess: an
    identical prompt against an ordinary WORKTREE file failed the same way, same error, same
    zero tokens. So this measures agy's transport on this machine (see `headless-invocation.md`
    on the mid-2026 consumer-OAuth wind-down), not its willingness to read under `.git`. The
    question stays open for agy and must be re-probed before anything relies on it.

WHAT THE TWO YESES DO NOT LICENSE. Nothing in Plan I puts this path into a prompt, so no step
here rests on the probe either way. Plan J is the first plan that hands a seat the pointer, and
it may not wire agy's seat to it on two measurements out of three. A seat that cannot read its
entrypoint cannot quote the sentinel and scores `failed`, which is the fail-closed direction —
the run stays honest, but the reason string is unmapped.

EMPTY DIRECTORIES ARE NOT CARRIED, and that is the same ceiling `snapshot.Entry` declares:
directories are not inventoried, so a bundle that means to hand a seat an empty `output/`
cannot. Say so to the author rather than materializing something the manifest cannot check.
"""
import hashlib
import json
import os
import shutil
import stat
from dataclasses import dataclass, fields
from pathlib import Path, PurePosixPath

from . import bundle as bundlemod
from . import gitcmd, snapshot, storage

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


def _check_rel(rel: str) -> str:
    """A path this module is willing to READ INTO a bundle or LAY DOWN from one, or a refusal.

    Split out of `_rel` because it has a second caller that does not come from `os.walk`:
    `materialize` is handed a `TaskBundle`, and `_decode` type-checks entry fields without
    ever validating `path` — so the recorded manifest `read_task_bundle` returns is a route
    to a write whose path `_rel` never stood in front of. `bundle._safe_rel` composes the
    same two rules one module over, for the symmetric reason.
    """
    # One spelling of the rule, imported rather than re-inlined: `bundle.py:191` re-inlined
    # `harvest._literal` and that divergence route is on this project's open-defect list.
    #
    # Wrapped rather than left to propagate: `_assert_contained` raises `bundle.BundleError`,
    # a type this module's own contract does not promise. Still unreachable through `_rel` —
    # `p` always comes from `os.walk` under `root` (see `_walk`), and `os.walk` never yields a
    # literal `..` component, which is the only way a successful `relative_to` could leave one
    # for `_assert_contained` to catch — but LIVE through `materialize`, whose paths come off a
    # `TaskBundle` that may have been decoded rather than scanned. That is the whole reason the
    # wrap is here rather than leaned on by each caller.
    try:
        bundlemod._assert_contained(rel, "a task bundle path")
    except bundlemod.BundleError as e:
        raise TaskBundleError(str(e)) from e
    if bundlemod._names_dotgit(rel):
        raise TaskBundleError(
            f"a task bundle may not carry git's own directory: {rel!r}. A `.git/config` "
            "laid into a clone takes its hooks pin and its identity, and both are §4.1's.")
    return rel


def _contained(root, rel: str, what: str, *, create_dirs: bool = False):
    """`bundle.contained`, wrapped into this module's declared class.

    The wrap is `_check_rel`'s, for the same reason stated one function up: `BundleError` is a
    type this module's contract does not promise, and an error class no caller of `materialize`
    knows to catch is a refusal that reaches them as a crash.
    """
    try:
        return bundlemod.contained(root, rel, what, create_dirs=create_dirs)
    except bundlemod.BundleError as e:
        raise TaskBundleError(str(e)) from e


def _rel(root: Path, p: Path) -> str:
    """The canonical POSIX relative path, refused if it could name anything but a bundle path."""
    return _check_rel(PurePosixPath(p.relative_to(root)).as_posix())


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


# `snapshot.walk_error`'s rule under this module's class — one spelling, since this was
# that argument and that f-string copied out. What is specific here is what a SHORT manifest
# is read as, and all three callers read it as an answer: `scan`, where it under-describes
# what the seat was given; `verify_materialized`, where a directory that cannot be read makes
# the re-derived manifest differ and reports it as a MISSING FILE; and `installed_closure`,
# where it is a provenance hash over part of a closure, compared for equality against two
# others.
_walk_error = snapshot.walk_error(TaskBundleError)


def _walk(root: Path, quota: storage.Quota) -> list:
    """Every file and symlink under `root`, sorted, never following a directory symlink."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False,
                                                onerror=_walk_error):
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
    except OSError as e:
        # EVERY OTHER WAY THE OPEN CAN FAIL, under this module's own class. A DIRECTORY at
        # this name raises `IsADirectoryError` and an unreadable one `PermissionError`;
        # neither is a `FileNotFoundError`, so both used to leave a function whose declared
        # failure mode is `TaskBundleError` as a bare `OSError`. `read_task_bundle_if_recorded`
        # below is what makes that reachable in production: it hands the caller a promise that
        # everything but "the file is not there" arrives as `TaskBundleError`, and that promise
        # has to be true here, where the read happens, rather than restated one function down.
        raise TaskBundleError(f"{path} could not be read: {e}") from e
    try:
        return _decode(json.loads(raw), path)
    except ValueError as e:
        raise TaskBundleError(f"{path} is not readable as JSON: {e}") from e


def read_task_bundle_if_recorded(run_dir) -> TaskBundle | None:
    """The run's bundle, or `None` because this run recorded none — and `None` for NOTHING ELSE.

    `read_task_bundle`'s raise is right for a caller that knows a bundle exists;
    `runner.run_seat` is a caller that does not, because runs predating §20 record none. So the
    ABSENCE of the file is an answer here.

    WHAT IS NOT AN ANSWER: a file that exists and this engine cannot read. Folding that into
    `None` would launch three seats with nothing materialized while the launcher's
    `prompt_identity` carries a `bundle_sha256` the front end computed from the same path a
    moment earlier — a record claiming a bundle over seats that got none. Every failure but
    "the file is not there" therefore propagates as `TaskBundleError`.

    `os.path.lexists`, NOT `Path.exists()`. `exists()` resolves the link and answers False for
    a DANGLING symlink at this name — so a `task-bundle.json` pointing at a file that has been
    removed would read as "this run recorded none", which is the exact collapse the paragraph
    above forbids, arriving by the one route the paragraph does not name. `lexists` sees the
    link, the read below fails, and the caller learns the bundle is unreadable rather than
    absent.

    A FILE THAT VANISHES BETWEEN THE TWO CALLS RAISES, and that is correct rather than a race
    to smooth over: something removed a run's task bundle while the run was reading it, and
    `read_task_bundle`'s own message names the path.
    """
    if not os.path.lexists(storage.task_bundle_path(run_dir)):
        return None
    return read_task_bundle(run_dir)


def task_dir(seat_path) -> Path:
    """Where a seat's bundle lives: `<git-dir>/khenrix-forge/task`, with git asked for the git dir.

    ASKED, NEVER JOINED. `Path(seat) / ".git"` is a directory in an ordinary clone and a
    FILE in a linked worktree, so the join is right by luck and wrong the moment §16's
    synthesis worktree exists. `rev-parse --absolute-git-dir` is measured safe on all three
    of this package's git closures — it loads no index, fires no hook and runs no diff
    driver — so it needs `READONLY` and nothing else.
    """
    out = gitcmd.git(seat_path, "rev-parse", "--absolute-git-dir",
                     env_extra=gitcmd.READONLY).stdout.strip()
    if not out:
        raise TaskBundleError(f"git named no git directory for {seat_path}")
    return Path(out) / "khenrix-forge" / "task"


def materialize(b: TaskBundle, source_root, seat_path) -> Path:
    """Lay `b`'s bytes down inside `seat_path`'s git directory, preserving mode and kind.

    ENGINE-OWNED COPIER, NOT `shutil.copytree` AND NOT `git clone --template`. The template
    path drops every dot-name at every level and normalizes modes (see the module
    docstring). `copytree` would be closer but still walks the SOURCE, and what must be laid
    down is what the MANIFEST says — otherwise a file added to the source between `scan` and
    here arrives in the seat unhashed and unnamed.

    A REFUSAL, NOT AN OVERWRITE, when the directory already exists. §8.1 gives every retry a
    fresh clone: a second materialization into a live seat would be the reset-and-rerun-in-place
    §8.1 forbids, one directory over. This module deletes nothing it did not itself just
    create — the one `rmtree` below is the failure path unwinding THIS call's own `mkdir`,
    which the refusal above is what guarantees, and it is the only reason "a refused bundle
    leaves nothing behind" is a true sentence rather than an intention (see the loop).
    """
    # EVERY PATH, KIND AND MODE RE-CHECKED, BEFORE ANYTHING IS CREATED. `scan` routes each path
    # through `_rel`, so a scanned bundle cannot carry a `..` or a `.git` component — but a
    # bundle does not have to have been scanned in this process. `_decode` type-checks entry
    # fields and never validates `path`, so the manifest `read_task_bundle` hands back, which is
    # what §8.1's fresh-clone retry re-materializes from, reaches this write with whatever
    # string was on disk. `dest / "../../x"` escapes without Path complaining. Checked ahead of
    # the `mkdir` so a refused bundle leaves nothing behind: `dest.exists()` is the refusal
    # above, and a half-created directory would wedge the very retry that is meant to recover.
    #
    # `kind` and `mode` are re-checked for the same reason and were not: `_decode` bounds them
    # only for a bundle that came off disk, and a `TaskBundle` built in process reaches the
    # `os.chmod` below with any int at all — 0o4755 sets a setuid bit the manifest never
    # described (inert on a self-owned file, but a property nothing authored), and a negative
    # one raises `OverflowError`, an error class no caller of this module knows to catch.
    for e in b.entries:
        _check_rel(e.path)
        if e.kind not in ("file", "symlink"):
            raise TaskBundleError(
                f"{e.path!r}: kind is 'file' or 'symlink', not {e.kind!r}; a bundle entry "
                "this engine cannot name is one it must not lay down as a file by default")
        if not isinstance(e.mode, int) or isinstance(e.mode, bool) or not 0 <= e.mode <= 0o777:
            raise TaskBundleError(
                f"{e.path!r}: mode is a permission triple in 0..0o777, not {e.mode!r}")
    # The check over the entry SET, which the per-entry loop above cannot make and which the
    # paragraph above was wrong to imply it covered. See `bundle._assert_no_collision`: two
    # entries claiming one path used to be discovered by `FileExistsError` from the write
    # loop, AFTER `dest` and the earlier entries existed.
    try:
        bundlemod._assert_no_collision([e.path for e in b.entries], "a task bundle path")
    except bundlemod.BundleError as e:
        raise TaskBundleError(str(e)) from e
    dest = task_dir(seat_path)
    # `lexists`, for `read_task_bundle_if_recorded`'s reason and so this module holds ONE
    # spelling of it: `Path.exists()` resolves the link and answers False for a DANGLING
    # symlink at this name, and the `mkdir` below then raises `FileExistsError` — a class no
    # caller of this module knows to catch, which is the same complaint the write loop's own
    # `O_EXCL` comment makes two paragraphs down.
    if os.path.lexists(dest):
        raise TaskBundleError(
            f"{dest} already holds something. §8.1 gives a retry a FRESH clone; "
            "re-materializing into a live seat is a reset-and-rerun in place.")
    source_root = Path(source_root)
    dest.mkdir(parents=True)
    # THE PRE-WRITE CHECKS ARE NOT THE WHOLE OF "LEAVES NOTHING BEHIND", and the comment
    # above used to be the whole of the argument for it. Two refusals live INSIDE this loop
    # and cannot be hoisted out of it: `_read_entry` reads the source live, so entry 3's file
    # can have been deleted or replaced since the scan (its own docstring says so), and the
    # leaf write refuses anything that appeared under a concurrent writer. Measured before
    # this `try`: a manifest whose second entry's source was gone left the seat holding
    # `d/one.txt`, and the §8.1 retry that is meant to recover then refused with "already
    # holds a task bundle" — the invariant broken and the recovery path wedged with it.
    #
    # `dest` is provably this call's to remove: the `dest.exists()` refusal above is what
    # makes the `mkdir` the thing that created it. That is `fleet.clone_seat`'s own
    # `dest_preexisted` distinction, and it is why the module docstring's "no delete of any
    # kind" now says what it always meant — nothing this module did not just create.
    try:
        for e in b.entries:
            payload, link_target = _read_entry(source_root, e)
            # BOTH SIDES DESCEND BY DESCRIPTOR, and the write side is the one that gets
            # executed. Entries are laid down IN ORDER, so an earlier one can install a link
            # that changes where a later one's NAME lands: `a -> .`, `a/b -> ..`, `a/b/c -> ..`
            # and the file `a/b/c/hooks/pre-commit` passes `_check_rel` on every path and
            # `_escapes` on every target, and used to put an executable at
            # `<seat>/.git/hooks/pre-commit` that `git commit` ran — measured end to end
            # through `read_task_bundle` -> `materialize`. `bundle.contained` resolves one
            # component at a time against the previous component's open descriptor,
            # `O_NOFOLLOW`, so there is no name left for a link to redirect.
            with _contained(dest, e.path, "a task bundle path", create_dirs=True) as at:
                # `O_EXCL` because nothing in this module overwrites. What it refuses is now
                # only what a CONCURRENT WRITER put there: `dest` was just created empty and
                # `_assert_no_collision` has already refused a manifest that claims one path
                # twice, which is what used to reach this open and raise `FileExistsError` —
                # a class no caller of this module knows to catch.
                try:
                    if e.kind == "symlink":
                        os.symlink(link_target, at.leaf, dir_fd=at.fd)
                    else:
                        # Bytes then mode, in that order: a 0400 file created mode-first
                        # cannot be written to, so it is created 0600 and `fchmod`'d — on the
                        # DESCRIPTOR, which is also the only spelling that cannot be pointed
                        # at another file.
                        fd = bundlemod.open_leaf(
                            at, os.O_WRONLY | os.O_CREAT | os.O_EXCL, "a task bundle path")
                        try:
                            written = 0
                            while written < len(payload):
                                written += os.write(fd, payload[written:])
                            os.fchmod(fd, e.mode)
                        finally:
                            os.close(fd)
                except OSError as err:
                    raise TaskBundleError(
                        f"{e.path!r} could not be laid down in the seat ({err.strerror}); "
                        "something else is writing this bundle directory") from err
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return dest


def _read_entry(source_root: Path, e: BundleEntry) -> tuple:
    """One entry's payload off `source_root`: `(bytes, None)` for a file, `(None, target)` for
    a link — or a refusal, with nothing yet written into the seat.

    READ LIVE, AND THAT IS NOT OPTIONAL FOR A LINK: a `BundleEntry.sha256` is the hash of the
    TARGET TEXT (`_entry`'s own docstring), never the text itself, so this is the only place
    the real target is available at all. `_entry` checked `_escapes` on this exact value at
    scan time; a manifest that reaches `materialize` without passing through `scan` in this
    process — `read_task_bundle`'s decode, which is what §8.1's retry re-materializes from —
    never ran that check, and even a freshly scanned one is re-reading a `source_root`
    `materialize`'s own docstring admits can change in between.

    THE COMPONENTS ARE DESCENDED, NOT JOINED, and that is the half a leaf-only check missed.
    `lstat` on `source_root / "a/b/x"` answers about the LEAF; if `source_root/a` has become a
    symlink to `/etc`, the leaf is a perfectly regular host file, `S_ISREG` passes, and the
    bytes are copied into the seat as if the bundle had authored them — with
    `verify_materialized` clean afterwards, since it re-derives what is now really there.
    `bundle.contained` refuses the intermediate link instead; `O_NOFOLLOW` on the leaf refuses
    the one `read_bytes` used to follow, and `fstat` on the descriptor that was actually opened
    is what says the thing read was a regular file.
    """
    with _contained(source_root, e.path, "a task bundle source path") as at:
        if e.kind == "symlink":
            try:
                target = os.readlink(at.leaf, dir_fd=at.fd)
            except OSError as err:
                raise TaskBundleError(
                    f"{e.path!r} is recorded as a symlink but the source no longer names one "
                    f"({err.strerror})") from err
            if bundlemod._escapes(e.path, target):
                raise TaskBundleError(
                    f"a task bundle symlink escapes the bundle: {e.path!r} -> {target!r}. "
                    "Materializing an escaping link points a seat at a host path nobody "
                    "authored.")
            return None, target
        try:
            fd = bundlemod.open_leaf(at, os.O_RDONLY, "a task bundle source path")
        except OSError as err:
            raise TaskBundleError(
                f"{e.path!r} is recorded as a file but the source no longer names one "
                f"({err.strerror}). Reading through whatever it now names would copy that "
                "content into the seat as if the bundle had authored it.") from err
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise TaskBundleError(
                    f"{e.path!r} is recorded as a file but the source no longer names one "
                    f"(mode {oct(st.st_mode)}). Reading through whatever it now names would "
                    "copy that content into the seat as if the bundle had authored it.")
            return bundlemod.read_fd(fd), None
        finally:
            os.close(fd)


def verify_materialized(b: TaskBundle, seat_path) -> None:
    """Re-derive the manifest FROM THE SEAT and refuse any difference from the authored one.

    THE ONLY STEP THAT TURNS A SILENT LOSS INTO A REFUSAL. A copier that runs and returns is
    not evidence the bytes arrived — `fleet.Seat.verified` makes exactly this argument about
    an empty `filesystem_manifest` making a check "vacuous and still answer True", which is
    why the trusted parent recomputes readiness from primary evidence. Everything git's
    template path loses (dot names, 0600, the +x-only mode rule) is a difference here.

    Compared through `bundle_hash` AND field by field: the hash is what §11 records, and the
    per-entry diff is what a human reading the refusal can act on. A message naming only "the
    hashes differ" sends the reader through the whole closure by hand.
    """
    dest = task_dir(seat_path)
    if not dest.is_dir():
        raise TaskBundleError(f"{dest} holds no task bundle; nothing was materialized")
    # RE-DERIVED UNDER THE CAPS THAT WERE ACTUALLY APPLIED, reconstructed from the bundle
    # rather than re-read from `Quota.for_task_bundle()`. That is what recording them on the
    # value was for: a re-derivation under today's caps could refuse a bundle that legitimately
    # fit yesterday's, and the refusal would name the wrong failure.
    quota = storage.Quota(max_files=b.max_files, max_file_bytes=b.max_file_bytes,
                          max_total_bytes=b.max_total_bytes)
    seen = TaskBundle(b.version, b.entrypoint, tuple(_walk(dest, quota)),
                      b.max_files, b.max_file_bytes, b.max_total_bytes)
    if bundle_hash(seen) == bundle_hash(b):
        return
    authored = {e.path: e for e in b.entries}
    found = {e.path: e for e in seen.entries}
    lost = sorted(set(authored) - set(found))
    extra = sorted(set(found) - set(authored))
    changed = sorted(p for p in set(authored) & set(found) if authored[p] != found[p])
    raise TaskBundleError(
        f"the bundle in {dest} does not match the authored manifest: missing={lost} "
        f"unexpected={extra} altered={changed}. §20 requires it materialized IDENTICALLY in "
        "every clone, and a manifest that lists a file the seat does not have is the one "
        "failure no later check can see.")


# The three live installed plugin paths. DUPLICATED from `scripts/refresh.py:INSTALL_GLOBS`
# rather than imported, because `shared/lib/` is importable by three CLIs and `scripts/` is
# this repository's own tooling — the import would invert the layering. Two spellings of one
# fact eventually disagree, so `tests/test_forge_seams.py` asserts they are equal.
INSTALL_GLOBS = {
    "claude": ["~/.claude/plugins/cache/khenrix-claude-marketplace/khenrix-utils/*"],
    "codex": ["~/.codex/plugins/cache/khenrix-codex-marketplace/khenrix-utils/*"],
    "agy": ["~/.gemini/config/plugins/khenrix-utils"],
}


def _install_dirs(cli: str) -> list:
    """Mirrors `refresh.installed_dirs`. Seam-tested against it; see INSTALL_GLOBS."""
    out = []
    for g in INSTALL_GLOBS[cli]:
        base = Path(g.replace("~", str(Path.home())))
        if "*" in g:
            out += [p for p in base.parent.glob(base.name) if p.is_dir()]
        elif base.is_dir():
            out.append(base)
    return sorted(out)


def installed_closure(cli: str) -> str | None:
    """The hash of `cli`'s LIVE INSTALLED plugin closure, or None because it is not installed.

    THE INSTALLED COPY, NEVER THE REPO SOURCE, and the reason is mechanical rather than
    stylistic: `refresh.sync` does `copytree(src, d, dirs_exist_ok=True)`, an ADDITIVE
    overwrite that never removes a stale file. An installed copy can therefore be a strict
    superset of the repo source, so `checks.source_hash` provably cannot stand in for it —
    which is exactly §20's distinction between byte-identical source and three current,
    identical installed copies.

    PATHS ARE NOT HASHED. The three CLIs install to three different absolute paths by
    construction; a hash carrying the path would make §20's "all three hash identically"
    rule unsatisfiable for a reason that has nothing to do with the closures.

    None, NEVER an empty-manifest hash, AND NEVER A RAISE. `refresh.installed_dirs` returns []
    for a CLI that is not installed, and hashing [] gives every uninstalled CLI the SAME
    value — three seats "hashing identically", which is precisely §20's licence to rely on an
    ambient skill, manufactured out of three absences. `seat.read_proof`'s rule, one module
    over: a missing measurement fails closed.

    AN EMPTY MANIFEST ARRIVES BY TWO ROUTES AND BOTH ARE REFUSED. `not dirs` is one; a
    directory that EXISTS and holds no files is the other, and it reaches the same `[]` — an
    interrupted `refresh.sync` (`copytree` creates the directory before it copies into it), or
    a cache whose contents were deleted. Guarding only the first left the hash this paragraph
    forbids reachable from the second, which is also the value `fingerprint.PromptIdentity`
    carries as `plugin_closure_sha256`: two seats recording `sha256("[]")` compare equal and
    `fingerprint.agreement_label` calls them identically-prompted.

    THE SECOND HALF IS NOT DECORATION. `_walk` walks the LIVE INSTALLED directories and refuses
    a `.git` component, an escaping symlink, a special file and a cap breach. Those refusals
    are right for an AUTHORED bundle, where the author is present and wrong. The installed
    plugin cache is not authored — `refresh.sync` is an additive `copytree` over whatever is on
    disk — so a stale `.git` or a symlink under `~/.claude/plugins/cache/...` would turn a
    provenance hash into a RUN-ENDING exception out of a function whose declared type is
    `str | None`. It already defines the fail-closed value; use it. The refusal is not lost:
    `None` fails `ambient_verdict` exactly as "not installed" does, which is the same verdict
    for the same reason — this closure could not be described.

    `OSError` IS CAUGHT ALONGSIDE, or the paragraph above would be false. `_walk` reaches
    `snapshot._digest`, which OPENS every regular file, and a plugin cache is a tree this
    engine did not write: one unreadable file there raises `PermissionError` — an `OSError`,
    not a `TaskBundleError` — straight out of a `str | None`. Same verdict for the same
    reason, since a closure that could not be read is a closure that could not be described.

    A CLI THIS ENGINE HAS NO GLOBS FOR IS `None` TOO, and it was a `KeyError` — out of a
    function whose second paragraph says NEVER A RAISE, straight through
    `fingerprint.build`'s `closure(cli)` call and out of a record assembler. The value is
    already defined for "this closure could not be described", and a name with no install
    location is exactly that. It is not swallowed: `ambient_verdict` reads `None` as False for
    the same reason it reads an uninstalled CLI that way.

    `for_harvest`'s caps, not `for_task_bundle`'s: this is a tree this engine did not choose,
    which is the question `for_harvest` was sized for. A breach still answers `None`.
    """
    if cli not in INSTALL_GLOBS:
        return None
    dirs = _install_dirs(cli)
    if not dirs:
        return None
    rows = []
    try:
        for d in dirs:
            rows += _rows(_walk(d, storage.Quota.for_harvest()))
    except (TaskBundleError, OSError):
        return None
    if not rows:
        return None
    return hashlib.sha256(json.dumps(sorted(rows), sort_keys=True).encode()).hexdigest()


def ambient_verdict(closures: dict) -> bool:
    """§20's bar: a named skill may be relied on only when ALL THREE hash identically.

    A `None` anywhere is False, and that is the whole point — see `installed_closure`. Both
    halves are recorded by the caller, never just this verdict: a record holding only the
    boolean cannot show what it compared.
    """
    values = [closures.get(c) for c in ("claude", "codex", "agy")]
    return all(v is not None for v in values) and len(set(values)) == 1


def ambient_note(skill: str) -> str:
    """§20's "bar ambient invocation of the same skill", as the instruction it actually is.

    THIS IS NOT A MECHANICAL BAR and the report must never present it as one. The only
    mechanism in reach is a sentence in the prompt; a seat that ignores it is not stopped by
    anything. Recorded in the register §10.1 reserves for `manual_trace_confirmed` — an
    instruction issued, not a property enforced. If a per-CLI settings toggle that really
    does bar it exists, it has to be MEASURED before any code here claims it.
    """
    return (f"Do not invoke the ambient `{skill}` skill. The task bundle materialized for "
            "this run is the only copy you may read; an ambient copy may differ from it.")
