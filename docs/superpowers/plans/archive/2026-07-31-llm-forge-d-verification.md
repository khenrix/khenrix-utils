# llm-forge Plan D: Verification in a Clone the Builder Never Touched

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a seat's verdict mean something. Today `clone_seat` proves a seat *started* from the right tree and `harvest` says what it changed — but nothing checks that the result builds, and any check run inside the seat is a check the builder could have rigged. This plan materializes each candidate into a fresh clone the builder never had access to, runs the confirmed setup and verify commands there, and classifies the outcome honestly.

**Architecture:** Two new modules — `bundle.py` (the versioned `CandidateBundle`: exactly what crosses from a seat into a verifier) and `verify.py` (verifier-clone construction, the engine-run gate, the outcome classification and the generator fixed point) — plus a `GeneratorContract` recorded at preflight in `inspect.py`. Everything stays hermetic: the "verify command" in every test is a shell command in a fixture repo, so no provider runs and no token is spent.

**Tech Stack:** Python 3.11+ stdlib only. git 2.53. pytest via `uvx pytest` from the repo root.

**Spec:** `docs/superpowers/specs/2026-07-30-llm-forge-design.md` §6 (verification), §6.1 (gate surface), §6.2 (verify outcomes), §7.2 (the generator fixed point). Plan C shipped the seat and harvest this consumes. **Deliberately out of scope:** the journal and state machine (§14), review and ultrareview (§13), handover (§16), the skill and its evals (§18/§20), and the strategy/fallback rules (§12).

## Global Constraints

- Python **stdlib-only**; must run on any Python 3.11+ machine with no install step.
- **A check the builder could have rigged is not a check.** Every verification runs in a clone built from the baseline, into which the harvested candidate is materialized — never in the seat. A seat can replace `.venv/bin/pytest`, delete an auto-discovered test file, or weaken the Makefile; none of those follow the candidate across.
- **Ask git where things are — never string-join `.git`.** `<repo>/.git` is a FILE in a linked worktree. This bug has shipped three times in this project; `--absolute-git-dir` for per-worktree state, `--git-common-dir` for shared state.
- **Commands run as argv lists, never through a shell.** A confirmed verify command is a sequence of `{argv, cwd, env, timeout}` steps; shell metacharacter syntax is rejected, not reinterpreted.
- **Fail closed, and never report a partial result as complete.** Every outcome in §6.2's table is explicit; "exit 0" alone is not `PASS`.
- Never edit anything under `marketplaces/` by hand — run `make render` and commit the regenerated files.
- After every task: `uvx pytest tests/ -m "not slow" -q` stays green; Plans A–C suites are untouched except where a task says otherwise.
- Implementers run **strictly sequentially** — never two in flight. `make render` reads the working tree, so a concurrent source edit lands in another agent's rendered output even when file sets are disjoint. Commit with an explicit pathspec, never a tree-wide `git add`.
- Commit messages end with the repo's standard trailer.

## What Plan C hands you, verbatim

Verify these against the code before relying on them — every plan in this project has had draft code that was wrong, including four cases where an instruction I gave was insufficient or backwards.

- `harvest.ArtifactSet(paths, origin, setup_overlap, tracked_diff, verify_overlap)` — note `verify_overlap` is **last**, deliberately, so the first four keep positional compatibility.
- **`tracked_diff` is a PARTIAL view of `paths`, for three causes**: untracked content, binary/`-diff` files, and submodule content. Do not write code assuming it covers `paths`.
- `tracked_diff` is decoded with `surrogateescape`; re-encoding requires `.encode("utf-8", "surrogateescape")`.
- `harvest.record(seat_path, *, quota=None)`, `harvest.Phases(f0, fsetup, fwork, fverify)`, `harvest.HarvestError`. Default quota for a seat is `storage.Quota.for_harvest()`, not `.default()` — the screen's cap is a different question and is too small for a seat.
- `snapshot.Entry.kind` emits only `"file"`, `"symlink"`, `"special"`; `"dir"` is reserved and never produced, so **empty directories are invisible to `diff()`**. Only `"file"` carries a content digest; a symlink's `mode` is a fabricated `0` and its digest hashes the target text. `snapshot.take` raises `SnapshotError`.
- `fleet.clone_seat(repo, baseline, dest, *, name, identity, template_dir=None) -> Seat`, with `Seat.path/.branch/.verified/.replayed` and `SeatError`. `Seat.branch` and `.verified` default to the un-launchable state on purpose.
- `baseline.Baseline(base_commit, tracked_tree_oid, commit, ref, dirty, sidecars, filesystem_manifest)`; `sidecars` is `None` — "nobody looked", not "there are none".
- `gitcmd.git(repo, *args, env_extra=None, check=True, binary=False, timeout=60)`, `READONLY`, `NO_DAEMON_CACHE`, `HOSTILE_ENV`, `REDIRECTING_ENV`, `NO_USER_CONFIG`, `zero_oid`; `GitError`. **`HOSTILE_ENV` is the list any child environment strips** — `REDIRECTING_ENV` is the strictly narrower subset it is built from (redirection only), and reaching for it instead leaves `GIT_CONFIG_COUNT`, `GIT_CONFIG_PARAMETERS` and `GIT_TEMPLATE_DIR` ambient.

