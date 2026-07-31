# llm-forge Plan C: Launchable Seat + Harvest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the Plan B substrate into a seat that can actually do work and be harvested — a clone with its own identity, its own branch and a verified checkout, plus the four-phase inventory that says what the agent changed, separated from what setup and verification changed.

**Architecture:** Two new modules under `shared/lib/forge/` — `snapshot.py` (a filesystem inventory primitive) and `harvest.py` (the four-phase artifact set) — plus completion work on `fleet.py`. Everything stays hermetic: a stub "agent" is a shell command that writes files, so no provider ever runs and no token is spent.

**Tech Stack:** Python 3.11+ stdlib only. git 2.53. pytest via `uvx pytest` from the repo root.

**Spec:** `docs/superpowers/specs/2026-07-30-llm-forge-design.md` §4 (items 1, 2, 4, 6), §6.1, §7.1, §7.3, §7.4. Plan B (`2026-07-31-llm-forge-b-substrate.md`) shipped the substrate this consumes. **Deliberately out of scope:** verifier clones and the GeneratorContract fixed point (§6.2/§7.2), the journal and state machine (§14), review and ultrareview (§13), handover (§16), the skill and its evals (§18/§20).

## Global Constraints

- Python **stdlib-only**; must run on any Python 3.11+ machine with no install step.
- **The engine never mutates the user's repository** beyond objects and refs under `refs/khenrix-forge/<run-id>/`. Every read-only git call carries `GIT_OPTIONAL_LOCKS=0` (`gitcmd.READONLY`).
- **Ask git where things are — never string-join `.git`.** In a linked worktree `<repo>/.git` is a FILE. This exact bug shipped twice in Plan B (`baseline.py`, then silently again in `fleet.py`). Use `rev-parse --absolute-git-dir` for the git dir and `--git-common-dir` for shared state like `info/exclude`; they differ for a linked worktree.
- **Seats are fallible, not adversarial** (spec §1). Readiness-critical facts are recomputed by the trusted parent from primary evidence — never read back from anything a seat could have written.
- **Change detection is content hash + mode + size only.** Never mtime, ctime or inode: a build step that does rmtree-then-copy replaces every inode with byte-identical content, and an lstat-keyed predicate would report the whole tree as changed.
- Never edit anything under `marketplaces/` by hand. Run `make render` before every commit and include the regenerated files, or `make precommit`'s render-drift check fails.
- After every task: `uvx pytest tests/ -m "not slow" -q` stays green. Plan A and Plan B suites are untouched except where a task says otherwise.
- **Each implementer works in its own git worktree** (see Process below). Commit with an explicit pathspec, never a tree-wide `git add`.
- Commit messages end with the repo's standard trailer.

## Process: one worktree per implementer

Plan B lost time to two collisions between agents sharing this checkout. `make render` reads the *working tree*, so a concurrent source edit lands in another agent's rendered output even when their file sets are disjoint — pathspec-scoped commits contain the damage but do not prevent it. For this plan:

```bash
git worktree add --lock --reason "plan-C task N" ../forge-c-taskN HEAD
```

Work there, run the gates there, commit there, then `git worktree remove ../forge-c-taskN`. The controller merges nothing — commits land on `main` from the worktree directly, since it shares `.git`.

## File Structure

| Path | Responsibility |
|---|---|
| `shared/lib/forge/snapshot.py` | filesystem inventory: content-hash keyed, symlink-safe, quota-bounded |
| `shared/lib/forge/harvest.py` | the four-phase artifact set with origin labelling |
| `shared/lib/forge/fleet.py` | *(modified)* seat identity, branch, verified checkout |
| `shared/lib/forge/inspect.py` | *(modified)* `index_sha` gains a consumer |
| `shared/lib/forge/baseline.py` | *(modified)* honest `sidecars`, mid-snapshot drift check |
| `tests/test_forge_snapshot.py`, `tests/test_forge_harvest.py`, `tests/test_forge_seams.py` | new suites |

---

### Task 1: Close the carried follow-ups

**Files:**
- Modify: `shared/lib/forge/baseline.py`, `shared/lib/forge/inspect.py`, `shared/lib/forge/screen.py`, `shared/lib/forge/fleet.py`
- Test: `tests/test_forge_baseline.py`, `tests/test_forge_screen.py`, `tests/test_forge_fleet.py` (extend)

