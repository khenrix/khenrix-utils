"""The append-only record that makes a crashed run legible (spec §14.1).

Exactly-once is not deliverable and the spec opens by conceding it: arbitrary setup commands
and LLM edits are not idempotent, and a SIGKILL after setup mutated a database but before its
completion record landed leaves nothing on disk separating never-started from partly-ran from
completed. What IS deliverable is a record in which those three are DISTINGUISHABLE, and that
is the whole job of this file.

The mechanism is write-ahead: `intent(kind)` is appended before the operation runs and
`done(kind)` after it returns, so a crash between them leaves a start with no done — an
ORPHAN. An orphan is not a failure. It is the one shape that says "this ran and we do not know
how it ended", which §14.1 requires be recorded `outcome_unknown` and never silently retried.

Two framing rules keep the file readable after a crash: an unterminated LAST line is
discarded, while a line that fails to parse anywhere else is refused. The second is what stops
one damaged record from silently dropping the facts written after it. It rests on the writer's
one invariant — no COMPLETE record is ever removed or rewritten — so a damaged record with
complete records behind it cannot have come from a crash. The invariant is not "the file only
grows": `record` truncates a torn tail, and that is the single exception, an unterminated line
that was never a record.

Taken alone those two leave a resumed run unable to read its own journal: it appends behind
the tear, and the tear is then in the middle. `record` reconciles them by dropping a torn tail
before it appends, so the crash this file exists to survive is not also what makes the file
unreadable.
"""
import fcntl
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import storage


class JournalError(RuntimeError):
    """A log that cannot be read as a sequence of facts, or a record that would stop it
    being one."""


_START = "_start"
_DONE = "_done"

# The fields a record IS, as opposed to the payload it carries. Enforced on read, so a
# producer that stops writing one is caught by the next reader rather than by whoever
# eventually needs the missing field.
_CORE = ("seq", "event", "operation_id", "at")

_BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"
_PROC_STAT_PATH = "/proc/self/stat"
_UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class Event:
    """One record. `data` is everything that is not one of the four named fields, so it
    carries the process identity too — a reader asking "is this operation still running"
    needs that beside the event, not in a sidecar it has to correlate."""
    seq: int
    event: str
    operation_id: str
    at: str
    data: dict


def intent(kind: str) -> str:
    """The name to append BEFORE the operation runs."""
    return f"{kind}{_START}"


def done(kind: str) -> str:
    """The name to append after it returns."""
    return f"{kind}{_DONE}"


def orphans(events) -> tuple[Event, ...]:
    """Every start with no matching done, oldest first — the `outcome_unknown` candidates.

    Pairs are keyed on (kind, operation_id), not on the id alone: an operation_id names one
    operation rather than one seat's whole life, and matching on it alone would let any later
    `_done` close any earlier `_start` carrying the same id — which erases exactly the
    operation whose outcome nobody knows.

    Three shapes raise instead of being interpreted. A `_done` with no `_start` says the intent
    record was lost, which the fsync discipline rules out for a record whose write returned. A
    repeated `_start`, or a second `_done` closing a pair that is already closed, says the
    operation_id does not identify one operation — and then "did it finish" has two answers and
    no reader may pick one.
    """
    started: dict[tuple[str, str], Event] = {}
    finished: set[tuple[str, str]] = set()
    for e in events:
        pair = _pair(e.event)
        if pair is None:
            continue
        kind, half = pair
        key = (kind, e.operation_id)
        if half == "start":
            if key in started:
                raise JournalError(
                    f"{e.event} repeats for operation {e.operation_id!r} at seq {e.seq}")
            started[key] = e
        elif key not in started:
            raise JournalError(
                f"{e.event} at seq {e.seq} has no matching {intent(kind)} for operation "
                f"{e.operation_id!r}")
        elif key in finished:
            raise JournalError(
                f"{e.event} repeats for operation {e.operation_id!r} at seq {e.seq}")
        else:
            finished.add(key)
    return tuple(e for key, e in started.items() if key not in finished)


