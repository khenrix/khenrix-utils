# llm-forge Plan I: the evidence record

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the record the decision engine will read — a materialized-and-re-verified task bundle in every seat, a strict claim ledger with cycle-checked dependencies, coverage predicates that only claim what a predicate can prove, and a per-seat prompt fingerprint produced by a *real* provider adapter and carried into a schema'd seat record.

**Architecture:** Plan H ends with three verified candidates on disk and nothing choosing between them. Choosing needs evidence that does not exist yet: what each seat was actually given (§20), what is claimed about the result (§10), which of those claims a machine can check (§10.1), and how comparable the three seats were (§11). This plan builds exactly that record and stops. **It decides nothing.** Every module here is a value with a strict decoder and a content hash; nothing in it ranks, falls back, reviews or ships.

**Tech Stack:** Python 3.11+ stdlib only. `git` 2.53 via `shared/lib/forge/gitcmd.py`. The council engine at `shared/lib/council/engine.py` for provider invocation. pytest via `uvx --with pytest pytest -q`.

---

## THIS PLAN IS A SPLIT. READ THIS BEFORE ANYTHING ELSE.

The brief asked for §20 → §10 → §10.1 → §11 → §12 → §13 → §13.1 in one plan. **It does not fit.** Sized against the eight prior plans (4–6 tasks each, one independently testable deliverable per task), the seven sections are ten to eleven tasks. A plan that pretends otherwise produces tasks with three deliverables each, which is where this project's recurring "a fix narrower than the finding" defect comes from.

**The seam is between what is *measured* and what is *decided*.**

