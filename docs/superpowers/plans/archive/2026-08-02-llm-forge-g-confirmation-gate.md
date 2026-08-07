# llm-forge Plan G: the confirmation gate

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Nothing runs until a human has seen the truth — a preflight that refuses what §2.3 says must be refused, a cost quote that is honest about the worst case, and one gate that asks once and records the answer in the write-once manifest.

**Architecture:** Six plans have built machinery with no caller. This one builds the caller for the part that comes *before* any provider spends a token: the static read-only preflight (§5 step 1), and the single confirmation gate (§5 step 2) whose answer becomes the manifest. It also pays three debts that block it — an amended state graph, `inspect.rejections` finally having a consumer, and two of §9's protected items being recorded. It does **not** launch a seat; that is the next plan.

**Tech Stack:** Python 3.11+ stdlib only. `git` 2.53 via `shared/lib/forge/gitcmd.py`. pytest via `uvx`.

## Global Constraints

- **Python stdlib only.** No pip dependencies. Must run on any Python 3.11+ machine with no install step.
- **Commands run as argv lists, never through a shell.** Shell metacharacter syntax is **rejected**, not reinterpreted (§5.1).
- **Git is located by asking git**, never by string-joining `.git`. Every git call goes through `gitcmd.git`.
- **Fail closed.** A measurement that could not be taken is `None`/UNKNOWN, never an empty success.
- **A verdict must never read cleaner than its evidence.**
- **A comment asserting something the code does not do is a defect** — and so is a claim about a configuration the package *refuses*, unless you have measured that it refuses it. Four successive claims about `--skip-worktree` in the preceding plan were measured wrong, each differently, and the last one shipped as a correction of the one before.
- **Preflight is static and read-only.** No arbitrary project setup code runs before authorization (§5). If you find yourself executing anything the repository supplies, you have left preflight.
- `shared/lib/forge/**` is source of truth; `marketplaces/**` is render output. Never hand-edit it — run `make render`.
- Every task ends with `make render`, an explicit-pathspec `git add` **including `marketplaces`**, then `make verify` and `make precommit`, then the commit. Never `git add -A`. The order matters: `precommit`'s drift check compares the working tree to the **index**, so running it before the render output is staged fails unconditionally. **Run the gates unpiped and capture `$?`** — a pipe reports the pipe's exit status, so `make verify | tail` reports a green nobody measured.
- Use **`scripts/mutate.py`** for mutations. Do not author a harness. It now refuses `pytest`'s exit 5 (collected nothing) and requires a green unmutated baseline, because without those a typo'd `-k` manufactures a false CAUGHT — the verdict that makes someone *not* add a test.

## The binding decisions this plan executes

Recorded 2026-08-02, and the reason each is not a local patch:

1. **Amend §14's state graph for the whole class** — a terminal edge from every non-terminal phase, plus §9's any-phase `source_diverged`. Not the two named gaps: the local patch closes the least of it and creates the illusion the question was answered. **The spec amendment lands before any task wires a caller to `advance`**, which is why it is Task 1.
2. **Close both §9 gaps** — the exact-name-plus-OID forge whitelist, and the recorded index hash. Today's namespace prefix filter is the very thing §9 says lets a seat write into forge's namespace invisibly; and without the index hash, `--assume-unchanged` on a path makes the user's own later edits to it permanently invisible to the drift check.

## What Plan F hands you, verbatim

Verify these against the code before relying on them. Every plan in this project has had draft code that was wrong, and **five controller instructions were measurably wrong in the preceding plan alone** — including one whose "fix" was backwards and shipped as a correction. In each of the last twelve tasks an implementer overturned something by measuring instead of complying, and every overturn held under independent review. That is the behaviour this plan wants.