## Two open decisions this plan must settle

**D-1 — the tracked symlink.** A tracked symlink enters `baseline.filesystem_manifest` carrying its *target's content* digest (the `ls-files` loop guards on `is_file()`, which follows links); `screen` breaches on it and `snapshot` digests it differently (target text). `fleet.py` skips symlinks during seat verification *precisely because* baseline hashes through them, and `tests/test_forge_fleet.py` asserts that as an explicit precondition. Plan C pinned this as a characterization test naming the fix site rather than changing two modules under a commit whose deliverable was assertions. **Task 1 settles it**, because the bundle is where a symlink's identity finally has to be decided.

**D-2 — symlink noise.** Plan C's Critical fix made `screen` breach on *any* symlink under a selection, not only escaping ones, to match the top-level rule. A repo with an ordinary in-tree `docs/latest -> v2` now fails closed where it passed. **Task 1 decides** whether the escape-vs-internal discrimination lives in `screen` or in the caller, and pins whichever it chooses.

## File Structure

| Path | Responsibility |
|---|---|
| `shared/lib/forge/bundle.py` | the versioned `CandidateBundle`: build from a seat, materialize into a clone |
| `shared/lib/forge/verify.py` | verifier clones, engine-run gate, outcome classification, generator fixed point |
| `shared/lib/forge/inspect.py` | *(modified)* `GeneratorContract` detection at preflight |
| `tests/test_forge_bundle.py`, `tests/test_forge_verify.py`, `tests/test_forge_seams.py` | new + extended |

---

### Task 1: The CandidateBundle

**Files:**
- Create: `shared/lib/forge/bundle.py`, `tests/test_forge_bundle.py`
- Modify: `shared/lib/forge/screen.py` (only if D-2 lands there)

**Interfaces:**
- Consumes: `harvest.ArtifactSet`, `baseline.Baseline`, `snapshot.take/Entry`, `gitcmd`.
- Produces:
  - `bundle.CandidateBundle` — frozen: `version: int`, `baseline_ref: str`, `baseline_commit: str`, `tracked_patch: bytes`, `sidecars: tuple[SidecarEntry, ...]`, `gate_delta: tuple[str, ...]`, `generator_contract_id: str`, `omitted: tuple[str, ...]`
  - `bundle.SidecarEntry` — frozen: `path: str`, `kind: str`, `mode: int`, `payload: bytes`
  - `bundle.build(seat_path, artifacts, baseline) -> CandidateBundle`
  - `bundle.materialize(bundle, dest) -> tuple[str, ...]` — returns the paths written
  - `bundle.BundleError`

