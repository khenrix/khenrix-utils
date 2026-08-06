"""The run directory, its layout and quotas, and the writes that let it survive a crash
(§14.1, §15).

Under XDG_STATE_HOME, not XDG_CACHE_HOME: the run directory holds the only copy of the
seats' work, and XDG defines the cache as data that can be deleted without loss — it is
what every cleanup tool targets first. The repo path is hashed rather than basenamed so
~/git/a/utils and ~/work/b/utils cannot collide.

For the same reason the writes below return only once their bytes are on the platter. A
record that reached the page cache and not the disk turns a crash into a run that never
happened while the work it describes did — the one asymmetry the whole run directory exists
to prevent. `os.replace` does not give that: it is atomic against a concurrent READER, which
is a different property from surviving power loss.
"""
import hashlib
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path


class StorageError(RuntimeError):
    """A write whose bytes did not all arrive, or a record that would break the framing the
    reader depends on."""


def _fsync_dir(path: Path) -> None:
    """fsync the DIRECTORY, which is what makes a rename or a creation survive power loss.

    A file's own fsync covers its contents and says nothing about the name pointing at them.
    Without this the directory entry can be lost while those bytes are safe, so a newly
    created `events.jsonl` vanishes entirely and a rename un-happens. Spec §10.1 names
    exactly this omission as its example of a coverage row that reads present while the
    property is false.
    """
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_exactly(fd: int, data: bytes, path: Path) -> None:
    """One write() of the whole record, or an error — never a partial one, never a loop.

    `os.write` is permitted to consume fewer bytes than it was handed, and an unchecked short
    write is a silent truncation: the file is well-formed to every later reader and missing
    its tail. Finishing it with a second write is what an O_APPEND journal cannot do, because
    another process's append can land between the two and split one record into two halves
    around a stranger. So a short write is reported rather than completed, and the caller
    learns the record's fate instead of inheriting a torn one.

    `atomic_write`'s temp file is exclusive and could safely loop; it does not, because one
    rule that always reports beats two rules a later reader has to tell apart.
    """
    n = os.write(fd, data)
    if n != len(data):
        raise StorageError(f"short write to {path}: {n} of {len(data)} bytes")


def atomic_write(path, data: bytes) -> None:
    """Publish `data` at `path` as one all-or-nothing replacement.

    Three steps whose ORDER is the durability argument: the bytes reach the platter, then the
    name is swapped, then the directory entry reaches the platter. Renaming first publishes a
    name pointing at content a crash can still lose; syncing the directory first flushes a
    directory that does not yet hold the rename, so the rename is exactly what a crash loses.

    The temp name carries a random suffix as well as the pid. A name a process can derive
    twice is one `O_EXCL` refuses, so debris from an interrupted write makes this call fail
    for a process that draws the same pid — on a directory whose whole purpose is to still be
    usable after a crash. Measured, the failure is ONE call, not permanent: the arm below
    unlinks the debris on its way out, so the next attempt succeeds. What the random suffix
    costs is that nothing ever collects debris again — the pid scheme cleaned up after itself
    on the following write, and this one leaves every interrupted write's temp file for a
    `--gc` walk, which is now mandatory rather than tidy.
    """
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            _write_exactly(fd, data, tmp)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        # The previous version survives by construction — nothing here has touched `path`.
        # What a failure would ALSO leave is a temp file that a later glob or a `--gc` walk
        # has to guess about, and removing it is this arm's whole job.
        tmp.unlink(missing_ok=True)
        raise
    _fsync_dir(path.parent)