- `storage.atomic_write(path, data)` / `storage.exclusive_write(path, data)` (publishes via `os.link`, raises `FileExistsError`, cannot overwrite) / `storage.append_line(path, data)` — all fsync the file **and** the containing directory, in that order relative to the rename/link. `storage.StorageError`.
- `storage.new_run_id()`, `storage.run_root(repo_path, run_id, must_be_new=True)`, `storage.Quota`, `storage.manifest_path/journal_path/seat_state_path/state_path/seat_names`.
- `journal.Journal(path).record(event, *, operation_id, **data)` / `.read()`; `journal.Event(seq, event, operation_id, at, data)`; `journal.intent(kind)` / `journal.done(kind)`; `journal.orphans(events)`; `journal.JournalError`. Every record carries `pid`, `process_start`, `boot_id` and a `*_source` for each — **two `"unavailable"` sentinels compare equal**, so a liveness check must consult `*_source == "proc"` on both records before comparing either value.
- `runstate.Manifest` (13 fields; `setup`/`verify` hold `verify.Step` so all four of §5.1's per-step fields round-trip; `generator_contract` round-trips as the real `GeneratorContract`), `write_manifest` (write-once via `os.link`; refuses a manifest that would not round-trip), `read_manifest`, `ManifestError`.
- `runstate.snapshot_refs(repo, selected_paths) -> (protected_refs, status_digest)`; `runstate.drift(manifest, repo)`; `runstate.reconstruct(run_dir, repo) -> Reconstruction(manifest, state, seats, orphans, diverged)`. `drift` **refuses a repository the manifest did not record**, resolved by `rev-parse --show-toplevel` then `realpath` — a subdirectory of the recorded repo is accepted, which is why `samefile` was rejected.
- `runstate.PHASES`, `runstate.TERMINAL`, `runstate.State(phase, round, attempt, verified_checkpoint, deliverable_checkpoint)`, `runstate.advance(state, phase)` (changes **only** `phase`; raises `TransitionError` on an undeclared edge), `runstate.write_seat/read_seat/write_state/read_state`, `runstate.OUTCOME_UNKNOWN`, `StateError`, `TransitionError`.
- `inspect.repo_facts(repo) -> RepoFacts`; **`inspect.rejections(facts, selected_untracked) -> list`** — computes §2.3 in full and **has zero consumers**, guarded by a deliberate seam test that goes red when one appears. `inspect.GeneratorContract(id, relations)`; `inspect.detect_generators(repo)` returns the **empty** contract.
- `screen.screen_tree(root, rel_paths, quota=None) -> (findings, breaches)` — also **zero consumers**, under the same tripwire.
- `baseline.materialize(...)`, `fleet.clone_seat(...)`, `harvest.record/artifact_set`, `bundle.build/materialize/with_gate_measurement`, `verify.build_verifier/validate_materialized/run_setup/fixed_point/calibrate/classify/gate_surface/Command/Step/Run`, `snapshot.take/diff`, `gitcmd.git` with `HOSTILE_ENV`/`READONLY`.

### Five inherited facts that shape this plan

1. **`inspect.rejections` and `screen.screen_tree` both have zero consumers**, each with a tripwire test asserting it. This plan makes both of them consumers. **Update those tests deliberately rather than deleting them** — they exist because a policy nothing calls is a policy that does not run.
2. **A live fail-open the preceding plan measured and could not close:** `git add -u -- :/` exits **1** on a `--skip-worktree` path — bare `git add -u` exits 0, which is what an earlier measurement caught — and `baseline.materialize` does not reach `add` at all on a clean tree: `dirty` is False and the early return fires, so B lacks the user's hidden edit because **B is HEAD**, and it reports `dirty=False`. §2.3 says skip-worktree state fails closed at preflight; `rejections` computes exactly that and nothing calls it. **Wiring the consumer is what closes this**, and it is the sharpest reason Task 2 exists.
3. **`--gc` is now mandatory, and the spec does not know.** Nothing collects temp-file debris since `atomic_write` took a random suffix; spec §21 still rates `--gc`/keep-last-N as "quota + documented path would suffice — disk hygiene."
4. **The §5 gate has accumulated four things it must show or ask** beyond the two command sequences — see Task 5.
5. **`PASS` never reads `baseline_run`**, and §6's chronology is enforced at one joint only. Neither is this plan's to fix; do not let a preflight report imply otherwise.

## Deliberately out of scope

Launching a seat; the phase machine actually driving a run; §12 strategy and fallback; §13 review and ultrareview; §10's claim ledger; §16 handover; §18/§20 the skill and its evals. **Nothing here launches a provider** — every command in every fixture is a shell script or `sys.executable -c`, and preflight executes nothing the repository supplies.

## File Structure

- **Modify `docs/superpowers/specs/2026-07-30-llm-forge-design.md`** — Task 1 only, the §14 graph.
- **Modify `shared/lib/forge/runstate.py`** — the amended graph; the forge-ref producer; the index hash.
- **Create `shared/lib/forge/preflight.py`** — the read-only report and its refusals. One responsibility: what is true about this repository before anything runs.
- **Create `shared/lib/forge/gate.py`** — the cost quote and the confirmation record. One responsibility: what a human is shown, and what their answer becomes.
- **Create `tests/test_forge_preflight.py`, `tests/test_forge_gate.py`** — added to `FORGE_TESTS` **in the task that creates them**; `tests/test_forge_packaging.py` asserts set-equality.
- Modify `tests/test_forge_seams.py` — the two tripwires, and the new seams.

---

### Task 1: Amend the state graph, for the whole class

**Files:**
- Modify: `docs/superpowers/specs/2026-07-30-llm-forge-design.md` (§14)
- Modify: `shared/lib/forge/runstate.py` (`_EDGES` and its comment)
- Test: `tests/test_forge_runstate.py` (extend)

**Interfaces:** no new symbol. `runstate.PHASES`, `TERMINAL`, `advance` keep their signatures; the edge set widens.

**Why first.** The decision is explicit that the amendment lands **before any task wires a caller to `advance`**, and Task 5 is that caller. The current graph reaches every terminal only from `reviewing`, so §9's *"if the user's checkout or protected branch changes **during the run**, transition to `source_diverged`"* is inexpressible at every phase but one, and §5's confirmed `abort` policy — calibration runs inside `setting_up` — has no ending at all.

The engine's refusal to invent those edges was correct and was ruled correct: a graph quietly wider than the spec is fail-open, and no test can tell an invented edge from a declared one. **So the spec moves first, and the code follows it.**

- [ ] **Step 1: Amend the spec**

In §14, replace the diagram with one that adds, in prose beneath it, the two universal edges:

```
created → confirmed → setting_up → building → harvested → comparing
        → synthesizing ⇄ verifying → reviewing → ready | degraded | review_blocked

From ANY non-terminal phase:
        → failed             (an unrecoverable error, including §5's confirmed `abort`)
        → source_diverged    (§9: the user's checkout or a protected ref moved)
```

Say in the amendment *why* it is universal rather than two edges: a terminal reachable from one phase is a state machine that cannot record where a run actually died, and §9's condition is checked continuously rather than at review.

- [ ] **Step 2: Write the failing test**

```python
# tests/test_forge_runstate.py  (append)
def test_every_non_terminal_phase_can_reach_failed_and_source_diverged():
    """§14 as amended. A terminal reachable from one phase cannot record where a run died,
    and §9 checks its condition continuously rather than at review."""
    for phase in runstate.PHASES:
        if phase in runstate.TERMINAL:
            continue
        s = runstate.State(phase=phase, round=0, attempt=0,
                           verified_checkpoint=None, deliverable_checkpoint=None)
        assert runstate.advance(s, "failed").phase == "failed", phase
        assert runstate.advance(s, "source_diverged").phase == "source_diverged", phase


def test_the_universal_edges_did_not_widen_anything_else():
    """The amendment adds two targets to every non-terminal phase and nothing else — a graph
    that gained a third would be one nobody declared."""
    for phase in runstate.PHASES:
        if phase in runstate.TERMINAL:
            continue
        got = _successors(phase)
        assert {"failed", "source_diverged"} <= got, phase
        assert got - {"failed", "source_diverged"} == _DECLARED_SPINE[phase], phase
```

Write `_DECLARED_SPINE` yourself as a module-level dict holding each non-terminal phase's successors **excluding** the two universal ones, restated literally rather than derived from `_EDGES` — a table read back to itself pins nothing, which is the defect a preceding task found in its own first reachability test.

- [ ] **Step 3: Run to verify it fails**

Run: `uvx pytest tests/test_forge_runstate.py -q`
Expected: `TransitionError` naming the legal successors of `created`.

- [ ] **Step 4: Implement**

Widen `_EDGES` so every non-terminal phase's successor set includes `failed` and `source_diverged`, and **rewrite the comment that currently records the gap** — it says the edges are deliberately not invented and that the amendment belongs to the whole class. That sentence becomes false the moment the spec lands; replace it with what the graph now expresses and why the two are universal.

Terminals still map to the empty set, so `advance` from a terminal still raises.

- [ ] **Step 5: Mutate**

`scripts/mutate.py`, one site at a time, with a control you expect to survive: add only `failed`; add only `source_diverged`; add both to terminals too; derive `_DECLARED_SPINE` from `_EDGES` instead of restating it (must break the second test's meaning — if it does not, the test reads a table back to itself).

- [ ] **Step 6: Render, gate, commit**

```bash
make render
git add docs/superpowers/specs/2026-07-30-llm-forge-design.md shared/lib/forge/runstate.py tests/test_forge_runstate.py marketplaces
make verify
make precommit
git commit -m "spec+forge: every non-terminal phase can record where a run died"
```

---

### Task 2: Preflight refuses what §2.3 says it must

**Files:**
- Create: `shared/lib/forge/preflight.py`
- Create: `tests/test_forge_preflight.py`
- Modify: `Makefile` (`FORGE_TESTS`), `tests/test_forge_seams.py`
- Test: itself

**Interfaces:**
- Consumes: `inspect.repo_facts`, `inspect.rejections`, `screen.screen_tree`, `runstate.snapshot_refs`.
- Produces:
  - `preflight.Report` — frozen: `repo: Path`, `facts`, `rejections: tuple[str, ...]`, `selected: tuple[str, ...]`, `secrets: tuple`, `breaches: tuple[str, ...]`, `contract`, `gate_surface: tuple[str, ...] | None`.
  - `preflight.inspect_repo(repo, selected_untracked=()) -> Report` — read-only; executes nothing the repository supplies.
  - `preflight.refusals(report) -> tuple[str, ...]` — everything that must stop the run.
  - `preflight.PreflightError(RuntimeError)`.

**Why this closes a live fail-open.** `inspect.rejections` computes §2.3 in full and **nothing calls it**. The measured consequence: `git add -u -- :/` exits **1** on a `--skip-worktree` path — bare `git add -u` exits 0, which is what an earlier measurement caught — and `baseline.materialize` does not reach `add` at all on a clean tree: `dirty` is False and the early return fires, so B lacks the user's hidden edit because **B is HEAD**, and it reports `dirty=False`. §2.3 lists skip-worktree state among the conditions that fail closed *at preflight*, and preflight is what this task builds.

**The two tripwires are the point, not an obstacle.** `tests/test_forge_seams.py` asserts that `rejections` and `screen_tree` have zero consumers, because a policy nothing calls is a policy that does not run. **This task makes both of them consumers, so both tests must change.** Rewrite each to assert the property that now holds — that the consumer is `preflight` and that the refusal actually stops the run — rather than deleting it.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_forge_preflight.py
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))
sys.path.insert(0, str(ROOT / "tests"))

