# llm-forge Plan B1: Baseline Foundations Implementation Plan

> **SUPERSEDED — do not execute.** This draft proposed a `B1 / B2 / C` decomposition that was
> replaced by the A–P plan series. Its work all shipped, under different module names:
> `gitio.py`→`gitcmd.py`, `secrets.py`→`screen.py`, `state.py`→`runstate.py`, and
> `python3 -m forge`→`scripts/forge.py` (the entry point the skill actually locates). Its two
> §19 leftovers are both live: `council.engine.MODE_TIMEOUT["forge"]` is 3600 and
> `AGY_STRUCTURED_TIMEOUT` maps agy's own timeout. Kept for the reasoning, not for the tasks —
> a plan-coverage audit that reads it as outstanding work is reading a draft as a backlog.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The forge engine's trust-primitive layer as a stdlib Python package
`shared/lib/forge/`: the append-only journal, the durable run store, read-only baseline
preflight with fail-closed structural checks, the pre-launch secret screen, composite
baseline `B₁` construction that never touches the user's index, exact-ref clone
materialization with byte-fidelity validation, and a `python3 -m forge preflight` CLI —
plus the two §19 leftovers in the council engine (`MODE_TIMEOUT["forge"]`, agy
timeout-reason mapping).

**Architecture:** Spec `docs/superpowers/specs/2026-07-30-llm-forge-design.md` §2, §3, §14.1,
§15, §19 (leftovers), §1 threat model. Everything here is deterministic engine code, testable
against fixture git repos with zero provider calls. Plan B2 (seat fleet, harvest, verifier
clones, handover) builds on these exact interfaces; Plan C adds the skill + evals. Plan A's
seams are in place at `shared/lib/council/engine.py`.

**Tech Stack:** Python 3.11+ stdlib only. pytest via `uvx pytest` from the repo root.
git 2.53 on this machine (plan declares ≥2.30 for `--no-write-fetch-head`).

## Global Constraints

- **Stdlib-only Python**; no pip deps, runs on any 3.11+ machine.
- Never edit `marketplaces/**` by hand; `make render` before every commit and include the
  regenerated files.
- Plan A's suites must stay green: `make council-test` exit 0 after every task here.
- Every git invocation in forge code is an **argv list with an explicit env dict** — never
  a shell string. Read-only phases carry `GIT_OPTIONAL_LOCKS=0` and
  `-c core.fsmonitor=false -c core.untrackedCache=false`; fleet operations additionally
  carry `GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null`.
- The engine **never writes to the user's index, checkout, or non-forge refs**. The only
  writes into the user's repo are objects plus refs under `refs/khenrix-forge/<run-id>/`
  (spec §2, §9). Tests assert the index byte-hash is unchanged.
- Fixture repos are built inside each test via `tmp_path` — never commit fixture repos.
- Before each commit: `.git/index.lock` absent; `pgrep -af 'make |render.py'` quiet.
- Commit trailer: end commit messages with the repo's standard `Co-Authored-By` trailer.

## File Structure

| Path | Responsibility |
|---|---|
| `shared/lib/forge/__init__.py` | empty package marker |
| `shared/lib/forge/journal.py` | append-only events journal, write-ahead intent, torn-line reader |
| `shared/lib/forge/state.py` | run directory under XDG_STATE_HOME, run lock with liveness, write-once manifest |
| `shared/lib/forge/gitio.py` | the ONE place argv/env git invocation lives |
| `shared/lib/forge/preflight.py` | read-only phase-1: facts, fail-closed checks, candidates, fs manifest |
| `shared/lib/forge/secrets.py` | pre-launch directory secret screen (delegates per-file logic to checks.scan_path) |
| `shared/lib/forge/baseline.py` | phase-2: alternate index, `B₁` commit, forge ref, index-preservation proof |
| `shared/lib/forge/fleet.py` | exact-ref materialization + byte-fidelity validation (B2 extends) |
| `shared/lib/forge/__main__.py` | `python3 -m forge preflight` CLI |
| `tests/test_forge_journal.py` … `tests/test_forge_cli.py` | one test file per module |
| `shared/lib/council/engine.py` | §19 leftovers only: `MODE_TIMEOUT["forge"]`, agy timeout mapping |
| `scripts/render.py` | `SHARED_LIBS` gains `"forge"` |
| `Makefile` | `FORGE_TESTS` wired into verify/test |

---

### Task 1: Journal

**Files:**
- Create: `shared/lib/forge/__init__.py` (empty), `shared/lib/forge/journal.py`
- Test: `tests/test_forge_journal.py`

**Interfaces:**
- Produces (B2 and every later task build on these):
  `Journal(path).append(record: dict) -> None`; `Journal.read() -> list[dict]`;
  `Journal.start(op: str, op_id: str, **fields)`; `Journal.done(op: str, op_id: str, **fields)`;
  `unresolved(records: list[dict]) -> list[dict]`; `class JournalCorrupt(RuntimeError)`.

- [ ] **Step 1: Write the failing tests**

```python
"""Journal: the durable spine (spec §14.1). Torn tails tolerated, interior corruption loud."""
import json
import os
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared" / "lib"))
from forge.journal import Journal, JournalCorrupt, unresolved  # noqa: E402


def test_append_read_roundtrip(tmp_path):
    j = Journal(tmp_path / "events.jsonl")
    j.append({"event": "phase", "value": 1})
    j.append({"event": "phase", "value": 2})
    assert [r["value"] for r in j.read()] == [1, 2]


def test_missing_file_reads_empty(tmp_path):
    assert Journal(tmp_path / "nope.jsonl").read() == []


def test_torn_final_line_is_discarded_not_fatal(tmp_path):
    p = tmp_path / "events.jsonl"
    j = Journal(p)
    j.append({"event": "a_start", "op_id": "1"})
    with open(p, "ab") as f:
        f.write(b'{"event": "a_done", "op_id"')   # crash mid-write: no newline
    assert [r["event"] for r in j.read()] == ["a_start"]


def test_interior_corruption_raises(tmp_path):
    p = tmp_path / "events.jsonl"
    p.write_bytes(b'not json\n{"event": "x"}\n')
    with pytest.raises(JournalCorrupt):
        Journal(p).read()


def test_write_ahead_intent_and_unresolved(tmp_path):
    j = Journal(tmp_path / "events.jsonl")
    j.start("setup", "op-1", argv=["make"])
    j.done("setup", "op-1", exit_code=0)
    j.start("verify", "op-2", argv=["make", "verify"])   # crash before done
    open_ops = unresolved(j.read())
    assert len(open_ops) == 1 and open_ops[0]["op_id"] == "op-2"


def test_records_are_single_lines_sorted_keys(tmp_path):
    p = tmp_path / "events.jsonl"
    Journal(p).append({"b": 1, "a": {"y": 2, "x": [3]}})
    raw = p.read_bytes()
    assert raw.endswith(b"\n") and raw.count(b"\n") == 1
    assert json.loads(raw) == {"a": {"x": [3], "y": 2}, "b": 1}
```

- [ ] **Step 2: Run — expect ImportError** — `uvx pytest tests/test_forge_journal.py -v`

- [ ] **Step 3: Implement `shared/lib/forge/journal.py`**

