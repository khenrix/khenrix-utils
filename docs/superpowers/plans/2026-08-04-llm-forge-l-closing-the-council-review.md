# llm-forge Plan L — Closing the Council Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the verified path-escape, the two founding-premise violations that let a builder rig its own verification, and the roll-up defect that lets an unmeasured claim outrank a measured one — then make the certification layer capable of failing.

**Architecture:** Bands ordered by *how much evidence already exists* rather than by module. **L0** is four defects reproduced on this machine, needing no design decision — three inside forge's own state and one (L0.4) whose consequence leaves the machine. **L1** narrows the cluster where the builder can influence the check that judges it; it does **not** close that premise — L1.1 closes one route of six and the rest are deferred by name, because a plan that claimed otherwise would be a verdict reading cleaner than its evidence. **L2** replaces two hand-maintained copies of one predicate with a single exported one and makes a zero exit stop meaning "the claim holds". **L3** makes the eval set and the receipt able to fail. **L4** makes the quote structurally honest, and **L5** corrects SKILL.md last, against what L4 proves. Everything else is listed and deferred to Plan M.

**Tech Stack:** Python 3.11+ stdlib only; pytest; git plumbing (`ls-files -z`, `check-attr -z`, `cat-file`); Make.

## Global Constraints

- Python is **stdlib-only**. No pip dependencies. Must run on any Python 3.11+ machine with no install step.
- `make verify` is the gate; **`make precommit` is the commit boundary**. A skill change needs an eval receipt (CLAUDE.md, "Skill changes require evals").
- Commit directly to `main`. **Do not push.**
- Reconcile is non-destructive by design: it only adds missing entries or updates ones tagged `khenrix-managed`, and **never removes machine-specific config**. Preserve this invariant.
- The two binding rules: **FAIL CLOSED**, and **A VERDICT MUST NEVER READ CLEANER THAN ITS EVIDENCE**.
- The founding premise: **a check the builder could have rigged is not a check.**
- **Every new `tests/test_*.py` file** MUST be named in exactly one Makefile variable (`FORGE_TESTS`, `FORGE_SLOW_TESTS`, `COUNCIL_TESTS`, `DOCTOR_TESTS`, `AUDIT_TESTS`, `BATS_SUITES`) — never two. L0.3 widens the orphan gate to `test_*.py`, so this binds the non-forge suites this plan adds (`tests/test_checks_secrets.py`, `tests/test_eval_harness_receipt.py`) as well.

---

## Revisions — after a 3-of-3 council deep review of this plan

The plan was reviewed at `--mode deep` by all three seats before execution. **Its central claim
survived and the plan itself violated that claim three times.** Recorded here rather than quietly
patched, because the pattern is the point.

**The three self-inflicted instances, all now fixed:**

1. **L1.4's test was true by construction under the layout L1.4 itself chose.**
   `assert ledger.parent != root.parent` — with the ledger at `run_dir/ledger.json` and roots under
   `forge_root()/<name>-review`, those parents are **unequal no matter what the code does**. A test
   that cannot fail, in the task whose thesis is that such tests are the defect.
2. **L1.1's `assert moved` asked whether the function the task introduces does what the task
   introduced it to do.** It never asserted *which* path moved, and passes for any unrelated surface
   movement while the rigging route stays open.
3. **L2.1's third test asserted source text** via `inspect.getsource`. Breakable by inlining or
   renaming without violating the property; passable by writing `unmeasured = 0` in the body.

**Four factual errors in the plan, corrected:**

- **`remeasure_gate_surface` could not be implemented as written** — four API errors in five lines,
  all verified: `Verifier` has no `command` field; `_surface_state(root, paths)` takes paths, not a
  `Command`; `with_gate_measurement` lives in **`bundle`**, keyword-only with `surface=`/`delta=`; and
  **the baseline half of the delta is a local taken before `bundle.materialize` and never retained**,
  so the delta this plan specified is unrecoverable after setup.
- **The "Corrections to Plan K" framing mis-stated Plan K.** K already documents the sibling reach in
  its own text — *"in `run_dir/review/round-N/checkout` reaches `../../../ledger.json` … by ordinary
  relative path"* — and its C-table logs it as *"Inherited, not introduced — now **stated** rather than
  implied away,"* handing closure to Plan L. **This is an inheritance, not a correction.** Section
  renamed and rewritten.
- **`test_forge_verify.py:2030` will not fail.** It is
  `test_setup_that_only_touches_untracked_paths_is_clean`, which tests `run_setup`'s *overlap* check
  and never builds a gate surface. Its docstring is load-bearing — *"A rule over content alone would
  refuse every `.venv` and `node_modules` setup exists to create"* — so the instruction to "update it
  to assert `GATE_CHANGED`" **would have refused every virtualenv install in every repository.** The
  test that actually blesses the capability is `test_forge_runner.py:951`, which the plan cited in its
  *Why* and then omitted from its expected-failure list.
- **L4's claim was overstated.** It would have caught the unwired review loop and SKILL.md's quote.
  It would **not** have caught the oscillation gap — that is not a priced term.

**Scope corrections:**

- **L1.1 closes one route of six.** Re-measuring re-runs the *same* enumeration, so a gate resolved
  through `PATH`, a `make` recipe invoking a stub, interpreter-side rigging (`sitecustomize`, `.pth`,
  `PYTHONPATH`), a path `_command_paths` silently drops, and the symlink referent all survive. The
  Architecture note implying the four L1 tasks together close the premise was wrong; L1.1 is now
  scoped honestly and the rest are deferred **by name**.
- **L1.4 cannot close its finding at all** without an OS boundary. `forge_root()/<name>-review` is a
  sibling at a computable name, same UID, and reviewers have a shell. It also makes `storage.run_dirs`
  enumerate a phantom run. The move is **dropped**; the two-way containment assertion is kept, and the
  honest closure is a written admission in the same voice L1.6 uses for seats.