from forge import preflight  # noqa: E402
from forge_fixtures import commit_all, git as _git, make_repo, write  # noqa: E402


def test_a_clean_repository_has_no_refusals(tmp_path):
    repo = make_repo(tmp_path)
    r = preflight.inspect_repo(repo)
    assert preflight.refusals(r) == ()
    assert r.repo == Path(repo)


def test_skip_worktree_is_refused_at_preflight(tmp_path):
    """§2.3 lists it, and the reason is measured: `git add -u -- :/` exits 0 and SILENTLY
    SKIPS such a path, so the baseline is built without the user's hidden edit and reports
    itself clean. Nothing downstream can see what preflight does not stop."""
    repo = make_repo(tmp_path)
    _git(repo, "update-index", "--skip-worktree", "seed.txt")
    got = preflight.refusals(preflight.inspect_repo(repo))
    assert any("skip-worktree" in line for line in got), got


def test_a_shallow_repository_is_refused(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "second.txt", "x\n")
    commit_all(repo, "second")
    shallow = tmp_path / "shallow"
    _git(tmp_path, "clone", "--depth", "1", f"file://{repo}", str(shallow))
    got = preflight.refusals(preflight.inspect_repo(shallow))
    assert any("shallow" in line for line in got), got


def test_a_selected_path_that_escapes_the_repository_is_refused(tmp_path):
    """The containment check the selection has never had. A selected `../outside.txt`
    reaches the carried digest, which then hashes content outside the repository."""
    repo = make_repo(tmp_path)
    (tmp_path / "outside.txt").write_text("host\n")
    got = preflight.refusals(preflight.inspect_repo(repo, ("../outside.txt",)))
    assert any("outside" in line or "escapes" in line for line in got), got