`omitted` is the field that keeps this honest: every path in `artifacts.paths` that the bundle cannot carry is named there, so a verifier failure caused by a missing input is distinguishable from a candidate defect.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_forge_bundle.py
"""What crosses from a seat into a verifier — and what provably does not."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

import pytest  # noqa: E402
from forge import baseline, bundle, fleet, harvest, inspect as finspect  # noqa: E402
from forge_fixtures import make_repo, write, git  # noqa: E402

IDENT = ("Forge Seat", "seat@forge.invalid")


def _seat(tmp_path, selected=(), name="claude"):
    repo = make_repo(tmp_path)
    run = tmp_path / "run"; run.mkdir()
    f = finspect.repo_facts(repo)
    b = baseline.materialize(repo, run, f, list(selected), "r1")
    s = fleet.clone_seat(repo, b, tmp_path / name, name=name, identity=IDENT)
    return repo, b, s


def _phases(seat, work):
    f0 = harvest.record(seat)
    fsetup = harvest.record(seat)
    work()
    fwork = harvest.record(seat)
    return harvest.Phases(f0=f0, fsetup=fsetup, fwork=fwork, fverify=harvest.record(seat))


def test_a_tracked_edit_crosses_and_applies(tmp_path):
    repo, b, s = _seat(tmp_path)
    p = _phases(s.path, lambda: write(s.path, "seed.txt", "agent edit\n"))
    a = harvest.artifact_set(p, s.path, b.commit)
    cb = bundle.build(s.path, a, b)
    dest = tmp_path / "verifier"
    fleet.clone_seat(repo, b, dest, name="verifier", identity=IDENT)
    written = bundle.materialize(cb, dest)
    assert "seed.txt" in written
    assert (dest / "seed.txt").read_text() == "agent edit\n"


def test_an_untracked_file_crosses_as_a_sidecar_not_a_patch(tmp_path):
    """`tracked_diff` is a partial view of `paths` — untracked is one of its three holes."""
    repo, b, s = _seat(tmp_path)
    p = _phases(s.path, lambda: write(s.path, "new.py", "print('hi')\n"))
    a = harvest.artifact_set(p, s.path, b.commit)
    cb = bundle.build(s.path, a, b)
    assert "new.py" in [e.path for e in cb.sidecars]
    assert cb.omitted == ()
    dest = tmp_path / "verifier"
    fleet.clone_seat(repo, b, dest, name="verifier", identity=IDENT)
    bundle.materialize(cb, dest)
    assert (dest / "new.py").read_text() == "print('hi')\n"


def test_an_executable_bit_survives_the_crossing(tmp_path):
    repo, b, s = _seat(tmp_path)
    def work():
        q = write(s.path, "run.sh", "#!/bin/sh\necho hi\n")
        q.chmod(0o755)
    p = _phases(s.path, work)
    a = harvest.artifact_set(p, s.path, b.commit)
    cb = bundle.build(s.path, a, b)
    dest = tmp_path / "verifier"
    fleet.clone_seat(repo, b, dest, name="verifier", identity=IDENT)
    bundle.materialize(cb, dest)
    assert os.access(dest / "run.sh", os.X_OK), "mode dropped; a test runner would not run"


def test_a_binary_file_crosses_intact(tmp_path):
    """`git diff` without --binary drops content at exit 0; harvest passes --binary."""
    repo, b, s = _seat(tmp_path)
    blob = bytes(range(256)) * 4
    p = _phases(s.path, lambda: (s.path / "img.bin").write_bytes(blob))
    a = harvest.artifact_set(p, s.path, b.commit)
    cb = bundle.build(s.path, a, b)
    dest = tmp_path / "verifier"
    fleet.clone_seat(repo, b, dest, name="verifier", identity=IDENT)
    bundle.materialize(cb, dest)
    assert (dest / "img.bin").read_bytes() == blob


def test_a_deletion_crosses_as_a_deletion(tmp_path):
    repo, b, s = _seat(tmp_path)
    p = _phases(s.path, lambda: (s.path / "seed.txt").unlink())
    a = harvest.artifact_set(p, s.path, b.commit)
    cb = bundle.build(s.path, a, b)
    dest = tmp_path / "verifier"
    fleet.clone_seat(repo, b, dest, name="verifier", identity=IDENT)
    bundle.materialize(cb, dest)
    assert not (dest / "seed.txt").exists()


def test_setup_output_does_not_cross(tmp_path):
    """The bundle carries the AGENT's work. node_modules is not it."""
    repo, b, s = _seat(tmp_path)
    f0 = harvest.record(s.path)
    (s.path / "node_modules").mkdir()
    write(s.path, "node_modules/dep.js", "dep\n")
    fsetup = harvest.record(s.path)
    write(s.path, "src.py", "work\n")
    fwork = harvest.record(s.path)
    a = harvest.artifact_set(
        harvest.Phases(f0=f0, fsetup=fsetup, fwork=fwork, fverify=fwork), s.path, b.commit)
    cb = bundle.build(s.path, a, b)
    carried = set(_carried(cb))
    assert "src.py" in carried
    assert not any(c.startswith("node_modules/") for c in carried)


def test_a_path_the_bundle_cannot_carry_is_named_in_omitted(tmp_path):
    """A verifier failure from a missing input must be distinguishable from a defect."""
    repo, b, s = _seat(tmp_path)
    os.mkfifo(s.path / "pipe")            # in paths; no honest payload
    p = _phases(s.path, lambda: write(s.path, "src.py", "work\n"))
    a = harvest.artifact_set(p, s.path, b.commit)
    cb = bundle.build(s.path, a, b)
    assert "pipe" in cb.omitted
    assert "src.py" not in cb.omitted


def test_materialize_refuses_a_bundle_from_a_different_baseline(tmp_path):
    repo, b, s = _seat(tmp_path)
    p = _phases(s.path, lambda: write(s.path, "src.py", "work\n"))
    cb = bundle.build(s.path, harvest.artifact_set(p, s.path, b.commit), b)
    other = make_repo(tmp_path, "other")
    run2 = tmp_path / "run2"; run2.mkdir()
    b2 = baseline.materialize(other, run2, finspect.repo_facts(other), [], "r2")
    dest = tmp_path / "verifier"
    fleet.clone_seat(other, b2, dest, name="verifier", identity=IDENT)
    with pytest.raises(bundle.BundleError, match="baseline"):
        bundle.materialize(cb, dest)


def _carried(cb):
    """Every path the bundle actually carries, from both channels."""
    out = [e.path for e in cb.sidecars]
    for line in cb.tracked_patch.decode("utf-8", "surrogateescape").splitlines():
        if line.startswith("+++ b/"):
            out.append(line[6:])
    return out
