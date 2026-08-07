"""The append-only event log and what it can still say after a crash (spec §14.1)."""
import ast
import json
import os
import re
import subprocess
import sys
import traceback
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

from forge import journal, storage  # noqa: E402

# A real boot id is a UUID. The fallback must not be mistakable for one by a reader that
# only ever sees the value.
_UUIDISH = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _rows(path: Path) -> list[dict]:
    """Every complete record as raw JSON, framed the way the file frames it.

    `split(b"\\n")` and not `splitlines()`, which also breaks on a bare carriage return. The
    writer escapes one, so on every record this suite writes the two agree — this helper
    frames the file the way the file is framed rather than agreeing by coincidence with a
    splitter that would mis-frame bytes the writer did not produce.
    """
    return [json.loads(line) for line in path.read_bytes().split(b"\n") if line]


def test_a_torn_final_line_is_discarded_and_the_rest_is_authoritative(tmp_path):
    """§14.1's rule verbatim. A process killed mid-write leaves a partial last record; every
    record before it reached the platter and is a fact."""
    p = tmp_path / "events.jsonl"
    j = journal.Journal(p)
    j.record("seat_start", operation_id="op1", seat="claude")
    j.record("seat_done", operation_id="op1", exit_code=0)
    with open(p, "ab") as fh:
        fh.write(b'{"event":"seat_start","operation_id":"op2"')   # no brace, no newline
    events = journal.Journal(p).read()
    assert [e.event for e in events] == ["seat_start", "seat_done"]


def test_a_torn_line_that_is_not_last_is_a_refusal_not_a_skip(tmp_path):
    """Tolerating a torn line ANYWHERE would let one corrupt record silently drop a fact that
    was fully written after it. No complete record is ever removed or rewritten here, so a
    damaged one with complete records behind it is corruption rather than a crash.

    The DIAGNOSIS is asserted, not just the raise, because this fixture is numbered 1 then 3
    and so trips a second net: a reader that silently skipped the torn line would produce a
    sequence gap and raise anyway. Measured — that is not hypothetical, it is what the
    torn-line-tolerant mutant does — and without this assertion the test would report the
    density rule as evidence for the framing rule.
    """
    p = tmp_path / "events.jsonl"
    p.write_bytes(b'{"event":"a","operation_id":"o","seq":1,"at":"t"}\n'
                  b'{"broken"\n'
                  b'{"event":"c","operation_id":"o","seq":3,"at":"t"}\n')
    with pytest.raises(journal.JournalError) as e:
        journal.Journal(p).read()
    assert "record 2 is not JSON" in str(e.value), str(e.value)


def test_a_record_missing_a_required_field_is_a_refusal(tmp_path):
    """A row that parses as JSON but is not a record. Left to the reader's own indexing it
    surfaces as a KeyError or a TypeError from inside the parser — an exception no caller can
    tell from a bug in the parser, in the one place where "the file is damaged" is the answer
    that matters. `seq` is the sharpest case: it is what orders the file."""
    p = tmp_path / "e.jsonl"
    for row in (b'{"event":"a","operation_id":"o","at":"t"}',            # no seq
                b'{"event":"a","operation_id":"o","seq":"1","at":"t"}',  # seq is not a number
                b'{"event":null,"operation_id":"o","seq":1,"at":"t"}',
                b'["not","a","record"]'):                                # valid JSON, not a row
        p.write_bytes(row + b"\n")
        with pytest.raises(journal.JournalError):
            journal.Journal(p).read()


def test_an_operation_that_started_and_never_finished_is_an_orphan(tmp_path):
    j = journal.Journal(tmp_path / "e.jsonl")
    j.record(journal.intent("setup"), operation_id="op1", seat="codex")
    j.record(journal.intent("setup"), operation_id="op2", seat="agy")
    j.record(journal.done("setup"), operation_id="op2", exit_code=0)
    orphans = journal.orphans(journal.Journal(tmp_path / "e.jsonl").read())
    assert [e.operation_id for e in orphans] == ["op1"]


def test_a_done_without_a_start_is_a_refusal(tmp_path):
    """The write-ahead rule is what makes an orphan mean anything. A `_done` alone says the
    intent record was lost, which is the one thing this file's fsync discipline rules out —
    so it is corruption, not a state to interpret."""
    j = journal.Journal(tmp_path / "e.jsonl")
    j.record(journal.done("setup"), operation_id="op9", exit_code=0)
    with pytest.raises(journal.JournalError):
        journal.orphans(journal.Journal(tmp_path / "e.jsonl").read())