def test_a_secret_in_a_selected_path_is_a_refusal_not_a_note(tmp_path):
    """§3: the screen runs BEFORE any provider starts. A finding that only informs is a
    finding that ships the credential to three providers."""
    repo = make_repo(tmp_path)
    write(repo, "scratch/.env", "AWS_SECRET_ACCESS_KEY=" + "A" * 40 + "\n")
    got = preflight.refusals(preflight.inspect_repo(repo, ("scratch",)))
    assert got, "a screened secret must stop the run"


def test_preflight_executes_nothing_the_repository_supplies(tmp_path):
    """§5 step 1 is STATIC and READ-ONLY: no arbitrary project setup code runs before
    authorization. A Makefile, a setup.py and a conftest that would fail loudly if run are
    all present and must not be."""
    repo = make_repo(tmp_path)
    write(repo, "Makefile", "all:\n\t@exit 42\n")
    write(repo, "setup.py", "raise SystemExit('preflight executed setup.py')\n")
    write(repo, "conftest.py", "raise SystemExit('preflight imported conftest')\n")
    commit_all(repo, "hostile")
    r = preflight.inspect_repo(repo)
    assert preflight.refusals(r) == ()


def test_the_report_does_not_claim_a_gate_surface_it_could_not_measure(tmp_path):
    """`gate_surface` needs a confirmed verify command, which preflight does not have yet.
    None is "nobody looked"; () would say "this repository defines no gate", which is a
    different and much stronger claim."""
    repo = make_repo(tmp_path)
    assert preflight.inspect_repo(repo).gate_surface is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `uvx pytest tests/test_forge_preflight.py -q`
Expected: `ModuleNotFoundError: No module named 'forge.preflight'`.

- [ ] **Step 3: Implement**