class Journal:
    """One append-only log file, serialized across every writer by an advisory lock.

    WHAT THE LOCK IS FOR, AND IT IS NOT THE BYTES. `storage.append_line` opens `O_APPEND` and
    writes the record and its terminator in ONE call, so two writers' bytes can never
    interleave — that half was always safe. What races is the NUMBER: `seq` is derived from
    the file and counted on from there, and `_parse` requires a dense sequence in which record
    N carries seq N. Two writers that each derive 5 both write 5, the second lands at line 6,
    and every later `read()` refuses the WHOLE file. That failure is total by design, so the
    fix belongs at the allocation rather than at the reader.

    SO THE SEQ IS DERIVED AND APPENDED INSIDE ONE CRITICAL SECTION. Holding the lock across
    both is the whole point: a lock around the append alone would still let two writers derive
    the same number first, and a lock around the derivation alone would let a second writer
    append between the first's derivation and its own write.

    RE-DERIVED EVERY TIME, NOT CACHED ACROSS THE LOCK. A cached counter is exactly what goes
    stale when another writer appends, and re-reading is what `_resolve_last_seq` already calls
    the point rather than a side effect — it validates the whole file on the way. A run's
    journal holds a few hundred records, so this is microseconds against clones and provider
    calls.

    A FRESH DESCRIPTOR PER ACQUISITION, WHICH IS WHAT MAKES IT WORK BETWEEN THREADS. `flock`
    is held by the open file DESCRIPTION, so two threads sharing one cached fd would both
    "hold" the same lock and serialize nothing. Opening per acquisition gives each waiter its
    own description, so threads in one process block each other exactly as separate processes
    do — and the fleet's builders are threads.

    ADVISORY, AND ONLY THIS CLASS TAKES IT. A writer that bypasses `record` is unaffected;
    nothing in this package has one, and the alternative — a mandatory lock — is not portable.
    """

    def __init__(self, path):
        self.path = Path(path)
        self._identity = _identity()
        self._last_seq = None      # derived from the file at the first append

    def read(self) -> tuple[Event, ...]:
        """Every complete record, oldest first. `()` when the file does not exist — a run
        that has recorded nothing is not an error."""
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return ()
        return _parse(raw, self.path)

    def record(self, event: str, *, operation_id: str, **data) -> Event:
        """Append one record and return it. On the platter before it returns."""
        if not isinstance(event, str) or not event:
            raise JournalError(f"an event needs a name: {event!r}")
        if not isinstance(operation_id, str) or not operation_id:
            raise JournalError(f"{event} needs an operation_id: {operation_id!r}")
        identity = self._identity_now()
        reserved = sorted(set(data) & (set(_CORE) | set(identity)))
        if reserved:
            # Not corruption, which is why it is refused: the result would be a WELL-FORMED
            # record whose identity or ordering is the caller's fiction, and every later
            # reader believes it.
            raise JournalError(f"{event} payload keys {reserved} are the record's own fields")
        at = datetime.now(timezone.utc).isoformat()
        payload = {**identity, **data}
        with self._exclusive():
            return self._append_locked(event, operation_id, at, payload)

    def _append_locked(self, event, operation_id, at, payload) -> Event:
        """The seq allocation and the append, which have to happen together — see the class.

        THE CALLER HOLDS THE LOCK. Split out rather than nested so the critical section is one
        function and cannot grow a return path that leaves it early.
        """
        # RE-DERIVED UNDER THE LOCK, never taken from the cache: another writer may have
        # appended since this one last did, and its `seq` is the one on the file now.
        self._last_seq = None
        seq = self._resolve_last_seq() + 1
        row = {"seq": seq, "event": event, "operation_id": operation_id, "at": at, **payload}
        try:
            # sort_keys so one fact has one spelling on disk: without it the same event
            # recorded from two call sites differs byte-for-byte with the caller's keyword
            # order, and a line cannot be compared or content-addressed without re-parsing.
            line = json.dumps(row, sort_keys=True).encode("utf-8")
        except (TypeError, ValueError) as e:
            raise JournalError(f"{event} carries a payload json cannot serialize: {e}") from e
        try:
            storage.append_line(self.path, line)
        except BaseException:
            # An append that raised may have written nothing (a record refused before the file
            # was opened) or part of a record (a short write, reported after the bytes landed).
            # Only a re-read can tell which, so the cached counter is dropped rather than
            # reused: the next attempt re-derives it from the file, dropping a torn tail on the
            # way.
            self._last_seq = None
            raise
        self._last_seq = seq
        return Event(seq=seq, event=event, operation_id=operation_id, at=at, data=payload)

    @contextmanager
    def _exclusive(self):
        """Hold this journal's advisory write lock for the body.

        A SIDECAR FILE, NOT THE JOURNAL ITSELF. The journal is truncated by
        `_drop_a_torn_tail` and created by `append_line`'s own `O_EXCL` dance; locking a
        separate path keeps the lock's lifetime independent of both, so a writer waiting on
        the lock is never waiting on a descriptor another writer is about to replace.

        `O_CLOEXEC` so a lock this process holds is not inherited by a builder's subprocess —
        `run_seat` spawns provider CLIs, and a child holding the journal lock past its
        parent's release would block the fleet on a process that has no idea it is a writer.
        """
        fd = os.open(str(self.path) + ".lock",
                     os.O_CREAT | os.O_WRONLY | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _identity_now(self) -> dict:
        """The identity of the process appending now, which is not always the one that opened
        the journal.

        A fork inherits this object along with the pid and start time it cached, and both
        belong to the parent. A child recording under them is fail-OPEN in the one field whose
        job is to fail closed: the parent is still alive, so a liveness check answers "still
        running" for an operation that died with the child. The boot id cannot go stale the
        same way — a reboot ends the process holding it — but it is re-read with the rest
        because the cost is one small file per fork, not per record.

        The inherited sequence counter goes too: the parent may have appended since the fork.
        """
        if self._identity["pid"] != os.getpid():
            self._identity = _identity()
            self._last_seq = None
        return self._identity

    def _resolve_last_seq(self) -> int:
        """The last seq on disk, derived the first time this writer appends, then counted on.

        Deriving it validates the whole file, which is the point rather than a side effect:
        `_parse` stops at the first damaged record, so appending to a log the reader refuses
        buries every new fact behind the break. Those records are written and fsynced and no
        reader will ever reach them, which is worse than not writing them — the engine holds a
        record it cannot read and does not know it.
        """
        if self._last_seq is None:
            self._drop_a_torn_tail()
            events = self.read()
            self._last_seq = events[-1].seq if events else 0
        return self._last_seq

    def _drop_a_torn_tail(self) -> None:
        """Truncate an unterminated final line rather than append behind it.

        The bytes discarded were never acknowledged to anyone. `storage.append_line` writes a
        record and its terminator in one call and fsyncs before returning, so an unterminated
        tail belongs to a write whose caller was told it failed or was killed before it was
        told anything. Keeping it would move it into the middle of the file on the next
        append, where it is corruption rather than a crash — and a run resuming after a crash
        is precisely when that append happens.

        No directory fsync: the name is not changing, and `append_line` remains the only route
        by which this file is created, which is where the directory entry is made durable.
        """
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return
        if not raw or raw.endswith(b"\n"):
            return
        fd = os.open(self.path, os.O_WRONLY)
        try:
            os.ftruncate(fd, raw.rfind(b"\n") + 1)   # 0 when nothing at all is terminated
            os.fsync(fd)
        finally:
            os.close(fd)


def _pair(event: str):
    """(kind, "start"|"done") for a write-ahead event, None for a plain one."""
    if event.endswith(_START):
        return event[:-len(_START)], "start"
    if event.endswith(_DONE):
        return event[:-len(_DONE)], "done"
    return None


def _read_boot_id() -> tuple[str, str]:
    """The machine's boot identity and WHERE IT CAME FROM, in that order.

    The source travels with the value because the fallback is a string too, and a fallback
    that reads like a boot id makes "did this operation survive a reboot" answerable and
    wrong: two records written in different boots would both carry the sentinel and compare
    equal. A consumer may compare two boot ids only when both records say `proc`.

    Degrading rather than refusing outright is deliberate, and it is the opposite call from the
    rest of a package that narrows to Linux and fails loudly there. The start/done pair needs
    nothing from /proc, and it is the pair that makes never-started, partly-ran and completed
    distinguishable. So an unreadable /proc costs the liveness question, which then fails
    closed to `outcome_unknown`; refusing instead would cost the record itself, and a crashed
    run with no record is the state §14.1 exists to prevent.
    """
    try:
        value = Path(_BOOT_ID_PATH).read_text().strip()
    except OSError:
        return _UNAVAILABLE, _UNAVAILABLE
    return (value, "proc") if value else (_UNAVAILABLE, _UNAVAILABLE)


def _read_process_start() -> tuple[str, str]:
    """Field 22 of /proc/self/stat — `starttime`, in clock ticks since boot — and its source.

    A pid alone answers nothing, because pids are recycled: "is pid 4211 still the process
    that wrote this" needs the start time to reject a recycled pid, and the boot id to make
    the start time comparable at all, since it is counted from boot.

    Field 2 is the executable name in parentheses and may itself contain spaces and
    parentheses, so splitting the line on whitespace mis-numbers every field after it for a
    process named `(a b)`. The fields are taken after the LAST `)`, where field N lands at
    index N-3.
    """
    try:
        raw = Path(_PROC_STAT_PATH).read_text()
    except OSError:
        return _UNAVAILABLE, _UNAVAILABLE
    cut = raw.rfind(")")
    fields = raw[cut + 1:].split() if cut >= 0 else []
    if len(fields) < 20 or not fields[19].isdigit():
        return _UNAVAILABLE, _UNAVAILABLE
    return fields[19], "proc"


def _identity() -> dict:
    boot_id, boot_source = _read_boot_id()
    process_start, start_source = _read_process_start()
    return {"pid": os.getpid(), "boot_id": boot_id, "boot_id_source": boot_source,
            "process_start": process_start, "process_start_source": start_source}


def _parse(raw: bytes, path) -> tuple[Event, ...]:
    lines = raw.split(b"\n")
    # split() leaves the bytes after the last terminator as the final element: empty when the
    # file ends with one, and the unterminated tail of a write that never completed when it
    # does not. §14.1 discards exactly that tail, so both cases drop it.
    lines.pop()
    events = tuple(_event(line, n, path) for n, line in enumerate(lines, start=1))
    for n, e in enumerate(events, start=1):
        if e.seq != n:
            # A dense sequence is what an append-only writer produces, so a break in it is a
            # record removed or rewritten, or a second writer's appends interleaved with the
            # first's — none of which the file can still claim to be a sequence of facts under.
            raise JournalError(f"{path}: record {n} carries seq {e.seq}, expected {n}")
    return events


def _event(line: bytes, n: int, path) -> Event:
    try:
        row = json.loads(line)
    except ValueError as e:
        raise JournalError(f"{path}: record {n} is not JSON: {line[:80]!r}") from e
    if not isinstance(row, dict):
        raise JournalError(f"{path}: record {n} is not an object: {line[:80]!r}")
    missing = [k for k in _CORE if k not in row]
    if missing:
        raise JournalError(f"{path}: record {n} is missing {missing}")
    if not isinstance(row["seq"], int) or isinstance(row["seq"], bool):
        raise JournalError(f"{path}: record {n} has a non-integer seq {row['seq']!r}")
    for key in ("event", "operation_id", "at"):
        if not isinstance(row[key], str):
            raise JournalError(f"{path}: record {n} has a non-string {key} {row[key]!r}")
    return Event(seq=row["seq"], event=row["event"], operation_id=row["operation_id"],
                 at=row["at"], data={k: v for k, v in row.items() if k not in _CORE})