```python
"""Append-only operation journal — the durable spine of a forge run (spec §14.1).

Contracts, and their limits, stated plainly:
- one newline-terminated JSON object per record, written via os.write to an O_APPEND fd
  and fsync'd; the parent DIRECTORY is fsync'd when the file is first created — a new
  file without a directory fsync can vanish entirely in a crash (§10.1 uses exactly
  that omission as its false-green example);
- a torn FINAL line (crash mid-write) is discarded by the reader — everything before it
  is authoritative; interior corruption is a JournalCorrupt, never silently skipped;
- write-ahead intent: append "<op>_start" BEFORE the effect, "<op>_done" after. A start
  with no done and no surviving process is outcome_unknown and is NEVER silently
  retried — exactly-once execution of arbitrary work is not deliverable (§14.1).
"""
from __future__ import annotations

import json
import os
from pathlib import Path


class JournalCorrupt(RuntimeError):
    pass


class Journal:
    def __init__(self, path: Path):
        self.path = Path(path)

    def append(self, record: dict) -> None:
        data = (json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n").encode()
        existed = self.path.exists()
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        if not existed:
            dfd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)

    def read(self) -> list[dict]:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return []
        parts = raw.split(b"\n")
        # No trailing newline -> parts[-1] is a torn fragment: never parsed, by design.
        records = []
        for line in parts[:-1]:
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise JournalCorrupt(f"{self.path}: interior corruption: {e}") from e
        return records

    def start(self, op: str, op_id: str, **fields) -> None:
        self.append({"event": f"{op}_start", "op_id": op_id, **fields})

    def done(self, op: str, op_id: str, **fields) -> None:
        self.append({"event": f"{op}_done", "op_id": op_id, **fields})


def unresolved(records: list[dict]) -> list[dict]:
    """Start records lacking a matching done — the outcome_unknown candidates."""
    done_ids = {r.get("op_id") for r in records if str(r.get("event", "")).endswith("_done")}
    return [r for r in records
            if str(r.get("event", "")).endswith("_start") and r.get("op_id") not in done_ids]
```

Also create empty `shared/lib/forge/__init__.py`.

- [ ] **Step 4: Run — all pass** — `uvx pytest tests/test_forge_journal.py -v`

- [ ] **Step 5: Commit**

```bash
git add shared/lib/forge tests/test_forge_journal.py
git commit -m "feat(forge): append-only journal with write-ahead intent (B1 task 1)"
```

---

### Task 2: Run store and lock

**Files:**
- Create: `shared/lib/forge/state.py`
- Test: `tests/test_forge_state.py`

**Interfaces:**
- Produces: `state_root() -> Path` (respects `XDG_STATE_HOME`);
  `run_dir(repo_path: Path, run_id: str) -> Path` (`<root>/khenrix-forge/<sha256(realpath)[:12]>-<run_id>`, mode 0700, parents created);
  `boot_id() -> str`; `proc_start(pid: int) -> str | None`;
  `class RunLock(path)` with `.acquire() -> None` (raises `Locked` on a LIVE holder;
  silently replaces a stale one), `.release()`;
  `write_manifest(path: Path, data: dict) -> None` (write-once: raises `FileExistsError`
  if present; tmp + fsync + `os.replace` + dir fsync); `read_manifest(path) -> dict`;
  `class Locked(RuntimeError)`.

- [ ] **Step 1: Write the failing tests**

```python
import json
import os
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared" / "lib"))
from forge import state  # noqa: E402


def test_run_dir_hashed_and_private(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    a = state.run_dir(Path("/home/u/git/a/utils"), "run1")
    b = state.run_dir(Path("/home/u/work/b/utils"), "run1")
    assert a != b, "same-basename repos must not collide (spec §15)"
    assert a.is_dir() and (a.stat().st_mode & 0o777) == 0o700
    assert str(a).startswith(str(tmp_path / "xdg" / "khenrix-forge"))


def test_boot_id_and_proc_start_read_real_proc():
    assert len(state.boot_id()) >= 8
    assert state.proc_start(os.getpid()) is not None
    assert state.proc_start(2**22 + 12345) is None   # implausible pid


def test_lock_blocks_live_holder_replaces_stale(tmp_path):
    lock = state.RunLock(tmp_path / "lock.json")
    lock.acquire()   # us: live
    with pytest.raises(state.Locked):
        state.RunLock(tmp_path / "lock.json").acquire()
    # Forge a stale lock: dead pid, same boot.
    (tmp_path / "lock.json").write_text(json.dumps(
        {"pid": 2**22 + 99999, "start": "0", "boot": state.boot_id()}))
    state.RunLock(tmp_path / "lock.json").acquire()   # stale -> replaced, no raise


def test_lock_pid_reuse_defeated_by_start_time(tmp_path):
    # Same pid, same boot, WRONG start time -> a recycled pid, not our holder: stale.
    (tmp_path / "lock.json").write_text(json.dumps(
        {"pid": os.getpid(), "start": "1", "boot": state.boot_id()}))
    state.RunLock(tmp_path / "lock.json").acquire()


def test_manifest_write_once(tmp_path):
    p = tmp_path / "run.json"
    state.write_manifest(p, {"base_commit": "abc"})
    assert state.read_manifest(p) == {"base_commit": "abc"}
    with pytest.raises(FileExistsError):
        state.write_manifest(p, {"base_commit": "clobber"})
```

- [ ] **Step 2: Run — expect failures** — `uvx pytest tests/test_forge_state.py -v`

- [ ] **Step 3: Implement `shared/lib/forge/state.py`**

```python
"""Durable run store (spec §15) and the liveness lock (spec §14.1).

XDG_STATE_HOME, not ~/.cache: cache is defined as deletable-without-loss and is what
every cleanup tool targets first — it must not hold the only copy of a run. The repo
path is hashed so /a/utils and /b/utils cannot collide. Lock liveness is
(pid, proc start time, boot id): a pid alone survives reuse, a pid+start survives
same-boot reuse, and the boot id retires every lock from before a reboot.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


class Locked(RuntimeError):
    pass


def state_root() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME",
                               str(Path.home() / ".local" / "state"))) / "khenrix-forge"


def run_dir(repo_path: Path, run_id: str) -> Path:
    tag = hashlib.sha256(str(Path(repo_path)).encode()).hexdigest()[:12]
    d = state_root() / f"{tag}-{run_id}"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(d, 0o700)   # mkdir mode is umask-filtered; enforce
    return d


def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def proc_start(pid: int) -> str | None:
    """Field 22 of /proc/<pid>/stat — parsed after the last ')' so a comm containing
    spaces or parens cannot shift the fields."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    post = stat.rpartition(")")[2].split()   # fields 3..N
    return post[19] if len(post) > 19 else None


class RunLock:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _holder_alive(self, data: dict) -> bool:
        if data.get("boot") != boot_id():
            return False
        start = proc_start(int(data.get("pid", -1)))
        return start is not None and start == data.get("start")

    def acquire(self) -> None:
        me = {"pid": os.getpid(), "start": proc_start(os.getpid()), "boot": boot_id()}
        payload = json.dumps(me).encode()
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            try:
                held = json.loads(self.path.read_text())
            except (OSError, json.JSONDecodeError):
                held = {}
            if self._holder_alive(held):
                raise Locked(f"{self.path} held by live pid {held.get('pid')}") from None
            # Stale: replace atomically. A race between two replacers is benign —
            # both holders are us-or-dead; last write wins.
            tmp = self.path.with_suffix(".tmp")
            tmp.write_bytes(payload)
            os.replace(tmp, self.path)
            return
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)

    def release(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def write_manifest(path: Path, data: dict) -> None:
    """Write-once (spec §14.2: the run manifest is immutable — commands are never
    re-detected on resume)."""
    path = Path(path)
    if path.exists():
        raise FileExistsError(f"{path} is write-once")
    tmp = path.with_suffix(".tmp")
    raw = json.dumps(data, indent=1, sort_keys=True).encode()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, raw)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    dfd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def read_manifest(path: Path) -> dict:
    return json.loads(Path(path).read_text())
```

- [ ] **Step 4: Run — all pass**, then **Step 5: Commit**