`inspect_repo` calls `inspect.repo_facts`, then `inspect.rejections(facts, selected)`, then `screen.screen_tree(repo, selected)` — **screen the selection, not the whole tree**; §2.3's scoping paragraph explains why an unscoped sweep aborts on artifacts the user never created.

**The containment check on the selection is new and belongs here**, not in `rejections`: a selected `../outside.txt` currently returns `[]` from `rejections` and then reaches the carried digest. Refuse an absolute path and any `..` component, lexically, matching `bundle._assert_contained` — and reuse that predicate rather than restating it if the import does not create a cycle; say which you did and why.

`refusals` is the single ordered tuple a caller acts on: `rejections`, then containment, then screen findings, then screen breaches. `gate_surface` stays `None` — preflight has no confirmed command.

- [ ] **Step 4: Update the two tripwires**

In `tests/test_forge_seams.py`, the test asserting `rejections` and `screen_tree` have zero consumers now fails. Rewrite it to assert what now holds: `preflight` is the consumer, and a repository `rejections` names is one `refusals` refuses. **Do not delete it** — its subject was that a policy nothing calls does not run, and the new subject is that the policy is now wired to the thing that stops a run.

- [ ] **Step 5: Wire the gate**

Add `tests/test_forge_preflight.py` to `FORGE_TESTS`. Run `uvx pytest tests/test_forge_packaging.py -q` and confirm the set-equality test passes.

- [ ] **Step 6: Mutate**

One site at a time, with a control: drop the `rejections` call; drop the containment check; screen the whole tree instead of the selection; return `()` instead of `None` for `gate_surface`; make `refusals` return only the first category.

- [ ] **Step 7: Render, gate, commit**

```bash
make render
git add shared/lib/forge/preflight.py tests/test_forge_preflight.py tests/test_forge_seams.py Makefile marketplaces
make verify
make precommit
git commit -m "feat(forge): preflight refuses what §2.3 says it must, and now something calls it"
```

---

### Task 3: Record the two §9 facts nothing records

**Files:**
- Modify: `shared/lib/forge/runstate.py`
- Test: `tests/test_forge_runstate.py` (extend)

**Interfaces:**
- Produces:
  - `runstate.snapshot_refs(repo, selected_paths, *, forge_refs=()) -> (protected_refs, status_digest)` — `forge_refs` is the exact names this run will create, recorded by name **and** OID.
  - `runstate.Manifest` gains `forge_refs: dict` and `index_digest: str`.
  - `runstate.drift` reports a forge ref that moved **off** its recorded OID.

**Why.** §9 whitelists forge's refs *"by exact ref name **and** the exact OID recorded at creation — a namespace whitelist would let a seat write into forge's own namespace invisibly."* Today's prefix filter is that namespace whitelist. And §9 lists the index hash among protected state; nothing digests `.git/index`, so `--assume-unchanged` on a path makes the user's own later edits to it **permanently** invisible to the drift check.

**The ordering problem to solve, not assume.** A run's forge refs are created *after* the manifest is written — `baseline.materialize` makes `refs/khenrix-forge/<run>/base`. So "recorded at creation" cannot mean "recorded in the t0 snapshot" for refs that do not exist yet. Decide what it does mean — a declared set of names whose OIDs are filled as each is created, or a second record — and **say why the shape you chose cannot let a seat's ref pass as forge's own.**

- [ ] **Step 1: Write the failing test**

```python
# tests/test_forge_runstate.py  (append)
def test_a_forge_ref_moving_off_its_recorded_oid_is_drift(tmp_path):
    """§9's whole point: the whitelist is by name AND OID, because a namespace whitelist
    lets a seat write into forge's own namespace invisibly."""
    repo = make_repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "update-ref", "refs/khenrix-forge/r1/base", head)
    refs, digest = runstate.snapshot_refs(repo, (), forge_refs=("refs/khenrix-forge/r1/base",))
    m = _manifest(repo, protected_refs=refs, status_digest=digest,
                  forge_refs={"refs/khenrix-forge/r1/base": head})
    assert runstate.drift(m, repo) == ()
    other = _git(repo, "commit-tree", "-m", "x", f"{head}^{{tree}}").stdout.strip()
    _git(repo, "update-ref", "refs/khenrix-forge/r1/base", other)
    assert "refs/khenrix-forge/r1/base" in runstate.drift(m, repo)


def test_a_forge_ref_the_run_never_declared_is_drift(tmp_path):
    """The namespace hole, closed: a seat creating `refs/heads/forge/r1/impostor` is not
    forge's ref because forge never declared it."""
    repo = make_repo(tmp_path)
    refs, digest = runstate.snapshot_refs(repo, (), forge_refs=())
    m = _manifest(repo, protected_refs=refs, status_digest=digest, forge_refs={})
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "update-ref", "refs/heads/forge/r1/impostor", head)
    assert runstate.drift(m, repo) != ()


def test_the_index_digest_moves_when_a_skip_bit_is_set(tmp_path):
    """§9 lists the index hash among protected state. Without it, `--assume-unchanged`
    makes the user's own later edits to that path permanently invisible."""
    repo = make_repo(tmp_path)
    m = _manifest(repo)
    _git(repo, "update-index", "--assume-unchanged", "seed.txt")
    assert "index" in runstate.drift(m, repo)


def test_the_index_digest_ignores_a_stat_only_refresh(tmp_path):
    """A refresh rewrites `.git/index` and changes nothing the user did. A digest that
    fired on it would report drift on every run and be edited around."""
    repo = make_repo(tmp_path)
    m = _manifest(repo)
    _git(repo, "update-index", "--refresh")
    assert "index" not in runstate.drift(m, repo)
```

