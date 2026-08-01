# llm-forge Plan E: the producible verdict

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `verify.classify` able to return `PASS` — and able to return it honestly — by closing the three gaps Plan D measured and carried, plus spec §6 step 3.

**Architecture:** Plan D built every part of a verdict and wired none of them to a producer. `gate_delta` is always `None`, so every classification is `GATE_CHANGED`; `baseline_run` has no producer at all, so `BASELINE_RED_NO_NEW_IDENTIFIED_FAILURE` can never fire; `generator_contract_id` is written by nobody and read by nobody, so "a seat cannot widen the contract" holds only by the accident of both sides being empty; and the confirmed setup command is never re-run in the verifier, so setup-phase state the builder controls is excluded from harvest by an argument no code makes. This plan supplies the missing producers, all inside `verify.py` and `bundle.py`, and converts Plan D's reachability pins from "this cannot happen" into "this is what happens".

**Tech Stack:** Python 3.11+ stdlib only. `git` 2.53 via `shared/lib/forge/gitcmd.py`. pytest via `uvx`.

## Global Constraints

- **Python stdlib only.** No pip dependencies. Must run on any Python 3.11+ machine with no install step.
- **Commands run as argv lists, never through a shell.** Shell metacharacter syntax is rejected, not reinterpreted (spec §5.1).
- **Git is located by asking git**, never by string-joining `.git`. Every git call goes through `gitcmd.git`.
- **Fail closed.** Never report a partial result as complete. A measurement that could not be taken is `None`/UNKNOWN, never an empty success.
- **A verdict must never read cleaner than its evidence.**
- **A comment asserting something the code does not do is a defect.** Sweep your own prose against the code as it stands, not as you were thinking about it while writing.
- `shared/lib/forge/**` is source of truth; `marketplaces/**` is render output. Never hand-edit it — run `make render`.
- Every task ends with `make render`, `make verify`, `make precommit` and an explicit-pathspec commit. Never `git add -A`.

## What Plan D hands you, verbatim

Verify these against the code before relying on them — every plan in this project has had draft code that was wrong, and several controller instructions have been measurably wrong. Three implementers and one reviewer overturned an instruction by measuring instead of complying; that is the behaviour this plan wants.

- `bundle.CandidateBundle(version, baseline_ref, baseline_commit, tracked_patch=b"", sidecars=(), gate_delta=None, generator_contract_id="", omitted=())` — **frozen**. `gate_delta is None` means "nobody looked" and a consumer must read it as UNKNOWN; `()` would be the fail-open reading. `generator_contract_id=""` means "the run declared no contract", which admits nothing — the fail-closed sentinel.
- `bundle.SidecarEntry(path, kind, mode, payload)`; `kind` is `"file"` or `"symlink"`; a symlink's `mode` is a fabricated `0` and its `payload` is the target text encoded with `surrogateescape`.
- `bundle.build(seat_path, artifacts, baseline) -> CandidateBundle`; `bundle.materialize(bundle, dest) -> tuple[str, ...]` returns every path touched, **sorted and once each** (a set, not a log). `materialize` checks `dest` sits at the bundle's baseline commit **before** writing anything. `BundleError`.
- `bundle.VERSION` is the schema integer `build` stamps.
- `verify.build_verifier(repo, baseline, candidate, dest, *, identity) -> Path` — `fleet.clone_seat` + a hooks pin + `bundle.materialize` + a pin readback. **This plan changes its signature and return type.**
- `verify.Command.parse(spec) -> Command` — a spec is a LIST of argv lists. `verify.Step(argv, cwd=None, env=None)`. `verify.run_command(cwd, command, *, env=None) -> Run`.
- `verify.Run(exit_code, stdout, stderr, duration_sec, step_index)`.
- `verify.fixed_point(verifier_path, command, contract, *, max_passes=2, env=None) -> FixedPoint`; `FixedPoint(run, admitted=(), unexplained=())`. `unexplained` is measured **only for a pass whose gate exited 0**; on a failing run it holds whatever earlier passes measured. `admitted` is what the ENGINE staged, not everything under the contract that moved. Raises `GeneratorUnstable` (a `VerifyError`) on non-convergence.
- `verify.gate_surface(verifier_path, contract, *, command=None) -> tuple[str, ...]` — the files in ONE tree that define the gate. Sorted.
- `verify.classify(candidate_run, baseline_run, bundle, *, rerun=None) -> tuple[str, str]`. Accepts a `Run` or a `FixedPoint` for either run. Outcome constants `PASS`, `FAIL`, `FLAKY`, `BASELINE_RED_NO_NEW_IDENTIFIED_FAILURE`, `HARVEST_INCOMPLETE`, `GATE_CHANGED`; the `OUTCOMES` tuple.
- `verify._inventory(root) -> dict` — one content-keyed inventory under `Quota.for_harvest()`, raising `VerifyError` on a quota breach rather than returning `{}` (which would manufacture a fixed point). `verify._tracked(root) -> frozenset` — the index, decoded with `surrogateescape`. `verify._declared(contract, path) -> bool` — matches the OUTPUT glob only; `*` crosses `/`.
- `inspect.GeneratorContract(id="", relations=())`; a relation is `(source glob, output glob)` and only the output side decides admission. `relations` non-empty with an empty `id` raises `ValueError`. `inspect.detect_generators(repo) -> GeneratorContract` returns the **empty** contract today, on measurement — the relation lives in `scripts/render.py` as Python, and importing it would mean the engine executing builder-controlled code outside the gate.
- `fleet.clone_seat(repo, baseline, dest, *, name, identity, template_dir=None) -> Seat` with `.path/.branch/.verified/.replayed`; `SeatError`. `fleet.forge_child_env(repo)`.
- `baseline.materialize(repo, run_dir, facts, selected_untracked, run_id, author=None) -> Baseline` with `.base_commit/.tracked_tree_oid/.commit/.ref/.dirty/.sidecars/.filesystem_manifest`. `Baseline.sidecars` is `None` meaning "nobody looked".
- `gitcmd.git(repo, *args, env_extra=None, check=True, binary=False, timeout=60)`; `GitError`. **`HOSTILE_ENV` is the list any child environment strips** — `REDIRECTING_ENV` is the strictly narrower subset it is built from, and reaching for it leaves `GIT_CONFIG_COUNT`, `GIT_CONFIG_PARAMETERS` and `GIT_TEMPLATE_DIR` ambient. `READONLY`, `NO_USER_CONFIG`.
- `snapshot.take(root, *, quota=None, skip_dirs=(".git",))` prunes at **every** level; `snapshot.diff(before, after)` is content-keyed; `Entry.kind` never emits `"dir"`, so **empty directories are invisible to `diff()`**.
- Test fixtures: `tests/forge_fixtures.py` — `make_repo`, `write`, `commit_all`, `git`. Suites are `tests/test_forge_*.py`, all named in `FORGE_TESTS` in the `Makefile`, and `tests/test_forge_packaging.py` asserts that set-equality.

## Two gates this plan must not break

`tests/test_forge_packaging.py` carries two prose gates added at the end of Plan D. Both will fire on sloppy work in this plan, and both are meant to:

- `test_every_test_named_in_shipped_forge_prose_still_exists` — a docstring in `shared/lib/forge/**` naming a `test_…` that does not exist fails the build. **You will rename tests in Task 5**; sweep the citations with them.
- `test_no_shipped_forge_prose_dates_itself_to_a_commit` — shipped prose may not say `this commit`, `until now`, or `Task N`. Say what the constraint is, not when it was written. Note the second one especially: several comments you are about to *replace* say a producer does not exist, and the replacement must not simply move the tense.