```

- [ ] **Step 2: Run to verify it fails**

Run: `uvx pytest tests/test_forge_bundle.py -q`
Expected: `ModuleNotFoundError: No module named 'forge.bundle'`.

- [ ] **Step 3: Implement**

Write `shared/lib/forge/bundle.py`. The shape:

```python
"""The CandidateBundle: exactly what crosses from a seat into a verifier (spec §6).

A verifier clone is worthless if the thing verified is the seat's own tree — the builder
could have rigged it. So the candidate crosses as DATA, and the bundle is that data.

Two channels, because `tracked_diff` is a partial view of `paths` for three causes
(untracked, binary/-diff, submodule content):
  * tracked_patch  — `git diff --binary` bytes, applied with `git apply --index`
  * sidecars       — everything else, carried as literal payload with its mode

Anything neither channel can carry honestly goes in `omitted`, so a verifier failure
caused by a missing input is never mistaken for a candidate defect.
"""
```

- `build` reads `artifacts.tracked_diff` for the patch channel (re-encode with
  `.encode("utf-8", "surrogateescape")` — that is a caller obligation with nothing
  mechanically enforcing it), then walks `artifacts.paths` and classifies each path not
  represented in the patch: a regular file becomes a `SidecarEntry` with its bytes and
  `st_mode & 0o777`; a symlink becomes a `SidecarEntry` with `kind="symlink"` and the
  target as payload **iff D-1 says symlinks cross**; anything else (`kind="special"`,
  unreadable, absent) goes to `omitted`.
- `materialize` asserts `bundle.baseline_commit == HEAD` of `dest` before writing anything
  and raises `BundleError` otherwise; applies the patch with `git apply --index` (so modes
  in the patch are honoured); writes sidecars with their recorded mode; returns the paths
  written.
- `version = 1`, checked on materialize.

**Settle D-1 here, and record the decision in the module docstring**: does a symlink cross
as a link (`kind="symlink"`, payload = target text), or land in `omitted`? Whichever you
choose, the `baseline.filesystem_manifest` inconsistency it was pinned against must either
be fixed in the same commit or the characterization test in `tests/test_forge_seams.py`
updated to describe the new truth. Do not leave two modules disagreeing.

**Settle D-2 here too**: if the escape-vs-internal discrimination belongs in `screen`,
implement it (an in-tree link whose normalized target stays under the root is not a
breach); if it belongs in the caller, say so in `screen`'s docstring and leave the
conservative behaviour. Pin whichever you choose.

- [ ] **Step 4: Run to verify it passes**

Run: `uvx pytest tests/test_forge_bundle.py -q` → 8 passed.
Run: `uvx pytest tests/ -m "not slow" -q` → all pass.

- [ ] **Step 5: Render and commit**

```bash
make render
git add shared/lib/forge/bundle.py shared/lib/forge/screen.py tests/test_forge_bundle.py tests/test_forge_seams.py marketplaces
git commit -m "feat(forge): the CandidateBundle — what crosses into a verifier, and what does not"
```

---

### Task 2: Verifier clones and the engine-run gate

**Files:**
- Create: `shared/lib/forge/verify.py`, `tests/test_forge_verify.py`

**Interfaces:**
- Consumes: `bundle`, `fleet.clone_seat`, `gitcmd`, `storage`.
- Produces:
  - `verify.Step` — frozen: `argv: tuple[str, ...]`, `cwd: str = ""`, `env: dict = {}`, `timeout: int = 600`
  - `verify.Command` — frozen: `steps: tuple[Step, ...]`; `Command.parse(spec) -> Command` rejecting shell metacharacters
  - `verify.Run` — frozen: `exit_code: int`, `stdout: str`, `stderr: str`, `duration_sec: float`, `step_index: int`
  - `verify.build_verifier(repo, baseline, candidate, dest, *, identity) -> Path`
  - `verify.run_command(cwd, command, *, env=None) -> Run`
  - `verify.VerifyError`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_forge_verify.py
"""The gate runs where the builder never was, and the engine runs it (spec §6)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

import pytest  # noqa: E402
from forge import baseline, bundle, fleet, harvest, inspect as finspect, verify  # noqa: E402
from forge_fixtures import make_repo, write  # noqa: E402

IDENT = ("Forge Seat", "seat@forge.invalid")


def test_parse_rejects_shell_metacharacters():
    with pytest.raises(verify.VerifyError, match="shell"):
        verify.Command.parse(["make verify && rm -rf /"])
    c = verify.Command.parse([["make", "verify"]])
    assert c.steps[0].argv == ("make", "verify")


def test_a_sabotaged_test_runner_does_not_cross_into_the_verifier(tmp_path):
    """The headline property: a check the builder could rig is not a check."""
    repo = make_repo(tmp_path)
    write(repo, "check.sh", "#!/bin/sh\nexit 1\n")
    (Path(repo) / "check.sh").chmod(0o755)
    run = tmp_path / "run"; run.mkdir()
    f = finspect.repo_facts(repo)
    b = baseline.materialize(repo, run, f, ["check.sh"], "r1")
    s = fleet.clone_seat(repo, b, tmp_path / "seat", name="claude", identity=IDENT)

    f0 = harvest.record(s.path); fsetup = harvest.record(s.path)
    # The seat rigs the gate in a path OUTSIDE its own work.
    (s.path / ".venv").mkdir(); write(s.path, ".venv/rigged", "yes\n")
    write(s.path, "src.py", "the actual work\n")
    fwork = harvest.record(s.path)
    a = harvest.artifact_set(
        harvest.Phases(f0=f0, fsetup=fsetup, fwork=fwork, fverify=fwork), s.path, b.commit)
    cb = bundle.build(s.path, a, b)

    v = verify.build_verifier(repo, b, cb, tmp_path / "verifier", identity=IDENT)
    assert (v / "src.py").exists(), "the agent's work must cross"
    assert not (v / ".venv").exists(), "seat-only state must not cross"
    r = verify.run_command(v, verify.Command.parse([["./check.sh"]]))
    assert r.exit_code == 1, "the ORIGINAL gate ran, not whatever the seat left behind"


def test_run_command_reports_the_failing_step_index(tmp_path):
    d = tmp_path / "w"; d.mkdir()
    c = verify.Command.parse([["true"], ["false"], ["true"]])
    r = verify.run_command(d, c)
    assert r.exit_code != 0 and r.step_index == 1


def test_a_step_timeout_is_a_verify_error_not_a_hang(tmp_path):
    d = tmp_path / "w"; d.mkdir()
    c = verify.Command(steps=(verify.Step(argv=("sleep", "30"), timeout=1),))
    with pytest.raises(verify.VerifyError, match="timeout"):
        verify.run_command(d, c)


def test_the_verifier_clone_has_no_origin_and_its_own_identity(tmp_path):
    repo = make_repo(tmp_path)
    run = tmp_path / "run"; run.mkdir()
    b = baseline.materialize(repo, run, finspect.repo_facts(repo), [], "r1")
    s = fleet.clone_seat(repo, b, tmp_path / "seat", name="claude", identity=IDENT)
    f0 = harvest.record(s.path)
    a = harvest.artifact_set(
        harvest.Phases(f0=f0, fsetup=f0, fwork=f0, fverify=f0), s.path, b.commit)
    v = verify.build_verifier(repo, b, bundle.build(s.path, a, b),
                              tmp_path / "verifier", identity=IDENT)
    import subprocess
    assert subprocess.run(["git", "-C", str(v), "remote"], capture_output=True,
                          text=True).stdout.strip() == ""
```