**Interfaces:**
- Consumes: everything Plan B shipped.
- Produces: `baseline.materialize` aborts on mid-snapshot drift; `Baseline.sidecars` is `None` until a producer exists; `screen_tree` reports absolute paths as breaches and counts what it skipped; `forge_child_env` raises `ForgeEnvError`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_forge_baseline.py  (append)
def test_materialize_aborts_when_the_index_moves_mid_snapshot(tmp_path, monkeypatch):
    """§2.2: 'abort if the source moved mid-snapshot'. index_sha had no consumer."""
    repo = make_repo(tmp_path)
    write(repo, "d.txt", "d\n")
    run = tmp_path / "run"; run.mkdir()
    f = finspect.repo_facts(repo)
    f = finspect.replace(f, index_sha="0" * 64)      # pretend the index differed at describe time
    with pytest.raises(baseline.BaselineError, match="moved"):
        baseline.materialize(repo, run, f, ["d.txt"], "r1")


def test_sidecars_is_none_until_a_producer_exists(tmp_path):
    """An empty list reads as 'there are none'; None reads as 'nobody looked'."""
    repo = make_repo(tmp_path)
    run = tmp_path / "run"; run.mkdir()
    b = _mk(repo, run)
    assert b.sidecars is None
```

```python
# tests/test_forge_screen.py  (append)
def test_absolute_selected_path_is_a_breach_not_a_crash(tmp_path):
    repo = make_repo(tmp_path)
    findings, breaches = screen.screen_tree(repo, ["/etc/hostname"])
    assert findings == [] and any("absolute" in b for b in breaches)


def test_skipped_count_is_reported_so_coverage_is_not_overstated(tmp_path):
    repo = make_repo(tmp_path)
    (Path(repo) / "link").symlink_to("seed.txt")
    findings, breaches = screen.screen_tree(repo, ["seed.txt", "link"])
    assert any("link" in b for b in breaches)
```

```python
# tests/test_forge_fleet.py  (append)
def test_forge_child_env_raises_a_named_error_on_a_bad_depth(tmp_path):
    repo = make_repo(tmp_path)
    with pytest.raises(fleet.ForgeEnvError, match="LLM_FORGE_DEPTH"):
        fleet.forge_child_env(repo, {"LLM_FORGE_DEPTH": "not-a-number"})
```

- [ ] **Step 2: Run to verify they fail**

Run: `uvx pytest tests/test_forge_baseline.py tests/test_forge_screen.py tests/test_forge_fleet.py -q`
Expected: 5 failures (`BaselineError` not raised; `sidecars == []`; `ValueError` from `relative_to`; no breach for the symlink; bare `ValueError`).

- [ ] **Step 3: Implement**

In `baseline.py`, change the `sidecars` field to `sidecars: list | None = None` and stop defaulting it to a list. Then, immediately after `repo = Path(facts.root)` is established and before any object is written, add the drift check:

```python
    # §2.2: the describe pass recorded the index hash; if it moved between then and now,
    # a concurrent editor or IDE wrote the index and the baseline would describe a tree
    # nobody asked for. index_sha had a producer and no consumer until this check.
    idx_now = _index_sha(repo)
    if facts.index_sha and idx_now and idx_now != facts.index_sha:
        raise BaselineError(
            "the repository index moved between preflight and baseline construction "
            f"({facts.index_sha[:12]} -> {idx_now[:12]}); re-run preflight")
```

with the helper, which asks git rather than joining `.git`:

```python
def _index_sha(repo) -> str:
    gd = Path(gitcmd.git(repo, "rev-parse", "--absolute-git-dir",
                         env_extra=gitcmd.READONLY).stdout.strip())
    idx = gd / "index"
    return hashlib.sha256(idx.read_bytes()).hexdigest() if idx.is_file() else ""
```

In `screen.py`, inside the selection loop, before `p = root / rel`:

```python
        if os.path.isabs(rel):
            breaches.append(f"{rel}: not screened — selection must be repo-relative")
            continue
```

and where a symlink or missing path is already turned into a breach, keep that behaviour — the tests above only pin it.

In `fleet.py`, add the error type and use it:

```python
class ForgeEnvError(RuntimeError):
    """The environment handed to a seat cannot be built."""
```

```python
    raw = out.get("LLM_FORGE_DEPTH", "0") or "0"
    try:
        cur = int(raw)
    except ValueError as e:
        raise ForgeEnvError(
            f"ambient LLM_FORGE_DEPTH is not a number: {raw!r}") from e