## File Structure

- `shared/lib/forge/verify.py` — every new producer lands here. It is already the largest module in the package (~1200 lines); this plan adds roughly 250. If a reviewer calls for a split, the natural seam is `gate_surface` + its role tables into `shared/lib/forge/surface.py`, but **do not split it as part of this plan** — a split and a semantics change in one diff is unreviewable.
- `shared/lib/forge/bundle.py` — `build` gains a contract parameter; a new `with_gate_delta` returns a new frozen instance.
- `tests/test_forge_verify.py`, `tests/test_forge_bundle.py`, `tests/test_forge_seams.py` — extended.
- No new module, no new suite. `FORGE_TESTS` therefore needs no edit, and `test_every_forge_suite_is_named_in_the_makefile_gate` proves it.

---

### Task 1: Bind the contract to the bundle

**Files:**
- Modify: `shared/lib/forge/bundle.py` (`build`)
- Modify: `shared/lib/forge/verify.py` (`build_verifier`)
- Test: `tests/test_forge_bundle.py`, `tests/test_forge_verify.py` (extend)

**Interfaces:**
- Consumes: `bundle.build(seat_path, artifacts, baseline)`, `verify.build_verifier(repo, baseline, candidate, dest, *, identity)`, `inspect.GeneratorContract`.
- Produces:
  - `bundle.build(seat_path, artifacts, baseline, *, contract=None) -> CandidateBundle` — stamps `generator_contract_id=contract.id` (or `""` when `contract is None`).
  - `verify.build_verifier(repo, baseline, candidate, dest, *, identity, contract) -> Path` — `contract` is a **required keyword**. Refuses when `candidate.generator_contract_id != contract.id`.
  - `verify.ContractMismatch(VerifyError)`.

**Why this is Task 1.** The gate admits verify-origin rewrites under a contract. The manifest records which contract. Today nothing writes the id and nothing compares it, so a manifest recording contract `X` while the gate admitted under contract `Y` is undetectable — and both sides being `""` is the only reason that is not already a hole. Tasks 2–4 all hand a contract to `build_verifier`; binding it first means they inherit the check rather than each restating it.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_forge_bundle.py  (append)
def test_build_stamps_the_runs_contract_id_into_the_bundle(tmp_path):
    repo = make_repo(tmp_path)
    run = tmp_path / "run"; run.mkdir()
    b = baseline.materialize(repo, run, finspect.repo_facts(repo), [], "r1")
    s = fleet.clone_seat(repo, b, tmp_path / "seat", name="claude", identity=IDENT)
    f0 = harvest.record(s.path)
    write(s.path, "src.py", "work\n")
    fw = harvest.record(s.path)
    a = harvest.artifact_set(
        harvest.Phases(f0=f0, fsetup=f0, fwork=fw, fverify=fw), s.path, b.commit)
    c = finspect.GeneratorContract(id="render-v1", relations=(("shared/*", "gen/*"),))
    assert bundle.build(s.path, a, b, contract=c).generator_contract_id == "render-v1"


def test_build_without_a_contract_stamps_the_fail_closed_sentinel(tmp_path):
    """No contract is not the same claim as an unrecorded one: "" admits nothing."""
    repo = make_repo(tmp_path)
    run = tmp_path / "run"; run.mkdir()
    b = baseline.materialize(repo, run, finspect.repo_facts(repo), [], "r1")
    s = fleet.clone_seat(repo, b, tmp_path / "seat", name="claude", identity=IDENT)
    f0 = harvest.record(s.path)
    a = harvest.artifact_set(
        harvest.Phases(f0=f0, fsetup=f0, fwork=f0, fverify=f0), s.path, b.commit)
    assert bundle.build(s.path, a, b).generator_contract_id == ""
```

```python
# tests/test_forge_verify.py  (append)
def test_a_verifier_refuses_a_bundle_built_under_another_contract(tmp_path):
    """The manifest's contract and the gate's contract are one fact or the run is a lie."""
    repo, b, cb = _repo_baseline_bundle(tmp_path, contract_id="render-v1")
    other = finspect.GeneratorContract(id="render-v2", relations=(("a/*", "b/*"),))
    with pytest.raises(verify.ContractMismatch) as e:
        verify.build_verifier(repo, b, cb, tmp_path / "verifier",
                              identity=IDENT, contract=other)
    assert "render-v1" in str(e.value) and "render-v2" in str(e.value)


def test_a_verifier_refuses_a_contract_when_the_bundle_recorded_none(tmp_path):
    """The dangerous direction: a bundle that admits nothing, verified under one that does."""
    repo, b, cb = _repo_baseline_bundle(tmp_path, contract_id=None)
    c = finspect.GeneratorContract(id="render-v1", relations=(("shared/*", "gen/*"),))
    with pytest.raises(verify.ContractMismatch):
        verify.build_verifier(repo, b, cb, tmp_path / "verifier",
                              identity=IDENT, contract=c)


def test_a_verifier_accepts_the_contract_its_bundle_was_built_under(tmp_path):
    repo, b, cb = _repo_baseline_bundle(tmp_path, contract_id="render-v1")
    c = finspect.GeneratorContract(id="render-v1", relations=(("shared/*", "gen/*"),))
    v = verify.build_verifier(repo, b, cb, tmp_path / "verifier",
                              identity=IDENT, contract=c)
    assert (v / "src.py").is_file()
```

Write `_repo_baseline_bundle(tmp_path, *, contract_id)` yourself as a module-level helper in `tests/test_forge_verify.py`: it must build a real repo, baseline, seat and bundle through the chain (`make_repo` → `baseline.materialize` → `fleet.clone_seat` → one `write` of `src.py` → `harvest.record` ×2 → `harvest.artifact_set` → `bundle.build`), passing `contract=finspect.GeneratorContract(id=contract_id, relations=(("shared/*", "gen/*"),))` when `contract_id` is not `None`. Returning a hand-constructed `CandidateBundle` instead would test the check against a shape `build` never produces.

- [ ] **Step 2: Run to verify they fail**

Run: `uvx pytest tests/test_forge_bundle.py tests/test_forge_verify.py -q`
Expected: `TypeError: build() got an unexpected keyword argument 'contract'` and `AttributeError: module 'forge.verify' has no attribute 'ContractMismatch'`.

- [ ] **Step 3: Implement**

In `bundle.build`, add the keyword and stamp it:

```python
def build(seat_path, artifacts, baseline, *, contract=None) -> CandidateBundle:
```

and in the returned `CandidateBundle(...)`, add:

```python
        generator_contract_id=contract.id if contract is not None else "",
```

In `verify.py`, add the error class beside `GeneratorUnstable`:

```python
class ContractMismatch(VerifyError):
    """A bundle and a verifier disagree about which generator contract the run declared.

    Fatal rather than resolved in either direction. Taking the bundle's would let a
    candidate's own recorded id decide what the gate admits; taking the verifier's would
    write a manifest whose contract is not the one the gate ran under. Both are the failure
    §7.2 names — a success criterion the run did not confirm.
    """