- [ ] **Step 2: Run to verify it fails**

Run: `uvx pytest tests/test_forge_verify.py -q`
Expected: `ModuleNotFoundError: No module named 'forge.verify'`.

- [ ] **Step 3: Implement**

`build_verifier` calls `fleet.clone_seat` (reusing every defence it already carries — no
origin, no hardlinks, no ambient template, own identity, verified checkout) then
`bundle.materialize` into it. `run_command` executes each `Step` with
`subprocess.run(argv, cwd=…, env=…, timeout=…)`, capturing output; a `TimeoutExpired`
becomes `VerifyError`. `Command.parse` accepts a list of argv lists and rejects any string
containing shell metacharacters, naming the character found.

Repository hooks must be disabled in the verifier: pass `core.hooksPath` at `/dev/null`
via `-c`, or unset it in the clone's config. Verify which actually works and say so.

- [ ] **Step 4: Run to verify it passes**

Run: `uvx pytest tests/test_forge_verify.py -q` → 5 passed.

- [ ] **Step 5: Render and commit**

```bash
make render
git add shared/lib/forge/verify.py tests/test_forge_verify.py marketplaces
git commit -m "feat(forge): verifier clones and an engine-run gate"
```

---

### Task 3: The GeneratorContract and its fixed point

**Files:**
- Modify: `shared/lib/forge/inspect.py`, `shared/lib/forge/verify.py`
- Test: `tests/test_forge_verify.py`, `tests/test_forge_inspect.py` (extend)

**Interfaces:**
- Produces:
  - `inspect.GeneratorContract` — frozen: `id: str`, `relations: tuple[tuple[str, str], ...]` (source glob → output glob)
  - `inspect.detect_generators(repo) -> GeneratorContract` — static, read-only, engine-owned
  - `verify.fixed_point(verifier_path, command, contract, *, max_passes=2) -> tuple[Run, tuple[str, ...]]` — returns the final run and the admitted generated paths
  - `verify.GeneratorUnstable(VerifyError)`

**The contract is a property of the RUN, not of a seat.** A seat-declared generator set
lets a candidate define its own success criterion — "receipts are generated" is one line
away from laundering an unearned eval receipt past a commit gate. Detect it statically,
confirm it at the gate, record it in the manifest, and never let a seat widen it.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_forge_verify.py  (append)
def test_a_verify_that_regenerates_tracked_files_converges_in_two_passes(tmp_path):
    """This repo's own shape: `make verify` runs `render`, which rewrites tracked output."""
    repo = make_repo(tmp_path)
    write(repo, "src/a.txt", "v1\n")
    write(repo, "gen/a.txt", "v1\n")
    write(repo, "build.sh", "#!/bin/sh\nmkdir -p gen\ncp src/a.txt gen/a.txt\n")
    (Path(repo) / "build.sh").chmod(0o755)
    from forge_fixtures import commit_all
    commit_all(repo, "seed generator")
    run = tmp_path / "run"; run.mkdir()
    b = baseline.materialize(repo, run, finspect.repo_facts(repo), [], "r1")
    s = fleet.clone_seat(repo, b, tmp_path / "seat", name="claude", identity=IDENT)
    f0 = harvest.record(s.path); fsetup = harvest.record(s.path)
    write(s.path, "src/a.txt", "v2\n")          # the agent edits the SOURCE only
    fwork = harvest.record(s.path)
    a = harvest.artifact_set(
        harvest.Phases(f0=f0, fsetup=fsetup, fwork=fwork, fverify=fwork), s.path, b.commit)
    cb = bundle.build(s.path, a, b)
    v = verify.build_verifier(repo, b, cb, tmp_path / "verifier", identity=IDENT)

    contract = inspect_mod_contract(("src/*", "gen/*"))
    r, admitted = verify.fixed_point(v, verify.Command.parse([["./build.sh"]]), contract)
    assert r.exit_code == 0
    assert "gen/a.txt" in admitted, "the regenerated output must be admitted, not discarded"
    assert (v / "gen" / "a.txt").read_text() == "v2\n"