def test_matching_is_per_kind_so_a_verify_does_not_close_a_setup(tmp_path):
    """An operation_id names one operation, not one seat's whole life. Keying the pair on
    the id alone would let any later `_done` close any earlier `_start` that happened to
    carry the same id, and the orphan — the outcome nobody knows — would disappear."""
    j = journal.Journal(tmp_path / "e.jsonl")
    j.record(journal.intent("setup"), operation_id="op1")
    j.record(journal.intent("verify"), operation_id="op1")
    j.record(journal.done("verify"), operation_id="op1")
    assert [e.event for e in journal.orphans(j.read())] == ["setup_start"]


def test_two_starts_for_one_operation_is_a_refusal(tmp_path):
    """With two starts and one done, "did this operation finish" has two answers and the
    engine would have to pick one. The id is the identity; a repeat means it is not."""
    j = journal.Journal(tmp_path / "e.jsonl")
    j.record(journal.intent("setup"), operation_id="op1")
    j.record(journal.intent("setup"), operation_id="op1")
    with pytest.raises(journal.JournalError):
        journal.orphans(j.read())


def test_the_sequence_is_dense_and_a_gap_is_a_refusal(tmp_path):
    p = tmp_path / "e.jsonl"
    j = journal.Journal(p)
    j.record("a", operation_id="o")
    j.record("b", operation_id="o")
    assert [e.seq for e in journal.Journal(p).read()] == [1, 2]
    rows = [json.loads(line) for line in p.read_text().splitlines()]
    rows[1]["seq"] = 7
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    with pytest.raises(journal.JournalError):
        journal.Journal(p).read()


def test_a_second_writer_instance_continues_the_sequence(tmp_path):
    """Restarting the count at 1 would not merely renumber the log — it collides with a
    number already on disk, and the density rule then refuses the whole file. `--collect` is
    a fresh process on an existing journal, so this is the ordinary path, not an edge."""
    p = tmp_path / "e.jsonl"
    journal.Journal(p).record("a", operation_id="o")
    journal.Journal(p).record("b", operation_id="o")
    assert [e.seq for e in journal.Journal(p).read()] == [1, 2]


def test_a_torn_tail_is_dropped_by_the_next_write_so_a_resumed_run_stays_readable(tmp_path):
    """Where the two framing rules meet, and where applying each on its own is a trap.

    A crash tears the last line. The resumed run appends behind it — and the tear is now in
    the MIDDLE, where the reader refuses the file. The crash the journal exists to survive
    would be the thing that makes the journal unreadable from then on. So the writer drops the
    torn tail before appending; the bytes it drops belong to a write whose caller was never
    told it succeeded.
    """
    p = tmp_path / "e.jsonl"
    journal.Journal(p).record("seat_start", operation_id="op1")
    with open(p, "ab") as fh:
        fh.write(b'{"event":"seat_done","operation_id":"op1"')
    journal.Journal(p).record("seat_done", operation_id="op1")
    assert [(e.seq, e.event) for e in journal.Journal(p).read()] == \
        [(1, "seat_start"), (2, "seat_done")]


def test_a_journal_that_cannot_be_read_gains_no_record(tmp_path):
    """The reader stops at the first damaged record, so anything appended behind the break is
    written, fsynced and unreachable forever. The engine would then hold a durable record of
    facts no reader can ever see, and would have no way to know it.

    "Gains no record", not "is never touched": a refused append can still have dropped a torn
    TAIL first, which is bytes no writer was ever told had landed. The fixture below ends in a
    terminator so the file is byte-identical, and the second one carries a tail as well, so
    the property is asserted in both shapes rather than only the quiet one.
    """
    p = tmp_path / "e.jsonl"
    p.write_bytes(b'{"event":"a","operation_id":"o","seq":1,"at":"t"}\n{"broken"\n')
    before = p.read_bytes()
    with pytest.raises(journal.JournalError):
        journal.Journal(p).record("x", operation_id="o")
    assert p.read_bytes() == before

    q = tmp_path / "f.jsonl"
    q.write_bytes(b'{"event":"a","operation_id":"o","seq":1,"at":"t"}\n{"broken"\n{"tor')
    with pytest.raises(journal.JournalError):
        journal.Journal(q).record("x", operation_id="o")
    assert b'"event":"x"' not in q.read_bytes(), "the refused record must not have landed"
    assert q.read_bytes().endswith(b'{"broken"\n'), \
        "only the unacknowledged tail may go, and the damage itself must remain"