```

and make `build_verifier` take and check it, **before** the clone, so a mismatch costs no disk:

```python
def build_verifier(repo, baseline, candidate, dest, *, identity, contract) -> Path:
    if candidate.generator_contract_id != contract.id:
        raise ContractMismatch(
            f"the candidate was built under generator contract "
            f"{candidate.generator_contract_id!r} and this verifier was handed "
            f"{contract.id!r}; a run has one contract, confirmed once at the §5 gate")
    seat = fleet.clone_seat(repo, baseline, dest, name=VERIFIER_NAME, identity=identity)
```

Update `build_verifier`'s docstring: `contract` is required rather than defaulted because a default is a policy, and the policy this field encodes is confirmed by a human at the §5 gate.

- [ ] **Step 4: Update the existing callers**

Every existing `build_verifier(...)` call in `tests/` must pass `contract=finspect.GeneratorContract()` unless the test is about a contract. That is the empty contract — the same fail-closed value `detect_generators` returns — so no existing assertion changes meaning.

Run: `uvx pytest tests/test_forge_verify.py tests/test_forge_bundle.py tests/test_forge_seams.py -q`
Expected: all pass.

- [ ] **Step 5: Mutate**

Mutate **one site at a time** and report the table. At minimum: `!=` → `==`; the raise → `pass`; `contract.id` → `""` in `build`; the check moved *after* `clone_seat` (must still be caught — by a test asserting `dest` does not exist after a mismatch; add it if nothing catches it).

For every new fixture, run it against both the correct and the mutated form and confirm they **disagree**. A fixture that rejects under both is decoration — this project has shipped three.

- [ ] **Step 6: Render, gate, commit**

```bash
make render
make verify
make precommit
git add shared/lib/forge/bundle.py shared/lib/forge/verify.py tests/test_forge_bundle.py tests/test_forge_verify.py tests/test_forge_seams.py marketplaces
git commit -m "feat(forge): one contract per run, bound to the bundle that records it"
```

---

### Task 2: Produce the gate delta

**Files:**
- Modify: `shared/lib/forge/bundle.py` (add `with_gate_delta`)
- Modify: `shared/lib/forge/verify.py` (`build_verifier`)
- Test: `tests/test_forge_bundle.py`, `tests/test_forge_verify.py` (extend)

**Interfaces:**
- Consumes: Task 1's `build_verifier(..., contract=)`; `verify.gate_surface(verifier_path, contract, *, command=None)`.
- Produces:
  - `bundle.with_gate_delta(candidate, delta) -> CandidateBundle` — a new frozen instance with `gate_delta` set. Refuses to overwrite a delta that is already not `None`.
  - `verify.Verifier` — frozen: `path: Path`, `candidate: CandidateBundle`, `baseline_surface: tuple[str, ...]`, `candidate_surface: tuple[str, ...]`.
  - `verify.build_verifier(repo, baseline, candidate, dest, *, identity, contract, command=None) -> Verifier` — **return type changes from `Path` to `Verifier`.**

**Why the delta is produced here and nowhere else.** `gate_surface` answers one tree. A delta needs two, and the only place both exist is between the clone and the materialization: `clone_seat` leaves the verifier holding exactly the baseline, and `materialize` turns it into exactly the candidate. Measuring there costs one extra `gate_surface` call and no extra clone. `bundle.build` cannot do it — it is handed a seat path, an `ArtifactSet` and a `Baseline`, and the seat's tree is builder-writable, which is the one tree a gate surface must not be read from.

**The return-type change is deliberate.** `build_verifier` currently returns a `Path`, and every caller then classifies with `gate_delta=None` — i.e. every caller today produces `GATE_CHANGED`. Adding a second entry point would leave the old one as a "do not use this" that tests keep using. One entry point, one honest answer.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_forge_bundle.py  (append)
def test_with_gate_delta_returns_a_new_bundle_and_leaves_the_original(tmp_path):
    cb = bundle.CandidateBundle(version=bundle.VERSION, baseline_ref="r", baseline_commit="c")
    out = bundle.with_gate_delta(cb, ("Makefile",))
    assert out.gate_delta == ("Makefile",)
    assert cb.gate_delta is None, "the input is frozen and must be untouched"
    assert out.baseline_commit == "c" and out.version == bundle.VERSION


def test_with_gate_delta_refuses_to_overwrite_a_measurement(tmp_path):
    """Two measurements of one tree pair disagree only if one of them is wrong."""
    cb = bundle.CandidateBundle(version=bundle.VERSION, baseline_ref="r",
                                baseline_commit="c", gate_delta=())
    with pytest.raises(bundle.BundleError):
        bundle.with_gate_delta(cb, ("Makefile",))


def test_with_gate_delta_accepts_the_empty_measurement(tmp_path):
    """() is a RESULT here — the candidate changed nothing that defines the gate."""
    cb = bundle.CandidateBundle(version=bundle.VERSION, baseline_ref="r", baseline_commit="c")
    assert bundle.with_gate_delta(cb, ()).gate_delta == ()
```

```python
# tests/test_forge_verify.py  (append)
def test_a_candidate_that_edits_the_gate_lands_in_the_bundles_delta(tmp_path):
    """The measurement §6.1 asks for, taken between the clone and the candidate."""
    repo = make_repo(tmp_path)
    write(repo, "Makefile", "verify:\n\t@true\n")
    write(repo, "tests/test_a.py", "def test_a():\n    assert True\n")
    commit_all(repo, "gate")
    run = tmp_path / "run"; run.mkdir()
    b = baseline.materialize(repo, run, finspect.repo_facts(repo), [], "r1")
    s = fleet.clone_seat(repo, b, tmp_path / "seat", name="claude", identity=IDENT)
    f0 = harvest.record(s.path)
    write(s.path, "Makefile", "verify:\n\t@echo weakened\n")
    fw = harvest.record(s.path)
    a = harvest.artifact_set(
        harvest.Phases(f0=f0, fsetup=f0, fwork=fw, fverify=fw), s.path, b.commit)
    cb = bundle.build(s.path, a, b)
    v = verify.build_verifier(repo, b, cb, tmp_path / "verifier",
                              identity=IDENT, contract=finspect.GeneratorContract())
    assert "Makefile" in v.candidate.gate_delta
    assert "tests/test_a.py" not in v.candidate.gate_delta, \
        "an untouched gate file is surface, not delta"


def test_a_candidate_that_leaves_the_gate_alone_gets_an_empty_delta_not_none(tmp_path):
    """The whole point: () is measured-and-clean, and only () can reach PASS."""
    repo = make_repo(tmp_path)
    write(repo, "Makefile", "verify:\n\t@true\n")
    commit_all(repo, "gate")
    run = tmp_path / "run"; run.mkdir()
    b = baseline.materialize(repo, run, finspect.repo_facts(repo), [], "r1")
    s = fleet.clone_seat(repo, b, tmp_path / "seat", name="claude", identity=IDENT)
    f0 = harvest.record(s.path)
    write(s.path, "src.py", "work\n")
    fw = harvest.record(s.path)
    a = harvest.artifact_set(
        harvest.Phases(f0=f0, fsetup=f0, fwork=fw, fverify=fw), s.path, b.commit)
    cb = bundle.build(s.path, a, b)
    v = verify.build_verifier(repo, b, cb, tmp_path / "verifier",
                              identity=IDENT, contract=finspect.GeneratorContract())
    assert v.candidate.gate_delta == ()
    r = verify.run_command(v.path, verify.Command.parse([["true"]]))
    assert verify.classify(r, r, v.candidate)[0] == verify.PASS, \
        "a clean gate delta is what makes PASS reachable at all"


def test_a_candidate_that_deletes_a_discovered_test_lands_in_the_delta(tmp_path):
    """The §6 threat verbatim: delete a test file, leave the Makefile untouched."""
    repo = make_repo(tmp_path)
    write(repo, "Makefile", "verify:\n\t@true\n")
    write(repo, "tests/test_a.py", "def test_a():\n    assert True\n")
    commit_all(repo, "gate")
    run = tmp_path / "run"; run.mkdir()
    b = baseline.materialize(repo, run, finspect.repo_facts(repo), [], "r1")
    s = fleet.clone_seat(repo, b, tmp_path / "seat", name="claude", identity=IDENT)
    f0 = harvest.record(s.path)
    _git(s.path, "rm", "-q", "tests/test_a.py")
    fw = harvest.record(s.path)
    a = harvest.artifact_set(
        harvest.Phases(f0=f0, fsetup=f0, fwork=fw, fverify=fw), s.path, b.commit)
    cb = bundle.build(s.path, a, b)
    v = verify.build_verifier(repo, b, cb, tmp_path / "verifier",
                              identity=IDENT, contract=finspect.GeneratorContract())
    assert "tests/test_a.py" in v.candidate.gate_delta
```