def exclusive_write(path, data: bytes) -> None:
    """Publish `data` at `path`, or raise FileExistsError because something is already there.

    `atomic_write`'s steps with `os.link` in place of `os.replace`, and that one substitution
    IS the guarantee: `replace` overwrites whatever holds the name and `link` cannot. A caller
    holding a record that must never be rewritten cannot get this from checking first — the
    check passes, the process is descheduled, and the write still lands on top of the file the
    check was protecting. Here the kernel decides, once.

    It costs one step more than `atomic_write`: `replace` publishes the name and removes the
    staging one in a single syscall, while `link` only adds a name, so the staging name has to
    be unlinked separately. Four steps here, three there — the durability ORDER is the same in
    both, with the directory synced last.

    The staging file is unlinked on both paths, so a refusal leaves the directory exactly as
    it found it. What a CRASH leaves is the same debris `atomic_write` leaves, for the same
    `--gc` walk.
    """
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            _write_exactly(fd, data, tmp)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.link(tmp, path)
    finally:
        # Never conditional on success: after the link the inode is reachable by its real
        # name, so the staging name is debris either way.
        tmp.unlink(missing_ok=True)
    _fsync_dir(path.parent)


def append_line(path, data: bytes) -> None:
    """Append one newline-terminated record, durably.

    `O_APPEND` so the record lands at the end however many writers there are, one write of
    the record AND its terminator so no other writer's append can land between them, and an
    fsync before returning so a caller that has seen this call return knows the record is on
    the platter.

    The directory is synced on creation only: without it the first record's file can vanish
    whole, and once the entry exists every later append would be paying a second fsync for a
    name that is already durable. Creation is OBSERVED rather than inferred — an `O_EXCL`
    attempt first, falling back on `FileExistsError` — because a `path.exists()` check has a
    window in both directions, and the direction that matters is a log unlinked between the
    check and the open: this call would then create the file and skip the directory sync,
    which is the one failure the sync exists to prevent.

    The sync only ever happens on the call that WINS the `O_EXCL`, and that call therefore
    owes it whether or not its RECORD lands. A creating call that raises — a short write, an
    `fsync` returning EIO — has still put the file on disk, and every later call then finds it
    and takes the `FileExistsError` branch, so a sync skipped once is skipped for the rest of
    the run. Hence the `finally`: the failing call discharges the obligation itself rather
    than leaving it for the next one. The next one cannot discharge it — nothing on disk
    distinguishes a synced directory entry from an unsynced one, so a later call, and above
    all a later PROCESS, has no way to learn the debt exists. What that leaves the caller is a
    torn record whose file is nonetheless durably named, which `journal._drop_a_torn_tail`
    knows how to finish; what the alternative leaves is a crash window in which the entire log
    disappears and every partly-ran operation reads as never-started.

    The obligation stops at this function's own body. A log first brought into existence by a
    route that does not sync the directory itself — a `touch`, a shell redirect, a plain
    `open(path, "w")` — has a directory entry nothing here will ever sync. `atomic_write` and
    `exclusive_write` are NOT such routes: each ends by syncing the directory.
    """
    path = Path(path)
    if b"\n" in data:
        # Refused BEFORE the file is opened: a rejected record must not be what creates the
        # log, or a reader finds an empty journal where the writer reported an error.
        raise StorageError(
            f"an event record may not contain a newline: {data[:80]!r}. One record per line "
            "is the reader's only framing, so an embedded newline lands as two lines, "
            "neither of which is the record and both of which the reader treats as "
            "authoritative — the torn-tail rule discards only an unterminated LAST line, "
            "and these are terminated.")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND, 0o600)
    except FileExistsError:
        created = False
        fd = os.open(path, os.O_WRONLY | os.O_APPEND, 0o600)
    else:
        # Set HERE and not before the open: an open that fails for any other reason created
        # nothing, and syncing the parent then raises out of a directory that may not exist —
        # replacing the error naming the log with one naming its parent.
        created = True
    try:
        try:
            _write_exactly(fd, data + b"\n", path)
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if created:
            _fsync_dir(path.parent)


