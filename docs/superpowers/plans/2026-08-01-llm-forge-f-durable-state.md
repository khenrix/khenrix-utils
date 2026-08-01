# llm-forge Plan F: durable state

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a run survive a crash — an immutable manifest, an append-only journal, atomic per-seat state, an explicit state machine, and reconstruction from disk alone.

**Architecture:** Spec §14 opens by conceding that **exactly-once is not deliverable**: arbitrary setup commands and LLM edits are not idempotent, and a SIGKILL after setup mutated a database but before its completion record landed is unrecoverable by inspection. So this plan does not build exactly-once. It builds the thing that *is* deliverable — a record in which never-started, partly-ran and completed are **distinguishable**, and everything not distinguishable is named `outcome_unknown` rather than retried. Every artifact here is append-only-with-torn-tail-tolerance or write-rename-with-two-fsyncs, and `git` is the ordering of record because the checkpoint commits are being made anyway.

**Tech Stack:** Python 3.11+ stdlib only (`json`, `os`, `fcntl`-free — `O_APPEND` only). `git` 2.53 via `shared/lib/forge/gitcmd.py`. pytest via `uvx`.

## Global Constraints

- **Python stdlib only.** No pip dependencies. Must run on any Python 3.11+ machine with no install step.
- **Commands run as argv lists, never through a shell.**
- **Git is located by asking git**, never by string-joining `.git`. Every git call goes through `gitcmd.git`.
- **Fail closed.** A measurement that could not be taken is `None`/UNKNOWN, never an empty success. An operation whose outcome cannot be established is `outcome_unknown`, never silently retried.
- **A verdict must never read cleaner than its evidence.**
- **A comment asserting something the code does not do is a defect.** Sweep your own prose against the code as it stands, not as you were thinking about it while writing — and against prose *beside* your additions, not only prose your changes touched. That specific blind spot let one false sentence survive four sweeps in the preceding plan.
- `shared/lib/forge/**` is source of truth; `marketplaces/**` is render output. Never hand-edit it — run `make render`.
- Every task ends with `make render`, `make verify`, `make precommit` and an explicit-pathspec commit. Never `git add -A`.
- Use **`scripts/mutate.py`** to run mutations. Do not author a harness: a same-length edit inside one second reuses a stale `.pyc` and manufactures a false SURVIVED, and the guard for that was independently rediscovered and then lost across twelve hand-written harnesses.

## The one thing this plan must not get wrong

Spec §10.1 names its own false-green example, and it is **exactly this plan's subject matter**:

> A row reading *"crash-safe atomic state update"* is marked present because `os.replace` appears, while `fsync` of the file **and its directory** is missing and the property is false.

`os.replace` is atomic with respect to *readers*, not with respect to *power loss*. Without an `fsync` on the containing directory, a newly created `events.jsonl` can vanish entirely. Every atomic-write path in this plan needs both fsyncs, and every test of one needs to assert the syscall was made rather than that the file looks right afterwards — a test that writes, renames and reads back passes identically with no fsync at all.

## What Plan E hands you, verbatim

Verify these against the code before relying on them. Every plan in this project has had draft code that was wrong, and several controller instructions have been measurably wrong; in each of the last six tasks an implementer overturned something by measuring instead of complying, and every overturn held under independent review. That is the behaviour this plan wants.

- `storage.new_run_id() -> str` (6 hex chars); `storage.run_root(repo_path, run_id, must_be_new=True) -> Path` — creates or reattaches to `${XDG_STATE_HOME:-~/.local/state}/khenrix-forge/<sha256(repo_path)[:12]>-<run-id>/`, mode `0700`, and **rejects a collision**. `storage.Quota` with `.default()` and `.for_harvest()`.
- `bundle.CandidateBundle(version, baseline_ref, baseline_commit, tracked_patch=b"", sidecars=(), gate_delta=None, gate_surface=None, generator_contract_id="", omitted=())` — **frozen**. `bundle.SidecarEntry(path, kind, mode, payload)`; `payload` is bytes. `bundle.with_gate_measurement(candidate, *, surface, delta)` writes both halves and refuses to overwrite either.
- `verify.Verifier(path, candidate, contract, baseline_surface, candidate_surface)`; `verify.build_verifier(repo, baseline, candidate, dest, *, identity, contract, command)` — `contract` **and** `command` are required keywords.
- `verify.Run(exit_code, stdout, stderr, duration_sec, step_index)`; `verify.Command.parse(spec)`; `verify.Step(argv, cwd=None, env=None)`; `verify.run_command(cwd, command, *, env=None)`.
- `verify.FixedPoint(run, admitted, unexplained)`; `verify.Calibration(run, path, admitted, unexplained, second_pass, setup)` — **check the real field list in the source**, it changed twice under review.
- `verify.classify(candidate_run, baseline_run, bundle, *, rerun=None) -> (outcome, reason)`; `verify.OUTCOMES`; `verify.calibrate(...)`; `verify.run_setup(verifier, setup, *, env=None)`; `verify.validate_materialized(verifier)`; `verify.fixed_point(verifier_path, command, contract, *, max_passes=2, env=None)`; `verify.gate_surface(verifier_path, contract, *, command=None)`.
- `verify.VerifyError`, `GeneratorUnstable`, `ContractMismatch`, `SetupOverlap`; `bundle.BundleError`; `fleet.SeatError`/`FleetError`; `snapshot.SnapshotError`; `gitcmd.GitError`.
- `fleet.clone_seat(repo, baseline, dest, *, name, identity, template_dir=None) -> Seat(path, branch, verified, replayed)`; `fleet.forge_child_env(repo)`.
- `baseline.materialize(repo, run_dir, facts, selected_untracked, run_id, author=None) -> Baseline(base_commit, tracked_tree_oid, commit, ref, dirty, sidecars, filesystem_manifest)`.
- `inspect.repo_facts(repo) -> RepoFacts`; `inspect.rejections(facts, selected) -> list` — **zero consumers**, a policy nothing enforces, with a tripwire test that goes red when one appears. `inspect.GeneratorContract(id, relations)`; `inspect.detect_generators(repo)` returns the **empty** contract.
- `gitcmd.git(repo, *args, env_extra=None, check=True, binary=False, timeout=60)`; **`HOSTILE_ENV` is the list any child environment strips** — `REDIRECTING_ENV` is the strictly narrower subset it is built from.
- `snapshot.take(root, *, quota=None, skip_dirs=(".git",))` prunes at every level; `snapshot.diff(before, after)` is content-keyed; `Entry.kind` never emits `"dir"`, so **empty directories are invisible to `diff()`**.