- [ ] **Step 2: Run to verify they fail**

Run: `uvx pytest tests/test_forge_bundle.py tests/test_forge_verify.py -q`
Expected: `AttributeError: module 'forge.bundle' has no attribute 'with_gate_delta'`, and the verifier tests failing on `'PosixPath' object has no attribute 'candidate'`.

- [ ] **Step 3: Implement `with_gate_delta`**

```python
def with_gate_delta(candidate: CandidateBundle, delta) -> CandidateBundle:
    """The same candidate with its gate-surface delta recorded.

    A new instance rather than a mutation because `CandidateBundle` is frozen, and it is
    frozen because a candidate that can be edited after it is built is a candidate whose
    manifest describes something else.

    Refuses when a delta is already recorded. Two measurements of one tree pair agree or
    one of them is wrong, and silently taking the second would make which one you get
    depend on call order.
    """
    if candidate.gate_delta is not None:
        raise BundleError(
            f"this candidate already records a gate delta ({candidate.gate_delta!r}); a "
            f"second measurement ({tuple(delta)!r}) is a disagreement, not an update")
    return replace(candidate, gate_delta=tuple(delta))
```

`replace` is `dataclasses.replace` — add it to `bundle.py`'s existing `from dataclasses import …` line.

- [ ] **Step 4: Implement `Verifier` and the measurement**

```python
@dataclass(frozen=True)
class Verifier:
    """A tree the builder never had access to, and what building it measured.

    `candidate` is NOT the bundle the caller passed in: it is that bundle with its
    `gate_delta` filled from the two trees this function had and no other caller has. A
    caller that classifies the input bundle instead gets `gate_delta is None`, which is
    UNKNOWN, which is `GATE_CHANGED` — correct but useless.
    """
    path: Path
    candidate: bundle.CandidateBundle
    baseline_surface: tuple[str, ...] = ()
    candidate_surface: tuple[str, ...] = ()
```

and in `build_verifier`, after `_hooks_pin` and around `materialize`:

```python
    _hooks_pin(seat.path)
    # BEFORE the candidate: this is the only moment the verifier holds exactly the
    # baseline. `clone_seat` has verified the checkout against B1's manifest, so it is also
    # the only gate surface in the run that is known not to have been written by a builder.
    before = gate_surface(seat.path, contract, command=command)
    bundle.materialize(candidate, seat.path)
    _assert_hooks_pinned(seat.path)
    after = gate_surface(seat.path, contract, command=command)
    return Verifier(
        path=seat.path,
        candidate=bundle.with_gate_delta(candidate, sorted(set(before) ^ set(after))),
        baseline_surface=before,
        candidate_surface=after,
    )
```

The delta is a **symmetric difference**: a gate file the candidate deletes leaves the surface and a gate file it adds enters it, and §6.1 requires both. An asymmetric `after - before` would report an added test file and miss a deleted one, which is the exact §6 threat.

**A surface change is not the same as a content change**, and this measurement sees only the first. A candidate that rewrites `Makefile` without adding or removing any gate-defining path produces an empty symmetric difference. Test `test_a_candidate_that_edits_the_gate_lands_in_the_bundles_delta` above expects `Makefile` in the delta, so **this is the step where the plan's draft may be wrong**: run it before believing it. If a content-edited gate file does not appear, the delta must be computed over `(path, content hash)` pairs for the surface, not over paths alone — measure which, implement that, and say what you measured. Do not weaken the test.

- [ ] **Step 5: Update the existing callers**

Every existing `v = verify.build_verifier(...)` in `tests/` becomes `v = verify.build_verifier(...).path`, except where the test is about the delta. Mechanical; the type checker is your test runner here.

Run: `uvx pytest tests/ -m "not slow" -q`
Expected: all pass.

- [ ] **Step 6: Mutate**

One site at a time. At minimum: `^` → `-`; `^` → `|`; `sorted(set(...))` → `tuple(...)` (ordering); `before` measured after `materialize`; `with_gate_delta` → returning the input unchanged; the `is not None` guard inverted. The `^` → `-` mutant must be killed by the deletion test — if it is not, that test is decoration.

- [ ] **Step 7: Render, gate, commit**

```bash
make render
make verify
make precommit
git add shared/lib/forge/bundle.py shared/lib/forge/verify.py tests marketplaces
git commit -m "feat(forge): measure the gate delta where both trees exist"
```

---

### Task 3: Replay setup in the verifier

**Files:**
- Modify: `shared/lib/forge/verify.py`
- Test: `tests/test_forge_verify.py` (extend)

**Interfaces:**
- Consumes: Task 2's `Verifier`; `verify._inventory`, `verify._tracked`, `verify._declared`, `verify.run_command`, `verify.Command`.
- Produces:
  - `verify.SetupResult` — frozen: `run: Run`, `overlap: tuple[str, ...]`.
  - `verify.run_setup(verifier, setup, contract, *, env=None) -> SetupResult` — takes a `Verifier`, not a path.
  - `verify.SetupOverlap(VerifyError)`.
  - `verify.validate_materialized(verifier) -> None`; raises `VerifyError`.

**Why.** Spec §6 runs the confirmed setup command in the verifier at step 3, *after* hash-validating the materialized candidate at step 2. Neither exists. The consequence Plan D measured and could not close: a rig the builder places during its own *work* phase crosses as a sidecar, and the argument that it does not matter rests on setup being re-run here. Also §6: **setup must leave tracked files clean** — a setup step with tracked effects would be applied twice, once inside the candidate's content and once when the verifier re-runs setup. A tracked `Vsetup` delta outside the contract fails the candidate closed as `setup_overlap` rather than being double-applied.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_forge_verify.py  (append)
def test_the_materialized_candidate_is_validated_before_setup_runs(tmp_path):
    """§6 step 2 before step 3: a tree that does not match the bundle never runs setup."""
    repo, b, cb = _repo_baseline_bundle(tmp_path, contract_id=None)
    v = verify.build_verifier(repo, b, cb, tmp_path / "verifier",
                              identity=IDENT, contract=finspect.GeneratorContract())
    verify.validate_materialized(v)                      # clean
    (v.path / "src.py").write_text("tampered\n")
    with pytest.raises(verify.VerifyError) as e:
        verify.validate_materialized(v)
    assert "src.py" in str(e.value)