**The last two tests are in tension and that is deliberate** — a raw `sha256` of `.git/index` fails the second. Find what distinguishes them and say what you measured; `ls-files --debug`, `ls-files -v`'s tag column, and the index's own extension records are the places to look. If no read-only derivation separates them, that is a finding: report it and pick the direction that fails **noisily** rather than silently, with the cost stated.

- [ ] **Step 2: Run to verify it fails**

Run: `uvx pytest tests/test_forge_runstate.py -q`
Expected: `TypeError: snapshot_refs() got an unexpected keyword argument 'forge_refs'`.

- [ ] **Step 3: Implement, then Step 4: mutate, Step 5: render, gate, commit**

```bash
make render
git add shared/lib/forge/runstate.py tests/test_forge_runstate.py marketplaces
make verify
make precommit
git commit -m "feat(forge): forge's refs by name and OID, and an index the drift check can see"
```

---

### Task 4: The cost quote

**Files:**
- Create: `shared/lib/forge/gate.py`
- Create: `tests/test_forge_gate.py`
- Modify: `Makefile` (`FORGE_TESTS`)

**Interfaces:**
- Produces:
  - `gate.Quote` — frozen: `provider_calls: int`, `ultrareview: str`, `setup_runs: int`, `verify_runs: int`, `peak_disk_gb: float`, `lines: tuple[str, ...]`.
  - `gate.quote(report, *, seats=3, attempts=3, review_rounds=2) -> Quote`.
  - `gate.provider_invoking_verify(repo, command) -> tuple[str, ...]` — the §5.2 detector.
  - `gate.GateError(RuntimeError)`.

**Why the quote is a refusal surface, not a display.** §5.2 quotes the worst case *honestly*: 3 builders × 3 attempts = 9, plus synthesis, plus review at `--retries 0` = **16**. Plus ultrareview's $5–25, calibration setup+verify ×2, builder setup, a fresh verifier setup+verify **per candidate**, synthesis verification after each fix, peak disk (three no-hardlink clones plus three dependency trees is plausibly 6–10 GB), and wall clock.

And §5.2's sharpest rule, which is about **this repository**: *"A verify command that transitively invokes a provider CLI is detected and refused."* On this repo, `make precommit` → `receipt_gate` → the documented remedy `make eval SKILL=<skill>` → `run_council` with real providers: **~24 provider calls per verify**, re-run fresh per candidate — two orders of magnitude above the quote. Preflight greps the resolved target for council/eval entry points and steers the operator to `make verify`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_forge_gate.py
def test_the_worst_case_is_quoted_not_the_happy_path():
    """§5.2: 3 builders × 3 attempts = 9, plus synthesis, plus review at --retries 0 = 16."""
    q = gate.quote(_report(), seats=3, attempts=3, review_rounds=2)
    assert q.provider_calls == 16, q.lines


def test_a_verify_command_that_invokes_a_provider_is_detected(tmp_path):
    """§5.2 on this repository: precommit → receipt_gate → `make eval` → run_council with
    real providers, ~24 calls PER VERIFY, re-run fresh per candidate."""
    repo = make_repo(tmp_path)
    write(repo, "Makefile",
          "precommit:\n\t@python3 scripts/receipt_gate.py\n"
          "eval:\n\t@python3 scripts/run_council.py\n")
    write(repo, "scripts/run_council.py", "# invokes claude, codex and agy\n")
    commit_all(repo, "gates")
    found = gate.provider_invoking_verify(repo, verify.Command.parse([["make", "precommit"]]))
    assert found, "a verify that spends provider calls must be named before the gate"


def test_a_plain_verify_command_is_not_flagged(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "Makefile", "verify:\n\t@pytest -q\n")
    commit_all(repo, "gates")
    assert gate.provider_invoking_verify(
        repo, verify.Command.parse([["make", "verify"]])) == ()