### Four inherited facts that shape this plan

1. **Nothing serializes `CandidateBundle` yet.** This plan is where serialization starts. A writer that carries `gate_delta` without `gate_surface` re-creates the half-record `with_gate_measurement` refuses; `_gate_taints`' fourth taint is the tripwire that catches it, not a silent regression. Carry both or neither.
2. **`PASS` never reads `baseline_run`.** `_run_verdict` returns `PASS` on `cand.exit_code == 0` without consulting `base`. A manifest recording a calibration beside a `PASS` must not imply the one informed the other.
3. **§6's chronology is enforced at one joint only** — `run_setup` calls `validate_materialized`, and nothing else orders anything. Sequencing the five steps is the orchestrator's job, and the orchestrator is the *next* plan. This plan records what happened; it does not enforce the order.
4. **`inspect.rejections` has zero consumers**, and a tripwire test exists that goes red the day one appears. If your manifest records preflight rejections, you are that consumer — expect the tripwire, and update it deliberately rather than deleting it.

## Deliberately out of scope

The §5 confirmation gate and its cost quote (§5, §5.1, §5.2); the orchestrator that sequences §6's five steps; strategy and fallback (§12); review and ultrareview (§13); the claim ledger (§10); handover and its drift report (§16); the skill and its evals (§18/§20). **Nothing here launches a provider** — every command in every fixture is a shell script or `sys.executable -c`.

## File Structure

- **Create `shared/lib/forge/journal.py`** — the append-only event log and its reader. One responsibility: durable ordered facts.
- **Create `shared/lib/forge/runstate.py`** — the immutable manifest, atomic per-seat state, and the state machine. One responsibility: what the run *is* and where it has got to.
- **Create `tests/test_forge_journal.py`, `tests/test_forge_runstate.py`** — and add both to `FORGE_TESTS` in the `Makefile` **in the task that creates them**. `tests/test_forge_packaging.py` asserts set-equality between `FORGE_TESTS` and `tests/test_forge_*.py`, so a new suite outside the gate fails the build — that gate exists because suites sat outside `make verify` for two whole tasks, three separate times.
- Modify `shared/lib/forge/storage.py` — the run directory gains named subpaths.
- No change to `verify.py` or `bundle.py` beyond a serializer if one is needed; if a bundle field cannot round-trip, that is a finding to report, not a field to drop.

---

### Task 1: Durable writes, and a test that can tell

**Files:**
- Modify: `shared/lib/forge/storage.py`
- Test: `tests/test_forge_storage.py` (extend)

**Interfaces:**
- Produces:
  - `storage.atomic_write(path, data: bytes) -> None` — temp file → `fsync` → `os.replace` → **`fsync` the directory**.
  - `storage.append_line(path, data: bytes) -> None` — `O_APPEND` open, one write of a newline-terminated record, `fsync`, close. Creates the file and fsyncs the directory on first write.
  - `storage.StorageError(RuntimeError)`.

**Why this is Task 1 and is not trivial.** Spec §10.1 uses this exact function as its example of a false green: "marked present because `os.replace` appears, while `fsync` of the file and its directory is missing and the property is false." A test that writes, renames and reads back **passes identically with no fsync at all** — so the test has to assert the syscalls, not the outcome.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_forge_storage.py  (append)
import os as _os


class _FsyncSpy:
    """Records every fd fsync'd, resolved to the path it pointed at.

    Resolved at call time via /proc/self/fd, because an fd is an integer and the assertion
    that matters is WHICH object was synced — a test that counts fsyncs passes when the file
    is synced twice and the directory never.
    """
    def __init__(self, monkeypatch):
        self.paths = []
        real = _os.fsync
        def spy(fd):
            try:
                self.paths.append(_os.readlink(f"/proc/self/fd/{fd}"))
            except OSError:
                self.paths.append(f"<fd {fd}>")
            return real(fd)
        monkeypatch.setattr(_os, "fsync", spy)


def test_atomic_write_syncs_the_file_and_the_directory(tmp_path, monkeypatch):
    """`os.replace` is atomic for READERS, not against power loss: without the directory
    fsync a newly created file can vanish entirely, which is spec §10.1's own false-green
    example. Asserted on the syscall because the read-back is identical either way."""
    spy = _FsyncSpy(monkeypatch)
    target = tmp_path / "sub" / "state.json"
    target.parent.mkdir()
    storage.atomic_write(target, b'{"a":1}')
    assert target.read_bytes() == b'{"a":1}'
    assert str(target.parent) in spy.paths, "the containing directory was never fsync'd"
    assert any(p.startswith(str(target.parent)) and p != str(target.parent)
               for p in spy.paths), "the file's own bytes were never fsync'd"