# THE SAME RULE `_SEAT_NAME` STATES, FOR THE COMPONENT ONE LEVEL UP. `new_run_id` draws six
# hex characters, but `cli.collect` and `gc.collect` take a run id OFF THE COMMAND LINE and
# `run_root` interpolates it into a path, creates parents, and chmods the result — so an id of
# "x/../../victim" created and chmod-0700'd a directory outside `forge_root()`. Measured
# 2026-08-04.
#
# THE CHARACTER CLASS IS `gc`'S, NOT A SECOND OPINION ABOUT IT. `gc` had this rule first and
# reasoned it out: dots are admitted INSIDE the name and not at the front, which keeps `.` and
# `..` out without forbidding an operator's `r1.2`. Two spellings of one rule would have meant
# `--gc r1.2` passing gc's validation and then dying with a `StorageError` from another module
# over a message gc's own text contradicts. `gc` now references this one, on the precedent it
# already set for `_FORGE_REF_PREFIXES`: the module that decides a value owns the strings that
# spell it. The only thing added here is the length cap, so a run id cannot be the reason a
# path is too long for the filesystem.
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


def new_run_id() -> str:
    return secrets.token_hex(3)   # 6 hex chars; run_root() rejects a collision, see below


def state_root() -> Path:
    """Where this machine keeps state that is not a cache — `XDG_STATE_HOME` or its default.

    An EMPTY value falls back, which is what `or` buys over `os.environ.get(..., default)`:
    `XDG_STATE_HOME=` set to nothing names the filesystem root once joined, and every run
    directory on the machine would be created at `/khenrix-forge`.
    """
    return Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))


def forge_root() -> Path:
    """The one directory this engine writes runs into. Named here so `run_root`, `run_dirs`
    and `gc`'s ownership check cannot come to disagree about which directory that is."""
    return state_root() / "khenrix-forge"


def run_digest(repo_path) -> str:
    """The twelve hex characters that separate one repository's runs from another's.

    ONE SPELLING, READ BY THE WRITER AND BY THE WALK. `run_root` builds a path with it and
    `run_dirs` enumerates with it, and if those were two expressions the failure mode would be
    silent in the worst direction: the walk would find nothing, `--gc all` would report "no
    forge runs are on disk for this repository", and every run would stay on disk unnamed.
    """
    return hashlib.sha256(str(Path(repo_path).resolve()).encode()).hexdigest()[:12]


def run_root(repo_path, run_id: str, must_be_new: bool = True) -> Path:
    """Create (or reattach to) the directory holding this run's work.

    `must_be_new` makes the collision real rather than silent: a second run that draws the
    same run id for the same repo raises FileExistsError instead of sharing a directory
    with the first. Reattaching to a run that already exists — collecting results the
    engine wrote earlier — must pass False.

    IT CREATES, WHICH IS WHY BOTH ITS READ-ONLY CALLERS CLEAN UP AFTER IT. `cli.collect` and
    `gc.collect` are handed a run id off the command line, and a typo would otherwise leave an
    empty directory that the `--gc` walk then reports as a run. Each removes it with `rmdir`,
    which only ever succeeds while it is empty.

    AND THAT CLEANUP IS WHY THE ID IS VALIDATED FIRST. `rmdir` undoes a typo; it does not undo
    an escape, because an escape usually lands on a directory that is not empty. Measured
    2026-08-04: `run_id="x/../../victim"` created `<state>/victim` and chmod-0700'd it. The
    rule below is `_SEAT_NAME`'s, one level up, for the same stated reason.
    """
    if not isinstance(run_id, str) or not _RUN_ID.match(run_id):
        raise StorageError(
            f"a run id is one path component of letters, digits, '-' and '_' (64 max), "
            f"starting alphanumeric: {run_id!r}. It is interpolated into a directory name "
            "under the forge state root, so anything else can create and chmod a directory "
            "outside it — and the `rmdir` both read-only callers use to clean up only ever "
            "succeeds while the target is empty.")
    p = forge_root() / f"{run_digest(repo_path)}-{run_id}"
    if p.resolve().parent != forge_root().resolve():
        # BELT AND BRACES, AND IT CATCHES WHAT THE PATTERN CANNOT: a `forge_root()` that is
        # itself a symlink, or a pre-existing symlinked run directory, resolves out of the
        # tree without the id containing a single suspicious character.
        raise StorageError(
            f"run id {run_id!r} resolves to {p.resolve()}, which is not an immediate child of "
            f"the forge state root {forge_root()}; refusing to create it.")
    p.mkdir(mode=0o700, parents=True, exist_ok=not must_be_new)
    p.chmod(0o700)   # mkdir's mode is masked by umask; chmod is not
    return p