def test_setup_that_changes_a_tracked_file_fails_the_candidate_closed(tmp_path):
    """§6: it would otherwise be applied twice — once in the candidate, once here."""
    repo = make_repo(tmp_path)
    write(repo, "schema.lock", "1\n")
    commit_all(repo, "seed")
    run = tmp_path / "run"; run.mkdir()
    b = baseline.materialize(repo, run, finspect.repo_facts(repo), [], "r1")
    cb = bundle.CandidateBundle(version=bundle.VERSION, baseline_ref=b.ref,
                                baseline_commit=b.commit)
    v = verify.build_verifier(repo, b, cb, tmp_path / "verifier",
                              identity=IDENT, contract=finspect.GeneratorContract())
    setup = verify.Command.parse(
        [[sys.executable, "-c", "open('schema.lock','w').write('2\\n')"]])
    with pytest.raises(verify.SetupOverlap) as e:
        verify.run_setup(v, setup, finspect.GeneratorContract())
    assert "schema.lock" in str(e.value)


def test_setup_that_only_touches_untracked_paths_is_clean(tmp_path):
    """Installing a toolchain is what setup is FOR; only tracked effects are the defect."""
    repo, b, cb = _repo_baseline_bundle(tmp_path, contract_id=None)
    v = verify.build_verifier(repo, b, cb, tmp_path / "verifier",
                              identity=IDENT, contract=finspect.GeneratorContract())
    setup = verify.Command.parse(
        [[sys.executable, "-c", "import os; os.makedirs('.venv'); "
          "open('.venv/pytest','w').write('#!/bin/sh\\n')"]])
    out = verify.run_setup(v, setup, finspect.GeneratorContract())
    assert out.run.exit_code == 0 and out.overlap == ()


def test_a_tracked_setup_effect_the_contract_declares_is_not_an_overlap(tmp_path):
    """A generator IS allowed to run in setup — that is what the contract admits."""
    repo = make_repo(tmp_path)
    write(repo, "gen/a.txt", "1\n")
    commit_all(repo, "seed")
    run = tmp_path / "run"; run.mkdir()
    b = baseline.materialize(repo, run, finspect.repo_facts(repo), [], "r1")
    c = finspect.GeneratorContract(id="g1", relations=(("src/*", "gen/*"),))
    cb = bundle.CandidateBundle(version=bundle.VERSION, baseline_ref=b.ref,
                                baseline_commit=b.commit, generator_contract_id="g1")
    v = verify.build_verifier(repo, b, cb, tmp_path / "verifier", identity=IDENT, contract=c)
    setup = verify.Command.parse(
        [[sys.executable, "-c", "open('gen/a.txt','w').write('2\\n')"]])
    assert verify.run_setup(v, setup, c).overlap == ()


def test_a_failing_setup_returns_its_run_rather_than_raising(tmp_path):
    """A setup that exits nonzero is a verdict about the candidate's tree, not a defect
    in the engine — the caller classifies it. Only an OVERLAP is a refusal."""
    repo, b, cb = _repo_baseline_bundle(tmp_path, contract_id=None)
    v = verify.build_verifier(repo, b, cb, tmp_path / "verifier",
                              identity=IDENT, contract=finspect.GeneratorContract())
    setup = verify.Command.parse([[sys.executable, "-c", "raise SystemExit(3)"]])
    out = verify.run_setup(v, setup, finspect.GeneratorContract())
    assert out.run.exit_code == 3 and out.overlap == ()
```

- [ ] **Step 2: Run to verify they fail**

Run: `uvx pytest tests/test_forge_verify.py -q`
Expected: `AttributeError: module 'forge.verify' has no attribute 'validate_materialized'`.

- [ ] **Step 3: Implement `validate_materialized`**

Compare the verifier's tree against the bundle's own sidecar payloads and the patch's postimage paths:

```python
def validate_materialized(verifier: Verifier) -> None:
    """The tree matches the bundle it was built from, or nothing else in §6 is trustworthy.

    Runs BEFORE setup because setup is the first thing that legitimately changes this tree:
    after it, "differs from the bundle" and "setup installed something" are the same
    observation, and the check can no longer be made.

    Only the SIDECARS are content-checked. The patch is applied by `git apply --index`,
    which fails loudly on a context mismatch and is already checked against the baseline
    commit by `materialize`; re-hashing its postimages would re-derive what git enforced.
    A sidecar is a raw write, and a raw write is what a mode-dropping or truncating
    materialization would get wrong.
    """
    bad = []
    for e in verifier.candidate.sidecars:
        p = verifier.path / e.path
        if e.kind == "symlink":
            actual = os.readlink(p).encode("utf-8", "surrogateescape") \
                if p.is_symlink() else None
        else:
            actual = p.read_bytes() if p.is_file() and not p.is_symlink() else None
        if actual != e.payload:
            bad.append(e.path)
    if bad:
        raise VerifyError(
            f"the materialized candidate does not match its bundle at "
            f"{_paths_phrase(bad)}; the gate would be measuring a tree nobody described")
```

- [ ] **Step 4: Implement `run_setup`**

```python
@dataclass(frozen=True)
class SetupResult:
    """What re-running the confirmed setup command in the verifier measured.

    `overlap` is the tracked paths setup changed that no relation declares. It is a tuple
    rather than a raise-only signal because a caller reporting the candidate wants the
    names, and `SetupOverlap` carries the same list in its message.
    """
    run: Run
    overlap: tuple[str, ...] = ()


class SetupOverlap(VerifyError):
    """Setup changed a tracked file the contract does not declare (spec §6).

    Fails the candidate CLOSED rather than continuing. The change is already inside the
    candidate's own B→final content, so letting the verifier's setup apply it again runs the
    gate against a tree that is neither the baseline nor the candidate — a `schema.lock`
    bumped to 2 by the seat and to 3 here.
    """


def run_setup(verifier: Verifier, setup: Command, contract, *, env=None) -> SetupResult:
    """Run the confirmed setup command in the verifier and refuse a tracked effect."""
    before = _inventory(verifier.path)
    tracked_before = _tracked(verifier.path)
    run = run_command(verifier.path, setup, env=env)
    after = _inventory(verifier.path)
    tracked = _tracked(verifier.path)
    moved = ({p for p in snapshot.diff(before, after) if p in tracked}
             | (tracked_before ^ tracked))
    overlap = tuple(sorted(p for p in moved if not _declared(contract, p)))
    if overlap:
        raise SetupOverlap(
            f"the setup command changed {_paths_phrase(overlap)}, which the run's generator "
            f"contract does not declare; those changes are already inside the candidate, so "
            f"running setup here would apply them twice")
    return SetupResult(run=run, overlap=overlap)