```

- [ ] **Step 4: Run to verify they pass**

Run: `uvx pytest tests/ -m "not slow" -q`
Expected: all pass. Any test asserting `b.sidecars == []` must be updated to `is None` — that is a contract change, not a weakened assertion.

- [ ] **Step 5: Render and commit**

```bash
make render
git add shared/lib/forge tests/test_forge_baseline.py tests/test_forge_screen.py tests/test_forge_fleet.py marketplaces
git commit -m "fix(forge): give index_sha a consumer, and stop empty lists claiming completeness"
```

---

### Task 2: A seat that can do its job

**Files:**
- Modify: `shared/lib/forge/fleet.py`
- Test: `tests/test_forge_fleet.py` (extend)

**Interfaces:**
- Consumes: `baseline.Baseline` (`.ref`, `.commit`, `.filesystem_manifest`), `gitcmd`.
- Produces:
  - `fleet.clone_seat(repo, baseline, dest, *, name, identity, template_dir=None) -> Seat`
    where `name` is the seat name (`claude`/`codex`/`agy`) and `identity` is `(author_name, author_email)`
  - `Seat` gains `branch: str` and `verified: bool`
  - `fleet.SeatError` — raised when the checkout does not match the baseline

The four §4 items ship together because a seat missing any one of them cannot be launched: no identity means it cannot commit, no branch means its work is unreachable, no verified checkout means nothing proved it got the right tree.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_forge_fleet.py  (append)
IDENT = ("Forge Seat", "seat@forge.invalid")


def test_seat_is_on_its_own_named_branch(tmp_path):
    repo = make_repo(tmp_path)
    run = tmp_path / "run"; run.mkdir()
    b = _mk_baseline(repo, run)
    seat = fleet.clone_seat(repo, b, tmp_path / "s1", name="claude", identity=IDENT)
    assert seat.branch == "forge/r1/claude"
    assert _git(seat.path, "rev-parse", "--abbrev-ref", "HEAD") == "forge/r1/claude"


def test_seat_can_commit_without_the_users_global_config(tmp_path):
    """A seat as shipped could not commit: no identity, and global config is disabled."""
    repo = make_repo(tmp_path)
    run = tmp_path / "run"; run.mkdir()
    seat = fleet.clone_seat(repo, _mk_baseline(repo, run), tmp_path / "s1",
                            name="codex", identity=IDENT)
    (seat.path / "work.txt").write_text("did work\n")
    r = subprocess.run(["git", "-C", str(seat.path), "add", "-A"],
                       capture_output=True, text=True,
                       env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull,
                            "GIT_CONFIG_SYSTEM": os.devnull})
    assert r.returncode == 0, r.stderr
    r = subprocess.run(["git", "-C", str(seat.path), "commit", "-qm", "seat work"],
                       capture_output=True, text=True,
                       env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull,
                            "GIT_CONFIG_SYSTEM": os.devnull})
    assert r.returncode == 0, r.stderr
    who = _git(seat.path, "log", "-1", "--format=%an <%ae>")
    assert who == "Forge Seat <seat@forge.invalid>"


def test_clone_seat_verifies_the_checkout_against_the_manifest(tmp_path):
    """The module must recompute this, not rely on a test doing it (spec §1, §4 item 1)."""
    repo = make_repo(tmp_path)
    write(repo, "d.txt", "dirty\n")
    run = tmp_path / "run"; run.mkdir()
    b = _mk_baseline(repo, run, selected=["d.txt"])
    seat = fleet.clone_seat(repo, b, tmp_path / "s1", name="agy", identity=IDENT)
    assert seat.verified is True


def test_clone_seat_raises_when_the_checkout_does_not_match(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    run = tmp_path / "run"; run.mkdir()
    b = _mk_baseline(repo, run)
    bad = baseline.Baseline(
        base_commit=b.base_commit, tracked_tree_oid=b.tracked_tree_oid,
        commit=b.commit, ref=b.ref, dirty=b.dirty, sidecars=None,
        filesystem_manifest={**b.filesystem_manifest, "seed.txt": "0" * 64})
    with pytest.raises(fleet.SeatError, match="seed.txt"):
        fleet.clone_seat(repo, bad, tmp_path / "s1", name="claude", identity=IDENT)
```

- [ ] **Step 2: Run to verify they fail**

Run: `uvx pytest tests/test_forge_fleet.py -q`
Expected: 4 failures — `clone_seat()` got an unexpected keyword `name`.

- [ ] **Step 3: Implement**

Extend the `Seat` dataclass with `branch: str = ""` and `verified: bool = False`, add the error type, and change `clone_seat`'s signature to `(repo, baseline, dest, *, name, identity, template_dir=None)`. The run id comes from the ref: `baseline.ref` is `refs/khenrix-forge/<run-id>/base`, so `run_id = baseline.ref.split("/")[2]`.

After the clone and the origin removal, before returning:

```python
    # §4 item 6: the seat gets its own branch. --revision leaves a detached HEAD, so the
    # seat's work would be unreachable the moment anything else moved.
    branch = f"forge/{run_id}/{name}"
    gitcmd.git(dest, "checkout", "-q", "-b", branch, env_extra=env)

    # §4 item 2: identity, written into the clone's LOCAL config. Without this a seat
    # cannot commit at all — global config is disabled by design (§4.2), so git would
    # fail with "Please tell me who you are".
    gitcmd.git(dest, "config", "user.name", identity[0], env_extra=env)
    gitcmd.git(dest, "config", "user.email", identity[1], env_extra=env)

    # §4 item 1 + §1: the trusted parent recomputes this from primary evidence. Until
    # now the TEST asserted HEAD == B1 and the module asserted nothing.
    head = gitcmd.git(dest, "rev-parse", "HEAD", env_extra=env).stdout.strip()
    if head != baseline.commit:
        raise SeatError(f"seat checked out {head[:12]}, expected {baseline.commit[:12]}")
    for rel, want in (baseline.filesystem_manifest or {}).items():
        p = dest / rel
        if not p.is_file() or p.is_symlink():
            continue          # symlinks and absent paths are the manifest's own gap (F6)
        got = _sha256_file(p)
        if got != want:
            raise SeatError(f"seat content differs from the baseline manifest: {rel}")
    verified = True
```

and return `Seat(path=dest, branch=branch, verified=verified, replayed=tuple(replayed))`, preserving whatever fields `Seat` already carries. Add `_sha256_file` mirroring `baseline.py`'s.

- [ ] **Step 4: Run to verify they pass**

Run: `uvx pytest tests/test_forge_fleet.py -q` → all pass.
Run: `uvx pytest tests/ -m "not slow" -q` → all pass. Existing `clone_seat` callers in the suite need the two new keyword arguments; update them.

- [ ] **Step 5: Render and commit**

```bash
make render
git add shared/lib/forge/fleet.py tests/test_forge_fleet.py marketplaces
git commit -m "feat(forge): a seat with its own branch, identity and a verified checkout"
```

---

### Task 3: The snapshot primitive

**Files:**
- Create: `shared/lib/forge/snapshot.py`, `tests/test_forge_snapshot.py`

**Interfaces:**
- Consumes: `storage.Quota`.
- Produces:
  - `snapshot.Entry` — frozen dataclass `path: str`, `digest: str`, `mode: int`, `size: int`, `kind: str` (`"file"`/`"symlink"`/`"dir"`)
  - `snapshot.take(root, *, quota=None, skip_dirs=(".git",)) -> tuple[dict[str, Entry], list[str]]` — returns `(entries, breaches)`
  - `snapshot.diff(before, after) -> dict[str, str]` — path → `"added"`/`"removed"`/`"modified"`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_forge_snapshot.py
"""Filesystem inventory keyed on CONTENT, never on lstat (spec §7.3)."""
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

from forge import snapshot, storage  # noqa: E402
from forge_fixtures import make_repo, write  # noqa: E402


def test_rmtree_and_copy_produces_no_phantom_changes(tmp_path):
    """The bug this predicate exists to prevent: new inodes, identical bytes."""
    src = tmp_path / "t"; src.mkdir()
    (src / "a.txt").write_text("a\n")
    (src / "sub").mkdir(); (src / "sub" / "b.txt").write_text("b\n")
    before, _ = snapshot.take(src)
    copy = tmp_path / "copy"
    shutil.copytree(src, copy)
    shutil.rmtree(src)
    shutil.copytree(copy, src)
    after, _ = snapshot.take(src)
    assert snapshot.diff(before, after) == {}


def test_detects_added_removed_modified(tmp_path):
    d = tmp_path / "t"; d.mkdir()
    (d / "keep.txt").write_text("k\n")
    (d / "gone.txt").write_text("g\n")
    (d / "edit.txt").write_text("v1\n")
    before, _ = snapshot.take(d)
    (d / "gone.txt").unlink()
    (d / "edit.txt").write_text("v2\n")
    (d / "new.txt").write_text("n\n")
    after, _ = snapshot.take(d)
    assert snapshot.diff(before, after) == {
        "gone.txt": "removed", "edit.txt": "modified", "new.txt": "added"}


def test_mode_change_alone_is_a_modification(tmp_path):
    d = tmp_path / "t"; d.mkdir()
    p = d / "s.sh"; p.write_text("#!/bin/sh\n"); p.chmod(0o644)
    before, _ = snapshot.take(d)
    p.chmod(0o755)
    after, _ = snapshot.take(d)
    assert snapshot.diff(before, after) == {"s.sh": "modified"}