def test_a_run_that_has_recorded_nothing_reads_empty_rather_than_failing(tmp_path):
    assert journal.Journal(tmp_path / "never-written.jsonl").read() == ()
    assert journal.orphans(()) == ()


def test_every_record_carries_the_identity_a_crash_needs(tmp_path):
    """§14.1 requires PID, process start time and boot id: a PID alone is reused, so
    "is that operation still running" cannot be asked without the other two."""
    p = tmp_path / "e.jsonl"
    journal.Journal(p).record("x", operation_id="o")
    row = _rows(p)[0]
    for key in ("pid", "process_start", "boot_id"):
        assert row[key], key
    assert row["pid"] == os.getpid()
    assert _UUIDISH.match(row["boot_id"]), row["boot_id"]
    assert row["boot_id_source"] == "proc" and row["process_start_source"] == "proc"
    assert row["process_start"].isdigit(), row["process_start"]


def test_an_identity_that_could_not_be_read_says_so_instead_of_looking_like_one(
        tmp_path, monkeypatch):
    """A fallback that reads like a boot id makes "did this survive a reboot" answerable and
    WRONG: two records from different boots would both carry the sentinel and compare equal.
    So the source travels with the value, and the value itself is not UUID-shaped.

    Degrading rather than refusing is deliberate. The start/done pair needs nothing from
    /proc, and it is what makes never-started, partly-ran and completed distinguishable; an
    unreadable /proc costs the liveness question (which then fails closed to
    outcome_unknown), not the record.
    """
    monkeypatch.setattr(journal, "_BOOT_ID_PATH", str(tmp_path / "absent-boot-id"))
    monkeypatch.setattr(journal, "_PROC_STAT_PATH", str(tmp_path / "absent-stat"))
    p = tmp_path / "e.jsonl"
    journal.Journal(p).record("x", operation_id="o")
    row = _rows(p)[0]
    assert row["boot_id_source"] == "unavailable"
    assert row["process_start_source"] == "unavailable"
    assert not _UUIDISH.match(row["boot_id"]), row["boot_id"]
    assert row["pid"] == os.getpid(), "the one identity that never needed /proc"


def test_a_process_name_with_spaces_does_not_mis_number_the_stat_fields(tmp_path, monkeypatch):
    """Field 2 of /proc/[pid]/stat is the process name IN PARENTHESES and may contain spaces
    and parentheses of its own.

    Splitting the whole line on whitespace then shifts every field after it, and
    `process_start` becomes a different field of the same process that still looks like a
    plausible number. That is the failure worth a fixture: silent, and it makes "is this pid
    still the one that wrote the record" answer confidently and wrong.

    A synthetic file stands in for /proc so the expected value can be exact — each field here
    carries its own number, so a mis-numbered read returns a WRONG number rather than falling
    back, and a fallback would mask the defect as mere unavailability. The sibling test below
    is what shows this shape is one /proc really produces.
    """
    fake = tmp_path / "stat"
    fake.write_text("4211 (weird ) name) " + " ".join(str(n) for n in range(3, 53)) + "\n")
    monkeypatch.setattr(journal, "_PROC_STAT_PATH", str(fake))
    assert journal._read_process_start() == ("22", "proc")