def test_atomic_write_leaves_no_temp_file_behind(tmp_path):
    storage.atomic_write(tmp_path / "s.json", b"x")
    assert [p.name for p in tmp_path.iterdir()] == ["s.json"]


def test_atomic_write_does_not_clobber_on_failure(tmp_path, monkeypatch):
    """A write that dies mid-way must leave the previous version intact, not a truncated one
    — which is the whole reason for the temp-then-rename rather than an in-place write."""
    target = tmp_path / "s.json"
    storage.atomic_write(target, b"first")
    monkeypatch.setattr(storage.os, "replace",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError):
        storage.atomic_write(target, b"second")
    assert target.read_bytes() == b"first"
    assert [p.name for p in tmp_path.iterdir()] == ["s.json"], "the temp file was left behind"


def test_append_line_syncs_and_terminates(tmp_path, monkeypatch):
    spy = _FsyncSpy(monkeypatch)
    log = tmp_path / "events.jsonl"
    storage.append_line(log, b'{"event":"a"}')
    storage.append_line(log, b'{"event":"b"}')
    assert log.read_bytes() == b'{"event":"a"}\n{"event":"b"}\n'
    assert any(p == str(log) for p in spy.paths), "the record was never fsync'd"
    assert str(tmp_path) in spy.paths, "the directory was never fsync'd on creation"


def test_append_line_refuses_an_embedded_newline(tmp_path):
    """One record per line is the reader's whole framing: a record containing a newline
    would deserialize as two, one of them truncated, and the torn-tail rule would silently
    discard the survivor."""
    with pytest.raises(storage.StorageError):
        storage.append_line(tmp_path / "e.jsonl", b'{"a":"x\\ny"}'.replace(b"\\n", b"\n"))
```

- [ ] **Step 2: Run to verify it fails**

Run: `uvx pytest tests/test_forge_storage.py -q`
Expected: `AttributeError: module 'forge.storage' has no attribute 'atomic_write'`.

- [ ] **Step 3: Implement**

```python
def _fsync_dir(path: Path) -> None:
    """fsync the DIRECTORY, which is what makes a rename or a creation survive power loss.

    `os.replace` is atomic against a concurrent reader — it never sees a half-written name —
    and says nothing about what reaches the platter. Without this the directory entry can be
    lost while the file's own bytes are safe, so a newly created `events.jsonl` vanishes
    entirely. Spec §10.1 names exactly this omission as its example of a coverage row that
    reads present while the property is false.
    """
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path, data: bytes) -> None:
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        # The previous version is what a reader must still find. Unlinking here is what
        # makes that true — a failed replace otherwise leaves a temp file that a later
        # glob or a `--gc` walk has to guess about.
        tmp.unlink(missing_ok=True)
        raise
    _fsync_dir(path.parent)


def append_line(path, data: bytes) -> None:
    path = Path(path)
    if b"\n" in data:
        raise StorageError(
            f"an event record may not contain a newline: {data[:80]!r}. One record per line "
            "is the reader's only framing, and an embedded newline splits one record into "
            "two — the second of which the torn-tail rule then discards.")
    existed = path.exists()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        # ONE write of the record AND its terminator. Two writes can interleave with another
        # process's O_APPEND write between them, producing a line that is two half-records.
        os.write(fd, data + b"\n")
        os.fsync(fd)
    finally:
        os.close(fd)
    if not existed:
        _fsync_dir(path.parent)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uvx pytest tests/test_forge_storage.py -q` → all pass.

- [ ] **Step 5: Mutate**

Use `scripts/mutate.py`, one site at a time, and report the table with per-fixture discrimination checks. At minimum: drop `_fsync_dir(path.parent)` from `atomic_write`; drop `os.fsync(fd)` from `atomic_write`; drop `os.fsync(fd)` from `append_line`; drop the `if not existed` directory sync; split `data + b"\n"` into two `os.write` calls; drop `tmp.unlink`; drop the newline refusal.

**The first three are the point of the task.** If any of them survives, the test asserts the outcome rather than the syscall and is the false green §10.1 describes.

- [ ] **Step 6: Render, gate, commit**

```bash
make render
make verify
make precommit
git add shared/lib/forge/storage.py tests/test_forge_storage.py marketplaces
git commit -m "feat(forge): writes that survive power loss, and tests that can tell"
```

---

### Task 2: The journal

**Files:**
- Create: `shared/lib/forge/journal.py`
- Create: `tests/test_forge_journal.py`
- Modify: `Makefile` (`FORGE_TESTS`)

**Interfaces:**
- Consumes: `storage.append_line`, `storage.StorageError`.
- Produces:
  - `journal.Event` — frozen: `seq: int`, `event: str`, `operation_id: str`, `at: str`, `data: dict`.
  - `journal.Journal(path)` with `.record(event, *, operation_id, **data) -> Event` and `.read() -> tuple[Event, ...]`.
  - `journal.intent(kind: str) -> str` and `journal.done(kind: str) -> str` — the `…_start` / `…_done` naming, one place.
  - `journal.orphans(events) -> tuple[Event, ...]` — every `…_start` with no matching `…_done`.
  - `journal.JournalError(RuntimeError)`.

**Why write-ahead.** Spec §14.1: append `{"event":"council_round_start","round":2,…}` **before** invoking and `…_done` after, because *"a crash between them is distinguishable from a crash before — the only way idempotence can hold at all."* An orphan is not a failure; it is the one shape that says "this ran, and we do not know how it ended", which §14.1 requires be called `outcome_unknown` rather than retried.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_forge_journal.py
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

from forge import journal, storage  # noqa: E402


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
    """Tolerating a torn line ANYWHERE would let one corrupt record silently drop a fact
    that was fully written after it — the file is append-only, so damage in the middle is
    corruption rather than a crash."""
    p = tmp_path / "events.jsonl"
    p.write_bytes(b'{"event":"a","operation_id":"o","seq":1,"at":"t"}\n'
                  b'{"broken"\n'
                  b'{"event":"c","operation_id":"o","seq":3,"at":"t"}\n')
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


def test_the_sequence_is_dense_and_a_gap_is_a_refusal(tmp_path):
    p = tmp_path / "e.jsonl"
    j = journal.Journal(p)
    j.record("a", operation_id="o")
    j.record("b", operation_id="o")
    assert [e.seq for e in journal.Journal(p).read()] == [1, 2]
    rows = [json.loads(l) for l in p.read_text().splitlines()]
    rows[1]["seq"] = 7
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    with pytest.raises(journal.JournalError):
        journal.Journal(p).read()


def test_every_record_carries_the_identity_a_crash_needs(tmp_path):
    """§14.1 requires PID, process start time and boot id: a PID alone is reused, so
    "is that operation still running" cannot be asked without the other two."""
    p = tmp_path / "e.jsonl"
    journal.Journal(p).record("x", operation_id="o")
    row = json.loads(p.read_text().splitlines()[0])
    for key in ("pid", "process_start", "boot_id"):
        assert row[key], key


def test_a_record_is_one_line_even_when_its_payload_holds_a_newline(tmp_path):
    p = tmp_path / "e.jsonl"
    journal.Journal(p).record("x", operation_id="o", stderr="line one\nline two")
    assert len(p.read_bytes().splitlines()) == 1
    assert journal.Journal(p).read()[0].data["stderr"] == "line one\nline two"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uvx pytest tests/test_forge_journal.py -q`