```bash
git add shared/lib/forge/state.py tests/test_forge_state.py
git commit -m "feat(forge): run store, liveness lock, write-once manifest (B1 task 2)"
```

---

### Task 3: git invocation layer + read-only preflight

**Files:**
- Create: `shared/lib/forge/gitio.py`, `shared/lib/forge/preflight.py`
- Test: `tests/test_forge_preflight.py`

**Interfaces:**
- Produces:
  - `gitio.git(repo, *args, env_extra: dict | None = None, binary: bool = False, check: bool = True) -> subprocess.CompletedProcess` — always argv, always `-c core.fsmonitor=false -c core.untrackedCache=false`, `GIT_OPTIONAL_LOCKS=0` merged in; raises `GitError` on nonzero when `check`.
  - `gitio.fleet_env() -> dict` — `{"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}` (Task 6 uses it).
  - `preflight.repo_facts(repo) -> dict` — keys `toplevel, head, head_tree, object_format, zeros_oid`.
  - `preflight.fail_closed(repo, selected: list[str]) -> list[str]` — problem strings, empty = pass; **scoped**: structural rejections cover tracked content + selected paths only (spec §2.3).
  - `preflight.untracked_candidates(repo) -> list[str]` — non-ignored untracked, NUL-safe.
  - `preflight.fs_manifest(repo, paths: list[str]) -> dict[str, dict]` — per path `{"sha256", "mode", "size"}`; symlinks hash the target string, never followed.
  - `class GitError(RuntimeError)`.

- [ ] **Step 1: Write the failing tests**

```python
import os
import subprocess
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared" / "lib"))
from forge import gitio, preflight  # noqa: E402


def mk_repo(tmp_path, name="r"):
    r = tmp_path / name
    r.mkdir()
    subprocess.run(["git", "-C", str(r), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(r), "-c", "user.email=u@t", "-c", "user.name=u",
                    "commit", "--allow-empty", "-q", "-m", "seed"], check=True)
    return r


def commit_file(r, rel, content=b"x\n", mode=None):
    p = r / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    if mode:
        os.chmod(p, mode)
    subprocess.run(["git", "-C", str(r), "add", rel], check=True)
    subprocess.run(["git", "-C", str(r), "-c", "user.email=u@t", "-c", "user.name=u",
                    "commit", "-q", "-m", f"add {rel}"], check=True)


def test_repo_facts(tmp_path):
    r = mk_repo(tmp_path)
    f = preflight.repo_facts(r)
    assert f["toplevel"] == str(r.resolve())
    assert len(f["head"]) in (40, 64)
    assert f["zeros_oid"] == "0" * len(f["head"])
    assert f["object_format"] in ("sha1", "sha256")


def test_clean_repo_passes_fail_closed(tmp_path):
    r = mk_repo(tmp_path)
    commit_file(r, "a.txt")
    assert preflight.fail_closed(r, []) == []


def test_submodule_rejected(tmp_path):
    r, sub = mk_repo(tmp_path, "r"), mk_repo(tmp_path, "sub")
    subprocess.run(["git", "-C", str(r), "-c", "protocol.file.allow=always",
                    "submodule", "add", "-q", str(sub), "s"], check=True)
    assert any("submodule" in p for p in preflight.fail_closed(r, []))


def test_filter_attr_rejected(tmp_path):
    r = mk_repo(tmp_path)
    commit_file(r, ".gitattributes", b"*.x filter=lfs\n")
    commit_file(r, "data.x", b"payload\n")
    assert any("filter" in p for p in preflight.fail_closed(r, []))


def test_unselected_nested_repo_ignored_selected_rejected(tmp_path):
    # The leaked-worktree case (spec §2.3): a .git FILE in an ignored dir must NOT
    # abort preflight — unless the user selects a path inside it.
    r = mk_repo(tmp_path)
    commit_file(r, ".gitignore", b"scratch/\n")
    nest = r / "scratch" / "wt"
    nest.mkdir(parents=True)
    (nest / ".git").write_text("gitdir: /somewhere/else\n")
    (nest / "f.txt").write_text("x")
    assert preflight.fail_closed(r, []) == []
    assert any("nested" in p for p in preflight.fail_closed(r, ["scratch/wt/f.txt"]))


def test_escaping_symlink_rejected_inside_ok(tmp_path):
    r = mk_repo(tmp_path)
    (r / "inside.txt").write_text("x")
    os.symlink("inside.txt", r / "ok_link")
    os.symlink("../outside", r / "bad_link")
    subprocess.run(["git", "-C", str(r), "add", "ok_link", "bad_link"], check=True)
    subprocess.run(["git", "-C", str(r), "-c", "user.email=u@t", "-c", "user.name=u",
                    "commit", "-q", "-m", "links"], check=True)
    probs = preflight.fail_closed(r, [])
    assert any("bad_link" in p for p in probs)
    assert not any("ok_link" in p for p in probs)


def test_intent_to_add_rejected(tmp_path):
    r = mk_repo(tmp_path)
    (r / "ita.txt").write_text("x")
    subprocess.run(["git", "-C", str(r), "add", "-N", "ita.txt"], check=True)
    assert any("intent-to-add" in p for p in preflight.fail_closed(r, []))


def test_untracked_candidates_excludes_ignored(tmp_path):
    r = mk_repo(tmp_path)
    commit_file(r, ".gitignore", b"*.log\n")
    (r / "new.py").write_text("x")
    (r / "noise.log").write_text("x")
    c = preflight.untracked_candidates(r)
    assert "new.py" in c and "noise.log" not in c


def test_fs_manifest_modes_and_symlink_target(tmp_path):
    r = mk_repo(tmp_path)
    commit_file(r, "plain.txt", b"hello\n")
    commit_file(r, "runme.sh", b"#!/bin/sh\n", mode=0o755)
    os.symlink("plain.txt", r / "lnk")
    m = preflight.fs_manifest(r, ["plain.txt", "runme.sh", "lnk"])
    assert m["plain.txt"]["mode"] == "100644" and m["runme.sh"]["mode"] == "100755"
    assert m["lnk"]["mode"] == "120000"
    import hashlib
    assert m["lnk"]["sha256"] == hashlib.sha256(b"plain.txt").hexdigest()


def test_preflight_writes_nothing(tmp_path):
    r = mk_repo(tmp_path)
    commit_file(r, "a.txt")
    (r / "dirty.txt").write_text("uncommitted")
    idx = (r / ".git" / "index").read_bytes()
    preflight.repo_facts(r)
    preflight.fail_closed(r, [])
    preflight.untracked_candidates(r)
    preflight.fs_manifest(r, ["a.txt"])
    assert (r / ".git" / "index").read_bytes() == idx, "read-only phase wrote the index"
```

- [ ] **Step 2: Run — expect failures**, then **Step 3: Implement**

`shared/lib/forge/gitio.py`:

```python
"""The one place a git process is spawned. Argv lists + explicit env, never shell
(spec §2.2, §5.1). Daemon state (fsmonitor, untracked cache) is pinned off so no
snapshot depends on it; GIT_OPTIONAL_LOCKS=0 stops read-oriented commands
opportunistically refreshing the user's real index — it does NOT make required
locks safe, which is why baseline.py never runs write-tree against the real index."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def git(repo, *args, env_extra: dict | None = None, binary: bool = False,
        check: bool = True) -> subprocess.CompletedProcess:
    argv = ["git", "-C", str(repo),
            "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", *args]
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", **(env_extra or {})}
    r = subprocess.run(argv, capture_output=True, text=not binary, env=env, timeout=120)
    if check and r.returncode != 0:
        err = r.stderr if isinstance(r.stderr, str) else r.stderr.decode(errors="replace")
        raise GitError(f"git {' '.join(args[:3])}… rc={r.returncode}: {err.strip()[:300]}")
    return r


def fleet_env() -> dict:
    """Global/system config disabled for clone/fetch and every seat/verifier git call:
    an empty template does not neutralise a global core.hooksPath or url.insteadOf
    (spec §4.1)."""
    return {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
```