```

The `moved` expression is deliberately the same shape `fixed_point` uses — content that moved under a tracked path, **or** tracked-ness that moved in either direction. Plan D measured that a content-only predicate misses a `git add` of a carried sidecar and a `git rm --cached`, both of which leave the bytes untouched. Do not simplify it to a content diff.

- [ ] **Step 5: Run to verify they pass**

Run: `uvx pytest tests/test_forge_verify.py -q`
Expected: all pass.

- [ ] **Step 6: Mutate**

One site at a time: drop the `p in tracked` filter; drop the `tracked_before ^ tracked` half; `not _declared` → `_declared`; `raise` → `pass`; `validate_materialized` called after `run_command` instead of before; the symlink branch comparing `str` instead of bytes. Report the table with each fixture's discrimination check.

- [ ] **Step 7: Render, gate, commit**

```bash
make render
make verify
make precommit
git add shared/lib/forge/verify.py tests/test_forge_verify.py marketplaces
git commit -m "feat(forge): replay setup where the builder never was, and refuse its overlap"
```

---

### Task 4: Calibrate the baseline

**Files:**
- Modify: `shared/lib/forge/verify.py`
- Test: `tests/test_forge_verify.py` (extend)

**Interfaces:**
- Consumes: Tasks 1–3.
- Produces:
  - `verify.Calibration` — frozen: `run: Run`, `path: Path`, `admitted: tuple[str, ...]`, `converged: bool`.
  - `verify.calibrate(repo, baseline, dest, *, identity, contract, setup, command, env=None) -> Calibration`.

**Why.** `classify`'s `baseline_run` has no producer, so `BASELINE_RED_NO_NEW_IDENTIFIED_FAILURE` cannot fire. Worse, spec §6.2 makes that outcome a claim about a *calibration* — and `verify._as_run` is explicit that passing its type check "is no evidence that the run came from `run_command`, or from a verifier, or from anywhere in particular". A calibration run taken in a seat makes that outcome a check the builder can rig, which is the premise this whole layer defends. `calibrate` is the engine-owned producer: a sacrificial verifier built from an **empty candidate** — the untouched baseline — through the same `build_verifier` path as every other tree.

Spec §5 step 3 also gives calibration a second job: running verify **twice** on the untouched baseline, where the second pass must show zero tracked delta, converts "assume the generator is a fixed point" into measured evidence *before any provider spends a token*. That is exactly `fixed_point`, so `calibrate` uses it rather than restating the loop.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_forge_verify.py  (append)
def _calibrate_repo(tmp_path, makefile: str):
    repo = make_repo(tmp_path)
    write(repo, "check.sh", makefile)
    (Path(repo) / "check.sh").chmod(0o755)
    commit_all(repo, "gate")
    run = tmp_path / "run"; run.mkdir()
    return repo, baseline.materialize(repo, run, finspect.repo_facts(repo), [], "r1")


def test_calibration_runs_on_the_baseline_in_a_clone_the_builder_never_had(tmp_path):
    repo, b = _calibrate_repo(tmp_path, "#!/bin/sh\nexit 0\n")
    cal = verify.calibrate(
        repo, b, tmp_path / "cal", identity=IDENT,
        contract=finspect.GeneratorContract(),
        setup=verify.Command.parse([["true"]]),
        command=verify.Command.parse([["./check.sh"]]))
    assert cal.run.exit_code == 0 and cal.converged
    assert gitcmd.git(cal.path, "remote", env_extra=gitcmd.READONLY).stdout.strip() == "", \
        "a calibration tree is a verifier: no origin, like every other clone in the run"
    assert not (cal.path / "src.py").exists(), "the baseline, not any candidate"


def test_a_red_baseline_calibrates_red_rather_than_raising(tmp_path):
    """§6.2 needs the red result — it is what BASELINE_RED_… is a comparison against."""
    repo, b = _calibrate_repo(tmp_path, "#!/bin/sh\nexit 1\n")
    cal = verify.calibrate(
        repo, b, tmp_path / "cal", identity=IDENT,
        contract=finspect.GeneratorContract(),
        setup=verify.Command.parse([["true"]]),
        command=verify.Command.parse([["./check.sh"]]))
    assert cal.run.exit_code == 1


def test_a_calibration_run_makes_a_red_baseline_classifiable(tmp_path):
    """The seam this task exists to close: nothing could produce this argument before."""
    repo, b = _calibrate_repo(tmp_path, "#!/bin/sh\nexit 1\n")
    cal = verify.calibrate(
        repo, b, tmp_path / "cal", identity=IDENT,
        contract=finspect.GeneratorContract(),
        setup=verify.Command.parse([["true"]]),
        command=verify.Command.parse([["./check.sh"]]))
    cb = bundle.CandidateBundle(version=bundle.VERSION, baseline_ref=b.ref,
                                baseline_commit=b.commit, gate_delta=())
    outcome, _ = verify.classify(_run(1, "1 failed"), cal.run, cb)
    assert outcome == verify.BASELINE_RED_NO_NEW_IDENTIFIED_FAILURE


def test_calibration_refuses_a_setup_that_touches_a_tracked_file(tmp_path):
    """The §6 rule holds for calibration too — it is a verifier like any other."""
    repo, b = _calibrate_repo(tmp_path, "#!/bin/sh\nexit 0\n")
    with pytest.raises(verify.SetupOverlap):
        verify.calibrate(
            repo, b, tmp_path / "cal", identity=IDENT,
            contract=finspect.GeneratorContract(),
            setup=verify.Command.parse(
                [[sys.executable, "-c", "open('check.sh','w').write('#!/bin/sh\\n')"]]),
            command=verify.Command.parse([["./check.sh"]]))


def test_a_nondeterministic_baseline_generator_is_caught_before_any_provider_runs(tmp_path):
    """§5 step 3's whole purpose: measured evidence, not an assumption, and measured
    before a token is spent."""
    repo = make_repo(tmp_path)
    write(repo, "gen.txt", "seed\n")
    write(repo, "check.sh", "#!/bin/sh\ndate +%s%N > gen.txt\n")
    (Path(repo) / "check.sh").chmod(0o755)
    commit_all(repo, "gate")
    run = tmp_path / "run"; run.mkdir()
    b = baseline.materialize(repo, run, finspect.repo_facts(repo), [], "r1")
    c = finspect.GeneratorContract(id="g1", relations=(("src/*", "gen.txt"),))
    with pytest.raises(verify.GeneratorUnstable):
        verify.calibrate(repo, b, tmp_path / "cal", identity=IDENT, contract=c,
                         setup=verify.Command.parse([["true"]]),
                         command=verify.Command.parse([["./check.sh"]]))
```

`_run` is the existing helper in that suite; reuse it rather than restating it.

- [ ] **Step 2: Run to verify they fail**

Run: `uvx pytest tests/test_forge_verify.py -q`
Expected: `AttributeError: module 'forge.verify' has no attribute 'calibrate'`.

- [ ] **Step 3: Implement**