def test_a_nondeterministic_generator_is_unstable_not_silently_accepted(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "build.sh", "#!/bin/sh\ndate +%s%N > gen.txt\n")
    (Path(repo) / "build.sh").chmod(0o755)
    write(repo, "gen.txt", "seed\n")
    from forge_fixtures import commit_all
    commit_all(repo, "seed")
    run = tmp_path / "run"; run.mkdir()
    b = baseline.materialize(repo, run, finspect.repo_facts(repo), [], "r1")
    s = fleet.clone_seat(repo, b, tmp_path / "seat", name="claude", identity=IDENT)
    f0 = harvest.record(s.path)
    a = harvest.artifact_set(
        harvest.Phases(f0=f0, fsetup=f0, fwork=f0, fverify=f0), s.path, b.commit)
    v = verify.build_verifier(repo, b, bundle.build(s.path, a, b),
                              tmp_path / "verifier", identity=IDENT)
    contract = inspect_mod_contract((".", "gen.txt"))
    with pytest.raises(verify.GeneratorUnstable):
        verify.fixed_point(v, verify.Command.parse([["./build.sh"]]), contract)


def test_an_output_outside_the_contract_is_not_admitted(tmp_path):
    """A seat cannot widen the contract by writing somewhere new."""
    repo = make_repo(tmp_path)
    write(repo, "build.sh", "#!/bin/sh\necho sneaky > receipt.json\n")
    (Path(repo) / "build.sh").chmod(0o755)
    from forge_fixtures import commit_all
    commit_all(repo, "seed")
    run = tmp_path / "run"; run.mkdir()
    b = baseline.materialize(repo, run, finspect.repo_facts(repo), [], "r1")
    s = fleet.clone_seat(repo, b, tmp_path / "seat", name="claude", identity=IDENT)
    f0 = harvest.record(s.path)
    a = harvest.artifact_set(
        harvest.Phases(f0=f0, fsetup=f0, fwork=f0, fverify=f0), s.path, b.commit)
    v = verify.build_verifier(repo, b, bundle.build(s.path, a, b),
                              tmp_path / "verifier", identity=IDENT)
    contract = inspect_mod_contract(("src/*", "gen/*"))
    r, admitted = verify.fixed_point(v, verify.Command.parse([["./build.sh"]]), contract)
    assert "receipt.json" not in admitted


def inspect_mod_contract(relation):
    from forge import inspect as fi
    return fi.GeneratorContract(id="test", relations=(relation,))