`shared/lib/forge/preflight.py`:

```python
"""Read-only phase 1 (spec §2.2): describe, never create. No write-tree here — on the
real index it takes index.lock unconditionally and rewrites the stale cache-tree,
which is precisely the dirty-tree case forge exists for.

fail_closed() is SCOPED to tracked content plus the user's selected paths (spec §2.3):
this repo itself carries leaked agy worktrees under gitignored evals/*/workspace/, and
an unscoped structural sweep would abort every first run on artifacts the user never
created."""
from __future__ import annotations

import hashlib
import os
import stat as stat_mod
from pathlib import Path

from .gitio import git


def repo_facts(repo) -> dict:
    top = git(repo, "rev-parse", "--show-toplevel").stdout.strip()
    head = git(repo, "rev-parse", "--verify", "HEAD^{commit}").stdout.strip()
    tree = git(repo, "rev-parse", "HEAD^{tree}").stdout.strip()
    fmt = git(repo, "rev-parse", "--show-object-format").stdout.strip()
    return {"toplevel": top, "head": head, "head_tree": tree,
            "object_format": fmt, "zeros_oid": "0" * len(head)}


def _tracked(repo) -> list[str]:
    out = git(repo, "ls-files", "-z").stdout
    return [p for p in out.split("\0") if p]


def untracked_candidates(repo) -> list[str]:
    out = git(repo, "ls-files", "-o", "--exclude-standard", "-z").stdout
    return sorted(p for p in out.split("\0") if p)


def fail_closed(repo, selected: list[str]) -> list[str]:
    probs: list[str] = []
    top = Path(git(repo, "rev-parse", "--show-toplevel").stdout.strip()).resolve()

    if git(repo, "rev-parse", "--is-shallow-repository").stdout.strip() == "true":
        probs.append("shallow repository (spec §2.3)")
    if git(repo, "config", "--get", "extensions.partialClone", check=False).returncode == 0 \
       or git(repo, "config", "--get-regexp", r"remote\..*\.promisor", check=False).returncode == 0:
        probs.append("partial clone / promisor remote (spec §2.3)")
    if git(repo, "sparse-checkout", "list", check=False).returncode == 0:
        probs.append("sparse checkout (spec §2.3)")
    if git(repo, "ls-files", "--unmerged").stdout.strip():
        probs.append("unmerged index entries (mid-merge?) (spec §2.3)")

    # Stage scan: submodules (gitlinks), intent-to-add (null OID), escaping symlinks.
    zeros = None
    for line in git(repo, "ls-files", "-s", "-z").stdout.split("\0"):
        if not line:
            continue
        meta, _, rel = line.partition("\t")
        mode, oid, _stage = meta.split()
        zeros = zeros or "0" * len(oid)
        if mode == "160000":
            probs.append(f"submodule at {rel} (spec §2.3: all submodules rejected)")
        elif oid == zeros:
            probs.append(f"intent-to-add entry {rel} (spec §2.3)")
        elif mode == "120000":
            target = os.readlink(top / rel) if (top / rel).is_symlink() else ""
            dest = (top / rel).parent / target
            try:
                dest.resolve().relative_to(top)
            except ValueError:
                probs.append(f"symlink {rel} escapes the tree -> {target} (spec §2.3)")

    # filter= / working-tree-encoding attributes on tracked + selected paths.
    paths = _tracked(repo) + list(selected)
    if paths:
        joined = "\0".join(paths)
        out = git(repo, "check-attr", "--stdin", "-z", "filter", "working-tree-encoding",
                  env_extra={}, binary=False, check=True,
                  ).stdout if False else None
        # check-attr -z --stdin reads NUL-separated paths on stdin; gitio.git has no
        # stdin channel, so run it via subprocess here with the same discipline:
        import subprocess, os as _os
        r = subprocess.run(
            ["git", "-C", str(repo), "check-attr", "--stdin", "-z",
             "filter", "working-tree-encoding"],
            input=joined.encode() + b"\0", capture_output=True,
            env={**_os.environ, "GIT_OPTIONAL_LOCKS": "0"}, timeout=120)
        fields = r.stdout.decode(errors="replace").split("\0")
        # -z output: path, attr, value, path, attr, value, ...
        for i in range(0, len(fields) - 2, 3):
            p, attr, value = fields[i], fields[i + 1], fields[i + 2]
            if value not in ("unspecified", ""):
                probs.append(f"{attr}={value} on {p} (spec §2.3: content filters rejected)")

    # Selected-path structural checks (the SCOPE rule): nested repos only matter if selected.
    for rel in selected:
        parts = Path(rel).parts
        for i in range(1, len(parts) + 1):
            probe = top.joinpath(*parts[:i], ".git")
            if probe.exists():
                probs.append(f"nested repository at {Path(*parts[:i])} covers selected "
                             f"path {rel} (spec §2.3)")
                break
        ap = top / rel
        if ap.is_symlink():
            try:
                ap.resolve().relative_to(top)
            except ValueError:
                probs.append(f"selected symlink {rel} escapes the tree (spec §2.3)")
    return sorted(set(probs))


def fs_manifest(repo, paths: list[str]) -> dict[str, dict]:
    top = Path(git(repo, "rev-parse", "--show-toplevel").stdout.strip())
    out: dict[str, dict] = {}
    for rel in paths:
        p = top / rel
        st = os.lstat(p)
        if stat_mod.S_ISLNK(st.st_mode):
            data, mode = os.readlink(p).encode(), "120000"
        else:
            data = p.read_bytes()
            mode = "100755" if st.st_mode & 0o100 else "100644"
        out[rel] = {"sha256": hashlib.sha256(data).hexdigest(),
                    "mode": mode, "size": len(data)}
    return out
```

**Note on the check-attr block:** the dead `out = … if False else None` line in the sketch
above is a plan artifact — do not transcribe it. Write the subprocess call directly (it is
the one git call needing stdin; keep the module-level imports at the top of the file, not
inline). If `git ls-files -s` on an intent-to-add entry does NOT show a null OID on git
2.53, detect ITA instead via `git status --porcelain=v2` lines whose second field is
`N...` with an `A` in the index column — the TEST (reject `git add -N`) is the contract,
the detection mechanism is the adaptation point; cite what you observed in the report.

- [ ] **Step 4: Run — all pass** — `uvx pytest tests/test_forge_preflight.py -v`
- [ ] **Step 5: Commit**

```bash
git add shared/lib/forge/gitio.py shared/lib/forge/preflight.py tests/test_forge_preflight.py
git commit -m "feat(forge): gitio + read-only preflight with scoped fail-closed checks (B1 task 3)"
```

---

### Task 4: Secret screen

**Files:**
- Create: `shared/lib/forge/secrets.py`
- Test: `tests/test_forge_secrets.py`