- **`SETUP_FAILED` must not enter `verify.OUTCOMES`.** That vocabulary's contract is *"every value
  `classify` can return"*, and adding a **runner** verdict breaks three totality tests —
  `test_forge_verify.py:1728` (AST-walks `verify.py`'s returns), `test_forge_strategy.py:411`
  (`classify_failure` raises by design on an unread outcome), `test_forge_rubric.py:135`
  (`GATE_RANK` is contiguous 0–5 with no free integer below `FAIL`). Now a runner-level sentinel.
- **L1.5 as written fires on every git-lfs repository**, after every provider call is paid, because
  `check-attr` reports the *effective* attribute and `filter=lfs` is declared in the **baseline's own**
  `.gitattributes`. Now compares seat against baseline and requires `filter.<name>.clean` to be defined
  in the **seat's** config.
- **L2.1 lost the empty-report guard.** `unmeasured(()) == ()` leaves an empty report rankable at
  `(0, 0)` — re-opening the very hole s3/s4 found. Also: `_lines` is a **two-way** dispatch that
  returns the *unresolved* list for any label but `"unsatisfied"`, and `Report` is a frozen four-field
  dataclass built **positionally at 12+ sites**. The third roll-up is now its own task.
- **L1.6 depended on L4 circularly** and burned a real eval receipt that four later tasks stale.
  **Moved to the end.**
- **L3.1 tested the adjacent finding** — generator-vs-fixture equality is s7 M2 (drift on eval *1*),
  not the false oracle on eval *0*. A generator that fabricates the sidecar and a fixture regenerated
  from it **agree perfectly while the oracle stays false.** Now drives `cli._sidecars_of` over a real
  baseline and asserts absence.
- **L3.2 created `tests/test_eval_harness_receipt.py` and never named it in the Makefile** — red under
  the gate L0.3 installs two tasks earlier. The Global Constraint said `test_forge_*.py` and no longer
  matches its own widened glob. Both fixed.

**One task added.** **L0.4 — §3's screen does not cover the baseline three cloud agents receive.**
This is the only finding in seven slices whose consequence is **irreversible and lands outside this
machine**, and the plan's own L0 rationale asserted no such finding was outstanding. It was not in the
deferral list under any name that identifies it.

**The deferral list was not honest.** Slice **s4 was absent entirely** — including three Highs that are
load-bearing for tasks this plan schedules (the `& 0o777` setuid mask under L1.4's own bracket; the
missing `kind` in `snapshot.diff` inside L1.5's own path; `GATE_RANK`'s ordering, which L1.2 must
renumber). s5 and s6 contributed nine Criticals and twelve Highs against five list entries. Rewritten
in full below.

---

## Why this order

The review produced 24 Critical and 40+ High findings. Sequencing them by severity alone would start with design arguments; sequencing by module would scatter fixes that share a root. This order is by **evidence already in hand**:

1. **L0** — three defects **reproduced on this machine**, each with a fix that already exists elsewhere in the same file. No design decision, no spec reading, no debate. They are also the only findings with a security consequence outside forge's own state.
2. **L1** — the founding premise. Four findings that are **one path**: the gate surface is measured before the candidate's setup runs, so setup can install the gate; a failed setup does not stop verification; harvest runs a builder-chosen filter whose output *replaces* the builder's bytes; and the seats are not isolated from each other. Fixing any one alone leaves the path open.
3. **L2** — one predicate, three copies. The fix must **remove copies, not add a fourth**.
4. **L3** — the certification layer, with the false oracle first because it currently grades a correct answer wrong.

**Deferred to Plan M** (listed at the end): the remaining Mediums and L-tier, reconcile's marker destruction, Fwork byte-binding, the control-plane tripwire, durable-state reconstruction, and §18's live smoke.

## What this plan inherits from Plan K

Plan K is written and **not yet executed**. Nothing below corrects it; one thing is inherited from it, and the earlier draft of this section got that wrong.

**K Task 4 already states the sibling reach, and hands closure here.** Its own text: *"in `run_dir/review/round-N/checkout` reaches `../../../ledger.json`, `../council/<name>.result.txt` and `../../../seats/<seat>/attempt-N` by ordinary relative path."* Its C-table logs it as **H1 — "Inherited, not introduced — now *stated* rather than implied away."** K Task 4 was written for the linked-worktree `.git` hazard and closes that; the ledger reach is an inheritance it declined to hide.

**And it cannot be closed by moving the clone.** `forge_root()/<name>-review` is a sibling at a computable name, owned by the same UID, mode 0700 — and reviewers run as that UID with a shell. `ls ..`, a glob over `$XDG_STATE_HOME/khenrix-forge/*/ledger.json`, or `find` all reach it. **There is no permission boundary anywhere in this design**, which is exactly what L1.6 admits about seats. The same admission defeats the reviewer case. It would also make `storage.run_dirs` — which enumerates every directory under `forge_root()` whose name starts with `run_digest(repo) + "-"` — report a phantom run `<run_id>-review` to `--gc all`.

So L1.4 keeps the half that is real today (containment asked **both** ways) and states the rest, rather than shipping a speed bump described as a boundary. **Nothing in Plan K needs amending.**

**Plan I₂'s oscillation wiring was specified and never scheduled.** I₂ Task 2 states verbatim: *"Task 5 calls `cap_remaining` and `oscillation`; Task 6 calls `cap_remaining`."* Measured: Plan K contains **zero** occurrences of either name, and in `shared/lib/forge/` — excluding `progress.py` and tests — `oscillation`, `from_runs` and `compare` have **0** production callers while `cap_remaining` has 3 and both writers are wired. `review.py:1700-1703` hard-codes `prog=progress.Progress(None, None)`, so every sighting is unmeasured by construction. The root cause is the injected `fix` contract at `review.py:1629`, which returns `(new_checkpoint | None, verified: bool)` — a boolean where `from_runs` needs the candidate and baseline `Run`s. **This plan records the deferral explicitly (Plan M, item 5) rather than widening the contract mid-band**, because the contract change touches Plan K Tasks 3 and 4 and must land with them.

---

## File structure

| File | Responsibility in this plan |
|---|---|
| `shared/lib/forge/storage.py` | L0.1 — validate `run_id` as one path component before any filesystem call. |
| `scripts/lib/checks.py` | L0.2 — NUL-delimited `ls-files`, index-blob namespace, explicit failure on unresolvable names. L3.2 — receipt closure. |
| `Makefile` | L0.3 — name `tests/test_mutate.py`. |
| `tests/test_forge_packaging.py` | L0.3 — widen the orphan-suite glob. L4 — the quote-reachability test. |
| `shared/lib/forge/verify.py` | L1.1 — re-measure the gate surface after setup. L1.2 — refuse a nonzero verifier setup. L1.3 — calibration takes the hooks read. |
| `shared/lib/forge/runner.py` | L1.1/L1.2 — consume the post-setup surface; stop on setup failure; rename `Status.setup`'s source. |
| `shared/lib/forge/harvest.py` | L1.5 — refuse a harvest when any custom clean filter is active. |
| `shared/lib/forge/review.py` | L1.4 — review clone outside `run_dir`; both-direction containment. |
| `shared/lib/forge/coverage.py` | L2.1 — export the "did a predicate run" predicate; third roll-up. L2.2 — real pytest receipt. |
| `shared/lib/forge/rubric.py`, `strategy.py` | L2.1 — **delete** their copies of the predicate and import the exported one. |
| `evals/llm-forge/evals.json`, `fixtures/` | L3.1 — correct the false oracle. |
| `scripts/eval_harness.py` | L3.2 — typed receipt with test counts. L3.3 — `parse_comparison` three-state. |
| `shared/skills/llm-forge/SKILL.md` | L1.6 — correct the isolation and cost claims. |

---

## Task L0.1: `run_id` is a path component, not a path

**Files:**
- Modify: `shared/lib/forge/storage.py:228-244`
- Test: `tests/test_forge_storage.py`

**Interfaces:**
- Consumes: `storage.forge_root() -> Path`, `storage.run_digest(repo_path) -> str`, `storage.StorageError`
- Produces: `storage._RUN_ID` (compiled pattern), and `run_root` raising `StorageError` on any `run_id` that is not a single safe component.

**Why:** MEASURED on this machine in an isolated `XDG_STATE_HOME` —

```
forge_root()                     : …/state/khenrix-forge
run_root(repo, "x/../../victim") : …/state/khenrix-forge/4c14cb5b26ec-x/../../victim
resolved                         : …/state/victim
ESCAPED forge_root : True    exists : True    mode : 0o700
```

A directory **created outside the state root and chmod'd 0700**, reachable from argv at `cli.py:593` (`storage.run_root(repo, args.collect, must_be_new=False)`) before any manifest is validated. `gc` takes the same path. `run_root`'s own docstring says both read-only callers `rmdir` afterwards *"which only ever succeeds while it is empty"* — so **if the escape target has contents, the directory and its new mode persist.**

The fix already exists **twelve lines below**: `seat_state_path` validates against `_SEAT_NAME` because *"it becomes a filename inside the run directory, so anything else can put a run's state where nothing accounts for it."* That argument is verbatim true of `run_id`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_forge_storage.py`:

```python
def test_a_run_id_that_is_not_one_component_cannot_reach_outside_the_state_root(tmp_path, monkeypatch):
    """The external question: can this argument name a directory forge_root does not contain?

    Not "does the regex match" — that would restate the implementation. This asserts the
    property the regex exists to buy, so a future rewrite that keeps the property passes.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    for hostile in ("x/../../victim", "../victim", "a/b", "/abs", ".", "..", "", "x/"):
        with pytest.raises(storage.StorageError) as e:
            storage.run_root(tmp_path, hostile, must_be_new=True)
        assert "run id" in str(e.value)
    assert not (tmp_path / "state" / "victim").exists(), \
        "a refused run id must not leave a directory behind"


def test_a_run_root_is_always_an_immediate_child_of_forge_root(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    p = storage.run_root(tmp_path, "a1b2c3", must_be_new=True)
    assert p.resolve().parent == storage.forge_root().resolve()
    assert p.stat().st_mode & 0o7777 == 0o700


def test_the_run_id_rule_admits_what_new_run_id_actually_draws(tmp_path, monkeypatch):
    """A validator that refuses the engine's own ids would be found in production, not here."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    for _ in range(50):
        storage.run_root(tmp_path, storage.new_run_id(), must_be_new=True)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m pytest tests/test_forge_storage.py -k run_id -v`
Expected: FAIL — `run_root` currently creates `…/state/victim` and raises nothing.

- [ ] **Step 3: Implement**

In `shared/lib/forge/storage.py`, beside `_SEAT_NAME`:

```python
# THE SAME RULE `_SEAT_NAME` STATES, FOR THE COMPONENT ONE LEVEL UP. A run id is drawn by
# `new_run_id` as six hex characters, but `cli.collect` and `gc.collect` take it OFF THE
# COMMAND LINE — and `run_root` interpolates it, creates parents, and chmods the result. An
# id of "x/../../victim" therefore created and chmod-0700'd a directory outside `forge_root()`;
# measured 2026-08-04. `rmdir` cleanup does not undo it, because `rmdir` only succeeds while
# the target is empty and an escape usually lands on something that is not.
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
```

Then make `run_root`'s first statement:

```python
    if not isinstance(run_id, str) or not _RUN_ID.match(run_id):
        raise StorageError(
            f"a run id is one path component of letters, digits, '-' and '_' (64 max), "
            f"starting alphanumeric: {run_id!r}. It is interpolated into a directory name "
            "under the forge state root, so anything else can create and chmod a directory "
            "outside it — and the `rmdir` both read-only callers use to clean up only ever "
            "succeeds while the target is empty.")
    p = forge_root() / f"{run_digest(repo_path)}-{run_id}"
    if p.resolve().parent != forge_root().resolve():
        raise StorageError(
            f"run id {run_id!r} resolves outside the forge state root ({p.resolve()}); "
            "refusing to create it.")
    p.mkdir(mode=0o700, parents=True, exist_ok=not must_be_new)
    p.chmod(0o700)   # mkdir's mode is masked by umask; chmod is not
    return p
```

Add `import re` if absent.

- [ ] **Step 4: Run the tests**

Run: `python3 -m pytest tests/test_forge_storage.py -v`
Expected: PASS, all tests in the file.

- [ ] **Step 5: Check no caller relied on the old laxity**

Run: `python3 -m pytest tests/test_forge_cli.py tests/test_forge_gc.py -v`
Expected: PASS. If a fixture used a run id with a `/` or a leading dot, fix the fixture — it was relying on the escape.

- [ ] **Step 6: Commit**

```bash
git add shared/lib/forge/storage.py tests/test_forge_storage.py
git commit -m "fix(forge): a run id names one directory, and the rule was already written one function below"
```

---

## Task L0.2: the secret scanner reads names git did not give it

**Files:**
- Modify: `scripts/lib/checks.py:148-171`
- Test: `tests/test_checks_secrets.py` (create)

**Interfaces:**
- Consumes: `SECRET_FAIL`, `SECRET_ALLOW_SHA`, `SCAN_SKIP_SUFFIX`, `SCAN_SKIP_DIRS`, `SCAN_SKIP_PATHS`
- Produces: `scan_secrets(root: Path) -> list[str]` — unchanged signature, now covering the index namespace and refusing unresolvable names.

**Why:** MEASURED on a fresh repository —

```
git ls-files       -> "caf\303\251.txt"    (quoted, escaped display form)
open that literal  -> FileNotFoundError    -> treated as "deleted", scan PASSES
git ls-files -z    -> café.txt             (the real name)
```

**A tracked file with a non-ASCII name holding a live secret is silently skipped by the pre-commit gate that guards this repository.** No adversary required — any repo with accented filenames.

The docstring's errno argument is **correct for the case it contemplates** (*"a tracked file deleted from the working tree … has no working-tree secret to leak"*) and the quoting bug admits a case it never contemplated: a **present** file whose name git escaped. This is the house pattern — a sound argument, and a route that walks around it.

Second namespace gap: stage a file containing a token, then clean the working-tree copy without staging the cleanup. **The index still carries the secret that will be committed**; the scanner reads the clean working tree and passes.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_checks_secrets.py`:

```python
"""The external question for a secret scanner: over which bytes is its emptiness a claim?

Not "does the regex match" — `checks.py --self-test` already pins that. These ask whether
a file that IS there and DOES hold a secret can leave the same record as a clean tree.
"""
import subprocess, sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))
import checks

LIVE = "AKIA" + "Q7ZB3KXJ2M9WLPRT"      # matches AKIA[0-9A-Z]{16}, not in SECRET_ALLOW_SHA


def _repo(tmp_path):
    subprocess.run(["git", "init", "-q", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    return tmp_path


def test_a_tracked_file_whose_name_git_quotes_is_still_scanned(tmp_path):
    r = _repo(tmp_path)
    (r / "café.txt").write_text(f'AWS_KEY = "{LIVE}"\n')
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    problems = checks.scan_secrets(r)
    assert any("café.txt" in p for p in problems), (
        "git ls-files prints \"caf\\303\\251.txt\" without -z; opening that literal raises "
        "FileNotFoundError, which the ENOENT branch treats as a harmless deletion")


def test_a_secret_staged_but_cleaned_from_the_working_tree_is_still_found(tmp_path):
    r = _repo(tmp_path)
    (r / "conf.py").write_text(f'KEY = "{LIVE}"\n')
    subprocess.run(["git", "add", "conf.py"], cwd=r, check=True)
    (r / "conf.py").write_text("KEY = os.environ['KEY']\n")     # cleaned, NOT staged
    problems = checks.scan_secrets(r)
    assert any("conf.py" in p for p in problems), (
        "the index holds the bytes that will be committed; a clean working tree is not a "
        "clean commit")


def test_a_name_that_cannot_be_resolved_in_either_namespace_is_a_failure_not_a_skip(tmp_path):
    r = _repo(tmp_path)
    (r / "gone.txt").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    subprocess.run(["git", "commit", "-qm", "x"], cwd=r, check=True)
    (r / "gone.txt").unlink()                                    # deleted, not staged
    problems = checks.scan_secrets(r)
    assert problems == [] or all("NOT SCANNED" not in p for p in problems), (
        "a genuinely deleted working-tree file still has index bytes and must be scanned "
        "from the index, not skipped")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m pytest tests/test_checks_secrets.py -v`
Expected: FAIL on the first two — the quoted name is skipped, the staged secret is missed.

- [ ] **Step 3: Implement**

Replace `scan_secrets`'s body (keep the docstring, add the paragraph below):

```python
    # TWO NAMESPACES, BECAUSE A COMMIT SHIPS THE INDEX AND NOT THE WORKING TREE. The
    # working-tree read alone missed a token staged and then cleaned without staging the
    # cleanup. And `-z`: without it `git ls-files` prints a QUOTED, C-escaped DISPLAY form
    # for any name outside plain ASCII ("caf\303\251.txt"), and opening that literal raised
    # FileNotFoundError, which the ENOENT branch below reads as an ordinary deletion — so a
    # tracked "café.txt" holding a live key was never scanned. Measured 2026-08-04.
    out = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True,
                         check=True).stdout
    files = [b.decode("utf-8", "surrogateescape") for b in out.split(b"\0") if b]
    problems = []
    for rel in files:
        if rel.endswith(SCAN_SKIP_SUFFIX) or any(rel.startswith(d) for d in SCAN_SKIP_DIRS):
            continue
        if rel in SCAN_SKIP_PATHS:
            continue
        sources = []
        try:
            sources.append(("working tree", (root / rel).read_text(errors="ignore")))
        except FileNotFoundError:
            pass                        # no working-tree bytes; the index read below still runs
        except OSError as e:
            problems.append(
                f"{rel}: NOT SCANNED for secrets ({type(e).__name__}: "
                f"{e.strerror or e}) — this file is tracked and its bytes were never read, "
                f"so `make verify` cannot certify it. Make it readable and re-run.")
            continue
        blob = subprocess.run(["git", "cat-file", "-p", f":{rel}"], cwd=root,
                              capture_output=True)
        if blob.returncode == 0:
            sources.append(("index", blob.stdout.decode("utf-8", "ignore")))
        if not sources:
            problems.append(
                f"{rel}: NOT SCANNED for secrets — `git ls-files` names it, but it has "
                f"neither working-tree bytes nor an index blob. Emptiness here would be a "
                f"clean bill of health over a file nobody read.")
            continue
        for where, text in sources:
            hit = False
            for rx in SECRET_FAIL:
                m = rx.search(text)
                if m and hashlib.sha256(m.group(0).encode()).hexdigest() not in SECRET_ALLOW_SHA:
                    problems.append(f"{rel} ({where}): matches secret pattern /{rx.pattern[:20]}…/")
                    hit = True
                    break
            if hit:
                break
    return problems
```

- [ ] **Step 4: Run the new tests**

Run: `python3 -m pytest tests/test_checks_secrets.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Run the scanner against this repository**

Run: `python3 scripts/lib/checks.py --self-test`
Then: `make verify`
Expected: both green. If `make verify` now reports a secret it did not before, **read it before assuming a false positive** — the index namespace is genuinely new coverage.

- [ ] **Step 6: Add the suite to the Makefile**

In `Makefile`, add `tests/test_checks_secrets.py` to `COUNCIL_TESTS` (it is fast and has no clone fleet).

- [ ] **Step 7: Commit**

```bash
git add scripts/lib/checks.py tests/test_checks_secrets.py Makefile
git commit -m "fix(checks): the scanner read names git never gave it, and only half the bytes a commit ships"
```

---

## Task L0.3: a suite nothing runs leaves the record of a suite that passes

**Files:**
- Modify: `Makefile:17-46`, `tests/test_forge_packaging.py:79-101`

**Why:** MEASURED by set arithmetic over `COUNCIL_TESTS ∪ FORGE_TESTS ∪ FORGE_SLOW_TESTS ∪ DOCTOR_TESTS ∪ AUDIT_TESTS ∪ BATS_SUITES` against `tests/test_*.{py,bats}`: **42 on disk, 41 named, orphan = `tests/test_mutate.py`, 13 tests.**

`test_every_forge_suite_is_named_in_the_makefile_gate` is the gate for exactly this class and its own docstring says *"it has happened in this package before."* **Its glob is `test_forge_*.py`, and `test_mutate.py` falls one character outside it.** The 13 orphaned tests are the ones pinning `mutate.py`'s false-CAUGHT guards.

- [ ] **Step 1: Write the failing test**

Replace the body of `test_every_forge_suite_is_named_in_the_makefile_gate` (keep its docstring, add the final paragraph):

```python
    THE GLOB IS `test_*.py` AND NOT `test_forge_*.py`. The narrower one let
    `tests/test_mutate.py` — 13 tests pinning the mutation tool's false-CAUGHT guards — sit
    in no variable at all, run in no target, and stay invisible to the very gate written to
    catch that. One character of scope was the whole defect.
    """
    on_disk = ({f"tests/{p.name}" for p in (ROOT / "tests").glob("test_*.py")}
               | {f"tests/{p.name}" for p in (ROOT / "tests").glob("test_*.bats")})
    named = set()
    for var in ("FORGE_TESTS", "FORGE_SLOW_TESTS", "COUNCIL_TESTS",
                "DOCTOR_TESTS", "AUDIT_TESTS", "BATS_SUITES"):
        named |= _make_variable(var)
    assert named == on_disk, (
        f"unnamed suites run in no target: {sorted(on_disk - named)}; "
        f"stale names point at files that no longer exist: {sorted(named - on_disk)}")
    fast, slow = _make_variable("FORGE_TESTS"), _make_variable("FORGE_SLOW_TESTS")
    assert fast & slow == set(), \
        "a suite in both variables is run twice and gated by neither list alone"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 -m pytest tests/test_forge_packaging.py::test_every_forge_suite_is_named_in_the_makefile_gate -v`
Expected: FAIL — `unnamed suites run in no target: ['tests/test_mutate.py']`.

- [ ] **Step 3: Name the suite**

In `Makefile`, append `tests/test_mutate.py` to `COUNCIL_TESTS` (it is fast; `mutate.py` spawns subprocesses but no clone fleet).

- [ ] **Step 4: Run the gate and the recovered suite**

Run: `python3 -m pytest tests/test_forge_packaging.py -v`
Expected: PASS.
Run: `python3 -m pytest tests/test_mutate.py -v`
Expected: **13 passed.** If any fail, that is a real regression the orphaning hid — fix it in this task and say so in the commit message.

- [ ] **Step 5: Commit**

```bash
git add Makefile tests/test_forge_packaging.py
git commit -m "fix(tests): the orphan-suite gate globbed test_forge_*, and the orphan was test_mutate"
```

---

## Task L0.4: the screen answers `()` over the baseline three cloud agents receive

**Files:**
- Modify: `shared/lib/forge/preflight.py:181`, `:57-65`; `shared/lib/forge/gate.py:933-943`, `:1199-1256`
- Test: `tests/test_forge_preflight.py`, `tests/test_forge_gate.py`

**Why this is in L0 and not deferred.** Of 24 Criticals, this is **the only one whose consequence is
irreversible and lands outside this machine**: a live credential shipped to three cloud CLIs. An earlier
draft of this plan claimed L0's three tasks *"are the only findings with a security consequence outside
forge's own state"* — **false while this was deferred**, and it was not in the deferral list under any
name that identified it.

Two defects. MEASURED for the first:

```
screen_tree(root, [])              -> ([], [])   ← identical to a clean repository
screen_tree(root, ["settings.py"]) -> [Finding('settings.py', 1, 'AKIA[0-9A-Z]{16}')]
```

`cli.py:177` passes `args.select or ()`, so **the empty selection is the default path**, and
`refusals()` then returns the value a fully-screened clean repo returns. Nothing on `Report`
distinguishes them. Meanwhile `baseline.py:356` puts **every tracked file** into B₁.

Second, from the recovered codex seat: **TOCTOU.** `open_run` reuses the earlier report
(`gate.py:1585`); baseline construction guards **only the real index hash** (`baseline.py:321`) and
then re-reads current bytes. **An unstaged edit, or a new file under a selected directory, does not
change that index hash** — so a background process creating `scratch/.env` between preflight and
`open_run` puts a credential in B₁ **with no scan.**

The engine **already built this mechanism one field over**: `GATE_SURFACE_EMPTY` exists because an
empty §6.1 surface would let a `PASS` read as "measured and unchanged", ruling *"on the condition that
you are told once, before a token is spent."* §3's screen has higher stakes by its own docstring —
*"Scanning the OUTPUT is too late"* — and got no condition, no gap id, no line.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_run_that_screened_nothing_does_not_report_what_a_clean_repo_reports(tmp_path):
    """The external question: over how many files is this emptiness a claim?"""
    repo = _repo_with_tracked_secret(tmp_path)
    rep = preflight.inspect_repo(repo, ())          # the DEFAULT path: no --select
    assert rep.screened == 0
    assert any("screen" in line.lower() for line in gate.must_show(rep, _quote(), _cmd())), \
        "a price was shown over an unscreened baseline and no line said so"


def test_the_screen_covers_what_B1_actually_carries(tmp_path):
    """baseline.py:356 puts every TRACKED file in B1; the screen read the selection."""
    repo = _repo_with_tracked_secret(tmp_path)      # secret is tracked, nothing selected
    assert preflight.refusals(preflight.inspect_repo(repo, ())), \
        "a tracked credential reached B1 without entering the scanner"
```

- [ ] **Step 2: Run to verify they fail.** Expected: `AttributeError: 'Report' has no 'screened'`, then an empty `refusals()`.

- [ ] **Step 3: Implement** — add `screened: int` to `preflight.Report`; screen the **tracked set plus the selection** rather than the selection alone; add a `SCREEN_SCOPE` gap id beside `GATE_SURFACE_EMPTY` and one `must_show` line naming the count. **Either half stands alone** — ship both.

- [ ] **Step 4: Run the tests**, then `make verify`.

- [ ] **Step 5: Record the TOCTOU half as deferred** in `preflight.py:57-65`'s docstring: the index hash does not move for an unstaged edit, so a file created between preflight and `open_run` is force-added unscanned. Closing it means screening the **content-addressed B₁ path set after B₁ is built** — Plan M item 1.

- [ ] **Step 6: Commit**

```bash
git add shared/lib/forge/preflight.py shared/lib/forge/gate.py tests/
git commit -m "fix(forge): the screen answered () over a baseline it never opened, and the gate said nothing"
```

---

## Task L1.1: the gate surface is measured before the candidate's setup runs

**Files:**
- Modify: `shared/lib/forge/verify.py` (`build_verifier`, `_surface_state`), `shared/lib/forge/runner.py:1105-1145`
- Test: `tests/test_forge_verify.py`, `tests/test_forge_runner.py`

**Interfaces:**
- Produces: `verify.remeasure_gate_surface(v: Verifier) -> Verifier` — returns a `Verifier` whose `candidate` carries a **post-setup** `gate_surface`/`gate_delta`, computed against the same calibration surface.

**Why:** `runner.py`'s own comment sits between `build_verifier` and `run_setup`:

> `# THE FIRST MEASUREMENT EXISTS NOW. §6.1's gate_delta/gate_surface are on 'v.candidate' from this line onward`

`run_setup` is **eleven lines below**, and the comment at `verify.assert_hooks_pinned(v)` says that call is *"the only point in the run where the confirmed setup command has already executed in the candidate's tree and the gate has not yet started."* **The only post-setup integrity check is `core.hooksPath`.**

So: confirmed setup `./bootstrap.sh`, confirmed verify `.venv/bin/pytest`. The candidate edits `bootstrap.sh` to create a fake `.venv/bin/pytest` that exits 0. At measurement time that binary **does not exist**; `bootstrap.sh` is named by neither the verify command nor the gate-file heuristics; no tracked path changes; `core.hooksPath` is intact; `fixed_point` sees no delta → **PASS**.

**The test that should have caught this proves the general capability and then asserts one instance of it.** `test_forge_runner.py:951` proves a candidate can rewrite the setup entrypoint — and checks exactly one resulting mutation, `core.hooksPath`. `test_forge_verify.py:2030` *deliberately declares* installing `.venv/bin/pytest` "clean".

- [ ] **Step 1: Write the failing test**

Append to `tests/test_forge_verify.py`:

```python
def test_a_gate_binary_that_setup_creates_is_inside_the_measured_surface(tmp_path):
    """The external question: what ELSE can the proven capability do?

    test_forge_runner.py:951 proves a candidate can rewrite the setup entrypoint, then
    asserts one consequence (core.hooksPath). This asserts the consequence that matters:
    the entrypoint can write the gate itself.
    """
    v = _verifier_with(tmp_path, setup=[["./bootstrap.sh"]], verify=[[".venv/bin/pytest"]])
    before = set(v.candidate.gate_surface)
    assert ".venv/bin/pytest" not in {str(p) for p in before}, \
        "precondition: the runner does not exist when the surface is first measured"

    (v.path / "bootstrap.sh").write_text(
        "#!/bin/sh\nmkdir -p .venv/bin\nprintf '#!/bin/sh\\nexit 0\\n' > .venv/bin/pytest\n"
        "chmod +x .venv/bin/pytest\n")
    (v.path / "bootstrap.sh").chmod(0o755)
    verify.run_setup(v, verify.Command([["./bootstrap.sh"]]), env={})

    v2 = verify.remeasure_gate_surface(v, command=verify.Command([[".venv/bin/pytest"]]))
    moved = set(v2.candidate.gate_delta or ())
    assert ".venv/bin/pytest" in moved, (
        f"the confirmed verify command's OWN binary was created by candidate-owned setup and "
        f"is not in the delta ({sorted(moved)}); the clone is fresh and the gate is not "
        "independent")
    outcome, _ = verify.classify(_fixed_point_exit0(v2), _green_calibration(), v2.candidate)
    assert outcome == verify.GATE_CHANGED, (
        "a moved surface that still classifies PASS has measured nothing an operator can act on")
```

**Why this assertion and not `assert moved`:** the earlier draft asserted only that the delta was
non-empty — which is satisfied by *any* surface movement, including a setup that touches an unrelated
gate file while the rigging route stays open, and which asks nothing more than "does the function this
task introduces do what this task introduced it to do." Naming the path and pinning the *outcome* asks
the external question: **can a candidate author the binary its own gate runs and still be told PASS?**

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 -m pytest tests/test_forge_verify.py -k gate_binary_that_setup_creates -v`
Expected: FAIL with `AttributeError: module 'verify' has no attribute 'remeasure_gate_surface'`.

- [ ] **Step 3: Implement `remeasure_gate_surface`**

**Four API facts the earlier draft of this task got wrong — all verified in the tree:** `Verifier`
has fields `path, candidate, contract, baseline_surface, candidate_surface` and **no `command`**;
`_surface_state(root, paths)` takes **path names**, not a `Command`; `with_gate_measurement` lives in
**`bundle`** and is **keyword-only** (`bundle.with_gate_measurement(candidate, *, surface, delta)`);
and `before = _surface_state(seat.path, baseline_surface)` at `verify.py:919` is a **local taken
before `bundle.materialize`** that `Verifier` never retains.

That last one is structural: **the pre-materialize state cannot be re-derived after setup has run.**
So `build_verifier` must persist it. Add one field to `Verifier` — its docstring forbids *defaults*,
not fields — and pass it from the existing local:

```python
# In verify.py, on Verifier:
    baseline_state: dict     # `_surface_state(path, baseline_surface)` BEFORE materialize.

# In build_verifier, reusing the local it already computes at :919:
    return Verifier(path=seat.path, ..., baseline_state=before)
```

Then, beside `build_verifier`:

```python
def remeasure_gate_surface(v: "Verifier", *, command: "Command") -> "Verifier":
    """§6.1's surface, ENUMERATED AND READ AGAIN after the confirmed setup command has run.

    THE FIRST READ CANNOT SEE WHAT SETUP CREATES, AND SETUP IS CANDIDATE-OWNED. `build_verifier`
    measures before the candidate's setup executes, so a verify command naming `.venv/bin/pytest`
    is measured while that path does not exist — and a candidate that edits the setup entrypoint
    to write a stub there moves nothing tracked, leaves `core.hooksPath` alone, and receives PASS
    over a gate it authored.

    THE ENUMERATION IS RE-RUN, NOT REUSED. `gate_surface` resolves the command's paths against
    the tree, and the whole point is that the tree changed; reusing `v.candidate_surface` would
    ask the pre-setup question again.

    WHAT THIS DOES NOT CLOSE, because the re-read runs the SAME enumeration: a gate resolved
    through PATH (`verify = [["pytest"]]` with setup prepending `.venv/bin`); a `make` recipe
    invoking a stub, where `_command_paths` yields only the Makefile; interpreter-side rigging
    (`sitecustomize.py`, a `.pth`, `PYTHONPATH`, a `-p` plugin) — the language-indirection gap
    `verify.py:1953` already admits; a token `_command_paths` silently drops as uncontained, which
    is absent from BOTH reads so the delta is `()` twice; and a symlinked gate whose referent is
    rewritten, since `_surface_state` hashes only target text. Those are Plan M items 2 and 3.
    """
    after_surface = gate_surface(v.path, v.contract, command=command)
    return replace(
        v,
        candidate_surface=after_surface,
        candidate=bundle.with_gate_measurement(
            v.candidate,
            surface=tuple(sorted(set(v.baseline_surface) | set(after_surface))),
            delta=_gate_delta(v.baseline_state, _surface_state(v.path, after_surface))))
```

- [ ] **Step 4: Call it in the runner**

In `shared/lib/forge/runner.py`, replace the **existing** `on_measurement` call in the `if setup.steps:`
branch (`:1125-1127`) — do not add a second one, and **do not touch the `else` branch**, whose comment
records that handing nothing over there is deliberate:

```python
    if setup.steps:
        setup_result = verify.run_setup(v, setup, env=child_env)
        # §6.1 RE-READ, for the same reason `assert_hooks_pinned` is re-read here: this is the
        # only window in which candidate-owned setup has executed and the gate has not started.
        # `core.hooksPath` was the one post-setup fact this checked, and a stub interpreter
        # written into `.venv/bin/` by the setup entrypoint moves neither it nor any tracked path.
        v = verify.remeasure_gate_surface(v, command=command)
        # BEFORE `assert_hooks_pinned`, which is the step the adversarial rig is refused at.
        if on_measurement is not None:
            on_measurement(v.candidate, setup_result)
```

- [ ] **Step 5: Run the tests**

Run: `python3 -m pytest tests/test_forge_verify.py tests/test_forge_runner.py -v`
Expected: PASS, **and `test_forge_verify.py:2030` stays green.**

**Do not touch `test_forge_verify.py:2030.`** An earlier draft of this task predicted it would fail and
instructed rewriting it. That was wrong: it is
`test_setup_that_only_touches_untracked_paths_is_clean`, it exercises `run_setup`'s **overlap** check,
it never builds a gate surface — and its docstring is load-bearing: *"A rule over content alone would
refuse every `.venv` and `node_modules` setup exists to create."* Following the old instruction would
have **refused every virtualenv install in every repository**.

The test that blesses the capability this task closes is **`test_forge_runner.py:951`** — it proves a
candidate can rewrite the setup entrypoint and then asserts exactly one consequence (`core.hooksPath`).
Extend it with the gate-binary consequence; do not delete it.

- [ ] **Step 6: Commit**

```bash
git add shared/lib/forge/verify.py shared/lib/forge/runner.py tests/test_forge_verify.py tests/test_forge_runner.py
git commit -m "fix(forge): the gate surface was measured before the candidate's setup could write the gate"
```

---

## Task L1.2: a failed verifier setup must not produce a PASS

**Files:**
- Modify: `shared/lib/forge/runner.py:1122-1145`, `:660-670`, `:900-950`
- Test: `tests/test_forge_runner.py`

**Why:** `runner.py:1122` **never inspects `setup_result.run.exit_code`** and always proceeds to the gate. A nonzero setup becomes **prose** via `_with_setup_caveat` while the outcome stays `PASS` → `status.verify="pass"`.

Compounded: `seat.py:4` describes `setup` as an independent measurement **from a fresh clone**; `runner.py:667` sets `Status.setup` from setup **in the builder clone, before the agent runs**. So the record reads:

```
setup=pass   verify=pass   forge=completed   verifier_setup.exit_code=3
```

**The builder's setup passing in the clone §6 says cannot be trusted overrides the verifier's setup failing in the clone that can.** `test_forge_runner.py:847` and `:1527` both bless this.

- [ ] **Step 1: Write the failing test**

```python
def test_a_verifier_whose_setup_failed_cannot_report_pass(tmp_path):
    """A caveat in prose does not repair a field that downstream code branches on."""
    outcome, reason, v, sr = _verify_with(tmp_path, setup_exit=3, gate_exit=0)
    assert outcome != verify.PASS, \
        "the gate ran in a tree the confirmed setup command failed to prepare"
    assert outcome == verify.SETUP_FAILED
    assert sr.run.exit_code == 3


def test_the_setup_dimension_names_the_clone_it_measured(tmp_path):
    """seat.py:4 calls `setup` a fresh-clone measurement; runner.py:667 filled it from the
    builder's clone. Two names, because they are two facts."""
    rec = _seat_record_after(tmp_path, builder_setup_exit=0, verifier_setup_exit=3)
    assert rec.status.builder_setup == "pass"
    assert rec.status.verifier_setup == "fail"
    assert rec.status.verify == "not-run"
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_forge_runner.py -k verifier_setup -v`
Expected: FAIL — outcome is `PASS`.

- [ ] **Step 3: Implement — as a RUNNER sentinel, not a `verify.OUTCOMES` member**

**`SETUP_FAILED` must not join `verify.OUTCOMES`.** That vocabulary's documented contract is *"every
value **`classify`** can return"* (`verify.py:126-127`), and this is a **runner** verdict. Adding it
breaks three totality tests, all verified:

- `test_forge_verify.py:1728` AST-walks **`verify.py`'s own return statements** and asserts
  `returned == set(verify.OUTCOMES)`. This return lives in `runner.py`. **Hard fail.**
- `test_forge_strategy.py:411` calls `classify_failure` for every outcome, and `strategy.py:459-466`
  **raises by design** for an outcome with no branch — *"the day §6.2 gains a row is the day this
  function is required to fail."* **Hard fail**, and this plan never touches `classify_failure`.
- `test_forge_rubric.py:135` requires distinct ranks **and** `max(GATE_RANK) == HARVEST_INCOMPLETE`.
  `GATE_RANK` is contiguous 0–5, so "below `FAIL`" has **no free integer**, and appending at 6 makes
  `SETUP_FAILED` the max. **Fail** unless `HARVEST_INCOMPLETE` is renumbered — which re-blesses
  `GATE_CHANGED` at rank 2, a known deferred defect (Plan M item 4).

Use a runner-level sentinel instead — `runner.SETUP_REFUSED`, taught explicitly to `_verify_dim`:

```python
SETUP_REFUSED = "setup-refused"   # a RUNNER verdict; verify.OUTCOMES is what `classify` returns

# in verify_candidate, in the `if setup.steps:` branch, AFTER on_measurement:
    if setup_result.run.exit_code != 0:
        # §6 STEP 4 DOES NOT RUN. A gate executed in a tree the confirmed setup command failed
        # to prepare measures the preparation failure, not the candidate — and
        # `_with_setup_caveat` put that fact in PROSE beside a structured PASS that downstream
        # code branches on.
        return (SETUP_REFUSED,
                f"the verifier's own setup command exited {setup_result.run.exit_code}, so "
                "§6 step 4 was not run.", v, setup_result)
```

**`on_measurement` must fire before the return.** `test_forge_runner.py:1527` exists because *"the exit
code reached an operator as prose inside `reason` and **no record at all**"*, and its fixture is
precisely this path. Returning before the sink reverses s2's *Verified Sound* finding that
`on_measurement` keeps a bought measurement alive across a refusal.

Rename `Status.setup` → `Status.builder_setup` and add `Status.verifier_setup`. **Note the collision:**
the seat record already carries a top-level `verifier_setup` key (`seatrecord.py:51-52`) holding the
verifier's setup **run dict**. Adding a §8 dimension *string* under the same word puts two facts under
one name in one record — the shape this review filed three times. Name the dimension
`verifier_setup_dim`, or nest it, and say which in the commit message. `classify_seat` gains a required
keyword: **42 call sites, 30 in `tests/test_forge_seat.py`.** Map `SETUP_REFUSED` → `verify="not-run"`.

- [ ] **Step 4: Run the tests**

Run: `python3 -m pytest tests/test_forge_runner.py tests/test_forge_seat.py -v`
Expected: PASS. `:847` and `:1527` will fail — they encode the defect; update both and record why.

- [ ] **Step 5: Apply the same rule to calibration**

`runner.py:1383` ignores calibration setup failure and applies the operator's policy only to the later gate exit. Make `_obey_the_calibration_policy` read an aggregate that includes the setup exit **and** `second_pass`, and journal both (`:1373`) — today a pass/pass and a pass/fail calibration leave the same durable record after a crash.

- [ ] **Step 6: Commit**

```bash
git add shared/lib/forge/runner.py shared/lib/forge/verify.py shared/lib/forge/seat.py tests/
git commit -m "fix(forge): a gate run in a tree setup failed to prepare is not a PASS, and setup names its clone"
```

---

## Task L1.3: calibration takes the hooks read every candidate takes

**Files:**
- Modify: `shared/lib/forge/verify.py:1430-1437` (`calibrate`), `:684-707` (`_assert_hooks_pinned`)
- Test: `tests/test_forge_verify.py`

**Why:** `assert_hooks_pinned` has exactly **two** production call sites — inside `build_verifier` (the first read, pre-setup) and `runner.py:1140` (the second, post-setup). **`calibrate` is neither**, though it holds the `Verifier` in hand between `run_setup` and `fixed_point`.

The trigger is not adversarial — it is **husky**. `npm ci` runs `prepare`, `husky install` writes `core.hooksPath`. The calibration runs the gate **under the repository's own hooks** and comes back green, so §5 step 3 — the pass whose purpose is finding infrastructure problems *before a token is spent* — reports success. Then all three candidate verifiers raise *"the candidate rewrote the verifier's git config"* **after three providers have been paid**, and the message **blames the candidate** for the operator's own confirmed setup command.

- [ ] **Step 1: Write the failing test**

```python
def test_a_setup_command_that_moves_the_hook_path_is_caught_in_calibration(tmp_path):
    """The calibration is the only run with no candidate, so it is the only run that can
    tell 'the operator's setup did this' from 'the candidate did this'."""
    cal_setup = [["sh", "-c", "git config core.hooksPath .husky"]]
    with pytest.raises(verify.VerifyError) as e:
        verify.calibrate(tmp_path, _baseline(tmp_path), setup=verify.Command(cal_setup),
                         command=verify.Command([["true"]]), env={})
    assert "confirmed setup command" in str(e.value)
    assert "candidate" not in str(e.value), \
        "there is no candidate in a calibration; naming one sends the operator to the wrong file"
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_forge_verify.py -k hook_path_is_caught_in_calibration -v`
Expected: FAIL — `calibrate` returns a green `Calibration`.

- [ ] **Step 3: Implement**

Give `_assert_hooks_pinned` a `by` parameter (`"candidate"` or `"setup"`) selecting the sentence, then in `calibrate`:

```python
    setup_run = run_setup(v, setup, env=env).run if setup.steps else None
    # THE READ EVERY CANDIDATE GETS. Without it the control tree is held to a WEAKER premise
    # than everything compared against it: `Calibration`'s own docstring rests
    # BASELINE_RED_NO_NEW_IDENTIFIED_FAILURE on "the TREE this ran in", and a tree whose pin
    # was never re-read after setup is not the tree every candidate is required to have run in.
    assert_hooks_pinned(v, by="setup")
    fp = fixed_point(v.path, command, v.contract, env=env)
```

- [ ] **Step 4: Run the tests**

Run: `python3 -m pytest tests/test_forge_verify.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add shared/lib/forge/verify.py tests/test_forge_verify.py
git commit -m "fix(forge): the control tree took a weaker premise than every candidate measured against it"
```

---

## Task L1.4: containment is asked both ways, and the rest is written down rather than implied

**Files:**
- Modify: `shared/lib/forge/review.py:363-431` (`assert_ledger_is_out_of_reach`), `shared/skills/llm-forge/SKILL.md`
- Test: `tests/test_forge_review.py`

**Why:** `assert_ledger_is_out_of_reach` asks `lp.resolve().is_relative_to(root)` — **containment one way only**. The ledger, journal and manifest are **siblings** of the review checkout, so a reviewer runs **`cat ../ledger.json`** from its ordinary cwd. It is a **pure read**, and the bracket measures **writes**, so no tightening of the bracket could catch it.

**What this task does NOT do, and why.** An earlier draft moved the clone to `forge_root()/<name>-review`. That does not close it: the run directory is then a **sibling at a computable name**, same UID, and reviewers have a shell — `ls ..`, a glob over `$XDG_STATE_HOME/khenrix-forge/*/ledger.json`, or `find` all reach it. It would also make `storage.run_dirs` report a **phantom run** `<run_id>-review` to `--gc all`, and `--gc <run_id>-review` would then hit gc's "records no manifest" refusal. **There is no permission boundary anywhere in this design.** L1.6 admits exactly that about seats; the same admission is owed here.

So: fix the direction bug, which is real and cheap, and **write down the residual** instead of shipping a speed bump described as a boundary. The real closure — OS-level isolation, or holding the ledger in memory for the round — is Plan M item 9.

- [ ] **Step 1: Write the failing test**

```python
def test_containment_is_asked_in_both_directions(tmp_path):
    """The assertion asked "is the ledger under a reviewer root?" and not the reverse.

    NOT a sibling assertion. An earlier draft asserted `ledger.parent != root.parent`, which
    under the layout that draft chose was unequal BY CONSTRUCTION — a test that could not
    fail, in the task whose whole subject is tests that cannot fail.
    """
    run_dir, roots = _round_layout(tmp_path)
    ledger = run_dir / "ledger.json"
    ledger.write_text("{}")
    # a reviewer root that CONTAINS the ledger must be refused — the direction already checked
    with pytest.raises(review.ReviewError):
        review.assert_ledger_is_out_of_reach(ledger, [run_dir])
    # and one the ledger's directory contains must be refused too — the direction that was not
    with pytest.raises(review.ReviewError):
        review.assert_ledger_is_out_of_reach(ledger, [run_dir / "review" / "round-1"])
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_forge_review.py -k both_directions -v`
Expected: FAIL on the second case — a root *under* the ledger's directory is accepted today.

- [ ] **Step 3: Implement**

Extend `assert_ledger_is_out_of_reach` to refuse when the ledger is under a root **or** a root is under the ledger's directory. **Do not add a share-a-parent rule while §13 is unwired:** the synthesis checkout is `run_dir/synthesis` (`cli.py:251`, `:610`), a sibling of `run_dir/ledger.json`, so that rule would refuse unconditionally the moment the review verb lands.

- [ ] **Step 4: Run the tests**

Run: `python3 -m pytest tests/test_forge_review.py -v`
Expected: PASS.

- [ ] **Step 5: Write the residual into SKILL.md**

Add, in the same voice L1.6 uses for seats: *"Reviewers run as your user with a shell, in a clone beside the run directory. §13's blindness is asserted structurally against the ledger's path and is not enforced by the operating system; a reviewer that goes looking can read the run's ledger, journal and seat clones. Treat the panel as blind by construction, not by containment."*

- [ ] **Step 6: Commit**

```bash
git add shared/lib/forge/review.py shared/skills/llm-forge/SKILL.md tests/test_forge_review.py
git commit -m "fix(forge): containment was asked one way, and the rest is a promise the OS never made"
```

---

## Task L1.5: harvest refuses a builder-selected clean filter

**Files:**
- Modify: `shared/lib/forge/harvest.py:177-195`
- Test: `tests/test_forge_harvest.py`

**Why:** `harvest.py:177` states it outright: `filter.<name>.clean` **still executes during the trusted parent's `git diff`**, and its output **substitutes for the builder's bytes**. `test_forge_harvest.py:367` **asserts the filter runs.**

`ArtifactSet` carries no per-path Fwork hashes, and `bundle.build` treats a path present in the patch as **covered**, so the live file is never re-read as a sidecar — nothing downstream can notice.

> The builder leaves **broken** bytes in `app.py`, writes `.gitattributes` selecting `filter=rigged`, configures a clean filter emitting **passing** code. Fwork records the broken file; the patch carries the replacement; the verifier materializes and **passes** it; the result is presented as verification of the builder-origin artifact.

**Every other founding-premise finding is about a check the builder could influence. This one is about the bytes the check is run on.**

- [ ] **Step 1: Write the failing test**

```python
def test_a_seat_configured_clean_filter_refuses_the_harvest(tmp_path):
    seat = _seat_with_filter(tmp_path, path="app.py", broken="def f(): raise\n",
                             filtered="def f(): return 1\n")
    with pytest.raises(harvest.HarvestError) as e:
        harvest.artifact_set(seat, base="B1")
    assert "clean filter" in str(e.value)
```

- [ ] **Step 2: Run to verify it fails**

Expected: FAIL — `artifact_set` returns a patch holding the filtered bytes.

- [ ] **Step 3: Implement — and do not fire on git-lfs**

A naive *"refuse when any `filter` attribute is set"* raises `HarvestError` for **every seat, after
every provider call is paid, in every repository using git-lfs** — `check-attr` reports the
**effective** attribute, and `filter=lfs` is declared in the **baseline's own** `.gitattributes`. The
question is not *is a filter active* but **did the seat introduce it**:

```python
# `--stdin -z`, not argv: harvest routinely produces path counts that overflow ARG_MAX.
# The WORKTREE stack (not `--cached`), because that is what `git diff` converts against.
seat_attrs = _check_attr(seat_path, paths)          # {path: filter-name-or-None}
base_attrs = _check_attr(baseline_clone, paths)     # the same query against B1
introduced = {p: f for p, f in seat_attrs.items() if f and f != base_attrs.get(p)}
# The attribute alone is INERT — it selects a filter that must also be DEFINED. The config is
# what a seat can rig, and `.git/config` is untracked, so it never travels in the patch.
rigged = {p: f for p, f in introduced.items()
          if _git_config(seat_path, f"filter.{f}.clean") is not None}
if rigged:
    raise HarvestError(...)
```

**What this mechanism covers, and what it does not** — state it in the docstring rather than leaving
the next reader to assume:

- **Smudge filters: covered by construction.** There is no separate smudge attribute; `filter=<name>`
  selects `.clean`, `.smudge` and `.process` together. Harvest's exposure is `.clean` only
  (worktree→object, `harvest.py:190`); the smudge direction lands in the *verifier* on
  `bundle.materialize` and is bounded because `.git/config` is untracked.
- **`.gitattributes` in subdirectories: covered.** `check-attr` walks the full per-directory stack —
  which is exactly why it beats grepping the top-level file.
- **`$GIT_DIR/info/attributes` and `core.attributesFile`: covered.** Both are in the stack, both
  outrank in-tree files, and **no tree-only inspection sees either.**
- **Not covered:** the `text` / `eol` / `working-tree-encoding` attributes, which also convert bytes
  during diff — a narrower substitution channel, and it stays open. Plan M item 2.
- **Not covered:** the **detached-child** route to the same substitution (Fwork records v1, a `setsid`
  formatter writes v2 before the diff). Same outcome, different door — that is s1-codex's Fwork
  byte-binding, Plan M item 2. **This task closes the config-driven route only**, and the earlier
  draft's claim that it closes "the bytes the check is run on" was too broad.

- [ ] **Step 4: Run the tests.** `test_forge_harvest.py:367` will fail — it asserts the filter runs. It encoded the defect; invert it and record why.

- [ ] **Step 5: Commit**

```bash
git add shared/lib/forge/harvest.py tests/test_forge_harvest.py
git commit -m "fix(forge): the trusted parent ran a filter the builder chose, and kept its output as the candidate"
```

---

## Task L2.1: one predicate, one roll-up, three copies deleted

**Files:**
- Modify: `shared/lib/forge/coverage.py:62-64`, `:106-116`, `:140-150`; `rubric.py:152-170`; `strategy.py:494-506`; `handover.py:513-516`
- Test: `tests/test_forge_coverage.py`, `test_forge_rubric.py`, `test_forge_strategy.py`

**Interfaces:**
- Produces: `coverage.unmeasured(results) -> tuple[Result, ...]` — every result whose `method != "mechanically_checked"`; `Report.traced` — the third roll-up.

**Why:** §10.1 names **three** ways a criterion escapes mechanical checking in one sentence: *"Everything else is marked `manual_trace_confirmed` **or** `unresolved`."* The gap predicate catches **two**. Measured through `coverage.check` from a real `Ledger` — five accepted rows, `kind="prose"`, each traced:

```
results: 5  methods: ['manual_trace_confirmed']  unsatisfied: ()  unresolved: ()
_read_report -> answer=not_triggered  unsat=0  covered=0
```

`unsatisfied_criteria=0` is **the best possible value on §12.5's top dimension**, and the seat is **fully rankable**. Measured ranking: the seat that **failed the gate** with risk 5 and **no predicate run on any claim** is named `strongest` over one that **passed** with a measured miss. Because `kind` is the author's choice, **the rubric rewards writing unfalsifiable criteria.**

`strategy.classify_failure` holds a **second hand-maintained copy**, returns `synthesis_introduced → PERMITTED` (**buys a fallback**), and prints *"every one of this candidate's 5 criteria was mechanically checked and satisfied"* when **zero** were.

`Result.__post_init__` **already refuses** `(mechanically_checked, None)` on exactly these grounds — *"in neither `Report.unsatisfied` nor `Report.unresolved` … invisible in every roll-up"* — and the module then ships `manual_trace_confirmed` into that same blind spot.

**Do not patch `rubric` and `strategy` separately.** That would make a **third** copy, which is how this arose. `rubric` is honest today **only because it reaches past the roll-ups into `results`** — that asymmetry is the tell.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_report_of_traced_prose_is_not_rankable_on_the_top_dimension():
    """The external question: did a predicate RUN on every result?

    test_the_trigger_and_the_top_dimension_agree_on_every_report_shape defines complete as
    `bool(results) and not unresolved` — _read_report's own predicate restated. It asks
    whether the code agrees with itself.
    """
    rep = _report_from_ledger(_traced_prose_rows(5))
    d = rubric.dimensions_from(rep)
    assert d.unsatisfied_criteria is None and d.covered_criteria is None


def test_the_seat_that_measured_nothing_never_outranks_the_seat_that_measured():
    all_manual = _seat("all-manual", _traced_prose_rows(10), gate=verify.FAIL, risk=5)
    mechanical = _seat("all-mechanical", _checked_rows(9, misses=1), gate=verify.PASS, risk=0)
    assert rubric.strongest([all_manual, mechanical])[0] != "all-manual"


def test_the_two_readers_never_disagree_over_a_shape_built_from_a_REAL_ledger():
    """An agreement test between two copies cannot find a defect they share — measured, both
    modules agreed on the traced shape and both were wrong. So the shapes must come from
    `coverage.check` over real ledgers, and must INCLUDE the shape that broke them.
    """
    for rows in (_traced_prose_rows(3), _checked_rows(3), _mixed_rows(mech=4, traced=1),
                 _unresolved_rows(2), _empty_rows()):
        rep = _report_from_ledger(rows)
        gap_r = rubric.dimensions_from(rep).unsatisfied_criteria is None
        gap_s = strategy.classify_failure(verify.FAIL, report=rep)[0] is None
        assert gap_r == gap_s, f"the two readers disagree about {rows!r}"
```

**Not `inspect.getsource`.** An earlier draft asserted the source text of both functions. That test
breaks without violating the property (inline the predicate, rename it, mention the old name in a
comment) and passes while violating it (write `unmeasured = 0` in the body) — and its second assertion
would have forced deletion of `strategy.py:496-500`, whose reason string is the honest one:
*"nobody could check them… so 'every claim is satisfied' is not something this run measured."*

- [ ] **Step 2: Run to verify they fail.** Expected: the first returns `(0, 0)`; the second names `all-manual`.

- [ ] **Step 3: Implement — and keep the empty-report guard, which a naive replacement loses**

```python
def unmeasured(results) -> tuple["Result", ...]:
    """Every result no predicate ran on — §10.1's `manual_trace_confirmed` AND `unresolved`.

    ONE FUNCTION BECAUSE TWO SPELLINGS OF ONE JUDGEMENT CANNOT BE KEPT IN STEP BY BOTH BEING
    REMEMBERED. `rubric._read_report` and `strategy.classify_failure` each carried a copy that
    knew `unresolved` and not `manual_trace_confirmed`, and the agreement test between them
    passed because they AGREED — and were both wrong.
    """
    return tuple(r for r in results if r.method != "mechanically_checked")
```

**The replacement is not one-for-one.** The two copies each branch on `not report.results` **and**
`report.unresolved`. `unmeasured(())` is `()`, so swapping in the predicate alone leaves an **empty
report rankable at `(0, 0)`** — re-opening the hole `test_forge_rubric.py:216` and
`test_forge_strategy.py:355` both pin. Keep the empty branch and replace only the second:

```python
    if not report.results:   gap = (...)                  # unchanged
    if coverage.unmeasured(report.results): gap = (...)   # replaces the `report.unresolved` branch
```

**Placement, corrected:** in **`strategy.classify_failure`** the check goes after the two
`REQUIREMENT_GAP` branches so the trigger still outranks the gap. In **`rubric._read_report` it does
not** — `gap` is computed **before any answer is chosen**, deliberately, and s4 verified that structure
sound across all 34 report shapes. Moving it below the `TRIGGERED` returns would re-open the fail-open
that structure exists to close. **Same predicate, two placements, because the two functions are shaped
differently.**

Correct the two docstrings (`rubric.py:135-139`, `handover.py:513-516`) that assert the false belief.

- [ ] **Step 4: Run the tests.** Add the all-traced shape **and a mixed shape (4 mechanical + 1 traced)** to `_shapes()` — mixed is the realistic ledger, and an `all(...)`/`any(...)` slip passes every all-or-nothing case.

- [ ] **Step 5: Commit** (the third roll-up and the accepted-row filter are now their own tasks — see L2.3)

- [ ] **Step 6: Commit**

```bash
git add shared/lib/forge/coverage.py shared/lib/forge/rubric.py shared/lib/forge/strategy.py shared/lib/forge/handover.py tests/
git commit -m "fix(forge): manual_trace_confirmed was the third spelling of 'no predicate ran', and two copies knew two"
```

---

## Task L2.2: a zero exit is not a pytest pass

**Files:**
- Modify: `shared/lib/forge/coverage.py:317-345`
- Create: `shared/lib/forge/_pytest_receipt.py`
- Test: `tests/test_forge_coverage.py`

**Why:** `coverage.py:317` verifies **collection**, but the execution phase reduces the outcome to the **process return code** — at `:340` **every zero exit becomes "passed."** `@pytest.mark.skip` exits 0 → `mechanically_checked, True`. An **expected xfail whose body fails normally** exits 0 → a failing test recorded as satisfied.

`test_forge_coverage.py:67` supplies a **fake `(rc=0, "1 passed")`** and never exercises a real pytest outcome.

**This is the fail-open in the mechanical axis itself** — the axis §10.1 rests on. "Never executed" reaches the cleanest state available.

- [ ] **Step 1: Write the failing tests** — real pytest runs for a skipped test, an xfail whose body fails, and a genuine pass; assert `unresolved`, `unresolved`/`False`, and `True` respectively.
- [ ] **Step 2: Run to verify they fail** — the first two return `mechanically_checked, True`.
- [ ] **Step 3: Implement** a stdlib-only `pytest_runtest_logreport` plugin writing one JSON line per node id (`outcome`, `when`) to a temp path passed via `-p`; require **exactly one terminal `call` outcome** for the node id; map `skipped`/absent → `unresolved`, `failed` → `False`, `passed` → `True`; **refuse a zero exit with no matching receipt.**
- [ ] **Step 4: Run the tests.**
- [ ] **Step 5: Commit**

```bash
git add shared/lib/forge/coverage.py shared/lib/forge/_pytest_receipt.py tests/test_forge_coverage.py
git commit -m "fix(forge): a skipped test exits zero, and zero was being read as 'the claim holds'"
```

---

## Task L2.3: coverage counted over rows nobody accepted

> **The third roll-up was dropped during execution, and this is the record of why.** It was
> scheduled because a consumer reading only `Report`'s roll-ups saw a clean report over a
> claim no predicate had touched. **L2.1 closed that at the source** — `rubric` and `strategy`
> both call `coverage.unmeasured(report.results)` now, and neither infers completeness from
> what the roll-ups contain. Measured after L2.1: no module outside `coverage.py` reads
> `.unsatisfied`/`.unresolved` as a display surface. So the field would be a fifth positional
> element on a public dataclass built at a dozen sites, added for a consumer that does not
> exist, against this plan's own YAGNI constraint. The reasoning is recorded in `Report`'s
> docstring where the next author will meet it, with the condition that would make it
> necessary. **The accepted-row filter below is the half that was load-bearing, and it ships.**

**Files:** Modify `shared/lib/forge/coverage.py:55-64` (`_lines`), `:96-116` (`Result`), `:140-151` (`Report.__post_init__`), `:665-673` (`check`) · Test: `tests/test_forge_coverage.py`

**Split out of L2.1, because it is two schema changes and L2.1 is a predicate change.** Both were steps of one task whose commit message was about `manual_trace_confirmed`; each needs its own reviewer gate.

**What makes this more than a field addition:**

- **`_lines` is a two-way dispatch** (`coverage.py:55-64`): `"unsatisfied"` selects `satisfied is False`; **everything else** selects `method == "unresolved"`. So `_lines(results, "traced")` returns the *unresolved* list — and `Report.__post_init__` loops the label tuple and **raises on mismatch**, so adding `"traced"` without fixing `_lines` makes **every `Report` holding a traced result raise.**
- **`Report` is a frozen four-field dataclass built positionally at 12+ sites** across `coverage.py`, `test_forge_strategy.py`, `test_forge_rubric.py` and `test_forge_coverage.py`. A fifth field with a raising re-derivation breaks all of them. `check`'s docstring also says *"plus the contradictions and the **two** roll-ups"*.
- **`Result` gains a row-status field**, and its `__post_init__` is the most heavily pinned invariant in the module — ~44 `Result(` constructions across `shared/` and `tests/`.

**Why the accepted-row filter belongs here:** `check` evaluates `r.acceptance_criteria` regardless of `r.status`, so a **rejected** row's satisfied criterion increments `covered_criteria`. A seat that **obeys** a rejection fails that criterion and is charged a fallback; one that **wrongly implements it** ranks stronger. Record row status on `Result` so `_lines` and `_read_report` inherit the filter rather than each remembering it.

- [ ] **Step 1:** write the failing test — a rejected row with a satisfied criterion must not raise `covered_criteria`, and a report holding a traced result must construct.
- [ ] **Step 2:** run it; expect the `covered=1` from the rejected row.
- [ ] **Step 3:** make `_lines` a three-way dispatch keyed on an explicit predicate map; add `Report.traced`; convert the 12+ positional constructions to keywords **in the same commit**; add the status field to `Result`; filter `check` to accepted rows.
- [ ] **Step 4:** `python3 -m pytest tests/test_forge_coverage.py tests/test_forge_rubric.py tests/test_forge_strategy.py -v`.
- [ ] **Step 5: Commit**

```bash
git add shared/lib/forge/coverage.py tests/
git commit -m "fix(forge): a third roll-up, and coverage stops counting claims the ledger rejected"
```

---

## Task L3.1: the eval oracle that grades a correct answer wrong

**Files:**
- Modify: `evals/llm-forge/evals.json:13-16`, `evals/llm-forge/fixtures/make_handover_header.py:92`
- Test: `tests/test_forge_handover.py`

**Why:** Case 0's prompt selects only `scratch/notes.md`, while `dist/bundle.js` is **pre-existing and ignored**. `baseline.py:352` inventories tracked files **plus explicitly selected paths** and `:431` force-adds **only selected paths** — so the unselected ignored file **is not in B1, is not in the synthesis cloned from B1, and cannot come back** as a sidecar. The generator **fabricates the payload in memory** rather than exercising `_sidecars_of`/`--collect`.

**A correct answer — that the file is absent — is graded wrong.** This must be fixed **before** the contamination fix in Plan M, or a de-contaminated baseline gets penalised for being right.

- [ ] **Step 1: Write the failing test — over `_sidecars_of`, not over the generator**

```python
def test_an_unselected_ignored_file_is_not_an_out_of_band_artifact(tmp_path):
    """The external question: can --collect produce an out-of-band record for a file that
    was never in B1? `baseline.py:352` inventories tracked + SELECTED; `:431` force-adds only
    SELECTED. An unselected ignored file is in neither, so it cannot come back.
    """
    repo = _repo(tmp_path, tracked=["src/app.py"], ignored=["dist/bundle.js"])
    b1 = baseline.materialize(repo, select=["scratch/notes.md"], ...)
    assert "dist/bundle.js" not in cli._sidecars_of(_synthesis_cloned_from(b1))
```

**Not a generator-vs-fixture equality test.** That is s7 **M2** — fixture drift on eval *1*'s header —
and it is **green while this oracle stays false**, because a generator that fabricates the sidecar in
memory and a fixture regenerated from that generator agree perfectly. Keep the drift test too, but it
is a different finding and does not close this one.

- [ ] **Step 2: Run to verify it fails.**
- [ ] **Step 3:** correct assertion 3 to state the engine's actual behaviour, regenerate the fixture from `handover.text`, and make the generator use a real `runstate.Manifest` rather than the three-attribute stub.
- [ ] **Step 4:** `make verify`.
- [ ] **Step 5: Commit**

```bash
git add evals/llm-forge/ tests/test_forge_handover.py
git commit -m "fix(evals): case 0 asserted a sidecar the engine cannot produce, and graded the right answer wrong"
```

---

## Task L3.2: a receipt that can fail

**Files:**
- Modify: `scripts/eval_harness.py:87`, `:455-485`, `:556-571`; `scripts/lib/checks.py:249`, `:348`
- Test: `tests/test_eval_harness_receipt.py` (create)

**Why:** Four compounding defects — the deterministic command names **three** test modules where the Makefile has **31**; the omitted set **includes `test_forge_packaging.py`**; receipt writing checks **only the subprocess return code** (**an all-skipped pytest run exits 0**, and neither test count nor skip count is parsed); and `receipt_gate` compares **only `source_hash` and `eval_set_hash`**, so a receipt with matching hashes and `self_test: false` is accepted. The source closure **omits the certifier, the Makefile and the test suite** — so weakening `DETERMINISTIC_GATED` **does not stale a receipt**.

Every forbidden comparison at once: *all-skipped == all-passed · certifier-weakened == unchanged · self-test-failed == succeeded · no-judgment still leaves a fresh receipt.*

- [ ] **Step 1: Write the failing tests** — a command running zero tests must not write a receipt; an all-skipped run must not; a receipt with `self_test: false` must be rejected by `receipt_gate`; editing `eval_harness.py` must stale every receipt.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3:** run the **full** `FORGE_TESTS ∪ FORGE_SLOW_TESTS`; parse pytest's summary line; store `tests_run`, `skipped`, `command`, `interpreter`; require `tests_run > 0 and skipped == 0`; add `scripts/eval_harness.py`, `scripts/lib/checks.py` and the `Makefile` to the closure; make `receipt_gate` validate a **typed** receipt.
- [ ] **Step 4:** `make verify`, then `eval_harness.py --seed-receipt` for every skill (all receipts stale by design).
- [ ] **Step 5: Commit**

**Name the new suite** in `COUNCIL_TESTS` before running the gate — L0.3 widened the orphan check to
`test_*.py`, so an unnamed `tests/test_eval_harness_receipt.py` is a red `make verify` here.

```bash
git add scripts/eval_harness.py scripts/lib/checks.py tests/test_eval_harness_receipt.py Makefile evals/*/receipt.json
git commit -m "fix(evals): the receipt recorded that a command exited zero, not that anything was tested"
```

---

## Task L3.3: a dead judge is not a tie

**Files:**
- Modify: `scripts/eval_harness.py:219-231`, `:143-156`, `:378-385`, `:580-585`
- Test: `tests/test_eval_harness_receipt.py`

**Why:** `COMPARE_TMPL` never offers "tie" — it asks for `"winner": "A" or "B"`. **So every `tie` this harness produces is a parse failure, an empty answer, or an off-slot response.** `compare()` discards the judge record entirely, so unlike `grade()` it **cannot report that its judge never spoke**. It is `eval_trigger.parse_verdict`'s **already-fixed** bug one module over.

**Live witness in this skill's own artifacts:** all six `comparison.json` files record `winner_slot: "A"`, and since `blind_pair` alternates slots by parity, a constant-slot judge aggregates to a clean **3–3 `tie`** — with nothing anywhere reading `winner_slot`.

- [ ] **Step 1: Write the failing tests** — an unreadable comparison yields `winner_condition is None`; unreadables are excluded from the tally and reported; a constant `winner_slot` across all comparisons is **not** a tie.
- [ ] **Step 2–4:** implement three-state parsing, carry the judge record out of `compare()`, add the slot-degeneracy check; run.
- [ ] **Step 5: Commit**

```bash
git add scripts/eval_harness.py tests/test_eval_harness_receipt.py
git commit -m "fix(evals): every tie the harness emitted was a failure wearing a verdict's clothes"
```

---

## Task L4: every quoted term must have a reachable production caller

**Files:**
- Modify: `tests/test_forge_packaging.py`

**Why:** `test_the_skill_quotes_the_numbers_gate_quote_actually_produces` pins SKILL.md to `gate.quote`'s **output**, so it guarantees **prose ↔ formula** and nothing about **formula ↔ engine**. It is the strongest test in the file and structurally blind to the 19-call quote.

**This one test would have caught the unwired review loop, the oscillation gap, and SKILL.md's cost paragraph.** One seat cleared that paragraph as truthful *because* it matched the formula — the reasoning the defect is made of.

- [ ] **Step 1: Write the failing test**

```python
def test_every_term_the_quote_prices_has_a_reachable_production_caller():
    """prose ↔ formula is not formula ↔ engine.

    Instrument a complete fake run and reconcile OBSERVED events against the quote. A term
    the engine cannot spend must be priced at zero or removed.
    """
    q = gate.quote(_report(), seats=3, attempts=3, review_rounds=2, ultrareview=True)
    observed = _drive_a_complete_fake_run(seats=3, attempts=3, review_rounds=2)
    assert observed.provider_calls <= q.calls
    assert observed.clones <= q.clones
    for term, priced in q.terms.items():
        if priced:
            assert observed.by_term[term] > 0, (
                f"the quote prices {term!r} at {priced} and the engine never spends it; "
                "price it at zero or wire it")
```

- [ ] **Step 2: Run to verify it fails** — `review` and `review_fixes` are priced and never spent.
- [ ] **Step 3:** give `Quote` a `terms` mapping; build `_drive_a_complete_fake_run` on the existing fake launcher, counting provider calls, clones and verifier runs by term.
- [ ] **Step 4:** run; reconcile with L1.6's repricing.
- [ ] **Step 5: Commit**

```bash
git add tests/test_forge_packaging.py shared/lib/forge/gate.py
git commit -m "test(forge): the quote was pinned to its own formula, and the formula priced code nothing calls"
```

---

## Task L5 (RUNS LAST, after L4): SKILL.md stops claiming isolation and a review loop it does not have

> **Moved from position L1.6.** It depended on L4 — *"reprice against what L4's test proves
> reachable"* while L4 said *"reconcile with L1.6's repricing"* — a circular dependency six tasks
> apart. And it runs `make eval SKILL=llm-forge` and commits a receipt that **L2.x, L3.1 and L4 all
> stale**, because `SKILL_EXTRA_DIRS["llm-forge"]` covers `shared/lib/forge`, so `make precommit`
> would fail at the next commit boundary. Running it last means **one** eval run, and it is the real
> one rather than a `--seed-receipt` that discards it.

**Files:**
- Modify: `shared/skills/llm-forge/SKILL.md:3`, `:9`, `:14`, `:20-26`, `:55-62`, `:93`, `:176-196`, `:198`, `:184`, `:163-169`
- Test: `tests/test_forge_packaging.py`

**Why (six claims, each measured):**

| SKILL.md says | The engine does |
|---|---|
| `:9` "an isolated clone, with no access to each other's work" | separate **remote-less git clones**; predictable sibling worktrees (`runner.py:151`), sequential seats (`:1701`), same OS user — **no filesystem boundary** |
| `:20-26` ~19 calls / 17 clones / 56.7 GB | `review.run_round` has **no production caller** (measured; the only two references outside `review.py` are comments) — reachable ceiling ~9 / 13 / ~43 GB |
| `:14` "fusion, not selection" | the collector rejects **only** a tree identical to B1 (`handover.py:243`); `--strategy` is reported and `cli.py:772` says it cannot be checked |
| `:184` `_strongest` "usually" names nobody | `cli.py:448` returns nobody on **both** branches — never |
| `:198` baseline-owned = unchanged selected files | `cli.py:411` returns **every** selected path unconditionally |
| `:163-169` a misspelled `--strategy` "costs the ultrareview and then refuses" | `Provenance(` is `cli.py:657`, `run_ultra(` is `:679` — **validation is 22 lines earlier**; nothing is spent |

The last row is the **only** claim that describes the engine as *worse* than it is; the fix is a deletion.

- [ ] **Step 1** — rewrite `:9` to *"a separate clone of B1 with no git remote and no access to the other seats' branches. Seats are not filesystem-isolated from one another; treat the fleet as three agents on one machine."*
- [ ] **Step 2** — reprice `:20-26`, `:55-62` and `:93` against what L4's test proves reachable. Add the two hedges §4 omits: `Council: no review round was convened.` and `Ultrareview: N finding(s) reported` (with `ultra.py:492-500`'s note that the findings reach no terminal).
- [ ] **Step 3** — soften `:14` to an instruction, or defer the enforcement to Plan M and say the collector does not certify it.
- [ ] **Step 4** — `:184` "usually" → "never, until §12.5 is wired"; `:198` rename to "selected files carried into B1" (drop "unchanged"); **delete** `:163-169`'s warning.
- [ ] **Step 5** — `make verify && make eval SKILL=llm-forge && make precommit`. **A skill change needs a receipt.**
- [ ] **Step 6: Commit**

```bash
git add shared/skills/llm-forge/SKILL.md evals/llm-forge/receipt.json
git commit -m "docs(llm-forge): six claims the engine does not make, including the isolation in sentence two"
```

---

---

## Deferred to Plan M — explicit, so nothing is silently dropped

**The earlier draft of this list was not honest: slice s4 was absent entirely, and s5+s6 contributed
nine Criticals and twelve Highs against five entries.** Rewritten below, with the items that are
**load-bearing for something this plan schedules** marked ⚠ and ordered first.

1. ⚠ **s1 C-UPGRADE, TOCTOU half** — screen the **content-addressed B₁ path set after B₁ is built**,
   binding the scan to the per-path hashes seat verification consumes. L0.4 closes the coverage half
   and records this one in prose.
2. ⚠ **s4 H2 — `snapshot.py:206`'s `& 0o777`** drops setuid/setgid/sticky, so `chmod u+s` leaves a
   **byte-identical inventory**. One character class, and it is the change predicate **L1.4's own
   bracket rests on**. ⚠ **s4 H2b — `snapshot.py:210` omits `kind`**, and the FIFO/symlink collision
   demonstrated at `test_forge_review.py:1852` was never carried to harvest — **inside L1.5's own
   path**, where it silently drops a builder's artifact.
3. ⚠ **s4 M1 — `GATE_RANK` ranks `GATE_CHANGED` 2nd of 6**, above `FLAKY` and `FAIL`. L1.2 must
   renumber around it; renumbering is a deliberate re-blessing until this is decided.
4. ⚠ **s3 M3 — `coverage.check` calls `ledger._check_rows`, not `ledger._check`**, so stale and
   duplicate row ids pass. L2.1/L2.3 measure more carefully **on unverified identities**.
5. ⚠ **s4 H4 — `progress.pytest_fingerprints`** (bare-substring banners; any nonzero exit accepted on
   one `FAILED` line; `\S+` truncating parametrized ids so **two different failures compare equal**).
   s2 H2's deferred fix names this parser as its mechanism — *"a parser already exists"* — so
   deferring both builds the fix on a parser that manufactures measured sets.
6. ⚠ **s3 H2 — a row whose status is not `accepted` produces no `Result` at all** (forty unsettled
   claims, clean report). L2.3's accepted-row filter makes this strictly more consequential.
7. ⚠ **s1 M1 — `GIT_LITERAL_PATHSPECS` absent from `HOSTILE_ENV`.** `harvest.artifact_set` is one of
   the three unpinned sites, and with `LITERAL=1` ambient **the `git diff` L1.5 guards exits 0 with an
   empty patch.**
8. **`Status.setup` -> `Status.builder_setup`** — L1.2 closed the VERDICT half (a failing
   verifier setup can no longer return `PASS`; `runner.SETUP_REFUSED` maps to `verify="not-run"`)
   and corrected `seat.py`'s docstring, which claimed the field was a fresh-clone measurement
   when `run_seat` fills it from the BUILDER's clone. The rename itself is an on-disk schema
   change plus a required keyword across ~42 `classify_seat` call sites, 30 of them in
   `test_forge_seat.py`, and it collides with the existing top-level `verifier_setup` key on the
   attempt row — so it belongs in a task whose reviewer is looking at a rename, not as a rider.
9. **s4 remainder** — H3 oscillation (see 10); M2 `snapshot.take`'s undeclared `FileNotFoundError`;
   M3 `Size` accepting `(0,0)`/negatives/bools; M4 `_dir_digest`; L1 the lost-journal/no-fixes collapse.
10. **The blind-review boundary, really closed** — OS-level isolation for reviewers and seats, or
   holding the ledger in memory for the round. L1.4 fixes the direction bug and **writes the residual
   down**; this is the closure.
11. **The oscillation wiring** — widen `review.py:1629`'s `fix` contract to return the candidate and
    baseline `Run`s, call `from_runs` before `record_fix_done`, consult `oscillation` per round.
    **Correction:** the earlier draft attributed this to "Plan K Tasks 3 and 4, which own the
    contract." They do not — K Task 3 is `--collect`'s argv surface and Task 4 is the review clone, and
    **Plan K's own Order-of-work hands the review verb and `loop` to Plan L.** The real dependency is
    that **K Task 4 changes `loop`'s signature** and therefore edits `review.py:1699-1703` — the exact
    `prog=progress.Progress(None, None)` call site this item is about. **Sequence it immediately after
    K Task 4, as one visit.**
12. **s5** — C4 and all six Highs: council result files outside the bracket with no integrity re-check;
    the review bundle inside `.git` with its path in argv and no digest; repo-local diff drivers and
    `git replace` refs unmeasured for reviewers' own `git diff`; **a fully-silent 0/3 panel classifying
    `degraded` (ships) rather than `review_blocked`**; ultrareview's absent journal and durable receipt;
    plus its seven Mediums.
13. **s6** — `gc` deleting refs by namespace prefix rather than exact name+OID; the `PATCH_ONLY`
    handover citing a patch nothing generates; `--collect` discarding §9 drift and §14.1 orphans; the
    cloud review's missing idempotency guard **whose own refusal text tells the operator to re-trigger
    it**; the seat-count denominator taken from a disk glob rather than `manifest.seats`; the verify
    command truncated to step 0; **the handover asserting the synthesis branch from the run id while
    measuring HEAD, with `--gc` then reclaiming the difference**; plus its five Mediums.
14. **s7** — **"fusion, not selection" is unenforced** (the collector rejects only a tree identical to
    B₁; `--strategy` is reported and `cli.py:772` says it cannot be checked); the eval-baseline
    contamination (**after L3.1**, which fixes the oracle that would penalise a corrected baseline);
    `--collect`'s re-payable review; `mutate.py`'s bytecode purge and missing path containment;
    `eval_trigger`'s type coercions (`"false"` → true; `null` → `"None"` → the abstention label);
    `reconcile`'s orphaned-marker destruction and `backup()` overwrite; `--seats 1` vs "all three CLIs".
15. **s1 remainder** — the symlink gate referent; the make memo key **(fix together with `_scan_make`'s
    `--directory=`/`-C` parser gap — two holes in one detector)**; the calibration aggregate; the
    control-plane integrity tripwire; **Fwork byte-binding** (L1.5's other door); durable-state
    reconstruction; `Seat.verified` over `sidecars is None`; two index definitions; `_gate_taints`'
    `isinstance` gate; `_command_paths`' silent `continue`; `_AMBIENT_SKILL`'s short-path refusals;
    `screen.py` carrying this repo's allow-list into foreign repos; `fleet.clone_seat`'s bare
    `IndexError`; `Quote`'s unvalidated fields.
16. **s2 remainder** — `no_change` with `proven_read=False`; **the executed-and-refuted check recorded
    as `not-run`**; §8.1's missing input half; `RunnerError`-as-retry; the empty fleet reaching
    `comparing`; `FLAKY` unreachable; `_clip`'s evidence truncation; `_verify_dim`'s collapse.
17. **s3 remainder** — `installed_closure`'s permutation collision; `verify_materialized`'s copied
    fields **and** the size/cap-blind `bundle_hash`; the criterion-to-claim binding; seat provenance;
    the journal creation race; hash criteria not distinguishing a file from a symlink.
18. **§18's live three-provider write smoke** — no in-repo receipt with the required provenance exists.

---

## Self-review

**Spec coverage.** L0 closes three measured defects. L1 closes the founding-premise cluster (gate surface, setup failure, calibration, harvest filter, isolation prose) and the blind-review boundary. L2 closes the `manual_trace_confirmed` cluster and the pytest receipt. L3 makes the eval set and receipt able to fail. L4 makes the quote structurally honest. Everything not covered is in the deferral list with its finding.

**Placeholder scan.** No "TBD"/"add error handling"/"similar to Task N". L1.5, L2.2, L3.1–L3.3 give the rule and the assertion rather than every line — deliberate, because each depends on a fixture helper the implementer will write against the existing suite's conventions; each names the exact file, line and expected outcome.

**Type consistency.** `coverage.unmeasured(results) -> tuple[Result, ...]` and `Report.traced` are used identically in L2.1's three call sites. `verify.remeasure_gate_surface(v) -> Verifier` matches its `runner.py` call. `verify.SETUP_FAILED` is added in L1.2 and consumed by `_verify_dim` in the same task. `Status.setup` → `Status.builder_setup` + `Status.verifier_setup` is renamed once, in L1.2, and L1.1 does not touch it.

**One known ordering constraint:** L1.1 and L1.2 both edit `runner.py:1122-1145`. Execute L1.1 first; L1.2's early return sits above L1.1's `remeasure_gate_surface` call.