def test_ultrareview_is_quoted_as_money_not_as_a_count():
    """§13.1: $5–25 in usage credits, or one of three one-time free runs. A count would
    read as free."""
    q = gate.quote(_report())
    assert "$" in q.ultrareview


def test_the_quote_names_the_per_candidate_setup_it_cannot_avoid():
    """§6: one extra setup per candidate is essential, not optional hardening. A quote that
    hides it is a quote the operator will find wrong at 3am."""
    q = gate.quote(_report(), seats=3)
    assert q.setup_runs >= 5, q.lines   # calibration ×1, builders ×3, verifiers ≥1
```

Write `_report()` yourself as a minimal `preflight.Report` — the quote reads the repository's shape, not its contents.

- [ ] **Step 2: Run to verify it fails, Step 3: implement, Step 4: mutate, Step 5: wire `FORGE_TESTS`, Step 6: render, gate, commit**

```bash
make render
git add shared/lib/forge/gate.py tests/test_forge_gate.py Makefile marketplaces
make verify
make precommit
git commit -m "feat(forge): quote the worst case, and refuse a verify that spends providers"
```

---

### Task 5: The gate itself, and the manifest it writes

**Files:**
- Modify: `shared/lib/forge/gate.py`, `tests/test_forge_gate.py`, `tests/test_forge_seams.py`
- Test: itself

**Interfaces:**
- Produces:
  - `gate.Confirmation` — frozen: `setup: tuple[verify.Step, ...]`, `verify: tuple[verify.Step, ...]`, `on_calibration_failure: str` (`"abort"` | `"degraded"`), `strategy: str`, `accepted_gaps: tuple[str, ...]`.
  - `gate.confirm(report, quote, answers) -> Confirmation` — validates; raises `GateError`.
  - `gate.open_run(repo, report, confirmation, run_id) -> Path` — creates the run root, writes the manifest **once**, journals `confirmed`.
  - `gate.must_show(report) -> tuple[str, ...]` — everything a human must see before answering.

**What `must_show` carries, and why each is here rather than in a log.** Four items accumulated across three plans:

1. **An empty gate surface.** A repository whose gate the engine's rules cannot see produces `gate_surface == ()`, and a candidate can then gut the gate script and still earn a `PASS` whose reason says the surface was measured and unchanged. The verdict was made to qualify itself — the ruling was that displacing `PASS` would attribute to the candidate a fact about the engine — **on the condition that this is shown to a human once, before a token is spent.** That condition is this line.
2. **Refusals that the user may not override.** §2.3's list fails closed; `must_show` names them so the answer is not "why did it stop".
3. **The gaps the engine knows it has** — §9's remotes and configuration are not recorded; `--gc` is mandatory; the generator contract is empty for every repository the detector can read, so `PASS` requires a gate that rewrites no tracked file.
4. **The quote**, including the provider-invoking-verify finding.

**And the two policies §5 requires be answered once:** the calibration-failure policy (`abort` | `continue as degraded`) and the strategy rule to be applied later. §5 is explicit that these are asked **once** and not re-asked — *"Record the decision; do not ask again."*

- [ ] **Step 1: Write the failing test**

```python
# tests/test_forge_gate.py  (append)
def test_a_repository_whose_gate_cannot_be_seen_is_shown_to_the_human(tmp_path):
    """The condition the PASS ruling rests on: a qualified verdict is honest only if the
    operator was told once, before a token was spent, that the engine cannot see the gate."""
    repo = make_repo(tmp_path)          # no Makefile, no test files, no CI
    shown = gate.must_show(preflight.inspect_repo(repo))
    assert any("gate" in line for line in shown), shown


def test_the_confirmation_records_both_policies_or_refuses(tmp_path):
    """§5 asks once. A missing policy cannot be defaulted — the default IS the decision."""
    r, q = _report_and_quote(tmp_path)
    with pytest.raises(gate.GateError):
        gate.confirm(r, q, {"setup": [["true"]], "verify": [["true"]]})


def test_a_confirmed_run_writes_its_manifest_exactly_once(tmp_path):
    """§14.2: written once at `confirmed`, never rewritten, so commands are never
    re-detected. The second call is the resume that must not silently change the run."""
    r, q = _report_and_quote(tmp_path)
    c = gate.confirm(r, q, _answers())
    run = gate.open_run(r.repo, r, c, "r1")
    assert runstate.read_manifest(run).verify == c.verify
    with pytest.raises(runstate.ManifestError):
        gate.open_run(r.repo, r, c, "r1")