- **Plan I (this document) — the evidence record.** §20 task bundle · §10 claim ledger · §10.1 coverage predicates · §11 per-seat `PromptIdentity`, plus the two debts Plan H handed forward that live on the same seam (the seat record's missing schema, and the third instance of "a measurement that was taken must reach the record" inside `runner.verify_candidate`). Six tasks. Every output is a frozen value with a strict decoder; nothing consumes a verdict.
- **Plan I₂ — the decision engine proper.** To be authored as `docs/superpowers/plans/<date>-llm-forge-i2-the-decision-engine.md` after Plan I lands. Six tasks:
  1. §12.1 size gate over the candidate set + §12.3's three-way failure classification (infrastructure / synthesis-introduced / requirement-gap) — a different axis from `verify.classify`'s outcome.
  2. §12.3's progress tuple `(new_failure_count, failing_test_fingerprints)`, its three-outcome comparison, oscillation detection over `(tree_oid, fingerprints)` sightings read back from `events.jsonl`, and the separately-recorded synthesis-fix cap.
  3. §12.5's strongest-seat rubric over recorded dimensions, total by construction, with the seat-name tie-break; §12.4's coverage-as-fallback-trigger reading Plan I's `coverage.Report`.
  4. §13's reviewer input set + in-process `run_council` + the content-addressed `review_findings` record.
  5. §13's bounded round-1/round-2 loop, the `ready`/`review_blocked` transition reading the *record*, re-verify-and-re-checkpoint after every fix.
  6. §13.1's ultrareview, its five unavailability reasons, the minutes-not-seconds timeout, and `--no-ultra`.

**Why this is a clean seam and not an arbitrary cut.** Every Plan I₂ task consumes Plan I types and produces none that Plan I needs: the rubric reads `coverage.Report` and `seatrecord.SeatRecord`; the review loop reads `taskbundle.TaskBundle` and is *defined* by never reading `ledger.Ledger`'s path. Nothing flows backwards. Plan I is independently useful even if I₂ is never written — it closes two of Plan H's carried debts and makes `--collect` able to read a seat record at all.

---

## Global Constraints

Every task's requirements implicitly include this section.

- **Python stdlib only.** No pip dependencies. `tomllib`, `json`, `hashlib`, `ast`, `subprocess`, `shutil`, `os`, `re`.
- **Commands run as argv lists, never through a shell.** No `shell=True`, no string-joined commands.
- **Git only via `shared/lib/forge/gitcmd.py`.** Git is located **by asking git** — `gitcmd.git(seat, "rev-parse", "--absolute-git-dir")` — **never by string-joining `.git`** onto a path.
- **Fail closed.** A measurement that could not be taken is `None`/UNKNOWN, never an empty success. `None` never compares equal to `None` for "these are the same".
- **A verdict must never read cleaner than its evidence.**
- **A comment asserting something the code does not do is a defect.**
- **No test may invoke a real provider or spend money.** Providers are injected; tests pass fakes. `launch.py` (Task 6) is the real adapter and **its suite never runs it** — the injection point is a parameter with a default, and every test overrides the default.
- `shared/lib/forge/**` is source of truth; `marketplaces/**` is render output. **Never hand-edit `marketplaces/`** — run `make render`.
- **Every task ends with:** `make render`, then an explicit-pathspec `git add` **including `marketplaces`**, then `make verify` and `make precommit` **run unpiped with `$?` captured**, then the commit. A pipe reports the pipe's exit status.
- **Use `scripts/mutate.py`** for mutation testing. It refuses pytest's exit 5 and requires a green unmutated baseline. **Check `git status` before and after any mutation wave** — a killed run leaves the tree mutated and the next suite is green for the wrong reason.
- **Tests run via `uvx --with pytest pytest -q`** (no system pytest is assumed).
- **New test files are added to `FORGE_TESTS` in the `Makefile` in the task that creates them.**

---

## Decisions already taken. Build on these; do not reopen them.

1. **§20's task bundle must NOT use `fleet.clone_seat`'s `template_dir`.** Measured on git 2.53.0: git's clone-template copy **silently drops every dot-name at every level** (`.claude/` vanished, rc 0, no output), **rewrites 0600 → 0644**, and passing a directory flips `engine_owned = not template_dir` to False (`fleet.py:177`), which skips the pre-clean and installs the directory as a real git template — disarming Plan G's empty-template hook defence. Copy post-clone into the seat's own git dir and **re-verify from the seat's filesystem**.
2. **§13's codex reviewer uses `codex exec --json`, not `codex review`** — a deliberate, recorded deviation from §13's text. `codex review` has **no `--json`** (measured), so the engine's existing `extract_codex_json` turns every review into a silent `parse_failure` and "found nothing" becomes indistinguishable from "could not be read". Forge supplies the review framing itself. **This lands in Plan I₂ Task 4, but it is recorded here because the deviation must not be re-litigated by whoever writes it.**
3. **The fingerprint type is `PromptIdentity`, never `identity`.** `fleet.clone_seat(..., identity=…)` and `runner.run_seat(..., identity=…)` already mean the git author `(name, email)` pair. Conflating them is a trap with a six-week fuse.
4. **§13.1's `claude ultrareview --timeout` is in MINUTES** (measured; default 30). Every other timeout in forge and the council is seconds. Plan I₂ Task 6 owns this; it is recorded here so it is not lost.

---

## Debts this plan closes, inherited from Plan H

- **`forge_spec` has no production caller.** §8.1's validator has never run outside the suite. **Task 6 wires the real `launch` adapter and must wire `forge_spec` with it** — otherwise seats run under the council's own validity policy, which is the exact defect that module exists to close.
- **The seat record has no schema.** `runstate.write_seat` enforces none, deliberately (`runstate.py:1062-1076`): §14.2 assigns the record's fields to the *orchestrator*, so `runstate` refuses to be the authority on a record it does not own. **Plan I is the first real reader, so Task 6 adds the schema beside the reader** — in a new module, leaving `write_seat` unchanged.
- **The third "a measurement that was taken must reach the record"**, inside `runner.verify_candidate` (documented at `runner.py:862` and `runner.py:1365`): `build_verifier` fills §6.1's `gate_delta`/`gate_surface` and `run_setup` returns a `SetupResult`, then any later refusal drops both as locals. Measured through `runner.run` with the hooks-rig candidate: the record comes back `gate_delta: null, gate_surface: null, verifier_setup: null` for a verifier clone that was built, measured and paid for. **Task 6 closes it.**

  > **The brief says closing it "changes what a public function raises". It does not have to.** `verify_candidate`'s docstring commits to `VerifyError` propagating unwrapped and a test pins that. Task 6 closes the loss with a **measurement sink** — a keyword-only callback invoked the instant each measurement exists, before any step that can refuse — which is `journal`'s own write-ahead discipline applied to a value. Nothing about the raised set changes, the pinning test stays green, and `_measured`'s candidate-identity check still governs what is admitted.

---

## Corrections this plan makes to the spec

Recorded here because §10–§13 and §20 contradict themselves or the code in ways an implementer would otherwise resolve twice, differently.

| # | Contradiction | Resolution in this plan |
|---|---|---|
| 1 | §10: `blocks` and `requires` are one edge with two names. A cycle checker walking `requires` alone passes a cyclic graph written with `blocks`. | Normalize both to one directed edge set at decode time (Task 3). |
| 2 | §10: `[{id, requires\|conflicts\|blocks}]` does not say whether the relation is a key or a value. | Value form: `{"id": str, "relation": "requires"\|"conflicts"\|"blocks"}`; any other key or relation string is refused (Task 3). |
| 3 | §10.1's three values are a **method** axis; §12.4 uses coverage as a **result** axis. "Checked mechanically, and the answer is no" has nowhere to go. | Every coverage result is a pair `(method, satisfied)` (Task 4). |
| 4 | §10's degradation rule has no N: "above N KB union diff, drop to per-file summaries". | `ledger.DEGRADE_UNION_DIFF_BYTES = 512 * 1024`, and the threshold *and* the measured union-diff size are recorded **in the ledger**, not only in the report (Task 3). |
| 5 | §11 says a text-only `prompt_sha256` is insufficient; `run_council`'s manifest records exactly that, once per run. | `PromptIdentity` carries four values and is built by the launcher, which is the only party that knows the prompt (Tasks 5, 6). |
| 6 | §20's "materialize it identically in every clone" vs `template_dir` as the insertion point. | See Decision 1 (Task 2). |
| 7 | §20 says "bar ambient invocation of the same skill". The only mechanism in reach is a sentence in the prompt. | Recorded as *an instruction issued*, never as a mechanical bar (Task 2). |
| 8 | `scripts/lib/inventory.py:48` routes agy through `agy changelog` because `--version` was believed absent. | Measured this machine: `agy --version` → `1.1.9`, rc 0. **`inventory.version` is not reused** — it swallows every failure into a *string*, and two unreadable versions then compare equal (Task 5). |

---

## What Plan H hands you, verbatim

Verify each against the code before relying on it. **The plan's own draft code has been wrong in every task of every plan so far.** Treat everything below as a hypothesis and decline with a measurement.

- `storage.run_root`, `new_run_id`, `atomic_write(path, data: bytes)`, `exclusive_write`, `append_line`, `manifest_path`/`journal_path`/`seat_state_path`/`state_path`/`seat_names`, `Quota(max_files, max_file_bytes, max_total_bytes)` with `.default()`, `.for_harvest()`, `.breach(*, files, file_bytes, total_bytes)`, `StorageError`.
- `runstate.Manifest` (17 frozen fields), `write_manifest`/`read_manifest`, `_DECODERS`/`_decode` (the strict-decoder precedent), `write_seat(run_dir, name, payload: dict)` / `read_seat(run_dir, name) -> dict | None`, `State`, `advance`, `PHASES`, `TERMINAL`, `ManifestError`, `StateError`.
- `snapshot.Entry(path, digest, mode, size, kind)` — `kind` ∈ `{"file","symlink","special"}`; `_digest(p)`; `_symlink_entry(p, rel)` digests the **target text** with `surrogateescape`; mode and size are a fabricated 0 for a link; a special file is never opened.
- `bundle._names_dotgit(rel)`, `bundle._assert_contained(rel, what)`, `bundle._safe_rel(rel, what)`, `bundle.SidecarEntry`, `bundle.CandidateBundle` (`gate_delta`/`gate_surface` are `None` = "nobody looked"), `bundle.BundleError`.
- `fleet.clone_seat(repo, baseline, dest, *, name, identity, template_dir=None) -> Seat(path, branch, verified, replayed)`, `fleet.forge_child_env(repo, env=None)`, `fleet.SeatError`.
- `verify.Run(exit_code, stdout, stderr, duration_sec, step_index)`, `verify.SetupResult`, `verify.build_verifier`, `verify.run_setup`, `verify.OUTCOMES`, `verify.VerifyError`.
- `seat.classify_seat(...) -> Status`, `seat.Status(process, artifacts, proven_read, forge, setup, verify)`, `seat.forge_spec(name, prompt, timeout, **kw) -> engine.ProviderSpec` (takes only `cfg=` and `workdir=`; anything else raises `SeatStatusError`), `seat.read_proof(output, token)`, `seat.SeatStatusError`.
- `runner.run_seat(manifest, run_dir, baseline, *, name, attempt, identity, launch) -> SeatResult`, `runner.verify_candidate(manifest, run_dir, baseline, candidate, *, name, identity, calibration) -> (outcome, reason, Verifier, SetupResult|None)`, `runner.reclassify_seat`, `runner._record(result) -> dict`, `runner._measured(result, candidate, verifier_setup)`, `runner.run(run_dir, repo, *, identity, launch) -> tuple`, `runner.RunnerError`. **The injected launch contract is `launch(*, name, seat_path, token, env) -> Mapping`.**
- `journal.Journal(path).record(event, *, operation_id, **data)`, `journal.intent(kind)`/`done(kind)`, `journal.orphans(events)`.
- `gitcmd.git(repo, *args, env_extra=None, check=True, binary=False, timeout=60, user_config=False)`, `READONLY`, `NO_DAEMON_CACHE`, `NO_HOOKS`, `NO_DIFF_DRIVERS`, `GitError`. **`rev-parse` is on all three safe verb sets in `tests/test_forge_seams.py` (`_INDEX_SAFE`, `_HOOK_SAFE`, `_DIFF_DRIVER_SAFE`), measured including `--absolute-git-dir`.**
- `council.engine.ProviderSpec(name, argv, stdin, extract, model, thinking, log_file, cwd, sentinel, min_chars, validator)`, `run_provider(spec, retries, timeout, backoff, workdir) -> dict`, `run_council(specs, *, retries, timeout, backoff, workdir, prompt=None, requested=None, mode=None, read_only=None, install_signal_handler=True) -> dict`, `make_sentinel()`, `apply_sentinel(prompt, token)`, `SENTINEL_NOTE`, `build_real_spec(name, prompt, timeout, cfg, workdir)`, `MODE_TIMEOUT = {"normal": 300, "deep": 1200}`.
- `scripts/lib/checks.py`: `source_manifest(root, skill)`, `source_hash(root, skill)` — the repo's one spelling of "hash a closure": `sha256(json.dumps(sorted_pairs, sort_keys=True).encode())`.
- `scripts/refresh.py`: `INSTALL_GLOBS` (the three live plugin install paths), `installed_dirs(cli) -> list[Path]` (returns `[]` when a CLI is not installed), `sync` (an **additive** `copytree(dirs_exist_ok=True)` that never removes a stale file — which is why the installed copy can be a strict superset of the repo source and `checks.source_hash` cannot stand in for it).

---

## File Structure

- **Create `shared/lib/forge/taskbundle.py`** — §20. One responsibility: *what the seat was given*. The manifest, its hash, its materialization into a seat, its re-verification from the seat, and the three installed plugin closures. Tasks 1–2.
- **Create `shared/lib/forge/ledger.py`** — §10. One responsibility: *what is claimed*. The row type, the content-derived id, the strict decoder, the dependency graph and its refusals, the degradation record, the ledger hash. Task 3.
- **Create `shared/lib/forge/coverage.py`** — §10.1. One responsibility: *which claims a machine can check, and what it found*. Four predicates, the `(method, satisfied)` pair, the two mechanical contradiction assertions. Task 4.
- **Create `shared/lib/forge/fingerprint.py`** — §11. One responsibility: *how comparable two seats were*. `PromptIdentity`, its four values, and the three-state agreement label. Task 5.
- **Create `shared/lib/forge/launch.py`** — the real provider adapter that satisfies `runner`'s injected `launch` contract, wires `seat.forge_spec`, and returns the `PromptIdentity` with the provider record. Task 6.
- **Create `shared/lib/forge/seatrecord.py`** — the seat record's schema, beside its first reader. Task 6.
- **Modify `shared/lib/forge/storage.py`** — `task_bundle_path`, `ledger_path`, `Quota.for_task_bundle()`. Tasks 1, 3.
- **Modify `shared/lib/forge/runner.py`** — factor the engine-text strip (Task 5); carry `prompt_identity` in `_record`, add the measurement sink to `verify_candidate`, bind it in `_verify_a_seat` (Task 6).
- **Create `tests/test_forge_taskbundle.py`, `tests/test_forge_ledger.py`, `tests/test_forge_coverage.py`, `tests/test_forge_fingerprint.py`, `tests/test_forge_launch.py`, `tests/test_forge_seatrecord.py`** — each added to `FORGE_TESTS` in the task that creates it.
- **Modify `tests/test_forge_seams.py`** — the two cross-module seams this plan creates (Tasks 2, 5).

---

### Task 1: The task-bundle manifest — paths, modes, kinds, and one hash

**Files:**
- Create: `shared/lib/forge/taskbundle.py`
- Create: `tests/test_forge_taskbundle.py`
- Modify: `shared/lib/forge/storage.py` (add `task_bundle_path`, `Quota.for_task_bundle`)
- Modify: `Makefile` (add `tests/test_forge_taskbundle.py` to `FORGE_TESTS`)

**Interfaces:**

- **Consumes:** `storage.Quota`, `storage.atomic_write(path, data: bytes)`, `storage.StorageError`; `bundle._assert_contained(rel, what)`, `bundle._names_dotgit(rel)`, `bundle.BundleError`; `snapshot._digest(p)`.
- **Produces:**
  ```python
  taskbundle.VERSION: int = 1
  taskbundle.TaskBundleError(RuntimeError)
  taskbundle.BundleEntry(path: str, kind: str, mode: int, sha256: str, size: int)   # frozen
  taskbundle.TaskBundle(version: int, entrypoint: str, entries: tuple[BundleEntry, ...],
                        max_files: int, max_file_bytes: int, max_total_bytes: int)  # frozen
  taskbundle.scan(root, *, entrypoint: str, quota: storage.Quota | None = None) -> TaskBundle
  taskbundle.bundle_hash(b: TaskBundle) -> str
  taskbundle.write_task_bundle(run_dir, b: TaskBundle) -> None
  taskbundle.read_task_bundle(run_dir) -> TaskBundle
  storage.task_bundle_path(run_dir) -> Path
  storage.Quota.for_task_bundle() -> Quota
  ```

**Why the hash covers `(path, kind, mode, sha256)` and not just `(path, sha256)`.** `checks.source_manifest` hashes relpath/content pairs, and that is the precedent to follow for *spelling*. But §20's own stated failure is "copying without modes hands it a script it cannot execute" — a hash omitting `mode` cannot distinguish a script from a non-executable copy of the same bytes, so the very hash meant to prove identical materialization is blind to the failure §20 names. `kind` is in for the same reason one level over: a symlink and a regular file holding the target text as its content have the same `sha256` under `snapshot`'s rules and are not the same bundle.

**Why `entrypoint` and `version` are in the hash too.** §11.5.3 compares two seats' bundle hashes to decide whether they were identically prompted. Two seats handed the same files but told to start at different entrypoints are *differently prompted*, so a hash blind to the entrypoint would report agreement it did not measure. This is a deliberate extension of the design pass's `(path, kind, mode, sha256)`, in the strengthening direction only.

**Caps, measured rather than guessed.** `Quota.default()` (5000 / 32 MB / 512 MB) is sized for the user's whole selected baseline and `for_harvest()` (200 000 / 512 MB / 8 GB) for a seat with a dependency tree. A task bundle is an instruction closure. Measured on this machine: the entire installed claude plugin closure is **76 files / 1 257 929 bytes, largest file 134 044 bytes**; the largest skill directory in `shared/skills/` is `skill-tuneup` at **8 files / 149 149 bytes / 91 382 max**. `for_task_bundle()` is set at 2000 / 4 MiB / 64 MiB — roughly 26× the measured closure by count and 50× by bytes, and still far under `default`. The point of the cap is to separate an instruction closure from a runaway, not to admit everything.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_forge_taskbundle.py`:

```python
"""§20: what the seat was given, as a manifest that can be re-derived from the seat."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

import os  # noqa: E402
import pytest  # noqa: E402
from forge import storage, taskbundle  # noqa: E402


def _tree(root: Path) -> None:
    """A bundle shaped like a real skill closure: a dot directory, an executable script,
    a 0600 file and a symlink — every shape git's clone template was measured to lose."""
    (root / ".claude" / "skills").mkdir(parents=True)
    (root / ".claude" / "skills" / "S.md").write_text("skill body\n")
    (root / "SKILL.md").write_text("entry\n")
    (root / "scripts").mkdir()
    tool = root / "scripts" / "tool.sh"
    tool.write_text("#!/bin/sh\necho hi\n")
    tool.chmod(0o755)
    secret = root / "config.ini"
    secret.write_text("k=v\n")
    secret.chmod(0o600)
    os.symlink("SKILL.md", root / "alias.md")


def test_the_manifest_carries_every_shape_the_clone_template_drops(tmp_path):
    _tree(tmp_path)
    b = taskbundle.scan(tmp_path, entrypoint="SKILL.md")
    by = {e.path: e for e in b.entries}
    assert set(by) == {".claude/skills/S.md", "SKILL.md", "scripts/tool.sh",
                       "config.ini", "alias.md"}
    assert by["scripts/tool.sh"].mode == 0o755
    assert by["config.ini"].mode == 0o600, \
        "0600 is the mode git's template copy was measured to rewrite to 0644"
    assert by["alias.md"].kind == "symlink"
    assert by["alias.md"].mode == 0 and by["alias.md"].size == 0, \
        "snapshot.Entry's rule: a link's mode and size are fabricated, not read"


def test_the_hash_separates_a_script_from_a_non_executable_copy(tmp_path):
    """§20's own stated failure — 'copying without modes hands it a script it cannot
    execute' — must not be invisible to the hash that proves identical materialization."""
    _tree(tmp_path)
    before = taskbundle.bundle_hash(taskbundle.scan(tmp_path, entrypoint="SKILL.md"))
    (tmp_path / "scripts" / "tool.sh").chmod(0o644)
    after = taskbundle.bundle_hash(taskbundle.scan(tmp_path, entrypoint="SKILL.md"))
    assert before != after


def test_the_hash_separates_two_entrypoints_over_identical_bytes(tmp_path):
    """§11 compares bundle hashes to decide 'identically prompted'. Two seats told to
    start in different places were not identically prompted."""
    _tree(tmp_path)
    a = taskbundle.bundle_hash(taskbundle.scan(tmp_path, entrypoint="SKILL.md"))
    b = taskbundle.bundle_hash(taskbundle.scan(tmp_path, entrypoint=".claude/skills/S.md"))
    assert a != b


def test_an_empty_bundle_is_refused_rather_than_hashed(tmp_path):
    """fleet.Seat.verified's argument, one module over: a check over an empty manifest is
    vacuous and still answers True."""
    with pytest.raises(taskbundle.TaskBundleError, match="no entries"):
        taskbundle.scan(tmp_path, entrypoint="SKILL.md")


def test_an_entrypoint_no_entry_names_is_refused(tmp_path):
    _tree(tmp_path)
    with pytest.raises(taskbundle.TaskBundleError, match="entrypoint"):
        taskbundle.scan(tmp_path, entrypoint="does/not/exist.md")


def test_a_git_directory_inside_a_bundle_is_refused(tmp_path):
    _tree(tmp_path)
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]\n")
    with pytest.raises(taskbundle.TaskBundleError, match="git"):
        taskbundle.scan(tmp_path, entrypoint="SKILL.md")


def test_an_escaping_symlink_is_refused_not_carried(tmp_path):
    """The escaping-link lesson, carried forward: a link whose target leaves the bundle
    describes content the bundle does not claim to hold."""
    root = tmp_path / "b"
    root.mkdir()
    (root / "SKILL.md").write_text("entry\n")
    os.symlink("../outside.txt", root / "esc.md")
    with pytest.raises(taskbundle.TaskBundleError, match="escapes"):
        taskbundle.scan(root, entrypoint="SKILL.md")


def test_a_special_file_is_refused_rather_than_given_a_payload(tmp_path):
    root = tmp_path / "b"
    root.mkdir()
    (root / "SKILL.md").write_text("entry\n")
    os.mkfifo(root / "pipe")
    with pytest.raises(taskbundle.TaskBundleError, match="special"):
        taskbundle.scan(root, entrypoint="SKILL.md")


def test_a_breached_cap_is_a_refusal_and_names_the_cap_it_applied(tmp_path):
    _tree(tmp_path)
    tiny = storage.Quota(max_files=2, max_file_bytes=1 << 20, max_total_bytes=1 << 20)
    with pytest.raises(taskbundle.TaskBundleError, match="files: 5 > 2"):
        taskbundle.scan(tmp_path, entrypoint="SKILL.md", quota=tiny)


def test_the_caps_actually_applied_are_recorded_not_assumed(tmp_path):
    _tree(tmp_path)
    b = taskbundle.scan(tmp_path, entrypoint="SKILL.md")
    q = storage.Quota.for_task_bundle()
    assert (b.max_files, b.max_file_bytes, b.max_total_bytes) == \
        (q.max_files, q.max_file_bytes, q.max_total_bytes), \
        "a bundle that fit and a bundle nobody measured must be different records"


def test_the_bundle_round_trips_through_the_run_directory(tmp_path):
    _tree(tmp_path / "src")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    b = taskbundle.scan(tmp_path / "src", entrypoint="SKILL.md")
    taskbundle.write_task_bundle(run_dir, b)
    assert taskbundle.read_task_bundle(run_dir) == b
    assert taskbundle.bundle_hash(taskbundle.read_task_bundle(run_dir)) == \
        taskbundle.bundle_hash(b)


def test_a_field_the_decoder_does_not_know_is_refused(tmp_path):
    _tree(tmp_path / "src")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    taskbundle.write_task_bundle(run_dir, taskbundle.scan(tmp_path / "src",
                                                          entrypoint="SKILL.md"))
    p = storage.task_bundle_path(run_dir)
    p.write_text(p.read_text().replace('"version": 1', '"version": 1, "extra": 2'))
    with pytest.raises(taskbundle.TaskBundleError, match="does not know"):
        taskbundle.read_task_bundle(run_dir)


def test_a_missing_bundle_raises_rather_than_reading_as_empty(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    with pytest.raises(taskbundle.TaskBundleError, match="does not exist"):
        taskbundle.read_task_bundle(run_dir)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uvx --with pytest pytest -q tests/test_forge_taskbundle.py`
Expected: FAIL — collection error, `ImportError: cannot import name 'taskbundle' from 'forge'`.

- [ ] **Step 3: Add the storage path and the cap**

In `shared/lib/forge/storage.py`, beside `state_path`:

```python
def task_bundle_path(run_dir) -> Path:
    return Path(run_dir) / "task-bundle.json"


def ledger_path(run_dir) -> Path:
    return Path(run_dir) / "ledger.json"
```

> `ledger_path` is added here rather than in Task 3 so this file's four run-directory names are declared in one edit and one place. Task 3 is its first caller.

And in `class Quota`, beside `for_harvest`:

```python
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
```

- [ ] **Step 4: Write `taskbundle.py`'s manifest half**

Create `shared/lib/forge/taskbundle.py`:

```python
"""§20: the task bundle — what a seat was actually given, as a record it can be checked against.

Resolving a task into a portable instruction is a CLOSURE, NOT A BODY. Inlining only the
Markdown hands a seat prose referencing a `scripts/tool.sh` it does not have; copying
without modes hands it a script it cannot execute. So the unit is a manifest — canonical
relative path, kind, mode, content hash, size — plus an entrypoint, plus the caps that were
actually applied, and the bytes are materialized from it and then RE-DERIVED FROM THE SEAT
and compared (see `verify_materialized`, Task 2).

WHY NOT `fleet.clone_seat(template_dir=...)`, WHICH IS THE OBVIOUS HOOK. Measured on git
2.53.0, three independent ways it cannot satisfy §20:

  1. EVERY DOT-NAME IS SILENTLY DROPPED, AT EVERY LEVEL. A template holding
     `bundle/.claude/skills/S.md` and `bundle/.envrc` produced a clone containing neither,
     rc 0, no output. A skill closure is precisely the shape that has dot directories, and
     this is the worst available failure: the manifest lists the file, the hash covers it,
     the seat does not have it, and nothing says so.
  2. MODES ARE NORMALIZED, NOT PRESERVED. A 0600 template file arrives 0644 (git applies
     0666/0777 by the executable bit, masked by umask). Only +x survives.
  3. IT DISARMS AN EXISTING DEFENCE. `clone_seat` computes `engine_owned = not
     template_dir` (`fleet.py:177`); passing a directory flips it False, skipping the
     pre-clean and installing the directory as a real git template — git READS a template
     `config` (a malformed one aborted a clone outright) and installs a template `hooks/`
     that then runs for the agent's own commits.

The bundle is therefore copied AFTER the clone, into the seat's own git directory, and the
manifest is recomputed from the seat's filesystem and compared. That recomputation is the
only thing that turns the three silent losses above into a refusal, and it is the rule
`clone_seat` already applies to itself: the trusted parent recomputes readiness from primary
evidence rather than trusting the operation that produced it.

WHAT THIS MODULE DOES NOT CLAIM. §20 says "bar ambient invocation of the same skill". The
only mechanism in reach is a sentence in the prompt, so `ambient_note()` returns that
sentence and the record calls it an INSTRUCTION ISSUED. It is not a mechanical bar and the
report must never say it is; if a per-CLI settings toggle exists, it has to be measured
before it is claimed.

EMPTY DIRECTORIES ARE NOT CARRIED, and that is the same ceiling `snapshot.Entry` declares:
directories are not inventoried, so a bundle that means to hand a seat an empty `output/`
cannot. Say so to the author rather than materializing something the manifest cannot check.
"""
import hashlib
import json
import os
import stat
from dataclasses import dataclass, fields
from pathlib import Path, PurePosixPath

from . import bundle as bundlemod
from . import gitcmd, snapshot, storage

VERSION = 1


class TaskBundleError(RuntimeError):
    """This bundle cannot be described honestly, or cannot be laid down where it was asked."""


@dataclass(frozen=True)
class BundleEntry:
    """One path the bundle carries.

    `kind` is "file" or "symlink" — the two shapes with an honest payload. A FIFO, socket
    or device node is REFUSED, never given a fabricated one: `snapshot` records a special
    file by type because it is inventorying a tree it did not choose, while a bundle is
    authored, and an author who put a FIFO in one has made a mistake worth hearing about.

    `mode` is the real `st_mode & 0o777` for a file and a FABRICATED 0 for a symlink,
    matching `snapshot.Entry` and `bundle.SidecarEntry`. `size` is 0 for a symlink for the
    same reason — it is not the target's length.

    `sha256` is the content for a file and the sha256 of the TARGET TEXT, encoded with
    surrogateescape, for a symlink: a link target is a filesystem name, not text, and a
    strict `.encode()` took `baseline.materialize` down on an ordinary link to `café.txt`.
    """
    path: str
    kind: str
    mode: int
    sha256: str
    size: int


@dataclass(frozen=True)
class TaskBundle:
    """The manifest §20 asks for, plus the caps that were applied to produce it.

    THE CAPS ARE RECORDED, NOT ASSUMED. A bundle that fit under `Quota.for_task_bundle`
    and a bundle nobody measured are different records, and only one of them can say what
    it was measured against. `storage.Quota` is not stored as a nested object because JSON
    has one sequence type and the round-trip check below is cheaper to keep honest over
    three plain ints.
    """
    version: int
    entrypoint: str
    entries: tuple
    max_files: int
    max_file_bytes: int
    max_total_bytes: int


def _rel(root: Path, p: Path) -> str:
    """The canonical POSIX relative path, refused if it could name anything but a bundle path."""
    rel = PurePosixPath(p.relative_to(root)).as_posix()
    # One spelling of the rule, imported rather than re-inlined: `bundle.py:191` re-inlined
    # `harvest._literal` and that divergence route is on this project's open-defect list.
    bundlemod._assert_contained(rel, "a task bundle path")
    if bundlemod._names_dotgit(rel):
        raise TaskBundleError(
            f"a task bundle may not carry git's own directory: {rel!r}. A `.git/config` "
            "laid into a clone takes its hooks pin and its identity, and both are §4.1's.")
    return rel


def _entry(root: Path, p: Path) -> BundleEntry:
    st = p.lstat()
    if stat.S_ISLNK(st.st_mode):
        target = os.readlink(p)
        # The escaping-link rule, at the only place that can still refuse: a link whose
        # normalized target leaves the bundle describes content the bundle does not hold,
        # and materializing it points a seat at a host path nobody authored.
        joined = os.path.normpath(os.path.join(os.path.dirname(str(p.relative_to(root))),
                                               target))
        if os.path.isabs(target) or joined.startswith(".."):
            raise TaskBundleError(
                f"a task bundle symlink escapes the bundle: {_rel(root, p)!r} -> {target!r}")
        digest = hashlib.sha256(target.encode("utf-8", "surrogateescape")).hexdigest()
        return BundleEntry(_rel(root, p), "symlink", 0, digest, 0)
    if not stat.S_ISREG(st.st_mode):
        raise TaskBundleError(
            f"a task bundle may not carry a special file (FIFO, socket, device): "
            f"{_rel(root, p)!r}. It was NOT opened — a read-open on a FIFO blocks.")
    return BundleEntry(_rel(root, p), "file", st.st_mode & 0o777,
                       snapshot._digest(p), st.st_size)


def _walk(root: Path) -> list:
    """Every file and symlink under `root`, sorted, never following a directory symlink."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        d = Path(dirpath)
        # A symlink TO a directory is an entry, not a directory to descend: os.walk lists
        # it in dirnames and followlinks=False stops the descent but not the listing.
        for name in list(dirnames):
            if (d / name).is_symlink():
                dirnames.remove(name)
                out.append(_entry(root, d / name))
        for name in filenames:
            out.append(_entry(root, d / name))
    return sorted(out, key=lambda e: e.path)


def scan(root, *, entrypoint: str, quota: storage.Quota | None = None) -> TaskBundle:
    """The manifest for the closure rooted at `root`, or a refusal.

    Refused rather than truncated on every count, because every one of them makes a bundle
    that CLAIMS more than it carries: no entries at all, an entrypoint no entry names, a
    `.git` component, an escaping link, a special file, a breached cap.
    """
    root = Path(root)
    quota = quota or storage.Quota.for_task_bundle()
    entries = _walk(root)
    if not entries:
        raise TaskBundleError(
            f"{root} has no entries. An empty bundle hashes to a stable value and makes "
            "every later comparison vacuous while still answering True.")
    breach = quota.breach(files=len(entries),
                          file_bytes=max(e.size for e in entries),
                          total_bytes=sum(e.size for e in entries))
    if breach:
        raise TaskBundleError(f"this task bundle exceeds §20's caps — {breach}")
    if entrypoint not in {e.path for e in entries}:
        raise TaskBundleError(
            f"the entrypoint {entrypoint!r} is not one of this bundle's entries. The seat "
            "is told to read it, so a bundle that does not carry it is prose pointing at "
            "a file the seat does not have.")
    return TaskBundle(VERSION, entrypoint, tuple(entries),
                      quota.max_files, quota.max_file_bytes, quota.max_total_bytes)


def _rows(entries) -> list:
    """The four hashed fields per entry, sorted — the ONE spelling of "hash a closure" here."""
    return sorted([e.path, e.kind, e.mode, e.sha256] for e in entries)


def bundle_hash(b: TaskBundle) -> str:
    """§11.5.3's task/resource bundle hash.

    `checks.source_hash`'s spelling — `sha256(json.dumps(..., sort_keys=True).encode())` —
    EXTENDED in two directions, both strengthening. `source_manifest` hashes
    `(relpath, content_sha)` pairs; a bundle hash blind to `mode` cannot distinguish a
    script from a non-executable copy of the same bytes, which is §20's own named failure,
    invisible to the hash meant to prove identical materialization. `kind` is in for the
    same reason one level over. And the ENTRYPOINT is hashed because §11 compares this
    value to decide "identically prompted": two seats handed the same files and told to
    start in different places were not identically prompted.

    The caps are NOT hashed. They describe what was measured, not what was handed over,
    and two runs with different caps over the same bytes gave their seats the same bundle.
    """
    payload = {"version": b.version, "entrypoint": b.entrypoint, "entries": _rows(b.entries)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _row(b: TaskBundle) -> dict:
    return {"version": b.version, "entrypoint": b.entrypoint,
            "max_files": b.max_files, "max_file_bytes": b.max_file_bytes,
            "max_total_bytes": b.max_total_bytes,
            "entries": [{"path": e.path, "kind": e.kind, "mode": e.mode,
                         "sha256": e.sha256, "size": e.size} for e in b.entries]}


_ENTRY_TYPES = {"path": str, "kind": str, "mode": int, "sha256": str, "size": int}


def _decode(row, source) -> TaskBundle:
    """The `runstate._decode` precedent: missing refused, unknown refused, type-checked.

    NOT `write_seat`'s silence. That module argues its own case — §14.2 assigns the seat
    record's fields to the orchestrator, so `runstate` refuses to be the authority on a
    record it does not own. None of that transfers: §20 enumerates this manifest's fields
    exactly, and §11 hashes it into a comparison that decides whether three seats' agreement
    is creditable. A field a writer stopped writing changes the hash with nothing saying why.
    """
    if not isinstance(row, dict):
        raise TaskBundleError(f"{source}: a task bundle is an object, not {type(row).__name__}")
    names = [f.name for f in fields(TaskBundle)]
    missing = [n for n in names if n not in row]
    if missing:
        raise TaskBundleError(f"{source} is missing {missing}")
    unknown = sorted(set(row) - set(names))
    if unknown:
        raise TaskBundleError(f"{source} carries fields this engine does not know: {unknown}")
    for n in ("version", "max_files", "max_file_bytes", "max_total_bytes"):
        if not isinstance(row[n], int) or isinstance(row[n], bool):
            raise TaskBundleError(f"{source}: {n} is an int, not {row[n]!r}")
    if not isinstance(row["entrypoint"], str):
        raise TaskBundleError(f"{source}: entrypoint is a string, not {row['entrypoint']!r}")
    if row["version"] != VERSION:
        raise TaskBundleError(
            f"{source} was written by task-bundle version {row['version']}, and this engine "
            f"writes {VERSION}. A manifest read under the wrong version is a hash nobody can "
            "reproduce.")
    if not isinstance(row["entries"], list) or not row["entries"]:
        raise TaskBundleError(
            f"{source}: entries is a non-empty list. An empty one reads as a bundle with "
            "nothing in it, which every later check passes vacuously.")
    entries = []
    for i, e in enumerate(row["entries"]):
        if not isinstance(e, dict):
            raise TaskBundleError(f"{source}: entry {i} is an object, not {type(e).__name__}")
        emissing = sorted(set(_ENTRY_TYPES) - set(e))
        eunknown = sorted(set(e) - set(_ENTRY_TYPES))
        if emissing or eunknown:
            raise TaskBundleError(
                f"{source}: entry {i} is missing {emissing} and carries unknown {eunknown}")
        for k, t in _ENTRY_TYPES.items():
            if not isinstance(e[k], t) or (t is int and isinstance(e[k], bool)):
                raise TaskBundleError(f"{source}: entry {i}: {k} is {t.__name__}, not {e[k]!r}")
        if e["kind"] not in ("file", "symlink"):
            raise TaskBundleError(
                f"{source}: entry {i}: kind is 'file' or 'symlink', not {e['kind']!r}")
        entries.append(BundleEntry(e["path"], e["kind"], e["mode"], e["sha256"], e["size"]))
    return TaskBundle(row["version"], row["entrypoint"], tuple(entries),
                      row["max_files"], row["max_file_bytes"], row["max_total_bytes"])


def write_task_bundle(run_dir, b: TaskBundle) -> None:
    """Persist the resolved instruction so `--collect` never depends on vanished context (§20).

    `atomic_write`, not `exclusive_write`: the manifest is the run's write-once identity and
    this is not it — a resume may re-record the same bundle, and a rename-published file
    leaves a mid-write reader the previous one whole.
    """
    if not isinstance(b, TaskBundle):
        raise TaskBundleError(f"a TaskBundle is required, not {type(b).__name__}")
    blob = json.dumps(_row(b), sort_keys=True, indent=2).encode("utf-8") + b"\n"
    path = storage.task_bundle_path(run_dir)
    restored = _decode(json.loads(blob), path)
    if restored != b:
        differing = [f.name for f in fields(TaskBundle)
                     if getattr(restored, f.name) != getattr(b, f.name)]
        raise TaskBundleError(
            f"this task bundle does not survive its own round trip; {differing} come back as "
            "a different type. JSON has one sequence type: pass `entries` as a tuple of "
            "`BundleEntry`.")
    storage.atomic_write(path, blob)


def read_task_bundle(run_dir) -> TaskBundle:
    """What §20 recorded. Raises if it is absent — never an empty bundle.

    `read_manifest`'s precedent. An absent bundle defaulting to "no entries" would make
    every §11 comparison and every materialization check pass over nothing.
    """
    path = storage.task_bundle_path(run_dir)
    try:
        raw = path.read_bytes()
    except FileNotFoundError as e:
        raise TaskBundleError(
            f"{path} does not exist: this run recorded no task bundle, so there is nothing "
            "to say what the seats were given.") from e
    try:
        return _decode(json.loads(raw), path)
    except ValueError as e:
        raise TaskBundleError(f"{path} is not readable as JSON: {e}") from e
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uvx --with pytest pytest -q tests/test_forge_taskbundle.py`
Expected: PASS — 12 passed.

- [ ] **Step 6: Re-run the new tests under scrambled names**

Rename every `test_*` in `tests/test_forge_taskbundle.py` to `test_zz0`, `test_zz1`, … and re-run. Any test that changes result was passing on its own name (pytest derives `tmp_path`'s basename from it) — this project has hit that five times.

Run: `uvx --with pytest pytest -q tests/test_forge_taskbundle.py`
Expected: PASS — same count. **Restore the original names.**

- [ ] **Step 7: Mutate every new branch**

Run each, one at a time, checking `git status` between them:

```bash
scripts/mutate.py --file shared/lib/forge/taskbundle.py \
  --old 'payload = {"version": b.version, "entrypoint": b.entrypoint, "entries": _rows(b.entries)}' \
  --new 'payload = {"version": b.version, "entries": _rows(b.entries)}' \
  -- uvx --with pytest pytest -q tests/test_forge_taskbundle.py

scripts/mutate.py --file shared/lib/forge/taskbundle.py \
  --old 'return sorted([e.path, e.kind, e.mode, e.sha256] for e in entries)' \
  --new 'return sorted([e.path, e.sha256] for e in entries)' \
  -- uvx --with pytest pytest -q tests/test_forge_taskbundle.py

scripts/mutate.py --file shared/lib/forge/taskbundle.py \
  --old 'if os.path.isabs(target) or joined.startswith(".."):' \
  --new 'if os.path.isabs(target):' \
  -- uvx --with pytest pytest -q tests/test_forge_taskbundle.py

scripts/mutate.py --file shared/lib/forge/taskbundle.py \
  --old '    if not entries:' \
  --new '    if False:' \
  -- uvx --with pytest pytest -q tests/test_forge_taskbundle.py
```

Expected: every one exits 0 (CAUGHT). A SURVIVED row means the branch has no test — add one before continuing. Run `git status` after the wave; it must be clean.

- [ ] **Step 8: Add the test file to the Makefile**

In `Makefile`, extend `FORGE_TESTS`:

```make
FORGE_TESTS := tests/test_forge_storage.py tests/test_forge_inspect.py \
               tests/test_forge_baseline.py tests/test_forge_screen.py \
               tests/test_forge_fleet.py tests/test_forge_packaging.py \
               tests/test_forge_snapshot.py tests/test_forge_harvest.py \
               tests/test_forge_seams.py tests/test_forge_bundle.py \
               tests/test_forge_verify.py tests/test_forge_journal.py \
               tests/test_forge_runstate.py tests/test_forge_preflight.py \
               tests/test_forge_gate.py tests/test_forge_seat.py \
               tests/test_forge_runner.py tests/test_forge_taskbundle.py
```

- [ ] **Step 9: Render, gate and commit**

```bash
make render
git add shared/lib/forge/taskbundle.py shared/lib/forge/storage.py \
        tests/test_forge_taskbundle.py Makefile marketplaces
make verify; echo "verify rc=$?"
make precommit; echo "precommit rc=$?"
```

Expected: both `rc=0`. Then:

```bash
git commit -m "$(cat <<'EOF'
feat(forge): §20's bundle is a manifest, and the hash sees the mode git's template rewrites

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UiV66Pt8cZVMq9t8WEAhpN
EOF
)"
```

---

### Task 2: Materialize the bundle into a seat, re-derive it from the seat, and hash the three installed closures

**Files:**
- Modify: `shared/lib/forge/taskbundle.py` (add `task_dir`, `materialize`, `verify_materialized`, `INSTALL_GLOBS`, `installed_closure`, `ambient_verdict`, `ambient_note`)
- Modify: `tests/test_forge_taskbundle.py`
- Modify: `tests/test_forge_seams.py` (one new seam)

**Interfaces:**

- **Consumes (Task 1):** `taskbundle.TaskBundle`, `BundleEntry`, `scan`, `bundle_hash`, `TaskBundleError`, `VERSION`.
- **Consumes (Plan H):** `gitcmd.git(repo, *args, ...)`, `gitcmd.READONLY`, `fleet.clone_seat`, `fleet.Seat`.
- **Produces:**
  ```python
  taskbundle.task_dir(seat_path) -> Path              # <git-dir>/khenrix-forge/task, asked of git
  taskbundle.materialize(b, source_root, seat_path) -> Path
  taskbundle.verify_materialized(b, seat_path) -> None          # raises on any mismatch
  taskbundle.INSTALL_GLOBS: dict[str, list[str]]
  taskbundle.installed_closure(cli: str) -> str | None          # None = not installed
  taskbundle.ambient_verdict(closures: dict) -> bool
  taskbundle.ambient_note(skill: str) -> str
  ```

**Why under the git directory rather than in the worktree.** Measured: git's clone-template content lands in `<dest>/.git/` and the worktree stays clean (`git status --porcelain` empty). Putting the bundle there buys four properties at once, and each is a real failure it forecloses: the agent never commits it; it never appears in `git status` or a diff, so it cannot be mistaken for the agent's work; `snapshot.take` skips it (`skip_dirs=(".git",)`) and `harvest.record` hard-excludes `.git`, so it can never be harvested as a deliverable; and `bundle._names_dotgit` refuses any candidate path naming `.git`, so it cannot be smuggled into a candidate either.

**And the git directory is asked of git.** `Path(seat) / ".git"` is wrong for a linked worktree, where `.git` is a *file*. `gitcmd.git(seat, "rev-parse", "--absolute-git-dir")` is the measured-safe call — `rev-parse` is on all three safe verb sets in `tests/test_forge_seams.py`, explicitly including the `--absolute-git-dir` form.

**Why `installed_closure` hashes the LIVE INSTALLED copies and not the repo source.** `refresh.sync` does `shutil.copytree(src, d, dirs_exist_ok=True)` — an additive overwrite that never removes a stale file. An installed copy can therefore be a strict superset of the repo source, so `checks.source_hash` (which hashes the repo) genuinely cannot stand in for it. This is §20's whole point about "byte-identical *source* in this repo does not prove the three *installed plugin copies* are current and identical".

**Why paths are excluded from the closure hash.** The three CLIs install to three different absolute paths by construction. A hash including the path would make three byte-identical closures hash differently, which would make §20's "all three hash identically" rule unsatisfiable — the ambient bar would read as permanently closed for a reason that is not about the closures at all.

**The fail-open to avoid, named:** `refresh.installed_dirs` returns `[]` for a CLI that is not installed. An empty list hashed as an empty manifest gives every uninstalled CLI the *same* hash, so three uninstalled CLIs "hash identically" and §20's rule licenses an ambient skill none of them have. **Not-installed is `None`, and `None` fails the equality test.**

> **ONE MEASUREMENT THIS PLAN HAS NOT TAKEN, and it must not be claimed as taken.** Whether each CLI's file-reading tools will open a path under the git directory is **unmeasured**. If one refuses, that seat cannot read its entrypoint, cannot quote the sentinel, and scores `failed` — which is the fail-closed direction, so the run stays honest, but the reason string will be unmapped. **Step 8 below is a one-off manual probe, outside the suite, that costs a fraction of one provider call.** If you decline to run it, record in the module docstring that the measurement was NOT taken; do not write a sentence claiming the path works. If a CLI does refuse, the recorded fallback is a second copy at `<seat>/.forge-task/` named in `.git/info/exclude` — but `clone_seat` overwrites `info/exclude` (`fleet.py:242-247`), so that fallback is a `fleet` change with its own blast radius and belongs in its own task, not smuggled in here.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_forge_taskbundle.py`:

```python
from forge import fleet, gitcmd  # noqa: E402
from forge_fixtures import make_repo  # noqa: E402


def _seat(tmp_path):
    """A real clone, built the way §4 builds one, so the git-dir question is a real one."""
    repo = make_repo(tmp_path / "repo")
    dest = tmp_path / "seat"
    s = fleet.clone_seat(repo, "HEAD", dest, name="claude",
                         identity=("Forge", "forge@example.invalid"))
    return repo, s


def test_the_task_directory_is_asked_of_git_not_joined_onto_the_seat(tmp_path):
    _, s = _seat(tmp_path)
    d = taskbundle.task_dir(s.path)
    real = gitcmd.git(s.path, "rev-parse", "--absolute-git-dir",
                      env_extra=gitcmd.READONLY).stdout.strip()
    assert d == Path(real) / "khenrix-forge" / "task"


def test_materialize_then_reverify_carries_every_shape_the_template_drops(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _tree(src)
    _, s = _seat(tmp_path)
    b = taskbundle.scan(src, entrypoint="SKILL.md")
    dest = taskbundle.materialize(b, src, s.path)
    taskbundle.verify_materialized(b, s.path)          # must not raise
    assert (dest / ".claude" / "skills" / "S.md").is_file(), \
        "the dot directory git's clone template was measured to drop silently"
    assert (dest / "config.ini").stat().st_mode & 0o777 == 0o600, \
        "the 0600 git's clone template was measured to rewrite to 0644"
    assert (dest / "scripts" / "tool.sh").stat().st_mode & 0o777 == 0o755
    assert (dest / "alias.md").is_symlink()


def test_the_bundle_is_invisible_to_the_seats_worktree(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _tree(src)
    _, s = _seat(tmp_path)
    taskbundle.materialize(taskbundle.scan(src, entrypoint="SKILL.md"), src, s.path)
    porcelain = gitcmd.git(s.path, "status", "--porcelain", *gitcmd.NO_DAEMON_CACHE,
                           env_extra=gitcmd.READONLY).stdout
    assert porcelain == "", \
        "a bundle the agent can commit is a bundle that can be harvested as its work"


def test_a_lost_file_is_a_refusal_not_a_silent_pass(tmp_path):
    """The whole reason materialization is re-derived: a copier that returns is not
    evidence the bytes arrived."""
    src = tmp_path / "src"
    src.mkdir()
    _tree(src)
    _, s = _seat(tmp_path)
    b = taskbundle.scan(src, entrypoint="SKILL.md")
    dest = taskbundle.materialize(b, src, s.path)
    (dest / ".claude" / "skills" / "S.md").unlink()
    with pytest.raises(taskbundle.TaskBundleError, match="does not match the authored"):
        taskbundle.verify_materialized(b, s.path)


def test_a_rewritten_mode_is_a_refusal(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _tree(src)
    _, s = _seat(tmp_path)
    b = taskbundle.scan(src, entrypoint="SKILL.md")
    dest = taskbundle.materialize(b, src, s.path)
    (dest / "config.ini").chmod(0o644)
    with pytest.raises(taskbundle.TaskBundleError, match="does not match the authored"):
        taskbundle.verify_materialized(b, s.path)


def test_re_verification_reads_the_seat_not_the_source(tmp_path):
    """Non-vacuity: mutating the SOURCE after materialization must not move the verdict."""
    src = tmp_path / "src"
    src.mkdir()
    _tree(src)
    _, s = _seat(tmp_path)
    b = taskbundle.scan(src, entrypoint="SKILL.md")
    taskbundle.materialize(b, src, s.path)
    (src / "SKILL.md").write_text("something else entirely\n")
    taskbundle.verify_materialized(b, s.path)          # still clean: the seat is intact


def test_materializing_twice_is_a_refusal_not_an_overwrite(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _tree(src)
    _, s = _seat(tmp_path)
    b = taskbundle.scan(src, entrypoint="SKILL.md")
    taskbundle.materialize(b, src, s.path)
    with pytest.raises(taskbundle.TaskBundleError, match="already"):
        taskbundle.materialize(b, src, s.path)


def test_an_uninstalled_cli_is_none_and_none_never_agrees_with_none():
    """§20's licence to use an ambient skill requires all three to hash IDENTICALLY.
    Two CLIs that were never looked at must not satisfy it."""
    assert taskbundle.ambient_verdict({"claude": None, "codex": None, "agy": None}) is False
    assert taskbundle.ambient_verdict({"claude": "a", "codex": "a", "agy": None}) is False
    assert taskbundle.ambient_verdict({"claude": "a", "codex": "a", "agy": "b"}) is False
    assert taskbundle.ambient_verdict({"claude": "a", "codex": "a", "agy": "a"}) is True


def test_the_closure_hash_is_stable_and_path_independent(tmp_path, monkeypatch):
    """Three CLIs install to three different absolute paths by construction. A hash that
    included the path would make §20's identity rule unsatisfiable for a reason that is
    not about the closures."""
    a, b2 = tmp_path / "one", tmp_path / "two"
    for d in (a, b2):
        (d / "skills").mkdir(parents=True)
        (d / "skills" / "S.md").write_text("same bytes\n")
    monkeypatch.setattr(taskbundle, "_install_dirs", lambda cli: [a] if cli == "claude" else [b2])
    assert taskbundle.installed_closure("claude") == taskbundle.installed_closure("codex")


def test_the_ambient_note_is_an_instruction_never_a_bar():
    note = taskbundle.ambient_note("chunk-map")
    assert "chunk-map" in note
    assert taskbundle.ambient_note.__doc__ and "not a mechanical bar" in \
        taskbundle.ambient_note.__doc__.lower(), \
        "§20's 'bar ambient invocation' has no mechanism in reach; the docstring must say so"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uvx --with pytest pytest -q tests/test_forge_taskbundle.py`
Expected: FAIL — `AttributeError: module 'forge.taskbundle' has no attribute 'task_dir'`.

- [ ] **Step 3: Add the materialization half to `taskbundle.py`**

Append to `shared/lib/forge/taskbundle.py`:

```python
def task_dir(seat_path) -> Path:
    """Where a seat's bundle lives: `<git-dir>/khenrix-forge/task`, with git asked for the git dir.

    ASKED, NEVER JOINED. `Path(seat) / ".git"` is a directory in an ordinary clone and a
    FILE in a linked worktree, so the join is right by luck and wrong the moment §16's
    synthesis worktree exists. `rev-parse --absolute-git-dir` is measured safe on all three
    of this package's git closures — it loads no index, fires no hook and runs no diff
    driver — so it needs `READONLY` and nothing else.
    """
    out = gitcmd.git(seat_path, "rev-parse", "--absolute-git-dir",
                     env_extra=gitcmd.READONLY).stdout.strip()
    if not out:
        raise TaskBundleError(f"git named no git directory for {seat_path}")
    return Path(out) / "khenrix-forge" / "task"


def materialize(b: TaskBundle, source_root, seat_path) -> Path:
    """Lay `b`'s bytes down inside `seat_path`'s git directory, preserving mode and kind.

    ENGINE-OWNED COPIER, NOT `shutil.copytree` AND NOT `git clone --template`. The template
    path drops every dot-name at every level and normalizes modes (see the module
    docstring). `copytree` would be closer but still walks the SOURCE, and what must be laid
    down is what the MANIFEST says — otherwise a file added to the source between `scan` and
    here arrives in the seat unhashed and unnamed.

    A REFUSAL, NOT AN OVERWRITE, when the directory already exists. §8.1 gives every retry a
    fresh clone and this module contains no delete of any kind: a second materialization into
    a live seat would be the reset-and-rerun-in-place §8.1 forbids, one directory over.
    """
    dest = task_dir(seat_path)
    if dest.exists():
        raise TaskBundleError(
            f"{dest} already holds a task bundle. §8.1 gives a retry a FRESH clone; "
            "re-materializing into a live seat is a reset-and-rerun in place.")
    source_root = Path(source_root)
    dest.mkdir(parents=True)
    for e in b.entries:
        target = dest / e.path
        target.parent.mkdir(parents=True, exist_ok=True)
        src = source_root / e.path
        if e.kind == "symlink":
            os.symlink(os.readlink(src), target)
        else:
            # Bytes then mode, in that order: a 0400 file written mode-first cannot be
            # written to. `chmod` after `write_bytes` is the only ordering that works for
            # every mode the manifest can carry.
            target.write_bytes(src.read_bytes())
            os.chmod(target, e.mode)
    return dest


def verify_materialized(b: TaskBundle, seat_path) -> None:
    """Re-derive the manifest FROM THE SEAT and refuse any difference from the authored one.

    THE ONLY STEP THAT TURNS A SILENT LOSS INTO A REFUSAL. A copier that runs and returns is
    not evidence the bytes arrived — `fleet.Seat.verified` makes exactly this argument about
    an empty `filesystem_manifest` making a check "vacuous and still answer True", which is
    why the trusted parent recomputes readiness from primary evidence. Everything git's
    template path loses (dot names, 0600, the +x-only mode rule) is a difference here.

    Compared through `bundle_hash` AND field by field: the hash is what §11 records, and the
    per-entry diff is what a human reading the refusal can act on. A message naming only "the
    hashes differ" sends the reader through the whole closure by hand.
    """
    dest = task_dir(seat_path)
    if not dest.is_dir():
        raise TaskBundleError(f"{dest} holds no task bundle; nothing was materialized")
    seen = TaskBundle(b.version, b.entrypoint, tuple(_walk(dest)),
                      b.max_files, b.max_file_bytes, b.max_total_bytes)
    if bundle_hash(seen) == bundle_hash(b):
        return
    authored = {e.path: e for e in b.entries}
    found = {e.path: e for e in seen.entries}
    lost = sorted(set(authored) - set(found))
    extra = sorted(set(found) - set(authored))
    changed = sorted(p for p in set(authored) & set(found) if authored[p] != found[p])
    raise TaskBundleError(
        f"the bundle in {dest} does not match the authored manifest: missing={lost} "
        f"unexpected={extra} altered={changed}. §20 requires it materialized IDENTICALLY in "
        "every clone, and a manifest that lists a file the seat does not have is the one "
        "failure no later check can see.")


# The three live installed plugin paths. DUPLICATED from `scripts/refresh.py:INSTALL_GLOBS`
# rather than imported, because `shared/lib/` is importable by three CLIs and `scripts/` is
# this repository's own tooling — the import would invert the layering. Two spellings of one
# fact eventually disagree, so `tests/test_forge_seams.py` asserts they are equal.
INSTALL_GLOBS = {
    "claude": ["~/.claude/plugins/cache/khenrix-claude-marketplace/khenrix-utils/*"],
    "codex": ["~/.codex/plugins/cache/khenrix-codex-marketplace/khenrix-utils/*"],
    "agy": ["~/.gemini/config/plugins/khenrix-utils"],
}


def _install_dirs(cli: str) -> list:
    """Mirrors `refresh.installed_dirs`. Seam-tested against it; see INSTALL_GLOBS."""
    out = []
    for g in INSTALL_GLOBS[cli]:
        base = Path(g.replace("~", str(Path.home())))
        if "*" in g:
            out += [p for p in base.parent.glob(base.name) if p.is_dir()]
        elif base.is_dir():
            out.append(base)
    return sorted(out)


def installed_closure(cli: str) -> str | None:
    """The hash of `cli`'s LIVE INSTALLED plugin closure, or None because it is not installed.

    THE INSTALLED COPY, NEVER THE REPO SOURCE, and the reason is mechanical rather than
    stylistic: `refresh.sync` does `copytree(src, d, dirs_exist_ok=True)`, an ADDITIVE
    overwrite that never removes a stale file. An installed copy can therefore be a strict
    superset of the repo source, so `checks.source_hash` provably cannot stand in for it —
    which is exactly §20's distinction between byte-identical source and three current,
    identical installed copies.

    PATHS ARE NOT HASHED. The three CLIs install to three different absolute paths by
    construction; a hash carrying the path would make §20's "all three hash identically"
    rule unsatisfiable for a reason that has nothing to do with the closures.

    None, NEVER an empty-manifest hash. `refresh.installed_dirs` returns [] for a CLI that
    is not installed, and hashing [] gives every uninstalled CLI the SAME value — three
    seats "hashing identically", which is precisely §20's licence to rely on an ambient
    skill, manufactured out of three absences. `seat.read_proof`'s rule, one module over:
    a missing measurement fails closed.
    """
    dirs = _install_dirs(cli)
    if not dirs:
        return None
    rows = []
    for d in dirs:
        rows += _rows(_walk(d))
    return hashlib.sha256(json.dumps(sorted(rows), sort_keys=True).encode()).hexdigest()


def ambient_verdict(closures: dict) -> bool:
    """§20's bar: a named skill may be relied on only when ALL THREE hash identically.

    A `None` anywhere is False, and that is the whole point — see `installed_closure`. Both
    halves are recorded by the caller, never just this verdict: a record holding only the
    boolean cannot show what it compared.
    """
    values = [closures.get(c) for c in ("claude", "codex", "agy")]
    return all(v is not None for v in values) and len(set(values)) == 1


def ambient_note(skill: str) -> str:
    """§20's "bar ambient invocation of the same skill", as the instruction it actually is.

    THIS IS NOT A MECHANICAL BAR and the report must never present it as one. The only
    mechanism in reach is a sentence in the prompt; a seat that ignores it is not stopped by
    anything. Recorded in the register §10.1 reserves for `manual_trace_confirmed` — an
    instruction issued, not a property enforced. If a per-CLI settings toggle that really
    does bar it exists, it has to be MEASURED before any code here claims it.
    """
    return (f"Do not invoke the ambient `{skill}` skill. The task bundle materialized for "
            "this run is the only copy you may read; an ambient copy may differ from it.")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uvx --with pytest pytest -q tests/test_forge_taskbundle.py`
Expected: PASS — 22 passed.

- [ ] **Step 5: Add the cross-module seam**

Append to `tests/test_forge_seams.py`:

```python
def test_the_installed_plugin_paths_have_one_spelling():
    """`taskbundle.INSTALL_GLOBS` duplicates `refresh.INSTALL_GLOBS` because `shared/lib/`
    may not import `scripts/`. Two spellings of one predicate eventually disagree — and the
    disagreement here would be silent: a stale glob hashes a directory that is not the one
    `make khenrix-refresh` writes, and §20's identity rule would compare the wrong closures.
    """
    import sys as _sys
    _sys.path.insert(0, str(ROOT / "scripts"))
    import refresh  # noqa: PLC0415

    from forge import taskbundle  # noqa: PLC0415
    assert taskbundle.INSTALL_GLOBS == refresh.INSTALL_GLOBS
    for cli in refresh.CLIS:
        assert taskbundle._install_dirs(cli) == sorted(refresh.installed_dirs(cli)), \
            f"{cli}: the two enumerations disagree about what is installed"
```

> `ROOT` is already defined at the top of `tests/test_forge_seams.py`; verify that before relying on it and define it locally if it is not.

- [ ] **Step 6: Run the seam test**

Run: `uvx --with pytest pytest -q tests/test_forge_seams.py -k installed_plugin_paths`
Expected: PASS — 1 passed.

- [ ] **Step 7: Mutate the new branches**

```bash
scripts/mutate.py --file shared/lib/forge/taskbundle.py \
  --old '    if not dirs:\n        return None' \
  --new '    if not dirs:\n        return hashlib.sha256(b"[]").hexdigest()' \
  -- uvx --with pytest pytest -q tests/test_forge_taskbundle.py

scripts/mutate.py --file shared/lib/forge/taskbundle.py \
  --old 'return all(v is not None for v in values) and len(set(values)) == 1' \
  --new 'return len(set(values)) == 1' \
  -- uvx --with pytest pytest -q tests/test_forge_taskbundle.py

scripts/mutate.py --file shared/lib/forge/taskbundle.py \
  --old 'seen = TaskBundle(b.version, b.entrypoint, tuple(_walk(dest)),' \
  --new 'seen = TaskBundle(b.version, b.entrypoint, b.entries,' \
  -- uvx --with pytest pytest -q tests/test_forge_taskbundle.py

scripts/mutate.py --file shared/lib/forge/taskbundle.py \
  --old '            os.chmod(target, e.mode)' \
  --new '            pass' \
  -- uvx --with pytest pytest -q tests/test_forge_taskbundle.py
```

Expected: all four exit 0 (CAUGHT). `git status` clean afterwards.

- [ ] **Step 8: The one-off provider probe (manual, outside the suite)**

**This spends a fraction of one provider call and is NOT a test.** It answers whether a CLI's file-reading tools will open a path under the git directory.

```bash
tmp=$(mktemp -d)
git init -q "$tmp/seat"
mkdir -p "$tmp/seat/.git/khenrix-forge/task"
printf 'The magic word is PLUMBUS.\n' > "$tmp/seat/.git/khenrix-forge/task/SKILL.md"
cd "$tmp/seat" && claude -p "Read .git/khenrix-forge/task/SKILL.md and print the magic word." \
  --output-format json --dangerously-skip-permissions
```

Repeat for `codex exec -` and `agy`. Record the outcome for each CLI in `taskbundle.py`'s module docstring under a heading `MEASURED (date): CLI ACCESS TO THE TASK DIRECTORY`. **If you decline to run it, write `NOT MEASURED` there instead and do not add a sentence claiming the path works** — a comment asserting something nobody measured is the defect this project keeps finding.

- [ ] **Step 9: Render, gate and commit**

```bash
make render
git add shared/lib/forge/taskbundle.py tests/test_forge_taskbundle.py \
        tests/test_forge_seams.py marketplaces
make verify; echo "verify rc=$?"
make precommit; echo "precommit rc=$?"
```

Expected: both `rc=0`. Then:

```bash
git commit -m "$(cat <<'EOF'
feat(forge): the bundle is re-derived from the seat, because a copier that returns is not evidence

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UiV66Pt8cZVMq9t8WEAhpN
EOF
)"
```

---

### Task 3: The claim ledger — content-derived ids, one edge set, and a cycle that is a refusal

**Files:**
- Create: `shared/lib/forge/ledger.py`
- Create: `tests/test_forge_ledger.py`
- Modify: `Makefile` (add `tests/test_forge_ledger.py` to `FORGE_TESTS`)

**Interfaces:**

- **Consumes (Task 1):** `storage.ledger_path(run_dir)`, `storage.atomic_write(path, data: bytes)`.
- **Produces:**
  ```python
  ledger.VERSION: int = 1
  ledger.LedgerError(RuntimeError)
  ledger.KINDS = ("behavior","api","schema","migration","security","test","architecture","seam")
  ledger.STATUSES = ("accepted", "rejected", "deferred", "unresolved")
  ledger.RELATIONS = ("requires", "conflicts", "blocks")
  ledger.STANCES = ("supports", "contradicts", "silent")
  ledger.CRITERION_KINDS = ("test", "symbol", "hash", "schema", "prose")
  ledger.DEGRADE_UNION_DIFF_BYTES: int = 512 * 1024

  ledger.Dependency(id: str, relation: str)
  ledger.SeatEvidence(seat: str, stance: str, evidence: str, prompt_sha256: str | None)
  ledger.Criterion(kind: str, text: str, path: str | None, symbol: str | None,
                   node_id: str | None, sha256: str | None, trace: str | None)
  ledger.Row(id, requirement_id, requirement_span, requirement_sha256, kind, component,
             semantic_claim, status, dependencies: tuple[Dependency, ...],
             seat_evidence: tuple[SeatEvidence, ...], counterevidence: str,
             acceptance_criteria: tuple[Criterion, ...], synthesis_evidence: dict | None,
             verification_receipt: str | None, risk: str, rationale: str)
  ledger.Ledger(version, rows: tuple[Row, ...], union_diff_bytes: int,
                degrade_threshold_bytes: int, degraded: bool)

  ledger.row_id(requirement_id: str, semantic_claim: str) -> str
  ledger.edges(rows) -> tuple[tuple[str, str], ...]
  ledger.topological_order(rows) -> tuple[str, ...]
  ledger.write_ledger(run_dir, l: Ledger) -> None
  ledger.read_ledger(run_dir) -> Ledger
  ledger.ledger_hash(l: Ledger) -> str
  ```

**Storage: one JSON object at `<run_dir>/ledger.json`, published with `atomic_write`.**

- *Not `exclusive_write`.* The manifest is write-once because §14.2 makes the run's identity agreed once and never rewritten. The ledger is the opposite: §13's loop revises row `status` across review rounds and §12.2 freezes only the `seam` rows. Write-once would make round 2 a crash.
- *Not JSONL.* §14.1's append-only, torn-tail discipline is for *events*, whose value is that every line is a fact that happened. A row's `status` changes, so an append-only ledger forces every reader to fold the log to learn the current row — and two folds written in two places will eventually disagree. The journal covers "what happened"; the ledger covers "what is currently claimed". Two files, two disciplines.
- **The ledger's bytes exist under NO clone root** — not a seat, not the synthesis checkout, not a verifier clone, and above all not inside the §20 task bundle, which reviewers *are* given. §13 sets every reviewer's cwd to the synthesis checkout and a reviewer has a shell, so the guarantee has to be structural (the bytes are not in the tree) rather than textual (an instruction not to read them). Plan I₂ owes the mechanical assertion; Task 3 owes not creating the problem.
- **§14.1 embeds the ledger HASH in every checkpoint commit message, never row text.** Reviewers run `git log` by design. A hash is safe; a summary line quoting a claim is the ledger handed over through git. Say "hash" every time.

**Row identity: framed, never concatenated.** The spec writes `sha256(requirement_id || semantic_claim)`. A bare `||` is ambiguous — `("ab","c")` and `("a","bc")` produce the same bytes — and that is not theoretical: §10's stated reason for content-derived ids is that "a round splits or inserts a claim", and splitting a claim is exactly the operation that manufactures such a pair. A JSON array is injective because quoting and escaping delimit the fields, and it is this repo's existing spelling for a content hash (`checks.source_hash`).

**No normalization.** No strip, no casefold, no whitespace collapse, no NFC. Two reasons: a normalization rule is a second predicate every implementer must spell identically, and §10 *wants* an edited claim to be a different claim. Editing `semantic_claim` produces a **new row**; the round that rephrases inserts the new row and resolves the old one (`deferred`/`rejected`, `rationale` naming the successor id) rather than mutating text in place.

**The check that keeps the value honest:** `write_ledger` recomputes `row_id` for every row and refuses any row whose stored `id` differs. Without it, an editor that changes the claim text and leaves the id is *exactly* the failure §10 names, and it is invisible — the ledger stays well-formed while coverage keeps comparing a stale identity under a stable-looking key.

**`requires` and `blocks` are one edge with two names.** This is the first real defect in §10's row spec, and it is dangerous rather than cosmetic: a cycle checker walking only `requires` misses every cycle a writer expressed with `blocks`, and passes. Normalize at decode: `requires` on row X naming Y is the edge `X → Y`; `blocks` on row A naming B is `B → A` — the same edge `{"id": A, "relation": "requires"}` on row B would produce. `conflicts` is **symmetric and not part of the ordering graph**; a "cycle" of conflicts is meaningless.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_forge_ledger.py`:

```python
"""§10: the claim ledger — what is currently claimed, with an identity that cannot drift."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

import json  # noqa: E402
import pytest  # noqa: E402
from forge import ledger, storage  # noqa: E402


def _crit(**kw):
    base = dict(kind="prose", text="the thing works", path=None, symbol=None,
                node_id=None, sha256=None, trace=None)
    base.update(kw)
    return ledger.Criterion(**base)


def _row(requirement_id="R1", claim="records carry a monotonic seq", **kw):
    base = dict(
        id=ledger.row_id(requirement_id, claim),
        requirement_id=requirement_id,
        requirement_span="spec.md:10-12",
        requirement_sha256="0" * 64,
        kind="behavior", component="core", semantic_claim=claim, status="accepted",
        dependencies=(), seat_evidence=(), counterevidence="",
        acceptance_criteria=(_crit(),), synthesis_evidence=None,
        verification_receipt=None, risk="low", rationale="")
    base.update(kw)
    if "id" not in kw:
        base["id"] = ledger.row_id(base["requirement_id"], base["semantic_claim"])
    return ledger.Row(**base)


def _led(rows, **kw):
    base = dict(version=ledger.VERSION, rows=tuple(rows), union_diff_bytes=100,
                degrade_threshold_bytes=ledger.DEGRADE_UNION_DIFF_BYTES, degraded=False)
    base.update(kw)
    return ledger.Ledger(**base)


def test_the_id_frames_its_fields_so_a_split_claim_cannot_collide():
    """§10's own reason for content-derived ids is that a round SPLITS a claim — which is
    exactly the operation that manufactures a `||` collision."""
    assert ledger.row_id("ab", "c") != ledger.row_id("a", "bc")


def test_the_id_is_not_normalized_because_an_edited_claim_is_a_new_claim():
    assert ledger.row_id("R1", "x") != ledger.row_id("R1", " x")
    assert ledger.row_id("R1", "X") != ledger.row_id("R1", "x")


def test_a_row_whose_id_does_not_hash_its_own_claim_is_refused(tmp_path):
    """The invariant lives in the VALUE — the id IS the hash — and this check is what keeps
    the value honest. Without it, an edit that leaves the id stale is invisible."""
    r = _row()
    stale = ledger.Row(**{**r.__dict__, "semantic_claim": "something else"})
    with pytest.raises(ledger.LedgerError, match="does not hash its own"):
        ledger.write_ledger(tmp_path, _led([stale]))


def test_two_rows_with_one_id_and_different_claims_are_refused(tmp_path):
    a = _row("R1", "alpha")
    b = ledger.Row(**{**_row("R2", "beta").__dict__, "id": a.id})
    with pytest.raises(ledger.LedgerError, match="does not hash its own"):
        ledger.write_ledger(tmp_path, _led([a, b]))


def test_a_duplicated_row_id_is_refused_rather_than_merged(tmp_path):
    a = _row("R1", "alpha")
    with pytest.raises(ledger.LedgerError, match="twice"):
        ledger.write_ledger(tmp_path, _led([a, a]))


def test_a_cycle_written_with_blocks_is_caught(tmp_path):
    """The defect this is written against: `requires` and `blocks` are ONE edge with two
    names, and a checker walking `requires` alone passes a cyclic graph written with
    `blocks`."""
    a = _row("R1", "alpha")
    b = _row("R2", "beta")
    a2 = ledger.Row(**{**a.__dict__,
                       "dependencies": (ledger.Dependency(b.id, "blocks"),)})
    b2 = ledger.Row(**{**b.__dict__,
                       "dependencies": (ledger.Dependency(a.id, "blocks"),)})
    with pytest.raises(ledger.LedgerError, match="cycle"):
        ledger.write_ledger(tmp_path, _led([a2, b2]))


def test_a_cycle_mixing_the_two_spellings_is_caught(tmp_path):
    a, b = _row("R1", "alpha"), _row("R2", "beta")
    a2 = ledger.Row(**{**a.__dict__, "dependencies": (ledger.Dependency(b.id, "requires"),)})
    b2 = ledger.Row(**{**b.__dict__, "dependencies": (ledger.Dependency(a.id, "requires"),)})
    with pytest.raises(ledger.LedgerError, match="cycle"):
        ledger.write_ledger(tmp_path, _led([a2, b2]))


def test_the_cycle_refusal_names_the_path_and_clips_the_claims(tmp_path):
    """A human reading this refusal cannot act on twelve hex characters."""
    a, b = _row("R1", "alpha claim"), _row("R2", "beta claim")
    a2 = ledger.Row(**{**a.__dict__, "dependencies": (ledger.Dependency(b.id, "requires"),)})
    b2 = ledger.Row(**{**b.__dict__, "dependencies": (ledger.Dependency(a.id, "requires"),)})
    with pytest.raises(ledger.LedgerError) as e:
        ledger.write_ledger(tmp_path, _led([a2, b2]))
    assert a.id in str(e.value) and b.id in str(e.value)
    assert "alpha claim" in str(e.value) and "beta claim" in str(e.value)


def test_a_self_edge_is_a_one_node_cycle(tmp_path):
    a = _row("R1", "alpha")
    a2 = ledger.Row(**{**a.__dict__, "dependencies": (ledger.Dependency(a.id, "requires"),)})
    with pytest.raises(ledger.LedgerError, match="cycle"):
        ledger.write_ledger(tmp_path, _led([a2]))


def test_a_dangling_dependency_is_refused_not_skipped(tmp_path):
    """Skipping it makes the sort succeed over a graph MISSING the constraint, and §12.2's
    'topological ordering is a precondition of partitioned synthesis' would then be
    satisfied over the wrong graph."""
    a = _row("R1", "alpha")
    a2 = ledger.Row(**{**a.__dict__,
                       "dependencies": (ledger.Dependency("ffffffffffff", "requires"),)})
    with pytest.raises(ledger.LedgerError, match="no row carries"):
        ledger.write_ledger(tmp_path, _led([a2]))


def test_conflicts_is_symmetric_and_not_an_ordering_edge(tmp_path):
    """A 'cycle' of conflicts is meaningless; two rows may conflict mutually and still be
    a legal ledger. Whether they may both be ACCEPTED is a coverage question, not a write
    refusal."""
    a, b = _row("R1", "alpha"), _row("R2", "beta")
    a2 = ledger.Row(**{**a.__dict__, "dependencies": (ledger.Dependency(b.id, "conflicts"),)})
    b2 = ledger.Row(**{**b.__dict__, "dependencies": (ledger.Dependency(a.id, "conflicts"),)})
    ledger.write_ledger(tmp_path, _led([a2, b2]))
    assert ledger.read_ledger(tmp_path).rows[0].id == a.id
    assert ledger.edges([a2, b2]) == ()


def test_a_self_conflict_is_refused_as_nonsense(tmp_path):
    a = _row("R1", "alpha")
    a2 = ledger.Row(**{**a.__dict__, "dependencies": (ledger.Dependency(a.id, "conflicts"),)})
    with pytest.raises(ledger.LedgerError, match="conflicts with itself"):
        ledger.write_ledger(tmp_path, _led([a2]))


def test_the_order_covers_every_row_or_there_is_no_order():
    """Kahn's algorithm stops early on a cycle; a caller reading the emitted list without
    comparing its length to the row count synthesizes the partitions it happened to emit."""
    a, b = _row("R1", "alpha"), _row("R2", "beta")
    a2 = ledger.Row(**{**a.__dict__, "dependencies": (ledger.Dependency(b.id, "requires"),)})
    order = ledger.topological_order([a2, b])
    assert order == (b.id, a2.id)
    b2 = ledger.Row(**{**b.__dict__, "dependencies": (ledger.Dependency(a.id, "requires"),)})
    with pytest.raises(ledger.LedgerError, match="cycle"):
        ledger.topological_order([a2, b2])


def test_a_relation_the_vocabulary_does_not_name_is_refused(tmp_path):
    a = _row("R1", "alpha")
    a2 = ledger.Row(**{**a.__dict__, "dependencies": (ledger.Dependency(a.id, "supersedes"),)})
    with pytest.raises(ledger.LedgerError, match="relation"):
        ledger.write_ledger(tmp_path, _led([a2]))


def test_a_symbol_criterion_must_carry_structured_fields_not_a_sentence(tmp_path):
    """§10.1's own worked example is a symbol-presence check standing in for a behavioural
    claim while the property is false. A symbol predicate may only be attached to a
    criterion PHRASED as 'P defines S'."""
    bad = _row(acceptance_criteria=(_crit(kind="symbol",
                                          text="storage.atomic_write is crash-safe"),))
    with pytest.raises(ledger.LedgerError, match="path.*symbol"):
        ledger.write_ledger(tmp_path, _led([bad]))
    good = _row(acceptance_criteria=(_crit(kind="symbol",
                                           text="storage.py defines atomic_write",
                                           path="shared/lib/forge/storage.py",
                                           symbol="atomic_write"),))
    ledger.write_ledger(tmp_path, _led([good]))


def test_a_trace_may_not_hang_on_a_mechanical_criterion(tmp_path):
    """A human note beside a predicate that could flip the method to
    `manual_trace_confirmed` is §10.1's manufactured green by a second route."""
    bad = _row(acceptance_criteria=(_crit(kind="test", node_id="tests/t.py::test_x",
                                          trace="I checked it by hand"),))
    with pytest.raises(ledger.LedgerError, match="trace"):
        ledger.write_ledger(tmp_path, _led([bad]))


def test_the_degradation_is_recorded_in_the_ledger_not_only_in_a_report(tmp_path):
    """§10 also says from-scratch synthesis 'reads only the ledger', so a degraded ledger
    silently becomes the INPUT to synthesis. The threshold is recorded too, so changing it
    later cannot reinterpret an old ledger."""
    big = _led([_row()], union_diff_bytes=ledger.DEGRADE_UNION_DIFF_BYTES + 1, degraded=True)
    ledger.write_ledger(tmp_path, big)
    back = ledger.read_ledger(tmp_path)
    assert back.degraded is True
    assert back.degrade_threshold_bytes == ledger.DEGRADE_UNION_DIFF_BYTES
    assert back.union_diff_bytes == ledger.DEGRADE_UNION_DIFF_BYTES + 1


def test_a_ledger_claiming_undegraded_over_the_threshold_is_refused(tmp_path):
    with pytest.raises(ledger.LedgerError, match="degrad"):
        ledger.write_ledger(tmp_path, _led([_row()],
                                           union_diff_bytes=ledger.DEGRADE_UNION_DIFF_BYTES + 1,
                                           degraded=False))


def test_the_ledger_round_trips_and_hashes_stably(tmp_path):
    l = _led([_row("R1", "alpha"), _row("R2", "beta")])
    ledger.write_ledger(tmp_path, l)
    back = ledger.read_ledger(tmp_path)
    assert back == l
    assert ledger.ledger_hash(back) == ledger.ledger_hash(l)


def test_the_hash_moves_when_a_status_moves(tmp_path):
    """§14.1 embeds this in every checkpoint commit message; a status revision that did not
    move the hash would make the message a record of nothing."""
    a = _row("R1", "alpha")
    before = ledger.ledger_hash(_led([a]))
    after = ledger.ledger_hash(_led([ledger.Row(**{**a.__dict__, "status": "rejected"})]))
    assert before != after


def test_a_missing_ledger_raises_rather_than_reading_as_no_claims(tmp_path):
    """An empty ledger is a run with no claims, which the coverage check reports as fully
    covered."""
    with pytest.raises(ledger.LedgerError, match="does not exist"):
        ledger.read_ledger(tmp_path)


def test_a_status_the_vocabulary_does_not_name_is_refused(tmp_path):
    """The measured `{"phse": "biulding"}` failure, on a record where it decides the
    deliverable: `acceptd` reads as neither accepted nor rejected and coverage skips it."""
    ledger.write_ledger(tmp_path, _led([_row()]))
    p = storage.ledger_path(tmp_path)
    p.write_text(p.read_text().replace('"accepted"', '"acceptd"'))
    with pytest.raises(ledger.LedgerError, match="status"):
        ledger.read_ledger(tmp_path)


def test_an_unknown_row_field_is_refused_on_the_way_back_in(tmp_path):
    ledger.write_ledger(tmp_path, _led([_row()]))
    p = storage.ledger_path(tmp_path)
    row = json.loads(p.read_text())
    row["rows"][0]["novel"] = 1
    p.write_text(json.dumps(row))
    with pytest.raises(ledger.LedgerError, match="does not know"):
        ledger.read_ledger(tmp_path)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uvx --with pytest pytest -q tests/test_forge_ledger.py`
Expected: FAIL — `ImportError: cannot import name 'ledger' from 'forge'`.

- [ ] **Step 3: Write `ledger.py`**

Create `shared/lib/forge/ledger.py`:

```python
"""§10: the claim ledger — what is CURRENTLY CLAIMED about the fused result.

A COMPACTION-SURVIVABLE SPEC AND AUDIT TRAIL, claimed as nothing more. Writing it requires
reading all three artifact sets, so peak context is unchanged; it is not a context-budget
control and must not be sold as one.

TWO FILES, TWO DISCIPLINES. `events.jsonl` is append-only because every line is a fact that
HAPPENED. A ledger row's `status` CHANGES — §13's loop revises it across review rounds — so an
append-only ledger would force every reader to fold the log to learn the current row, and two
folds written in two places eventually disagree. The journal covers what happened; this covers
what is claimed. Published through `storage.atomic_write`, so a reader arriving mid-write sees
the previous ledger whole.

NOT `exclusive_write`. The manifest is write-once because §14.2 makes the run's identity agreed
once and never rewritten; this is the opposite, and write-once would make review round 2 a crash.

A STRICT DECODER, AND `write_seat`'S SILENCE IS NOT A PRECEDENT FOR IT. That module argues its
own case: §14.2 assigns the seat record's fields to the ORCHESTRATOR, so `runstate` refuses to
become the authority on a record another module owns. None of that transfers. §10 enumerates
these fields exactly; §14.1 hashes this file into EVERY checkpoint commit message, so a field a
writer stopped writing silently changes the hash with nothing saying why; and §10's own text
says from-scratch synthesis "reads only the ledger", so an unvalidated row is directly a wrong
deliverable rather than a confusing record. Measured on that module: `{"phse": "biulding"}`
round-trips with no complaint. Here, `status: "acceptd"` would read as neither accepted nor
rejected and the coverage check would silently skip it.

WHERE THE BYTES ARE, AND WHERE THEY ARE NOT. The run directory, at 0700, under
`${XDG_STATE_HOME}/khenrix-forge/<hash>-<run-id>/`. NO LEDGER BYTES EXIST UNDER ANY CLONE ROOT —
not a seat clone, not the synthesis checkout, not a verifier clone, and above all not inside the
§20 task bundle, which reviewers ARE given. §13 sets every reviewer's cwd to the synthesis
checkout and a reviewer has a shell, so the guarantee must be STRUCTURAL (the bytes are not in
the tree), never textual (an instruction not to read them). §14.1 embeds the ledger HASH in the
checkpoint commit message and reviewers run `git log` by design — a hash is safe, a summary line
quoting a claim is the ledger handed over through git. Say hash; never summary.

WHAT THIS MODULE REFUSES AT WRITE AND WHAT IT LEAVES TO COVERAGE. Structure is refused here,
where the producer is still present and can fix it (`write_manifest`'s round-trip refusal makes
the same argument): a stale id, a duplicate id, an unknown vocabulary word, a dangling
dependency, an ordering cycle, a self-conflict, a criterion whose kind and fields disagree.
SEMANTICS are `coverage`'s: whether two conflicting rows are both accepted, and whether an
accepted row contradicts a unanimous rejection, are findings a report carries — §12.4 makes the
coverage check "a fallback trigger AND a report line", which a write refusal cannot be.
"""
import dataclasses
import hashlib
import json
from dataclasses import dataclass, fields
from pathlib import Path

from . import storage

VERSION = 1

KINDS = ("behavior", "api", "schema", "migration", "security", "test", "architecture", "seam")
STATUSES = ("accepted", "rejected", "deferred", "unresolved")
# ONE ORDERING RELATION WITH TWO NAMES, plus a symmetric one that is not an ordering at all.
RELATIONS = ("requires", "conflicts", "blocks")
STANCES = ("supports", "contradicts", "silent")
CRITERION_KINDS = ("test", "symbol", "hash", "schema", "prose")

# §10 says "above N KB union diff, drop to per-file summaries and say so in the report" and
# never gives N. An unspecified threshold is one two implementers set differently, so it is a
# named constant here AND a recorded field on every ledger: changing this value later must not
# silently reinterpret a ledger written under the old one.
DEGRADE_UNION_DIFF_BYTES = 512 * 1024

# How much of a claim a refusal quotes. A human reading a cycle refusal cannot act on twelve
# hex characters, and a whole claim per node makes the message unreadable at four nodes.
_CLAIM_CLIP = 80


class LedgerError(RuntimeError):
    """This ledger cannot be recorded honestly, or cannot be read back as what was written."""


@dataclass(frozen=True)
class Dependency:
    """One relation this row declares. VALUE FORM, not key form.

    §10 writes `[{id, requires|conflicts|blocks}]`, which does not say whether the relation is
    a key (`{"id": X, "requires": true}`) or a value (`{"id": X, "relation": "requires"}`). Two
    implementers read it two ways and write two records. The value form is chosen and the
    decoder refuses any other key and any other relation string.
    """
    id: str
    relation: str


@dataclass(frozen=True)
class SeatEvidence:
    """One seat's stance on one claim. §10: a NESTED LIST, never flattened columns.

    Prompt-identity conditioning (§11) is per seat and cannot be recorded any other way.
    `prompt_sha256` is `str | None`, and `None` means the seat's identity was never captured —
    it must never compare equal to another `None` for "the same prompt".
    """
    seat: str
    stance: str
    evidence: str
    prompt_sha256: str | None


@dataclass(frozen=True)
class Criterion:
    """One acceptance criterion, with its evaluator's inputs as STRUCTURED FIELDS.

    THE RULE THAT MAKES §10.1 ENFORCEABLE. §10.1's worked example is a symbol-presence check
    (`os.replace` appears) standing in for a behavioural claim (crash-safe atomic update) while
    the property is false. So a mechanical criterion may only be attached to a criterion PHRASED
    as the thing the predicate proves: `kind="symbol"` must carry `path` and `symbol` as fields,
    and a sentence with a symbol name in it is refused. The decoder is where that lives, because
    a rule stated only in prose is one the next author will not meet.

    `trace` is a human's record of having traced the claim, and it is permitted ONLY on `prose`
    and `schema` — the two kinds with no predicate. On a mechanical kind the predicate IS the
    evidence, and a human note beside it that could flip the method to `manual_trace_confirmed`
    is §10.1's manufactured green arriving by a second route.
    """
    kind: str
    text: str
    path: str | None
    symbol: str | None
    node_id: str | None
    sha256: str | None
    trace: str | None


@dataclass(frozen=True)
class Row:
    """§10's row. `requirement_id` is split into three fields because §10 asks for it "+ source
    span/hash" and one string cannot carry three facts a reader has to compare separately.

    `rejected` IS FIRST-CLASS. If all three seats considered and rejected a cache layer, that is
    the most valuable signal in the run — and from-scratch synthesis, which reads only this file,
    would otherwise add it straight back.
    """
    id: str
    requirement_id: str
    requirement_span: str
    requirement_sha256: str
    kind: str
    component: str
    semantic_claim: str
    status: str
    dependencies: tuple
    seat_evidence: tuple
    counterevidence: str
    acceptance_criteria: tuple
    synthesis_evidence: dict | None
    verification_receipt: str | None
    risk: str
    rationale: str


@dataclass(frozen=True)
class Ledger:
    """The rows plus the degradation record §10 asks for.

    THE DEGRADATION IS IN THE LEDGER, NOT ONLY IN THE REPORT. §10 says to "say so in the
    report", but it also says from-scratch synthesis "reads only the ledger" — so a degraded
    ledger silently becomes the INPUT to synthesis while §12.2's seam claims assume it is a
    spec. Both the measured size and the threshold applied are recorded, so a later change to
    `DEGRADE_UNION_DIFF_BYTES` cannot reinterpret a ledger written under the old one.
    """
    version: int
    rows: tuple
    union_diff_bytes: int
    degrade_threshold_bytes: int
    degraded: bool


def row_id(requirement_id: str, semantic_claim: str) -> str:
    """§10's content-derived id: `sha256(requirement_id || semantic_claim)[:12]`, FRAMED.

    A bare `||` is ambiguous — `("ab","c")` and `("a","bc")` produce the same bytes — and that
    is not a theoretical collision here: §10's stated reason for content-derived ids is that "a
    round splits or inserts a claim", and splitting a claim is exactly the operation that
    manufactures such a pair. A JSON array is injective because quoting and escaping delimit the
    fields, and it is this repository's existing spelling for a content hash
    (`checks.source_hash`).

    `json.dumps(...).encode()` rather than hand-rolled `.encode("utf-8")` on the raw strings:
    a claim carrying a lone surrogate (possible if it was ever read from a filesystem name)
    raises `UnicodeEncodeError` out of the id function, while `json.dumps` escapes it.

    NO NORMALIZATION. No strip, no casefold, no whitespace collapse, no NFC. A normalization
    rule is a second predicate every implementer and every language reading this file has to
    spell identically — and §10 WANTS an edited claim to be a different claim, because coverage
    compares across review rounds and a shifting id makes the check compare stale identity. A
    round that rephrases a claim INSERTS the new row and resolves the old one (`deferred` or
    `rejected`, with `rationale` naming the successor id); it never mutates text in place.

    `.strip()` is the tempting one, and it is jointly wrong with the equality check in
    `write_ledger`: normalization makes two visibly different claims share a row, and the check
    then passes because both sides normalize.
    """
    canonical = json.dumps([requirement_id, semantic_claim], sort_keys=True).encode()
    return hashlib.sha256(canonical).hexdigest()[:12]


def edges(rows) -> tuple:
    """The ONE directed edge set, with both spellings normalized into it.

    §10's `requires` and `blocks` are THE SAME EDGE IN OPPOSITE DIRECTIONS: "A blocks B" and
    "B requires A" describe one ordering constraint. That is the first real defect in §10's row
    spec, and it is dangerous rather than cosmetic — a cycle checker that walks only `requires`
    misses every cycle a writer expressed with `blocks`, and PASSES.

    Direction: `requires` on row X naming Y is `X -> Y`; `blocks` on row A naming B is `B -> A`,
    which is the same edge `{"id": A, "relation": "requires"}` on row B would produce.

    `conflicts` IS NOT HERE. It is symmetric and not an ordering; a "cycle" of conflicts is
    meaningless. Whether two conflicting rows may both be accepted is a coverage assertion, not
    a sort.
    """
    out = []
    for r in rows:
        for d in r.dependencies:
            if d.relation == "requires":
                out.append((r.id, d.id))
            elif d.relation == "blocks":
                out.append((d.id, r.id))
    return tuple(out)


def topological_order(rows) -> tuple:
    """Kahn's algorithm over `edges`, or a `LedgerError` naming the full cycle as a path.

    ITERATIVE, NEVER A RECURSIVE DFS. Catching `RecursionError` and reporting "no cycle" would
    give a deep chain and a cycle the same outcome.

    THE LENGTH CHECK IS THE POINT. Kahn's produces a PARTIAL order and stops early on a cycle;
    a caller that reads the emitted list without comparing its length to the row count gets a
    silently truncated ordering and synthesizes the partitions it happened to emit — with
    §12.2's "topological ordering is a precondition of partitioned synthesis" satisfied over a
    graph that is not the one written down.
    """
    ids = [r.id for r in rows]
    claims = {r.id: r.semantic_claim for r in rows}
    succ = {i: [] for i in ids}
    indeg = {i: 0 for i in ids}
    for a, b in edges(rows):
        succ[a].append(b)
        indeg[b] += 1
    queue = sorted(i for i in ids if indeg[i] == 0)
    order = []
    while queue:
        n = queue.pop(0)
        order.append(n)
        for m in succ[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                queue.append(m)
        queue.sort()
    if len(order) != len(ids):
        raise LedgerError(
            "these claims form a dependency cycle, so no synthesis order exists: "
            + _render_cycle(succ, {i for i in ids if i not in set(order)}, claims))
    return tuple(order)


def _render_cycle(succ, remaining, claims) -> str:
    """One concrete cycle as `a1b2c3 (claim…) -> d4e5f6 (claim…) -> a1b2c3`."""
    start = sorted(remaining)[0]
    path, seen, node = [], set(), start
    while node not in seen:
        seen.add(node)
        path.append(node)
        nxt = sorted(m for m in succ[node] if m in remaining)
        if not nxt:
            break
        node = nxt[0]
    path.append(node)
    return " -> ".join(f"{n} ({claims.get(n, '')[:_CLAIM_CLIP]})" for n in path)


# Which structured fields each criterion kind REQUIRES. Anything not listed for a kind must be
# None: a criterion carrying a `node_id` under `kind="symbol"` is two evaluators' inputs in one
# record and nothing says which one was meant.
_CRITERION_FIELDS = {
    "test": ("node_id",),
    "symbol": ("path", "symbol"),
    "hash": ("path", "sha256"),
    "schema": (),
    "prose": (),
}
_CRITERION_OPTIONAL = ("path", "symbol", "node_id", "sha256")
# The two kinds with no predicate are the only ones a human trace may hang on. See `Criterion`.
_TRACEABLE = ("prose", "schema")


def _check_criterion(c: Criterion, where: str) -> None:
    if c.kind not in CRITERION_KINDS:
        raise LedgerError(f"{where}: criterion kind is one of {list(CRITERION_KINDS)}, "
                          f"not {c.kind!r}")
    required = _CRITERION_FIELDS[c.kind]
    for name in required:
        if not getattr(c, name):
            raise LedgerError(
                f"{where}: a {c.kind!r} criterion carries {list(required)} as STRUCTURED "
                f"fields and {name!r} is empty. §10.1's worked example is a symbol-presence "
                "check standing in for a behavioural claim while the property is false, so a "
                "criterion that is only a sentence with a symbol name in it is refused.")
    for name in _CRITERION_OPTIONAL:
        if name not in required and getattr(c, name) is not None:
            raise LedgerError(
                f"{where}: a {c.kind!r} criterion may not carry {name!r}; it is another "
                "evaluator's input and nothing would say which one was meant.")
    if c.trace is not None and c.kind not in _TRACEABLE:
        raise LedgerError(
            f"{where}: a trace may hang only on {list(_TRACEABLE)}, not on {c.kind!r}. On a "
            "mechanical kind the predicate is the evidence, and a human note beside it that "
            "could flip the method to `manual_trace_confirmed` is §10.1's manufactured green.")
    if not c.text:
        raise LedgerError(f"{where}: a criterion carries the human sentence it stands for")


def _check(l: Ledger) -> None:
    """Every structural refusal, at WRITE time, where the producer is present to fix it.

    A read-time refusal lands hours later on a resume with nobody able to say which round
    introduced the edge — `write_manifest`'s round-trip refusal makes the same argument.
    """
    if not isinstance(l, Ledger):
        raise LedgerError(f"a Ledger is required, not {type(l).__name__}")
    if l.version != VERSION:
        raise LedgerError(f"this engine writes ledger version {VERSION}, not {l.version!r}")
    if l.degrade_threshold_bytes != DEGRADE_UNION_DIFF_BYTES:
        raise LedgerError(
            f"the recorded degradation threshold is {l.degrade_threshold_bytes} and this "
            f"engine applies {DEGRADE_UNION_DIFF_BYTES}; record the one that was applied")
    if (l.union_diff_bytes > l.degrade_threshold_bytes) != bool(l.degraded):
        raise LedgerError(
            f"this ledger measured a {l.union_diff_bytes}-byte union diff against a "
            f"{l.degrade_threshold_bytes}-byte threshold and records degraded={l.degraded}. "
            "§10 says a degraded ledger must say so — and it becomes the INPUT to from-scratch "
            "synthesis, so a ledger that hides its own degradation misdescribes the spec.")
    seen = {}
    for r in l.rows:
        if not isinstance(r, Row):
            raise LedgerError(f"a ledger row is a Row, not {type(r).__name__}")
        if r.kind not in KINDS:
            raise LedgerError(f"row {r.id}: kind is one of {list(KINDS)}, not {r.kind!r}")
        if r.status not in STATUSES:
            raise LedgerError(f"row {r.id}: status is one of {list(STATUSES)}, "
                              f"not {r.status!r}")
        # THE CHECK THAT KEEPS THE VALUE HONEST. The id IS the hash; without this an editor
        # that changes the claim text and leaves the id is exactly the failure §10 names, and
        # it is invisible — the ledger stays well-formed while coverage keeps comparing a stale
        # identity under a stable-looking key.
        if r.id != row_id(r.requirement_id, r.semantic_claim):
            raise LedgerError(
                f"row {r.id!r} does not hash its own (requirement_id, semantic_claim). §10's "
                "ids are content-derived so coverage can compare across review rounds; a round "
                "that rephrases a claim inserts a NEW row and resolves the old one, never "
                "edits text under a stable key.")
        if r.id in seen:
            raise LedgerError(
                f"row id {r.id!r} appears twice. Merging would make one row where two claims "
                "were required, and coverage would report one covered.")
        seen[r.id] = r
        for e in r.seat_evidence:
            if e.stance not in STANCES:
                raise LedgerError(f"row {r.id}: stance is one of {list(STANCES)}, "
                                  f"not {e.stance!r}")
        for i, c in enumerate(r.acceptance_criteria):
            _check_criterion(c, f"row {r.id} criterion {i}")
    for r in l.rows:
        for d in r.dependencies:
            if d.relation not in RELATIONS:
                raise LedgerError(f"row {r.id}: relation is one of {list(RELATIONS)}, "
                                  f"not {d.relation!r}")
            if d.id not in seen:
                raise LedgerError(
                    f"row {r.id} declares a {d.relation} on {d.id!r}, which no row carries. "
                    "Skipping it would make the sort succeed over a graph missing the "
                    "constraint, and §12.2's precondition would hold over the wrong graph.")
            if d.relation == "conflicts" and d.id == r.id:
                raise LedgerError(f"row {r.id} conflicts with itself, which is not a claim")
    topological_order(l.rows)


def _crit_row(c: Criterion) -> dict:
    return dataclasses.asdict(c)


def _row_row(r: Row) -> dict:
    d = dataclasses.asdict(r)
    d["dependencies"] = [dataclasses.asdict(x) for x in r.dependencies]
    d["seat_evidence"] = [dataclasses.asdict(x) for x in r.seat_evidence]
    d["acceptance_criteria"] = [_crit_row(x) for x in r.acceptance_criteria]
    return d


def _payload(l: Ledger) -> dict:
    return {"version": l.version, "union_diff_bytes": l.union_diff_bytes,
            "degrade_threshold_bytes": l.degrade_threshold_bytes,
            "degraded": bool(l.degraded), "rows": [_row_row(r) for r in l.rows]}


def _sub(cls, row, where):
    names = [f.name for f in fields(cls)]
    if not isinstance(row, dict):
        raise LedgerError(f"{where}: expected an object, not {type(row).__name__}")
    missing = [n for n in names if n not in row]
    unknown = sorted(set(row) - set(names))
    if missing:
        raise LedgerError(f"{where} is missing {missing}")
    if unknown:
        raise LedgerError(f"{where} carries fields this engine does not know: {unknown}")
    return cls(**{n: row[n] for n in names})


def _decode(raw, source) -> Ledger:
    """Missing refused, unknown refused, vocabulary refused — `runstate._decode`'s precedent.

    `rows` DOES NOT DEFAULT TO `[]`. An empty ledger is a run with no claims, which the coverage
    check reports as fully covered; absence must raise.
    """
    if not isinstance(raw, dict):
        raise LedgerError(f"{source}: a ledger is an object, not {type(raw).__name__}")
    names = [f.name for f in fields(Ledger)]
    missing = [n for n in names if n not in raw]
    unknown = sorted(set(raw) - set(names))
    if missing:
        raise LedgerError(f"{source} is missing {missing}")
    if unknown:
        raise LedgerError(f"{source} carries fields this engine does not know: {unknown}")
    rows = []
    for i, r in enumerate(raw["rows"]):
        where = f"{source}: row {i}"
        if not isinstance(r, dict):
            raise LedgerError(f"{where}: expected an object, not {type(r).__name__}")
        body = dict(r)
        body["dependencies"] = tuple(
            _sub(Dependency, d, f"{where} dependency {j}")
            for j, d in enumerate(r.get("dependencies", [])))
        body["seat_evidence"] = tuple(
            _sub(SeatEvidence, e, f"{where} seat_evidence {j}")
            for j, e in enumerate(r.get("seat_evidence", [])))
        body["acceptance_criteria"] = tuple(
            _sub(Criterion, c, f"{where} criterion {j}")
            for j, c in enumerate(r.get("acceptance_criteria", [])))
        rows.append(_sub(Row, body, where))
    l = Ledger(raw["version"], tuple(rows), raw["union_diff_bytes"],
               raw["degrade_threshold_bytes"], raw["degraded"])
    _check(l)
    return l


def ledger_hash(l: Ledger) -> str:
    """The value §14.1 embeds in every checkpoint commit message. A HASH, NEVER A SUMMARY.

    Reviewers run `git log` in the synthesis checkout by design (§13), so a message carrying row
    text would be the ledger handed to a blind reviewer through git. A hash is not content; a
    line quoting a claim is. The whole payload is hashed — including `degraded`, because a
    degraded ledger IS a different spec.
    """
    return hashlib.sha256(
        json.dumps(_payload(l), sort_keys=True).encode()).hexdigest()


def write_ledger(run_dir, l: Ledger) -> None:
    """Publish the ledger, refusing anything structurally dishonest first."""
    _check(l)
    path = storage.ledger_path(run_dir)
    try:
        blob = json.dumps(_payload(l), sort_keys=True, indent=2,
                          allow_nan=False).encode("utf-8") + b"\n"
    except (TypeError, ValueError) as e:
        raise LedgerError(f"this ledger carries a value json cannot serialize: {e}") from e
    restored = _decode(json.loads(blob), path)
    if restored != l:
        differing = [f.name for f in fields(Ledger)
                     if getattr(restored, f.name) != getattr(l, f.name)]
        raise LedgerError(
            f"this ledger does not survive its own round trip; {differing} come back as a "
            "different type. JSON has one sequence type: pass the declared tuples as tuples.")
    storage.atomic_write(path, blob)


def read_ledger(run_dir) -> Ledger:
    """What is currently claimed. Raises if absent — never an empty ledger."""
    path = storage.ledger_path(run_dir)
    try:
        raw = path.read_bytes()
    except FileNotFoundError as e:
        raise LedgerError(
            f"{path} does not exist: this run has recorded no claims. An empty ledger is a run "
            "with no claims, which the coverage check reports as fully covered.") from e
    try:
        return _decode(json.loads(raw), path)
    except ValueError as e:
        raise LedgerError(f"{path} is not readable as JSON: {e}") from e
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uvx --with pytest pytest -q tests/test_forge_ledger.py`
Expected: PASS — 21 passed.

- [ ] **Step 5: Re-run under scrambled names**

Rename every `test_*` to `test_zz0`…`test_zzN` and re-run.

Run: `uvx --with pytest pytest -q tests/test_forge_ledger.py`
Expected: PASS, same count. **Restore the names.**

- [ ] **Step 6: Mutate every new branch**

```bash
scripts/mutate.py --file shared/lib/forge/ledger.py \
  --old '            elif d.relation == "blocks":\n                out.append((d.id, r.id))' \
  --new '            elif d.relation == "blocks":\n                pass' \
  -- uvx --with pytest pytest -q tests/test_forge_ledger.py

scripts/mutate.py --file shared/lib/forge/ledger.py \
  --old '    if len(order) != len(ids):' \
  --new '    if False:' \
  -- uvx --with pytest pytest -q tests/test_forge_ledger.py

scripts/mutate.py --file shared/lib/forge/ledger.py \
  --old '    canonical = json.dumps([requirement_id, semantic_claim], sort_keys=True).encode()' \
  --new '    canonical = (requirement_id + semantic_claim).encode()' \
  -- uvx --with pytest pytest -q tests/test_forge_ledger.py

scripts/mutate.py --file shared/lib/forge/ledger.py \
  --old '        if r.id != row_id(r.requirement_id, r.semantic_claim):' \
  --new '        if False:' \
  -- uvx --with pytest pytest -q tests/test_forge_ledger.py

scripts/mutate.py --file shared/lib/forge/ledger.py \
  --old '            if d.id not in seen:' \
  --new '            if False:' \
  -- uvx --with pytest pytest -q tests/test_forge_ledger.py

scripts/mutate.py --file shared/lib/forge/ledger.py \
  --old '    if c.trace is not None and c.kind not in _TRACEABLE:' \
  --new '    if False:' \
  -- uvx --with pytest pytest -q tests/test_forge_ledger.py
```

Expected: all six exit 0 (CAUGHT). `git status` clean afterwards.

- [ ] **Step 7: Add to the Makefile and commit**

In `Makefile`, append `tests/test_forge_ledger.py` to `FORGE_TESTS`. Then:

```bash
make render
git add shared/lib/forge/ledger.py tests/test_forge_ledger.py Makefile marketplaces
make verify; echo "verify rc=$?"
make precommit; echo "precommit rc=$?"
```

Expected: both `rc=0`. Then:

```bash
git commit -m "$(cat <<'EOF'
feat(forge): §10's ledger, where `blocks` and `requires` become one edge and a cycle is a refusal

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UiV66Pt8cZVMq9t8WEAhpN
EOF
)"
```

---

### Task 4: Coverage — four predicates, and a pair instead of one overloaded enum

**Files:**
- Create: `shared/lib/forge/coverage.py`
- Create: `tests/test_forge_coverage.py`
- Modify: `Makefile` (add `tests/test_forge_coverage.py` to `FORGE_TESTS`)

**Interfaces:**

- **Consumes (Task 3):** `ledger.Ledger`, `ledger.Row`, `ledger.Criterion`, `ledger.CRITERION_KINDS`, `ledger.LedgerError`.
- **Consumes (Plan H):** `snapshot._digest(p)` and `snapshot.Entry`'s kind rules.
- **Produces:**
  ```python
  coverage.CoverageError(RuntimeError)
  coverage.METHODS = ("mechanically_checked", "manual_trace_confirmed", "unresolved")
  coverage.Result(row_id: str, criterion_index: int, method: str,
                  satisfied: bool | None, detail: str)                       # frozen
  coverage.Report(results: tuple[Result, ...], contradictions: tuple[str, ...],
                  unsatisfied: tuple[str, ...], unresolved: tuple[str, ...]) # frozen
  coverage.evaluate(criterion, *, row_id, index, tree, pytest_argv=None,
                    run=subprocess.run) -> Result
  coverage.check(l, *, tree, pytest_argv=None, run=subprocess.run) -> Report
  ```

**THE SPEC AMBIGUITY THIS TASK RESOLVES, and it must be resolved before a line is written.** §10.1's three values (`mechanically_checked` / `manual_trace_confirmed` / `unresolved`) are a **method** axis — *how was this criterion checked*. §12.4 then uses coverage as a fallback trigger on a **result** axis — *a missing accepted row*. **One enum cannot carry both.** "Checked mechanically, and the answer is no" has nowhere to go, and it would land in `unresolved` beside "no evaluator exists for this kind of criterion" — two opposite facts under one word, which is the exact shape of a verdict reading cleaner than its evidence.

**Every coverage result is a pair `(method, satisfied)`.** `method` is §10.1's three-valued enum; `satisfied` is `True | False | None`, and `None` **only** when `method != "mechanically_checked"`. The report line and Plan I₂'s fallback trigger read `satisfied`; the honesty claim reads `method`.

**Per-predicate fail-closed rules, each with the fail-open it forecloses:**

| Kind | Evaluates | Fails closed to | The fail-open it exists to prevent |
|---|---|---|---|
| `test` | The named pytest node id, **selected and observed**, in the verifier tree | collected-count ≠ 1 → `unresolved`; rc 4 → `unresolved`; no pytest runner wired → `unresolved` | **Treating "the run's verify gate exited 0" as satisfying every test-ID criterion.** `verify.Run` holds only `exit_code`/`stdout`/`stderr` — forge has **no per-test result parser anywhere** — so "the suite passed, so the named test passed" is one line of code and is the manufactured green §10.1 exists to forbid. |
| `symbol` | stdlib `ast` over the candidate tree; the dotted name resolved through `ClassDef`/`FunctionDef`/`AsyncFunctionDef`/module-level assignment targets | `SyntaxError` → `unresolved` (never "absent"); non-Python path → `unresolved`; missing file → `(mechanically_checked, False)` | **grep.** `grep -n "def atomic_write"` matches a docstring, a comment, a string literal and prose — and this repository's modules are majority comment by line count, so grep-based symbol checking here is *more* wrong than average. A grep fallback would have to be reported `unresolved`, not as a check. |
| `hash` | sha256 recomputed over the path in the candidate tree | missing path → `(mechanically_checked, False)`; special file where a regular one was expected → `(mechanically_checked, False)`; `OSError` → `unresolved` | **Hashing through a symlink** (`open()` follows it), so the invariant describes content from outside the tree the ledger claims to describe. `fleet.clone_seat` already learned this; do not re-learn it. |
| `schema` | Nothing. There is no database and no schema in this repository, and no evaluator should be invented for one. | always `("unresolved", None)`, reason `"no schema evaluator is wired in this repository"` | **Falling through to `manual_trace_confirmed`**, which asserts a human traced it — an unwritten evaluator producing a value that reads as human diligence. `unresolved` is the honest word for "nobody looked". |
| `prose` | The recorded human trace, if any | no trace → `("unresolved", None)` | Calling a natural-language row "checked". §10.1: *a generic walk over natural-language rows is systematic review, not deterministic coverage.* |

**What `mechanically_checked` does NOT prove, and the report line must say so:** that the test tests the claim. A test passing for the wrong reason is this project's defect pattern #1 and no predicate reaches it. `mechanically_checked` is a claim about *mechanism*, not correctness.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_forge_coverage.py`:

```python
"""§10.1: only criteria with a real predicate are mechanically checked."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

import hashlib  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402
import pytest  # noqa: E402
from forge import coverage, ledger  # noqa: E402


def _crit(**kw):
    base = dict(kind="prose", text="it works", path=None, symbol=None,
                node_id=None, sha256=None, trace=None)
    base.update(kw)
    return ledger.Criterion(**base)


def _fake_run(script):
    """A pytest stand-in. NO TEST HERE RUNS A REAL PROVIDER OR A REAL SUITE."""
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        rc, out = script(argv)
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")
    run.calls = calls
    return run


def test_a_named_test_that_passes_is_mechanically_checked_and_satisfied(tmp_path):
    def script(argv):
        if "--collect-only" in argv:
            return 0, "tests/t.py::test_x\n"
        return 0, "1 passed"
    r = coverage.evaluate(_crit(kind="test", node_id="tests/t.py::test_x"),
                          row_id="a1", index=0, tree=tmp_path,
                          pytest_argv=["pytest"], run=_fake_run(script))
    assert (r.method, r.satisfied) == ("mechanically_checked", True)


def test_a_named_test_that_fails_is_mechanically_checked_and_not_satisfied(tmp_path):
    def script(argv):
        if "--collect-only" in argv:
            return 0, "tests/t.py::test_x\n"
        return 1, "1 failed"
    r = coverage.evaluate(_crit(kind="test", node_id="tests/t.py::test_x"),
                          row_id="a1", index=0, tree=tmp_path,
                          pytest_argv=["pytest"], run=_fake_run(script))
    assert (r.method, r.satisfied) == ("mechanically_checked", False), \
        "'checked mechanically, and the answer is no' must not land in `unresolved`"


def test_a_vanished_test_is_unresolved_not_failed(tmp_path):
    """A claim whose test vanished is not a claim that was tested."""
    def script(argv):
        return 5, ""
    r = coverage.evaluate(_crit(kind="test", node_id="tests/t.py::test_gone"),
                          row_id="a1", index=0, tree=tmp_path,
                          pytest_argv=["pytest"], run=_fake_run(script))
    assert (r.method, r.satisfied) == ("unresolved", None)


def test_a_node_id_selecting_more_than_one_test_is_unresolved(tmp_path):
    """The predicate must SELECT the named test and observe that one result — not read a
    green suite and infer it."""
    def script(argv):
        if "--collect-only" in argv:
            return 0, "tests/t.py::test_x\ntests/t.py::test_y\n"
        return 0, "2 passed"
    r = coverage.evaluate(_crit(kind="test", node_id="tests/t.py"),
                          row_id="a1", index=0, tree=tmp_path,
                          pytest_argv=["pytest"], run=_fake_run(script))
    assert (r.method, r.satisfied) == ("unresolved", None)


def test_a_usage_error_is_unresolved(tmp_path):
    r = coverage.evaluate(_crit(kind="test", node_id="tests/t.py::test_x"),
                          row_id="a1", index=0, tree=tmp_path,
                          pytest_argv=["pytest"],
                          run=_fake_run(lambda argv: (4, "usage error")))
    assert (r.method, r.satisfied) == ("unresolved", None)


def test_no_pytest_runner_is_unresolved_never_manual_trace(tmp_path):
    r = coverage.evaluate(_crit(kind="test", node_id="tests/t.py::test_x"),
                          row_id="a1", index=0, tree=tmp_path, pytest_argv=None)
    assert (r.method, r.satisfied) == ("unresolved", None)


def test_the_test_predicate_runs_in_the_candidate_tree(tmp_path):
    """Never the seat, never the user's checkout."""
    seen = {}

    def run(argv, **kw):
        seen["cwd"] = kw.get("cwd")
        return subprocess.CompletedProcess(argv, 0, stdout="tests/t.py::test_x\n", stderr="")
    coverage.evaluate(_crit(kind="test", node_id="tests/t.py::test_x"),
                      row_id="a1", index=0, tree=tmp_path, pytest_argv=["pytest"], run=run)
    assert Path(seen["cwd"]) == tmp_path


def test_a_symbol_that_exists_is_found_by_ast_not_by_grep(tmp_path):
    """grep matches a docstring, a comment and a string literal. This repository's modules
    are majority comment by line count."""
    (tmp_path / "m.py").write_text(
        '"""This module mentions def ghost_function in prose."""\n'
        "GHOST = 1\n\n\nclass C:\n    def method(self):\n        pass\n\n\n"
        "def real_function():\n    return 1\n")
    for sym, want in (("real_function", True), ("GHOST", True), ("C", True),
                      ("ghost_function", False)):
        r = coverage.evaluate(_crit(kind="symbol", text="m.py defines it",
                                    path="m.py", symbol=sym),
                              row_id="a1", index=0, tree=tmp_path)
        assert (r.method, r.satisfied) == ("mechanically_checked", want), sym


def test_a_dotted_symbol_resolves_through_a_class(tmp_path):
    (tmp_path / "m.py").write_text("class C:\n    def method(self):\n        pass\n")
    r = coverage.evaluate(_crit(kind="symbol", text="m.py defines C.method",
                                path="m.py", symbol="C.method"),
                          row_id="a1", index=0, tree=tmp_path)
    assert (r.method, r.satisfied) == ("mechanically_checked", True)
    r2 = coverage.evaluate(_crit(kind="symbol", text="m.py defines C.absent",
                                 path="m.py", symbol="C.absent"),
                           row_id="a1", index=0, tree=tmp_path)
    assert (r2.method, r2.satisfied) == ("mechanically_checked", False)


def test_a_syntax_error_is_unresolved_never_symbol_absent(tmp_path):
    (tmp_path / "m.py").write_text("def broken(:\n")
    r = coverage.evaluate(_crit(kind="symbol", text="m.py defines x", path="m.py", symbol="x"),
                          row_id="a1", index=0, tree=tmp_path)
    assert (r.method, r.satisfied) == ("unresolved", None)


def test_a_missing_file_definitively_does_not_define_the_symbol(tmp_path):
    r = coverage.evaluate(_crit(kind="symbol", text="gone.py defines x",
                                path="gone.py", symbol="x"),
                          row_id="a1", index=0, tree=tmp_path)
    assert (r.method, r.satisfied) == ("mechanically_checked", False)


def test_a_non_python_path_has_no_symbol_evaluator(tmp_path):
    (tmp_path / "m.txt").write_text("x = 1\n")
    r = coverage.evaluate(_crit(kind="symbol", text="m.txt defines x",
                                path="m.txt", symbol="x"),
                          row_id="a1", index=0, tree=tmp_path)
    assert (r.method, r.satisfied) == ("unresolved", None)


def test_a_hash_invariant_is_recomputed(tmp_path):
    (tmp_path / "f.bin").write_bytes(b"payload")
    digest = hashlib.sha256(b"payload").hexdigest()
    r = coverage.evaluate(_crit(kind="hash", text="f.bin is unchanged",
                                path="f.bin", sha256=digest),
                          row_id="a1", index=0, tree=tmp_path)
    assert (r.method, r.satisfied) == ("mechanically_checked", True)
    r2 = coverage.evaluate(_crit(kind="hash", text="f.bin is unchanged",
                                 path="f.bin", sha256="0" * 64),
                           row_id="a1", index=0, tree=tmp_path)
    assert (r2.method, r2.satisfied) == ("mechanically_checked", False)


def test_a_symlink_is_its_target_text_and_is_never_hashed_through(tmp_path):
    """`open()` follows a link, so the invariant would describe content from OUTSIDE the
    tree the ledger claims to describe."""
    (tmp_path / "real.txt").write_bytes(b"payload")
    os.symlink("real.txt", tmp_path / "alias.txt")
    through = hashlib.sha256(b"payload").hexdigest()
    target = hashlib.sha256(b"real.txt").hexdigest()
    r = coverage.evaluate(_crit(kind="hash", text="alias.txt", path="alias.txt",
                                sha256=through),
                          row_id="a1", index=0, tree=tmp_path)
    assert r.satisfied is False, "hashing through the link is the fail-open"
    r2 = coverage.evaluate(_crit(kind="hash", text="alias.txt", path="alias.txt",
                                 sha256=target),
                           row_id="a1", index=0, tree=tmp_path)
    assert (r2.method, r2.satisfied) == ("mechanically_checked", True)


def test_a_missing_path_definitively_fails_a_hash_invariant(tmp_path):
    r = coverage.evaluate(_crit(kind="hash", text="gone", path="gone.bin", sha256="0" * 64),
                          row_id="a1", index=0, tree=tmp_path)
    assert (r.method, r.satisfied) == ("mechanically_checked", False)


def test_a_special_file_fails_a_hash_invariant_without_being_opened(tmp_path):
    os.mkfifo(tmp_path / "pipe")
    r = coverage.evaluate(_crit(kind="hash", text="pipe", path="pipe", sha256="0" * 64),
                          row_id="a1", index=0, tree=tmp_path)
    assert (r.method, r.satisfied) == ("mechanically_checked", False)


def test_there_is_no_schema_evaluator_and_it_says_so(tmp_path):
    r = coverage.evaluate(_crit(kind="schema", text="the users table has a seq column"),
                          row_id="a1", index=0, tree=tmp_path)
    assert (r.method, r.satisfied) == ("unresolved", None)
    assert "no schema evaluator" in r.detail


def test_prose_with_a_trace_is_manual_and_without_one_is_unresolved(tmp_path):
    traced = coverage.evaluate(_crit(kind="prose", text="stable seams exist",
                                     trace="walked both partitions by hand"),
                               row_id="a1", index=0, tree=tmp_path)
    assert (traced.method, traced.satisfied) == ("manual_trace_confirmed", None)
    bare = coverage.evaluate(_crit(kind="prose", text="stable seams exist"),
                             row_id="a1", index=0, tree=tmp_path)
    assert (bare.method, bare.satisfied) == ("unresolved", None)


def test_satisfied_is_none_whenever_the_method_is_not_mechanical(tmp_path):
    for c in (_crit(kind="prose", text="x", trace="t"),
              _crit(kind="schema", text="x"),
              _crit(kind="prose", text="x")):
        r = coverage.evaluate(c, row_id="a1", index=0, tree=tmp_path)
        assert r.satisfied is None


def _row(rid, claim, status="accepted", **kw):
    base = dict(id=ledger.row_id(rid, claim), requirement_id=rid,
                requirement_span="s:1", requirement_sha256="0" * 64, kind="behavior",
                component="c", semantic_claim=claim, status=status, dependencies=(),
                seat_evidence=(), counterevidence="", acceptance_criteria=(_crit(),),
                synthesis_evidence=None, verification_receipt=None, risk="low", rationale="")
    base.update(kw)
    return ledger.Row(**base)


def _led(rows):
    return ledger.Ledger(ledger.VERSION, tuple(rows), 10,
                         ledger.DEGRADE_UNION_DIFF_BYTES, False)


def test_two_conflicting_rows_both_accepted_is_a_contradiction(tmp_path):
    a = _row("R1", "alpha")
    b = _row("R2", "beta")
    a2 = ledger.Row(**{**a.__dict__, "dependencies": (ledger.Dependency(b.id, "conflicts"),)})
    rep = coverage.check(_led([a2, b]), tree=tmp_path)
    assert any("conflict" in c for c in rep.contradictions)


def test_an_accepted_row_over_a_unanimous_rejection_is_a_contradiction(tmp_path):
    """§10: if all three seats considered and rejected a cache layer, that is the most
    valuable signal in the run — and from-scratch synthesis would otherwise add it back."""
    ev = tuple(ledger.SeatEvidence(s, "contradicts", "no", None)
               for s in ("claude", "codex", "agy"))
    rep = coverage.check(_led([_row("R1", "add a cache layer", seat_evidence=ev)]),
                         tree=tmp_path)
    assert any("unanimous" in c for c in rep.contradictions)


def test_a_silent_seat_is_not_a_rejection(tmp_path):
    """Non-vacuity: a seat that said nothing did not reject."""
    ev = (ledger.SeatEvidence("claude", "contradicts", "no", None),
          ledger.SeatEvidence("codex", "contradicts", "no", None),
          ledger.SeatEvidence("agy", "silent", "", None))
    rep = coverage.check(_led([_row("R1", "add a cache layer", seat_evidence=ev)]),
                         tree=tmp_path)
    assert not any("unanimous" in c for c in rep.contradictions)


def test_the_report_separates_unsatisfied_from_unresolved(tmp_path):
    """The two axes, kept apart: 'checked and false' and 'nobody could check' are opposite
    facts, and §12.4 acts on only one of them."""
    (tmp_path / "m.py").write_text("x = 1\n")
    row = _row("R1", "alpha", acceptance_criteria=(
        _crit(kind="symbol", text="m.py defines gone", path="m.py", symbol="gone"),
        _crit(kind="schema", text="a table exists")))
    rep = coverage.check(_led([row]), tree=tmp_path)
    assert len(rep.unsatisfied) == 1 and len(rep.unresolved) == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uvx --with pytest pytest -q tests/test_forge_coverage.py`
Expected: FAIL — `ImportError: cannot import name 'coverage' from 'forge'`.

- [ ] **Step 3: Write `coverage.py`**

Create `shared/lib/forge/coverage.py`:

```python
"""§10.1: coverage is only mechanical where a predicate exists.

A row reading "crash-safe atomic state update" is marked present because `os.replace`
appears, while `fsync` of the file and its directory is missing and the property is false. A
generic walk over natural-language rows is SYSTEMATIC REVIEW, NOT DETERMINISTIC COVERAGE —
calling it mechanical manufactures another false green. (That particular example is not live
in this tree: `storage._fsync_dir` and `storage.atomic_write` do fsync both file and
directory. It is illustrative, and the shape it illustrates is the whole point of this file.)

TWO AXES, NOT ONE ENUM. §10.1's three values are a METHOD axis — how was this criterion
checked. §12.4 then uses coverage as a fallback trigger on a RESULT axis — a missing accepted
row. One enum cannot carry both: "checked mechanically, and the answer is no" has nowhere to
go, and it lands in `unresolved` beside "no evaluator exists for this kind of criterion" —
two OPPOSITE facts under one word, which is exactly a verdict reading cleaner than its
evidence. So every result is the pair `(method, satisfied)`, with `satisfied` non-None only
when `method == "mechanically_checked"`.

WHAT `mechanically_checked` DOES NOT PROVE, and every report line must say so: that the test
tests the claim. A test passing for the wrong reason is this project's first recurring defect
and no predicate here reaches it. This is a claim about MECHANISM, never about correctness.

WHERE THE PREDICATES RUN: the candidate tree handed in as `tree` — the verifier's clone.
Never the seat (the builder wrote it) and never the user's checkout.

NOTHING HERE SPENDS A PROVIDER CALL. `run` is injected and defaults to `subprocess.run`; the
suite passes fakes.
"""
import ast
import hashlib
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import snapshot

METHODS = ("mechanically_checked", "manual_trace_confirmed", "unresolved")

# A predicate is bounded. A criterion whose test hangs must not hang the coverage check.
_TEST_TIMEOUT = 600


class CoverageError(RuntimeError):
    """This coverage check cannot be described honestly."""


@dataclass(frozen=True)
class Result:
    """One criterion's outcome, on both axes.

    `satisfied` is None whenever `method != "mechanically_checked"`, and that is an invariant
    the constructor below enforces rather than a convention: a `manual_trace_confirmed` result
    carrying `satisfied=True` would be a human's word rendered in the shape of a measurement.
    """
    row_id: str
    criterion_index: int
    method: str
    satisfied: bool | None
    detail: str


@dataclass(frozen=True)
class Report:
    """Every result, plus the three roll-ups §12.4 and §10 read.

    `unsatisfied` and `unresolved` are SEPARATE and that separation is the file's reason for
    existing: "checked and false" is a fallback trigger, "nobody could check" is a report line
    that must not be read as either a pass or a failure.
    """
    results: tuple
    contradictions: tuple
    unsatisfied: tuple
    unresolved: tuple


def _result(row_id, index, method, satisfied, detail) -> Result:
    if method not in METHODS:
        raise CoverageError(f"method is one of {list(METHODS)}, not {method!r}")
    if method != "mechanically_checked" and satisfied is not None:
        raise CoverageError(
            f"a {method!r} result may not carry satisfied={satisfied!r}: only a mechanical "
            "check produces an answer, and a human's word in a measurement's shape is the "
            "manufactured green §10.1 exists to forbid")
    return Result(row_id, index, method, satisfied, detail)


def _test(c, *, row_id, index, tree, pytest_argv, run) -> Result:
    """Run the named pytest node id and observe THAT ONE RESULT.

    THE FAIL-OPEN THIS FORECLOSES, and it is the obvious one: treating "the run's verify gate
    exited 0" as satisfying every test-ID criterion. `verify.Run` holds only `exit_code`,
    `stdout` and `stderr` — forge has NO per-test result parser anywhere — so "the suite passed,
    so the named test passed" is one line of code and is a manufactured green. The predicate
    must SELECT the named test and watch it.

    `--collect-only` first, and exactly one collected node required. A node id naming a FILE
    selects many tests, and a green run over many says nothing about the one the claim names.

    Pytest's exits carry the distinction: 0 passed, 1 failed, 4 usage error, 5 nothing
    collected. 5 is `unresolved`, never "checked and failed" — a claim whose test vanished is
    not a claim that was tested.
    """
    if not pytest_argv:
        return _result(row_id, index, "unresolved", None,
                       "no pytest runner is wired for this run, so no test-ID predicate exists")
    common = ["-p", "no:cacheprovider", "--no-header", "-q"]
    try:
        collected = run([*pytest_argv, "--collect-only", *common, c.node_id],
                        cwd=str(tree), capture_output=True, text=True, timeout=_TEST_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        return _result(row_id, index, "unresolved", None, f"pytest could not be run: {e}")
    if collected.returncode == 5:
        return _result(row_id, index, "unresolved", None,
                       f"{c.node_id!r} collected no tests in this tree: the claim's test is "
                       "absent, which is not the same as the claim being false")
    if collected.returncode != 0:
        return _result(row_id, index, "unresolved", None,
                       f"pytest --collect-only exited {collected.returncode} for {c.node_id!r}")
    names = [ln.strip() for ln in (collected.stdout or "").splitlines()
             if ln.strip().startswith(c.node_id.split("::")[0])]
    if len(names) != 1 or names[0] != c.node_id:
        return _result(row_id, index, "unresolved", None,
                       f"{c.node_id!r} selected {len(names)} tests; a predicate must observe "
                       "the named test's own result, not a green run over several")
    try:
        r = run([*pytest_argv, *common, c.node_id], cwd=str(tree),
                capture_output=True, text=True, timeout=_TEST_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        return _result(row_id, index, "unresolved", None, f"pytest could not be run: {e}")
    if r.returncode == 0:
        return _result(row_id, index, "mechanically_checked", True,
                       f"{c.node_id} passed. THIS DOES NOT PROVE THE TEST TESTS THE CLAIM: "
                       "mechanically_checked is a claim about mechanism, not correctness.")
    if r.returncode == 1:
        return _result(row_id, index, "mechanically_checked", False, f"{c.node_id} failed")
    return _result(row_id, index, "unresolved", None,
                   f"pytest exited {r.returncode} for {c.node_id!r}, which is neither a pass "
                   "nor a failure of the named test")


def _defines(tree_node, dotted: str) -> bool:
    """Resolve a dotted name through the module's own definitions. AST, never grep.

    `grep -n "def atomic_write"` matches a docstring, a comment, a string literal and a
    reference in prose — and this repository's modules are MAJORITY COMMENT by line count, so
    a grep-based symbol check here is more wrong than average, not less.
    """
    parts = dotted.split(".")
    body = tree_node.body
    for i, part in enumerate(parts):
        found = None
        for node in body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name == part:
                found = node
                break
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == part for t in node.targets):
                found = node
                break
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                    and node.target.id == part:
                found = node
                break
        if found is None:
            return False
        if i == len(parts) - 1:
            return True
        if not isinstance(found, ast.ClassDef):
            return False
        body = found.body
    return False


def _symbol(c, *, row_id, index, tree) -> Result:
    """`path` defines `symbol`, by parse.

    THE LARGER FAIL-OPEN, AND THE MOST IMPORTANT RULE IN THIS FILE: an exact-symbol predicate
    proves a SYMBOL EXISTS. §10.1's own worked example is a symbol-presence check standing in
    for a behavioural claim while the property is false. So this may only ever be attached to
    a criterion PHRASED as "P defines S" — which is why `ledger.Criterion` refuses a `symbol`
    criterion that does not carry `path` and `symbol` as structured fields. The rule lives in
    the decoder because a rule stated only here is one the next author will not meet.

    A `SyntaxError` is `unresolved`, NEVER "symbol absent": an unparseable file is one nobody
    could look in, and reporting absence would be a measurement nobody took.
    """
    p = Path(tree) / c.path
    if p.suffix != ".py":
        return _result(row_id, index, "unresolved", None,
                       f"{c.path!r} is not Python; no symbol evaluator is wired for it")
    try:
        src = p.read_bytes()
    except FileNotFoundError:
        return _result(row_id, index, "mechanically_checked", False,
                       f"{c.path!r} does not exist, so it does not define {c.symbol!r}")
    except OSError as e:
        return _result(row_id, index, "unresolved", None, f"{c.path!r} could not be read: {e}")
    try:
        parsed = ast.parse(src, filename=str(p))
    except SyntaxError as e:
        return _result(row_id, index, "unresolved", None,
                       f"{c.path!r} does not parse ({e}); nobody could look inside it, which "
                       "is not the same as the symbol being absent")
    ok = _defines(parsed, c.symbol)
    return _result(row_id, index, "mechanically_checked", ok,
                   f"{c.path} {'defines' if ok else 'does not define'} {c.symbol}")


def _hash(c, *, row_id, index, tree) -> Result:
    """Recompute sha256 over the path and compare.

    `snapshot`'s kind rules, reused rather than re-spelled: a SYMLINK is the sha256 of its
    TARGET TEXT with surrogateescape, never hashed through. `open()` follows a link, so hashing
    through one makes the invariant describe content from OUTSIDE the tree the ledger claims to
    describe — `fleet.clone_seat` already learned this; do not re-learn it.

    A missing path and a special file where a regular one was expected are both
    `(mechanically_checked, False)`: the invariant is definitively unsatisfied. An `OSError`
    reading a regular file is `unresolved` — nobody managed to look.
    """
    p = Path(tree) / c.path
    try:
        st = p.lstat()
    except FileNotFoundError:
        return _result(row_id, index, "mechanically_checked", False,
                       f"{c.path!r} does not exist, so the invariant is unsatisfied")
    except OSError as e:
        return _result(row_id, index, "unresolved", None, f"{c.path!r} could not be stat'd: {e}")
    if stat.S_ISLNK(st.st_mode):
        got = hashlib.sha256(
            os.readlink(p).encode("utf-8", "surrogateescape")).hexdigest()
    elif not stat.S_ISREG(st.st_mode):
        return _result(row_id, index, "mechanically_checked", False,
                       f"{c.path!r} is a special file, not the regular file the invariant "
                       "describes. It was NOT opened.")
    else:
        try:
            got = snapshot._digest(p)
        except OSError as e:
            return _result(row_id, index, "unresolved", None,
                           f"{c.path!r} could not be read: {e}")
    ok = got == c.sha256
    return _result(row_id, index, "mechanically_checked", ok,
                   f"{c.path}: {got} {'==' if ok else '!='} {c.sha256}")


def evaluate(criterion, *, row_id, index, tree, pytest_argv=None, run=subprocess.run) -> Result:
    """One criterion's `(method, satisfied)` pair.

    `schema` has NO EVALUATOR AND SAYS SO. There is no database and no schema in this
    repository and none should be invented for one; the enum value exists because §10.1 names
    it and a future repository will have one. It must NOT fall through to
    `manual_trace_confirmed`, which asserts a human traced it — an unwritten evaluator would
    then produce a value that reads as human diligence. `unresolved` is the honest word for
    "nobody looked".
    """
    k = criterion.kind
    if k == "test":
        return _test(criterion, row_id=row_id, index=index, tree=tree,
                     pytest_argv=pytest_argv, run=run)
    if k == "symbol":
        return _symbol(criterion, row_id=row_id, index=index, tree=tree)
    if k == "hash":
        return _hash(criterion, row_id=row_id, index=index, tree=tree)
    if k == "schema":
        return _result(row_id, index, "unresolved", None,
                       "no schema evaluator is wired in this repository; nobody looked")
    if k == "prose":
        if criterion.trace:
            return _result(row_id, index, "manual_trace_confirmed", None, criterion.trace)
        return _result(row_id, index, "unresolved", None,
                       "a natural-language criterion with no recorded trace: a generic walk "
                       "over it is systematic review, not deterministic coverage")
    raise CoverageError(f"no evaluator is declared for criterion kind {k!r}")


def _contradictions(l) -> tuple:
    """The two contradictions a machine CAN find, and a note about the one it cannot.

    (a) TWO ROWS THAT CONFLICT MAY NOT BOTH BE ACCEPTED. This is the mechanical reading of
    §10's "coverage asserts no accepted row contradicts a unanimous rejection", and it is a
    REPORT FINDING rather than a write refusal: §12.4 makes the coverage check "a fallback
    trigger AND a report line", and a write refusal can be neither.

    (b) AN ACCEPTED ROW WHOSE EVERY SEAT REJECTED IT. "Unanimous" requires at least two seats
    and NO `silent` one — a seat that said nothing did not reject. §10's example is the whole
    reason: if all three seats considered and rejected a cache layer, that is the most valuable
    signal in the run, and from-scratch synthesis (which reads only the ledger) would otherwise
    add it straight back.

    WHAT IS NOT REACHABLE HERE, stated so nobody reads this as complete: whether an accepted
    row's CONTENT contradicts a different, unanimously-rejected row is a semantic comparison no
    predicate can make. §10.1's own rule applies to this file too.
    """
    out = []
    status = {r.id: r.status for r in l.rows}
    claim = {r.id: r.semantic_claim for r in l.rows}
    pairs = set()
    for r in l.rows:
        for d in r.dependencies:
            if d.relation == "conflicts":
                pairs.add(tuple(sorted((r.id, d.id))))
    for a, b in sorted(pairs):
        if status.get(a) == "accepted" and status.get(b) == "accepted":
            out.append(f"{a} and {b} conflict and are both accepted: "
                       f"{claim.get(a, '')!r} vs {claim.get(b, '')!r}")
    for r in l.rows:
        if r.status != "accepted" or len(r.seat_evidence) < 2:
            continue
        if all(e.stance == "contradicts" for e in r.seat_evidence):
            out.append(f"{r.id} is accepted over a unanimous rejection by "
                       f"{[e.seat for e in r.seat_evidence]}: {r.semantic_claim!r}")
    return tuple(out)


def check(l, *, tree, pytest_argv=None, run=subprocess.run) -> Report:
    """Every criterion on every row, plus the contradictions and the two roll-ups."""
    results = []
    for r in l.rows:
        for i, c in enumerate(r.acceptance_criteria):
            results.append(evaluate(c, row_id=r.id, index=i, tree=tree,
                                    pytest_argv=pytest_argv, run=run))
    unsatisfied = tuple(f"{x.row_id}[{x.criterion_index}]: {x.detail}"
                        for x in results if x.satisfied is False)
    unresolved = tuple(f"{x.row_id}[{x.criterion_index}]: {x.detail}"
                       for x in results if x.method == "unresolved")
    return Report(tuple(results), _contradictions(l), unsatisfied, unresolved)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uvx --with pytest pytest -q tests/test_forge_coverage.py`
Expected: PASS — 22 passed.

- [ ] **Step 5: Re-run under scrambled names, then mutate**

Rename every `test_*` to `test_zz0`…`test_zzN`, re-run, confirm the same count, **restore the names**. Then:

```bash
scripts/mutate.py --file shared/lib/forge/coverage.py \
  --old '    if collected.returncode == 5:' \
  --new '    if False:' \
  -- uvx --with pytest pytest -q tests/test_forge_coverage.py

scripts/mutate.py --file shared/lib/forge/coverage.py \
  --old '    if len(names) != 1 or names[0] != c.node_id:' \
  --new '    if False:' \
  -- uvx --with pytest pytest -q tests/test_forge_coverage.py

scripts/mutate.py --file shared/lib/forge/coverage.py \
  --old '    if stat.S_ISLNK(st.st_mode):' \
  --new '    if False:' \
  -- uvx --with pytest pytest -q tests/test_forge_coverage.py

scripts/mutate.py --file shared/lib/forge/coverage.py \
  --old '        return _result(row_id, index, "unresolved", None,\n                       f"{c.path!r} does not parse ({e}); nobody could look inside it, which "\n                       "is not the same as the symbol being absent")' \
  --new '        return _result(row_id, index, "mechanically_checked", False, str(e))' \
  -- uvx --with pytest pytest -q tests/test_forge_coverage.py

scripts/mutate.py --file shared/lib/forge/coverage.py \
  --old '        if all(e.stance == "contradicts" for e in r.seat_evidence):' \
  --new '        if any(e.stance == "contradicts" for e in r.seat_evidence):' \
  -- uvx --with pytest pytest -q tests/test_forge_coverage.py

scripts/mutate.py --file shared/lib/forge/coverage.py \
  --old '    if method != "mechanically_checked" and satisfied is not None:' \
  --new '    if False:' \
  -- uvx --with pytest pytest -q tests/test_forge_coverage.py
```

Expected: all six exit 0 (CAUGHT). `git status` clean afterwards.

- [ ] **Step 6: Add to the Makefile and commit**

Append `tests/test_forge_coverage.py` to `FORGE_TESTS`, then:

```bash
make render
git add shared/lib/forge/coverage.py tests/test_forge_coverage.py Makefile marketplaces
make verify; echo "verify rc=$?"
make precommit; echo "precommit rc=$?"
```

Expected: both `rc=0`. Then:

```bash
git commit -m "$(cat <<'EOF'
feat(forge): coverage answers on two axes, so "checked and false" stops sharing a word with "nobody looked"

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UiV66Pt8cZVMq9t8WEAhpN
EOF
)"
```

---

### Task 5: `PromptIdentity` — four values, and a `None` that never agrees with a `None`

**Files:**
- Create: `shared/lib/forge/fingerprint.py`
- Create: `tests/test_forge_fingerprint.py`
- Modify: `shared/lib/forge/runner.py` (`_rationale` delegates to `fingerprint.without_engine_text`)
- Modify: `tests/test_forge_seams.py` (one new seam)
- Modify: `Makefile`

**Interfaces:**

- **Consumes (Task 2):** `taskbundle.installed_closure(cli) -> str | None`.
- **Consumes (Plan H):** `council.engine.SENTINEL_NOTE`, `engine.make_sentinel()`, `engine.ProviderSpec`, `runner._rationale(answer, token)`.
- **Produces:**
  ```python
  fingerprint.FingerprintError(RuntimeError)
  fingerprint.LABELS = ("identically-prompted", "differently-prompted", "not-comparable")
  fingerprint.PromptIdentity(prompt_sha256: str, semantic_sha256: str,
                             bundle_sha256: str | None, cli_path: str | None,
                             cli_version: str | None, model_requested: str | None,
                             model_reported: str | None,
                             plugin_closure_sha256: str | None)            # frozen
  fingerprint.without_engine_text(text: str, token: str) -> str
  fingerprint.prompt_hashes(prompt: str, token: str) -> tuple[str, str]
  fingerprint.probe_cli(name: str, *, run=subprocess.run) -> tuple[str | None, str | None]
  fingerprint.build(*, prompt, token, cli, bundle_sha256=None, model_requested=None,
                    model_reported=None, run=subprocess.run,
                    closure=taskbundle.installed_closure) -> PromptIdentity
  fingerprint.as_row(pi) -> dict
  fingerprint.from_row(row: dict) -> PromptIdentity
  fingerprint.agreement_label(ids) -> str
  fingerprint.creditable(label: str) -> bool
  ```

**The name.** Not `identity`. `fleet.clone_seat(..., identity=…)` (`fleet.py:117`) and `runner.run_seat(..., identity=…)` (`runner.py:460`) already mean the git author `(name, email)` pair, refused when either half is empty because a seat that cannot commit is unusable. Reusing the word is how the two get conflated in a review six weeks from now.

**Why the nonce-stripped hash is the only one that can answer §11's question.** `engine.make_sentinel` is called **per seat, per attempt** (`runner.py:526`), not once per run — so the three seats' *exact* prompt hashes can never match, by construction. Any code that compares exact hashes to decide "identically prompted" answers "differently prompted" 100% of the time and labels every agreement weaker. The semantic hash is the comparison that means something. **The exact hash is still recorded** — it is the provenance of what was actually sent.

**And the strip order is load-bearing.** `engine.SENTINEL_NOTE` is ~280 characters of engine text *containing the token*. Strip the token first and the note no longer matches itself — measured and documented at `runner.py:258-267`. The note is removed **first**, then the bare token, case-insensitively (because `seat.read_proof` folds case when it looks for the same token; if a differently-cased echo counts as proof it has to count as engine text here too). **This is `runner._rationale`'s existing recipe, and this task factors it out rather than re-spelling it** — two spellings of one predicate eventually disagree.

**`inventory.version` is NOT reused, and this is the one that will be got wrong.** `scripts/lib/inventory.py:55-65` ignores the return code, falls back from stdout to stderr, swallows every exception into the string `"(unavailable: …)"`, and returns `"(unknown)"` for empty output. **Those are strings.** Two seats whose versions could not be read both record `"(unknown)"` and **compare equal** — an unread version manufacturing an identity match, which is precisely §11's "agreement is provenance" being fabricated. Record `str | None`, `None` on rc≠0 / timeout / `FileNotFoundError`. `seat.read_proof`'s "a missing token fails closed: returns False, not True" is the precedent to name.

*(Measured on this machine, 2026-08-03: `claude --version` → `2.1.220 (Claude Code)`, `codex --version` → `codex-cli 0.145.0`, `agy --version` → `1.1.9`, all rc 0. This contradicts `scripts/lib/inventory.py:48`, which routes agy through `agy changelog` because `--version` was believed absent.)*

**Model is two fields, not one.** `ProviderSpec.model` is what forge *requested*. `model_reported` comes from the provider's own envelope where one exists (`extract_claude_json`, `extract_agy_json`) and is `None` where it does not. Collapsing them records a request as an observation.

**Three-state comparison.** `agreement_label` returns `not-comparable` when any compared value is `None`, and `creditable` is True only for `identically-prompted`. "We could not tell" must not be spelled the same way as "no" — but neither may it credit agreement. `bundle.CandidateBundle.gate_delta`'s three-state rule is the in-repo precedent: `None` is "nobody looked" and a consumer must treat it as UNKNOWN.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_forge_fingerprint.py`:

```python
"""§11: agreement is provenance, never a correctness argument."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

import hashlib  # noqa: E402
import subprocess  # noqa: E402
import pytest  # noqa: E402
from council import engine  # noqa: E402
from forge import fingerprint  # noqa: E402


def _pi(**kw):
    base = dict(prompt_sha256="a" * 64, semantic_sha256="b" * 64, bundle_sha256="c" * 64,
                cli_path="/usr/bin/x", cli_version="1.0", model_requested="m",
                model_reported="m", plugin_closure_sha256="d" * 64)
    base.update(kw)
    return fingerprint.PromptIdentity(**base)


def test_the_exact_hashes_of_three_seats_can_never_match_by_construction():
    """`make_sentinel` is per seat per attempt, so exact-hash comparison answers
    'differently prompted' 100% of the time."""
    task = "do the thing"
    hashes = set()
    for _ in range(3):
        tok = engine.make_sentinel()
        exact, _ = fingerprint.prompt_hashes(engine.apply_sentinel(task, tok), tok)
        hashes.add(exact)
    assert len(hashes) == 3


def test_the_semantic_hash_of_three_identically_prompted_seats_matches():
    task = "do the thing"
    semantics = set()
    for _ in range(3):
        tok = engine.make_sentinel()
        _, sem = fingerprint.prompt_hashes(engine.apply_sentinel(task, tok), tok)
        semantics.add(sem)
    assert len(semantics) == 1, \
        "this is the only comparison §11's question can be answered with"


def test_stripping_the_token_first_leaves_the_note_behind():
    """MEASURED at runner.py:258-267: the note CONTAINS the token, so token-first leaves ~280
    characters of engine text that still varies per seat — and a plan that then sees three
    different hashes concludes the seats were differently prompted when they were not."""
    tok = engine.make_sentinel()
    full = engine.apply_sentinel("body", tok)
    token_first = full.replace(tok, "")
    assert engine.SENTINEL_NOTE.format(token=tok) not in token_first
    assert fingerprint.without_engine_text(full, tok).strip() == "body"


def test_the_strip_folds_case_because_read_proof_does():
    tok = engine.make_sentinel()
    shouted = engine.apply_sentinel("body", tok).upper()
    assert tok.lower() not in fingerprint.without_engine_text(shouted, tok).lower()


def test_the_engine_text_rule_has_one_spelling():
    """`runner._rationale` and this function must be the same rule; two spellings of one
    predicate eventually disagree."""
    from forge import runner  # noqa: PLC0415
    tok = engine.make_sentinel()
    text = engine.apply_sentinel("an argued conclusion", tok)
    assert runner._rationale(text, tok) == fingerprint.without_engine_text(text, tok)


def test_a_version_that_could_not_be_read_is_none_never_a_string():
    """`inventory.version` returns "(unknown)" — a STRING — so two seats whose versions could
    not be read compare EQUAL, manufacturing the identity match §11 is about."""
    def failing(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")
    path, ver = fingerprint.probe_cli("claude", run=failing)
    assert ver is None


def test_a_missing_binary_is_none_not_an_exception():
    def missing(argv, **kw):
        raise FileNotFoundError(argv[0])
    path, ver = fingerprint.probe_cli("nosuchcli", run=missing)
    assert (path, ver) == (None, None)


def test_a_timeout_is_none():
    def slow(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 1)
    assert fingerprint.probe_cli("claude", run=slow)[1] is None


def test_a_clean_probe_records_the_version_verbatim():
    def ok(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, stdout="2.1.220 (Claude Code)\n", stderr="")
    assert fingerprint.probe_cli("claude", run=ok)[1] == "2.1.220 (Claude Code)"


def test_two_unread_versions_do_not_agree():
    a = _pi(cli_version=None)
    b = _pi(cli_version=None)
    assert fingerprint.agreement_label([a, b]) == "not-comparable"
    assert fingerprint.creditable("not-comparable") is False


def test_two_unhashed_bundles_do_not_agree():
    """CandidateBundle.gate_delta's three-state rule: None is 'nobody looked'."""
    assert fingerprint.agreement_label([_pi(bundle_sha256=None),
                                        _pi(bundle_sha256=None)]) == "not-comparable"


def test_two_uninstalled_plugin_closures_do_not_agree():
    assert fingerprint.agreement_label([_pi(plugin_closure_sha256=None),
                                        _pi(plugin_closure_sha256=None)]) == "not-comparable"


def test_identical_seats_are_identically_prompted_and_creditable():
    assert fingerprint.agreement_label([_pi(), _pi(), _pi()]) == "identically-prompted"
    assert fingerprint.creditable("identically-prompted") is True


def test_the_exact_hash_is_not_part_of_the_comparison():
    """It cannot be: the sentinel is per seat. A comparison that read it would label every
    real fleet differently-prompted."""
    assert fingerprint.agreement_label([_pi(prompt_sha256="1" * 64),
                                        _pi(prompt_sha256="2" * 64)]) == \
        "identically-prompted"


def test_a_different_semantic_hash_is_differently_prompted_and_not_creditable():
    assert fingerprint.agreement_label([_pi(), _pi(semantic_sha256="z" * 64)]) == \
        "differently-prompted"
    assert fingerprint.creditable("differently-prompted") is False


def test_a_different_model_is_differently_prompted():
    assert fingerprint.agreement_label([_pi(), _pi(model_requested="other")]) == \
        "differently-prompted"


def test_one_seat_cannot_agree_with_itself_alone():
    with pytest.raises(fingerprint.FingerprintError, match="two"):
        fingerprint.agreement_label([_pi()])


def test_a_requested_model_is_not_recorded_as_an_observed_one():
    pi = fingerprint.build(prompt="p", token="SENTINEL-abc", cli="claude",
                           model_requested="opus-5", model_reported=None,
                           run=lambda argv, **kw: subprocess.CompletedProcess(
                               argv, 0, stdout="2.1.220\n", stderr=""),
                           closure=lambda cli: "d" * 64)
    assert pi.model_requested == "opus-5" and pi.model_reported is None


def test_build_records_all_four_values_and_round_trips(tmp_path):
    pi = fingerprint.build(prompt="p", token="SENTINEL-abc", cli="claude",
                           bundle_sha256="c" * 64, model_requested="opus-5",
                           model_reported="opus-5",
                           run=lambda argv, **kw: subprocess.CompletedProcess(
                               argv, 0, stdout="2.1.220\n", stderr=""),
                           closure=lambda cli: "d" * 64)
    assert pi.prompt_sha256 == hashlib.sha256(b"p").hexdigest()
    assert pi.bundle_sha256 == "c" * 64
    assert pi.plugin_closure_sha256 == "d" * 64
    assert fingerprint.from_row(fingerprint.as_row(pi)) == pi


def test_a_row_missing_a_field_is_refused_rather_than_defaulted():
    row = fingerprint.as_row(_pi())
    del row["cli_version"]
    with pytest.raises(fingerprint.FingerprintError, match="missing"):
        fingerprint.from_row(row)


def test_a_row_with_an_unknown_field_is_refused():
    row = fingerprint.as_row(_pi())
    row["novel"] = 1
    with pytest.raises(fingerprint.FingerprintError, match="does not know"):
        fingerprint.from_row(row)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uvx --with pytest pytest -q tests/test_forge_fingerprint.py`
Expected: FAIL — `ImportError: cannot import name 'fingerprint' from 'forge'`.

- [ ] **Step 3: Write `fingerprint.py`**

Create `shared/lib/forge/fingerprint.py`:

```python
"""§11: per-seat prompt identity — agreement is provenance, never a correctness argument.

The trajectories are NOT statistically independent: they share the task text, the repository's
conventions and `CLAUDE.md`, the same test suite, the same skill instructions and correlated
model biases. A repeated mistake can be 3/3; the only correct fix can be unique to one. So this
file records what conditions the agreement, and NOTHING here promotes anything.

NOT NAMED `identity`. `fleet.clone_seat(..., identity=...)` and `runner.run_seat(...,
identity=...)` already mean the git author `(name, email)` pair, refused when either half is
empty because a seat that cannot commit is unusable. Reusing the word is how the two get
conflated in a review six weeks from now.

WHY THE SEMANTIC HASH IS THE ONLY COMPARISON THAT MEANS ANYTHING. `engine.make_sentinel` is
called PER SEAT, PER ATTEMPT, not once per run — so three seats' EXACT prompt hashes can never
match, by construction. Code that compares exact hashes to decide "identically prompted"
answers "differently prompted" 100% of the time and labels every agreement weaker. The exact
hash is still recorded, because it is the provenance of what was actually sent; it is simply
not what the comparison reads.

WHAT A `None` MEANS HERE, ON EVERY FIELD THAT HAS ONE: nobody looked. It must NEVER compare
equal to another `None` for "the same". `bundle.CandidateBundle.gate_delta`'s three-state rule
is the in-repo precedent, and §11 is where it matters most: two seats whose versions could not
be read, recorded as one string, would MANUFACTURE the identity match this section is written
to make honest.
"""
import hashlib
import shutil
import subprocess
from dataclasses import dataclass, fields

from council import engine

from . import taskbundle

# How long a `--version` probe may take. A CLI that cannot say its own version in this long is
# a CLI whose version was not measured, which is `None`.
_VERSION_TIMEOUT = 20

LABELS = ("identically-prompted", "differently-prompted", "not-comparable")


class FingerprintError(RuntimeError):
    """This seat's prompt identity cannot be recorded or compared honestly."""


@dataclass(frozen=True)
class PromptIdentity:
    """§11's four values, spread across eight fields because two of them are pairs.

    `prompt_sha256` — the exact text handed to the CLI. Captured BEFORE spec construction: for
    codex the prompt is `ProviderSpec.stdin`, for claude and agy it is buried inside `argv`, so
    recovering "the prompt" from a spec is per-provider guesswork.

    `semantic_sha256` — the same prompt with the engine's own text removed. See
    `without_engine_text`.

    `bundle_sha256` — §20's task-bundle manifest hash, or None when no bundle was supplied.

    `cli_path` / `cli_version` / `model_requested` / `model_reported` /
    `plugin_closure_sha256` — §11's fourth value, which is four sub-values with four different
    failure modes. MODEL IS TWO FIELDS: `ProviderSpec.model` is what forge REQUESTED, and the
    provider's own envelope is what it REPORTED. Collapsing them records a request as an
    observation.

    The CLI binary is recorded as a resolved absolute PATH and never hashed — these are
    multi-hundred-megabyte node bundles and the hash would dominate seat setup.
    """
    prompt_sha256: str
    semantic_sha256: str
    bundle_sha256: str | None
    cli_path: str | None
    cli_version: str | None
    model_requested: str | None
    model_reported: str | None
    plugin_closure_sha256: str | None


def without_engine_text(text: str, token: str) -> str:
    """`text` with the text FORGE supplied removed — the nonce-stripping rule, spelled once.

    THE ORDER IS LOAD-BEARING AND MEASURED (`runner.py:258-267`). `engine.SENTINEL_NOTE` is
    ~280 characters of instruction CONTAINING the token; strip the token first and the note no
    longer matches itself, leaving a "semantic" hash that still varies per seat — and a caller
    that then sees three different hashes concludes the seats were differently prompted when
    they were identically prompted.

    Case-insensitively, because `seat.read_proof` folds case when it looks for the same token:
    if a differently-cased echo counts as proof, it has to count as engine text here too, or
    one spelling would be proof AND content at once.

    WHAT THIS DOES NOT DO: it removes the note's EXACT text as `engine.apply_sentinel` writes
    it. A reflowed or paraphrased echo survives. It closes the one echo the engine's own
    instruction makes free, which is the one every seat is invited to make.

    `runner._rationale` DELEGATES HERE. Two spellings of one predicate eventually disagree, and
    this one decides both §8's rationale floor and §11's semantic hash.
    """
    import re  # noqa: PLC0415 — local, so this module's import list stays the four it uses
    if not token:
        return text
    for supplied in (engine.SENTINEL_NOTE.format(token=token), token):
        text = re.sub(re.escape(supplied), "", text, flags=re.IGNORECASE)
    return text


def prompt_hashes(prompt: str, token: str) -> tuple:
    """`(exact, nonce-stripped)`, in that order."""
    exact = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    semantic = hashlib.sha256(
        without_engine_text(prompt, token).encode("utf-8")).hexdigest()
    return exact, semantic


def probe_cli(name: str, *, run=subprocess.run) -> tuple:
    """`(absolute path, version)` for `name`, with `None` for anything unmeasured.

    NOT `inventory.version`, AND THIS IS THE ONE THAT WILL BE GOT WRONG. That function ignores
    the return code, falls back from stdout to stderr, swallows every exception into the string
    "(unavailable: ...)" and returns "(unknown)" for empty output. Those are STRINGS: two seats
    whose versions could not be read both record "(unknown)" and COMPARE EQUAL — an unread
    version manufacturing an identity match, which is precisely §11's "agreement is provenance"
    being fabricated.

    rc == 0 is REQUIRED. `seat.read_proof`'s rule, one module over: a missing measurement fails
    closed — it returns False, not True.

    Measured on this machine (2026-08-03): `claude --version` -> "2.1.220 (Claude Code)",
    `codex --version` -> "codex-cli 0.145.0", `agy --version` -> "1.1.9", all rc 0. That
    contradicts `scripts/lib/inventory.py:48`, which routes agy through `agy changelog` because
    `--version` was believed absent.
    """
    path = shutil.which(name)
    try:
        r = run([name, "--version"], capture_output=True, text=True,
                timeout=_VERSION_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return path, None
    if r.returncode != 0:
        return path, None
    out = (r.stdout or "").strip()
    return path, out or None


def build(*, prompt: str, token: str, cli: str, bundle_sha256=None,
          model_requested=None, model_reported=None, run=subprocess.run,
          closure=taskbundle.installed_closure) -> PromptIdentity:
    """The whole fingerprint for one seat.

    `closure` is injected so the suite can exercise the record without depending on which CLIs
    happen to be installed on the machine running it. Its default is the real resolver, which
    returns None for a CLI that is not installed — and None must fail the equality test, or
    three uninstalled CLIs "hash identically" and §20's licence to use an ambient skill is
    manufactured out of three absences.
    """
    exact, semantic = prompt_hashes(prompt, token)
    path, version = probe_cli(cli, run=run)
    return PromptIdentity(exact, semantic, bundle_sha256, path, version,
                          model_requested, model_reported, closure(cli))


def as_row(pi: PromptIdentity) -> dict:
    if not isinstance(pi, PromptIdentity):
        raise FingerprintError(f"a PromptIdentity is required, not {type(pi).__name__}")
    return {f.name: getattr(pi, f.name) for f in fields(PromptIdentity)}


def from_row(row) -> PromptIdentity:
    """The seat record's stored fingerprint, type-checked.

    Missing refused, unknown refused. A field defaulted here is a fact the run never measured,
    read back as one it did — §8's `proven_read`/`partial` rule: a measurement that was never
    taken is `partial`, not a free pass.
    """
    if not isinstance(row, dict):
        raise FingerprintError(f"a prompt identity is an object, not {type(row).__name__}")
    names = [f.name for f in fields(PromptIdentity)]
    missing = [n for n in names if n not in row]
    if missing:
        raise FingerprintError(f"this prompt identity is missing {missing}")
    unknown = sorted(set(row) - set(names))
    if unknown:
        raise FingerprintError(
            f"this prompt identity carries fields this engine does not know: {unknown}")
    for n in ("prompt_sha256", "semantic_sha256"):
        if not isinstance(row[n], str) or not row[n]:
            raise FingerprintError(f"{n} is a non-empty string, not {row[n]!r}")
    for n in names:
        if row[n] is not None and not isinstance(row[n], str):
            raise FingerprintError(f"{n} is a string or None, not {row[n]!r}")
    return PromptIdentity(**{n: row[n] for n in names})


# WHAT IS COMPARED, and why `prompt_sha256` is not in it: the sentinel is minted per seat per
# attempt, so the exact hashes are different by construction and a comparison reading them
# would label every real fleet differently-prompted. Everything here is a fact about what the
# seat was GIVEN, not about the nonce it was given it with.
_COMPARED = ("semantic_sha256", "bundle_sha256", "cli_version", "model_requested",
             "plugin_closure_sha256")


def agreement_label(ids) -> str:
    """How comparable these seats were: one of `LABELS`.

    THREE VALUES, NOT TWO. "We could not tell" must not be spelled the same way as "no" — but
    it must not credit agreement either, which is why `creditable` is True for exactly one of
    them. A `None` in any compared field is `not-comparable`: two seats with
    `bundle_sha256=None` are two seats whose bundles were never hashed, not two seats with the
    same bundle.

    `model_reported` is NOT compared — it is `None` for any provider without an envelope, which
    would make every codex/claude pair not-comparable for a reason that is about the envelope
    rather than about the prompt. It is recorded, and a report that wants it says so.

    NOTHING DOWNSTREAM MAY TREAT `identically-prompted` AS A CORRECTNESS ARGUMENT. §11's last
    line: agreement never substitutes for one.
    """
    ids = list(ids)
    if len(ids) < 2:
        raise FingerprintError(
            "an agreement label describes at least two seats; one seat agreeing with itself "
            "is not a measurement")
    for name in _COMPARED:
        values = [getattr(pi, name) for pi in ids]
        if any(v is None for v in values):
            return "not-comparable"
        if len(set(values)) != 1:
            return "differently-prompted"
    return "identically-prompted"


def creditable(label: str) -> bool:
    """Whether this label lets a consumer treat the seats' agreement as conditioned evidence.

    True for exactly one label. `differently-prompted` is §11's "labelled weaker" and
    `not-comparable` is "nobody measured" — neither is agreement, and a consumer that folded
    them together would lose which one it had.
    """
    if label not in LABELS:
        raise FingerprintError(f"an agreement label is one of {list(LABELS)}, not {label!r}")
    return label == "identically-prompted"
```

- [ ] **Step 4: Point `runner._rationale` at the one spelling**

In `shared/lib/forge/runner.py`, add `fingerprint` to the package import list:

```python
from . import (baseline as baselinemod, bundle, fingerprint, fleet, gate, harvest, journal,
               runstate, seat as seatmod, storage, verify)
```

Then replace `_rationale`'s body (the four lines after its docstring) with:

```python
    return fingerprint.without_engine_text(answer, token)
```

and add one paragraph to the end of its docstring:

```
    THE RULE ITSELF LIVES IN `fingerprint.without_engine_text`, because §11's nonce-stripped
    semantic hash needs the identical strip and two spellings of one predicate eventually
    disagree. This function is the §8 reading of it; the docstring above is why the rule is
    what it is, and it stays here because this is where it was measured.
```

> Check whether `re` is still used elsewhere in `runner.py` after this change. If it is not, remove the `import re`; a stale import is not a defect but `make verify` may lint it.

- [ ] **Step 5: Add the cross-module seam**

Append to `tests/test_forge_seams.py`:

```python
def test_the_engine_text_strip_has_one_spelling_across_sections_8_and_11():
    """§8's rationale floor and §11's semantic hash must strip exactly the same text. They
    were two functions once; the note-before-token order is measured, and a second copy that
    reversed it would leave §11 labelling identically-prompted seats as differently-prompted
    while §8 went on measuring the right thing."""
    from council import engine as eng  # noqa: PLC0415
    from forge import fingerprint, runner  # noqa: PLC0415
    tok = eng.make_sentinel()
    for body in ("", "short", "an argued conclusion about why nothing needs to change"):
        text = eng.apply_sentinel(body, tok)
        assert runner._rationale(text, tok) == fingerprint.without_engine_text(text, tok)
    assert runner._rationale.__doc__ and "fingerprint.without_engine_text" in \
        runner._rationale.__doc__, "the delegation must be stated where the rule was measured"
```

- [ ] **Step 6: Run the tests**

Run: `uvx --with pytest pytest -q tests/test_forge_fingerprint.py tests/test_forge_runner.py tests/test_forge_seams.py`
Expected: PASS — the fingerprint file's 21 plus the existing runner and seam suites, all green.

- [ ] **Step 7: Re-run under scrambled names, then mutate**

Rename every `test_*` in `tests/test_forge_fingerprint.py` to `test_zz0`…`test_zzN`, re-run, confirm the same count, **restore the names**. Then:

```bash
scripts/mutate.py --file shared/lib/forge/fingerprint.py \
  --old '    for supplied in (engine.SENTINEL_NOTE.format(token=token), token):' \
  --new '    for supplied in (token, engine.SENTINEL_NOTE.format(token=token)):' \
  -- uvx --with pytest pytest -q tests/test_forge_fingerprint.py

scripts/mutate.py --file shared/lib/forge/fingerprint.py \
  --old '    if r.returncode != 0:\n        return path, None' \
  --new '    if False:\n        return path, None' \
  -- uvx --with pytest pytest -q tests/test_forge_fingerprint.py

scripts/mutate.py --file shared/lib/forge/fingerprint.py \
  --old '        if any(v is None for v in values):\n            return "not-comparable"' \
  --new '        if False:\n            return "not-comparable"' \
  -- uvx --with pytest pytest -q tests/test_forge_fingerprint.py

scripts/mutate.py --file shared/lib/forge/fingerprint.py \
  --old '_COMPARED = ("semantic_sha256", "bundle_sha256", "cli_version", "model_requested",\n             "plugin_closure_sha256")' \
  --new '_COMPARED = ("prompt_sha256", "semantic_sha256", "bundle_sha256", "cli_version",\n             "model_requested", "plugin_closure_sha256")' \
  -- uvx --with pytest pytest -q tests/test_forge_fingerprint.py

scripts/mutate.py --file shared/lib/forge/fingerprint.py \
  --old '    return label == "identically-prompted"' \
  --new '    return label != "differently-prompted"' \
  -- uvx --with pytest pytest -q tests/test_forge_fingerprint.py
```

Expected: all five exit 0 (CAUGHT). `git status` clean afterwards.

- [ ] **Step 8: Add to the Makefile and commit**

Append `tests/test_forge_fingerprint.py` to `FORGE_TESTS`, then:

```bash
make render
git add shared/lib/forge/fingerprint.py shared/lib/forge/runner.py \
        tests/test_forge_fingerprint.py tests/test_forge_seams.py Makefile marketplaces
make verify; echo "verify rc=$?"
make precommit; echo "precommit rc=$?"
```

Expected: both `rc=0`. Then:

```bash
git commit -m "$(cat <<'EOF'
feat(forge): §11's identity, where an unread version is None and two Nones never agree

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UiV66Pt8cZVMq9t8WEAhpN
EOF
)"
```

---

### Task 6: The real launch adapter, the seat record's schema, and every measurement reaching it

**Files:**
- Create: `shared/lib/forge/launch.py`
- Create: `shared/lib/forge/seatrecord.py`
- Create: `tests/test_forge_launch.py`, `tests/test_forge_seatrecord.py`
- Modify: `shared/lib/forge/runner.py` (`_record` carries `prompt_identity`; `verify_candidate` gains a measurement sink; `_verify_a_seat` binds it)
- Modify: `tests/test_forge_runner.py`
- Modify: `Makefile`

**Interfaces:**

- **Consumes (Task 5):** `fingerprint.PromptIdentity`, `fingerprint.build`, `fingerprint.as_row`, `fingerprint.from_row`, `fingerprint.FingerprintError`.
- **Consumes (Task 2):** `taskbundle.installed_closure`.
- **Consumes (Plan H):** `seat.forge_spec(name, prompt, timeout, **kw)`, `engine.run_provider(spec, retries, timeout, backoff, workdir)`, `engine.apply_sentinel(prompt, token)`, `runner.SeatResult`, `runner._record`, `runner._measured`, `runner.verify_candidate`, `verify.SetupResult`, `bundle.CandidateBundle`.
- **Produces:**
  ```python
  launch.LaunchError(RuntimeError)
  launch.make_launcher(*, prompt: str, timeout: int, cfg: dict | None = None,
                       bundle_sha256: str | None = None, retries: int = 0,
                       backoff: float = 0.0,
                       run_provider=engine.run_provider,
                       probe=fingerprint.build) -> Callable
  # the returned callable satisfies runner's contract exactly:
  #   launch(*, name, seat_path, token, env) -> dict     # provider record + "prompt_identity"

  seatrecord.SeatRecordError(RuntimeError)
  seatrecord.Attempt(...)          # frozen; every key runner._record writes
  seatrecord.SeatRecord(name: str, attempts: tuple[Attempt, ...])
  seatrecord.decode(payload: dict) -> SeatRecord
  seatrecord.identities(rec: SeatRecord) -> tuple[fingerprint.PromptIdentity | None, ...]

  # runner, changed:
  runner.verify_candidate(manifest, run_dir, baseline, candidate, *, name, identity,
                          calibration, on_measurement=None) -> tuple
  ```

**Three things, one deliverable: the seat record becomes a schema'd, complete record produced by a real adapter.** They are one task because they are one sentence — a schema whose producer omits a field is a schema over a fiction, and a fingerprint that no real launcher produces is Plan H's lesson #1 (*"an approved rule with no caller is an untested rule, however well argued"*) repeated.

**`forge_spec` must be wired here or nowhere.** It has had no production caller since Plan H, so §8.1's validator has never run outside the suite. Whatever wires the real `launch` adapter wires `forge_spec` with it, or seats run under the council's own validity policy — the exact defect that module exists to close. `_forge_validator` neutralizes `sentinel` and `min_chars` deliberately (a builder seat's terse sign-off must not trigger a re-run); §8's `proven_read` reads the token separately, through `seat.read_proof`.

**The launcher applies the sentinel, and it must be `engine.apply_sentinel`.** `runner._rationale` — now `fingerprint.without_engine_text` — strips `SENTINEL_NOTE.format(token=token)` and then the token, which is exactly `apply_sentinel`'s output. **A launcher that composes the prompt any other way makes the strip a no-op**, and §8's rationale floor and §11's semantic hash both silently start measuring engine text. This is a cross-module seam of precisely the shape that has bitten this project four times.

**Why the launcher owns the fingerprint.** `runner.run_seat` never sees the prompt: it mints the token and calls `launch(name=, seat_path=, token=, env=)`. For codex the prompt is `ProviderSpec.stdin`; for claude and agy it is buried inside `argv`. Recovering "the prompt" from a spec is per-provider guesswork, so the launcher — the only party that knows it — returns the `PromptIdentity` in its record.

**Closing `runner.verify_candidate`'s measurement loss without changing what it raises.** The docstring commits to `VerifyError` propagating unwrapped and a test pins that. A keyword-only `on_measurement` callback is invoked the instant each measurement exists — the same write-ahead discipline `journal.intent` applies to an operation, applied to a value. `_verify_a_seat` binds what the sink hands it and its existing handler puts it through `_measured`, whose candidate-identity check still governs admission: *a candidate that is not this seat's is a measurement that must not reach this seat's record at all.*

- [ ] **Step 1: Write the failing tests for the record schema**

Create `tests/test_forge_seatrecord.py`:

```python
"""§14.2's per-seat record, with the schema its first real reader needs."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

import pytest  # noqa: E402
from forge import fingerprint, seatrecord  # noqa: E402


def _pi_row():
    return fingerprint.as_row(fingerprint.PromptIdentity(
        "a" * 64, "b" * 64, "c" * 64, "/usr/bin/claude", "2.1.220", "opus-5", "opus-5",
        "d" * 64))


def _attempt(**kw):
    base = dict(attempt=1, path="/run/seat-claude-1", branch="khenrix-forge/claude",
                sentinel="SENTINEL-abc", status=None, verification=None,
                verification_refused=None, setup_run=None, verifier_setup=None,
                artifacts={"paths": [], "origin": {}, "setup_overlap": [],
                           "verify_overlap": []},
                candidate={"baseline_ref": "refs/x", "baseline_commit": "0" * 40,
                           "tracked_patch_bytes": 0, "sidecars": [], "omitted": [],
                           "generator_contract_id": "", "gate_delta": None,
                           "gate_surface": None},
                launch=None, prompt_identity=_pi_row())
    base.update(kw)
    return base


def test_a_well_formed_record_decodes(tmp_path):
    rec = seatrecord.decode({"name": "claude", "attempts": [_attempt()]})
    assert rec.name == "claude" and len(rec.attempts) == 1
    assert rec.attempts[0].prompt_identity.cli_version == "2.1.220"


def test_the_measured_seat_record_typo_is_now_refused():
    """MEASURED on runstate.write_seat: `{"phse": "biulding"}` is written, read and
    reconstructed with no complaint. This is the reader that ends that."""
    with pytest.raises(seatrecord.SeatRecordError, match="does not know"):
        seatrecord.decode({"name": "claude", "attempts": [{**_attempt(), "phse": "biulding"}]})


def test_a_missing_field_is_refused_rather_than_defaulted():
    a = _attempt()
    del a["sentinel"]
    with pytest.raises(seatrecord.SeatRecordError, match="missing"):
        seatrecord.decode({"name": "claude", "attempts": [a]})


def test_a_launch_that_returned_no_fingerprint_is_null_not_absent():
    """§8's proven_read/partial rule: a measurement never taken is `partial`, not a free
    pass. The KEY is always present; its value is None."""
    rec = seatrecord.decode({"name": "claude",
                             "attempts": [_attempt(prompt_identity=None)]})
    assert rec.attempts[0].prompt_identity is None


def test_a_malformed_fingerprint_is_a_refusal_not_a_none():
    """The difference that matters: 'nobody measured' and 'somebody wrote nonsense' are not
    the same record."""
    bad = _attempt(prompt_identity={"prompt_sha256": "a" * 64})
    with pytest.raises(seatrecord.SeatRecordError, match="missing"):
        seatrecord.decode({"name": "claude", "attempts": [bad]})


def test_an_attempt_number_that_repeats_is_refused():
    """§8.1 preserves every attempt as partial input; two records under one number make
    'which clone is this' unanswerable."""
    with pytest.raises(seatrecord.SeatRecordError, match="twice"):
        seatrecord.decode({"name": "claude",
                           "attempts": [_attempt(attempt=1), _attempt(attempt=1)]})


def test_a_half_gate_measurement_is_refused():
    """`with_gate_measurement`'s rule: a delta with no surface beside it cannot say whether
    `()` means 'measured, nothing moved' or 'nothing was looked at'."""
    half = _attempt(candidate={**_attempt()["candidate"], "gate_delta": []})
    with pytest.raises(seatrecord.SeatRecordError, match="gate_surface"):
        seatrecord.decode({"name": "claude", "attempts": [half]})


def test_identities_returns_one_entry_per_attempt_including_the_unmeasured_ones():
    rec = seatrecord.decode({"name": "claude",
                             "attempts": [_attempt(attempt=1),
                                          _attempt(attempt=2, prompt_identity=None)]})
    got = seatrecord.identities(rec)
    assert len(got) == 2 and got[1] is None


def test_an_empty_record_is_refused():
    with pytest.raises(seatrecord.SeatRecordError, match="at least one"):
        seatrecord.decode({"name": "claude", "attempts": []})
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uvx --with pytest pytest -q tests/test_forge_seatrecord.py`
Expected: FAIL — `ImportError: cannot import name 'seatrecord' from 'forge'`.

- [ ] **Step 3: Write `seatrecord.py`**

Create `shared/lib/forge/seatrecord.py`:

```python
"""§14.2's per-seat record, as a schema — written beside its first real reader.

WHY THE SCHEMA IS HERE AND NOT IN `runstate`. `runstate.write_seat` deliberately enforces
none, and its docstring argues the case: §14.2 assigns the seat record's fields to the
ORCHESTRATOR, so `runstate` refuses to become the authority on a record another module owns,
and hoisting the shape into that writer would put one rule in two places. That argument is
still right. What changed is that the record now has a real READER — §12's rubric and §13's
fusion read it, and `--collect` reconstructs a run from it — so the type-check the writer
declined belongs at the reading boundary, which is here.

MEASURED, AND THIS IS WHY IT MATTERS: `{"phse": "biulding"}` is written, read and
reconstructed by `runstate` with no complaint. On a record that decides which candidate ships,
that is the fail-open this package refuses everywhere else.

WHAT `None` MEANS ON EACH OPTIONAL FIELD, and it is the same thing every time: the measurement
was never taken. `prompt_identity: None` is a launch that returned no fingerprint;
`verification: None` is a seat §6 never reached. A MALFORMED value is NOT None — it raises,
because "nobody measured" and "somebody wrote nonsense" are different records and only one of
them is safe to act on.

THE WRITER PROVES ITS OWN OUTPUT DECODES. `runner._record` calls `decode` on the payload it is
about to hand `write_seat`, so a field the writer stops writing fails at the writer rather than
hours later on a resume. The invariant lives in the value; this is what keeps it honest.
"""
from dataclasses import dataclass, fields

from . import fingerprint


class SeatRecordError(RuntimeError):
    """This seat record cannot be read as a description of a seat."""


@dataclass(frozen=True)
class Attempt:
    """One attempt at one seat — exactly the keys `runner._record` writes.

    Kept in lockstep with that function by `tests/test_forge_runner.py`'s round-trip test: a
    key added there and not here fails the next write, which is the direction that catches it
    while the producer is still present.
    """
    attempt: int
    path: str
    branch: str | None
    sentinel: str | None
    status: dict | None
    verification: dict | None
    verification_refused: str | None
    setup_run: dict | None
    verifier_setup: dict | None
    artifacts: dict
    candidate: dict
    launch: dict | None
    prompt_identity: object            # fingerprint.PromptIdentity | None, after decode


@dataclass(frozen=True)
class SeatRecord:
    name: str
    attempts: tuple


_ATTEMPT_NAMES = tuple(f.name for f in fields(Attempt))


def _attempt(row, where) -> Attempt:
    if not isinstance(row, dict):
        raise SeatRecordError(f"{where}: an attempt is an object, not {type(row).__name__}")
    missing = [n for n in _ATTEMPT_NAMES if n not in row]
    if missing:
        raise SeatRecordError(f"{where} is missing {missing}")
    unknown = sorted(set(row) - set(_ATTEMPT_NAMES))
    if unknown:
        raise SeatRecordError(
            f"{where} carries fields this engine does not know: {unknown}. A recorder that "
            "once wrote a fact this reader drops would let a later phase answer questions "
            "about the seat out of a record it only partly understands.")
    if not isinstance(row["attempt"], int) or isinstance(row["attempt"], bool):
        raise SeatRecordError(f"{where}: attempt is an int, not {row['attempt']!r}")
    if not isinstance(row["path"], str) or not row["path"]:
        raise SeatRecordError(f"{where}: path is a non-empty string")
    for n in ("status", "verification", "setup_run", "verifier_setup", "launch"):
        if row[n] is not None and not isinstance(row[n], dict):
            raise SeatRecordError(f"{where}: {n} is an object or null, not {row[n]!r}")
    for n in ("artifacts", "candidate"):
        if not isinstance(row[n], dict):
            raise SeatRecordError(f"{where}: {n} is an object, not {row[n]!r}")
    cand = row["candidate"]
    # `with_gate_measurement`'s rule, enforced where the record is read: §6.1's delta and the
    # surface it ranged over go together or not at all. A delta with no surface cannot say
    # whether `()` means "measured over four files and none moved" or "nothing was looked at",
    # and `None` for both is the third state — nobody measured.
    if (cand.get("gate_delta") is None) != (cand.get("gate_surface") is None):
        raise SeatRecordError(
            f"{where}: gate_delta and gate_surface go together or not at all; this record has "
            f"delta={cand.get('gate_delta')!r} beside surface={cand.get('gate_surface')!r}, "
            "which cannot say whether an empty delta means a clean gate or an unlooked-at one")
    pi = row["prompt_identity"]
    if pi is not None:
        try:
            pi = fingerprint.from_row(pi)
        except fingerprint.FingerprintError as e:
            # NOT swallowed into None. `None` is "the launch returned no fingerprint", and a
            # malformed one read as that would file a damaged measurement as an absent one.
            raise SeatRecordError(f"{where}: prompt_identity: {e}") from e
    body = {n: row[n] for n in _ATTEMPT_NAMES}
    body["prompt_identity"] = pi
    return Attempt(**body)


def decode(payload) -> SeatRecord:
    """`runstate.read_seat`'s payload, type-checked into a value a later phase can act on."""
    if not isinstance(payload, dict):
        raise SeatRecordError(f"a seat record is an object, not {type(payload).__name__}")
    names = ("name", "attempts")
    missing = [n for n in names if n not in payload]
    if missing:
        raise SeatRecordError(f"this seat record is missing {missing}")
    unknown = sorted(set(payload) - set(names))
    if unknown:
        raise SeatRecordError(
            f"this seat record carries fields this engine does not know: {unknown}")
    if not isinstance(payload["name"], str) or not payload["name"]:
        raise SeatRecordError("a seat record's name is a non-empty string")
    if not isinstance(payload["attempts"], list) or not payload["attempts"]:
        raise SeatRecordError(
            "a seat record holds at least one attempt. An empty list is falsy exactly as the "
            "None a seat that never wrote reads back as, and §14.1 requires those stay apart.")
    attempts, seen = [], set()
    for i, row in enumerate(payload["attempts"]):
        a = _attempt(row, f"{payload['name']} attempt {i}")
        if a.attempt in seen:
            raise SeatRecordError(
                f"{payload['name']}: attempt {a.attempt} is recorded twice. §8.1 preserves "
                "every failed attempt as partial input, so two records under one number make "
                "'which clone is this' unanswerable.")
        seen.add(a.attempt)
        attempts.append(a)
    return SeatRecord(payload["name"], tuple(attempts))


def identities(rec: SeatRecord) -> tuple:
    """One entry per attempt, `None` where the launch returned no fingerprint.

    One per attempt rather than one per seat, and never filtered: §11's comparison is over
    what each seat was GIVEN, and dropping the unmeasured ones would make a fleet of three
    seats with one fingerprint between them read as a fleet of one.
    """
    return tuple(a.prompt_identity for a in rec.attempts)
```

- [ ] **Step 4: Run the record tests**

Run: `uvx --with pytest pytest -q tests/test_forge_seatrecord.py`
Expected: PASS — 9 passed.

- [ ] **Step 5: Write the failing tests for the launcher and the runner changes**

Create `tests/test_forge_launch.py`:

```python
"""The real provider adapter — wired to §8.1's validator, and never invoked by this suite."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

import hashlib  # noqa: E402
import subprocess  # noqa: E402
import pytest  # noqa: E402
from council import engine  # noqa: E402
from forge import fingerprint, launch, seat  # noqa: E402


def _fake_provider(seen):
    def run_provider(spec, retries, timeout, backoff, workdir):
        seen.append(spec)
        return {"status": "ok", "reason": "", "valid": True, "structured": True,
                "exit_code": 0, "duration_sec": 1.0, "attempts": 1,
                "result_text": "done"}
    return run_provider


def _probe(**kw):
    def build(**called):
        called.update(kw)
        return fingerprint.PromptIdentity(
            *fingerprint.prompt_hashes(called["prompt"], called["token"]),
            called.get("bundle_sha256"), "/usr/bin/x", "1.0",
            called.get("model_requested"), called.get("model_reported"), "d" * 64)
    return build


def test_the_launcher_runs_the_forge_validator_not_the_councils(tmp_path):
    """§8.1's validator has had NO production caller since Plan H. Whatever wires the real
    adapter must wire `forge_spec` with it, or seats run under council's own validity policy
    — the defect that module exists to close."""
    seen = []
    fn = launch.make_launcher(prompt="do it", timeout=60,
                              run_provider=_fake_provider(seen), probe=_probe())
    fn(name="claude", seat_path=tmp_path, token="SENTINEL-abc", env={})
    assert seen[0].validator is seat._forge_validator
    assert seen[0].min_chars == 0


def test_the_seat_runs_from_its_own_clone(tmp_path):
    """§18's 'no seat launches with cwd=None' is satisfiable only if somebody sets it."""
    seen = []
    fn = launch.make_launcher(prompt="do it", timeout=60,
                              run_provider=_fake_provider(seen), probe=_probe())
    fn(name="claude", seat_path=tmp_path, token="SENTINEL-abc", env={})
    assert seen[0].cwd == str(tmp_path)


def test_the_prompt_is_composed_by_apply_sentinel(tmp_path):
    """THE CROSS-MODULE SEAM: `fingerprint.without_engine_text` strips exactly
    `apply_sentinel`'s output. A launcher composing the prompt any other way makes the strip a
    no-op, and §8's rationale floor and §11's semantic hash both start measuring engine text."""
    seen = []
    fn = launch.make_launcher(prompt="do it", timeout=60,
                              run_provider=_fake_provider(seen), probe=_probe())
    r = fn(name="claude", seat_path=tmp_path, token="SENTINEL-abc", env={})
    expected = engine.apply_sentinel("do it", "SENTINEL-abc")
    assert r["prompt_identity"]["prompt_sha256"] == \
        hashlib.sha256(expected.encode()).hexdigest()
    assert fingerprint.without_engine_text(expected, "SENTINEL-abc").strip() == "do it"


def test_the_record_carries_the_fingerprint_beside_the_provider_record(tmp_path):
    fn = launch.make_launcher(prompt="do it", timeout=60, bundle_sha256="c" * 64,
                              run_provider=_fake_provider([]), probe=_probe())
    r = fn(name="claude", seat_path=tmp_path, token="SENTINEL-abc", env={})
    assert r["valid"] is True and r["result_text"] == "done"
    assert fingerprint.from_row(r["prompt_identity"]).bundle_sha256 == "c" * 64


def test_the_launcher_never_overwrites_a_provider_field(tmp_path):
    """A provider that one day returns its own `prompt_identity` must not be silently
    replaced — nor silently win."""
    def run_provider(spec, retries, timeout, backoff, workdir):
        return {"valid": True, "result_text": "x", "prompt_identity": {"rogue": 1}}
    fn = launch.make_launcher(prompt="p", timeout=60,
                              run_provider=run_provider, probe=_probe())
    with pytest.raises(launch.LaunchError, match="prompt_identity"):
        fn(name="claude", seat_path=tmp_path, token="SENTINEL-abc", env={})


def test_retries_default_to_zero_because_the_gate_priced_them_that_way(tmp_path):
    seen = {}

    def run_provider(spec, retries, timeout, backoff, workdir):
        seen["retries"] = retries
        return {"valid": True, "result_text": "x"}
    launch.make_launcher(prompt="p", timeout=60, run_provider=run_provider,
                         probe=_probe())(name="claude", seat_path=tmp_path,
                                         token="SENTINEL-abc", env={})
    assert seen["retries"] == 0


def test_a_provider_record_that_is_not_a_mapping_is_refused(tmp_path):
    def run_provider(spec, retries, timeout, backoff, workdir):
        return "ok"
    fn = launch.make_launcher(prompt="p", timeout=60, run_provider=run_provider,
                              probe=_probe())
    with pytest.raises(launch.LaunchError, match="mapping"):
        fn(name="claude", seat_path=tmp_path, token="SENTINEL-abc", env={})
```

Append to `tests/test_forge_runner.py`. **These use the helpers that file already defines** — `_open`, `_fake`, `_edit`, `_per_seat`, `_attempt`, `_confirmed`, `GATE`, `IDENT`, `write`, `commit_all` — verified present at `tests/test_forge_runner.py:36, 79, 139, 1157, 1175, 1179`:

```python
def test_the_record_always_carries_the_prompt_identity_key(tmp_path):
    """§8's proven_read/partial rule: a launch that returned no fingerprint must not produce
    a record that OMITS the key. An omitted key and an unmeasured one read the same to
    `--collect`, and only one of them is a run that lost a measurement."""
    repo, run, b, m = _open(tmp_path)
    runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT,
                    launch=_fake(_edit))
    entry = _attempt(run, "claude", 1)
    assert "prompt_identity" in entry and entry["prompt_identity"] is None


def test_a_malformed_prompt_identity_is_refused_at_the_writer(tmp_path):
    """'Nobody measured' and 'somebody wrote nonsense' are different records, and only one
    of them is safe to act on."""
    def launch(*, name, seat_path, token, env):
        _edit(Path(seat_path))
        return {"name": name, "status": "ok", "valid": True, "reason": "ok",
                "exit_code": 0, "duration_sec": 1.0, "structured": False, "attempts": 1,
                "result_text": f"{ANSWER}\n{token}",
                "prompt_identity": {"prompt_sha256": "a" * 64}}

    repo, run, b, m = _open(tmp_path)
    with pytest.raises(runner.RunnerError, match="prompt_identity"):
        runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT, launch=launch)


def test_a_measurement_taken_before_a_refusal_still_reaches_the_record(tmp_path):
    """THE THIRD INSTANCE, closed. `build_verifier` fills §6.1's gate surface and `run_setup`
    returns a SetupResult; a later refusal dropped both as locals, so a verifier clone that
    was built, whose gate surface WAS measured over two trees, and whose setup ran and
    exited 0 came back `gate_delta: null, gate_surface: null, verifier_setup: null`.

    THE RIG IS THE ADVERSARIAL ONE, not a lookalike. It is the same candidate
    `test_a_candidate_that_repoints_the_hooks_path_through_setup_does_not_reach_the_gate`
    uses (`tests/test_forge_runner.py:950`) — a `./setup.sh` that repoints
    `core.hooksPath` — driven through `runner.run` so `_verify_a_seat` runs and the record
    is written. A fixture built only of realistic-looking answers cannot reach the guard
    that exists for unrealistic ones.
    """
    def seed(repo):
        write(repo, "gate.sh", "#!/bin/sh\nexit 0\n").chmod(0o755)
        write(repo, "setup.sh", "#!/bin/sh\nexit 0\n").chmod(0o755)
        commit_all(repo, "gate and setup")

    setup = (verify.Step(argv=("./setup.sh",)),)
    repo, run, b, m = _open(tmp_path, setup=setup, gate=GATE, seed=seed,
                            seats=1, attempts=1)
    _confirmed(run)

    def rig(name, n, p):
        write(p, "setup.sh", "#!/bin/sh\ngit config --local core.hooksPath .githooks\n"
                             "exit 0\n").chmod(0o755)
        return True

    runner.run(run, repo, identity=IDENT, launch=_per_seat(rig))
    entry = _attempt(run, "claude", 1)
    assert entry["verification_refused"] and "core.hooksPath" in entry["verification_refused"]
    assert entry["candidate"]["gate_surface"] is not None, \
        "§6.1's surface was measured over two trees in a clone that was built and paid for"
    assert entry["candidate"]["gate_delta"] is not None
    assert entry["verifier_setup"] is not None and \
        entry["verifier_setup"]["exit_code"] == 0, \
        "the verifier's setup ran and exited 0 — the very fact explaining how the hooks moved"
```

> **Verify the seat name before relying on it.** `_per_seat` answers for whichever seats `runner.run` drives, and `_attempt(run, "claude", 1)` assumes `claude` is among them. Read `runner._fleet` and the manifest `_open` writes; if the fleet's names differ, use the name the loop actually drove. **Do not** change `seats=1` to make an assertion pass — a one-seat fleet is chosen here so exactly one record exists to read.

- [ ] **Step 6: Run them to verify they fail**

Run: `uvx --with pytest pytest -q tests/test_forge_launch.py tests/test_forge_runner.py`
Expected: FAIL — `ImportError: cannot import name 'launch' from 'forge'`, and the three new runner tests failing on the missing key.

- [ ] **Step 7: Write `launch.py`**

Create `shared/lib/forge/launch.py`:

```python
"""The real provider adapter: `runner`'s injected `launch` contract, satisfied for real.

NOTHING IN THIS PACKAGE'S SUITE CALLS A PROVIDER. `run_provider` and the fingerprint probe are
both parameters with real defaults, and every test overrides them. §5.2 prices a real fleet in
provider calls and a suite that spends them is one nobody runs — so the first real invocation
is the skill's own eval, one plan away. What that costs is that NOTHING HERE PROVES THE REAL
PROVIDER PATH WORKS; the signature is kept narrow enough that the shape is obviously the same.

WHY THIS FILE EXISTS AT ALL, beyond convenience: `seat.forge_spec` has had NO PRODUCTION
CALLER since Plan H, so §8.1's validator has never run outside the suite. Whatever wires the
real adapter must wire `forge_spec` with it, or seats run under the council's own validity
policy — a builder seat that worked for forty minutes and signed off in one line would be
RE-RUN on top of itself, which is the defect `_forge_validator` exists to close.

THE PROMPT IS COMPOSED BY `engine.apply_sentinel`, AND THAT IS A SEAM, NOT A STYLE CHOICE.
`fingerprint.without_engine_text` — which is both §8's rationale floor and §11's nonce-stripped
semantic hash — removes `SENTINEL_NOTE.format(token=token)` and then the token, which is
exactly what `apply_sentinel` writes. A launcher that composed the prompt any other way would
make that strip a no-op, and both measurements would silently start counting engine text as
the seat's own.

WHY THE LAUNCHER OWNS THE FINGERPRINT. `runner.run_seat` never sees the prompt: it mints the
token and calls `launch(name=, seat_path=, token=, env=)`. For codex the prompt is
`ProviderSpec.stdin` and for claude and agy it is buried inside `argv`, so recovering "the
prompt" from a spec is per-provider guesswork. The only party that knows it is this one.

`retries=0` BY DEFAULT, EXPLICITLY. `gate.quote` prices one attempt per seat per round and
`run_provider` takes `retries` as a required positional — an inherited default here would
re-price the run without anybody choosing to.
"""
from pathlib import Path

from council import engine

from . import fingerprint, seat as seatmod


class LaunchError(RuntimeError):
    """This provider could not be launched, or answered something that cannot be recorded."""


def make_launcher(*, prompt: str, timeout: int, cfg=None, bundle_sha256=None,
                  retries: int = 0, backoff: float = 0.0,
                  run_provider=engine.run_provider,
                  probe=fingerprint.build):
    """A callable satisfying `runner`'s contract: `launch(*, name, seat_path, token, env)`.

    `env` is `fleet.forge_child_env`'s scrubbed environment. It is NOT passed to
    `run_provider`, which builds its own child environment — it is accepted because the
    contract declares it, and ignoring a declared parameter silently would be worse than
    saying so here.

    The adapter owns the model and the TIMEOUT: §19 forbids a second timeout mechanism, so
    there is deliberately no timeout parameter on the returned callable.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise LaunchError("a seat is launched with a task, and this prompt is empty")
    cfg = cfg or {}

    def launch(*, name, seat_path, token, env):
        seat_prompt = engine.apply_sentinel(prompt, token)
        spec = seatmod.forge_spec(name, seat_prompt, timeout, cfg=cfg,
                                  workdir=Path(seat_path))
        # §13 plants the proof token in a bundle for a REVIEWER; a builder seat's token is in
        # the prompt, as `apply_sentinel` put it. `spec.sentinel` is set for provenance only —
        # `_forge_validator` neutralizes it deliberately, and §8's `proven_read` reads the
        # token separately through `seat.read_proof`.
        spec.sentinel = token
        spec.cwd = str(seat_path)
        record = run_provider(spec, retries, timeout, backoff, Path(seat_path))
        if not isinstance(record, dict):
            raise LaunchError(
                f"{name}: a provider record is a mapping, not {type(record).__name__}; §8's "
                "`process` dimension is read off it and there is no third value for "
                "'unreadable'")
        if "prompt_identity" in record:
            raise LaunchError(
                f"{name}: the provider record already carries `prompt_identity`. This adapter "
                "is the only party that knows the prompt, so a second one would mean two "
                "answers to §11's question with nothing saying which was measured.")
        pi = probe(prompt=seat_prompt, token=token, cli=name,
                   bundle_sha256=bundle_sha256,
                   model_requested=spec.model,
                   model_reported=_reported(record))
        return {**record, "prompt_identity": fingerprint.as_row(pi)}

    return launch


def _reported(record) -> str | None:
    """The model the PROVIDER named, or None where its envelope names none.

    Recorded apart from `model_requested` because collapsing them records a request as an
    observation. `None` here is honest for codex, whose record carries no model field.
    """
    v = record.get("model")
    return v if isinstance(v, str) and v else None
```

- [ ] **Step 8: Change `runner._record` to carry the fingerprint**

In `shared/lib/forge/runner.py`, add `seatrecord` to the package import list, then add this key to the dict `_record` returns, immediately after `"launch"`:

```python
        # §11's four values, ALWAYS PRESENT AS A KEY. `None` is a launch that returned no
        # fingerprint — §8's `proven_read`/`partial` rule: a measurement never taken is
        # recorded as absent, never omitted, because an omitted key and an unmeasured one read
        # the same to `--collect` and only one of them is a run that lost a measurement.
        # A MALFORMED one raises: "nobody measured" and "somebody wrote nonsense" are
        # different records and only one is safe to act on.
        "prompt_identity": _prompt_identity(result.launch_result),
```

and add the helper beside `_clip`:

```python
def _prompt_identity(launch_result):
    """§11's fingerprint as the launcher returned it, type-checked here.

    `_prior_attempts` type-checks `attempts` on the read side for the same reason: a damaged
    record costs nothing yet at the one point where the producer is still present.
    """
    if not isinstance(launch_result, dict) or launch_result.get("prompt_identity") is None:
        return None
    try:
        return fingerprint.as_row(
            fingerprint.from_row(launch_result["prompt_identity"]))
    except fingerprint.FingerprintError as e:
        raise RunnerError(f"this launch returned an unreadable prompt_identity: {e}") from e
```

Then, at the end of `_write` (or immediately before `runstate.write_seat` is called in it), prove the payload decodes:

```python
    # THE WRITER PROVES ITS OWN OUTPUT DECODES. `runstate.write_seat` enforces no schema by
    # design; `seatrecord` is the reader's, and running it here fails a dropped or renamed
    # field at the writer rather than hours later on a resume.
    seatrecord.decode(payload)
```

- [ ] **Step 9: Close the measurement loss in `verify_candidate`**

In `shared/lib/forge/runner.py`, change the signature:

```python
def verify_candidate(manifest, run_dir, baseline, candidate, *, name, identity,
                     calibration, on_measurement=None) -> tuple:
```

Add this paragraph to the docstring, **replacing** the one beginning *"ON THE RETURN PATH ONLY, AND THAT IS STILL OPEN"*:

```
    ON THE RETURN PATH, AND NOW ALSO ON THE REFUSAL PATH. Everything here is measured in order
    — `build_verifier` fills §6.1's `gate_delta`/`gate_surface`, then `run_setup` runs — and a
    refusal from any step AFTER them used to leave both as locals this function dropped on its
    way out. Measured through the loop: a candidate that repoints `core.hooksPath` through its
    own setup script is refused at `assert_hooks_pinned`, and the seat's record came back
    `gate_delta: null, gate_surface: null, verifier_setup: null` for a verifier clone that was
    built, whose gate surface WAS measured over two trees, and whose setup ran and exited 0 —
    the very fact explaining how the hooks moved.

    `on_measurement` closes it WITHOUT CHANGING WHAT THIS FUNCTION RAISES. It is called
    `on_measurement(candidate_or_None, setup_result_or_None)` the instant each measurement
    exists, before any step that can refuse it — `journal.intent`'s write-ahead discipline,
    applied to a value rather than to an operation. A narrower `except` at the call site cannot
    do this: the values never leave this frame, and a handler reaching back for `v.candidate`
    cannot distinguish "§8 refused this status" from "this candidate is not this seat's". The
    admission rule stays in `_measured`, where it already is.
```

Immediately after `build_verifier` returns and before the next statement that can raise:

```python
    if on_measurement is not None:
        on_measurement(v.candidate, None)
```

and immediately after `run_setup` returns its `SetupResult`:

```python
    if on_measurement is not None:
        on_measurement(v.candidate, setup_result)
```

> Read the function body before editing. The two call sites are wherever `build_verifier` and `run_setup` actually return in the current code — **do not** guess line numbers; the sink must fire before the *next* statement that can raise, not at the end of a block.

- [ ] **Step 10: Bind the sink in `_verify_a_seat`**

In `_verify_a_seat`, replace the `try` block's first statement group with:

```python
    taken = {"candidate": None, "setup": None}

    def _sink(candidate, setup_result):
        # Last write wins by design: the second call carries the same candidate plus the
        # setup result, so a refusal between the two leaves the first call's value in place
        # rather than nothing.
        if candidate is not None:
            taken["candidate"] = candidate
        if setup_result is not None:
            taken["setup"] = setup_result

    try:
        outcome, reason, v, setup_result = verify_candidate(
            manifest, run_dir, base, result.candidate, name=result.name, identity=identity,
            calibration=calibration, on_measurement=_sink)
        measured = replace(_measured(result, v.candidate, setup_result),
                           verification=(outcome, reason))
```

and in the `except (RunnerError, verify.VerifyError)` handler, put what the sink caught through the same admission rule before recording:

```python
    except (RunnerError, verify.VerifyError) as e:
        log.record(journal.done(_VERIFICATION), operation_id=op, seat=result.name,
                   refused=str(e))
        # WHAT WAS MEASURED SURVIVES THE REFUSAL, and it goes through `_measured` rather than
        # around it: a candidate that is not this seat's is a measurement that must not reach
        # this seat's record at all, which is the whole of what that check is for. `_measured`
        # raising here would be that check doing its job, so it is not caught.
        kept = _measured(result, taken["candidate"], taken["setup"])
        return _revise(run_dir, replace(kept, verification_refused=str(e)))
```

> **Read `_verify_a_seat` in full before editing.** Its two `except` blocks and their ordering are the subject of a closed Critical; preserve them. Verify that `_measured` raising inside this handler is acceptable — the docstring at `runner.py:1365` says the sibling case (`_revise` failing inside `reclassify_seat`) stays open because swallowing it would fail open, and the same argument applies here.

- [ ] **Step 11: Run everything**

Run: `uvx --with pytest pytest -q tests/test_forge_launch.py tests/test_forge_seatrecord.py tests/test_forge_runner.py tests/test_forge_seams.py`
Expected: PASS — all four suites green, including the pre-existing runner tests that pin `VerifyError` propagating unwrapped.

Then the whole forge suite:

Run: `uvx --with pytest pytest -q tests/`
Expected: PASS — the full count (1067 at Plan H's close, plus this plan's additions).

- [ ] **Step 12: Re-run under scrambled names, then mutate**

Rename every new `test_*` in `tests/test_forge_launch.py` and `tests/test_forge_seatrecord.py` to `test_zz0`…`test_zzN`, re-run, confirm the same count, **restore the names**. Then:

```bash
scripts/mutate.py --file shared/lib/forge/launch.py \
  --old '        seat_prompt = engine.apply_sentinel(prompt, token)' \
  --new '        seat_prompt = f"{prompt}\n\nquote {token}"' \
  -- uvx --with pytest pytest -q tests/test_forge_launch.py

scripts/mutate.py --file shared/lib/forge/launch.py \
  --old '        spec = seatmod.forge_spec(name, seat_prompt, timeout, cfg=cfg,\n                                  workdir=Path(seat_path))' \
  --new '        spec = engine.build_real_spec(name, seat_prompt, timeout, cfg, Path(seat_path))' \
  -- uvx --with pytest pytest -q tests/test_forge_launch.py

scripts/mutate.py --file shared/lib/forge/launch.py \
  --old '        spec.cwd = str(seat_path)' \
  --new '        pass' \
  -- uvx --with pytest pytest -q tests/test_forge_launch.py

scripts/mutate.py --file shared/lib/forge/seatrecord.py \
  --old '    if (cand.get("gate_delta") is None) != (cand.get("gate_surface") is None):' \
  --new '    if False:' \
  -- uvx --with pytest pytest -q tests/test_forge_seatrecord.py

scripts/mutate.py --file shared/lib/forge/seatrecord.py \
  --old '            raise SeatRecordError(f"{where}: prompt_identity: {e}") from e' \
  --new '            pi = None' \
  -- uvx --with pytest pytest -q tests/test_forge_seatrecord.py

scripts/mutate.py --file shared/lib/forge/runner.py \
  --old '        kept = _measured(result, taken["candidate"], taken["setup"])' \
  --new '        kept = result' \
  -- uvx --with pytest pytest -q tests/test_forge_runner.py
```

Expected: all six exit 0 (CAUGHT). `git status` clean afterwards.

- [ ] **Step 13: Add to the Makefile and commit**

Append `tests/test_forge_launch.py` and `tests/test_forge_seatrecord.py` to `FORGE_TESTS`, then:

```bash
make render
git add shared/lib/forge/launch.py shared/lib/forge/seatrecord.py \
        shared/lib/forge/runner.py tests/test_forge_launch.py \
        tests/test_forge_seatrecord.py tests/test_forge_runner.py Makefile marketplaces
make verify; echo "verify rc=$?"
make precommit; echo "precommit rc=$?"
```

Expected: both `rc=0`. Then:

```bash
git commit -m "$(cat <<'EOF'
feat(forge): a real adapter, a schema'd seat record, and the third dropped measurement closed

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UiV66Pt8cZVMq9t8WEAhpN
EOF
)"
```

---

## Deliberately out of scope

Everything that **decides**. §12's size gate, failure classification, progress tuple, oscillation detection, synthesis-fix cap and strongest-seat rubric; §13's reviewer input set, in-process `run_council`, `review_findings` record and bounded loop; §13.1's ultrareview. All six are Plan I₂, listed at the top of this document.

Also out, and carried forward rather than lost:

- **§16/§16.1 handover and provenance header, §15's `--gc`, the CLI (`--start`/`--collect`/`--gc`/`--no-ultra`), §18's `evals/llm-forge/`, §19's three council-engine items** — Plan J.
- **§22 Q5's memory measurement.** No concurrency cap exists; `gate.py` prices peak disk and nothing measures memory. A one-off: run a three-seat load, derive a concurrent-seat cap (plausibly 2 on this ~7.9 GB box), and make sure an OOM classifies as an **infrastructure** failure so §12.3 never reads it as non-progress.
- **The five carried debts** (§9's unrecorded remotes/config, `--gc`, `PASS` never reading `baseline_run`, nothing serializing `CandidateBundle`, `filter.<d>.clean`). None is on this plan's seam.
- **`fleet.clone_seat` does not pin `core.hooksPath` for a builder seat** the way `verify._hooks_pin` does for a verifier — same exploitable class as the fsmonitor hole, and a fleet change with its own blast radius.

---

## Self-Review

Run against the spec (§10, §10.1, §11, §20) and the design pass with fresh eyes.

**1. Spec coverage.**

| Requirement | Task |
|---|---|
| §20 task-bundle manifest: canonical relative paths, byte hashes, type, mode, symlink target, size caps, an entrypoint | 1 (`scan`, `BundleEntry`, `Quota.for_task_bundle`) |
| §20 materialize identically in every clone, modes preserved | 2 (`materialize` + `verify_materialized`) |
| §20 resolve and hash the live installed closures; named skill only when all three hash identically | 2 (`installed_closure`, `ambient_verdict`) |
| §20 bar ambient invocation | 2 (`ambient_note`, recorded as an instruction issued, **not** a mechanical bar) |
| §20 persist the resolved instruction + hashes for `--collect` | 1 (`write_task_bundle` / `read_task_bundle`) |
| §20 *do not automatically translate provider-specific tools*; *fail preflight for irreducibly provider-specific workflows* | **NOT IN THIS PLAN.** No translation is attempted — the bundle is copied verbatim, which satisfies the prohibition by construction. The preflight refusal needs `preflight.refusals` to learn a new reason and a front end to surface it; **it belongs with Plan J's CLI** and is recorded here so it is not lost. |
| §10 fourteen row fields, `rejected` first-class, content-derived ids, cycle-checked dependencies | 3 |
| §10 hard degradation rule with a recorded threshold | 3 (`DEGRADE_UNION_DIFF_BYTES` + two recorded fields) |
| §10 ledger hash for §14.1's checkpoint message | 3 (`ledger_hash`, with "hash, never rows" stated) |
| §10.1 four predicates; natural language may not be reported as checked | 4 |
| §10.1 the three-valued method enum | 4 (`METHODS`), paired with `satisfied` |
| §10 coverage asserts no accepted row contradicts a unanimous rejection | 4 (`_contradictions`, with the semantic reading explicitly declared unreachable) |
| §11 four per-seat values | 5 (`PromptIdentity`) |
| §11 classification conditioned on prompt identity; agreement across differently-prompted seats labelled weaker | 5 (`agreement_label`, `creditable`) — **the label is produced here; nothing consumes it until Plan I₂'s rubric.** §21 records that conditional agreement labelling was a candidate cut precisely because "nothing downstream acts on the label"; the owner kept it, and §11's last line keeps the fingerprints regardless. |
| The seat-record schema carrying §11's fingerprints | 6 (`seatrecord`) |
| `forge_spec` gets a production caller | 6 (`launch.make_launcher`) |
| `runner.verify_candidate`'s dropped measurements | 6 (`on_measurement`) |

**Gap found and closed by this review:** §20's preflight refusal for irreducibly provider-specific workflows has no task here. It is *not* silently dropped — it is named in the table above and in "Deliberately out of scope", with the reason (it needs a front end that does not exist) and the destination (Plan J).

**2. Placeholder scan.** One real defect was found and fixed inline: Task 6 Step 5's runner tests originally called a helper `_a_seat_result_with_launch` that **exists in no task and no file** — the skill's own "references to functions not defined in any task" failure. Rewritten against the helpers `tests/test_forge_runner.py` actually defines, each cited by line (`_open:79`, `_fake:36`, `_attempt:139`, `_confirmed:1157`, `_edit:1175`, `_per_seat:1179`), and the hooks-rig test it reuses is cited at `:950`. All three test bodies are now complete code. No `TBD`, no "add appropriate error handling", no "similar to Task N", no step that describes without showing, and no ellipsis anywhere in the plan.

**3. Type consistency.** Checked across tasks:
- `taskbundle.bundle_hash` (Task 1) → `fingerprint.build(bundle_sha256=…)` (Task 5) → `launch.make_launcher(bundle_sha256=…)` (Task 6). One `str | None` throughout.
- `taskbundle.installed_closure(cli) -> str | None` (Task 2) → `fingerprint.build(closure=…)` (Task 5) → `PromptIdentity.plugin_closure_sha256`. Consistent.
- `ledger.Criterion` (Task 3) fields `kind/text/path/symbol/node_id/sha256/trace` are exactly what `coverage.evaluate` reads (Task 4) — including `trace`, which Task 3 adds and Task 4 consumes for `prose`/`schema` only.
- `ledger.CRITERION_KINDS` (Task 3) and `coverage.evaluate`'s branches (Task 4) cover the same five names; `evaluate` raises `CoverageError` on any sixth, so adding a kind in Task 3 without an evaluator fails loudly.
- `fingerprint.as_row` / `from_row` (Task 5) are the pair `launch` writes (Task 6) and `seatrecord._attempt` reads (Task 6). Same eight keys.
- `runner._record`'s new `"prompt_identity"` key (Task 6) is in `seatrecord.Attempt`'s field list (Task 6), and `_write` proves it by decoding.
- `coverage.METHODS` uses §10.1's exact spellings — `mechanically_checked`, `manual_trace_confirmed`, `unresolved`.

**4. Naming traps checked.** `PromptIdentity` is never `identity`; `runner`/`fleet`'s `identity` parameter is untouched. `taskbundle` is never `bundle` — `bundle.py` is the *candidate* bundle, flowing the opposite direction, and both are imported in Task 1 (`from . import bundle as bundlemod`).

---

## Execution Handoff

Plan complete. Two execution options:

1. **Subagent-Driven (recommended)** — a fresh subagent per task, with a two-stage review between tasks. This project's eight prior plans all ran this way, **strictly sequential** (never two implementers in flight: `make render` reads the working tree, so a concurrent edit lands in another agent's rendered output even with disjoint file sets).
2. **Inline Execution** — execute the tasks in one session using `superpowers:executing-plans`, with checkpoints for review.

Brief every implementer and reviewer with the standing defect-pattern list, and in particular: **the plan's own draft code has been wrong in every task of every plan so far — treat this document as a set of hypotheses and decline with a measurement.**