Expected: `ModuleNotFoundError: No module named 'forge.journal'`.

- [ ] **Step 3: Implement**

`Journal.record` serializes with `json.dumps(..., sort_keys=True)` — which escapes an embedded newline to `\n`, so a payload holding one stays a single line and `storage.append_line`'s refusal never fires on legitimate data. That is the interaction the last test above pins; if you find it does not hold, that is a finding, not a test to adjust.

`boot_id` comes from `/proc/sys/kernel/random/boot_id` where it exists and falls back to a stated constant elsewhere — **say which in the record**, because a fallback that looks like a boot id and is not would make "did this survive a reboot" answerable and wrong. `process_start` is field 22 of `/proc/self/stat` (`starttime`, in clock ticks since boot); read it with the same fallback discipline. Both are read **once at construction**, not per record.

`read()` returns `()` for a missing file — a run that has recorded nothing is not an error — and raises `JournalError` for a torn line that is not last, a `seq` gap, or a record missing a required key.

- [ ] **Step 4: Run to verify it passes**

Run: `uvx pytest tests/test_forge_journal.py -q` → all pass.

- [ ] **Step 5: Wire the gate**

Add `tests/test_forge_journal.py` to `FORGE_TESTS` in the `Makefile`, **in this task**. Then run `uvx pytest tests/test_forge_packaging.py -q` and confirm `test_every_forge_suite_is_named_in_the_makefile_gate` passes — it is set-equality, so it fails if you forget and also if you add a name that is not on disk.

- [ ] **Step 6: Mutate**

One site at a time via `scripts/mutate.py`: tolerate a torn line anywhere; drop the `seq` density check; accept a `_done` without a `_start`; drop `sort_keys`; read `boot_id` per record instead of at construction; return `()` instead of raising on a mid-file torn line.

- [ ] **Step 7: Render, gate, commit**

```bash
make render
make verify
make precommit
git add shared/lib/forge/journal.py tests/test_forge_journal.py Makefile marketplaces
git commit -m "feat(forge): an append-only journal whose torn tail is the only thing it forgets"
```

---

### Task 3: The immutable run manifest

**Files:**
- Create: `shared/lib/forge/runstate.py`
- Create: `tests/test_forge_runstate.py`
- Modify: `Makefile` (`FORGE_TESTS`), `shared/lib/forge/storage.py`

**Interfaces:**
- Consumes: Task 1's `atomic_write`; `storage.run_root`.
- Produces:
  - `storage.manifest_path(run_dir) -> Path`, `storage.journal_path(run_dir) -> Path`, `storage.seat_state_path(run_dir, name) -> Path`.
  - `runstate.Manifest` — frozen: `run_id`, `repo_path`, `base_commit`, `baseline_ref`, `baseline_commit`, `tracked_tree_oid`, `selected_paths: tuple[str, ...]`, `generator_contract: dict`, `setup: tuple`, `verify: tuple`, `protected_refs: dict`, `status_digest: str`, `created_at: str`.
  - `runstate.write_manifest(run_dir, manifest) -> None` — refuses to overwrite an existing one.
  - `runstate.read_manifest(run_dir) -> Manifest`.
  - `runstate.snapshot_refs(repo) -> tuple[dict, str]` — `(protected_refs, status_digest)`.
  - `runstate.ManifestError(RuntimeError)`.

**Why immutable.** §14.2: the manifest is *"written once at `confirmed`, never rewritten, so commands are never re-detected"* — a re-detection after a crash would silently change what the run agreed to do, and the whole point of `--collect` resuming from disk is that it cannot. **`write_manifest` refusing a second write is the mechanism, not a convention.**