**Interfaces:**
- Produces: `scan_tree(root: Path, rel_paths: list[str], *, per_file_cap=1_000_000,
  total_cap=50_000_000) -> list[str]` — problems; empty = clean. Delegates per-file
  pattern logic to `checks.scan_path` (the repo's existing scanner) — filters, caps and
  the walk live here; patterns are NOT forked (spec §3). Raises `ScreenUnavailable`
  when `checks.py` cannot be imported (plugin layout — Plan C revisits).

- [ ] **Step 1: Write the failing tests**

```python
from pathlib import Path

import pytest

import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))
from forge import secrets  # noqa: E402


def test_planted_token_is_flagged(tmp_path):
    bad = tmp_path / "config.local.py"
    bad.write_text('TOKEN = "ghp_' + "a" * 36 + '"\n')
    probs = secrets.scan_tree(tmp_path, ["config.local.py"])
    assert probs and "config.local.py" in probs[0]


def test_clean_file_passes(tmp_path):
    (tmp_path / "ok.py").write_text("x = 1\n")
    assert secrets.scan_tree(tmp_path, ["ok.py"]) == []


def test_binary_skipped(tmp_path):
    (tmp_path / "blob.bin").write_bytes(b"\x00" + b"ghp_" + b"a" * 36)
    assert secrets.scan_tree(tmp_path, ["blob.bin"]) == []


def test_per_file_cap_fails_closed(tmp_path):
    big = tmp_path / "big.txt"
    big.write_bytes(b"a" * 2048)
    probs = secrets.scan_tree(tmp_path, ["big.txt"], per_file_cap=1024)
    assert probs and "cap" in probs[0], "an unscannable file is a PROBLEM, not a skip (spec §3)"


def test_total_cap_fails_closed(tmp_path):
    for i in range(4):
        (tmp_path / f"f{i}.txt").write_bytes(b"a" * 400)
    probs = secrets.scan_tree(tmp_path, [f"f{i}.txt" for i in range(4)], total_cap=1000)
    assert any("aggregate" in p for p in probs)


def test_missing_file_is_a_problem(tmp_path):
    assert secrets.scan_tree(tmp_path, ["ghost.txt"])
```

- [ ] **Step 2: Run — expect failures**, then **Step 3: Implement `shared/lib/forge/secrets.py`**

```python
"""Pre-launch secret screen (spec §3): runs BEFORE any provider starts, because the
baseline is what N cloud-backed full-permission seats read — a post-harvest scan is
after the exposure. Stated honestly: a high-confidence screen, never proof.

Per-file pattern logic is checks.scan_path — the repo's existing scanner, with its
SECRET_FAIL / allowlist / skip-list semantics. This module adds only what §3 requires
on top: the walk, a binary skip, and caps that FAIL CLOSED with a report line."""
from __future__ import annotations

import sys
from pathlib import Path


class ScreenUnavailable(RuntimeError):
    pass


def _checks():
    root = Path(__file__).resolve().parents[3]   # shared/lib/forge -> repo root
    cand = root / "scripts" / "lib"
    if str(cand) not in sys.path:
        sys.path.insert(0, str(cand))
    try:
        import checks  # noqa: PLC0415
        return checks
    except ImportError as e:
        raise ScreenUnavailable(
            "checks.py not importable (plugin layout?) — the secret screen requires "
            "the repo's scanner; Plan C revisits packaging") from e


def scan_tree(root: Path, rel_paths: list[str], *,
              per_file_cap: int = 1_000_000, total_cap: int = 50_000_000) -> list[str]:
    checks = _checks()
    root = Path(root)
    probs: list[str] = []
    total = 0
    skip_suffix = tuple(getattr(checks, "SCAN_SKIP_SUFFIX", ()))
    skip_dirs = set(getattr(checks, "SCAN_SKIP_DIRS", ()))
    for rel in sorted(rel_paths):
        p = root / rel
        if any(part in skip_dirs for part in Path(rel).parts[:-1]):
            continue
        if rel.endswith(skip_suffix):
            continue
        if p.is_symlink():
            continue   # targets are hashed, never read, elsewhere; do not follow (spec §7.4)
        if not p.is_file():
            probs.append(f"secret-screen: {rel}: missing or not a regular file")
            continue
        size = p.stat().st_size
        if size > per_file_cap:
            probs.append(f"secret-screen: {rel}: {size}B exceeds per-file cap "
                         f"{per_file_cap} — fail closed (spec §3)")
            continue
        total += size
        if total > total_cap:
            probs.append(f"secret-screen: aggregate cap {total_cap}B exceeded at {rel} "
                         "— fail closed (spec §3)")
            break
        with open(p, "rb") as f:
            if b"\0" in f.read(8192):
                continue   # binary: line patterns are meaningless
        probs.extend(checks.scan_path(p))
    return probs
```

If `checks.scan_path`'s real signature differs (read it first — it may take the repo
root or return tuples), adapt the delegation call ONLY, and say so in your report.

- [ ] **Step 4: Run — all pass**, **Step 5: Commit**

```bash
git add shared/lib/forge/secrets.py tests/test_forge_secrets.py
git commit -m "feat(forge): pre-launch secret screen delegating to checks.scan_path (B1 task 4)"
```

---

### Task 5: Baseline B₁ construction

**Files:**
- Create: `shared/lib/forge/baseline.py`
- Test: `tests/test_forge_baseline.py`

**Interfaces:**
- Consumes: `gitio.git`, `preflight.repo_facts`, `preflight.fs_manifest`, `state.write_manifest`.
- Produces: `build_baseline(repo, run: Path, run_id: str, selected: list[str]) -> dict` with keys
  `base_commit, tracked_tree_oid, b1, ref, created` — `created` False when the tree is
  clean (`b1 == base_commit`, no commit object made, ref still written). Raises
  `BaselineAbort` if the user's index byte-hash or the tracked filesystem changed
  underneath the snapshot. `class BaselineAbort(RuntimeError)`.

- [ ] **Step 1: Write the failing tests**