def test_a_real_process_can_carry_the_hostile_name_the_fixture_above_invents(tmp_path):
    """Without this, the fixture above is a shape I made up and the parser is hardened against
    nothing. Measured: `comm` comes from the EXECUTABLE'S BASENAME, truncated to 15 characters
    — not from argv[0], which `exec -a` changes and which leaves `comm` untouched. So a
    symlink whose own name holds a space and a `)` is enough to produce one, and the engine
    runs providers out of paths it did not choose.

    The naive split is run inside the same process for contrast, so this also records that the
    two parses genuinely disagree there rather than agreeing by luck.
    """
    link = tmp_path / "we) ird"
    link.symlink_to(sys.executable)
    prog = (
        "import pathlib, sys;"
        f"sys.path.insert(0, {str(ROOT / 'shared' / 'lib')!r});"
        "from forge import journal;"
        "raw = pathlib.Path('/proc/self/stat').read_text();"
        "naive = raw.split()[2:];"
        "print(raw[raw.index('('):raw.rindex(')') + 1]);"
        "print(journal._read_process_start());"
        "print(naive[19] if len(naive) > 19 else 'short')"
    )
    r = subprocess.run([str(link), "-c", prog], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    comm, parsed, naive = r.stdout.splitlines()

    assert comm == "(we) ird)", comm      # the premise: a real comm with a space and a paren
    value, source = ast.literal_eval(parsed)
    assert source == "proc" and value.isdigit(), parsed
    assert naive != value, "the naive split happened to agree, so this proves nothing"


def test_the_machine_identity_is_read_once_per_process_not_once_per_record(
        tmp_path, monkeypatch):
    """A boot id cannot change under a running process — a reboot ends it — so a per-record
    read returns the same bytes and costs an open per record for them. It IS re-read after a
    fork, where the pid beside it did genuinely change; the call counted here is the one at
    construction."""
    calls = []
    real = journal._read_boot_id
    monkeypatch.setattr(journal, "_read_boot_id",
                        lambda: (calls.append(1), real())[1])
    j = journal.Journal(tmp_path / "e.jsonl")
    for i in range(3):
        j.record("x", operation_id=f"op{i}")
    assert len(calls) == 1, f"read {len(calls)} times for 3 records"


def test_the_identity_follows_a_fork_not_the_process_that_opened_the_journal(tmp_path):
    """A fork inherits this object, and the pid it cached belongs to the parent.

    A child recording under the parent's identity is fail-OPEN in the one field whose job is
    to fail closed: the parent is still alive, so a liveness check answers "still running"
    for an operation that died with the child, and the orphan §14.1 requires be called
    outcome_unknown is silently resolved the wrong way.
    """
    p = tmp_path / "e.jsonl"
    j = journal.Journal(p)
    j.record("parent", operation_id="op-parent")
    err = tmp_path / "child.err"

    child_pid = os.fork()
    if child_pid == 0:                              # pragma: no cover - the child exits here
        code = 0
        try:
            j.record("child", operation_id="op-child")
        except BaseException:
            err.write_text(traceback.format_exc())
            code = 1
        os._exit(code)                              # never run pytest's teardown twice

    _, status = os.waitpid(child_pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0, \
        err.read_text() if err.exists() else "the child died without recording anything"

    parent_row, child_row = _rows(p)
    assert parent_row["pid"] == os.getpid()
    assert child_row["pid"] == child_pid
    assert child_row["process_start"] != parent_row["process_start"]
    assert child_row["boot_id"] == parent_row["boot_id"], "same machine, same boot"
    assert child_row["seq"] == 2, "the inherited counter was re-derived from the file"


def test_a_payload_may_not_overwrite_the_record_s_own_fields(tmp_path):
    """Not corruption, which is why it is refused: a payload key landing on `pid` or `seq`
    produces a WELL-FORMED record whose identity or ordering is the caller's fiction, and
    every later reader believes it."""
    p = tmp_path / "e.jsonl"
    for bad in ({"pid": 1}, {"seq": 99}, {"boot_id": "x"}, {"at": "t"}):
        with pytest.raises(journal.JournalError):
            journal.Journal(p).record("x", operation_id="o", **bad)
    assert not p.exists(), "a refused record created the log it was refused from"


def test_a_payload_json_cannot_serialize_is_refused_before_anything_is_written(tmp_path):
    p = tmp_path / "e.jsonl"
    with pytest.raises(journal.JournalError):
        journal.Journal(p).record("x", operation_id="o", where=Path("/tmp"))
    assert not p.exists()


def test_a_record_is_one_line_even_when_its_payload_holds_a_newline(tmp_path):
    """`json.dumps` escapes an embedded newline (and a carriage return) rather than emitting
    it, so a payload holding one stays a single line and `storage.append_line`'s refusal
    never fires on legitimate data. Both bytes are checked because they are exactly the two
    `bytes.splitlines` treats as terminators, and it is what this assertion frames with."""
    p = tmp_path / "e.jsonl"
    journal.Journal(p).record("x", operation_id="o", stderr="line one\nline two\rback")
    assert len(p.read_bytes().splitlines()) == 1
    assert journal.Journal(p).read()[0].data["stderr"] == "line one\nline two\rback"


def test_the_field_order_does_not_depend_on_the_callers_keyword_order(tmp_path):
    """One fact must have one spelling on disk. Without a fixed key order the same event
    recorded from two call sites differs byte-for-byte, so a line cannot be compared,
    deduplicated or content-addressed without re-parsing it first."""
    p = tmp_path / "e.jsonl"
    j = journal.Journal(p)
    j.record("x", operation_id="o", alpha=1, zulu=2)
    j.record("x", operation_id="o", zulu=2, alpha=1)
    first, second = _rows(p)
    assert list(first) == list(second), "one fact, two spellings"
    assert list(first) == sorted(first)


def test_the_event_record_returns_is_the_event_read_gives_back(tmp_path):
    p = tmp_path / "e.jsonl"
    written = journal.Journal(p).record("seat_start", operation_id="op1", seat="claude")
    assert journal.Journal(p).read() == (written,)
    assert written.data["seat"] == "claude"


def test_the_append_goes_through_the_durable_writer(tmp_path, monkeypatch):
    """Write-ahead is worth nothing unless the intent record is on the platter before the
    operation it precedes starts, and `storage.append_line` is what makes that true. A
    buffered `open(..., "ab")` returns with the record in Python's own buffer, and even after
    close it has only reached the page cache — neither of those is the platter."""
    seen = []
    real = storage.append_line
    monkeypatch.setattr(journal.storage, "append_line",
                        lambda path, data: (seen.append((Path(path), data)), real(path, data))[1])
    p = tmp_path / "e.jsonl"
    journal.Journal(p).record("x", operation_id="o")
    assert [q for q, _ in seen] == [p]
    assert b"\n" not in seen[0][1], "the terminator is append_line's to add, in one write"


def test_intent_and_done_are_the_only_spelling_of_the_pair():
    assert journal.intent("council_round") == "council_round_start"
    assert journal.done("council_round") == "council_round_done"


# ---- concurrent writers ----------------------------------------------------------------
def test_threads_appending_at_once_leave_a_journal_that_still_reads(tmp_path):
    """THE EXTERNAL QUESTION: after N writers race, can the file still be read as a sequence
    of facts? Not "was a lock taken".

    REPRODUCED BEFORE THE LOCK, and it is why the fleet could not be parallelised: each writer
    derived `seq` from the file and counted on, so two that derived 5 both wrote 5 and the
    second landed at line 6. `_parse` then refused the WHOLE file — every fact in the run,
    including the ones written correctly before the race.

    THREADS AND NOT ONLY PROCESSES, because `flock` is held by the open file DESCRIPTION: a
    cached descriptor shared between threads would have both "holding" one lock and serializing
    nothing, and the fleet's builders are threads.
    """
    import threading
    log = journal.Journal(storage.journal_path(tmp_path))
    writers, per = 8, 12
    errors = []

    def _spam(n):
        try:
            w = journal.Journal(storage.journal_path(tmp_path))
            for i in range(per):
                w.record(journal.intent("seat"), operation_id=f"w{n}-{i}", seat="claude")
        except Exception as e:            # noqa: BLE001 — reported, not swallowed
            errors.append(e)

    threads = [threading.Thread(target=_spam, args=(n,)) for n in range(writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [], errors
    events = log.read()
    assert len(events) == writers * per
    assert [e.seq for e in events] == list(range(1, writers * per + 1))


def test_processes_appending_at_once_leave_a_journal_that_still_reads(tmp_path):
    """The other half, and it is a DIFFERENT mechanism rather than the same test twice: a
    thread shares the process's memory (so a cached counter is shared) while a fork does not,
    and only an OS-level lock covers both. §14.1's record has to survive either."""
    import subprocess
    import textwrap
    prog = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(ROOT / "shared" / "lib")!r})
        from forge import journal, storage
        w = journal.Journal(storage.journal_path({str(tmp_path)!r}))
        for i in range(12):
            w.record(journal.intent("seat"), operation_id=sys.argv[1] + str(i), seat="codex")
    """)
    procs = [subprocess.Popen([sys.executable, "-c", prog, f"p{n}-"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
             for n in range(6)]
    for p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0, err.decode()
    events = journal.Journal(storage.journal_path(tmp_path)).read()
    assert len(events) == 6 * 12
    assert [e.seq for e in events] == list(range(1, 6 * 12 + 1))


def test_a_writer_sees_another_writers_records_rather_than_its_cached_counter(tmp_path):
    """THE STALENESS THIS MUST NOT HAVE. Two `Journal` objects on one path: the first appends,
    caches seq=1, the second appends, then the first appends again. A counter carried across
    the lock would hand back 2 for a file whose last record is already 2."""
    a = journal.Journal(storage.journal_path(tmp_path))
    b = journal.Journal(storage.journal_path(tmp_path))
    assert a.record(journal.intent("seat"), operation_id="a1").seq == 1
    assert b.record(journal.intent("seat"), operation_id="b1").seq == 2
    assert a.record(journal.intent("seat"), operation_id="a2").seq == 3
    assert [e.seq for e in a.read()] == [1, 2, 3]