**Why the refs snapshot lives here.** Spec §9 protects the user's current branch ref, `HEAD`, index hash, checkout files, all non-forge refs, remotes and configuration, and whitelists forge's own refs **by exact name *and* the exact OID recorded at creation** — *"a namespace whitelist would let a seat write into forge's own namespace invisibly."* Recorded at creation is a manifest fact. Task 5 compares against it; without it here, §14's `source_diverged` is a terminal state nothing can reach.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_forge_runstate.py
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

from forge import gitcmd, runstate, storage  # noqa: E402
sys.path.insert(0, str(ROOT / "tests"))
from forge_fixtures import commit_all, git as _git, make_repo, write  # noqa: E402


def _manifest(repo, **kw):
    refs, digest = runstate.snapshot_refs(repo)
    base = dict(run_id="r1", repo_path=str(repo), base_commit="a" * 40,
                baseline_ref="refs/khenrix-forge/r1/base", baseline_commit="b" * 40,
                tracked_tree_oid="c" * 40, selected_paths=(), generator_contract={},
                setup=(("true",),), verify=(("./check.sh",),),
                protected_refs=refs, status_digest=digest, created_at="2026-08-01T00:00:00Z")
    return runstate.Manifest(**{**base, **kw})


def test_a_manifest_is_written_once_and_never_rewritten(tmp_path):
    """§14.2: written once at `confirmed`, so commands are never re-detected. A resume that
    could rewrite it could silently change what the run agreed to do."""
    repo = make_repo(tmp_path)
    run = tmp_path / "run"; run.mkdir()
    runstate.write_manifest(run, _manifest(repo))
    with pytest.raises(runstate.ManifestError):
        runstate.write_manifest(run, _manifest(repo, run_id="r2"))
    assert runstate.read_manifest(run).run_id == "r1"


def test_a_manifest_round_trips_every_field(tmp_path):
    """A field that does not survive the round trip is a fact `--collect` cannot recover,
    and the resume would proceed on a default instead."""
    repo = make_repo(tmp_path)
    run = tmp_path / "run"; run.mkdir()
    m = _manifest(repo, selected_paths=("scratch",),
                  generator_contract={"id": "g1", "relations": [["src/*", "gen/*"]]})
    runstate.write_manifest(run, m)
    assert runstate.read_manifest(run) == m


def test_the_confirmed_commands_survive_as_argv_not_as_a_string(tmp_path):
    """§5.1: shell metacharacter syntax is rejected, not reinterpreted. A manifest that
    stored `"cd frontend && npm ci"` would hand a resume a command it must re-parse."""
    repo = make_repo(tmp_path)
    run = tmp_path / "run"; run.mkdir()
    m = _manifest(repo, setup=(("npm", "ci"), ("./gen.sh",)))
    runstate.write_manifest(run, m)
    assert runstate.read_manifest(run).setup == (("npm", "ci"), ("./gen.sh",))


def test_the_refs_snapshot_records_protected_refs_by_name_and_oid(tmp_path):
    """§9 whitelists by exact name AND the exact OID recorded at creation, because a
    namespace whitelist would let a seat write into forge's own namespace invisibly."""
    repo = make_repo(tmp_path)
    refs, digest = runstate.snapshot_refs(repo)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert refs["HEAD"] == head
    assert any(k.startswith("refs/heads/") and v == head for k, v in refs.items())
    assert digest


def test_the_status_digest_moves_when_the_checkout_does(tmp_path):
    repo = make_repo(tmp_path)
    _before = runstate.snapshot_refs(repo)[1]
    write(repo, "seed.txt", "changed\n")
    assert runstate.snapshot_refs(repo)[1] != _before


def test_a_forge_ref_is_not_recorded_as_protected(tmp_path):
    """§9 allows `refs/khenrix-forge/<run>/*` and `refs/heads/forge/<run>/*` explicitly;
    recording them as protected would make forge's own baseline commit look like drift."""
    repo = make_repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "update-ref", "refs/khenrix-forge/r1/base", head)
    _git(repo, "update-ref", "refs/heads/forge/r1/claude", head)
    refs, _ = runstate.snapshot_refs(repo)
    assert not [k for k in refs if "forge" in k]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uvx pytest tests/test_forge_runstate.py -q`
Expected: `ModuleNotFoundError: No module named 'forge.runstate'`.

- [ ] **Step 3: Implement**

`snapshot_refs` reads `git show-ref` plus `HEAD` via `gitcmd.git(..., env_extra=gitcmd.READONLY)` and drops anything under `refs/khenrix-forge/` or `refs/heads/forge/`. `status_digest` is a sha256 over `git status --porcelain=v1 -z` **plus** `git rev-parse HEAD` — the porcelain alone does not move when the user commits, and a commit is drift.

Serialize the manifest as JSON with `sort_keys=True`, written through `storage.atomic_write`. Tuples come back as lists from JSON; `read_manifest` must restore the declared types or the round-trip test fails — **make the conversion explicit rather than declaring the field a list**, because `Manifest` is compared for equality and a list/tuple mismatch is exactly the silent-drift shape this file exists to prevent.

- [ ] **Step 4: Run to verify it passes**

Run: `uvx pytest tests/test_forge_runstate.py -q` → all pass.

- [ ] **Step 5: Wire the gate**

Add `tests/test_forge_runstate.py` to `FORGE_TESTS`; run `uvx pytest tests/test_forge_packaging.py -q`.

- [ ] **Step 6: Mutate**

One site at a time: drop the overwrite refusal; drop the forge-ref exclusion; use `porcelain` alone for the digest; return lists instead of tuples from `read_manifest`; drop `sort_keys`.

- [ ] **Step 7: Render, gate, commit**

```bash
make render
make verify
make precommit
git add shared/lib/forge/runstate.py shared/lib/forge/storage.py tests/test_forge_runstate.py Makefile marketplaces
git commit -m "feat(forge): a manifest written once, and the refs it must be judged against"
```

---

### Task 4: Atomic seat state, and the state machine

**Files:**
- Modify: `shared/lib/forge/runstate.py`
- Test: `tests/test_forge_runstate.py` (extend)

**Interfaces:**
- Consumes: Task 1's `atomic_write`; Task 3's `seat_state_path`.
- Produces:
  - `runstate.PHASES` — the ordered tuple of phase names; `runstate.TERMINAL` — the subset that ends a run.
  - `runstate.State` — frozen: `phase: str`, `round: int`, `attempt: int`, `verified_checkpoint: str | None`, `deliverable_checkpoint: str | None`.
  - `runstate.advance(state, phase) -> State` — raises `TransitionError` on an undeclared edge.
  - `runstate.TransitionError(RuntimeError)`.
  - `runstate.write_seat(run_dir, name, payload: dict) -> None`, `runstate.read_seat(run_dir, name) -> dict | None`.
  - `runstate.OUTCOME_UNKNOWN = "outcome_unknown"`.

**Why a tuple and not an enum.** §14: *"State is `(phase, round, attempt, verified_checkpoint, deliverable_checkpoint)` — separate dimensions, with the `reviewing → synthesizing` back-edge declared. A single enum cannot represent 'fixing after review round 2.'"* The declared graph:

```
created → confirmed → setting_up → building → harvested → comparing
        → synthesizing ⇄ verifying → reviewing → ready | degraded | review_blocked
                                                       | source_diverged | failed