```python
import hashlib
import os
import subprocess
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared" / "lib"))
from forge import baseline, gitio  # noqa: E402
from test_forge_preflight import mk_repo, commit_file  # noqa: E402


def tree_of(repo, commit):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", f"{commit}^{{tree}}"],
                          capture_output=True, text=True).stdout.strip()


def ls_tree(repo, tree):
    out = subprocess.run(["git", "-C", str(repo), "ls-tree", "-r", tree],
                         capture_output=True, text=True).stdout
    return {line.split("\t")[1]: line.split()[0] for line in out.splitlines()}


def test_dirty_tracked_and_selected_untracked_land_in_b1(tmp_path):
    r = mk_repo(tmp_path)
    commit_file(r, "src/app.py", b"v1\n")
    (r / "src" / "app.py").write_bytes(b"v2-unstaged\n")     # unstaged tracked edit
    (r / "staged.py").write_text("staged\n")
    subprocess.run(["git", "-C", str(r), "add", "staged.py"], check=True)
    (r / "parser.py").write_text("selected untracked\n")     # spec §2.1's parser case
    (r / "notes.txt").write_text("NOT selected\n")
    run = tmp_path / "run"; run.mkdir()
    b = baseline.build_baseline(r, run, "t1", ["parser.py"])
    entries = ls_tree(r, b["tracked_tree_oid"])
    assert "parser.py" in entries and "staged.py" in entries
    assert "notes.txt" not in entries, "unselected untracked must NOT be swept in (add -A ban)"
    blob = subprocess.run(["git", "-C", str(r), "show", f"{b['b1']}:src/app.py"],
                          capture_output=True, text=True).stdout
    assert blob == "v2-unstaged\n"
    assert b["created"] is True and b["b1"] != b["base_commit"]


def test_clean_tree_degenerates_to_head(tmp_path):
    r = mk_repo(tmp_path)
    commit_file(r, "a.txt")
    run = tmp_path / "run"; run.mkdir()
    b = baseline.build_baseline(r, run, "t2", [])
    assert b["created"] is False and b["b1"] == b["base_commit"]
    ref = subprocess.run(["git", "-C", str(r), "rev-parse", b["ref"]],
                         capture_output=True, text=True).stdout.strip()
    assert ref == b["base_commit"], "ref must exist even in the degenerate case"


def test_users_index_and_worktree_untouched(tmp_path):
    r = mk_repo(tmp_path)
    commit_file(r, "a.txt")
    (r / "a.txt").write_bytes(b"dirty\n")
    (r / "sel.txt").write_text("sel\n")
    idx_before = (r / ".git" / "index").read_bytes()
    status_before = subprocess.run(["git", "-C", str(r), "status", "--porcelain"],
                                   capture_output=True, text=True).stdout
    run = tmp_path / "run"; run.mkdir()
    baseline.build_baseline(r, run, "t3", ["sel.txt"])
    assert (r / ".git" / "index").read_bytes() == idx_before
    status_after = subprocess.run(["git", "-C", str(r), "status", "--porcelain"],
                                  capture_output=True, text=True).stdout
    assert status_after == status_before, "sel.txt must still be untracked for the USER"


def test_literal_pathspecs_glob_and_dash_names(tmp_path):
    r = mk_repo(tmp_path)
    commit_file(r, "a.txt")
    (r / "*.c").write_text("literal star\n")
    (r / "-dash.txt").write_text("dash\n")
    (r / "real.c").write_text("must not be swept by *.c\n")
    run = tmp_path / "run"; run.mkdir()
    b = baseline.build_baseline(r, run, "t4", ["*.c", "-dash.txt"])
    entries = ls_tree(r, b["tracked_tree_oid"])
    assert "*.c" in entries and "-dash.txt" in entries
    assert "real.c" not in entries, "glob expanded — GIT_LITERAL_PATHSPECS was not applied"


def test_b1_authorship_user_author_forge_committer(tmp_path):
    r = mk_repo(tmp_path)
    subprocess.run(["git", "-C", str(r), "config", "user.name", "Real User"], check=True)
    subprocess.run(["git", "-C", str(r), "config", "user.email", "real@user"], check=True)
    commit_file(r, "a.txt")
    (r / "a.txt").write_bytes(b"dirty\n")
    run = tmp_path / "run"; run.mkdir()
    b = baseline.build_baseline(r, run, "t5", [])
    show = subprocess.run(["git", "-C", str(r), "show", "-s",
                           "--format=%an|%ae|%cn", b["b1"]],
                          capture_output=True, text=True).stdout.strip()
    an, ae, cn = show.split("|")
    assert (an, ae) == ("Real User", "real@user")
    assert cn == "llm-forge"
    msg = subprocess.run(["git", "-C", str(r), "show", "-s", "--format=%B", b["b1"]],
                         capture_output=True, text=True).stdout
    assert "this commit is yours, not forge's" in msg


def test_concurrent_edit_aborts(tmp_path, monkeypatch):
    r = mk_repo(tmp_path)
    commit_file(r, "a.txt")
    (r / "a.txt").write_bytes(b"dirty\n")
    run = tmp_path / "run"; run.mkdir()
    real = baseline._post_fs_state
    monkeypatch.setattr(baseline, "_post_fs_state",
                        lambda repo, paths: {**real(repo, paths), "a.txt": {"sha256": "MOVED"}})
    with pytest.raises(baseline.BaselineAbort):
        baseline.build_baseline(r, run, "t6", [])
```

- [ ] **Step 2: Run — expect failures**, then **Step 3: Implement `shared/lib/forge/baseline.py`**

```python
"""Phase 2: create the composite baseline's git objects (spec §2.1–§2.2), post-consent.

The alternate index starts as a BYTE COPY of the real index — git writes the index by
atomic rename, so a plain copy is a consistent snapshot, never torn — and every
command runs under GIT_INDEX_FILE. `git write-tree` never touches the real index
(it takes index.lock unconditionally and rewrites a stale cache-tree: B2 of round 3).

B₁ = commit-tree <tree> -p HEAD, author = the user, committer = forge, with a message
saying exactly whose work it snapshots. A clean tree creates NO commit: b1 == HEAD and
the ref points at HEAD so every consumer stays uniform. update-ref immediately follows
commit-tree — an unreferenced commit is `gc --prune=now` bait — with the all-zeros
old-value guard at the repository's hash width (64 in a sha256 repo, not always 40)."""
from __future__ import annotations

import hashlib
from pathlib import Path

from .gitio import git
from .preflight import fs_manifest, repo_facts, _tracked


class BaselineAbort(RuntimeError):
    pass


BASELINE_MSG = ("forge: snapshot of your uncommitted working tree at {when} "
                "— this commit is yours, not forge's.")


def _index_path(repo) -> Path:
    return Path(git(repo, "rev-parse", "--git-path", "index").stdout.strip()
                if Path(git(repo, "rev-parse", "--git-path", "index").stdout.strip()).is_absolute()
                else Path(git(repo, "rev-parse", "--show-toplevel").stdout.strip())
                / git(repo, "rev-parse", "--git-path", "index").stdout.strip())


def _post_fs_state(repo, paths):
    return fs_manifest(repo, paths)


def _user_identity(repo) -> tuple[str, str]:
    name = git(repo, "config", "user.name", check=False).stdout.strip() or "forge-user"
    email = git(repo, "config", "user.email", check=False).stdout.strip() or "forge@local"
    return name, email


def build_baseline(repo, run: Path, run_id: str, selected: list[str]) -> dict:
    facts = repo_facts(repo)
    top = Path(facts["toplevel"])
    idx = _index_path(repo)
    idx_before = idx.read_bytes()
    watch = _tracked(repo) + list(selected)
    fs_before = fs_manifest(repo, watch)

    alt = Path(run) / "index"
    if alt.exists():
        raise BaselineAbort(f"{alt} already exists — refusing to reuse an alternate index")
    alt.write_bytes(idx_before)                      # consistent snapshot: atomic-rename write
    env_idx = {"GIT_INDEX_FILE": str(alt)}

    git(top, "add", "-u", "--", ":/", env_extra=env_idx)      # unstaged tracked, repo-wide
    if selected:
        pf = Path(run) / "pathspecs"
        pf.write_bytes(b"\0".join(p.encode() for p in selected))
        git(top, "add", "-f", "--pathspec-from-file", str(pf), "--pathspec-file-nul",
            env_extra={**env_idx, "GIT_LITERAL_PATHSPECS": "1"})
    tree = git(top, "write-tree", env_extra=env_idx).stdout.strip()

    if tree == facts["head_tree"] and not selected:
        b1, created = facts["head"], False
    elif tree == facts["head_tree"]:
        b1, created = facts["head"], False           # selected paths matched nothing new
    else:
        import datetime
        an, ae = _user_identity(repo)
        msg = BASELINE_MSG.format(
            when=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"))
        b1 = git(top, "commit-tree", tree, "-p", facts["head"], "-m", msg,
                 env_extra={"GIT_AUTHOR_NAME": an, "GIT_AUTHOR_EMAIL": ae,
                            "GIT_COMMITTER_NAME": "llm-forge",
                            "GIT_COMMITTER_EMAIL": "forge@khenrix.local"},
                 ).stdout.strip()
        created = True
    ref = f"refs/khenrix-forge/{run_id}/base"
    git(top, "update-ref", ref, b1, facts["zeros_oid"])

    if idx.read_bytes() != idx_before:
        raise BaselineAbort("engine wrote the user's index — protected-state violation (spec §9)")
    if _post_fs_state(repo, watch) != fs_before:
        raise BaselineAbort("source tree changed mid-snapshot — baseline is not trustworthy "
                            "(spec §2.2); re-run when the tree is quiet")
    return {"base_commit": facts["head"], "tracked_tree_oid": tree,
            "b1": b1, "ref": ref, "created": created}
```

**Simplify `_index_path` when you write it** — the sketch's ternary is convoluted; the
clean form: resolve `git rev-parse --git-path index` and, if relative, join it onto the
toplevel. Three lines. (The sketch calls git twice; yours should call it once.)