```python
@dataclass(frozen=True)
class Calibration:
    """What the untouched baseline does under the confirmed commands (spec §5 step 3).

    `run` is the only value in this package a caller may pass as `classify`'s
    `baseline_run`. Nothing enforces that — `classify` takes a `Run`, and a `Run` carries no
    provenance — so it is stated here and asserted next door: a calibration taken anywhere a
    builder could reach turns `BASELINE_RED_NO_NEW_IDENTIFIED_FAILURE` into a verdict the
    builder chose, which is the one thing §6 exists to prevent.

    `converged` is §5 step 3's second job: the second verify pass showed no tracked delta, so
    "the generator reaches a fixed point" is measured rather than assumed — and measured
    before any provider has spent a token. It is always True when this object exists;
    non-convergence raises `GeneratorUnstable` instead, and the field is here so a caller
    writing a manifest records the fact rather than the absence of an exception.
    """
    run: Run
    path: Path
    admitted: tuple[str, ...] = ()
    converged: bool = True


def calibrate(repo, baseline_, dest, *, identity, contract, setup, command,
              env=None) -> Calibration:
    """Run setup and verify on the untouched baseline, in a sacrificial verifier.

    The candidate is EMPTY — a bundle with no patch and no sidecars — so `build_verifier`
    produces exactly the baseline through the same path every other tree in the run takes.
    Building it any other way would make the calibration's clone the one tree in the run
    whose construction nobody had reviewed.
    """
    empty = bundle.CandidateBundle(
        version=bundle.VERSION, baseline_ref=baseline_.ref,
        baseline_commit=baseline_.commit, generator_contract_id=contract.id)
    v = build_verifier(repo, baseline_, empty, dest, identity=identity, contract=contract,
                       command=command)
    validate_materialized(v)
    run_setup(v, setup, contract, env=env)
    fp = fixed_point(v.path, command, contract, env=env)
    return Calibration(run=fp.run, path=v.path, admitted=fp.admitted, converged=True)
```

Note the parameter is `baseline_`, not `baseline` — `verify.py` imports the `baseline` module, and Plan D's branch review flagged the same shadow in `classify` as a live footgun for exactly this kind of later edit. Do not repeat it.

- [ ] **Step 4: Run to verify they pass**

Run: `uvx pytest tests/test_forge_verify.py -q`
Expected: all pass.

- [ ] **Step 5: Mutate**

One site at a time: `validate_materialized` dropped; `run_setup` dropped; `fixed_point` → `run_command` (loses convergence); `converged=True` → `False`; the empty bundle given a sidecar. Each must be caught by exactly one test, and report which.

- [ ] **Step 6: Render, gate, commit**

```bash
make render
make verify
make precommit
git add shared/lib/forge/verify.py tests/test_forge_verify.py marketplaces
git commit -m "feat(forge): calibrate the baseline where the builder never was"
```

---

### Task 5: Convert the reachability pins and close the seams

**Files:**
- Modify: `tests/test_forge_seams.py`, `tests/test_forge_verify.py`
- Test: itself

**Interfaces:** consumes Tasks 1–4. Produces no new engine symbol.

**Why this is a task and not cleanup.** Plan D pinned each unreachable branch with a test asserting it *stays* unreachable, and made those tests fail loudly on the day a producer landed — by equality on the outcome **and** on the reason substring, specifically so a `!= None` form could not stay green through exactly this event. That day is now. Those tests are the designed signal, and converting them is the point; **deleting one is the failure mode this task exists to prevent.**

The ledger names three properties that were true only by accident and must now be true by construction:

1. `generator_contract_id` linked nothing — closed by Task 1, and this task asserts the link across the `bundle`/`verify` seam.
2. `baseline_run` had no producer — closed by Task 4, and this task asserts the produced run's tree has no origin and is not a seat.
3. `gate_delta` had no producer, so `PASS` was dead — closed by Task 2, and this task asserts a full chain reaches `PASS`.

- [ ] **Step 1: Find every pin that this plan makes false**

Run: `uvx pytest tests/ -m "not slow" -q`

Expected after Tasks 1–4: **failures**, not passes. At minimum
`test_a_repo_preflight_admits_completes_the_chain` (`tests/test_forge_seams.py`), which asserts `GATE_CHANGED` by equality plus both reason substrings, and `test_build_leaves_the_gate_delta_unknown_rather_than_empty` (`tests/test_forge_bundle.py`), whose subject is now only true of `build` and not of the chain.

List every failure and, for each, decide **convert or keep** — a pin whose subject is still true (`build` alone still leaves `gate_delta` unknown) is kept and its docstring narrowed; a pin whose subject this plan falsified is converted to assert the new behaviour under a new name. Report the list before changing anything.

- [ ] **Step 2: Convert the chain pin**

```python
# tests/test_forge_seams.py — replaces test_a_repo_preflight_admits_completes_the_chain
def test_a_repo_preflight_admits_reaches_a_clean_pass(tmp_path):
    """The other half of the refusal seam, and the property Plan E exists to make true.

    Before the gate-delta producer landed this test asserted `GATE_CHANGED` with the reason
    "nobody measured the gate surface" — an admission that the chain could not reach a
    verdict, pinned so it would fail on the day it stopped being true. It is that day.

    Every value here comes from the chain: the delta from `build_verifier`, which is the
    only caller holding both the baseline tree and the candidate tree, and the baseline run
    from `calibrate`, which is the only producer of one.
    """
    repo = make_repo(tmp_path)
    write(repo, "check.sh", "#!/bin/sh\nexit 0\n")
    (Path(repo) / "check.sh").chmod(0o755)
    commit_all(repo, "gate")
    f = finspect.repo_facts(repo)
    assert finspect.rejections(f, []) == []
    run = tmp_path / "run"; run.mkdir()
    b = baseline.materialize(repo, run, f, [], "r1")
    c = finspect.detect_generators(repo)
    setup = verify.Command.parse([["true"]])
    gate = verify.Command.parse([["./check.sh"]])
    cal = verify.calibrate(repo, b, tmp_path / "cal", identity=IDENT, contract=c,
                           setup=setup, command=gate)

    s = fleet.clone_seat(repo, b, tmp_path / "seat", name="claude", identity=IDENT)
    f0 = harvest.record(s.path)
    write(s.path, "src.py", "work\n")
    fw = harvest.record(s.path)
    a = harvest.artifact_set(
        harvest.Phases(f0=f0, fsetup=f0, fwork=fw, fverify=fw), s.path, b.commit)
    cb = bundle.build(s.path, a, b, contract=c)
    v = verify.build_verifier(repo, b, cb, tmp_path / "verifier", identity=IDENT,
                              contract=c, command=gate)
    verify.validate_materialized(v)
    verify.run_setup(v, setup, c)
    fp = verify.fixed_point(v.path, gate, c)
    outcome, reason = verify.classify(fp, cal.run, v.candidate)
    assert outcome == verify.PASS, reason
    assert v.candidate.gate_delta == (), "measured and clean, not unmeasured"
```

- [ ] **Step 3: Write the three seam assertions**