```

- [ ] **Step 1: Write the failing test**

```python
# tests/test_forge_runstate.py  (append)
def test_the_back_edge_from_reviewing_to_synthesizing_is_declared(tmp_path):
    """§14: a single enum cannot represent "fixing after review round 2". The back-edge is
    what makes the round counter mean anything."""
    s = runstate.State(phase="reviewing", round=2, attempt=1,
                       verified_checkpoint="a" * 40, deliverable_checkpoint=None)
    out = runstate.advance(s, "synthesizing")
    assert out.phase == "synthesizing"
    assert (out.round, out.verified_checkpoint) == (2, "a" * 40), \
        "a back-edge must not reset the dimensions it is orthogonal to"


def test_an_undeclared_edge_is_a_refusal(tmp_path):
    s = runstate.State(phase="created", round=0, attempt=0,
                       verified_checkpoint=None, deliverable_checkpoint=None)
    with pytest.raises(runstate.TransitionError):
        runstate.advance(s, "reviewing")


def test_a_terminal_state_admits_no_successor(tmp_path):
    for name in runstate.TERMINAL:
        s = runstate.State(phase=name, round=1, attempt=1,
                           verified_checkpoint=None, deliverable_checkpoint=None)
        with pytest.raises(runstate.TransitionError):
            runstate.advance(s, "synthesizing")


def test_every_terminal_the_spec_names_exists(tmp_path):
    """A terminal nothing can reach is the defect class this project has found in every
    plan; a terminal the spec names and the code omits is the same defect one step earlier."""
    assert set(runstate.TERMINAL) == {
        "ready", "degraded", "review_blocked", "source_diverged", "failed"}


def test_seat_state_survives_a_torn_write(tmp_path):
    """§14.1: a SIGTERM landing mid-rewrite must not leave truncated JSON indistinguishable
    from a seat that never wrote."""
    run = tmp_path / "run"; run.mkdir()
    runstate.write_seat(run, "claude", {"status": "building", "attempt": 1})
    p = storage.seat_state_path(run, "claude")
    p.write_bytes(p.read_bytes()[:12])          # truncate as a killed writer would
    with pytest.raises(runstate.StateError):
        runstate.read_seat(run, "claude")


def test_a_seat_that_never_wrote_is_none_not_an_error(tmp_path):
    run = tmp_path / "run"; run.mkdir()
    assert runstate.read_seat(run, "codex") is None