Note the degenerate branch: `update-ref` with the zeros guard runs in BOTH cases, so a
second `build_baseline` for the same run_id fails loudly (ref exists) rather than
silently re-pointing — write-once at the ref layer.

- [ ] **Step 4: Run — all pass** — `uvx pytest tests/test_forge_baseline.py -v`
- [ ] **Step 5: Commit**

```bash
git add shared/lib/forge/baseline.py tests/test_forge_baseline.py
git commit -m "feat(forge): composite baseline B1 via alternate index; user index provably untouched (B1 task 5)"
```

---

### Task 6: Exact-ref materialization + fidelity

**Files:**
- Create: `shared/lib/forge/fleet.py`
- Test: `tests/test_forge_fleet.py`

**Interfaces:**
- Consumes: `gitio.git`, `gitio.fleet_env`, `preflight.fs_manifest`, Task 5's ref.
- Produces: `materialize(repo, ref: str, dest: Path, *, branch: str | None = None) -> str`
  (returns checked-out OID; `git init` + `fetch file://` with an explicit `+<ref>:refs/forge/base`
  refspec — **no origin remote ever exists**, closing the push vector without a removal
  step) and `validate_manifest(dest: Path, manifest: dict[str, dict]) -> list[str]`
  (mismatches; empty = byte-faithful). B2 builds seat clones and verifier clones on
  exactly these two calls.

- [ ] **Step 1: Write the failing tests**

```python
import os
import subprocess
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared" / "lib"))
from forge import baseline, fleet, preflight  # noqa: E402
from test_forge_preflight import mk_repo, commit_file  # noqa: E402


def build(tmp_path, selected=()):
    r = mk_repo(tmp_path)
    commit_file(r, "plain.txt", b"hello\n")
    commit_file(r, "runme.sh", b"#!/bin/sh\n", mode=0o755)
    commit_file(r, "crlf.bin", b"line1\r\nline2\r\n")
    (r / "plain.txt").write_bytes(b"dirty edit\n")
    for s in selected:
        (r / s).write_text(f"sel {s}\n")
    run = tmp_path / "run"; run.mkdir()
    b = baseline.build_baseline(r, run, "m1", list(selected))
    return r, b


def test_clone_carries_the_dirty_baseline_not_head(tmp_path):
    r, b = build(tmp_path, selected=("parser.py",))
    dest = tmp_path / "seat"
    oid = fleet.materialize(r, b["ref"], dest)
    assert oid == b["b1"]
    assert (dest / "plain.txt").read_bytes() == b"dirty edit\n", \
        "clone got HEAD, not B1 — the --single-branch trap (spec §4.1)"
    assert (dest / "parser.py").read_text() == "sel parser.py\n"


def test_no_remote_exists(tmp_path):
    r, b = build(tmp_path)
    dest = tmp_path / "seat"
    fleet.materialize(r, b["ref"], dest)
    remotes = subprocess.run(["git", "-C", str(dest), "remote"],
                             capture_output=True, text=True).stdout.strip()
    assert remotes == "", "a push target exists — spec §4's H1 vector reopened"


def test_branch_mode_creates_named_branch(tmp_path):
    r, b = build(tmp_path)
    dest = tmp_path / "seat"
    fleet.materialize(r, b["ref"], dest, branch="forge/m1/claude")
    head = subprocess.run(["git", "-C", str(dest), "branch", "--show-current"],
                          capture_output=True, text=True).stdout.strip()
    assert head == "forge/m1/claude"


def test_fidelity_bytes_modes_crlf(tmp_path):
    r, b = build(tmp_path)
    dest = tmp_path / "seat"
    fleet.materialize(r, b["ref"], dest)
    assert (dest / "crlf.bin").read_bytes() == b"line1\r\nline2\r\n"
    assert os.access(dest / "runme.sh", os.X_OK)
    m = preflight.fs_manifest(r, ["runme.sh", "crlf.bin"])
    assert fleet.validate_manifest(dest, m) == []


def test_hostile_global_config_is_neutralised(tmp_path):
    # codex9: a global autocrlf/hooksPath must not alter the clone.
    r, b = build(tmp_path)
    fake_home = tmp_path / "ghome"; fake_home.mkdir()
    (fake_home / "gitconfig").write_text("[core]\n\tautocrlf = true\n")
    dest = tmp_path / "seat"
    env_backup = os.environ.get("GIT_CONFIG_GLOBAL")
    os.environ["GIT_CONFIG_GLOBAL"] = str(fake_home / "gitconfig")
    try:
        fleet.materialize(r, b["ref"], dest)
    finally:
        if env_backup is None:
            os.environ.pop("GIT_CONFIG_GLOBAL", None)
        else:
            os.environ["GIT_CONFIG_GLOBAL"] = env_backup
    assert (dest / "crlf.bin").read_bytes() == b"line1\r\nline2\r\n", \
        "global autocrlf rewrote bytes inside the clone (spec §4.1)"


def test_validate_manifest_reports_mismatch(tmp_path):
    r, b = build(tmp_path)
    dest = tmp_path / "seat"
    fleet.materialize(r, b["ref"], dest)
    (dest / "plain.txt").write_bytes(b"tampered\n")
    m = preflight.fs_manifest(r, ["plain.txt"])
    assert any("plain.txt" in x for x in fleet.validate_manifest(dest, m))
```

- [ ] **Step 2: Run — expect failures**, then **Step 3: Implement `shared/lib/forge/fleet.py`**

```python
"""Clone materialization (spec §4): init + explicit-refspec fetch, never `git clone`.

Why not clone: `git clone --single-branch <path>` follows the source's HEAD — it
fetches B₀ without the user's dirty state, and the synthetic ref under
refs/khenrix-forge/ is never transferred (round 3's most-likely-first-failure). And
`git clone` always writes remote.origin pointing at the user's repo, so every seat
would ship with a working push target. init+fetch has neither problem: the exact
refspec transfers exactly B₁, and no remote is ever configured.

All commands run under fleet_env(): global/system git config disabled, because an
empty template does not neutralise a global core.hooksPath or autocrlf (spec §4.1)."""
from __future__ import annotations

import hashlib
import os
import stat as stat_mod
from pathlib import Path

from .gitio import git, fleet_env


def materialize(repo, ref: str, dest: Path, *, branch: str | None = None) -> str:
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=False)
    env = fleet_env()
    git(dest, "init", "-q", env_extra=env)
    src = Path(git(repo, "rev-parse", "--show-toplevel").stdout.strip())
    git(dest, "fetch", "-q", f"file://{src}", f"+{ref}:refs/forge/base",
        "--no-tags", "--no-write-fetch-head", env_extra=env)
    oid = git(dest, "rev-parse", "refs/forge/base").stdout.strip()
    if branch:
        git(dest, "checkout", "-q", "-b", branch, oid, env_extra=env)
    else:
        git(dest, "-c", "advice.detachedHead=false", "checkout", "-q", "--detach", oid,
            env_extra=env)
    return oid


def validate_manifest(dest: Path, manifest: dict[str, dict]) -> list[str]:
    """Byte-fidelity check against preflight.fs_manifest output. Content hash + mode
    class + size ONLY — never mtime/inode (spec §7.3: lstat-keyed predicates report
    phantoms after any rmtree+copy)."""
    dest = Path(dest)
    probs = []
    for rel, want in manifest.items():
        p = dest / rel
        try:
            st = os.lstat(p)
        except FileNotFoundError:
            probs.append(f"{rel}: missing in clone")
            continue
        if stat_mod.S_ISLNK(st.st_mode):
            data, mode = os.readlink(p).encode(), "120000"
        else:
            data = p.read_bytes()
            mode = "100755" if st.st_mode & 0o100 else "100644"
        got = {"sha256": hashlib.sha256(data).hexdigest(), "mode": mode, "size": len(data)}
        if got != want:
            probs.append(f"{rel}: clone diverges from baseline manifest "
                         f"(want {want}, got {got})")
    return probs
```