```

- [ ] **Step 2: Run to verify it fails**

Run: `uvx pytest tests/test_forge_verify.py -q`
Expected: `AttributeError: module 'forge.inspect' has no attribute 'GeneratorContract'`.

- [ ] **Step 3: Implement**

`fixed_point` runs the command, snapshots the tree, and compares against the pre-run
snapshot. Tracked changes matching a contract relation's output glob are **admitted**:
staged into the verifier's index (a record-only checkpoint is not enough — this repo's
`precommit` fails on regenerated-but-unstaged output) and the command re-run. A clean
`PASS` requires exit 0 **and** no unexplained tracked delta, within `max_passes`. A third
pass with a delta raises `GeneratorUnstable` — infrastructure-class, never attributed to
the candidate. Changes outside the contract are returned but not admitted.

`detect_generators` is static and read-only: recognise the relation from the repo's own
build definition rather than guessing. For this repo the relation is
`shared/** + capabilities.toml → marketplaces/**`; find where that is declared
(`scripts/render.py`) and derive it, or return an empty contract and say why detection is
not possible without a declaration.

- [ ] **Step 4: Run to verify it passes**

Run: `uvx pytest tests/test_forge_verify.py tests/test_forge_inspect.py -q` → all pass.

- [ ] **Step 5: Render and commit**

```bash
make render
git add shared/lib/forge/inspect.py shared/lib/forge/verify.py tests marketplaces
git commit -m "feat(forge): an engine-owned generator contract and its fixed point"
```

---

### Task 4: Verify outcomes and the gate surface

**Files:**
- Modify: `shared/lib/forge/verify.py`
- Test: `tests/test_forge_verify.py` (extend)

**Interfaces:**
- Produces:
  - `verify.Outcome` — `str` enum-like constants: `PASS`, `FAIL`, `FLAKY`, `BASELINE_RED_NO_NEW_IDENTIFIED_FAILURE`, `HARVEST_INCOMPLETE`, `GATE_CHANGED`
  - `verify.classify(candidate_run, baseline_run, bundle, *, rerun=None) -> tuple[str, str]` — `(outcome, reason)`
  - `verify.gate_surface(verifier_path, contract) -> tuple[str, ...]`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_forge_verify.py  (append)
def _run(code, out=""):
    return verify.Run(exit_code=code, stdout=out, stderr="", duration_sec=0.1, step_index=0)


def test_exit_zero_alone_is_not_a_pass_when_the_bundle_omitted_an_input():
    b = bundle.CandidateBundle(version=1, baseline_ref="r", baseline_commit="c",
                               tracked_patch=b"", sidecars=(), gate_delta=(),
                               generator_contract_id="x", omitted=("fixtures/data.bin",))
    outcome, reason = verify.classify(_run(1, "FileNotFoundError: fixtures/data.bin"),
                                      _run(0), b)
    assert outcome == verify.HARVEST_INCOMPLETE
    assert "fixtures/data.bin" in reason


def test_a_baseline_that_was_already_red_is_not_reported_as_a_pass():
    b = bundle.CandidateBundle(version=1, baseline_ref="r", baseline_commit="c",
                               tracked_patch=b"", sidecars=(), gate_delta=(),
                               generator_contract_id="x", omitted=())
    outcome, _ = verify.classify(_run(1, "1 failed"), _run(1, "1 failed"), b)
    assert outcome == verify.BASELINE_RED_NO_NEW_IDENTIFIED_FAILURE


def test_a_fail_then_pass_rerun_is_flaky_not_a_pass():
    b = bundle.CandidateBundle(version=1, baseline_ref="r", baseline_commit="c",
                               tracked_patch=b"", sidecars=(), gate_delta=(),
                               generator_contract_id="x", omitted=())
    outcome, _ = verify.classify(_run(1), _run(0), b, rerun=_run(0))
    assert outcome == verify.FLAKY


def test_a_candidate_that_edited_the_gate_is_marked_not_silently_passed():
    b = bundle.CandidateBundle(version=1, baseline_ref="r", baseline_commit="c",
                               tracked_patch=b"", sidecars=(), gate_delta=("Makefile",),
                               generator_contract_id="x", omitted=())
    outcome, reason = verify.classify(_run(0), _run(0), b)
    assert outcome == verify.GATE_CHANGED and "Makefile" in reason


def test_a_clean_pass_is_a_pass():
    b = bundle.CandidateBundle(version=1, baseline_ref="r", baseline_commit="c",
                               tracked_patch=b"", sidecars=(), gate_delta=(),
                               generator_contract_id="x", omitted=())
    assert verify.classify(_run(0), _run(0), b)[0] == verify.PASS
```

- [ ] **Step 2: Run to verify it fails**

Run: `uvx pytest tests/test_forge_verify.py -q`
Expected: `AttributeError: module 'forge.verify' has no attribute 'classify'`.

- [ ] **Step 3: Implement**

`classify` applies §6.2's table in a fixed precedence: `HARVEST_INCOMPLETE` when the
candidate failed and the failure output names a path in `bundle.omitted`;
`GATE_CHANGED` when `bundle.gate_delta` is non-empty (a passing command whose
implementation was weakened is not equivalent evidence — mark it even on exit 0);
`BASELINE_RED_…` when both runs failed comparably; `FLAKY` when a rerun disagrees with the
first run; `FAIL` on a new failure; `PASS` only on exit 0 with none of the above.

`gate_surface` records the files that DEFINE the gate — build definitions, test-runner
config, CI config, discovered test files — so `bundle.build` can populate `gate_delta`.
Derive the list from what the repo actually has rather than a hardcoded name set, and say
what you derived it from.

- [ ] **Step 4: Run to verify it passes**

Run: `uvx pytest tests/test_forge_verify.py -q` → all pass.

- [ ] **Step 5: Render and commit**

```bash
make render
git add shared/lib/forge/verify.py tests/test_forge_verify.py marketplaces
git commit -m "feat(forge): honest verify outcomes and the gate surface"
```

---

### Task 5: Refusal-seam assertions, packaging, gates

**Files:**
- Modify: `tests/test_forge_seams.py`, `Makefile`
- Test: itself

Plan C's whole-branch review named the seam class this task closes:

> The seam class is closed only where **both sides are modules that produce a value the other consumes**. It is not closed where one side is a **refusal** or a **policy constant**. Nothing asserts that a repo preflight admits is one the chain completes, nor that one it refuses would have failed.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_forge_seams.py  (append)
"""SEAM CLASS: refusals. `inspect.rejections` is a policy that nothing downstream checks."""

REFUSAL_FIXTURES = [
    ("eol_no_roundtrip", _mk_eol_repo),        # define each helper below
    ("escaping_symlink", _mk_escaping_repo),
    ("shallow", _mk_shallow_repo),
]


@pytest.mark.parametrize("name,builder", REFUSAL_FIXTURES)
def test_a_repo_preflight_refuses_would_have_failed_downstream(name, builder, tmp_path):
    """Every refusal must be earned: prove the chain breaks without it."""
    repo = builder(tmp_path)
    f = finspect.repo_facts(repo)
    assert finspect.rejections(f, []) != [], f"{name}: fixture no longer triggers a refusal"
    run = tmp_path / "run"; run.mkdir()
    with _refusals_disabled():          # bypass the policy, keep the mechanism
        b = baseline.materialize(repo, run, f, [], "r1")
        with pytest.raises((fleet.SeatError, bundle.BundleError, verify.VerifyError)):
            s = fleet.clone_seat(repo, b, tmp_path / "seat", name="c", identity=IDENT)
            _assert_chain_completes(repo, b, s, tmp_path)


def test_a_repo_preflight_admits_completes_the_chain(tmp_path):
    """The other half: a clean repo must reach a verdict, not just avoid refusal."""
    repo = make_repo(tmp_path)
    write(repo, "check.sh", "#!/bin/sh\nexit 0\n")
    (Path(repo) / "check.sh").chmod(0o755)
    commit_all(repo, "gate")
    f = finspect.repo_facts(repo)
    assert finspect.rejections(f, []) == []
    run = tmp_path / "run"; run.mkdir()
    b = baseline.materialize(repo, run, f, [], "r1")
    s = fleet.clone_seat(repo, b, tmp_path / "seat", name="c", identity=IDENT)
    f0 = harvest.record(s.path)
    write(s.path, "src.py", "work\n")
    fw = harvest.record(s.path)
    a = harvest.artifact_set(harvest.Phases(f0=f0, fsetup=f0, fwork=fw, fverify=fw),
                             s.path, b.commit)
    cb = bundle.build(s.path, a, b)
    v = verify.build_verifier(repo, b, cb, tmp_path / "verifier", identity=IDENT)
    r = verify.run_command(v, verify.Command.parse([["./check.sh"]]))
    assert verify.classify(r, r, cb)[0] == verify.PASS
```

Write `_mk_eol_repo`, `_mk_escaping_repo`, `_mk_shallow_repo`, `_refusals_disabled` and
`_assert_chain_completes` yourself — each fixture must be the *minimal* repo that trips its
refusal, and `_refusals_disabled` must bypass only the policy check, never the mechanism it
protects. If a refusal turns out **not** to break the chain when bypassed, that is a finding:
report it rather than deleting the case, because it means either the refusal is unnecessary
or the chain fails silently.

- [ ] **Step 2: Run and record which refusals are earned**

Run: `uvx pytest tests/test_forge_seams.py -q`
Report every case that does not behave as the test expects — a refusal with no downstream
failure is a real finding either way.

- [ ] **Step 3: Wire the gate**

Extend `FORGE_TESTS` in the `Makefile` with `tests/test_forge_bundle.py` and
`tests/test_forge_verify.py`. **Verify** the closure with
`checks.source_manifest(ROOT, "llm-forge")` rather than assuming — the entry names
`shared/lib/forge` as a directory, so new modules should arrive automatically, but Plan C
found two suites sitting outside the gate for two whole tasks.

- [ ] **Step 4: Run the gates**

Run: `make render`; `uvx pytest tests/ -m "not slow" -q`; `make council-test`;
`make verify`; `make precommit` — record every exit code via `echo $?`, 15-minute timeouts.
If receipts stale, verify the cause first, then reseed **scoped** (`--seed-receipt --skill
<name>`); never unscoped — it destroys any real `provenance: "eval"` receipt.

- [ ] **Step 5: Commit**

```bash
git add tests/test_forge_seams.py Makefile marketplaces
git commit -m "test(forge): assert that refusals are earned and admissions complete"
```

---

## Self-review

**Spec coverage.** §6 verifier clones → Tasks 1–2 (the bundle is what makes a fresh clone
possible; `build_verifier` is the clone). §6.1 gate surface → Task 4 (`gate_surface`,
`gate_delta`, `GATE_CHANGED`). §6.2 outcomes → Task 4, all six including
`HARVEST_INCOMPLETE`, which is what makes `bundle.omitted` load-bearing rather than
informational. §7.2 generator fixed point → Task 3, engine-owned, two-pass bounded,
`GeneratorUnstable` for non-convergence.

**Deliberately out of scope**, each with a later home: the journal and state machine (§14),
review and ultrareview (§13), handover (§16), strategy and fallback (§12), the skill and
its evals (§18/§20). Nothing here launches a provider — every "verify command" is a shell
script in a fixture repo.

**The two open decisions are assigned, not deferred.** D-1 (tracked symlink) and D-2
(symlink noise) both land in Task 1, because the bundle is the first place a symlink's
identity must be decided rather than skipped. Task 1 must not leave two modules disagreeing.

**Placeholder scan.** None in the implementation steps. Task 5 deliberately asks the
implementer to write its own fixtures — that is the task, not a gap, and the plan states
what each must satisfy and what to do when one misbehaves.

**Type consistency.** `CandidateBundle`'s eight fields are used identically in Tasks 1, 2
and 4; `verify.Run`'s five fields match between `run_command`, `classify` and the `_run`
helper; `Outcome` constants are referenced by the same names in Task 4's tests and its
implementation; `GeneratorContract(id, relations)` matches between `detect_generators`,
`fixed_point` and Task 3's `inspect_mod_contract` helper.

**One risk worth naming.** Task 4's `classify` takes `baseline_run` as a parameter but
nothing in this plan produces one — the baseline calibration run belongs to §5's
confirmation gate, which is not in scope here. Tests supply it directly. That is honest for
this plan, but Plan E must wire a real calibration or `BASELINE_RED_…` can never fire in
production, and a classifier branch that cannot fire is the defect class this project has
found in every plan so far.