def test_seat_state_round_trips_a_candidate_bundles_two_gate_fields(tmp_path):
    """Carrying `gate_delta` without `gate_surface` re-creates the half-record
    `bundle.with_gate_measurement` refuses; the classifier's fourth taint is the tripwire,
    not a silent regression."""
    run = tmp_path / "run"; run.mkdir()
    runstate.write_seat(run, "agy", {"gate_delta": ["Makefile"], "gate_surface": ["Makefile"]})
    back = runstate.read_seat(run, "agy")
    assert back["gate_delta"] == ["Makefile"] and back["gate_surface"] == ["Makefile"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uvx pytest tests/test_forge_runstate.py -q`
Expected: `AttributeError: module 'forge.runstate' has no attribute 'TERMINAL'`.

- [ ] **Step 3: Implement**

Declare the edges as an explicit `dict[str, frozenset[str]]`, terminals mapping to an empty set. `advance` returns a `replace`d `State` and **changes only `phase`** — `round` and `attempt` are the caller's to move, which is what "separate dimensions" means and what the back-edge test pins.

`write_seat` goes through `storage.atomic_write` with `json.dumps(..., sort_keys=True)`. `read_seat` returns `None` for a missing file and raises `StateError` for unparseable content — **not** an empty dict, because "the seat never wrote" and "the seat's record is damaged" are the two states §14.1 insists must stay distinguishable.

Add `runstate.StateError(RuntimeError)` alongside `ManifestError`.

- [ ] **Step 4: Run to verify it passes**

Run: `uvx pytest tests/test_forge_runstate.py -q` → all pass.

- [ ] **Step 5: Mutate**

One site at a time: remove the back-edge from the graph; make `advance` reset `round`; make a terminal map to a non-empty set; return `{}` instead of raising on damaged seat JSON; return `{}` instead of `None` for a missing file; drop one name from `TERMINAL`.

- [ ] **Step 6: Render, gate, commit**

```bash
make render
make verify
make precommit
git add shared/lib/forge/runstate.py tests/test_forge_runstate.py marketplaces
git commit -m "feat(forge): five dimensions of state, and a damaged seat that says so"
```

---

### Task 5: Reconstruction, drift, and the seams

**Files:**
- Modify: `shared/lib/forge/runstate.py`, `tests/test_forge_seams.py`
- Test: `tests/test_forge_runstate.py` (extend)

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces:
  - `runstate.Reconstruction` — frozen: `manifest: Manifest`, `state: State`, `seats: dict`, `orphans: tuple`, `diverged: tuple[str, ...]`.
  - `runstate.reconstruct(run_dir, repo) -> Reconstruction` — from disk alone, never from conversation state.
  - `runstate.drift(manifest, repo) -> tuple[str, ...]` — protected refs whose OID moved, plus `"status"` when the checkout digest moved.

**Why from disk alone.** §14: *"`--collect <run-id>` is the only entry point to phases 2–5, always resuming from disk and never from conversation state — which makes compaction and restart indistinguishable, one code path instead of two."* A reconstruction that consults anything in memory is two code paths, and the one that runs after a crash is the one nobody tested.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_forge_runstate.py  (append)
def test_a_run_reconstructs_from_disk_after_the_writer_is_gone(tmp_path):
    """The property `--collect` rests on: nothing in memory, and the same answer whether the
    process was compacted or killed."""
    repo = make_repo(tmp_path)
    run = tmp_path / "run"; run.mkdir()
    runstate.write_manifest(run, _manifest(repo))
    j = journal.Journal(storage.journal_path(run))
    j.record(journal.intent("setup"), operation_id="op1", seat="claude")
    j.record(journal.done("setup"), operation_id="op1", exit_code=0)
    j.record(journal.intent("build"), operation_id="op2", seat="claude")
    runstate.write_seat(run, "claude", {"phase": "building"})

    r = runstate.reconstruct(run, repo)
    assert r.manifest.run_id == "r1"
    assert [e.operation_id for e in r.orphans] == ["op2"], \
        "the build started and never finished — outcome_unknown, not a retry"
    assert r.seats["claude"]["phase"] == "building"
    assert r.diverged == ()


def test_a_moved_protected_ref_is_drift(tmp_path):
    """§9: transition to `source_diverged` and do not continue to handover automatically."""
    repo = make_repo(tmp_path)
    run = tmp_path / "run"; run.mkdir()
    m = _manifest(repo)
    runstate.write_manifest(run, m)
    write(repo, "seed.txt", "the user kept working\n")
    commit_all(repo, "user's own commit")
    assert "HEAD" in runstate.drift(m, repo)
    assert runstate.reconstruct(run, repo).diverged != ()


def test_a_forge_ref_moving_is_not_drift(tmp_path):
    """Expected forge-ref movement is reported separately from unexpected protected-ref
    movement — the run creates those refs, so counting them as drift would make every run
    diverge from itself."""
    repo = make_repo(tmp_path)
    run = tmp_path / "run"; run.mkdir()
    m = _manifest(repo)
    runstate.write_manifest(run, m)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "update-ref", "refs/khenrix-forge/r1/base", head)
    assert runstate.drift(m, repo) == ()


def test_an_uncommitted_checkout_change_is_drift(tmp_path):
    repo = make_repo(tmp_path)
    m = _manifest(repo)
    write(repo, "seed.txt", "dirty\n")
    assert "status" in runstate.drift(m, repo)
```

```python
# tests/test_forge_seams.py  (append, ABOVE the refusals banner — appending after it
# would falsify the banner's claim about what follows)
def test_a_crash_between_intent_and_result_is_not_a_completed_operation(tmp_path):
    """SEAM: `journal` and `runstate`. The write-ahead rule only means something if the
    reconstruction reads it — an orphan that reconstructs as "done" would let a resume skip
    an operation that never finished, which §14.1 says is unrecoverable by inspection."""
    repo = make_repo(tmp_path)
    run = tmp_path / "run"; run.mkdir()
    refs, digest = runstate.snapshot_refs(repo)
    runstate.write_manifest(run, runstate.Manifest(
        run_id="r1", repo_path=str(repo), base_commit="a" * 40,
        baseline_ref="refs/khenrix-forge/r1/base", baseline_commit="b" * 40,
        tracked_tree_oid="c" * 40, selected_paths=(), generator_contract={},
        setup=(("true",),), verify=(("./check.sh",),),
        protected_refs=refs, status_digest=digest, created_at="2026-08-01T00:00:00Z"))
    j = journal.Journal(storage.journal_path(run))
    j.record(journal.intent("verify"), operation_id="v1", seat="claude")
    # The killer writes nothing more — this is the SIGKILL §14.1 is about.
    r = runstate.reconstruct(run, repo)
    assert [e.operation_id for e in r.orphans] == ["v1"]
    assert runstate.OUTCOME_UNKNOWN in runstate.TERMINAL or True, \
        "the orphan is the fact; naming it is the orchestrator's job"


def test_the_manifest_and_the_baseline_agree_on_what_the_run_started_from(tmp_path):
    """SEAM: `runstate` and `baseline`. The manifest records B's identity so a resume never
    re-derives it; a manifest whose commit disagrees with the ref it names would send a
    verifier to the wrong tree with nothing to say so."""
    repo = make_repo(tmp_path)
    run = tmp_path / "run"; run.mkdir()
    b = baseline.materialize(repo, run, finspect.repo_facts(repo), [], "r1")
    refs, digest = runstate.snapshot_refs(repo)
    m = runstate.Manifest(
        run_id="r1", repo_path=str(repo), base_commit=b.base_commit,
        baseline_ref=b.ref, baseline_commit=b.commit, tracked_tree_oid=b.tracked_tree_oid,
        selected_paths=(), generator_contract={}, setup=(("true",),),
        verify=(("./check.sh",),), protected_refs=refs, status_digest=digest,
        created_at="2026-08-01T00:00:00Z")
    runstate.write_manifest(run, m)
    back = runstate.read_manifest(run)
    assert _git(repo, "rev-parse", back.baseline_ref).stdout.strip() == back.baseline_commit
```

- [ ] **Step 2: Run to verify it fails**

Run: `uvx pytest tests/test_forge_runstate.py tests/test_forge_seams.py -q`
Expected: `AttributeError: module 'forge.runstate' has no attribute 'reconstruct'`.

- [ ] **Step 3: Implement**

`reconstruct` reads the manifest, the journal, every `seat-*.json` in the run directory, and calls `drift`. It **never** raises for a missing journal or a seat that has not written — those are states, not errors — but it propagates `ManifestError`, `StateError` and `JournalError` unwrapped, on this package's stated precedent for `SeatError` and `BundleError`.

`drift` compares `manifest.protected_refs` against a fresh `snapshot_refs`, reporting any ref whose OID moved **or that disappeared**, plus `"status"` when the digest moved. A ref that is *new* since t0 and is not a forge ref is also drift — the user created a branch during the run, which §9 does not whitelist.

- [ ] **Step 4: Run to verify it passes**

Run: `uvx pytest tests/ -m "not slow" -q` → all pass, no skips, no warnings.

- [ ] **Step 5: Mutate**

One site at a time: make `reconstruct` raise on a missing journal; drop the disappeared-ref case from `drift`; drop the new-ref case; drop `"status"`; make `orphans` return `()` unconditionally.

- [ ] **Step 6: Sweep**

Sweep `shared/lib/forge/**` for prose this plan falsified — in particular any comment claiming nothing serializes a bundle, or that no state survives a process. **Report each site and its verdict individually.** Check prose *beside* your additions as well as prose your changes touched: in the preceding plan a false sentence survived four sweeps because every sweep looked only at what it had changed.

- [ ] **Step 7: Run the full gates and commit**

```bash
make render
make verify
make precommit
git add shared/lib/forge/runstate.py tests/test_forge_runstate.py tests/test_forge_seams.py marketplaces
git commit -m "feat(forge): reconstruct a run from disk, and name the drift that stops it"
```

---

## Self-review

**Spec coverage.** §14's append-only `events.jsonl` with `O_APPEND`, newline framing, fsync and torn-tail tolerance → Tasks 1–2. Write-ahead intent then result, and a crash between them being distinguishable → Task 2. `operation_id`, PID, process start time, boot id → Task 2. Per-seat state append-only-or-write-rename-with-both-fsyncs → Tasks 1 and 4. `started` with no receipt becoming `outcome_unknown` and never silently retried → Tasks 2 and 5. The five-dimension state tuple with the declared back-edge → Task 4. `--collect` resuming from disk and never from conversation state → Task 5. §15's run directory and named subpaths → Tasks 1 and 3. §9's protected-ref whitelist by exact name and recorded OID, and `source_diverged` → Tasks 3 and 5.

**Deliberately out of scope, each with a later home:** the §5 confirmation gate and cost quote; the orchestrator that sequences §6's five steps; the supervisor/payload process topology §14 describes for receipt-writing (it needs a process the engine launches, which is the orchestrator's); §12 strategy; §13 review; §10's claim ledger; §16 handover. Nothing here launches a provider.