def run_dirs(repo_path) -> tuple[Path, ...]:
    """Every run directory this engine has recorded for `repo_path`, sorted by name.

    BY NAME AND NOT BY AGE: a run id is six random hex characters, so this order is stable
    and says nothing about when a run was opened. A caller that wants oldest-first has to
    read the manifests.

    `()` FOR AN ABSENT STATE DIRECTORY AND A RAISE FOR AN UNREADABLE ONE. The first is
    genuinely "no runs have ever been opened here"; the second is this walk not being able to
    say, and `--gc all` has to be able to tell an operator which one it met — a summed disk
    report that answered "nothing" for a directory it could not list is the one number they
    would act on, silently short by every run on the machine.
    """
    base = forge_root()
    try:
        entries = sorted(base.iterdir())
    except FileNotFoundError:
        return ()
    prefix = run_digest(repo_path) + "-"
    # `is_dir()` follows a link, so a run directory that is a SYMLINK is enumerated here
    # rather than skipped. That is deliberate: `gc` refuses to measure or delete through one
    # and says so per run, which an operator can act on, where dropping it from the listing
    # would leave a directory on their disk that nothing in this engine ever mentions again.
    return tuple(p for p in entries if p.name.startswith(prefix) and p.is_dir())


# One place that says where a run's files live, so a resume and a `--gc` walk look for the
# same names as the writer used. Spelled out here rather than at each call site because the
# writer and the reader are in different processes and often different sessions.
def manifest_path(run_dir) -> Path:
    return Path(run_dir) / "manifest.json"


def journal_path(run_dir) -> Path:
    return Path(run_dir) / "events.jsonl"


def state_path(run_dir) -> Path:
    return Path(run_dir) / "state.json"


def task_bundle_path(run_dir) -> Path:
    return Path(run_dir) / "task-bundle.json"


def task_source_path(run_dir) -> Path:
    """Where the run keeps the BYTES its task-bundle manifest describes.

    `task_bundle_path` holds the manifest — paths, kinds, modes, hashes. `materialize` needs
    the content, and reading it from wherever the operator's directory happened to be would
    make a resume depend on a tree that may be gone: §20 requires the resolved instruction be
    persisted so `--collect` never depends on vanished conversation context. One name here, so
    the writer and the reader cannot drift.
    """
    return Path(run_dir) / "task"


def ledger_path(run_dir) -> Path:
    return Path(run_dir) / "ledger.json"


def handover_path(run_dir) -> Path:
    """§16's delivery record, which §15's `--gc` reads for its licence to delete a synthesis
    worktree. Named here with the rest so the writer and a later `--gc` process agree."""
    return Path(run_dir) / "handover.json"


# Runs of lowercase letters and digits, joined by SINGLE hyphens: `a-b` passes and `a--b`,
# `-a` and `a-` do not. A seat is named for a provider, so this is not a narrowing anyone
# will notice — and the alternative is that a separator or a `..` in the name silently
# relocates the file the run's state lives in. "Internal hyphens" is what this said, and it
# understates the rule by exactly the doubled hyphen a reader would expect it to admit.
_SEAT_NAME = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*\Z")


def seat_state_path(run_dir, name: str) -> Path:
    if not isinstance(name, str) or not _SEAT_NAME.match(name):
        raise StorageError(
            f"a seat name is runs of lowercase letters and digits joined by single hyphens: "
            f"{name!r}. It becomes a filename inside the run directory, so anything else can "
            "put a run's state where nothing accounts for it.")
    return Path(run_dir) / f"seat-{name}.json"


def seat_bundle_path(run_dir, name: str) -> Path:
    """Where a seat's serialized `CandidateBundle` lives, beside its record.

    THE NAME GOES THROUGH `seat_state_path` rather than re-matching `_SEAT_NAME`, because a
    second copy of that predicate is a second chance for one spelling to admit what the other
    refuses — and this one builds a path from argv-derived text, which is exactly the
    interpolation `run_root` was fixed for.
    """
    return seat_state_path(run_dir, name).with_suffix(".bundle")