```python
# tests/test_forge_seams.py  (append)
def test_the_contract_the_gate_admits_under_is_the_one_the_manifest_records(tmp_path):
    """SEAM: `bundle.build` writes the id, `verify.build_verifier` refuses a disagreement.

    Before this was enforced the property held only because both sides were "": a manifest
    recording contract X while the gate admitted under Y was undetectable, and the test that
    covered it pinned only that the two DEFAULTS agree.
    """
    repo = make_repo(tmp_path)
    run = tmp_path / "run"; run.mkdir()
    b = baseline.materialize(repo, run, finspect.repo_facts(repo), [], "r1")
    s = fleet.clone_seat(repo, b, tmp_path / "seat", name="claude", identity=IDENT)
    f0 = harvest.record(s.path)
    write(s.path, "src.py", "work\n")
    fw = harvest.record(s.path)
    a = harvest.artifact_set(
        harvest.Phases(f0=f0, fsetup=f0, fwork=fw, fverify=fw), s.path, b.commit)
    x = finspect.GeneratorContract(id="x", relations=(("s/*", "o/*"),))
    y = finspect.GeneratorContract(id="y", relations=(("s/*", "o/*"),))
    cb = bundle.build(s.path, a, b, contract=x)
    with pytest.raises(verify.ContractMismatch):
        verify.build_verifier(repo, b, cb, tmp_path / "v1", identity=IDENT, contract=y)
    assert verify.build_verifier(repo, b, cb, tmp_path / "v2",
                                 identity=IDENT, contract=x).path.is_dir()


def test_the_calibration_tree_is_not_a_seat(tmp_path):
    """SEAM: `calibrate` and `fleet`. §6.2's baseline-red outcome is a claim about a tree
    the builder never had; a `Run` carries no provenance, so the tree is what must be
    checked, and this is where it is."""
    repo = make_repo(tmp_path)
    write(repo, "check.sh", "#!/bin/sh\nexit 0\n")
    (Path(repo) / "check.sh").chmod(0o755)
    commit_all(repo, "gate")
    run = tmp_path / "run"; run.mkdir()
    b = baseline.materialize(repo, run, finspect.repo_facts(repo), [], "r1")
    cal = verify.calibrate(repo, b, tmp_path / "cal", identity=IDENT,
                           contract=finspect.GeneratorContract(),
                           setup=verify.Command.parse([["true"]]),
                           command=verify.Command.parse([["./check.sh"]]))
    branch = gitcmd.git(cal.path, "rev-parse", "--abbrev-ref", "HEAD",
                        env_extra=gitcmd.READONLY).stdout.strip()
    assert branch.endswith("/verify"), branch
    assert gitcmd.git(cal.path, "remote", env_extra=gitcmd.READONLY).stdout.strip() == ""
    assert cal.path != tmp_path / "seat"


def test_setup_replayed_in_the_verifier_sees_the_candidates_own_setup_state(tmp_path):
    """SEAM: `harvest` excludes setup-phase state, `verify.run_setup` regenerates it.

    The exclusion was an argument no code made until setup was replayed here. A seat's
    setup-phase `.venv` never crosses; the verifier builds its own.
    """
    repo, b, cb = _repo_baseline_bundle_seam(tmp_path)
    v = verify.build_verifier(repo, b, cb, tmp_path / "verifier", identity=IDENT,
                              contract=finspect.GeneratorContract())
    assert not (v.path / ".venv").exists(), "the seat's setup state did not cross"
    verify.run_setup(v, verify.Command.parse(
        [[sys.executable, "-c", "import os; os.makedirs('.venv')"]]),
        finspect.GeneratorContract())
    assert (v.path / ".venv").is_dir(), "and the verifier built its own"
```

Write `_repo_baseline_bundle_seam(tmp_path)` in `tests/test_forge_seams.py`: a repo, baseline and seat where the seat creates `.venv/rigged` **before** `Fsetup` is recorded and edits `src.py` after, so the rig is setup-phase state and the edit is work. Plan D measured that placing the rig in the *work* phase makes it cross as an ordinary sidecar — that is correct behaviour, not the property under test here.

- [ ] **Step 4: Run everything**

Run: `uvx pytest tests/ -m "not slow" -q`
Expected: all pass, no skips, no warnings.

- [ ] **Step 5: Verify the gates still hold**

Run: `uvx pytest tests/test_forge_packaging.py -q`
Expected: pass — in particular `test_every_test_named_in_shipped_forge_prose_still_exists`, which fails if any docstring in `shared/lib/forge/**` still names a test this task renamed.

Then sweep `shared/lib/forge/**` for prose this plan falsified. At minimum: `bundle.py`'s `gate_delta` comment says what produces the delta and that `build` is not that caller — the second half stays true, the first must now name `verify.build_verifier`; `CandidateBundle.generator_contract_id`'s comment; `classify`'s `baseline_run` parameter, which says what a calibration *means* and never said nothing produced one. Report each site and its verdict.

- [ ] **Step 6: Run the full gates**

```bash
make render
make verify
make precommit
```

Record every exit code. If receipts are stale, verify the cause first, then reseed **scoped** (`--seed-receipt --skill <name>`); never unscoped, which destroys any real `provenance: "eval"` receipt.

- [ ] **Step 7: Commit**

```bash
git add tests/test_forge_seams.py tests/test_forge_verify.py tests/test_forge_bundle.py shared/lib/forge marketplaces
git commit -m "test(forge): the verdict is producible, and the pins that said it was not"
```

---

## Self-review

**Spec coverage.** §6 step 2 (hash-validate before setup) → Task 3 `validate_materialized`. §6 step 3 (run setup in the verifier) → Task 3 `run_setup`. §6's "setup must leave tracked files clean … fails the candidate closed as `setup_overlap`" → Task 3 `SetupOverlap`. §6.1 (record changes to the whole gate surface; a legitimate test edit marks `gate_changed`) → Task 2, symmetric difference. §6.2's `BASELINE_RED_NO_NEW_IDENTIFIED_FAILURE` becomes producible → Task 4. §7.2's "the contract is a property of the run, recorded in the manifest, never seat-writable" → Task 1. §5 step 3 (calibrate in a sacrificial clone through the seat code path, setup + verify twice, second pass zero delta) → Task 4.

**Deliberately out of scope**, each with a later home: the journal, the state machine and crash recovery (§14) and storage GC (§15) — Plan F; the confirmation gate's *asking* and its cost quote (§5 steps 1–2, §5.2) — Plan F, since it needs the manifest §14 defines; strategy and fallback (§12); review and ultrareview (§13); handover (§16); the skill and its evals (§18/§20). Nothing here launches a provider — every setup and verify command is a shell script or a `sys.executable -c` in a fixture repo.

**What this plan does not close, stated rather than implied.** `detect_generators` still returns the empty contract, so on this repository the contract is `""` and admits nothing; Task 1 makes the id *binding* but does not make it *non-empty*. Deriving the real relation needs a declaration `scripts/render.py` does not currently carry — Plan D measured that reading it would mean the engine importing builder-controlled code — so that is a repo change, not an engine change, and it belongs with the §5 confirmation gate that would show the derived contract to a human. Until then `fixed_point` admits nothing and `PASS` requires a gate that rewrites no tracked file, which this repository's own `make verify` does not satisfy.

**Placeholder scan.** None. Task 5 asks the implementer to enumerate failing pins rather than listing them, which is deliberate: the list depends on what Tasks 1–4 actually changed, and a stale list in a plan is worse than an instruction to measure. The two pins most likely to fire are named.

**Type consistency.** `Verifier.path` is a `Path` and `Verifier.candidate` a `CandidateBundle`; every Task 3–5 signature takes the `Verifier`, not the path, except `fixed_point`, which is Plan D's and keeps taking a path. `Calibration.run` is a `Run`, which is what `classify`'s `baseline_run` accepts. `bundle.with_gate_delta` and `verify.build_verifier` both return new frozen instances; nothing mutates a `CandidateBundle`. `contract` is an `inspect.GeneratorContract` at every call site.

**One risk worth naming.** Task 2's symmetric difference detects gate files entering and leaving the surface, and the plan's own draft test expects a *content* edit to `Makefile` to appear in the delta. Those are different measurements, and the step says so and tells the implementer to measure before believing it. If content edits must be caught — and §6.1's "a legitimate test edit is allowed but marks the candidate `gate_changed`" says they must — the surface has to be `(path, hash)` pairs rather than paths, and `CandidateBundle.gate_delta`'s declared type stays `tuple[str, ...]` by reporting the paths whose hash moved. Do not let this become a silent narrowing: a delta that misses a weakened Makefile is the fail-open direction, and §6 opens by naming exactly that attack.