- [ ] **Step 4: Run — all pass**, **Step 5: Commit**

```bash
git add shared/lib/forge/fleet.py tests/test_forge_fleet.py
git commit -m "feat(forge): exact-ref materialization, no-remote-by-construction, byte fidelity (B1 task 6)"
```

---

### Task 7: CLI, §19 leftovers, packaging, gates

**Files:**
- Create: `shared/lib/forge/__main__.py`
- Modify: `shared/lib/council/engine.py` (two §19 edits), `scripts/render.py` (one line),
  `Makefile` (FORGE_TESTS)
- Test: `tests/test_forge_cli.py`, extend `tests/test_council_seams.py`

**Interfaces:**
- Produces: `python3 -m forge preflight [--repo PATH] [--select PATH ...] [--json]`
  (exit 0 clean / 1 problems; read-only — B2 adds `start`/`collect`);
  council engine: `MODE_TIMEOUT == {"normal": 300, "deep": 1200, "forge": 3600}`;
  agy structured error text containing `"timeout waiting for response"` classifies as
  reason `"timeout"` (structured, retryable).

- [ ] **Step 1: Write the failing tests**

`tests/test_forge_cli.py`:

```python
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))
from test_forge_preflight import mk_repo, commit_file  # noqa: E402

ENV_LIB = str(ROOT / "shared" / "lib")


def run_cli(*args, cwd=None):
    import os
    env = {**os.environ, "PYTHONPATH": ENV_LIB}
    return subprocess.run([sys.executable, "-m", "forge", *args],
                          capture_output=True, text=True, env=env, cwd=cwd, timeout=120)


def test_preflight_clean_repo_exit_zero_json(tmp_path):
    r = mk_repo(tmp_path)
    commit_file(r, "a.txt")
    p = run_cli("preflight", "--repo", str(r), "--json")
    assert p.returncode == 0, p.stderr
    rep = json.loads(p.stdout)
    assert rep["problems"] == [] and rep["facts"]["head"]


def test_preflight_fail_closed_exit_one(tmp_path):
    r = mk_repo(tmp_path)
    (r / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(r), "add", "-N", "f.txt"], check=True)
    p = run_cli("preflight", "--repo", str(r), "--json")
    assert p.returncode == 1
    assert any("intent-to-add" in x for x in json.loads(p.stdout)["problems"])


def test_preflight_reports_untracked_candidates_and_secrets(tmp_path):
    r = mk_repo(tmp_path)
    commit_file(r, "a.txt")
    (r / "cand.py").write_text("x = 1\n")
    p = run_cli("preflight", "--repo", str(r), "--select", "cand.py", "--json")
    rep = json.loads(p.stdout)
    assert "cand.py" in rep["untracked_candidates"]
    assert rep["secret_screen"] == []
```

Append to `tests/test_council_seams.py`:

```python
def test_mode_timeout_has_forge_entry():
    f = import_fanout()
    assert f.MODE_TIMEOUT["forge"] >= 3600
    assert f.MODE_TIMEOUT["normal"] == 300 and f.MODE_TIMEOUT["deep"] == 1200


def test_agy_structured_timeout_maps_to_timeout_reason():
    import json as _json
    f = import_fanout()
    spec = f.ProviderSpec("agy", ["x"], None, f.extract_agy_json)
    deny = _json.dumps({"status": "ERROR", "error": "timeout waiting for response"})
    valid, reason, _text, structured = f.evaluate(1, deny, "", spec)
    assert (valid, reason, structured) == (False, "timeout", True)
    assert "timeout" not in f.STRUCTURED_TERMINAL_REASONS, \
        "adding timeout there would silently remove council's timeout retries (spec §19)"
```

- [ ] **Step 2: Run — expect failures**, then **Step 3: Implement**

`shared/lib/forge/__main__.py`:

```python
"""forge CLI. B1 ships `preflight` only — read-only, exit 0/1. B2 adds start/collect."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import preflight, secrets


def cmd_preflight(args) -> int:
    repo = Path(args.repo).resolve()
    facts = preflight.repo_facts(repo)
    selected = list(args.select or [])
    problems = preflight.fail_closed(repo, selected)
    candidates = preflight.untracked_candidates(repo)
    try:
        screen = secrets.scan_tree(Path(facts["toplevel"]),
                                   preflight._tracked(repo) + selected)
    except secrets.ScreenUnavailable as e:
        problems = problems + [f"secret-screen unavailable: {e} — fail closed (spec §3)"]
        screen = []
    report = {"facts": facts, "problems": problems, "secret_screen": screen,
              "untracked_candidates": candidates, "selected": selected}
    out = json.dumps(report, indent=1, sort_keys=True) if args.json else \
        "\n".join([f"repo: {facts['toplevel']}", f"head: {facts['head']}",
                   *(f"PROBLEM: {p}" for p in problems + screen),
                   *(f"candidate (untracked, selectable): {c}" for c in candidates)])
    print(out)
    return 1 if (problems or screen) else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="forge")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pf = sub.add_parser("preflight", help="read-only phase 1: facts, checks, candidates")
    pf.add_argument("--repo", default=".")
    pf.add_argument("--select", action="append", metavar="PATH")
    pf.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    return {"preflight": cmd_preflight}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
```

Council engine §19 edits (find by anchor, not line number):
1. `MODE_TIMEOUT = {"normal": 300, "deep": 1200}` → add `"forge": 3600` with the comment
   `# forge: builder seats wait on real setup/verify subprocesses (spec §19)`.
2. In `evaluate()`, the agy structured-error branch (anchor: `AGY_STRUCTURED_TOOL_PERMISSION`):
   BEFORE the tool-permission scan, add:

```python
        if "timeout waiting for response" in low:
            # agy self-reports its print-timeout in its own error field. Structured
            # provenance, reason "timeout" — retryable, because run_provider terminates
            # only on structured AND reason in STRUCTURED_TERMINAL_REASONS.
            # DO NOT add "timeout" to that set: it would silently remove council's
            # timeout retries (spec §19).
            return False, "timeout", result_text, True
```

(`low` already exists in that branch — verify, else lower `result_text` the way the
neighbouring code does.)

`scripts/render.py`: `SHARED_LIBS = ["wikisync", "council", "forge"]`.

`Makefile` (mirror the Plan A pattern exactly):
- `FORGE_TESTS := tests/test_forge_journal.py tests/test_forge_state.py tests/test_forge_preflight.py tests/test_forge_secrets.py tests/test_forge_baseline.py tests/test_forge_fleet.py tests/test_forge_cli.py`
- a `forge-test` target: `$(call RUN_PYTEST,-m "not slow" $(FORGE_TESTS))`
- add `forge-test` to `verify`'s prerequisites next to `council-test`, and to `test`.

- [ ] **Step 4: Run everything**

```
uvx pytest tests/test_forge_cli.py tests/test_council_seams.py -v -m "not slow"   # all pass
make forge-test    # exit 0
make council-test  # exit 0 — Plan A suites still green
make verify        # exit 0 (advisory receipt warnings for llm-council expected: engine.py changed)
```

- [ ] **Step 5: Render, reseed, gates, commit**

```bash
make render
python3 scripts/eval_harness.py --seed-receipt --skill llm-council   # engine.py edit staled it; scoped seed (F3!)
make precommit    # must exit 0
git add -A shared scripts/render.py Makefile tests evals/llm-council/receipt.json marketplaces
git commit -m "feat(forge): preflight CLI + packaging; council MODE_TIMEOUT[forge] + agy timeout mapping (B1 task 7)

The render.py SHARED_LIBS addition and engine.py §19 edits