def seat_names(run_dir) -> tuple[str, ...]:
    """Every seat with a record in `run_dir`, sorted — the inverse of `seat_state_path`.

    Kept beside it so the glob and the name it builds cannot drift apart: a resume enumerates
    seats it never launched, so the writer's spelling is the only thing that says which files
    are seats at all.

    NOT VALIDATED here. A name this directory holds that `seat_state_path` would refuse to
    produce is reported rather than skipped, so the caller that reads it meets the refusal —
    a filter here would make the enumeration quietly narrower than the directory.
    """
    return tuple(p.name[len("seat-"):-len(".json")]
                 for p in sorted(Path(run_dir).glob("seat-*.json")))


@dataclass(frozen=True)
class Quota:
    """Caps that FAIL CLOSED with a report line — never a silent truncation."""
    max_files: int
    max_file_bytes: int
    max_total_bytes: int

    @classmethod
    def default(cls) -> "Quota":
        """What the PRE-LAUNCH SCREEN is willing to decode out of the user's selected
        baseline — source the user wrote, before any provider starts. Setup has not run at
        that point, so no dependency tree is in scope here."""
        return cls(max_files=5000, max_file_bytes=32 * 1024 * 1024,
                   max_total_bytes=512 * 1024 * 1024)

    @classmethod
    def for_harvest(cls) -> "Quota":
        """What an inventory of a SEAT may weigh — a different question from `default`.

        `harvest`'s design turns on setup's output (`node_modules`, `.venv`) being PRESENT
        in `Fsetup`, so that `Fsetup -> Fwork` differences it out of the artifact path set.
        Under `default` the inventory that must observe it fails closed first: measured,
        `HarvestError: files: 5001 > 5000` on a 5200-file `node_modules` — and this
        stdlib-only repository's own worktree is 5654 files with no dependencies at all.

        All three caps move because all three were measured wrong for this question, on
        this machine's uv package cache: one cached package tree is 6,071 files / 337 MB,
        and the largest single file inside one is 117.7 MiB — nearly 4x `default`'s
        per-file cap. Leaving the byte caps alone would only move the same fail-closed
        refusal one step down the same seat.

        What does NOT move is the fail-closed property: these are hard caps that still make
        `snapshot.take` return a breach and `harvest.record` raise, never truncate. They are
        sized to separate a dependency tree from a runaway, not to admit everything.
        """
        return cls(max_files=200_000, max_file_bytes=512 * 1024 * 1024,
                   max_total_bytes=8 * 1024 * 1024 * 1024)

    @classmethod
    def for_task_bundle(cls) -> "Quota":
        """What §20's INSTRUCTION CLOSURE may weigh — a third question from the two above.

        `default` sizes the user's whole selected baseline and `for_harvest` a seat that
        has run `npm ci`. A task bundle is neither: it is a skill's resolved closure,
        materialized identically into three clones and hashed to prove it. Measured on
        this machine (2026-08-03): the entire installed claude plugin closure is 76 files
        / 1 257 929 bytes with a 134 044-byte largest file, and the largest directory in
        `shared/skills/` is `skill-tuneup` at 8 files / 149 149 bytes / 91 382 max. These
        caps are ~26x that by count and ~50x by bytes, which separates a closure from a
        runaway without admitting a dependency tree by accident.

        Fail-closed like its siblings: `breach` returns a description and the caller
        RAISES. Nothing here truncates.
        """
        return cls(max_files=2000, max_file_bytes=4 * 1024 * 1024,
                   max_total_bytes=64 * 1024 * 1024)

    def breach(self, *, files: int, file_bytes: int, total_bytes: int):
        """Return a human-readable breach description, or None when within limits."""
        if files > self.max_files:
            return f"files: {files} > {self.max_files}"
        if file_bytes > self.max_file_bytes:
            return f"file_bytes: {file_bytes} > {self.max_file_bytes}"
        if total_bytes > self.max_total_bytes:
            return f"total_bytes: {total_bytes} > {self.max_total_bytes}"
        return None