def test_symlinks_are_recorded_but_never_followed(tmp_path):
    outside = tmp_path / "outside"; outside.mkdir()
    (outside / "secret.txt").write_text("secret\n")
    d = tmp_path / "t"; d.mkdir()
    (d / "link").symlink_to(outside)
    entries, _ = snapshot.take(d)
    assert entries["link"].kind == "symlink"
    assert not any(e.startswith("link/") for e in entries)


def test_git_is_skipped_by_default(tmp_path):
    repo = make_repo(tmp_path)
    entries, _ = snapshot.take(repo)
    assert not any(p.startswith(".git/") for p in entries)
    assert "seed.txt" in entries


def test_quota_breach_fails_closed_with_no_partial_inventory(tmp_path):
    d = tmp_path / "t"; d.mkdir()
    for i in range(5):
        (d / f"f{i}.txt").write_text("x\n")
    entries, breaches = snapshot.take(d, quota=storage.Quota(
        max_files=2, max_file_bytes=1000, max_total_bytes=10_000))
    assert entries == {} and breaches and "files" in breaches[0]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uvx pytest tests/test_forge_snapshot.py -q`
Expected: `ModuleNotFoundError: No module named 'forge.snapshot'`.

- [ ] **Step 3: Implement**

```python
# shared/lib/forge/snapshot.py
"""Filesystem inventory for the harvest (spec §7.3).

The change predicate is CONTENT HASH + MODE + SIZE. Never mtime, ctime or inode: this
repo's own `render` step does rmtree-then-copytree, which replaces every inode with
byte-identical content — an lstat-keyed predicate reports the whole rendered tree as
changed and the generator fixed point never converges.

Symlinks are recorded by their target and never followed: following them would walk out
of the tree and could cycle.
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


def take(root, *, quota: Quota = None, skip_dirs=(".git",)):
    """Inventory `root`. Returns (entries, breaches); a breach means FAIL CLOSED — the
    entries dict is empty rather than a partial inventory reported as complete."""
    quota = quota or Quota.default()
    root = Path(root)
    entries, total, count = {}, 0, 0

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        here = Path(dirpath)
        # A symlink to a directory arrives in dirnames; record it and do not descend.
        for d in list(dirnames):
            p = here / d
            if p.is_symlink():
                dirnames.remove(d)
                rel = str(p.relative_to(root))
                entries[rel] = Entry(rel, hashlib.sha256(
                    os.readlink(p).encode()).hexdigest(), 0, 0, "symlink")
        for name in filenames:
            p = here / name
            rel = str(p.relative_to(root))
            count += 1
            if (b := quota.breach(files=count, file_bytes=0, total_bytes=total)):
                return {}, [b]
            if p.is_symlink():
                entries[rel] = Entry(rel, hashlib.sha256(
                    os.readlink(p).encode()).hexdigest(), 0, 0, "symlink")
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
```

- [ ] **Step 4: Run to verify it passes**

Run: `uvx pytest tests/test_forge_snapshot.py -q`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add shared/lib/forge/snapshot.py tests/test_forge_snapshot.py
git commit -m "feat(forge): content-keyed filesystem snapshot for the harvest"
```

---

### Task 4: The four-phase harvest

**Files:**
- Create: `shared/lib/forge/harvest.py`, `tests/test_forge_harvest.py`

**Interfaces:**
- Consumes: `snapshot.take`/`diff`, `gitcmd`, `baseline.Baseline`, `fleet.Seat`.
- Produces:
  - `harvest.Phases` — frozen dataclass holding the four inventories `f0`, `fsetup`, `fwork`, `fverify` (each a `dict[str, Entry]`)
  - `harvest.record(seat_path, *, quota=None) -> dict[str, Entry]` — one inventory
  - `harvest.artifact_set(phases, seat_path, baseline_commit) -> ArtifactSet`
  - `harvest.ArtifactSet` — `paths: tuple[str, ...]`, `origin: dict[str, str]` (path → `setup`/`builder`/`verify`), `setup_overlap: tuple[str, ...]`, `tracked_diff: str`

The artifact **path set** comes from `Fsetup → Fwork`; the **content** comes from `git diff <B> <final>` over those paths. Those two rules disagreed in the design until they were made to compose, and this is where they compose.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_forge_harvest.py
"""Origin is provenance, not eligibility (spec §7.1)."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

from forge import harvest  # noqa: E402
from forge_fixtures import make_repo, write  # noqa: E402


def _phases(seat, setup, work, verify):
    """Run three stub 'commands' around the three inventory points."""
    f0 = harvest.record(seat)
    setup()
    fsetup = harvest.record(seat)
    work()
    fwork = harvest.record(seat)
    verify()
    fverify = harvest.record(seat)
    return harvest.Phases(f0=f0, fsetup=fsetup, fwork=fwork, fverify=fverify)