def test_a_refused_repository_never_reaches_a_manifest(tmp_path):
    """Preflight's refusals are not advice. A run that opens over them is a run whose
    manifest records an agreement about a repository the engine said it could not handle."""
    repo = make_repo(tmp_path)
    _git(repo, "update-index", "--skip-worktree", "seed.txt")
    r = preflight.inspect_repo(repo)
    with pytest.raises(gate.GateError):
        gate.open_run(repo, r, _confirmation(), "r1")


def test_the_gate_records_what_the_operator_accepted(tmp_path):
    """A gap the human was shown and accepted is a different fact from one nobody raised,
    and only the first belongs in a handover."""
    r, q = _report_and_quote(tmp_path)
    c = gate.confirm(r, q, _answers(accepted_gaps=["gate-surface-empty"]))
    assert "gate-surface-empty" in c.accepted_gaps
```

- [ ] **Step 2: Run to verify it fails, Step 3: implement**

`open_run` refuses when `preflight.refusals(report)` is non-empty — **before** creating the run root, so a refused run leaves nothing on disk. Then `storage.run_root`, `runstate.write_manifest` (write-once via `os.link`, so the second call raises `ManifestError` from the mechanism rather than a check), and a journal `confirmed` record.

- [ ] **Step 4: Write the seams**

```python
# tests/test_forge_seams.py  (append ABOVE the refusals banner)
def test_the_manifest_records_the_commands_the_gate_confirmed(tmp_path):
    """SEAM: `gate` and `runstate`. §5.1's four per-step fields survive the round trip, so a
    resume runs what the human agreed to rather than what a reader supplied."""


def test_a_repository_preflight_refuses_never_reaches_a_run_directory(tmp_path):
    """SEAM: `preflight` and `gate` and `storage`. The refusal is what stops the run, and it
    stops it before anything is written — measured by the run root not existing."""
```

Write both bodies against the real chain.

- [ ] **Step 5: Mutate, Step 6: sweep the prose, Step 7: render, gate, commit**

Sweep `shared/lib/forge/**` for prose this plan falsified — in particular anything saying `rejections` or `screen_tree` has no consumer, and the `_EDGES` comment Task 1 rewrote. **Check prose beside your additions, not only prose your changes touched**; that blind spot shipped a false sentence in five of the preceding plan's six waves.

```bash
make render
git add shared/lib/forge tests marketplaces
make verify
make precommit
git commit -m "feat(forge): one gate, asked once, recorded where a resume will read it"
```

---

## Self-review

**Spec coverage.** §14's amended graph → Task 1. §2.3's fail-closed list, scoped to the selection → Task 2. §3's secret screen before any provider starts → Task 2. §5 step 1's static read-only preflight → Task 2. §9's exact-name-plus-OID forge whitelist and the index hash → Task 3. §5.1's argv sequences with all four per-step fields → Tasks 4–5 (the shape is Plan F's; this plan is the first caller). §5.2's worst-case quote and the provider-invoking-verify refusal → Task 4. §5 step 2's ask-once, both policies and the strategy rule → Task 5. §14.2's write-once manifest at `confirmed` → Task 5.

**Deliberately out of scope, each with a later home:** launching seats and the phase machine driving a run (§4, §8); §12 strategy and fallback beyond recording the rule; §13 review and ultrareview beyond quoting it; §10's claim ledger; §16 handover; §18/§20 the skill and its evals. §5 steps 3–5 (calibrate, build, apply the strategy rule) are next: `calibrate` already exists and this plan records the policy it needs.

**What this plan does not close, stated rather than implied.** §9's **remotes and configuration** remain unrecorded — two of the seven protected items, outside both binding decisions, and `drift` cannot speak for either. `--gc` is mandatory and unbuilt. Nothing serializes `CandidateBundle`, so the manifest still carries no candidate. `PASS` never reads `baseline_run`. §6's chronology is enforced at one joint only, and this plan does not sequence it — Task 5 writes the agreement, not the run.

**Placeholder scan.** None. Task 3's index-digest tension is stated as a measurement to take rather than an answer to write, with the fail-noisy direction named if no derivation separates the two cases.

**Type consistency.** `preflight.Report` is consumed by `gate.quote`, `gate.must_show` and `gate.open_run` with the same field names throughout; `Confirmation.setup`/`verify` are `tuple[verify.Step, ...]`, matching `Manifest.setup`/`verify` exactly, so `open_run` performs no conversion — which is the property H2 established in the preceding plan and the reason `generator_contract` needed a second fix.

**One risk worth naming.** Task 2 makes `rejections` and `screen_tree` consumers, and both are guarded by tests asserting they have none. Those tests are the plan's own tripwires and rewriting them is correct — but a rewrite is exactly where a tripwire quietly becomes a tautology. Each rewritten test must assert the **new** property by measurement: that a repository `rejections` names is one `refusals` refuses, and that a screened secret stops a run. A test that merely asserts `preflight` imports `inspect` would pass forever and pin nothing.