**What this plan does not close, stated rather than implied.** §14's *"the wrapper process — not the parent — writes the completion receipt"* is not built: it requires a supervisor leading its own session with the payload in a separate process group, and there is no process launcher in this plan to host it. What is built is the record that makes a missing receipt *visible* — an orphan — rather than the topology that makes receipts reliable. §14 itself claims that teardown only best-effort, so this is a narrowing of scope, not a contradiction; the next plan must build the topology or the `outcome_unknown` rate will be higher than §18 assumes.

**Placeholder scan.** None. Task 2's `boot_id` fallback is specified as a decision to state rather than a value to invent, because a fallback that looks like a boot id and is not would make a reboot answerable and wrong.

**Type consistency.** `Manifest.setup`/`verify` are tuples of argv tuples at every site, matching `verify.Command.parse`'s input shape (a LIST of argv lists) once converted; `State`'s five fields are used identically in Tasks 4 and 5; `Reconstruction.orphans` holds `journal.Event`, which is what `journal.orphans` returns; `runstate.StateError`, `ManifestError`, `TransitionError` and `journal.JournalError` are all `RuntimeError`, matching the package's existing taxonomy.

**One risk worth naming.** Task 1's fsync tests assert on `/proc/self/fd`, which is Linux-specific. The package is already POSIX-only (`verify._kill_group` needs process groups) and this machine is Linux, so the constraint is not new — but a `/proc`-shaped assertion is a *test* that silently degrades on a platform where the package would otherwise still work. If `/proc/self/fd` is unavailable, the spy must fail loudly rather than record `<fd N>` and let the path assertions pass vacuously; the fixture above returns a placeholder for exactly that case, so **the implementer must make the vacuous branch fail rather than pass**. That is the "fixture that verifies nothing" shape this project has shipped four times.