def test_setup_output_is_not_in_the_artifact_set(tmp_path):
    """npm ci / uv sync create thousands of files; they are not the agent's work."""
    seat = make_repo(tmp_path, "seat")
    base = subprocess.run(["git", "-C", str(seat), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    p = _phases(seat,
                setup=lambda: (Path(seat) / "node_modules").mkdir() or
                              (Path(seat) / "node_modules" / "x.js").write_text("dep\n"),
                work=lambda: write(seat, "src.py", "the agent's work\n"),
                verify=lambda: write(seat, "report.txt", "verify output\n"))
    a = harvest.artifact_set(p, seat, base)
    assert "src.py" in a.paths
    assert "node_modules/x.js" not in a.paths
    assert "report.txt" not in a.paths
    assert a.origin["src.py"] == "builder"


def test_a_path_touched_by_setup_and_the_agent_is_flagged_overlap(tmp_path):
    seat = make_repo(tmp_path, "seat")
    base = subprocess.run(["git", "-C", str(seat), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    p = _phases(seat,
                setup=lambda: write(seat, "lock.txt", "v1\n"),
                work=lambda: write(seat, "lock.txt", "v2\n"),
                verify=lambda: None)
    a = harvest.artifact_set(p, seat, base)
    assert "lock.txt" in a.paths
    assert "lock.txt" in a.setup_overlap


def test_verify_origin_is_recorded_not_discarded(tmp_path):
    """§7.2: a verify-origin change can be a REQUIRED deliverable, so label it."""
    seat = make_repo(tmp_path, "seat")
    base = subprocess.run(["git", "-C", str(seat), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    p = _phases(seat, setup=lambda: None,
                work=lambda: write(seat, "src.py", "work\n"),
                verify=lambda: write(seat, "generated.txt", "rendered\n"))
    a = harvest.artifact_set(p, seat, base)
    assert a.origin.get("generated.txt") == "verify"
    assert "generated.txt" not in a.paths


def test_tracked_content_comes_from_the_baseline_not_the_seat_head(tmp_path):
    """A seat that commits would show an empty `git diff` against its own HEAD."""
    seat = make_repo(tmp_path, "seat")
    base = subprocess.run(["git", "-C", str(seat), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    p = _phases(seat, setup=lambda: None,
                work=lambda: (write(seat, "seed.txt", "changed by agent\n"),
                              subprocess.run(["git", "-C", str(seat), "commit", "-aqm", "w"],
                                             check=True)),
                verify=lambda: None)
    a = harvest.artifact_set(p, seat, base)
    assert "seed.txt" in a.paths
    assert "changed by agent" in a.tracked_diff
```

- [ ] **Step 2: Run to verify it fails**

Run: `uvx pytest tests/test_forge_harvest.py -q`
Expected: `ModuleNotFoundError: No module named 'forge.harvest'`.

- [ ] **Step 3: Implement**

```python
# shared/lib/forge/harvest.py
"""The four-phase artifact set (spec §6.1, §7.1).

Origin is PROVENANCE, not eligibility. Four inventories are taken — F0 (baseline
checkout), Fsetup (after the engine's setup), Fwork (after the agent exits), Fverify
(after the engine's verify) — and every changed path carries the phase that produced it.

The artifact PATH set is Fsetup -> Fwork: setup's output (node_modules, .venv) is not the
agent's work, and verify's output is recorded separately because §7.2 may admit it as a
required deliverable under a declared generator contract — that decision belongs to a
later plan, and discarding it here would foreclose it.

The artifact CONTENT is `git diff <B> <final>` over those paths, against the PINNED
baseline commit — never the seat's own HEAD, which a seat that commits would leave empty.
"""
from dataclasses import dataclass, field

from . import gitcmd, snapshot


@dataclass(frozen=True)
class Phases:
    f0: dict
    fsetup: dict
    fwork: dict
    fverify: dict


@dataclass(frozen=True)
class ArtifactSet:
    paths: tuple = ()
    origin: dict = field(default_factory=dict)
    setup_overlap: tuple = ()
    tracked_diff: str = ""


def record(seat_path, *, quota=None) -> dict:
    """One inventory of a seat, .git excluded."""
    entries, breaches = snapshot.take(seat_path, quota=quota)
    if breaches:
        raise RuntimeError("; ".join(breaches))
    return entries


def artifact_set(phases: Phases, seat_path, baseline_commit: str) -> ArtifactSet:
    setup_changes = snapshot.diff(phases.f0, phases.fsetup)
    work_changes = snapshot.diff(phases.fsetup, phases.fwork)
    verify_changes = snapshot.diff(phases.fwork, phases.fverify)

    origin = {}
    for p in setup_changes:
        origin[p] = "setup"
    for p in work_changes:
        origin[p] = "builder"
    for p in verify_changes:
        origin.setdefault(p, "verify")

    paths = tuple(sorted(work_changes))
    overlap = tuple(sorted(set(work_changes) & set(setup_changes)))

    diff_text = ""
    if paths:
        diff_text = gitcmd.git(
            seat_path, "diff", baseline_commit, "--", *paths,
            env_extra=gitcmd.READONLY, check=False).stdout
    return ArtifactSet(paths=paths, origin=origin,
                       setup_overlap=overlap, tracked_diff=diff_text)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uvx pytest tests/test_forge_harvest.py -q`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add shared/lib/forge/harvest.py tests/test_forge_harvest.py
git commit -m "feat(forge): four-phase harvest with origin as provenance"
```

---

### Task 5: Cross-seam assertions, packaging, gates

**Files:**
- Create: `tests/test_forge_seams.py`
- Modify: `Makefile` (extend `FORGE_TESTS`), `scripts/lib/checks.py` if the closure needs the new modules

**Interfaces:**
- Consumes: every module.
- Produces: the assertions Plan B structurally lacked — properties that hold *across* module boundaries.

Plan B's whole-branch review found that no test asserted a property across a seam: every suite exercised one module while others appeared only as feeders. Three real defects lived in exactly those gaps. This task closes the pattern, not just the instances.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_forge_seams.py
"""Properties that must hold ACROSS module boundaries.

Plan B's suites each exercised one module, with others present only as fixtures. Three
shipped defects lived in those seams: the manifest missed a selected directory's
contents, the seat's exclude replay silently no-opped in a linked worktree, and the
seat's environment re-admitted the redirectors gitcmd strips. Each is one assertion here.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

from forge import baseline, fleet, gitcmd, inspect as finspect, screen  # noqa: E402
from forge_fixtures import make_repo, write  # noqa: E402

IDENT = ("Forge Seat", "seat@forge.invalid")


def _baseline(repo, run, selected=()):
    f = finspect.repo_facts(repo)
    return baseline.materialize(repo, run, f, list(selected), "r1")


def test_everything_in_the_tree_is_in_the_manifest(tmp_path):
    """SEAM: baseline's tree vs its own manifest. A selected directory broke this."""
    repo = make_repo(tmp_path)
    write(repo, "scratch/a.txt", "a\n")
    write(repo, "scratch/sub/b.txt", "b\n")
    run = tmp_path / "run"; run.mkdir()
    b = _baseline(repo, run, selected=["scratch"])
    tree = set(gitcmd.git(repo, "ls-tree", "-r", "--name-only", b.tracked_tree_oid,
                          env_extra=gitcmd.READONLY).stdout.split())
    missing = tree - set(b.filesystem_manifest)
    assert missing == set(), f"in the tree but not the manifest: {sorted(missing)}"


def test_everything_the_manifest_names_is_screenable(tmp_path):
    """SEAM: baseline's manifest vs screen's selection contract."""
    repo = make_repo(tmp_path)
    write(repo, "cfg.py", "ok\n")
    run = tmp_path / "run"; run.mkdir()
    b = _baseline(repo, run, selected=["cfg.py"])
    findings, breaches = screen.screen_tree(repo, sorted(b.filesystem_manifest))
    assert breaches == [], f"the screen could not read what the baseline recorded: {breaches}"


def test_the_seat_checkout_matches_the_baseline_manifest(tmp_path):
    """SEAM: baseline's manifest vs the seat fleet builds from it."""
    repo = make_repo(tmp_path)
    write(repo, "d.txt", "dirty\n")
    run = tmp_path / "run"; run.mkdir()
    b = _baseline(repo, run, selected=["d.txt"])
    seat = fleet.clone_seat(repo, b, tmp_path / "s1", name="claude", identity=IDENT)
    for rel, want in b.filesystem_manifest.items():
        p = seat.path / rel
        if p.is_file() and not p.is_symlink():
            import hashlib
            got = hashlib.sha256(p.read_bytes()).hexdigest()
            assert got == want, f"seat content differs from the baseline: {rel}"


def test_a_linked_worktree_survives_the_whole_chain(tmp_path):
    """SEAM: the .git-as-file bug shipped TWICE in Plan B. Pin the chain, not one module."""
    repo = make_repo(tmp_path)
    wt = tmp_path / "wt"
    gitcmd.git(repo, "worktree", "add", "-q", "--detach", str(wt), "HEAD")
    excl = Path(gitcmd.git(repo, "rev-parse", "--git-common-dir",
                           env_extra=gitcmd.READONLY).stdout.strip()) / "info" / "exclude"
    excl.parent.mkdir(parents=True, exist_ok=True)
    excl.write_text("scratch/\n")
    write(wt, "d.txt", "dirty\n")
    run = tmp_path / "run"; run.mkdir()
    f = finspect.repo_facts(wt)
    b = baseline.materialize(wt, run, f, ["d.txt"], "r2")
    seat = fleet.clone_seat(wt, b, tmp_path / "s1", name="codex", identity=IDENT)
    assert "info/exclude" in seat.replayed
    assert (seat.path / ".git" / "info" / "exclude").read_text() == "scratch/\n"


def test_the_seat_environment_admits_no_git_redirector(tmp_path):
    """SEAM: gitcmd strips these for engine calls; the seat env must not re-admit them."""
    repo = make_repo(tmp_path)
    hostile = {"GIT_DIR": "/elsewhere/.git", "GIT_WORK_TREE": "/elsewhere",
               "GIT_ALTERNATE_OBJECT_DIRECTORIES": "/elsewhere/objects",
               "GIT_CONFIG_COUNT": "1", "PATH": "/usr/bin"}
    out = fleet.forge_child_env(repo, hostile)
    for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
              "GIT_CONFIG_COUNT"):
        assert k not in out, f"{k} reached the seat"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uvx pytest tests/test_forge_seams.py -q`
Expected: failures until Tasks 1–4 are complete; if all four are done, the manifest-vs-tree and linked-worktree tests should already pass and the rest confirm.

- [ ] **Step 3: Wire the gate**

In `Makefile`, extend `FORGE_TESTS` with the three new suites:

```make
               tests/test_forge_snapshot.py tests/test_forge_harvest.py \
               tests/test_forge_seams.py
```

Check whether `scripts/lib/checks.py`'s `SKILL_EXTRA_DIRS["llm-forge"]` already covers the new modules — it names `shared/lib/forge` as a directory, so `snapshot.py` and `harvest.py` are included automatically. Verify with `checks.source_manifest(ROOT, "llm-forge")` rather than assuming.

- [ ] **Step 4: Run the gates**

Run: `make render`
Run: `uvx pytest tests/ -m "not slow" -q` → all pass
Run: `make council-test`; `echo $?` → 0, with all nine forge suites collected
Run: `make verify`; `echo $?` → 0
Run: `make precommit`; `echo $?` → 0. If receipts stale, **verify the cause first** (`render.py` and `checks.py` are the only global inputs), then reseed **scoped**: `python3 scripts/eval_harness.py --seed-receipt --skill <name>`. Never an unscoped reseed — it destroys any real `provenance: "eval"` receipt.

- [ ] **Step 5: Commit**

```bash
git add tests/test_forge_seams.py Makefile marketplaces
git commit -m "test(forge): assert the properties that live between modules"
```

---

## Self-review

**Spec coverage.** §4 items 1, 2, 4 and 6 → Task 2 (verified checkout, identity, exclude recording from Plan B's fix, own branch), shipped together because a seat missing any one cannot be launched. §7.3 content-keyed change detection → Task 3. §6.1 four inventories and §7.1 path-set-vs-content → Task 4. Carried follow-ups F1, F2, F3, F7 and the screen's skipped-count → Task 1. The structural finding — no cross-seam assertions — → Task 5.

**Deliberately out of scope**, each with a later home: verifier clones and the GeneratorContract fixed point (§6.2/§7.2 — Plan D), the journal and state machine (§14 — Plan E), review and ultrareview (§13), handover (§16), the skill and evals (§18/§20). Nothing here launches a provider or spends a token; the "agent" in every harvest test is a lambda that writes a file.

**Placeholder scan.** None. Two adaptation points are named explicitly rather than left vague: Task 2's `Seat` field preservation (read the current dataclass before extending it) and Task 5's closure check (verify with `source_manifest`, do not assume).

**Type consistency.** `Seat.path`/`.branch`/`.verified`/`.replayed` are used identically in Tasks 2 and 5; `snapshot.Entry`'s five fields are consumed by `snapshot.diff` and `harvest.artifact_set` under the same names; `Phases.f0/fsetup/fwork/fverify` match between the dataclass, the test helper and `artifact_set`; `baseline.Baseline.sidecars` becomes `None` in Task 1 and is passed as `None` in Task 2's test.

**One risk worth naming.** Task 4's `artifact_set` calls `git diff <B> -- <paths>` with `check=False`, so a seat whose paths are all untracked yields an empty `tracked_diff` and no error. That is correct — untracked artifacts have no tracked diff — but it means a git failure and a legitimately-empty diff are indistinguishable. Plan D's verifier clones will need to tell them apart.
