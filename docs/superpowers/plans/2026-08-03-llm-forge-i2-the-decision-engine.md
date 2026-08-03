# llm-forge Plan I₂: the decision engine

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn Plan I's evidence record into decisions — which strategy the measured artifact size selects, whether a synthesis attempt made progress or is oscillating, which seat is strongest, what three independent reviewers found, and which of `ready` / `degraded` / `review_blocked` the run ends in — with every verdict refusing to be cleaner than the evidence under it.

**Architecture:** Plan I measured and decided nothing. This plan adds six pure-ish modules that consume Plan I's frozen values (`coverage.Report`, `seatrecord.SeatRecord`, `ledger.Ledger`, `taskbundle.TaskBundle`, `fingerprint.PromptIdentity`) plus `verify`'s outcomes and `journal`'s events, and produce named verdicts. Every module's central risk is the same one: the obvious implementation answers "we could not tell" with the same value it uses for "no", and the run then reads clean. Every comparison in this plan is therefore **three-valued**, and every unmeasured input is `None` rather than an empty success.

**Tech Stack:** Python 3.11+ stdlib only. `git` 2.53.0 via `shared/lib/forge/gitcmd.py`. The council engine at `shared/lib/council/engine.py` for reviewer invocation. pytest via `uvx --with pytest pytest -q` (1363 currently passing).

---

## SCOPE NOTE — six tasks hold, but two of them are heavy. Read this before Task 1.

The six-task decomposition at `docs/superpowers/plans/2026-08-03-llm-forge-i-the-decision-engine.md:20-27` is the agreed one and this document follows it. Two honest warnings:

- **Task 2 carries two deliverables** and they are committed **separately**: the synthesis-fix cap has to be threaded through `gate.Quote` → `gate.Confirmation` → `runstate.Manifest` (a schema change touching five test call sites), and only then can `progress.py` read it. Steps 1–6 are the cap; steps 7–17 are `progress.py`. Two commits, one task, because the cap has no independent reviewer question — it is the budget the loop spends.
- **Task 4 is the largest task in this plan.** It builds the reviewer bundle, the ledger-exclusion assertion, three `ProviderSpec`s, the in-process `run_council` call with its three measured hazards, the findings parser and the durable record. It is not split because every piece is load-bearing for the *first* review call — a Task 4 that stopped short of the record would produce a review whose findings live only in the council's non-durable `write_text` output, which is the state §13 is written against.

**Nothing in this plan is wired into `runner.run`.** `runner.run` still stops at `comparing` (`runner.py:1546`, and its docstring says so). The synthesis executor, the checkpoint writer and the `--collect` front end are Plan J. Every module here takes its inputs as arguments and its side-effecting collaborators (providers, `subprocess.run`, verify, fix) as **injected parameters with real defaults**, so this plan's suite never spends a provider call.

---

## Global Constraints

Every task's requirements implicitly include this section.

- **Python stdlib only.** No pip dependencies. `tomllib`, `json`, `hashlib`, `os`, `re`, `subprocess`, `shutil`, `dataclasses`, `pathlib`.
- **Commands run as argv lists, never through a shell.** No `shell=True`, no string-joined commands.
- **Git only via `shared/lib/forge/gitcmd.py`.** Git is located **by asking git** — `gitcmd.git(path, "rev-parse", "--absolute-git-dir")` — **never by string-joining `.git`** onto a path. Top-level `-c` presets (`gitcmd.NO_DAEMON_CACHE`, `gitcmd.NO_HOOKS`) are splatted **before** the subcommand: `gitcmd.git(root, *gitcmd.NO_DAEMON_CACHE, "diff", …)`. Measured: after the subcommand git answers `error: unknown switch 'c'`, rc 129.
- **Fail closed.** A measurement that could not be taken is `None`/UNKNOWN, never an empty success. `None` never compares equal to `None` for "these are the same".
- **A verdict must never read cleaner than its evidence.**
- **A comment asserting something the code does not do is a defect.**
- **No test may invoke a real provider or spend money.** Providers are injected; tests pass fakes.
- `shared/lib/forge/**` is source of truth; `marketplaces/**` is render output. **Never hand-edit `marketplaces/`** — run `make render`.
- **Every task ends with:** `make render`, then an explicit-pathspec `git add` **including `marketplaces`**, then `make verify` and `make precommit` **run unpiped with `$?` captured**, then the commit. A pipe reports the pipe's exit status.
- **Use `scripts/mutate.py`** for mutation testing. It refuses any test-command status other than 1 (0 is SURVIVED; 2/3/4/5 are the run failing to happen and are refused by name). Its `--old`/`--new` do **not** decode `\n` — use `$'...'` for a multi-line pattern. **Check `git status` before and after any mutation wave** — a killed run leaves the tree mutated and the next suite is green for the wrong reason.
- **Tests run via `uvx --with pytest pytest -q`** (no system pytest is assumed).
- **New test files are added to `FORGE_TESTS` in the `Makefile` in the task that creates them.**
- **Touching `shared/lib/council/` stales `llm-council`'s eval receipt.** No task in this plan changes `shared/lib/council/`. If one becomes necessary, put it on its **own commit** and gate it with `shared/skills/llm-council/scripts/fanout.py --self-test` plus `make smoke-llm-council` — **not** the judge harness (`docs/skill-eval-process.md`). Do not run `make eval SKILL=llm-council` for a council code change; it is not the gate for that skill.

---

## Decisions already taken. Build on these; do not reopen them.

1. **§13's codex reviewer uses `codex exec --json`, not `codex review`.** Measured: `codex review [OPTIONS] [PROMPT]` has **no `--json`, no `--model`, no `--cd`**, so the engine's existing `extract_codex_json` turns every review into a silent `parse_failure` — making "found nothing" indistinguishable from "could not be read". Forge supplies the review framing itself, as the prompt. This is a recorded deviation from §13's text and is **not** to be re-litigated. Task 4.
2. **`claude ultrareview --timeout` is in MINUTES** (measured 2026-08-03 on this machine: `--timeout <minutes>  Maximum minutes to wait for the review to finish (default: 30)`). Every other timeout in forge and the council is seconds. Task 6.
3. **The ledger's bytes must exist under NO clone root** — not a seat, not the synthesis checkout, not a verifier clone, and above all not inside the §20 task bundle, which reviewers *are* given. §13 sets every reviewer's cwd to the synthesis checkout and a reviewer has a shell, so the guarantee must be **structural** (the bytes are not in the tree), never textual (an instruction not to read them). **Task 4 owes the mechanical assertion.**
4. **The fingerprint type is `PromptIdentity`, never `identity`.** `fleet.clone_seat(..., identity=…)` and `runner.run_seat(..., identity=…)` already mean the git author `(name, email)` pair.

---

## Decisions this plan takes, with reasons. Three of them settle recorded contradictions.

- **Contradiction 5 — one string, one constant.** §13 and §14.2 say "verified but not independently **reviewed**"; §13.1 says "**re-reviewed**". Two spellings of one predicate. **`review.VERIFIED_NOT_INDEPENDENTLY_REVIEWED = "verified but not independently reviewed"`** is declared once in Task 5 and imported by Task 6. A test asserts no other module spells the phrase itself.
- **Contradiction 6 — §13.1 and §14.2 assigned different terminals to identical evidence.** §14.2 makes a successful post-round-2 fix terminal `review_blocked`; §13.1 says its own fix creates "no new loop, no new state", which lands an otherwise-clean run in `ready`. Same facts, two terminals. **Resolution: the terminal is decided by the finding's *resolution*, not by which reviewer found it.** An **unresolved blocker** → `review_blocked`. A **fixed-but-not-re-reviewed** finding → `degraded`, whether it came from review round 2 or from ultrareview. This uses §14's own vocabulary (`degraded` is already a declared successor of `reviewing`, `runstate.py:1029-1030`), gives identical evidence one terminal, and keeps `review_blocked` meaning what §13's last paragraph needs it to mean: *a blocker is still open*.
- **Contradiction 7 — §16's out-of-band artifact manifest is a Plan-J artifact.** §13's reviewer input set names it. Task 4 takes it as a parameter typed `str | None`; when it is `None` the review instructions **say so in the reviewer's own bundle** rather than silently shipping a four-item input set described as five. A reviewer told "no artifact manifest was produced for this run" can weigh that; one never told it cannot.
- **§13.1's five unavailability reasons do not cover an exit-0 whose `--json` payload cannot be read.** Task 6 adds a sixth, `unreadable_output`, and says why: folding it into "ran, found no bugs" is this project's exact false green. This is a deliberate, flagged extension of the spec's list.
- **The size gate's above-threshold branch produces no strategy.** §12.1's "stable seams exist" is §10.1's non-mechanical criterion. Above the threshold Task 1 returns `strategy=None, method="unresolved"` and offers a separate recorded-judgement door. It never guesses `partition` or `base_and_port` from a number.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `shared/lib/forge/strategy.py` *(new, Task 1)* | §12.1's size measurement over the candidate set; §12's strategy rule applied to it; §12.3's three-way failure classification and its fallback disposition. |
| `shared/lib/forge/progress.py` *(new, Task 2)* | §12.3's progress tuple, its three-outcome comparison, oscillation detection read back out of `events.jsonl`, and the synthesis-fix cap's remaining budget. |
| `shared/lib/forge/rubric.py` *(new, Task 3)* | §12.5's strongest-seat rubric over recorded dimensions, total by construction; §12.4's coverage-as-fallback-trigger. |
| `shared/lib/forge/review.py` *(new, Tasks 4 & 5)* | §13's reviewer bundle, the structural ledger-exclusion assertion, the three reviewer specs, the in-process `run_council` call, the findings parser, the durable `review_findings` record (Task 4); the bounded round-1/round-2 loop and the terminal read off the record (Task 5). |
| `shared/lib/forge/ultra.py` *(new, Task 6)* | §13.1's ultrareview: minutes-not-seconds timeout, six unavailability reasons, diff pre-flight, `--no-ultra`. |
| `shared/lib/forge/gate.py` *(modified, Task 2)* | `Quote`/`Confirmation` carry `review_rounds` and `synthesis_fix_cap`. |
| `shared/lib/forge/runstate.py` *(modified, Task 2)* | `Manifest` records the same two numbers, type-checked on the way back. |
| `tests/test_forge_strategy.py`, `test_forge_progress.py`, `test_forge_rubric.py`, `test_forge_review.py`, `test_forge_ultra.py` *(new)* | One per module; each added to `FORGE_TESTS`. |

---
### Task 1: The size gate decides a strategy, and a verify failure gets a class

**Files:**
- Create: `shared/lib/forge/strategy.py`
- Create: `tests/test_forge_strategy.py`
- Modify: `Makefile:21-32` (add the new test file to `FORGE_TESTS`)

**Interfaces:**

- **Consumes** (all already exist, nothing from a later task):
  - `bundle.CandidateBundle` — fields `version, baseline_ref, baseline_commit, tracked_patch: bytes, sidecars: tuple[SidecarEntry, ...], gate_delta: tuple|None, gate_surface: tuple|None, generator_contract_id: str, omitted: tuple[str, ...]` (`shared/lib/forge/bundle.py:94-130`).
  - `bundle.SidecarEntry` — `path: str, kind: str, mode: int, payload: bytes` (`bundle.py:74-90`).
  - `gitcmd.git(repo, *args, env_extra=None, check=True, binary=False, timeout=60, user_config=False)` and `gitcmd.READONLY` (`gitcmd.py:171`, `:25`).
  - `verify.OUTCOMES = (PASS, BASELINE_RED_NO_NEW_IDENTIFIED_FAILURE, FAIL, FLAKY, HARVEST_INCOMPLETE, GATE_CHANGED)` (`verify.py:118-128`).
  - `coverage.Report` — `results, contradictions, unsatisfied, unresolved` (all tuples) (`coverage.py:119-138`).
  - `coverage.METHODS = ("mechanically_checked", "manual_trace_confirmed", "unresolved")` (`coverage.py:40`).
  - `gate.STRATEGY_RULES = ("size-gated", "fusion", "base-and-port")` (`gate.py:946`), which is what `Confirmation.strategy` holds.
- **Produces** (later tasks rely on exactly these names):
  - `strategy.Size(changed_lines: int | None, changed_files: int | None, unmeasured: tuple[str, ...])`
  - `strategy.measure(repo, candidates: dict[str, bundle.CandidateBundle]) -> Size`
  - `strategy.Decision(strategy: str | None, method: str, detail: str)`
  - `strategy.decide(rule: str, size: Size) -> Decision`
  - `strategy.recorded_seam_analysis(chosen: str, rationale: str) -> Decision`
  - `strategy.FROM_SCRATCH = "from_scratch"`, `PARTITION = "partition"`, `BASE_AND_PORT = "base_and_port"`, `STRATEGIES`
  - `strategy.classify_failure(outcome: str, *, report: coverage.Report | None) -> tuple[str | None, str]`
  - `strategy.INFRASTRUCTURE = "infrastructure"`, `SYNTHESIS_INTRODUCED = "synthesis_introduced"`, `REQUIREMENT_GAP = "requirement_gap"`, `FAILURE_CLASSES`
  - `strategy.fallback_disposition(failure_class: str | None) -> str` returning one of `PERMITTED = "permitted"`, `REFUSED = "refused"`, `UNDECIDABLE = "undecidable"`
  - `strategy.StrategyError(RuntimeError)`
  - Task 3 consumes `Size.changed_lines`/`Size.changed_files` as the rubric's `diff_complexity` dimension.

**The input that would make this read cleaner than its evidence.** A candidate set whose patch forge cannot count — a **binary file**, a non-empty `omitted`, or a `git apply --numstat` that fails. Measured on git 2.53.0, `git apply --numstat -z` emits `-\t-\tb.bin` for a binary change: an implementation that `int()`s those cells or skips them counts a whole-blob rewrite as **zero changed lines**, the size lands under 400/15, and the run picks from-scratch fusion on a measurement nobody took. The second one: a `FAIL` whose coverage report is entirely `unresolved` — nothing was checkable — read as `synthesis_introduced`, which is the one class that *permits* fallback. Both are closed below, and both have a mutation in Step 8.

---

- [ ] **Step 1: Write the failing tests for the size measurement**

Create `tests/test_forge_strategy.py`:

```python
"""§12.1's size gate and §12.3's failure classification."""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

from forge import bundle, coverage, ledger, strategy, verify  # noqa: E402

from forge_fixtures import commit_all, global_identity, make_repo, write  # noqa: E402,F401


def _repo(tmp_path):
    r = make_repo(tmp_path)
    write(r, "keep.txt", "x\n")
    commit_all(r, "base")
    return r


def _patch(repo, edits) -> bytes:
    """A real tracked patch: apply `edits`, stage, ask git for the staged diff, reset."""
    for rel, text in edits.items():
        (repo / rel).write_bytes(text if isinstance(text, bytes) else text.encode())
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True,
                   capture_output=True)
    out = subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--binary"],
                         check=True, capture_output=True).stdout
    subprocess.run(["git", "-C", str(repo), "reset", "--hard", "HEAD"], check=True,
                   capture_output=True)
    return out


def _cand(patch=b"", sidecars=(), omitted=()):
    return bundle.CandidateBundle(version=1, baseline_ref="refs/x", baseline_commit="0" * 40,
                                  tracked_patch=patch, sidecars=tuple(sidecars),
                                  omitted=tuple(omitted))


def test_a_text_patch_is_counted_in_lines_and_files(tmp_path, global_identity):
    r = _repo(tmp_path)
    p = _patch(r, {"keep.txt": "a\nb\nc\n", "new.txt": "n\n"})
    s = strategy.measure(r, {"claude": _cand(p)})
    assert s.unmeasured == ()
    assert s.changed_files == 2
    # keep.txt: 3 added / 1 deleted; new.txt: 1 added / 0 deleted.
    assert s.changed_lines == 5


def test_a_binary_patch_makes_the_line_count_unknown_not_zero(tmp_path, global_identity):
    """THE FAIL-OPEN. `git apply --numstat` prints `-` for a binary file (measured, git
    2.53.0). Counting it as 0 puts a whole-blob rewrite under the 400-line threshold."""
    r = _repo(tmp_path)
    p = _patch(r, {"blob.bin": b"\x00\x01\x02\x03\x04"})
    s = strategy.measure(r, {"claude": _cand(p)})
    assert s.changed_lines is None
    assert s.changed_files == 1          # the PATH is still measured; only the lines are not
    assert any("blob.bin" in u for u in s.unmeasured)


def test_an_omitted_artifact_makes_the_whole_size_unknown(tmp_path, global_identity):
    r = _repo(tmp_path)
    p = _patch(r, {"keep.txt": "a\n"})
    s = strategy.measure(r, {"claude": _cand(p, omitted=("secret.link",))})
    assert s.changed_lines is None and s.changed_files is None
    assert any("omitted" in u for u in s.unmeasured)


def test_a_text_sidecar_counts_and_a_binary_one_does_not(tmp_path, global_identity):
    r = _repo(tmp_path)
    txt = bundle.SidecarEntry(path="doc.md", kind="file", mode=0o644, payload=b"a\nb\n")
    binr = bundle.SidecarEntry(path="img.png", kind="file", mode=0o644, payload=b"\xff\xfe\x00")
    ok = strategy.measure(r, {"claude": _cand(sidecars=[txt])})
    assert ok.changed_lines == 2 and ok.changed_files == 1 and ok.unmeasured == ()
    bad = strategy.measure(r, {"claude": _cand(sidecars=[txt, binr])})
    assert bad.changed_lines is None
    assert bad.changed_files == 2
    assert any("img.png" in u for u in bad.unmeasured)


def test_the_union_is_over_paths_not_a_sum_over_seats(tmp_path, global_identity):
    """§12.1 sizes the CHANGE, and three seats editing one file is one changed file."""
    r = _repo(tmp_path)
    p = _patch(r, {"keep.txt": "a\n"})
    s = strategy.measure(r, {"claude": _cand(p), "codex": _cand(p), "agy": _cand(p)})
    assert s.changed_files == 1


def test_an_empty_candidate_set_is_unknown_not_zero(tmp_path, global_identity):
    r = _repo(tmp_path)
    s = strategy.measure(r, {})
    assert s.changed_lines is None and s.changed_files is None
    assert any("no candidate" in u for u in s.unmeasured)
```

- [ ] **Step 2: Run them to verify they fail**

```bash
uvx --with pytest pytest -q tests/test_forge_strategy.py
```

Expected: FAIL — `ModuleNotFoundError: No module named 'forge.strategy'` (collection error, exit 2).

- [ ] **Step 3: Write the size measurement**

Create `shared/lib/forge/strategy.py`:

```python
"""§12's strategy decision and §12.3's failure classification.

WHAT THIS MODULE REFUSES TO DO. §12.1 says the size gate "triggers analysis, it does not
force partitioning", and admits a partition only "where stable seams exist" — which §10.1
names as the kind of natural-language criterion that must never be presented as a checked
predicate. So above the threshold this module returns NO strategy and method `unresolved`.
The door to a strategy up there is `recorded_seam_analysis`, which takes a rationale and
records `manual_trace_confirmed`, exactly as §12.1 requires.

THE SIZE IS A UNION OVER PATHS, NOT A SUM OVER SEATS. Three seats editing one file is one
changed file; summing the fleet would treat agreement as bulk and push every three-seat run
over the threshold. Lines are summed per path across the seats' patches, because two seats
touching the same file really did produce two different amounts of change and the larger is
not a safe stand-in for either.
"""
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import bundle as bundlemod, coverage, gitcmd, verify

# §12.1's thresholds, verbatim: "Below ~400 changed lines / ~15 files, from-scratch fusion."
SIZE_LINE_THRESHOLD = 400
SIZE_FILE_THRESHOLD = 15

FROM_SCRATCH = "from_scratch"
PARTITION = "partition"
BASE_AND_PORT = "base_and_port"
STRATEGIES = (FROM_SCRATCH, PARTITION, BASE_AND_PORT)

INFRASTRUCTURE = "infrastructure"
SYNTHESIS_INTRODUCED = "synthesis_introduced"
REQUIREMENT_GAP = "requirement_gap"
FAILURE_CLASSES = (INFRASTRUCTURE, SYNTHESIS_INTRODUCED, REQUIREMENT_GAP)

PERMITTED = "permitted"
REFUSED = "refused"
UNDECIDABLE = "undecidable"
DISPOSITIONS = (PERMITTED, REFUSED, UNDECIDABLE)


class StrategyError(RuntimeError):
    """A strategy question this module will not answer on the evidence it was given."""


@dataclass(frozen=True)
class Size:
    """§12.1's measured artifact size, with the reasons it is partly or wholly unknown.

    `changed_lines` and `changed_files` are INDEPENDENTLY nullable, and that is not a
    convenience: a binary file has a countable PATH and an uncountable line delta, and
    collapsing both to None would throw away a measurement that was taken. `unmeasured` is
    never empty when either is None, and a reader that prints the size prints these lines
    beside it.
    """
    changed_lines: int | None
    changed_files: int | None
    unmeasured: tuple[str, ...]

    def __post_init__(self) -> None:
        if (self.changed_lines is None or self.changed_files is None) and not self.unmeasured:
            raise StrategyError(
                "a size with an unknown dimension must say what could not be measured; an "
                "unexplained None is a gap a reader cannot weigh")
        if self.unmeasured and self.changed_lines is not None and self.changed_files is not None:
            raise StrategyError(
                "this size names things it could not measure and yet reports both dimensions; "
                "one of the two statements is false")


def _numstat(repo, patch: bytes) -> tuple[dict, tuple[str, ...]]:
    """`git apply --numstat -z` over one patch: {path: lines} plus what could not be counted.

    THE PARSE IS GIT'S, NOT A HAND-WRITTEN ONE, for `bundle._covered`'s reason: a
    `diff --git` header C-quotes an unusual path, so reading names off the header text means
    reimplementing `unquote_c_style`. Under `-z` git emits `<added> TAB <deleted> TAB <path>
    NUL` with the name raw.

    A `-` CELL IS A BINARY FILE AND IT IS NOT ZERO. Measured on git 2.53.0 against a patch
    touching one text and one binary file:

        -\tb.bin
        2\t1\tt.txt

    `int("-")` raises and a `try/except: continue` would silently drop the path's lines,
    which is how a whole-blob rewrite lands under a 400-line threshold. The path is still
    counted; only its lines are refused, and the refusal is named.
    """
    if not patch:
        return {}, ()
    lines, unmeasured = {}, []
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "candidate.patch"
        f.write_bytes(patch)
        r = gitcmd.git(repo, "apply", "--numstat", "-z", str(f),
                       env_extra=gitcmd.READONLY, check=False, binary=True)
        if r.returncode != 0:
            return {}, (f"git apply --numstat -> {r.returncode}: "
                        f"{r.stderr.decode('utf-8', 'replace').strip()}",)
        for rec in r.stdout.decode("utf-8", "surrogateescape").split("\0"):
            if not rec:
                continue
            # split(maxsplit=2): a path may itself contain a TAB, the two counts never can.
            added, deleted, path = rec.split("\t", 2)
            if added == "-" or deleted == "-":
                lines.setdefault(path, None)
                unmeasured.append(
                    f"{path}: git reports a binary delta (`-`), so its changed-line count is "
                    "not a number this run measured")
                continue
            prior = lines.get(path)
            n = int(added) + int(deleted)
            lines[path] = n if prior is None and path not in lines else (prior or 0) + n
    return lines, tuple(unmeasured)


def _sidecar_lines(e) -> tuple[int | None, str | None]:
    """One sidecar's changed-line count, or None with the reason it has none.

    A symlink is one line — its target text — which is `baseline`'s own reading of a link
    (D-1: "a symlink IS its target text"). A file whose payload is not valid UTF-8 has no
    line count at all; counting `\\n` bytes in a PNG is a number with no meaning, and a number
    with no meaning is what the threshold would then compare.
    """
    if e.kind == "symlink":
        return 1, None
    try:
        text = e.payload.decode("utf-8")
    except UnicodeDecodeError:
        return None, (f"{e.path}: this sidecar's payload is not UTF-8, so it has no "
                      "changed-line count; counting newline bytes in binary is not a measurement")
    if not text:
        return 0, None
    return text.count("\n") + (0 if text.endswith("\n") else 1), None


def measure(repo, candidates) -> Size:
    """§12.1's size over the whole candidate set, refusing every count it cannot take.

    `repo` is any git repository the patches can be `git apply --numstat`'d against — the
    numstat pass does not apply anything and does not need the patch to be applicable, only
    parseable. A verifier clone or the baseline clone is the natural one.

    A NON-EMPTY `omitted` VOIDS THE WHOLE SIZE. `CandidateBundle.omitted` is what the harvest
    could not carry, and §6.2 already treats it as displacing every other verdict
    (`verify.classify` returns HARVEST_INCOMPLETE before it reads the exit code). A size
    computed over a bundle that is missing artifacts is an undercount presented as a total,
    which is the shape §12.1's threshold is least able to survive.
    """
    if not isinstance(candidates, dict):
        raise StrategyError(f"the candidate set is a mapping of seat -> CandidateBundle, "
                            f"not {type(candidates).__name__}")
    if not candidates:
        return Size(None, None, (
            "no candidate was supplied, so there is nothing to size; an empty set measuring "
            "0 lines and 0 files would select from-scratch fusion over no evidence at all",))
    wrong = sorted({name for name, c in candidates.items()
                    if not isinstance(c, bundlemod.CandidateBundle)})
    if wrong:
        raise StrategyError(f"these are not CandidateBundles: {wrong}")

    omitted = sorted({p for c in candidates.values() for p in c.omitted})
    if omitted:
        return Size(None, None, (
            f"{len(omitted)} artifact(s) were omitted from the harvest ({omitted[:5]}), so "
            "the candidate set is incomplete and any size over it is an undercount reported "
            "as a total",))

    per_path: dict = {}
    unmeasured: list = []
    for name in sorted(candidates):
        c = candidates[name]
        lines, why = _numstat(repo, c.tracked_patch)
        unmeasured.extend(f"{name}: {w}" for w in why)
        for path, n in lines.items():
            if n is None or per_path.get(path, 0) is None:
                per_path[path] = None
            else:
                per_path[path] = per_path.get(path, 0) + n
        for e in c.sidecars:
            n, why_one = _sidecar_lines(e)
            if why_one:
                unmeasured.append(f"{name}: {why_one}")
            if n is None or per_path.get(e.path, 0) is None:
                per_path[e.path] = None
            else:
                per_path[e.path] = per_path.get(e.path, 0) + n

    changed_files = len(per_path)
    changed_lines = None if any(v is None for v in per_path.values()) \
        else sum(per_path.values())
    if changed_lines is None and not unmeasured:
        # Belt and braces: `per_path` only ever holds None where a reason was appended, and
        # this refuses the state where that stops being true rather than shipping a silent one.
        unmeasured.append("a path's changed-line count is unknown and nothing recorded why")
    return Size(changed_lines, changed_files, tuple(unmeasured))
```

- [ ] **Step 4: Run the size tests**

```bash
uvx --with pytest pytest -q tests/test_forge_strategy.py
```

Expected: `6 passed`.

- [ ] **Step 5: Write the failing tests for the decision and the failure classification**

Append to `tests/test_forge_strategy.py`:

```python
# --------------------------------------------------------------------------- #
# §12's strategy rule, applied to a measured size.
# --------------------------------------------------------------------------- #
def test_a_small_measured_change_selects_from_scratch_mechanically():
    d = strategy.decide("size-gated", strategy.Size(10, 2, ()))
    assert d.strategy == strategy.FROM_SCRATCH
    assert d.method == "mechanically_checked"


def test_an_unmeasured_size_never_selects_from_scratch():
    """THE FAIL-OPEN. A size nobody could take is not a small one."""
    d = strategy.decide("size-gated", strategy.Size(None, 2, ("a binary delta",)))
    assert d.strategy is None
    assert d.method == "unresolved"
    assert "binary" in d.detail


def test_above_the_threshold_no_strategy_is_produced():
    over = strategy.decide("size-gated", strategy.Size(401, 2, ()))
    assert over.strategy is None and over.method == "unresolved"
    assert strategy.decide("size-gated", strategy.Size(10, 16, ())).strategy is None


def test_the_thresholds_are_exclusive_at_the_boundary():
    assert strategy.decide("size-gated", strategy.Size(400, 15, ())).strategy is None
    assert strategy.decide("size-gated", strategy.Size(399, 14, ())).strategy \
        == strategy.FROM_SCRATCH


def test_a_gate_confirmed_rule_is_a_recorded_human_decision_not_a_measurement():
    for rule, expected in (("fusion", strategy.FROM_SCRATCH),
                           ("base-and-port", strategy.BASE_AND_PORT)):
        d = strategy.decide(rule, strategy.Size(None, None, ("nothing was measured",)))
        assert d.strategy == expected
        assert d.method == "manual_trace_confirmed"


def test_an_unknown_rule_is_refused():
    with pytest.raises(strategy.StrategyError):
        strategy.decide("whatever-seems-best", strategy.Size(1, 1, ()))


def test_a_seam_analysis_is_recorded_manually_and_never_mechanically():
    d = strategy.recorded_seam_analysis(strategy.PARTITION, "the API boundary is frozen")
    assert d.method == "manual_trace_confirmed" and d.strategy == strategy.PARTITION
    with pytest.raises(strategy.StrategyError):
        strategy.recorded_seam_analysis(strategy.PARTITION, "   ")
    with pytest.raises(strategy.StrategyError):
        strategy.recorded_seam_analysis(strategy.FROM_SCRATCH, "it looked small")


def test_a_decision_cannot_carry_a_method_its_strategy_does_not_support():
    with pytest.raises(strategy.StrategyError):
        strategy.Decision(strategy.PARTITION, "mechanically_checked", "no predicate says this")
    with pytest.raises(strategy.StrategyError):
        strategy.Decision(strategy.FROM_SCRATCH, "unresolved", "unresolved carries no strategy")
    with pytest.raises(strategy.StrategyError):
        strategy.Decision(None, "manual_trace_confirmed", "a confirmed nothing")


# --------------------------------------------------------------------------- #
# §12.3's three-way failure classification.
# --------------------------------------------------------------------------- #
def _report(*results):
    rs = tuple(results)
    return coverage.Report(rs,
                           (),
                           tuple(f"{r.row_id}[{r.criterion_index}]: {r.detail}"
                                 for r in rs if r.satisfied is False),
                           tuple(f"{r.row_id}[{r.criterion_index}]: {r.detail}"
                                 for r in rs if r.method == "unresolved"))


def _ok(row="a"):
    return coverage.Result(row, 0, "mechanically_checked", True, "the named test passed")


def _bad(row="a"):
    return coverage.Result(row, 0, "mechanically_checked", False, "the named test failed")


def _unknown(row="a"):
    return coverage.Result(row, 0, "unresolved", None, "no predicate exists for this row")


def test_infrastructure_outcomes_are_classified_and_never_permit_fallback():
    for outcome in (verify.BASELINE_RED_NO_NEW_IDENTIFIED_FAILURE, verify.HARVEST_INCOMPLETE):
        cls, why = strategy.classify_failure(outcome, report=_report(_ok()))
        assert cls == strategy.INFRASTRUCTURE, outcome
        assert strategy.fallback_disposition(cls) == strategy.REFUSED
        assert why


def test_a_flaky_or_gate_changed_outcome_classifies_as_nothing():
    for outcome in (verify.FLAKY, verify.GATE_CHANGED):
        cls, why = strategy.classify_failure(outcome, report=_report(_ok()))
        assert cls is None, outcome
        assert strategy.fallback_disposition(cls) == strategy.UNDECIDABLE
        assert why


def test_a_fail_with_an_unsatisfied_claim_is_a_requirement_gap():
    cls, _ = strategy.classify_failure(verify.FAIL, report=_report(_ok(), _bad("b")))
    assert cls == strategy.REQUIREMENT_GAP
    assert strategy.fallback_disposition(cls) == strategy.PERMITTED


def test_a_fail_whose_claims_all_checked_clean_is_synthesis_introduced():
    cls, _ = strategy.classify_failure(verify.FAIL, report=_report(_ok(), _ok("b")))
    assert cls == strategy.SYNTHESIS_INTRODUCED
    assert strategy.fallback_disposition(cls) == strategy.PERMITTED


def test_a_fail_over_an_unresolved_report_classifies_as_nothing():
    """THE FAIL-OPEN. `unresolved` means nobody could check. Reading it as 'the claims are
    fine, so synthesis broke it' permits fallback on a measurement never taken."""
    cls, why = strategy.classify_failure(verify.FAIL, report=_report(_ok(), _unknown("b")))
    assert cls is None
    assert strategy.fallback_disposition(cls) == strategy.UNDECIDABLE
    assert "unresolved" in why


def test_a_fail_with_no_report_at_all_classifies_as_nothing():
    cls, why = strategy.classify_failure(verify.FAIL, report=None)
    assert cls is None and "no coverage report" in why


def test_a_contradicted_ledger_is_a_requirement_gap_whatever_the_criteria_say():
    r = coverage.Report((_ok(),), ("row b contradicts a unanimous rejection",), (), ())
    cls, _ = strategy.classify_failure(verify.FAIL, report=r)
    assert cls == strategy.REQUIREMENT_GAP


def test_a_passing_outcome_has_no_failure_to_classify():
    cls, why = strategy.classify_failure(verify.PASS, report=_report(_ok()))
    assert cls is None and "not a failure" in why


def test_an_outcome_outside_the_declared_set_is_refused():
    with pytest.raises(strategy.StrategyError):
        strategy.classify_failure("MOSTLY_FINE", report=None)


def test_every_verify_outcome_has_a_declared_reading():
    """A new §6.2 outcome must fail loudly here rather than sort as unknown."""
    for outcome in verify.OUTCOMES:
        strategy.classify_failure(outcome, report=_report(_ok()))


def test_an_undeclared_failure_class_is_refused_by_the_disposition():
    with pytest.raises(strategy.StrategyError):
        strategy.fallback_disposition("probably_fine")
```

- [ ] **Step 6: Run them to verify they fail**

```bash
uvx --with pytest pytest -q tests/test_forge_strategy.py
```

Expected: FAIL — `AttributeError: module 'forge.strategy' has no attribute 'decide'`.

- [ ] **Step 7: Write the decision and the failure classification**

Append to `shared/lib/forge/strategy.py`:

```python
@dataclass(frozen=True)
class Decision:
    """Which strategy, and on what kind of evidence — §10.1's method axis, reused verbatim.

    THE PAIRING IS ENFORCED HERE, in `coverage.Result`'s footsteps and for its reason: a rule
    that holds only on the path its author remembered is what this package refuses everywhere
    else, and `Decision` is a public dataclass a later phase will build for itself.

      * `mechanically_checked` belongs to exactly one branch — the size gate found both
        dimensions under §12.1's thresholds. That is a numeric comparison over a measured
        value, which is what "mechanical" means here.
      * `manual_trace_confirmed` carries a strategy a HUMAN chose: the §5 gate's confirmed
        rule, or a recorded seam analysis. §12.1 requires exactly this label for the partition
        decision and forbids presenting it as a checked predicate.
      * `unresolved` carries NO strategy. It is the state above the threshold before anyone
        has looked at the seams, and the state where the size could not be measured at all.
    """
    strategy: str | None
    method: str
    detail: str

    def __post_init__(self) -> None:
        if self.method not in coverage.METHODS:
            raise StrategyError(f"method is one of {list(coverage.METHODS)}, "
                                f"not {self.method!r}")
        if self.strategy is not None and self.strategy not in STRATEGIES:
            raise StrategyError(f"strategy is one of {list(STRATEGIES)} or None, "
                                f"not {self.strategy!r}")
        if not isinstance(self.detail, str) or not self.detail.strip():
            raise StrategyError("a decision says why; an empty detail is a verdict with no "
                                "evidence attached to it")
        if self.method == "unresolved" and self.strategy is not None:
            raise StrategyError(
                f"an unresolved decision names no strategy, and this one names "
                f"{self.strategy!r}: 'nobody decided' and 'we chose this' are the two states "
                "this axis exists to keep apart")
        if self.method != "unresolved" and self.strategy is None:
            raise StrategyError(
                f"a {self.method!r} decision that chose nothing is a measurement with no "
                "answer, which appears in no roll-up and is invisible to every reader")
        if self.method == "mechanically_checked" and self.strategy != FROM_SCRATCH:
            raise StrategyError(
                f"{self.strategy!r} cannot be mechanically checked: §12.1 admits a partition "
                "only where stable seams exist, and §10.1 forbids presenting that "
                "natural-language criterion as a checked predicate. The only mechanical "
                f"branch is the size gate selecting {FROM_SCRATCH!r}.")


# §5's confirmed rule (`gate.STRATEGY_RULES`) mapped to what it means once artifacts exist.
# `size-gated` is the only one that consults the measurement; the other two were chosen
# before any artifact existed, which is what makes them a human's decision rather than one.
_PRECOMMITTED = {"fusion": FROM_SCRATCH, "base-and-port": BASE_AND_PORT}


def decide(rule: str, size: Size) -> Decision:
    """§12's confirmed rule applied to §12.1's measured size."""
    if not isinstance(size, Size):
        raise StrategyError(f"a Size is required, not {type(size).__name__}")
    if rule in _PRECOMMITTED:
        return Decision(_PRECOMMITTED[rule], "manual_trace_confirmed",
                        f"the §5 gate confirmed the rule {rule!r} before any artifact "
                        "existed, so this strategy is the operator's recorded decision and "
                        "not a reading of the measured size")
    if rule != "size-gated":
        raise StrategyError(
            f"the confirmed strategy rule is one of {['size-gated', *_PRECOMMITTED]}, not "
            f"{rule!r}; `gate.STRATEGY_RULES` is where §5 step 2's answer is bounded")
    if size.changed_lines is None or size.changed_files is None:
        return Decision(None, "unresolved",
                        "§12.1's threshold cannot be applied to a size this run did not "
                        f"measure: {'; '.join(size.unmeasured)}. An unmeasured size is not a "
                        "small one, and defaulting it to from-scratch fusion would spend the "
                        "run's whole synthesis budget on evidence nobody took.")
    if size.changed_lines < SIZE_LINE_THRESHOLD and size.changed_files < SIZE_FILE_THRESHOLD:
        return Decision(FROM_SCRATCH, "mechanically_checked",
                        f"{size.changed_lines} changed lines across {size.changed_files} "
                        f"file(s), both under §12.1's ~{SIZE_LINE_THRESHOLD}/"
                        f"~{SIZE_FILE_THRESHOLD}")
    return Decision(None, "unresolved",
                    f"{size.changed_lines} changed lines across {size.changed_files} file(s) "
                    f"is at or over §12.1's ~{SIZE_LINE_THRESHOLD}/~{SIZE_FILE_THRESHOLD}, "
                    "which triggers a seam analysis rather than forcing a partition. "
                    "'Stable seams exist' is §10.1's non-mechanical criterion; record the "
                    "analysis through `recorded_seam_analysis` and it becomes "
                    "`manual_trace_confirmed`.")


def recorded_seam_analysis(chosen: str, rationale: str) -> Decision:
    """§12.1's above-threshold choice, recorded as the human judgement it is.

    `from_scratch` IS REFUSED HERE. Below the threshold the size gate already produces it
    mechanically; above the threshold §12.1 offers partition or base-and-port and nothing
    else. Admitting it would let a `manual_trace_confirmed` decision overwrite a
    `mechanically_checked` one with a weaker method under the same strategy name.
    """
    if chosen not in (PARTITION, BASE_AND_PORT):
        raise StrategyError(
            f"a seam analysis chooses {PARTITION!r} or {BASE_AND_PORT!r}, not {chosen!r}: "
            "§12.1 offers those two above the threshold, and the from-scratch branch is the "
            "one the size gate decides mechanically below it")
    if not isinstance(rationale, str) or not rationale.strip():
        raise StrategyError(
            "a recorded seam analysis carries its rationale; §12.3's last sentence forbids an "
            "unrecorded intuition, and a blank one is exactly that with a field around it")
    return Decision(chosen, "manual_trace_confirmed", rationale.strip())


# §12.3's axis, which is NOT `verify.classify`'s. §6.2 answers "what did the gate say"; this
# answers "why did it say it", and the two have different vocabularies on purpose.
_INFRASTRUCTURE_OUTCOMES = (verify.BASELINE_RED_NO_NEW_IDENTIFIED_FAILURE,
                            verify.HARVEST_INCOMPLETE)


def classify_failure(outcome: str, *, report) -> tuple:
    """§12.3's (class, reason) for one verify outcome. `None` when nothing can be said.

    THREE VALUES PLUS A REFUSAL, and the refusal is the point. §12.3 forbids falling back on
    an infrastructure failure and requires falling back when synthesis has stopped making
    progress — so the class decides money. Two outcomes are deliberately unclassifiable:

      * `FLAKY` says the run pair cannot answer at all. Calling it infrastructure would refuse
        a fallback the run may need; calling it synthesis-introduced would spend one on a coin
        flip.
      * `GATE_CHANGED` says the candidate moved a gate-defining file, so the gate that
        measured it is not the baseline's. `verify.classify`'s own reason for that outcome
        carries "on the runs alone this would have been PASS" — the failure being classified
        may not be a failure.

    THE UNRESOLVED-REPORT BRANCH IS THIS FUNCTION'S FAIL-CLOSED HALF. A `coverage.Report`
    separates "checked and false" (`unsatisfied`) from "nobody could check" (`unresolved`)
    precisely so those cannot be read as one another. A FAIL whose report holds an unresolved
    criterion has NOT established that every claim is satisfied, so it has not established
    that the failure came from synthesis — and `synthesis_introduced` is the class that
    permits spending a fallback.
    """
    if outcome not in verify.OUTCOMES:
        raise StrategyError(
            f"{outcome!r} is not one of §6.2's outcomes {list(verify.OUTCOMES)}; a class "
            "assigned to a verdict this engine does not produce is a reading of nothing")
    if outcome == verify.PASS:
        return None, ("this outcome is not a failure, so §12.3 has nothing to classify; a "
                      "class here would describe a run that passed")
    if outcome in _INFRASTRUCTURE_OUTCOMES:
        return INFRASTRUCTURE, (
            f"{outcome} is a statement about the harness rather than about the candidate — "
            "§12.3: never fall back on an infrastructure failure, because base-and-port "
            "cannot help")
    if outcome == verify.FLAKY:
        return None, ("the gate disagreed with itself across reruns, so this run pair says "
                      "nothing about why the candidate failed; §12.3's three classes are all "
                      "claims this evidence does not support")
    if outcome == verify.GATE_CHANGED:
        return None, ("the candidate moved the gate surface, so the gate that produced this "
                      "verdict is not the baseline's; §6.2 records that on the runs alone the "
                      "outcome would have been different, and neither reading of the failure "
                      "survives that")
    # verify.FAIL, and only it, reaches the ledger.
    if report is None:
        return None, ("no coverage report was taken for this candidate, so whether a required "
                      "claim is unmet is a question nobody asked; §12.4 makes that check the "
                      "only thing that catches a false green and it did not run")
    if not isinstance(report, coverage.Report):
        raise StrategyError(f"a coverage.Report or None is required, "
                            f"not {type(report).__name__}")
    if not report.results:
        return None, ("this coverage report holds no results at all, so it says nothing about "
                      "any claim; an empty report reading as a clean one is §10.1's own "
                      "failure shape")
    if report.contradictions:
        return REQUIREMENT_GAP, (
            f"{len(report.contradictions)} ledger contradiction(s): {report.contradictions[0]}")
    if report.unsatisfied:
        return REQUIREMENT_GAP, (
            f"{len(report.unsatisfied)} accepted claim(s) were checked and are not satisfied: "
            f"{report.unsatisfied[0]}")
    if report.unresolved:
        return None, (
            f"{len(report.unresolved)} criterion/criteria are unresolved — nobody could check "
            f"them ({report.unresolved[0]}) — so 'every claim is satisfied' is not something "
            "this run measured, and `synthesis_introduced` cannot be concluded from it")
    return SYNTHESIS_INTRODUCED, (
        f"every one of this candidate's {len(report.results)} criteria was mechanically "
        "checked and satisfied, and the gate still failed, so the failure is in the "
        "synthesis rather than in the requirements")


def fallback_disposition(failure_class) -> str:
    """Whether §12.3 permits base-and-port on this class. Three values, never a boolean.

    `permitted` for `synthesis_introduced` (§12.3: "fall back when synthesis is infeasible or
    has stopped making progress") and for `requirement_gap` (§12.4: "a missing accepted row is
    a fallback trigger *and* a report line, regardless of verify" — a seat may well have
    implemented what synthesis missed). `refused` for `infrastructure`, which §12.3 names
    outright. `undecidable` for `None`, and it is a THIRD value rather than a False so that
    "we could not tell" is never spelled the same way as "no": a caller that folded them
    together would report a run that refused to fall back and a run that could not decide
    with the same sentence.
    """
    if failure_class is None:
        return UNDECIDABLE
    if failure_class not in FAILURE_CLASSES:
        raise StrategyError(f"a failure class is one of {list(FAILURE_CLASSES)} or None, "
                            f"not {failure_class!r}")
    return REFUSED if failure_class == INFRASTRUCTURE else PERMITTED
```

Delete the now-unused imports if the linter in `make verify` objects: `os` and `subprocess` are not used by the final file — remove both from the import block, leaving `import tempfile`, `from dataclasses import dataclass`, `from pathlib import Path`, and `from . import bundle as bundlemod, coverage, gitcmd, verify`.

- [ ] **Step 8: Run the whole file**

```bash
uvx --with pytest pytest -q tests/test_forge_strategy.py
```

Expected: `24 passed`.

- [ ] **Step 9: Mutate every new branch**

Run each one at a time, checking `git status` between them:

```bash
scripts/mutate.py --file shared/lib/forge/strategy.py \
  --old 'if added == "-" or deleted == "-":' \
  --new 'if False:' \
  -- uvx --with pytest pytest -q tests/test_forge_strategy.py

scripts/mutate.py --file shared/lib/forge/strategy.py \
  --old '    if omitted:' \
  --new '    if False:' \
  -- uvx --with pytest pytest -q tests/test_forge_strategy.py

scripts/mutate.py --file shared/lib/forge/strategy.py \
  --old 'if size.changed_lines is None or size.changed_files is None:' \
  --new 'if False:' \
  -- uvx --with pytest pytest -q tests/test_forge_strategy.py

scripts/mutate.py --file shared/lib/forge/strategy.py \
  --old 'if size.changed_lines < SIZE_LINE_THRESHOLD and size.changed_files < SIZE_FILE_THRESHOLD:' \
  --new 'if size.changed_lines <= SIZE_LINE_THRESHOLD and size.changed_files <= SIZE_FILE_THRESHOLD:' \
  -- uvx --with pytest pytest -q tests/test_forge_strategy.py

scripts/mutate.py --file shared/lib/forge/strategy.py \
  --old '    if report.unresolved:' \
  --new '    if False:' \
  -- uvx --with pytest pytest -q tests/test_forge_strategy.py

scripts/mutate.py --file shared/lib/forge/strategy.py \
  --old '    if not report.results:' \
  --new '    if False:' \
  -- uvx --with pytest pytest -q tests/test_forge_strategy.py

scripts/mutate.py --file shared/lib/forge/strategy.py \
  --old 'return REFUSED if failure_class == INFRASTRUCTURE else PERMITTED' \
  --new 'return PERMITTED' \
  -- uvx --with pytest pytest -q tests/test_forge_strategy.py

scripts/mutate.py --file shared/lib/forge/strategy.py \
  --old 'if self.method == "mechanically_checked" and self.strategy != FROM_SCRATCH:' \
  --new 'if False:' \
  -- uvx --with pytest pytest -q tests/test_forge_strategy.py
```

Expected: every one exits 0 (CAUGHT). A SURVIVED row means the branch has no test — add one before continuing. Run `git status` after the wave; it must be clean.

- [ ] **Step 10: Add the test file to the Makefile**

In `Makefile`, extend `FORGE_TESTS` (line 21-32) by appending to the last continuation line:

```make
               tests/test_forge_seatrecord.py tests/test_forge_strategy.py
```

- [ ] **Step 11: Render, gate and commit**

```bash
make render
git add shared/lib/forge/strategy.py tests/test_forge_strategy.py Makefile marketplaces
make verify; echo "verify rc=$?"
make precommit; echo "precommit rc=$?"
```

Expected: both `rc=0`. Then:

```bash
git commit -m "$(cat <<'EOF'
feat(forge): §12.1's size gate refuses a binary delta, and §12.3's class refuses an unresolved report

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UiV66Pt8cZVMq9t8WEAhpN
EOF
)"
```

---
### Task 2: The progress tuple, oscillation read back out of the journal, and the cap that bounds them

**Two commits.** Steps 1–7 give the synthesis-fix cap a recorded home; steps 8–20 build `progress.py`. Commit each half separately — the cap is a schema change to a write-once record and a reviewer must be able to reject it on its own.

**Files:**
- Modify: `shared/lib/forge/gate.py:186-213` (`Quote`), `:236-330` (`quote`), `:1068-1113` (`Confirmation`), `:1517-1540` (`open_run`'s `Manifest(...)`)
- Modify: `shared/lib/forge/runstate.py:321-337` (`Manifest` fields), `:943-961` (`_DECODERS`)
- Modify: `tests/test_forge_gate.py:1604-1615`, `tests/test_forge_runstate.py:24-36`, `tests/test_forge_runner.py:67-77`, `tests/test_forge_seams.py:648-666` and `:825-832` (the five direct `runstate.Manifest(...)` construction sites)
- Create: `shared/lib/forge/progress.py`
- Create: `tests/test_forge_progress.py`
- Modify: `Makefile:21-32`

**Interfaces:**

- **Consumes:**
  - `verify.Run(exit_code: int, stdout: str, stderr: str, duration_sec: float, step_index: int)` (`verify.py:362-374`).
  - `journal.Event(seq: int, event: str, operation_id: str, at: str, data: dict)` (`journal.py:54-63`); `journal.intent(kind) -> f"{kind}_start"`, `journal.done(kind) -> f"{kind}_done"` (`journal.py:66-73`); `journal.orphans(events) -> tuple[Event, ...]` (`journal.py:76`); `journal.Journal(path).read() -> tuple[Event, ...]` and `.record(event, *, operation_id, **data) -> Event` (`journal.py:130`, `:139`).
  - `runstate.Manifest` — after this task it also carries `review_rounds: int` and `synthesis_fix_cap: int`.
  - `runstate.count(name, value, source, *, floor=1)` (`runstate.py:780`).
  - `storage.journal_path(run_dir) -> Path` (`storage.py:224`).
- **Produces:**
  - `gate.Quote.review_rounds: int`, `gate.Quote.synthesis_fix_cap: int`
  - `gate.Confirmation.review_rounds: int`, `gate.Confirmation.synthesis_fix_cap: int`
  - `runstate.Manifest.review_rounds: int`, `runstate.Manifest.synthesis_fix_cap: int`
  - `progress.Progress(new_failure_count: int | None, failing_test_fingerprints: frozenset | None)`
  - `progress.BETTER = "better"`, `NOT_BETTER = "not_better"`, `NOT_COMPARABLE = "not_comparable"`, `COMPARISONS`
  - `progress.compare(before: Progress, after: Progress) -> str`
  - `progress.pytest_fingerprints(stdout: str, stderr: str, exit_code: int) -> frozenset | None`
  - `progress.from_runs(candidate_run, baseline_run, *, parse=pytest_fingerprints) -> Progress`
  - `progress.FIX_KIND = "synthesis_fix"`
  - `progress.record_fix_start(log, *, operation_id, tree_oid)` / `progress.record_fix_done(log, *, operation_id, tree_oid, prog)`
  - `progress.Sighting(tree_oid: str, fingerprints: frozenset | None)`
  - `progress.sightings(events) -> tuple[Sighting, ...]`
  - `progress.OSCILLATING = "oscillating"`, `NOT_OSCILLATING = "not_oscillating"`, `OSCILLATION_UNKNOWN = "unknown"`
  - `progress.oscillation(events) -> tuple[str, str]`
  - `progress.cap_remaining(manifest, events) -> int`
  - `progress.ProgressError(RuntimeError)`
  - Task 5 calls `cap_remaining` and `oscillation`; Task 6 calls `cap_remaining`.

**The input that would make this read cleaner than its evidence.** A gate whose output forge cannot parse — anything that is not pytest, a pytest run that died during collection, a runner wrapped in `make`. The obvious implementation returns `frozenset()` for "no failures found in the text". `frozenset()` is a subset of every set, so **every** comparison answers `better`, the loop runs to its hard cap reporting improvement at each step, and the run's final line reads "stopped after N fixes" rather than "never measured whether anything improved". This is recorded verbatim in `.superpowers/sdd/progress.md` as one of the four fail-open shapes the design pass found. The second one: `cap_remaining` counting `synthesis_fix_done` records instead of `synthesis_fix_start` records, so a crashed-and-restarted fix that really did spend a provider call reads as unspent budget.

---

- [ ] **Step 1: Write the failing test for the cap's recorded home**

Append to `tests/test_forge_gate.py`:

```python
def test_the_priced_synthesis_fix_cap_is_the_recorded_one(tmp_path, monkeypatch):
    """§12.3's cap has one number, and §5.2 is where it is priced.

    `Manifest.attempts` is the BUILDER budget — `quote` computes `builders = seats *
    attempts` — and reusing it would silently re-price a 3-attempt run as 9. `State.attempt`
    is already spent on builder attempts. So the cap is its own field, derived from the same
    arithmetic the quote showed the operator.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    repo = make_repo(tmp_path)
    report = preflight.inspect_repo(repo)
    q = gate.quote(report, review_rounds=2, ultrareview=True)
    assert (q.review_rounds, q.synthesis_fix_cap) == (2, 3)
    assert gate.quote(report, review_rounds=2, ultrareview=False).synthesis_fix_cap == 2
    assert gate.quote(report, review_rounds=0, ultrareview=False).synthesis_fix_cap == 0

    c = gate.confirm(report, q, _answered())
    assert (c.review_rounds, c.synthesis_fix_cap) == (2, 3)
    run = gate.open_run(report, c, "r-cap")
    back = runstate.read_manifest(run)
    assert (back.review_rounds, back.synthesis_fix_cap) == (2, 3), \
        "the priced number and the recorded number are the same number"


def test_a_cap_below_the_rounds_it_must_cover_is_refused():
    with pytest.raises(gate.GateError):
        gate.Confirmation(setup=(), verify=(verify.Step(argv=("true",)),),
                          on_calibration_failure="abort", strategy="size-gated",
                          accepted_gaps=(), author=("A", "a@b.invalid"),
                          seats=3, attempts=3, review_rounds=2, synthesis_fix_cap=1)
```

> `_answered` is `test_forge_gate.py`'s existing answer-sheet helper; `make_repo`, `preflight`, `runstate`, `verify` and `pytest` are already imported in that file. If `_answered` takes no default arguments in your copy, call it exactly as the neighbouring tests do.

- [ ] **Step 2: Run it to verify it fails**

```bash
uvx --with pytest pytest -q tests/test_forge_gate.py -k "synthesis_fix_cap or cap_below"
```

Expected: FAIL — `TypeError: quote() got an unexpected keyword argument` is *not* what you should see (`review_rounds` already exists); the failure is `AttributeError: 'Quote' object has no attribute 'review_rounds'`.

- [ ] **Step 3: Put the two numbers on the quote**

In `shared/lib/forge/gate.py`, extend `Quote` (after `attempts: int`, `gate.py:213`):

```python
    # §12.3's synthesis-fix cap, and the rounds it has to cover. They are HERE for
    # `seats`/`attempts`' reason one section on: everything downstream reads the run's shape
    # off the quote, so the number the operator was shown and the number the loop spends are
    # one number. `Manifest.attempts` cannot hold this — it is the BUILDER budget, and
    # `quote` prices post-review synthesis separately as `review_rounds + ultra_fixes`, so
    # reusing it turns a 3-attempt run into a 9-attempt one and leaves `--collect` unable to
    # say which budget an `attempt` value was spending.
    review_rounds: int
    synthesis_fix_cap: int
```

In `quote`, the arithmetic already exists as `review_fixes`. Change the `return` (`gate.py:~329`) to pass both:

```python
    return Quote(provider_calls=calls, ultrareview=ultra_line, setup_runs=setup_runs,
                 verify_runs=verify_runs, peak_disk_gb=peak_disk_gb, lines=tuple(lines),
                 seats=seats, attempts=attempts,
                 review_rounds=review_rounds, synthesis_fix_cap=review_fixes)
```

- [ ] **Step 4: Put them on the confirmation**

In `Confirmation` (`gate.py:1068-1076`), after `attempts: int`:

```python
    review_rounds: int
    synthesis_fix_cap: int
```

and in `Confirmation.__post_init__`, after the two `_confirmed_count` calls:

```python
        _confirmed_count("review_rounds", self.review_rounds, "§5 step 2", floor=0)
        _confirmed_count("synthesis_fix_cap", self.synthesis_fix_cap, "§5 step 2", floor=0)
        if self.synthesis_fix_cap < self.review_rounds:
            # Each round can produce at most one fix, and §13.1 adds one more. A cap under
            # the rounds it must cover is a budget the loop is guaranteed to exhaust before
            # the review it was priced for finishes, which would report `review_blocked` for
            # an arithmetic mistake made at the gate.
            raise GateError(
                f"synthesis_fix_cap={self.synthesis_fix_cap} is below review_rounds="
                f"{self.review_rounds}: §5.2 prices one post-review synthesis per round plus "
                "§13.1's, so a cap under the round count cannot fund the review it agreed to")
```

In `confirm`'s `return Confirmation(...)` (`gate.py:~1383`), add:

```python
                        seats=quote_.seats, attempts=quote_.attempts,
                        review_rounds=quote_.review_rounds,
                        synthesis_fix_cap=quote_.synthesis_fix_cap)
```

In `open_run`'s `runstate.Manifest(...)` (`gate.py:1517`), add after `attempts=confirmation.attempts`:

```python
        review_rounds=confirmation.review_rounds,
        synthesis_fix_cap=confirmation.synthesis_fix_cap,
```

> If `open_run` currently spells `seats=confirmation.seats, attempts=confirmation.attempts` on one line, keep that line and add the two new keywords beside it.

- [ ] **Step 5: Put them on the manifest**

In `shared/lib/forge/runstate.py`, add to `Manifest` after `attempts: int` (`runstate.py:337`):

```python
    review_rounds: int
    synthesis_fix_cap: int
```

Add the decoder above `_DECODERS` (`runstate.py:943`):

```python
def _budget(name, value, source):
    """A post-review budget, floored at ZERO rather than at one.

    `count`'s floor of 1 is right for `seats` and `attempts` — a run with no seat is not a
    run. Zero review rounds IS a run (`gate.quote` says so at its own floor), and a run that
    priced no post-review synthesis has a cap of 0 rather than a missing field.
    """
    return count(name, value, source, floor=0)
```

and register both in `_DECODERS`, after `"attempts": count,`:

```python
    "review_rounds": _budget,
    "synthesis_fix_cap": _budget,
```

- [ ] **Step 6: Update the five direct construction sites in the tests**

Add `review_rounds=2, synthesis_fix_cap=3` to each of:

- `tests/test_forge_gate.py:1608-1614` — inside the `fields = dict(...)` literal in `_manifest`.
- `tests/test_forge_runstate.py:29-35` — inside the `base = dict(...)` literal in `_manifest`.
- `tests/test_forge_runner.py:69-77` — inside `runstate.Manifest(...)` in `_manifest`.
- `tests/test_forge_seams.py:656-666` — inside `runstate.Manifest(...)` in `_manifest_for`.
- `tests/test_forge_seams.py:825-832` — inside `runstate.write_manifest(run, runstate.Manifest(...))`.

`tests/test_forge_runner.py:428` and `:927` use `**m.__dict__` and need no change.

Also update `tests/test_forge_gate.py`'s `Confirmation(...)` construction site (search for `gate.Confirmation(` in that file) to pass `review_rounds=2, synthesis_fix_cap=3`.

- [ ] **Step 7: Run the affected suites, then render, gate and commit the cap**

```bash
uvx --with pytest pytest -q tests/test_forge_gate.py tests/test_forge_runstate.py \
    tests/test_forge_runner.py tests/test_forge_seams.py
```

Expected: all pass, with two more tests than before in `test_forge_gate.py`.

```bash
make render
git add shared/lib/forge/gate.py shared/lib/forge/runstate.py \
        tests/test_forge_gate.py tests/test_forge_runstate.py \
        tests/test_forge_runner.py tests/test_forge_seams.py marketplaces
make verify; echo "verify rc=$?"
make precommit; echo "precommit rc=$?"
git commit -m "$(cat <<'EOF'
feat(forge): §12.3's synthesis-fix cap is the number §5.2 priced, not the builder budget

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UiV66Pt8cZVMq9t8WEAhpN
EOF
)"
```

- [ ] **Step 8: Write the failing tests for the progress tuple**

Create `tests/test_forge_progress.py`:

```python
"""§12.3's progress tuple, its three-outcome comparison, and oscillation over the journal."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

from forge import journal, progress, storage, verify  # noqa: E402


def _run(exit_code=1, stdout="", stderr=""):
    return verify.Run(exit_code=exit_code, stdout=stdout, stderr=stderr,
                      duration_sec=0.1, step_index=0)


PYTEST_TAIL = """\
=================================== FAILURES ===================================
_______________________________ test_alpha _____________________________________
=========================== short test summary info ============================
FAILED tests/test_a.py::test_alpha - AssertionError
FAILED tests/test_b.py::test_beta - ValueError
=========================== 2 failed, 8 passed in 1.2s =========================
"""

PYTEST_GREEN = "=========================== 10 passed in 1.1s ==================\n"

COLLECTION_ERROR = """\
==================================== ERRORS ====================================
ImportError while importing test module 'tests/test_a.py'.
=========================== short test summary info ============================
ERROR tests/test_a.py
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
"""


def test_a_pytest_failure_tail_yields_the_named_ids():
    ids = progress.pytest_fingerprints(PYTEST_TAIL, "", 1)
    assert ids == frozenset({"tests/test_a.py::test_alpha", "tests/test_b.py::test_beta"})


def test_a_green_pytest_run_yields_the_empty_set_and_that_is_honest():
    assert progress.pytest_fingerprints(PYTEST_GREEN, "", 0) == frozenset()


def test_output_that_is_not_pytest_yields_unknown_never_the_empty_set():
    """THE FAIL-OPEN. An empty set is a subset of everything, so it compares as progress
    against every prior attempt and the loop runs to its cap reporting improvement."""
    assert progress.pytest_fingerprints("make: *** [verify] Error 2\n", "", 2) is None


def test_a_collection_error_yields_unknown_not_one_failing_id():
    """pytest exited nonzero and named no failing TEST. 'ERROR <module>' is not a test id,
    and reading it as one manufactures a fingerprint that can never shrink."""
    assert progress.pytest_fingerprints(COLLECTION_ERROR, "", 2) is None


def test_a_nonzero_pytest_run_with_a_banner_and_no_failed_line_is_unknown():
    banner = "=========================== short test summary info ============\n"
    assert progress.pytest_fingerprints(banner, "", 1) is None


def test_the_parser_reads_stderr_too_because_a_gate_may_redirect():
    assert progress.pytest_fingerprints("", PYTEST_TAIL, 1) == \
        frozenset({"tests/test_a.py::test_alpha", "tests/test_b.py::test_beta"})


def test_from_runs_counts_only_failures_the_baseline_did_not_have():
    base = _run(1, "=== short test summary info ===\nFAILED t.py::old - X\n")
    cand = _run(1, "=== short test summary info ===\nFAILED t.py::old - X\n"
                   "FAILED t.py::new - Y\n")
    p = progress.from_runs(cand, base)
    assert p.new_failure_count == 1
    assert p.failing_test_fingerprints == frozenset({"t.py::old", "t.py::new"})


def test_an_unparseable_baseline_makes_the_whole_tuple_unknown():
    cand = _run(1, PYTEST_TAIL)
    p = progress.from_runs(cand, _run(2, "make: *** Error 2\n"))
    assert p.new_failure_count is None and p.failing_test_fingerprints is None


def test_an_unparseable_candidate_makes_the_whole_tuple_unknown():
    p = progress.from_runs(_run(2, "make: *** Error 2\n"), _run(0, PYTEST_GREEN))
    assert p.new_failure_count is None and p.failing_test_fingerprints is None


# --------------------------------------------------------------------------- comparison
def _p(n, ids):
    return progress.Progress(n, None if ids is None else frozenset(ids))


def test_a_shrinking_count_is_progress():
    assert progress.compare(_p(3, {"a", "b", "c"}), _p(1, {"a"})) == progress.BETTER


def test_a_strictly_shrinking_set_is_progress():
    assert progress.compare(_p(2, {"a", "b"}), _p(2, {"a"})) == progress.BETTER


def test_an_identical_tuple_is_not_progress():
    assert progress.compare(_p(2, {"a", "b"}), _p(2, {"a", "b"})) == progress.NOT_BETTER


def test_a_traded_failure_is_not_progress_and_is_not_unknown_either():
    """Neither set contains the other. That IS a measurement, and its answer is 'no'.
    `not_comparable` is reserved for 'we could not tell', which is what makes it useful."""
    assert progress.compare(_p(1, {"a"}), _p(1, {"b"})) == progress.NOT_BETTER


def test_a_growing_set_is_not_progress():
    assert progress.compare(_p(1, {"a"}), _p(2, {"a", "b"})) == progress.NOT_BETTER


def test_an_unknown_on_either_side_is_not_comparable():
    assert progress.compare(_p(1, {"a"}), _p(None, None)) == progress.NOT_COMPARABLE
    assert progress.compare(_p(None, None), _p(1, {"a"})) == progress.NOT_COMPARABLE


def test_a_half_measured_tuple_cannot_be_built():
    with pytest.raises(progress.ProgressError):
        progress.Progress(3, None)
    with pytest.raises(progress.ProgressError):
        progress.Progress(None, frozenset())
```

- [ ] **Step 9: Run them to verify they fail**

```bash
uvx --with pytest pytest -q tests/test_forge_progress.py
```

Expected: FAIL — `ModuleNotFoundError: No module named 'forge.progress'`.

- [ ] **Step 10: Write the tuple, the parser and the comparison**

Create `shared/lib/forge/progress.py`:

```python
"""§12.3's progress tuple, and the oscillation detector that reads it back off the journal.

WHY EVERY ANSWER HERE IS THREE-VALUED. §12.3 makes "has stopped making progress" the trigger
for spending a fallback. A boolean cannot distinguish "this attempt did not improve anything"
from "nothing about this attempt could be measured", and the second one is the common case:
forge has NO per-test parser anywhere, so a repository whose gate is not pytest produces
output this module cannot read at all. An unreadable gate answering `frozenset()` would make
every attempt a strict improvement over its predecessor — the empty set is a subset of every
set — and the loop would run to its hard cap reporting progress at each step.

WHERE THE STATE LIVES: `events.jsonl`, not memory and not `runstate.State`. `State` is five
scalars and §14 argues for those five BECAUSE one enum cannot hold them; a sixth free-form
dimension would be a different change from the one §14 makes. The journal is where §14.1 puts
write-ahead pairs, and §13 states the rule this follows verbatim for `review_findings`: the
transition reads the RECORD, not the return value, because a compaction between "the fix
returned" and "the orchestrator classified it" must not lose the sighting.
"""
import re
from dataclasses import dataclass

from . import journal as journalmod

BETTER = "better"
NOT_BETTER = "not_better"
NOT_COMPARABLE = "not_comparable"
COMPARISONS = (BETTER, NOT_BETTER, NOT_COMPARABLE)

OSCILLATING = "oscillating"
NOT_OSCILLATING = "not_oscillating"
OSCILLATION_UNKNOWN = "unknown"
OSCILLATION_ANSWERS = (OSCILLATING, NOT_OSCILLATING, OSCILLATION_UNKNOWN)

# §14.2 names this event by this spelling; `journal.intent`/`journal.done` add the halves.
FIX_KIND = "synthesis_fix"

# pytest's own short-summary line. `FAILED <nodeid>` and `ERROR <nodeid>` both appear there,
# and only `FAILED` names a TEST — an `ERROR` row is a module that could not be collected, so
# reading it as a failing test invents a fingerprint that can never shrink.
_FAILED = re.compile(r"^FAILED (\S+)", re.MULTILINE)
# The evidence that this output came from pytest at all. Without one of these the text is
# some other runner's and nothing below may be read out of it.
_PYTEST_BANNERS = ("short test summary info", "=== FAILURES ===", " passed", " failed")


class ProgressError(RuntimeError):
    """A progress question this module will not answer on the evidence it was given."""


@dataclass(frozen=True)
class Progress:
    """§12.3's tuple: (new-failure count, failing-test fingerprint set).

    BOTH FIELDS ARE UNKNOWN TOGETHER OR NEITHER IS. The count is derived from the two sets, so
    a tuple carrying one without the other describes a measurement that cannot have happened,
    and `compare` would then have to decide which half to believe.
    """
    new_failure_count: int | None
    failing_test_fingerprints: frozenset | None

    def __post_init__(self) -> None:
        a = self.new_failure_count is None
        b = self.failing_test_fingerprints is None
        if a != b:
            raise ProgressError(
                "a progress tuple is measured whole or not at all: the new-failure count is "
                "derived from the fingerprint sets, so "
                f"(count={self.new_failure_count!r}, fingerprints="
                f"{self.failing_test_fingerprints!r}) describes a measurement that cannot "
                "have been taken")
        if not a:
            if not isinstance(self.new_failure_count, int) \
                    or isinstance(self.new_failure_count, bool) \
                    or self.new_failure_count < 0:
                raise ProgressError(
                    f"a new-failure count is a whole number, not "
                    f"{self.new_failure_count!r}")
            if not isinstance(self.failing_test_fingerprints, frozenset):
                raise ProgressError(
                    "the fingerprint set is a frozenset, so a tuple is hashable and can be "
                    f"used as a sighting key; got {type(self.failing_test_fingerprints).__name__}")


def pytest_fingerprints(stdout: str, stderr: str, exit_code: int):
    """The failing test ids in this gate's output, or `None` when it cannot be read.

    THE DOMAIN IS DECLARED AND NARROW: pytest, recognised by its own banner. Forge has no
    per-test parser and §12.3 needs one, so this is the one runner it claims to understand —
    and for every other runner the honest answer is `None`. Widening this later means adding
    a named parser, never loosening the banner check.

    A ZERO EXIT WITH A BANNER IS AN HONEST EMPTY SET: pytest ran, and nothing failed. A
    NONZERO exit with a banner and no `FAILED` line is `None`, not `frozenset()` — pytest's
    exits 2, 3 and 4 (collection error, internal error, usage error) all reach that state, and
    an empty set there is the subset-of-everything fail-open this module exists to close.
    """
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        raise ProgressError(f"an exit code is an int, not {exit_code!r}")
    text = f"{stdout or ''}\n{stderr or ''}"
    if not any(b in text for b in _PYTEST_BANNERS):
        return None
    ids = frozenset(_FAILED.findall(text))
    if exit_code == 0:
        # A green pytest run that nonetheless printed FAILED lines is a contradiction, and
        # believing either half would be a verdict over evidence that disagrees with itself.
        return None if ids else frozenset()
    return ids or None


def from_runs(candidate_run, baseline_run, *, parse=pytest_fingerprints) -> Progress:
    """§12.3's tuple for one synthesis attempt, measured against the run's calibration.

    "NEW" IS A STATEMENT ABOUT THE CANDIDATE, which is why the baseline is a required
    argument and not an optional one — the same reason `verify.classify` takes a calibration.
    A baseline whose output cannot be read makes "new" unanswerable, so the whole tuple is
    unknown even when the candidate parsed perfectly.
    """
    cand = parse(candidate_run.stdout, candidate_run.stderr, candidate_run.exit_code)
    base = parse(baseline_run.stdout, baseline_run.stderr, baseline_run.exit_code)
    if cand is None or base is None:
        return Progress(None, None)
    return Progress(len(cand - base), cand)


def compare(before: Progress, after: Progress) -> str:
    """§12.3's strict ordering, as three outcomes.

    Progress requires the count to DECREASE or the fingerprint set to shrink under strict set
    inclusion. Equal is not progress. Two sets neither of which contains the other — fix A
    traded failure X for Y — is not progress either, and it is `not_better` rather than
    `not_comparable`: that comparison was taken and its answer is no. `not_comparable` is
    spent on exactly one thing, an unmeasured side, so that "we could not tell" can never be
    read as "no". §12.3's consumer treats `not_comparable` as no progress claim, which means
    fall back or stop — never continue on an assumed improvement.
    """
    for name, value in (("before", before), ("after", after)):
        if not isinstance(value, Progress):
            raise ProgressError(f"{name} is a Progress, not {type(value).__name__}")
    if before.new_failure_count is None or after.new_failure_count is None:
        return NOT_COMPARABLE
    if after.new_failure_count < before.new_failure_count:
        return BETTER
    if after.failing_test_fingerprints < before.failing_test_fingerprints:
        return BETTER
    return NOT_BETTER
```

- [ ] **Step 11: Run the tuple tests**

```bash
uvx --with pytest pytest -q tests/test_forge_progress.py
```

Expected: `18 passed`.

- [ ] **Step 12: Write the failing tests for the sightings, the oscillation detector and the cap**

Append to `tests/test_forge_progress.py`:

```python
# --------------------------------------------------------------------------- journal
def _log(tmp_path):
    return journal.Journal(storage.journal_path(tmp_path))


def _fix(log, op, tree, ids, count=0):
    progress.record_fix_start(log, operation_id=op, tree_oid=tree)
    progress.record_fix_done(log, operation_id=op, tree_oid=tree,
                             prog=progress.Progress(count, frozenset(ids)))


def test_two_fixes_at_different_trees_are_not_oscillating(tmp_path):
    log = _log(tmp_path)
    _fix(log, "op1", "a" * 40, {"t::x"})
    _fix(log, "op2", "b" * 40, {"t::y"})
    answer, why = progress.oscillation(log.read())
    assert answer == progress.NOT_OSCILLATING and why


def test_the_second_sighting_of_one_tree_and_failure_pair_is_the_stop_signal(tmp_path):
    log = _log(tmp_path)
    _fix(log, "op1", "a" * 40, {"t::x"})
    _fix(log, "op2", "b" * 40, {"t::y"})
    _fix(log, "op3", "a" * 40, {"t::x"})
    answer, why = progress.oscillation(log.read())
    assert answer == progress.OSCILLATING
    assert "a" * 40 in why


def test_the_same_tree_with_a_different_failure_set_is_not_a_repeat(tmp_path):
    log = _log(tmp_path)
    _fix(log, "op1", "a" * 40, {"t::x"})
    _fix(log, "op2", "a" * 40, {"t::y"})
    assert progress.oscillation(log.read())[0] == progress.NOT_OSCILLATING


def test_an_unmeasured_failure_set_never_matches_another_one(tmp_path):
    """THE FAIL-OPEN. Two unreadable attempts at one tree are `(oid, None)` twice. Treating
    that as a repeat manufactures a stop signal and reports it as a measured recurrence."""
    log = _log(tmp_path)
    for op in ("op1", "op2"):
        progress.record_fix_start(log, operation_id=op, tree_oid="a" * 40)
        progress.record_fix_done(log, operation_id=op, tree_oid="a" * 40,
                                 prog=progress.Progress(None, None))
    answer, why = progress.oscillation(log.read())
    assert answer == progress.OSCILLATION_UNKNOWN
    assert "could not" in why or "unmeasured" in why


def test_a_fix_with_no_checkpoint_is_recorded_absent_and_never_as_empty_string(tmp_path):
    log = _log(tmp_path)
    progress.record_fix_start(log, operation_id="op1", tree_oid=None)
    progress.record_fix_done(log, operation_id="op1", tree_oid=None,
                             prog=progress.Progress(0, frozenset()))
    rows = [e for e in log.read() if e.event == journal.done(progress.FIX_KIND)]
    assert rows[0].data["tree_oid"] is None
    assert progress.sightings(log.read()) == ()
    assert progress.oscillation(log.read())[0] == progress.OSCILLATION_UNKNOWN


def test_an_empty_tree_oid_is_refused_at_the_writer(tmp_path):
    log = _log(tmp_path)
    with pytest.raises(progress.ProgressError):
        progress.record_fix_start(log, operation_id="op1", tree_oid="")


def test_an_orphaned_fix_makes_the_answer_unknown(tmp_path):
    log = _log(tmp_path)
    _fix(log, "op1", "a" * 40, {"t::x"})
    progress.record_fix_start(log, operation_id="op2", tree_oid="b" * 40)
    answer, why = progress.oscillation(log.read())
    assert answer == progress.OSCILLATION_UNKNOWN and "op2" in why


def test_a_proven_repeat_outranks_a_gap_in_the_evidence(tmp_path):
    """A positive finding is not weakened by missing evidence elsewhere — `agreement_label`'s
    rule ("a measured difference outranks an unmeasured field"), applied here."""
    log = _log(tmp_path)
    _fix(log, "op1", "a" * 40, {"t::x"})
    _fix(log, "op2", "a" * 40, {"t::x"})
    progress.record_fix_start(log, operation_id="op3", tree_oid="c" * 40)
    assert progress.oscillation(log.read())[0] == progress.OSCILLATING


def test_a_run_that_has_fixed_nothing_is_not_oscillating(tmp_path):
    assert progress.oscillation(())[0] == progress.NOT_OSCILLATING


# --------------------------------------------------------------------------- the cap
class _Cap:
    def __init__(self, cap):
        self.synthesis_fix_cap = cap


def test_the_cap_counts_starts_so_a_crashed_fix_is_still_spent(tmp_path):
    """THE FAIL-OPEN. Counting `_done` records lets a fix that crashed after spending a
    provider call read as unspent budget, and the loop buys it a second time."""
    log = _log(tmp_path)
    _fix(log, "op1", "a" * 40, {"t::x"})
    progress.record_fix_start(log, operation_id="op2", tree_oid="b" * 40)
    assert progress.cap_remaining(_Cap(3), log.read()) == 1


def test_the_cap_never_reports_negative_budget(tmp_path):
    log = _log(tmp_path)
    for i, op in enumerate(("op1", "op2", "op3")):
        _fix(log, op, chr(ord("a") + i) * 40, {f"t::{i}"})
    assert progress.cap_remaining(_Cap(2), log.read()) == 0


def test_a_manifest_with_no_cap_is_refused_rather_than_defaulted(tmp_path):
    with pytest.raises(progress.ProgressError):
        progress.cap_remaining(object(), ())
```

- [ ] **Step 13: Run them to verify they fail**

```bash
uvx --with pytest pytest -q tests/test_forge_progress.py
```

Expected: FAIL — `AttributeError: module 'forge.progress' has no attribute 'record_fix_start'`.

- [ ] **Step 14: Write the sightings, the detector and the cap**

Append to `shared/lib/forge/progress.py`:

```python
@dataclass(frozen=True)
class Sighting:
    """One completed synthesis fix, as the pair oscillation is about.

    THE TREE OID, NOT THE COMMIT OID. Two checkpoints with identical content but different
    messages or timestamps are different commits and the same tree, and oscillation is about
    CONTENT recurring. §13 requires a fresh checkpoint after every fix and §14.1 makes git
    "the ordering of record", so the tree always exists at the moment the pair is formed;
    §14.2's rule for an interrupted dirty tree (preserve it as a WIP checkpoint BEFORE
    touching it) produces one there too.
    """
    tree_oid: str
    fingerprints: frozenset | None


def _tree(tree_oid):
    """A checkpoint identity, or None. An EMPTY STRING is refused, loudly.

    `inspect.py:432-434` and `bundle.py:120-123` set the shape: a fail-closed sentinel admits
    nothing. An empty-string tree id used as a dict key would make every unrecorded attempt
    "the same tree" and fire the stop signal on the second one.
    """
    if tree_oid is None:
        return None
    if not isinstance(tree_oid, str) or not tree_oid.strip():
        raise ProgressError(
            f"a checkpoint tree id is a non-empty string or None, not {tree_oid!r}: an empty "
            "one would key every unrecorded attempt to the same sighting and stop the loop "
            "on the second one, reporting a content recurrence nobody observed")
    return tree_oid


def record_fix_start(log, *, operation_id: str, tree_oid) -> None:
    """§14.1's write-ahead intent for one synthesis fix, carrying the tree it starts from."""
    log.record(journalmod.intent(FIX_KIND), operation_id=operation_id,
               tree_oid=_tree(tree_oid))


def record_fix_done(log, *, operation_id: str, tree_oid, prog: Progress) -> None:
    """The completion half, carrying the pair the detector reads back.

    `failure_fingerprints` is a SORTED LIST because JSON has no set, and `None` when the
    tuple was not measured — never `[]`, which would read back as "this attempt had no
    failing tests" and compare equal to a green run.
    """
    if not isinstance(prog, Progress):
        raise ProgressError(f"a Progress is required, not {type(prog).__name__}")
    log.record(journalmod.done(FIX_KIND), operation_id=operation_id,
               tree_oid=_tree(tree_oid),
               new_failure_count=prog.new_failure_count,
               failure_fingerprints=(None if prog.failing_test_fingerprints is None
                                     else sorted(prog.failing_test_fingerprints)))


def sightings(events) -> tuple:
    """Every completed fix that produced a usable tree id, oldest first.

    A done record with `tree_oid: None` produces NO sighting, because the pair cannot be
    formed — and `oscillation` treats that absence as a gap in the evidence rather than as
    the absence of a repeat.
    """
    out = []
    for e in events:
        if e.event != journalmod.done(FIX_KIND):
            continue
        oid = e.data.get("tree_oid")
        if oid is None:
            continue
        if not isinstance(oid, str) or not oid.strip():
            raise ProgressError(
                f"the {journalmod.done(FIX_KIND)} record at seq {e.seq} carries "
                f"tree_oid={oid!r}; only a non-empty string or null was ever written, so this "
                "journal was produced by something other than `record_fix_done`")
        ids = e.data.get("failure_fingerprints")
        if ids is None:
            out.append(Sighting(oid, None))
        elif isinstance(ids, list) and all(isinstance(i, str) for i in ids):
            out.append(Sighting(oid, frozenset(ids)))
        else:
            raise ProgressError(
                f"the {journalmod.done(FIX_KIND)} record at seq {e.seq} carries "
                f"failure_fingerprints={ids!r}, which is neither a list of ids nor null")
    return tuple(out)


def oscillation(events) -> tuple:
    """§12.3's stop signal, as three answers.

    THE RULE: the second sighting of the same `(tree_oid, fingerprints)` pair is the stop
    signal — fix A trades failure X for Y, fix B trades back, and the run has returned to a
    state it has already been in.

    A SIGHTING WITH UNMEASURED FINGERPRINTS FORMS NO PAIR. Two of them at one tree are
    `(oid, None)` twice; matching them would manufacture the stop signal out of two
    measurements nobody could take and report it as an observed recurrence.

    A PROVEN REPEAT OUTRANKS A GAP. Once a pair has been seen twice the answer is
    `oscillating` whatever else is missing — `fingerprint.agreement_label`'s rule that a
    measured difference outranks an unmeasured field, in the other direction.

    An ORPHANED start — §14.1's `outcome_unknown` shape, which `journal.orphans` already
    identifies — is a fix whose sighting was never recorded, so the answer below it is
    `unknown` rather than `not_oscillating`.
    """
    seen, unmeasured = set(), 0
    for s in sightings(events):
        if s.fingerprints is None:
            unmeasured += 1
            continue
        key = (s.tree_oid, s.fingerprints)
        if key in seen:
            return OSCILLATING, (
                f"the synthesis has returned to tree {s.tree_oid} with the same failing-test "
                "set it had there before; §12.3 calls the second sighting of that pair the "
                "stop signal")
        seen.add(key)
    orphaned = tuple(e for e in journalmod.orphans(events)
                     if e.event == journalmod.intent(FIX_KIND))
    if orphaned:
        return OSCILLATION_UNKNOWN, (
            "a synthesis fix started and recorded no result (operation "
            f"{', '.join(sorted(e.operation_id for e in orphaned))}), so whether its tree and "
            "failure set repeat an earlier pair could not be measured")
    if unmeasured:
        return OSCILLATION_UNKNOWN, (
            f"{unmeasured} completed fix(es) recorded no failing-test set, so their pairs "
            "could not be formed and a recurrence among them would be invisible")
    return NOT_OSCILLATING, (
        f"{len(seen)} distinct (tree, failing-test set) pair(s) were recorded and none "
        "repeats")


def cap_remaining(manifest, events) -> int:
    """§12.3's hard cap, minus what has already been spent. Never negative.

    STARTS ARE WHAT COUNT, not dones. A fix that crashed between the two halves really did
    spend a provider call — §14.1 says so in the sentence that gives it the name
    `outcome_unknown` — and counting only completions would hand that budget back and let the
    loop buy the same fix twice.
    """
    cap = getattr(manifest, "synthesis_fix_cap", None)
    if not isinstance(cap, int) or isinstance(cap, bool) or cap < 0:
        raise ProgressError(
            f"the run's manifest records no synthesis_fix_cap (got {cap!r}); §12.3's cap is "
            "priced at §5.2 and recorded once, and a default chosen here would be a budget "
            "nobody agreed to")
    spent = sum(1 for e in events if e.event == journalmod.intent(FIX_KIND))
    return max(0, cap - spent)
```

- [ ] **Step 15: Run the whole file**

```bash
uvx --with pytest pytest -q tests/test_forge_progress.py
```

Expected: `30 passed`.

- [ ] **Step 16: Re-run the new tests under renamed test functions**

Rename every test in `tests/test_forge_progress.py` to `test_zz0`, `test_zz1`, … and re-run. Standing mitigation from the defect brief: pytest derives `tmp_path`'s basename from the test name, and an assertion that matches its own function name passes for the wrong reason.

```bash
uvx --with pytest pytest -q tests/test_forge_progress.py
```

Expected: `30 passed` — the same count. **Restore the original names.**

- [ ] **Step 17: Mutate every new branch**

```bash
scripts/mutate.py --file shared/lib/forge/progress.py \
  --old '    return ids or None' \
  --new '    return ids' \
  -- uvx --with pytest pytest -q tests/test_forge_progress.py

scripts/mutate.py --file shared/lib/forge/progress.py \
  --old '    if not any(b in text for b in _PYTEST_BANNERS):' \
  --new '    if False:' \
  -- uvx --with pytest pytest -q tests/test_forge_progress.py

scripts/mutate.py --file shared/lib/forge/progress.py \
  --old '    if after.failing_test_fingerprints < before.failing_test_fingerprints:' \
  --new '    if after.failing_test_fingerprints <= before.failing_test_fingerprints:' \
  -- uvx --with pytest pytest -q tests/test_forge_progress.py

scripts/mutate.py --file shared/lib/forge/progress.py \
  --old '        if s.fingerprints is None:' \
  --new '        if False:' \
  -- uvx --with pytest pytest -q tests/test_forge_progress.py

scripts/mutate.py --file shared/lib/forge/progress.py \
  --old '    spent = sum(1 for e in events if e.event == journalmod.intent(FIX_KIND))' \
  --new '    spent = sum(1 for e in events if e.event == journalmod.done(FIX_KIND))' \
  -- uvx --with pytest pytest -q tests/test_forge_progress.py

scripts/mutate.py --file shared/lib/forge/progress.py \
  --old '    if orphaned:' \
  --new '    if False:' \
  -- uvx --with pytest pytest -q tests/test_forge_progress.py

scripts/mutate.py --file shared/lib/forge/progress.py \
  --old '        if a != b:' \
  --new '        if False:' \
  -- uvx --with pytest pytest -q tests/test_forge_progress.py
```

Expected: every one exits 0 (CAUGHT). Run `git status` after the wave; it must be clean.

- [ ] **Step 18: Add the test file to the Makefile**

Extend `FORGE_TESTS` with `tests/test_forge_progress.py`.

- [ ] **Step 19: Render, gate and commit**

```bash
make render
git add shared/lib/forge/progress.py tests/test_forge_progress.py Makefile marketplaces
make verify; echo "verify rc=$?"
make precommit; echo "precommit rc=$?"
```

Expected: both `rc=0`. Then:

```bash
git commit -m "$(cat <<'EOF'
feat(forge): an unparseable gate is no progress claim, and the sighting comes off the journal

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UiV66Pt8cZVMq9t8WEAhpN
EOF
)"
```

---
### Task 3: The strongest-seat rubric, total by construction, and coverage as a fallback trigger

**Files:**
- Create: `shared/lib/forge/rubric.py`
- Create: `tests/test_forge_rubric.py`
- Modify: `Makefile:21-32`

**Interfaces:**

- **Consumes:**
  - `coverage.Report(results, contradictions, unsatisfied, unresolved)` and `coverage.Result(row_id, criterion_index, method, satisfied, detail)` (`coverage.py:67-138`).
  - `verify.OUTCOMES` (`verify.py:128`).
  - `strategy.Size` from Task 1, for the `diff_complexity` dimension.
  - `seatrecord.SeatRecord(name, attempts)` / `seatrecord.Attempt` (`seatrecord.py:36-62`) — read by the caller, not by this module; this module takes the extracted numbers.
- **Produces:**
  - `rubric.Dimensions(seat: str, unsatisfied_criteria: int | None, covered_criteria: int | None, gate_outcome: str | None, review_risk: int | None, diff_complexity: int | None)`
  - `rubric.dimensions_from(seat: str, *, report, gate_outcome, review_risk, size) -> Dimensions`
  - `rubric.GATE_RANK: dict[str, int]`
  - `rubric.Ranking(ordered: tuple[str, ...], unrankable: tuple[tuple[str, str], ...])`
  - `rubric.rank(dims) -> Ranking`
  - `rubric.strongest(dims) -> tuple[str | None, str]`
  - `rubric.TRIGGERED = "triggered"`, `NOT_TRIGGERED = "not_triggered"`, `TRIGGER_UNDECIDABLE = "undecidable"`, `TRIGGERS`
  - `rubric.fallback_trigger(report) -> tuple[str, str]`
  - `rubric.RubricError(RuntimeError)`
  - Task 5's loop reads `fallback_trigger`; Plan J reads `strongest`.

**The input that would make this read cleaner than its evidence.** Three seats where one has an unmeasured dimension. The obvious implementation ranks the two it can and reports the winner, so *"the strongest seat we were able to measure"* prints as *"the strongest seat"* — and the unmeasured one may have been better on the very dimension nobody read. `strongest` therefore names a seat only when **every** seat is rankable. The second one: a `coverage.Report` whose criteria are all `unresolved` reading as `not_triggered`. `unresolved` means nobody could check; folding it into "no trigger" turns §10.1's own failure ("nobody could check" read as "fine") into §12.4's fallback decision. The third: a `Report` with zero results at all — `coverage.check` refuses a zero-row ledger, but `Report` is a public dataclass whose `__post_init__` only re-derives roll-ups, so a hand-built empty one is constructible and reads clean. That is Plan I's Critical C4, one container further out again.

---

- [ ] **Step 1: Write the failing tests**

Create `tests/test_forge_rubric.py`:

```python
"""§12.5's strongest-seat rubric and §12.4's coverage-as-fallback-trigger."""
import itertools
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

from forge import coverage, rubric, strategy, verify  # noqa: E402


def _d(seat, *, unsat=0, covered=5, outcome=verify.PASS, risk=0, complexity=10):
    return rubric.Dimensions(seat=seat, unsatisfied_criteria=unsat, covered_criteria=covered,
                             gate_outcome=outcome, review_risk=risk, diff_complexity=complexity)


def _report(*results, contradictions=()):
    rs = tuple(results)
    return coverage.Report(rs, tuple(contradictions),
                           tuple(f"{r.row_id}[{r.criterion_index}]: {r.detail}"
                                 for r in rs if r.satisfied is False),
                           tuple(f"{r.row_id}[{r.criterion_index}]: {r.detail}"
                                 for r in rs if r.method == "unresolved"))


def _ok(row="a"):
    return coverage.Result(row, 0, "mechanically_checked", True, "checked and satisfied")


def _bad(row="a"):
    return coverage.Result(row, 0, "mechanically_checked", False, "checked and not satisfied")


def _unknown(row="a"):
    return coverage.Result(row, 0, "unresolved", None, "no predicate exists")


# --------------------------------------------------------------------------- ordering
def test_requirement_coverage_outranks_every_later_dimension():
    """§12.5's order: coverage first, gate second, review risk third, diff complexity last."""
    strong = _d("agy", unsat=0, outcome=verify.FAIL, risk=9, complexity=999)
    weak = _d("claude", unsat=1, outcome=verify.PASS, risk=0, complexity=1)
    assert rubric.rank([weak, strong]).ordered == ("agy", "claude")


def test_the_gate_outcome_outranks_review_risk_and_complexity():
    a = _d("agy", outcome=verify.PASS, risk=9, complexity=999)
    b = _d("claude", outcome=verify.FAIL, risk=0, complexity=1)
    assert rubric.rank([b, a]).ordered == ("agy", "claude")


def test_review_risk_outranks_diff_complexity():
    a = _d("agy", risk=0, complexity=999)
    b = _d("claude", risk=1, complexity=1)
    assert rubric.rank([b, a]).ordered == ("agy", "claude")


def test_the_seat_name_is_the_final_tie_break_and_makes_the_order_total():
    seats = [_d(n) for n in ("codex", "agy", "claude")]
    for order in itertools.permutations(seats):
        assert rubric.rank(list(order)).ordered == ("agy", "claude", "codex")


def test_no_two_distinct_seats_ever_compare_equal():
    """§12.3's last sentence: 'strongest seat' is never an unrecorded intuition. A rubric that
    can return a tie has an unrecorded tie-break by another route."""
    seats = [_d(n) for n in ("agy", "claude", "codex")]
    keys = [rubric._key(d) for d in seats]
    assert len(set(keys)) == len(keys)


def test_two_records_for_one_seat_name_are_refused():
    """A duplicate name makes the final tie-break non-total, silently."""
    with pytest.raises(rubric.RubricError):
        rubric.rank([_d("agy"), _d("agy", complexity=3)])


def test_every_verify_outcome_has_a_declared_rank():
    """A §6.2 outcome added later must fail loudly rather than sort as unknown."""
    assert set(rubric.GATE_RANK) == set(verify.OUTCOMES)


def test_an_outcome_outside_the_declared_set_makes_the_seat_unrankable():
    r = rubric.rank([_d("agy"), _d("claude", outcome="MOSTLY_FINE")])
    assert r.ordered == ("agy",)
    assert [s for s, _ in r.unrankable] == ["claude"]


# --------------------------------------------------------------------------- fail closed
def test_a_seat_with_an_unmeasured_dimension_is_unrankable_not_worst():
    r = rubric.rank([_d("agy"), _d("claude", risk=None)])
    assert r.ordered == ("agy",)
    assert r.unrankable == (("claude", "review_risk"),)


def test_strongest_refuses_to_name_a_seat_while_any_seat_is_unrankable():
    """THE FAIL-OPEN. Ranking only the measurable seats prints 'the strongest seat we could
    measure' as 'the strongest seat'."""
    name, why = rubric.strongest([_d("agy"), _d("claude", diff_complexity_unset=True)]
                                 if False else [_d("agy"), _d("claude", risk=None)])
    assert name is None
    assert "claude" in why and "review_risk" in why


def test_strongest_names_the_seat_when_every_dimension_was_measured():
    name, why = rubric.strongest([_d("agy", unsat=1), _d("claude", unsat=0)])
    assert name == "claude" and why


def test_strongest_over_no_seats_at_all_is_a_refusal():
    name, why = rubric.strongest([])
    assert name is None and "no seat" in why


# --------------------------------------------------------------------------- §12.4
def test_an_unsatisfied_accepted_row_triggers_fallback():
    answer, why = rubric.fallback_trigger(_report(_ok(), _bad("b")))
    assert answer == rubric.TRIGGERED and "b" in why


def test_a_contradiction_triggers_fallback():
    answer, _ = rubric.fallback_trigger(
        _report(_ok(), contradictions=("row b contradicts a unanimous rejection",)))
    assert answer == rubric.TRIGGERED


def test_an_unresolved_criterion_is_undecidable_never_a_clean_report():
    """THE FAIL-OPEN. 'Nobody could check' read as 'nothing is wrong' is §10.1's own
    failure, arriving as §12.4's fallback decision."""
    answer, why = rubric.fallback_trigger(_report(_ok(), _unknown("b")))
    assert answer == rubric.TRIGGER_UNDECIDABLE and "unresolved" in why


def test_a_fully_checked_clean_report_does_not_trigger():
    answer, _ = rubric.fallback_trigger(_report(_ok(), _ok("b")))
    assert answer == rubric.NOT_TRIGGERED


def test_an_empty_report_is_undecidable_not_clean():
    """C4's shape one container out again: `coverage.check` refuses a zero-ROW ledger, and
    `Report` is a public dataclass whose __post_init__ only re-derives roll-ups."""
    answer, why = rubric.fallback_trigger(coverage.Report((), (), (), ()))
    assert answer == rubric.TRIGGER_UNDECIDABLE and "no results" in why


def test_a_trigger_outranks_an_undecidable_because_it_is_a_measurement():
    answer, _ = rubric.fallback_trigger(_report(_bad("a"), _unknown("b")))
    assert answer == rubric.TRIGGERED


def test_a_missing_report_is_undecidable():
    answer, why = rubric.fallback_trigger(None)
    assert answer == rubric.TRIGGER_UNDECIDABLE and "no coverage report" in why


# --------------------------------------------------------------------------- extraction
def test_dimensions_are_extracted_from_the_records_and_never_re_measured():
    d = rubric.dimensions_from("agy", report=_report(_ok(), _bad("b")),
                               gate_outcome=verify.FAIL, review_risk=2,
                               size=strategy.Size(120, 4, ()))
    assert (d.seat, d.unsatisfied_criteria, d.covered_criteria) == ("agy", 1, 1)
    assert d.gate_outcome == verify.FAIL and d.review_risk == 2
    assert d.diff_complexity == 124


def test_an_unmeasured_size_yields_an_unmeasured_complexity_not_zero():
    d = rubric.dimensions_from("agy", report=_report(_ok()), gate_outcome=verify.PASS,
                               review_risk=0, size=strategy.Size(None, 4, ("a binary delta",)))
    assert d.diff_complexity is None


def test_a_missing_report_yields_unmeasured_coverage_not_a_clean_one():
    d = rubric.dimensions_from("agy", report=None, gate_outcome=verify.PASS,
                               review_risk=0, size=strategy.Size(1, 1, ()))
    assert d.unsatisfied_criteria is None and d.covered_criteria is None
```

> The odd-looking conditional in `test_strongest_refuses_to_name_a_seat_while_any_seat_is_unrankable` is a leftover — write it plainly as `rubric.strongest([_d("agy"), _d("claude", risk=None)])`.

- [ ] **Step 2: Run them to verify they fail**

```bash
uvx --with pytest pytest -q tests/test_forge_rubric.py
```

Expected: FAIL — `ModuleNotFoundError: No module named 'forge.rubric'`.

- [ ] **Step 3: Write the module**

Create `shared/lib/forge/rubric.py`:

```python
"""§12.5's strongest-seat rubric and §12.4's coverage-as-fallback-trigger.

A RUBRIC THAT READS A LIVE MEASUREMENT IS A RUBRIC NOBODY CAN REPRODUCE. Every dimension
below is a value already on the ledger, the coverage report or the seat record, and `rank` is
a pure function of them — so `--collect` re-running this hours later, on a machine where the
gate would now answer differently, gets the same order. `dimensions_from` is the one place
that reads a record, and it takes the record rather than a path.

TOTAL BY CONSTRUCTION. §12.3's last sentence forbids "strongest seat" being an unrecorded
intuition, and a comparison that can return "tie" is one by another route: the caller then
picks, and nothing records how. The four declared dimensions are followed by the SEAT NAME,
which `storage.seat_state_path` already requires to be unique inside a run — so any two
distinct seats compare unequal, and `test_no_two_distinct_seats_ever_compare_equal` is what
stands where that stops being true.
"""
from dataclasses import dataclass

from . import coverage as coveragemod, strategy as strategymod, verify

TRIGGERED = "triggered"
NOT_TRIGGERED = "not_triggered"
TRIGGER_UNDECIDABLE = "undecidable"
TRIGGERS = (TRIGGERED, NOT_TRIGGERED, TRIGGER_UNDECIDABLE)

# §12.5's second dimension, as a declared total order over §6.2's outcomes. Spelled out
# rather than derived from `verify.OUTCOMES`' declaration order, because that order is a
# reading list and this is a preference — and a test asserts the two sets are equal, so an
# outcome added to §6.2 later fails here loudly instead of sorting as unknown.
GATE_RANK = {
    verify.PASS: 0,
    verify.BASELINE_RED_NO_NEW_IDENTIFIED_FAILURE: 1,
    verify.GATE_CHANGED: 2,
    verify.FLAKY: 3,
    verify.FAIL: 4,
    verify.HARVEST_INCOMPLETE: 5,
}


class RubricError(RuntimeError):
    """A ranking question this module will not answer on the evidence it was given."""


@dataclass(frozen=True)
class Dimensions:
    """One seat's recorded values on §12.5's four dimensions.

    EVERY FIELD IS NULLABLE AND A NULL IS NOT A ZERO. An unmeasured `review_risk` is not a
    seat with no risk, and an unmeasured `diff_complexity` is not a seat with a small diff —
    both would sort that seat to the FRONT, which is the fail-open a rubric can least afford.
    A null makes the seat unrankable instead.
    """
    seat: str
    unsatisfied_criteria: int | None
    covered_criteria: int | None
    gate_outcome: str | None
    review_risk: int | None
    diff_complexity: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.seat, str) or not self.seat.strip():
            raise RubricError(f"a seat has a name, not {self.seat!r}")
        for name in ("unsatisfied_criteria", "covered_criteria", "review_risk",
                     "diff_complexity"):
            v = getattr(self, name)
            if v is None:
                continue
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                raise RubricError(f"{name} is a whole number or None, not {v!r}")


def dimensions_from(seat, *, report, gate_outcome, review_risk, size) -> Dimensions:
    """Extract §12.5's dimensions from the records that already hold them.

    A MISSING REPORT YIELDS TWO NULLS, not two zeros. "No accepted criterion is unsatisfied"
    and "nobody produced a coverage report" are the two states §10.1 exists to keep apart, and
    zero would spell them the same way — with the unmeasured seat sorting first.
    """
    if report is None:
        unsat = covered = None
    elif not isinstance(report, coveragemod.Report):
        raise RubricError(f"a coverage.Report or None is required, "
                          f"not {type(report).__name__}")
    elif not report.results:
        # C4's shape: an all-empty report is not a clean one.
        unsat = covered = None
    else:
        unsat = len(report.unsatisfied) + len(report.contradictions)
        covered = sum(1 for r in report.results
                      if r.method == "mechanically_checked" and r.satisfied is True)
    if not isinstance(size, strategymod.Size):
        raise RubricError(f"a strategy.Size is required, not {type(size).__name__}")
    complexity = (None if size.changed_lines is None or size.changed_files is None
                  else size.changed_lines + size.changed_files)
    return Dimensions(seat=seat, unsatisfied_criteria=unsat, covered_criteria=covered,
                      gate_outcome=gate_outcome, review_risk=review_risk,
                      diff_complexity=complexity)


def _unmeasured(d: Dimensions):
    """The first dimension this seat cannot be ranked on, or None. FIRST, in §12.5's order,
    so the reason a seat was dropped names the highest-priority thing that was missing."""
    if d.unsatisfied_criteria is None or d.covered_criteria is None:
        return "requirement_coverage"
    if d.gate_outcome not in GATE_RANK:
        return "gate_outcome"
    if d.review_risk is None:
        return "review_risk"
    if d.diff_complexity is None:
        return "diff_complexity"
    return None


def _key(d: Dimensions) -> tuple:
    """§12.5's order as one sort key, ascending: smaller is stronger.

    Requirement coverage is TWO numbers because §12.5 names one dimension that has two: fewer
    unsatisfied accepted claims first — that is the thing §12.4 calls a fallback trigger — and
    among seats tied there, more mechanically-checked-and-satisfied criteria. `-covered` puts
    "more" at the front of an ascending sort.
    """
    why = _unmeasured(d)
    if why is not None:
        raise RubricError(f"{d.seat} cannot be ranked: {why} was not measured")
    return (d.unsatisfied_criteria, -d.covered_criteria, GATE_RANK[d.gate_outcome],
            d.review_risk, d.diff_complexity, d.seat)


@dataclass(frozen=True)
class Ranking:
    """The rankable seats in §12.5's order, and every seat that was left out with its reason.

    `unrankable` is not a footnote. A ranking that dropped seats silently would let
    `strongest` describe a two-seat comparison as a fleet-wide verdict.
    """
    ordered: tuple
    unrankable: tuple


def rank(dims) -> Ranking:
    """Order the seats §12.5 can compare, and name the ones it cannot."""
    dims = list(dims)
    wrong = sorted({type(d).__name__ for d in dims if not isinstance(d, Dimensions)})
    if wrong:
        raise RubricError(f"a ranking is over Dimensions records, not {wrong}")
    names = [d.seat for d in dims]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise RubricError(
            f"two records name the seat(s) {dupes}. The seat name is §12.5's final "
            "tie-break, so a duplicate makes the order non-total exactly where the rubric "
            "stops being reproducible — and it would do it silently")
    good, bad = [], []
    for d in dims:
        why = _unmeasured(d)
        (bad if why else good).append((d.seat, why) if why else d)
    return Ranking(tuple(d.seat for d in sorted(good, key=_key)), tuple(sorted(bad)))


def strongest(dims) -> tuple:
    """§12.5's strongest seat, or `None` with the reason no seat can be named.

    A SEAT IS NAMED ONLY WHEN EVERY SEAT WAS RANKABLE. Ranking the measurable ones and
    reporting their winner turns "the strongest seat we were able to measure" into "the
    strongest seat" — and the seat that was dropped may have been better on the very
    dimension nobody read. §12.4's coverage check is a fallback trigger for the same reason:
    the thing that was not measured is exactly the thing that decides.
    """
    r = rank(dims)
    if r.unrankable:
        return None, (
            "no strongest seat can be named while "
            + "; ".join(f"{s} has no measured {why}" for s, why in r.unrankable)
            + " — a rubric run over the seats it could read would report the winner of a "
              "smaller comparison than the one the fleet ran")
    if not r.ordered:
        return None, ("no seat was supplied, so there is nothing to compare; an empty fleet "
                      "has no strongest member")
    return r.ordered[0], (f"§12.5's order over {len(r.ordered)} fully measured seat(s): "
                          f"{', '.join(r.ordered)}")


def fallback_trigger(report) -> tuple:
    """§12.4's check, as three answers.

    §12.4: "a missing accepted row is a fallback trigger *and* a report line, regardless of
    verify. This is the only thing that catches false-green." So a triggered answer is a
    MEASUREMENT and outranks a gap — an unresolved criterion beside an unsatisfied one does
    not soften the trigger.

    `undecidable` IS THE THIRD ANSWER AND IT IS THE ONE THIS FUNCTION EXISTS FOR. `unresolved`
    means nobody could check. Folding it into `not_triggered` is §10.1's own example failure
    — "marked present because os.replace appears" — arriving as §12's fallback decision.
    """
    if report is None:
        return TRIGGER_UNDECIDABLE, (
            "no coverage report was produced, so whether an accepted claim is missing is a "
            "question nobody asked; §12.4 calls this check the only thing that catches a "
            "false green")
    if not isinstance(report, coveragemod.Report):
        raise RubricError(f"a coverage.Report or None is required, "
                          f"not {type(report).__name__}")
    if report.contradictions:
        return TRIGGERED, (f"{len(report.contradictions)} ledger contradiction(s): "
                           f"{report.contradictions[0]}")
    if report.unsatisfied:
        return TRIGGERED, (f"{len(report.unsatisfied)} accepted claim(s) were checked and are "
                           f"not satisfied: {report.unsatisfied[0]}")
    if not report.results:
        return TRIGGER_UNDECIDABLE, (
            "this report holds no results at all, so it says nothing about any claim; an "
            "all-empty report reading as a covered run is §10.1's own failure shape")
    if report.unresolved:
        return TRIGGER_UNDECIDABLE, (
            f"{len(report.unresolved)} criterion/criteria are unresolved — nobody could check "
            f"them ({report.unresolved[0]}) — so 'no accepted row is missing' is not something "
            "this run measured")
    return NOT_TRIGGERED, (f"all {len(report.results)} criteria were mechanically checked and "
                           "satisfied, and no row contradicts a unanimous rejection")
```

- [ ] **Step 4: Run the tests**

```bash
uvx --with pytest pytest -q tests/test_forge_rubric.py
```

Expected: `22 passed`.

- [ ] **Step 5: Re-run under renamed test functions**

Rename every test to `test_zz0`…`test_zzN` and re-run. Expected: `22 passed`. **Restore the original names.**

- [ ] **Step 6: Mutate every new branch**

```bash
scripts/mutate.py --file shared/lib/forge/rubric.py \
  --old '    if r.unrankable:' \
  --new '    if False:' \
  -- uvx --with pytest pytest -q tests/test_forge_rubric.py

scripts/mutate.py --file shared/lib/forge/rubric.py \
  --old '    if report.unresolved:' \
  --new '    if False:' \
  -- uvx --with pytest pytest -q tests/test_forge_rubric.py

scripts/mutate.py --file shared/lib/forge/rubric.py \
  --old '    if not report.results:' \
  --new '    if False:' \
  -- uvx --with pytest pytest -q tests/test_forge_rubric.py

scripts/mutate.py --file shared/lib/forge/rubric.py \
  --old '    return (d.unsatisfied_criteria, -d.covered_criteria, GATE_RANK[d.gate_outcome],' \
  --new '    return (GATE_RANK[d.gate_outcome], d.unsatisfied_criteria, -d.covered_criteria,' \
  -- uvx --with pytest pytest -q tests/test_forge_rubric.py

scripts/mutate.py --file shared/lib/forge/rubric.py \
  --old '            d.review_risk, d.diff_complexity, d.seat)' \
  --new '            d.review_risk, d.diff_complexity)' \
  -- uvx --with pytest pytest -q tests/test_forge_rubric.py

scripts/mutate.py --file shared/lib/forge/rubric.py \
  --old '    if dupes:' \
  --new '    if False:' \
  -- uvx --with pytest pytest -q tests/test_forge_rubric.py

scripts/mutate.py --file shared/lib/forge/rubric.py \
  --old '    elif not report.results:' \
  --new '    elif False:' \
  -- uvx --with pytest pytest -q tests/test_forge_rubric.py
```

Expected: every one exits 0 (CAUGHT). The fifth one is the totality mutation — if it SURVIVES, `test_no_two_distinct_seats_ever_compare_equal` is not reaching `_key`'s last element. Run `git status` after the wave; it must be clean.

- [ ] **Step 7: Add the test file to the Makefile**

Extend `FORGE_TESTS` with `tests/test_forge_rubric.py`.

- [ ] **Step 8: Render, gate and commit**

```bash
make render
git add shared/lib/forge/rubric.py tests/test_forge_rubric.py Makefile marketplaces
make verify; echo "verify rc=$?"
make precommit; echo "precommit rc=$?"
```

Expected: both `rc=0`. Then:

```bash
git commit -m "$(cat <<'EOF'
feat(forge): §12.5's rubric names no seat while one is unmeasured, and unresolved is not clean

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UiV66Pt8cZVMq9t8WEAhpN
EOF
)"
```

---
### Task 4: The reviewer's input set, the in-process council call, and the record that outlives it

**Files:**
- Create: `shared/lib/forge/review.py`
- Create: `tests/test_forge_review.py`
- Modify: `Makefile:21-32`

**Interfaces:**

- **Consumes:**
  - `council.engine.ProviderSpec(name, argv, stdin, extract, model=None, thinking=None, log_file=None, cwd=None, sentinel=None, min_chars=MIN_SUBSTANTIVE_CHARS, validator=None)` — a **mutable** dataclass (`council/engine.py:645-663`).
  - `council.engine.build_real_spec(name, prompt, timeout, cfg, workdir) -> ProviderSpec` (`engine.py:676`). For codex it already returns `codex exec - --json` with `extract_codex_json`.
  - `council.engine.run_council(specs, *, retries, timeout, backoff, workdir, prompt=None, requested=None, mode=None, read_only=None, install_signal_handler=True) -> dict` (`engine.py:1274`).
  - `council.engine.MODE_TIMEOUT = {"normal": 300, "deep": 1200}` (`engine.py:90`); `engine.MIN_SUBSTANTIVE_CHARS = 400` (`engine.py:258`); `engine.make_sentinel() -> str` (`engine.py:268`); `engine.evaluate(exit_code, stdout, stderr, spec)` (`engine.py:1064`); `engine.DEFAULT_PROVIDERS = ["claude", "codex", "agy"]` (`engine.py:41`).
  - `fingerprint.build(*, prompt, token, cli, bundle_sha256=None, model_requested=None, model_reported=None, run=subprocess.run, closure=taskbundle.installed_closure) -> PromptIdentity` (`fingerprint.py:199`); `fingerprint.as_row(pi) -> dict` (`:221`).
  - `taskbundle.bundle_hash(b) -> str` (`taskbundle.py:277`); `taskbundle.read_task_bundle(run_dir) -> TaskBundle` (`:381`); `taskbundle.materialize(b, source_root, seat_path) -> Path` (`:416`); `taskbundle.task_dir(seat_path) -> Path` (`:400`).
  - `storage.atomic_write(path, data: bytes)` (`storage.py:62`); `storage.ledger_path(run_dir)` (`:236`); `storage.Quota.for_harvest()` (`:272+`).
  - `journal.Journal`, `journal.intent`, `journal.done`.
  - `gitcmd.git`, `gitcmd.READONLY`.
- **Produces:**
  - `review.ReviewError(RuntimeError)`
  - `review.REVIEW_TIMEOUT_SEC: int`
  - `review.SEVERITIES = ("blocker", "important", "minor")`; `review.RESOLUTIONS = ("open", "fixed", "unresolved", "rejected")`
  - `review.review_dir(checkout, round_) -> Path`
  - `review.write_reviewer_inputs(checkout, round_, *, checkpoint, baseline_commit, baseline_tree, artifact_manifest, token, task_bundle_present) -> Path`
  - `review.launcher_prompt(bundle_path) -> str`
  - `review.assert_ledger_is_out_of_reach(run_dir, *, checkout) -> None`
  - `review.reviewer_specs(names, *, prompt, timeout, cwd, token, workdir, cfg=None, build=engine.build_real_spec) -> list`
  - `review.parse_findings(text) -> tuple`
  - `review.Finding(id, round, seat, severity, claim, resolution)`; `review.Round(round, checkpoint, findings, identities, seats_responded, seats_silent)`
  - `review.round_dir(run_dir, round_) -> Path`; `review.findings_path(run_dir, round_) -> Path`
  - `review.write_round(run_dir, r: Round) -> str` (returns the content hash); `review.read_round(run_dir, round_) -> Round`
  - `review.run_round(run_dir, *, round_, checkout, checkpoint, baseline_commit, baseline_tree, artifact_manifest, log, names=engine.DEFAULT_PROVIDERS, cfg=None, run_council=engine.run_council, build=engine.build_real_spec, probe=fingerprint.build, make_token=engine.make_sentinel) -> Round`
  - Task 5 consumes `Round`, `read_round`, `run_round`, `SEVERITIES`, `RESOLUTIONS`. Task 6 imports `review.VERIFIED_NOT_INDEPENDENTLY_REVIEWED` (declared in Task 5).

**The inputs that would make this read cleaner than its evidence — four of them.**
1. **A reviewer whose call failed contributing zero findings.** The round then reads "no blockers" and the run ships clean. `Round` therefore carries `seats_silent`, and Task 5 refuses `ready` while it is non-empty.
2. **`run_provider`'s `result_text` is truncated** (`engine._truncate`, `RESULT_TRUNCATE`). Parsing findings out of it makes a long, correct review whose JSON block fell past the cut read as `unparseable`. Read `result_file` — the full text — and treat an unreadable one as a **silent seat**, never as a seat with no findings.
3. **`seat.forge_spec` reused for a reviewer.** `_forge_validator` (`seat.py:239-266`) delegates to `engine.evaluate` on a copy with `min_chars=0` and **`sentinel=None` regardless of what the spec carries** — its own docstring says so. That is correct for a *builder* (a terse forty-minute sign-off must not trigger a re-run on top of half-finished work) and fatal for a *reviewer*: one that never opened the bundle scores `valid`, and §13's whole proof-token argument evaporates. Reviewers get the council's default `evaluate`.
4. **`workdir=run_dir`.** `run_council` ends with `(workdir / "manifest.json").write_text(...)` (`engine.py:1320`) — plain, non-atomic, unfsynced — and `storage.manifest_path(run_dir)` is the same filename, written **once** via `exclusive_write` and never rewritten (`runstate.py:366`). A review would destroy the run's write-once identity and `--collect` would then reconstruct the run from the council's manifest.

---

- [ ] **Step 1: Write the failing tests for the reviewer inputs and the ledger assertion**

Create `tests/test_forge_review.py`:

```python
"""§13's reviewer input set, the in-process council call, and the durable findings record."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

from council import engine  # noqa: E402
from forge import journal, ledger, review, storage  # noqa: E402

from forge_fixtures import commit_all, global_identity, make_repo, write  # noqa: E402,F401


def _checkout(tmp_path, name="synthesis"):
    r = make_repo(tmp_path, name)
    write(r, "src.py", "print('hi')\n")
    commit_all(r, "synthesis checkpoint")
    return r


def _run_dir(tmp_path, name="run"):
    d = tmp_path / name
    d.mkdir()
    return d


def _ledger(run_dir):
    row = ledger.Row(
        id=ledger.row_id("R1", "the cache layer is rejected"), requirement_id="R1",
        requirement_span="spec.md:1-3", kind="architecture", component="core",
        semantic_claim="the cache layer is rejected", status="rejected", dependencies=(),
        seat_evidence=(), counterevidence="", acceptance_criteria=(),
        synthesis_evidence=None, verification_receipt=None, risk="", rationale="none")
    l = ledger.Ledger(version=ledger.VERSION, rows=(row,), union_diff_bytes=10,
                      degrade_threshold_bytes=ledger.DEGRADE_UNION_DIFF_BYTES,
                      degraded=False)
    ledger.write_ledger(run_dir, l)
    return l


def test_the_reviewer_inputs_name_every_item_section_13_requires(tmp_path, global_identity):
    co = _checkout(tmp_path)
    d = review.write_reviewer_inputs(co, 1, checkpoint="a" * 40, baseline_commit="b" * 40,
                                     baseline_tree="c" * 40, artifact_manifest=None,
                                     token="TOKEN-1", task_bundle_present=True)
    inputs = json.loads((d / "inputs.json").read_text())
    assert inputs["synthesis_checkpoint"] == "a" * 40
    assert inputs["baseline_commit"] == "b" * 40 and inputs["baseline_tree"] == "c" * 40
    assert inputs["artifact_manifest"] is None
    instr = (d / "REVIEW.md").read_text()
    assert "TOKEN-1" in instr
    assert "git diff" in instr


def test_a_missing_artifact_manifest_is_stated_to_the_reviewer(tmp_path, global_identity):
    """§16's manifest is a later plan's artifact. A four-item input set described as five is
    a reviewer told it has evidence it does not have."""
    co = _checkout(tmp_path)
    d = review.write_reviewer_inputs(co, 1, checkpoint="a" * 40, baseline_commit="b" * 40,
                                     baseline_tree="c" * 40, artifact_manifest=None,
                                     token="T", task_bundle_present=True)
    assert "no out-of-band artifact manifest" in (d / "REVIEW.md").read_text()


def test_a_missing_task_bundle_is_stated_rather_than_omitted(tmp_path, global_identity):
    co = _checkout(tmp_path)
    d = review.write_reviewer_inputs(co, 1, checkpoint="a" * 40, baseline_commit="b" * 40,
                                     baseline_tree="c" * 40, artifact_manifest=None,
                                     token="T", task_bundle_present=False)
    assert "no task bundle" in (d / "REVIEW.md").read_text()


def test_the_review_directory_is_asked_for_never_joined(tmp_path, global_identity):
    co = _checkout(tmp_path)
    gd = subprocess.run(["git", "-C", str(co), "rev-parse", "--absolute-git-dir"],
                        check=True, capture_output=True, text=True).stdout.strip()
    assert review.review_dir(co, 2) == Path(gd) / "khenrix-forge" / "review" / "round-2"


def test_the_ledger_is_out_of_reach_of_a_clean_checkout(tmp_path, global_identity):
    run = _run_dir(tmp_path)
    _ledger(run)
    co = _checkout(tmp_path)
    review.assert_ledger_is_out_of_reach(run, checkout=co)   # does not raise


def test_a_ledger_copied_into_the_checkout_is_caught_by_its_bytes(tmp_path, global_identity):
    """STRUCTURAL, NOT TEXTUAL. §13 gives every reviewer a shell in this tree, so 'do not
    read the ledger' in prose is not a guarantee. The bytes must not be here under ANY name."""
    run = _run_dir(tmp_path)
    _ledger(run)
    co = _checkout(tmp_path)
    (co / "notes.txt").write_bytes(storage.Path(storage.ledger_path(run)).read_bytes()
                                   if hasattr(storage, "Path") else
                                   Path(storage.ledger_path(run)).read_bytes())
    with pytest.raises(review.ReviewError) as e:
        review.assert_ledger_is_out_of_reach(run, checkout=co)
    assert "notes.txt" in str(e.value)


def test_a_ledger_copied_into_the_review_directory_is_caught(tmp_path, global_identity):
    run = _run_dir(tmp_path)
    _ledger(run)
    co = _checkout(tmp_path)
    d = review.write_reviewer_inputs(co, 1, checkpoint="a" * 40, baseline_commit="b" * 40,
                                     baseline_tree="c" * 40, artifact_manifest=None,
                                     token="T", task_bundle_present=True)
    (d / "extra.json").write_bytes(Path(storage.ledger_path(run)).read_bytes())
    with pytest.raises(review.ReviewError):
        review.assert_ledger_is_out_of_reach(run, checkout=co)


def test_a_run_directory_inside_the_checkout_is_refused_outright(tmp_path, global_identity):
    """The path check and the byte check are complementary, not redundant: a run directory
    UNDER the checkout puts the real ledger in the reviewer's tree with no copy involved."""
    co = _checkout(tmp_path)
    run = co / "state"
    run.mkdir()
    _ledger(run)
    with pytest.raises(review.ReviewError) as e:
        review.assert_ledger_is_out_of_reach(run, checkout=co)
    assert "under" in str(e.value)


def test_an_unreadable_directory_refuses_rather_than_scanning_nothing(tmp_path,
                                                                     global_identity):
    """os.walk with no `onerror` returns [] for an unreadable subtree — measured on this
    project three times — and an empty scan finding no ledger reads as a clean tree."""
    run = _run_dir(tmp_path)
    _ledger(run)
    co = _checkout(tmp_path)
    blind = co / "blind"
    blind.mkdir()
    (blind / "x.txt").write_text("x\n")
    blind.chmod(0o000)
    try:
        with pytest.raises(review.ReviewError):
            review.assert_ledger_is_out_of_reach(run, checkout=co)
    finally:
        blind.chmod(0o755)


def test_a_missing_ledger_refuses_rather_than_certifying_absence(tmp_path, global_identity):
    run = _run_dir(tmp_path)
    co = _checkout(tmp_path)
    with pytest.raises(review.ReviewError) as e:
        review.assert_ledger_is_out_of_reach(run, checkout=co)
    assert "no ledger" in str(e.value)
```

- [ ] **Step 2: Run them to verify they fail**

```bash
uvx --with pytest pytest -q tests/test_forge_review.py
```

Expected: FAIL — `ModuleNotFoundError: No module named 'forge.review'`.

- [ ] **Step 3: Write the reviewer inputs and the ledger assertion**

Create `shared/lib/forge/review.py`:

```python
"""§13's review: the reviewer's input set, the in-process council call, and the record.

WHY THE COUNCIL IS CALLED IN PROCESS. The council CLI cannot express either contract §13
depends on. `parse_args` has no `--cwd`, so a shelled run gives agy a worktree while claude
and codex inherit the ORCHESTRATOR's cwd — the user's live checkout, dirty edits and all —
and the green header would describe a blind review that never happened. And `main()`
unconditionally injects the sentinel into the prompt, so a bundle-resident token would be a
second token nothing checks and a reviewer could quote the argv token having read nothing but
its launcher. Forge sets every reviewer's cwd itself and plants the token in the bundle.

WHAT THIS MODULE DELIBERATELY DOES NOT CALL, each with its reason:

  * `engine.apply_sentinel` — §13 puts the proof token INSIDE the bundle. Applying it to the
    prompt would make quoting it prove only that the seat read argv.
  * `engine.isolate_agy_worktree` — it repoints agy's cwd at a throwaway worktree
    (`engine.py:971-1015`). §13 requires all three reviewers' cwd to be the synthesis
    checkout; moving one of them is the ambient-context failure §13 is written to prevent.
  * `engine.make_readonly` / `apply_member_note` / `apply_readonly_posture` — all `main()`-only.
    An in-process caller inherits none of them, which is mostly what forge wants; the
    read-only posture is a choice made explicitly here rather than inherited.
  * `seat.forge_spec` — see `reviewer_specs`. Its validator neutralises the sentinel.

ROUNDS RUN SEQUENTIALLY, AND THAT IS LOAD-BEARING. `engine._LIVE_PGIDS` (`engine.py:821`),
`_LIVE_WORKTREES` (`:923`) and `_STATE` (`:924`) are process-wide, so two concurrent
`run_council` calls share them and one round's teardown reaches the other's members. §13's
rounds are sequential by definition (round 2 reviews the fix round 1 asked for), so nothing
here needs concurrency and nothing here may introduce it.

`codex exec --json`, NOT `codex review`. Measured 2026-08-03: `codex review [OPTIONS]
[PROMPT]` has no `--json`, no `--model` and no `--cd`, so the engine's `extract_codex_json`
would turn every review into a silent `parse_failure` and "found nothing" would be
indistinguishable from "could not be read". A recorded deviation from §13's text: forge
supplies the review framing itself, as the prompt.
"""
import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, fields
from pathlib import Path

from council import engine

from . import fingerprint, gitcmd, journal as journalmod, storage

SEVERITIES = ("blocker", "important", "minor")
RESOLUTIONS = ("open", "fixed", "unresolved", "rejected")

# §13 asks for `--mode deep`. `mode` is a MANIFEST LABEL ONLY — passing it does not select
# `MODE_TIMEOUT["deep"]`, and the timeout is whatever the caller passes (`engine.py:1274`,
# `:90`). A manifest reading `mode: deep` beside a 300-second timeout is a record cleaner
# than its evidence, so the number is named here and a test pins the two together.
REVIEW_TIMEOUT_SEC = engine.MODE_TIMEOUT["deep"]

_INPUTS = "inputs.json"
_INSTRUCTIONS = "REVIEW.md"
_TOKEN_FILE = "proof-token.txt"
_REVIEW_SUBDIR = ("khenrix-forge", "review")

_FENCE = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)


class ReviewError(RuntimeError):
    """A review this module will not run, or a record it will not write."""


def review_dir(checkout, round_: int) -> Path:
    """Where round `n`'s reviewer inputs live: `<git-dir>/khenrix-forge/review/round-<n>`.

    ASKED, NEVER JOINED, for `taskbundle.task_dir`'s reason: `Path(x) / ".git"` is a
    directory in an ordinary clone and a FILE in a linked worktree, so the join is right by
    luck and wrong the moment §16's synthesis worktree exists. `rev-parse
    --absolute-git-dir` loads no index, fires no hook and runs no diff driver.

    INSIDE THE GIT DIRECTORY, so the worktree stays clean: §7.3's change predicate and
    `snapshot`/`harvest` all range over the worktree, and a review input dropped there would
    arrive in the next candidate as an artifact the reviewer wrote.
    """
    if not isinstance(round_, int) or isinstance(round_, bool) or round_ < 1:
        raise ReviewError(f"a review round is numbered from 1, not {round_!r}")
    out = gitcmd.git(checkout, "rev-parse", "--absolute-git-dir",
                     env_extra=gitcmd.READONLY).stdout.strip()
    if not out:
        raise ReviewError(f"git named no git directory for {checkout}")
    return Path(out).joinpath(*_REVIEW_SUBDIR, f"round-{round_}")


_TEMPLATE = """\
# Independent review — round {round_}

You are one of three reviewers looking at this change independently. You have a shell and a
git checkout. **Everything you are given is listed below; nothing else about this run was
prepared for you.**

## The change

* Synthesis checkpoint: `{checkpoint}`
* Baseline commit: `{baseline_commit}`
* Baseline tree: `{baseline_tree}`

Run the diff yourself and cite changed-file evidence for every finding:

```
git diff {baseline_commit}..{checkpoint}
```

## The task

{task_line}

## Artifacts

{artifact_line}

## Proof of reading

Quote this token verbatim somewhere in your answer: `{token}`

It is written only in this bundle. An answer that does not carry it is recorded as a seat
that did not read its input, which is a different thing from a seat that found nothing.

## How to answer

End your answer with exactly ONE fenced JSON block, and nothing after it:

```json
{{"findings": [{{"severity": "blocker", "claim": "…", "evidence": "path:line"}}]}}
```

`severity` is one of {severities}. An empty `findings` list is a valid answer and means you
found nothing — but the block must be present, because a missing block is recorded as an
answer that could not be read rather than as a clean review.
"""


def launcher_prompt(bundle_path) -> str:
    """The small prompt that points at the bundle. §13: a launcher, not the review.

    claude and agy place the prompt in ARGV (`engine.build_real_spec`), and a task plus its
    resolved closure can still hit `E2BIG` without the raw diff — so the review instructions
    live in a file and this names it.
    """
    return (f"Read {bundle_path}/{_INSTRUCTIONS} and follow it exactly. Your working "
            "directory is the checkout under review. Do not modify any file; this is a "
            "review.")


def write_reviewer_inputs(checkout, round_: int, *, checkpoint: str, baseline_commit: str,
                          baseline_tree: str, artifact_manifest, token: str,
                          task_bundle_present: bool) -> Path:
    """Lay round `n`'s inputs down inside the checkout's git directory, and return the path.

    EVERY FILENAME HERE IS A LITERAL IN THIS MODULE, which is why this writes plainly rather
    than through `bundle`'s dir-fd descent. That machinery exists because a MANIFEST supplies
    path components an attacker controls; nothing below takes a path from any record. The
    directory is created by this call and refused if it already exists, so no earlier write
    can have laid a symlink where a later one lands.

    AN ABSENT INPUT IS STATED, NEVER OMITTED. §16's out-of-band artifact manifest is a later
    plan's artifact and §20's task bundle may not have been materialized; a reviewer told
    "there is none" can weigh that, and one that simply never sees the section cannot.
    """
    for name, value in (("checkpoint", checkpoint), ("baseline_commit", baseline_commit),
                        ("baseline_tree", baseline_tree), ("token", token)):
        if not isinstance(value, str) or not value.strip():
            raise ReviewError(f"{name} is a non-empty string, not {value!r}")
    d = review_dir(checkout, round_)
    if d.exists():
        raise ReviewError(
            f"{d} already holds round {round_}'s reviewer inputs. A second write into a live "
            "round would change what a reviewer was given after it was given it, and the "
            "round's recorded prompt identity would then describe a bundle that no longer "
            "exists.")
    d.mkdir(parents=True)
    task_line = ("The immutable original task bundle is at "
                 f"`{'/'.join(('$(git rev-parse --absolute-git-dir)', 'khenrix-forge', 'task'))}`."
                 if task_bundle_present else
                 "**There is no task bundle in this checkout.** This run did not materialize "
                 "one, so the task text is not available to you here; review the diff against "
                 "the claims it makes for itself.")
    artifact_line = (f"Out-of-band artifact manifest: `{artifact_manifest}`"
                     if artifact_manifest else
                     "There is **no out-of-band artifact manifest** for this run — §16's "
                     "manifest is not produced yet — so no artifact outside the git diff has "
                     "been declared to you.")
    text = _TEMPLATE.format(round_=round_, checkpoint=checkpoint,
                            baseline_commit=baseline_commit, baseline_tree=baseline_tree,
                            token=token, task_line=task_line, artifact_line=artifact_line,
                            severities=list(SEVERITIES))
    storage.atomic_write(d / _INSTRUCTIONS, text.encode("utf-8"))
    storage.atomic_write(d / _TOKEN_FILE, (token + "\n").encode("utf-8"))
    storage.atomic_write(d / _INPUTS, (json.dumps({
        "round": round_,
        "synthesis_checkpoint": checkpoint,
        "baseline_commit": baseline_commit,
        "baseline_tree": baseline_tree,
        "artifact_manifest": artifact_manifest,
        "task_bundle_present": bool(task_bundle_present),
    }, sort_keys=True, indent=2) + "\n").encode("utf-8"))
    return d


def _raise(err: OSError):
    """`os.walk`'s onerror. WITHOUT THIS THE SCAN BELOW IS A FALSE GREEN.

    Measured on this project (task #45 in the ledger): an unreadable subdirectory makes an
    `os.walk` with no `onerror` return nothing for that subtree, silently — so a scan looking
    for the ledger's bytes finds none and certifies a tree it could not read.
    """
    raise ReviewError(
        f"the reviewer's tree could not be scanned whole ({err.filename}: {err.strerror}), so "
        "whether the ledger's bytes are in it is a question this run could not answer. §13's "
        "blindness has to be structural, and an unreadable subtree is not a clean one.") from err


def _digests_under(root: Path, target: bytes, cap: int) -> list:
    """Every path under `root` whose bytes are `target`. Refuses past `cap` files."""
    hits, seen = [], 0
    want = hashlib.sha256(target).hexdigest()
    for dirpath, _dirnames, filenames in os.walk(root, onerror=_raise):
        for fn in filenames:
            seen += 1
            if seen > cap:
                raise ReviewError(
                    f"more than {cap} files under {root}; the ledger-exclusion scan stopped "
                    "before it finished, and a partial scan proves nothing about the part it "
                    "did not reach")
            p = Path(dirpath) / fn
            try:
                if p.is_symlink():
                    # A link's own bytes are its target text; following it would read a file
                    # this scan is not scanning, and its TARGET is either in this tree (and
                    # visited on its own) or outside it (and not in the reviewer's tree).
                    continue
                if p.stat().st_size != len(target):
                    continue
                if hashlib.sha256(p.read_bytes()).hexdigest() == want:
                    hits.append(str(p))
            except OSError as e:
                _raise(e)
    return hits


def assert_ledger_is_out_of_reach(run_dir, *, checkout) -> None:
    """§13's blindness, as a mechanical assertion rather than a sentence in a prompt.

    "The ledger path is not passed" is not enough: §13 sets every reviewer's cwd to this
    checkout and a reviewer has a shell, so the guarantee has to be that THE BYTES ARE NOT IN
    THE TREE. Two complementary checks, neither of which subsumes the other:

      1. PATH. A run directory under the checkout puts the real ledger in the reviewer's tree
         with no copy involved, and no content scan is needed to see it.
      2. CONTENT. A copy under another name has a different path and the same bytes, which is
         what the digest sweep catches — over the worktree AND over the review inputs inside
         the git directory, because those are the two places §13 puts things a reviewer reads.

    A MISSING LEDGER IS A REFUSAL. You cannot assert that bytes are out of reach without
    reading them, and "there is no ledger, so nothing was leaked" is a clean verdict produced
    by the absence of the evidence rather than by the evidence.
    """
    lp = Path(storage.ledger_path(run_dir))
    try:
        blob = lp.read_bytes()
    except FileNotFoundError as e:
        raise ReviewError(
            f"there is no ledger at {lp}, so this run cannot assert that its bytes are out of "
            "a reviewer's reach — the check would pass because the evidence is missing, which "
            "is the shape §10.1 exists to refuse") from e
    except OSError as e:
        raise ReviewError(f"the ledger at {lp} could not be read ({e.strerror}), so its "
                          "containment could not be checked") from e

    co = Path(checkout).resolve()
    if lp.resolve().is_relative_to(co):
        raise ReviewError(
            f"the ledger is at {lp}, which is under the reviewer's checkout {co}. §13 gives "
            "every reviewer a shell in this tree, so a ledger inside it is passed to the "
            "review however carefully the prompt avoids naming it.")
    cap = storage.Quota.for_harvest().max_files
    hits = _digests_under(co, blob, cap)
    gd = Path(gitcmd.git(checkout, "rev-parse", "--absolute-git-dir",
                         env_extra=gitcmd.READONLY).stdout.strip())
    review_root = gd.joinpath(*_REVIEW_SUBDIR)
    if review_root.is_dir():
        hits += _digests_under(review_root, blob, cap)
    if hits:
        raise ReviewError(
            f"the ledger's exact bytes are present in the reviewer's tree at {sorted(hits)}. "
            "§13's blind review is the strongest call in this design; a copy under another "
            "name defeats it as completely as passing the path would.")
```

- [ ] **Step 4: Run the input tests**

```bash
uvx --with pytest pytest -q tests/test_forge_review.py
```

Expected: `10 passed`. Fix the leftover `storage.Path` expression in `test_a_ledger_copied_into_the_checkout_is_caught_by_its_bytes` to plain `Path(storage.ledger_path(run)).read_bytes()` if it has not been already.

- [ ] **Step 5: Write the failing tests for the specs, the parser and the record**

Append to `tests/test_forge_review.py`:

```python
# --------------------------------------------------------------------------- specs
def test_every_reviewer_runs_from_the_synthesis_checkout(tmp_path, global_identity):
    co = _checkout(tmp_path)
    specs = review.reviewer_specs(["claude", "codex", "agy"], prompt="go",
                                  timeout=review.REVIEW_TIMEOUT_SEC, cwd=co,
                                  token="TOK", workdir=tmp_path / "wd")
    assert {s.cwd for s in specs} == {str(co)}


def test_the_codex_reviewer_uses_codex_exec_json_and_not_codex_review(tmp_path,
                                                                     global_identity):
    """MEASURED: `codex review` has no --json, so the engine's extractor would turn every
    review into a silent parse_failure and 'found nothing' would be unreadable from
    'could not be read'."""
    co = _checkout(tmp_path)
    codex = [s for s in review.reviewer_specs(["codex"], prompt="go", timeout=60, cwd=co,
                                              token="TOK", workdir=tmp_path / "wd")][0]
    assert codex.argv[:4] == ["codex", "exec", "-", "--json"]
    assert "review" not in codex.argv
    assert codex.extract is engine.extract_codex_json


def test_a_reviewer_that_never_quoted_the_token_is_not_valid(tmp_path, global_identity):
    """THE FAIL-OPEN. `seat.forge_spec`'s validator forces sentinel=None on a COPY of the
    spec regardless of what it carries, so a reviewer wired through it scores valid having
    read nothing. Reviewers get the council's own `evaluate`."""
    co = _checkout(tmp_path)
    spec = review.reviewer_specs(["claude"], prompt="go", timeout=60, cwd=co, token="TOK-9",
                                 workdir=tmp_path / "wd")[0]
    assert spec.validator is None and spec.sentinel == "TOK-9"
    assert spec.min_chars == engine.MIN_SUBSTANTIVE_CHARS
    body = json.dumps({"type": "result", "result": "x" * 600})
    valid, reason, _, _ = engine.evaluate(0, body, "", spec)
    assert not valid and reason != "ok"
    body_ok = json.dumps({"type": "result", "result": "TOK-9 " + "x" * 600})
    assert engine.evaluate(0, body_ok, "", spec)[0]


def test_the_declared_mode_and_the_passed_timeout_are_the_same_number():
    """`mode` is a manifest LABEL; passing 'deep' does not select MODE_TIMEOUT['deep']."""
    assert review.REVIEW_TIMEOUT_SEC == engine.MODE_TIMEOUT["deep"]


# --------------------------------------------------------------------------- parsing
def test_a_well_formed_block_parses():
    rows, why = review.parse_findings(
        'prose\n```json\n{"findings": [{"severity": "blocker", "claim": "c"}]}\n```\n')
    assert rows == [{"severity": "blocker", "claim": "c"}] and why == ""


def test_an_empty_findings_list_is_a_real_answer():
    rows, why = review.parse_findings('```json\n{"findings": []}\n```')
    assert rows == [] and why == ""


def test_no_block_at_all_is_unreadable_not_empty():
    """THE FAIL-OPEN. 'Found nothing' and 'could not be read' must not be one value."""
    rows, why = review.parse_findings("I reviewed it and it looks fine to me.")
    assert rows is None and "no fenced" in why


def test_two_blocks_are_unreadable_because_nobody_may_pick_one():
    rows, why = review.parse_findings(
        '```json\n{"findings": []}\n```\n```json\n{"findings": [{"severity": "blocker",'
        ' "claim": "c"}]}\n```')
    assert rows is None and "two" in why or "more than one" in why


def test_a_severity_outside_the_declared_set_is_unreadable():
    rows, why = review.parse_findings(
        '```json\n{"findings": [{"severity": "catastrophic", "claim": "c"}]}\n```')
    assert rows is None and "severity" in why


def test_a_finding_with_no_claim_is_unreadable():
    rows, why = review.parse_findings(
        '```json\n{"findings": [{"severity": "blocker"}]}\n```')
    assert rows is None


def test_malformed_json_is_unreadable():
    rows, why = review.parse_findings('```json\n{"findings": [\n```')
    assert rows is None and "json" in why.lower()


# --------------------------------------------------------------------------- the record
def _finding(round_=1, seat="claude", severity="blocker", claim="c", resolution="open"):
    return review.Finding(id=review.finding_id(round_, seat, severity, claim), round=round_,
                          seat=seat, severity=severity, claim=claim, resolution=resolution)


def test_a_findings_id_is_content_derived_and_stable():
    a = review.finding_id(1, "claude", "blocker", "the cache is unbounded")
    b = review.finding_id(1, "claude", "blocker", "the cache is unbounded")
    c = review.finding_id(2, "claude", "blocker", "the cache is unbounded")
    assert a == b and a != c and len(a) == 12


def test_a_round_round_trips_through_disk(tmp_path):
    run = _run_dir(tmp_path)
    r = review.Round(round=1, checkpoint="a" * 40, findings=(_finding(),),
                     identities=({"prompt_sha256": "x"},), seats_responded=("claude",),
                     seats_silent=(("codex", "parse_failure"),))
    h = review.write_round(run, r)
    assert len(h) == 64
    assert review.read_round(run, 1) == r


def test_a_round_is_written_once_and_never_rewritten(tmp_path):
    run = _run_dir(tmp_path)
    r = review.Round(1, "a" * 40, (), (), (), ())
    review.write_round(run, r)
    with pytest.raises(review.ReviewError):
        review.write_round(run, r)


def test_a_silent_seat_is_recorded_rather_than_dropped(tmp_path):
    run = _run_dir(tmp_path)
    review.write_round(run, review.Round(1, "a" * 40, (), (), ("claude",),
                                         (("agy", "auth_or_quota"),)))
    assert review.read_round(run, 1).seats_silent == (("agy", "auth_or_quota"),)


def test_a_finding_with_an_undeclared_severity_cannot_be_recorded():
    with pytest.raises(review.ReviewError):
        review.Finding(id="x" * 12, round=1, seat="claude", severity="catastrophic",
                       claim="c", resolution="open")


def test_a_finding_with_an_undeclared_resolution_cannot_be_recorded():
    with pytest.raises(review.ReviewError):
        review.Finding(id="x" * 12, round=1, seat="claude", severity="blocker",
                       claim="c", resolution="probably-fine")
```

- [ ] **Step 6: Run them to verify they fail**

```bash
uvx --with pytest pytest -q tests/test_forge_review.py
```

Expected: FAIL — `AttributeError: module 'forge.review' has no attribute 'reviewer_specs'`.

- [ ] **Step 7: Write the specs, the parser and the record**

Append to `shared/lib/forge/review.py`:

```python
def reviewer_specs(names, *, prompt: str, timeout: int, cwd, token: str, workdir,
                   cfg=None, build=engine.build_real_spec) -> list:
    """One `ProviderSpec` per reviewer: forge's cwd, forge's token, the council's validator.

    BUILT THROUGH `engine.build_real_spec` RATHER THAN HAND-ROLLED, because that function
    holds per-provider knowledge this module must not fork — agy's Go-style flag parsing
    STOPS at the first positional, so every flag has to precede the prompt or
    `--dangerously-skip-permissions` is silently dropped and agy returns empty in seconds.
    A second copy of that rule here is a second place for it to be lost.

    `validator` IS LEFT `None` ON PURPOSE, AND THIS IS THE OPPOSITE OF WHAT A BUILDER NEEDS.
    `seat.forge_spec` installs `_forge_validator`, which delegates to `engine.evaluate` on a
    copy of the spec with `min_chars=0` and `sentinel=None` REGARDLESS of what the passed spec
    carries (`seat.py:239-266`, its own docstring). For a builder that is right: a terse
    sign-off after forty minutes of edits must not trigger a re-run on top of half-finished
    work. For a reviewer it is fatal — one that never opened the bundle would score `valid`,
    and §13's whole proof-token argument would evaporate. `None` here means `run_provider`
    falls back to `engine.evaluate`, which calls `score_seat(text, spec.sentinel,
    spec.min_chars)` (`engine.py:1109`).
    """
    if not isinstance(token, str) or not token.strip():
        raise ReviewError(f"a reviewer's proof token is a non-empty string, not {token!r}")
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    specs = []
    for name in names:
        spec = build(name, prompt, timeout, cfg or {}, workdir)
        spec.cwd = str(cwd)
        spec.sentinel = token
        if spec.validator is not None:
            raise ReviewError(
                f"{name}: this spec carries a validator, which would replace the council's "
                "own `evaluate` — and the only validator in this package forces "
                "`sentinel=None`, so a reviewer that never opened the bundle would score "
                "valid")
        specs.append(spec)
    return specs


def parse_findings(text) -> tuple:
    """The reviewer's structured findings, or `(None, why)` when the answer cannot be read.

    THE WHOLE POINT OF THIS FUNCTION IS THE DIFFERENCE BETWEEN `[]` AND `None`. A present,
    parseable block with an empty list is a reviewer saying "I found nothing" — a real answer
    §13's loop may act on. Everything else is "this answer could not be read", which §13's
    loop must record as a SILENT SEAT rather than as a clean review. Every early return below
    is one of the ways the second thing looks like the first.
    """
    if not isinstance(text, str):
        return None, f"a reviewer's answer is text, not {type(text).__name__}"
    blocks = _FENCE.findall(text)
    if not blocks:
        return None, ("no fenced ```json block was found in this answer, so its findings "
                      "could not be read; that is not the same as finding nothing")
    if len(blocks) > 1:
        return None, (f"this answer carries more than one fenced ```json block ({len(blocks)}"
                      "), and nothing may pick between them")
    try:
        payload = json.loads(blocks[0])
    except ValueError as e:
        return None, f"the fenced block is not readable as json: {e}"
    if not isinstance(payload, dict) or not isinstance(payload.get("findings"), list):
        return None, "the fenced block carries no `findings` list"
    for row in payload["findings"]:
        if not isinstance(row, dict):
            return None, f"a finding is an object, not {type(row).__name__}"
        if row.get("severity") not in SEVERITIES:
            return None, (f"a finding's severity is one of {list(SEVERITIES)}, not "
                          f"{row.get('severity')!r}")
        if not isinstance(row.get("claim"), str) or not row["claim"].strip():
            return None, "a finding carries a non-empty claim"
    return payload["findings"], ""


def finding_id(round_: int, seat: str, severity: str, claim: str) -> str:
    """Content-derived, never a counter — §10's rule for ledger rows, applied here.

    Coverage checks compare findings across rounds; if round 2 splits or inserts a finding
    and the ids shift, the comparison compares stale identity. The ROUND is in the hash
    because "the same claim, raised again after a fix" is a different fact from "the same
    claim, still open", and the resolution field is not, because a finding's id must not
    change when it is resolved.
    """
    blob = "\0".join((str(round_), seat, severity, claim)).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]


@dataclass(frozen=True)
class Finding:
    """One reviewer's one claim, at rest."""
    id: str
    round: int
    seat: str
    severity: str
    claim: str
    resolution: str

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ReviewError(f"severity is one of {list(SEVERITIES)}, "
                              f"not {self.severity!r}")
        if self.resolution not in RESOLUTIONS:
            raise ReviewError(f"resolution is one of {list(RESOLUTIONS)}, "
                              f"not {self.resolution!r}")
        for name in ("id", "seat", "claim"):
            v = getattr(self, name)
            if not isinstance(v, str) or not v.strip():
                raise ReviewError(f"{name} is a non-empty string, not {v!r}")


@dataclass(frozen=True)
class Round:
    """One review round, as the record §13 requires the terminal transition to read.

    `seats_silent` IS WHY THIS TYPE EXISTS. A round that recorded only findings would let a
    panel of one describe itself as a panel of three: two failed reviewers contribute zero
    findings, and zero findings reads as "no blockers". Each entry is `(seat, reason)`, and
    the reason is the council's own — `parse_failure`, `auth_or_quota`, `nonzero_exit`, or
    this module's `unreadable_findings` / `unreadable_result_file`.
    """
    round: int
    checkpoint: str
    findings: tuple
    identities: tuple
    seats_responded: tuple
    seats_silent: tuple


def round_dir(run_dir, round_: int) -> Path:
    if not isinstance(round_, int) or isinstance(round_, bool) or round_ < 1:
        raise ReviewError(f"a review round is numbered from 1, not {round_!r}")
    return Path(run_dir) / "review" / f"round-{round_}"


def findings_path(run_dir, round_: int) -> Path:
    """UNDER THE RUN DIRECTORY, never in a clone. §13 requires the record to be forge's own
    and durable; `run_provider` writes its `<name>.result.txt` with a plain `write_text`
    (`engine.py:1235-1240`), so a pointer into the council workdir is not a record."""
    return round_dir(run_dir, round_) / "findings.json"


def _payload(r: Round) -> dict:
    return {
        "round": r.round,
        "checkpoint": r.checkpoint,
        "findings": [{f.name: getattr(x, f.name) for f in fields(Finding)}
                     for x in r.findings],
        "identities": list(r.identities),
        "seats_responded": list(r.seats_responded),
        "seats_silent": [list(x) for x in r.seats_silent],
    }


def write_round(run_dir, r: Round) -> str:
    """Write the round's record and return its content hash. ON RECEIPT, before any
    classification — §13: "Findings are durable state, not model memory."

    WRITE-ONCE. `exclusive_write` rather than `atomic_write`, for the manifest's reason: a
    round that was answered and then re-recorded is a review whose findings changed after the
    fact, and the `ready`/`review_blocked` transition reads this file.
    """
    if not isinstance(r, Round):
        raise ReviewError(f"a Round is required, not {type(r).__name__}")
    path = findings_path(run_dir, r.round)
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = (json.dumps(_payload(r), sort_keys=True, indent=2) + "\n").encode("utf-8")
    try:
        storage.exclusive_write(path, blob)
    except FileExistsError as e:
        raise ReviewError(
            f"{path} already records round {r.round}'s findings and is never rewritten: the "
            "terminal transition reads this file, and a second write would let a run report "
            "an outcome and then change it") from e
    return hashlib.sha256(blob).hexdigest()


def read_round(run_dir, round_: int) -> Round:
    """The round's record, type-checked. Raises when it is absent or unreadable."""
    path = findings_path(run_dir, round_)
    try:
        raw = path.read_bytes()
    except FileNotFoundError as e:
        raise ReviewError(
            f"{path} does not exist, so round {round_}'s findings were never recorded. §13's "
            "transition reads the record rather than the return value, and a missing record "
            "is `outcome_unknown` — never a clean round.") from e
    try:
        row = json.loads(raw)
    except ValueError as e:
        raise ReviewError(f"{path} is not readable as JSON: {e}") from e
    names = ("round", "checkpoint", "findings", "identities", "seats_responded",
             "seats_silent")
    missing = [n for n in names if n not in row]
    if missing:
        raise ReviewError(f"{path} is missing {missing}")
    unknown = sorted(set(row) - set(names))
    if unknown:
        raise ReviewError(f"{path} carries fields this engine does not know: {unknown}")
    return Round(round=row["round"], checkpoint=row["checkpoint"],
                 findings=tuple(Finding(**f) for f in row["findings"]),
                 identities=tuple(row["identities"]),
                 seats_responded=tuple(row["seats_responded"]),
                 seats_silent=tuple(tuple(x) for x in row["seats_silent"]))
```

- [ ] **Step 8: Run the spec, parser and record tests**

```bash
uvx --with pytest pytest -q tests/test_forge_review.py
```

Expected: `27 passed`.

- [ ] **Step 9: Write the failing test for `run_round`**

Append to `tests/test_forge_review.py`:

```python
# --------------------------------------------------------------------------- the round
ANSWER = ('I reviewed the diff.\n' + 'x' * 500 + '\nToken: {tok}\n'
          '```json\n{{"findings": [{{"severity": "blocker", "claim": "unbounded cache"}}]}}\n```')


def _fake_council(tmp_path, *, answers, record):
    """A `run_council` stand-in. NO PROVIDER IS INVOKED ANYWHERE IN THIS SUITE."""
    def run_council(specs, *, retries, timeout, backoff, workdir, prompt=None,
                    requested=None, mode=None, read_only=None, install_signal_handler=True):
        record.update(retries=retries, timeout=timeout, workdir=Path(workdir), prompt=prompt,
                      mode=mode, read_only=read_only,
                      install_signal_handler=install_signal_handler,
                      cwds=[s.cwd for s in specs], sentinels=[s.sentinel for s in specs])
        Path(workdir).mkdir(parents=True, exist_ok=True)
        providers = []
        for s in specs:
            text, valid, reason = answers[s.name]
            rf = Path(workdir) / f"{s.name}.result.txt"
            rf.write_text(text)
            providers.append({"name": s.name, "valid": valid, "reason": reason,
                              "result_text": text[:80], "result_file": str(rf),
                              "model": s.model})
        (Path(workdir) / "manifest.json").write_text(json.dumps({"providers": providers}))
        return {"providers": providers, "prompt_sha256": None}
    return run_council


def _probe(**kw):
    from forge import fingerprint
    return fingerprint.build(prompt=kw["prompt"], token=kw["token"], cli=kw["cli"],
                             bundle_sha256=kw.get("bundle_sha256"),
                             model_requested=kw.get("model_requested"),
                             model_reported=kw.get("model_reported"),
                             run=lambda *a, **k: subprocess.CompletedProcess(a, 0, "1.0", ""),
                             closure=lambda cli: "closure-" + cli)


def test_a_round_records_findings_a_silent_seat_and_three_identities(tmp_path,
                                                                    global_identity):
    run = _run_dir(tmp_path)
    _ledger(run)
    co = _checkout(tmp_path)
    seen = {}
    answers = {"claude": (ANSWER.format(tok="TOK"), True, "ok"),
               "codex": ("I found nothing." + "y" * 500, True, "ok"),
               "agy": ("", False, "auth_or_quota")}
    r = review.run_round(
        run, round_=1, checkout=co, checkpoint="a" * 40, baseline_commit="b" * 40,
        baseline_tree="c" * 40, artifact_manifest=None,
        log=journal.Journal(storage.journal_path(run)),
        run_council=_fake_council(tmp_path, answers=answers, record=seen),
        probe=_probe, make_token=lambda: "TOK")

    assert [f.claim for f in r.findings] == ["unbounded cache"]
    assert r.seats_responded == ("claude",)
    assert dict(r.seats_silent) == {"agy": "auth_or_quota",
                                    "codex": "unreadable_findings"}, \
        "a reviewer whose answer could not be read is SILENT, never a reviewer with no findings"
    assert len(r.identities) == 3


def test_the_council_never_writes_into_the_run_directory(tmp_path, global_identity):
    """HAZARD 1. `run_council` ends with (workdir/'manifest.json').write_text — plain,
    non-atomic — and `storage.manifest_path(run_dir)` is the same filename, written once."""
    run = _run_dir(tmp_path)
    _ledger(run)
    storage.exclusive_write(storage.manifest_path(run), b'{"the run": "identity"}\n')
    co = _checkout(tmp_path)
    seen = {}
    review.run_round(run, round_=1, checkout=co, checkpoint="a" * 40,
                     baseline_commit="b" * 40, baseline_tree="c" * 40,
                     artifact_manifest=None,
                     log=journal.Journal(storage.journal_path(run)),
                     run_council=_fake_council(
                         tmp_path, answers={n: (ANSWER.format(tok="TOK"), True, "ok")
                                            for n in ("claude", "codex", "agy")},
                         record=seen),
                     probe=_probe, make_token=lambda: "TOK")
    assert Path(storage.manifest_path(run)).read_bytes() == b'{"the run": "identity"}\n'
    assert seen["workdir"] != Path(run)
    assert Path(run) in seen["workdir"].parents


def test_the_signal_handler_is_not_installed_and_retries_are_zero(tmp_path, global_identity):
    """HAZARD 2. The installed handler ends in os._exit(128+signum) (engine.py:942), which
    skips every finally — so `council_round_done` never lands on a plain Ctrl-C."""
    run = _run_dir(tmp_path)
    _ledger(run)
    co = _checkout(tmp_path)
    seen = {}
    review.run_round(run, round_=1, checkout=co, checkpoint="a" * 40,
                     baseline_commit="b" * 40, baseline_tree="c" * 40,
                     artifact_manifest=None,
                     log=journal.Journal(storage.journal_path(run)),
                     run_council=_fake_council(
                         tmp_path, answers={n: (ANSWER.format(tok="TOK"), True, "ok")
                                            for n in ("claude", "codex", "agy")},
                         record=seen),
                     probe=_probe, make_token=lambda: "TOK")
    assert seen["install_signal_handler"] is False
    assert seen["retries"] == 0
    assert seen["timeout"] == review.REVIEW_TIMEOUT_SEC
    assert seen["prompt"] is None, \
        "the council manifest's prompt_sha256 is one hash of one argument and is not any " \
        "seat's identity; §11's per-seat fingerprints are recorded by this module"
    assert set(seen["sentinels"]) == {"TOK"}


def test_the_round_is_journalled_write_ahead(tmp_path, global_identity):
    run = _run_dir(tmp_path)
    _ledger(run)
    co = _checkout(tmp_path)
    log = journal.Journal(storage.journal_path(run))
    review.run_round(run, round_=1, checkout=co, checkpoint="a" * 40,
                     baseline_commit="b" * 40, baseline_tree="c" * 40,
                     artifact_manifest=None, log=log,
                     run_council=_fake_council(
                         tmp_path, answers={n: (ANSWER.format(tok="TOK"), True, "ok")
                                            for n in ("claude", "codex", "agy")},
                         record={}),
                     probe=_probe, make_token=lambda: "TOK")
    events = [e.event for e in log.read()]
    assert events == [journal.intent("council_round"), journal.done("council_round")]
    assert journal.orphans(log.read()) == ()


def test_a_round_refuses_to_start_when_the_ledger_is_reachable(tmp_path, global_identity):
    run = _run_dir(tmp_path)
    _ledger(run)
    co = _checkout(tmp_path)
    (co / "leak.json").write_bytes(Path(storage.ledger_path(run)).read_bytes())
    with pytest.raises(review.ReviewError):
        review.run_round(run, round_=1, checkout=co, checkpoint="a" * 40,
                         baseline_commit="b" * 40, baseline_tree="c" * 40,
                         artifact_manifest=None,
                         log=journal.Journal(storage.journal_path(run)),
                         run_council=_fake_council(tmp_path, answers={}, record={}),
                         probe=_probe, make_token=lambda: "TOK")


def test_a_truncated_result_text_is_not_what_gets_parsed(tmp_path, global_identity):
    """`run_provider` truncates `result_text` (engine._truncate). Parsing it would make a
    long, correct review whose JSON block fell past the cut read as unparseable."""
    run = _run_dir(tmp_path)
    _ledger(run)
    co = _checkout(tmp_path)
    long_answer = ANSWER.format(tok="TOK")
    r = review.run_round(run, round_=1, checkout=co, checkpoint="a" * 40,
                         baseline_commit="b" * 40, baseline_tree="c" * 40,
                         artifact_manifest=None,
                         log=journal.Journal(storage.journal_path(run)),
                         run_council=_fake_council(
                             tmp_path,
                             answers={n: (long_answer, True, "ok")
                                      for n in ("claude", "codex", "agy")},
                             record={}),
                         probe=_probe, make_token=lambda: "TOK")
    assert len(r.findings) == 3 and r.seats_silent == ()


def test_an_unreadable_result_file_is_a_silent_seat(tmp_path, global_identity):
    run = _run_dir(tmp_path)
    _ledger(run)
    co = _checkout(tmp_path)

    def broken(specs, **kw):
        Path(kw["workdir"]).mkdir(parents=True, exist_ok=True)
        return {"providers": [{"name": s.name, "valid": True, "reason": "ok",
                               "result_text": "x", "result_file": str(Path(kw["workdir"])
                                                                     / "gone.txt"),
                               "model": None} for s in specs],
                "prompt_sha256": None}

    r = review.run_round(run, round_=1, checkout=co, checkpoint="a" * 40,
                         baseline_commit="b" * 40, baseline_tree="c" * 40,
                         artifact_manifest=None,
                         log=journal.Journal(storage.journal_path(run)),
                         run_council=broken, probe=_probe, make_token=lambda: "TOK")
    assert r.seats_responded == ()
    assert {reason for _, reason in r.seats_silent} == {"unreadable_result_file"}
```

- [ ] **Step 10: Run them to verify they fail**

```bash
uvx --with pytest pytest -q tests/test_forge_review.py
```

Expected: FAIL — `AttributeError: module 'forge.review' has no attribute 'run_round'`.

- [ ] **Step 11: Write `run_round`**

Append to `shared/lib/forge/review.py`:

```python
COUNCIL_KIND = "council_round"


def _result_text(record) -> tuple:
    """The FULL answer this reviewer gave, or `(None, reason)`.

    NOT `record["result_text"]`, which `run_provider` passes through `_truncate`
    (`engine.py:1263`). A long, correct review whose fenced block fell past the cut would
    read as `unreadable_findings`, and this module's whole contract is that "could not be
    read" and "found nothing" stay apart — so a truncation defect must not manufacture one.
    """
    path = record.get("result_file")
    if not isinstance(path, str) or not path:
        return None, "unreadable_result_file"
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace"), ""
    except OSError:
        return None, "unreadable_result_file"


def run_round(run_dir, *, round_: int, checkout, checkpoint: str, baseline_commit: str,
              baseline_tree: str, artifact_manifest, log,
              names=tuple(engine.DEFAULT_PROVIDERS), cfg=None,
              run_council=engine.run_council, build=engine.build_real_spec,
              probe=fingerprint.build, make_token=engine.make_sentinel) -> Round:
    """One review round: plant the inputs, convene the panel, record what came back.

    ORDER, AND WHAT EACH STEP MAKES TRUE. The ledger assertion runs BEFORE anything is
    written or spent, so a run whose blindness cannot be guaranteed costs nothing. Then the
    inputs, then §14.1's write-ahead intent, then the panel, then the record — written ON
    RECEIPT and before any classification, because §13's named failure is a compaction between
    "round 2 returned" and "the orchestrator classified it" leaving `--collect` unable to tell
    two OPPOSITE terminal states apart.

    THE RETURN VALUE IS A CONVENIENCE; THE RECORD IS THE FACT. Task 5's transition reads
    `read_round`, not this.

    `probe` and `run_council` are injected and every test passes a fake: §5.2 prices a real
    panel in provider calls and a suite that spends them is one nobody runs.
    """
    assert_ledger_is_out_of_reach(run_dir, checkout=checkout)
    token = make_token()
    task_present = Path(taskbundle_task_dir(checkout)).is_dir()
    inputs = write_reviewer_inputs(checkout, round_, checkpoint=checkpoint,
                                   baseline_commit=baseline_commit,
                                   baseline_tree=baseline_tree,
                                   artifact_manifest=artifact_manifest, token=token,
                                   task_bundle_present=task_present)
    # AFTER the inputs are laid down: the token file is now inside the tree, and the check
    # above proved only that the LEDGER is not. Re-asserting here would re-scan for the same
    # bytes and find the same answer, so it is deliberately not repeated — what changed is
    # this module's own files, whose contents this module wrote.
    prompt = launcher_prompt(inputs)
    workdir = round_dir(run_dir, round_) / "council"
    specs = reviewer_specs(list(names), prompt=prompt, timeout=REVIEW_TIMEOUT_SEC,
                           cwd=checkout, token=token, workdir=workdir, cfg=cfg, build=build)
    op = f"review-round-{round_}"
    log.record(journalmod.intent(COUNCIL_KIND), operation_id=op, round=round_,
               checkpoint=checkpoint, reviewers=sorted(s.name for s in specs))
    manifest = run_council(
        specs,
        retries=0,                      # §13: --retries 0, and `gate.quote` priced it that way
        timeout=REVIEW_TIMEOUT_SEC,     # the number, not the label — see REVIEW_TIMEOUT_SEC
        backoff=0.0,
        workdir=workdir,                # NEVER run_dir: same filename as the run manifest
        prompt=None,                    # its sha256 is not any seat's identity (§11)
        requested=[s.name for s in specs],
        mode="deep",
        read_only=False,
        install_signal_handler=False,   # its handler os._exit()s past the `done` record
    )
    findings, identities, responded, silent = [], [], [], []
    by_name = {r.get("name"): r for r in manifest.get("providers", [])}
    for spec in specs:
        record = by_name.get(spec.name)
        identities.append(fingerprint.as_row(probe(
            prompt=prompt, token=token, cli=spec.name,
            bundle_sha256=None, model_requested=spec.model, model_reported=None)))
        if record is None:
            silent.append((spec.name, "no_record"))
            continue
        if not record.get("valid"):
            silent.append((spec.name, str(record.get("reason") or "invalid")))
            continue
        text, why = _result_text(record)
        if text is None:
            silent.append((spec.name, why))
            continue
        rows, why = parse_findings(text)
        if rows is None:
            silent.append((spec.name, "unreadable_findings"))
            continue
        responded.append(spec.name)
        for row in rows:
            findings.append(Finding(
                id=finding_id(round_, spec.name, row["severity"], row["claim"]),
                round=round_, seat=spec.name, severity=row["severity"],
                claim=row["claim"], resolution="open"))
    r = Round(round=round_, checkpoint=checkpoint,
              findings=tuple(findings), identities=tuple(identities),
              seats_responded=tuple(sorted(responded)),
              seats_silent=tuple(sorted(silent)))
    digest = write_round(run_dir, r)
    log.record(journalmod.done(COUNCIL_KIND), operation_id=op, round=round_,
               findings_sha256=digest, responded=len(responded), silent=len(silent))
    return r
```

Add the one import this needs at the top of the module, beside the others:

```python
from .taskbundle import task_dir as taskbundle_task_dir
```

> Imported under an alias so the call site reads as "the task bundle's directory" rather than as a local helper. `taskbundle` imports `storage` and `gitcmd` only, so there is no cycle.

- [ ] **Step 12: Run the whole file**

```bash
uvx --with pytest pytest -q tests/test_forge_review.py
```

Expected: `34 passed`.

- [ ] **Step 13: Re-run under renamed test functions**

Rename every test to `test_zz0`…`test_zzN` and re-run. Expected: `34 passed`. **Restore the original names.**

- [ ] **Step 14: Mutate every new branch**

```bash
scripts/mutate.py --file shared/lib/forge/review.py \
  --old 'install_signal_handler=False,   # its handler os._exit()s past the `done` record' \
  --new 'install_signal_handler=True,' \
  -- uvx --with pytest pytest -q tests/test_forge_review.py

scripts/mutate.py --file shared/lib/forge/review.py \
  --old '        workdir=workdir,                # NEVER run_dir: same filename as the run manifest' \
  --new '        workdir=Path(run_dir),' \
  -- uvx --with pytest pytest -q tests/test_forge_review.py

scripts/mutate.py --file shared/lib/forge/review.py \
  --old '        spec.sentinel = token' \
  --new '        spec.sentinel = None' \
  -- uvx --with pytest pytest -q tests/test_forge_review.py

scripts/mutate.py --file shared/lib/forge/review.py \
  --old '        spec.cwd = str(cwd)' \
  --new '        spec.cwd = None' \
  -- uvx --with pytest pytest -q tests/test_forge_review.py

scripts/mutate.py --file shared/lib/forge/review.py \
  --old '    if len(blocks) > 1:' \
  --new '    if False:' \
  -- uvx --with pytest pytest -q tests/test_forge_review.py

scripts/mutate.py --file shared/lib/forge/review.py \
  --old '        if rows is None:' \
  --new '        if rows is None and False:' \
  -- uvx --with pytest pytest -q tests/test_forge_review.py

scripts/mutate.py --file shared/lib/forge/review.py \
  --old 'for dirpath, _dirnames, filenames in os.walk(root, onerror=_raise):' \
  --new 'for dirpath, _dirnames, filenames in os.walk(root):' \
  -- uvx --with pytest pytest -q tests/test_forge_review.py

scripts/mutate.py --file shared/lib/forge/review.py \
  --old '    if lp.resolve().is_relative_to(co):' \
  --new '    if False:' \
  -- uvx --with pytest pytest -q tests/test_forge_review.py

scripts/mutate.py --file shared/lib/forge/review.py \
  --old '        return Path(path).read_text(encoding="utf-8", errors="replace"), ""' \
  --new '        return str(record.get("result_text")), ""' \
  -- uvx --with pytest pytest -q tests/test_forge_review.py
```

Expected: every one exits 0 (CAUGHT). Run `git status` after the wave; it must be clean.

- [ ] **Step 15: Add the test file to the Makefile, then render, gate and commit**

Extend `FORGE_TESTS` with `tests/test_forge_review.py`.

```bash
make render
git add shared/lib/forge/review.py tests/test_forge_review.py Makefile marketplaces
make verify; echo "verify rc=$?"
make precommit; echo "precommit rc=$?"
```

Expected: both `rc=0`. Then:

```bash
git commit -m "$(cat <<'EOF'
feat(forge): §13's blindness is a byte scan, and a reviewer that could not be read is silent

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UiV66Pt8cZVMq9t8WEAhpN
EOF
)"
```

---
### Task 5: The bounded loop, and the terminal read off the record

**Files:**
- Modify: `shared/lib/forge/review.py` (append)
- Modify: `tests/test_forge_review.py` (append)

**Interfaces:**

- **Consumes:** Task 4's `Round`, `Finding`, `run_round`, `read_round`, `round_dir`, `SEVERITIES`, `RESOLUTIONS`, `ReviewError`; Task 2's `progress.cap_remaining`, `progress.record_fix_start`, `progress.record_fix_done`, `progress.Progress`; `runstate.State(phase, round, attempt, verified_checkpoint, deliverable_checkpoint)` (`runstate.py:1054-1070`), `runstate.advance(state, phase) -> State` (`:1073`), `runstate.write_state(run_dir, state)` (`:1220`); `journal.orphans`; `gitcmd.git`.
- **Produces:**
  - `review.VERIFIED_NOT_INDEPENDENTLY_REVIEWED = "verified but not independently reviewed"`
  - `review.Resolution(finding_id: str, resolution: str, checkpoint: str | None, verified: bool)`
  - `review.write_resolutions(run_dir, round_, rows) -> str`; `review.read_resolutions(run_dir, round_) -> tuple | None`
  - `review.READY = "ready"`, `DEGRADED = "degraded"`, `REVIEW_BLOCKED = "review_blocked"`
  - `review.terminal_from_record(run_dir, *, rounds_run: int, events) -> tuple[str, str]`
  - `review.settle(run_dir, state, *, rounds_run, events) -> tuple`
  - `review.loop(run_dir, *, checkout, checkpoint, baseline_commit, baseline_tree, artifact_manifest, log, manifest, fix, run=run_round) -> tuple[str, str]`

**Contradiction 6, resolved here.** §14.2 gives a successful post-round-2 fix the terminal `review_blocked`; §13.1 says its own fix creates "no new loop, no new state", which lands an otherwise-clean run in `ready`. **This plan decides the terminal by the finding's resolution, not by which reviewer found it:** an unresolved blocker is `review_blocked`; a blocker that was fixed and verified but not re-reviewed is `degraded`. Identical evidence gets one terminal, `review_blocked` keeps meaning "a blocker is still open", and `degraded` is already a declared successor of `reviewing` (`runstate.py:1029-1030`).

**The input that would make this read cleaner than its evidence.** A round whose record is missing after a crash. The obvious implementation reads what it can and returns `ready`, which is exactly §13's named failure: "a compaction between 'round 2 returned' and 'the orchestrator classified it' leaves `--collect` unable to tell those two *opposite* terminal states apart, and the wrong one ships a clean header over an unresolved blocker." A missing record and an orphaned `council_round_start` both **raise**, following `runner._refuse_an_unknown_outcome`. The second: a round with open blockers and no resolutions record reading as "nothing needed fixing" — absence of a fix record is an unfixed blocker, never a resolved one.

---

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_forge_review.py`:

```python
# --------------------------------------------------------------------------- the loop
class _Cap:
    def __init__(self, cap=3, rounds=2):
        self.synthesis_fix_cap = cap
        self.review_rounds = rounds


def _blocker(round_=1, seat="claude", claim="unbounded cache"):
    return review.Finding(id=review.finding_id(round_, seat, "blocker", claim),
                          round=round_, seat=seat, severity="blocker", claim=claim,
                          resolution="open")


def _clean_round(run, n, checkpoint="a" * 40, silent=()):
    review.write_round(run, review.Round(n, checkpoint, (), (), ("claude", "codex", "agy"),
                                         tuple(silent)))


def test_one_string_for_the_unreviewed_label():
    assert review.VERIFIED_NOT_INDEPENDENTLY_REVIEWED == \
        "verified but not independently reviewed"


def test_a_clean_single_round_with_a_full_panel_is_ready(tmp_path):
    run = _run_dir(tmp_path)
    _clean_round(run, 1)
    assert review.terminal_from_record(run, rounds_run=1, events=())[0] == review.READY


def test_a_silent_seat_degrades_a_round_that_found_nothing(tmp_path):
    """A panel of one describing itself as a panel of three is what §13's whole record is
    written against."""
    run = _run_dir(tmp_path)
    _clean_round(run, 1, silent=(("agy", "auth_or_quota"),))
    answer, why = review.terminal_from_record(run, rounds_run=1, events=())
    assert answer == review.DEGRADED and "agy" in why


def test_an_open_blocker_with_no_resolution_record_is_review_blocked(tmp_path):
    """Absence of a fix record is an unfixed blocker, never a resolved one."""
    run = _run_dir(tmp_path)
    review.write_round(run, review.Round(1, "a" * 40, (_blocker(),), (),
                                         ("claude", "codex", "agy"), ()))
    answer, _ = review.terminal_from_record(run, rounds_run=1, events=())
    assert answer == review.REVIEW_BLOCKED


def test_a_blocker_fixed_and_then_re_reviewed_clean_is_ready(tmp_path):
    run = _run_dir(tmp_path)
    b = _blocker()
    review.write_round(run, review.Round(1, "a" * 40, (b,), (),
                                         ("claude", "codex", "agy"), ()))
    review.write_resolutions(run, 1, (review.Resolution(b.id, "fixed", "b" * 40, True),))
    _clean_round(run, 2, checkpoint="b" * 40)
    assert review.terminal_from_record(run, rounds_run=2, events=())[0] == review.READY


def test_a_blocker_fixed_in_the_last_round_is_degraded_not_ready(tmp_path):
    """CONTRADICTION 6's resolution: fixed-but-not-re-reviewed is `degraded`, whether the
    finding came from review round 2 or from ultrareview."""
    run = _run_dir(tmp_path)
    b = _blocker(round_=2)
    _clean_round(run, 1)
    review.write_round(run, review.Round(2, "b" * 40, (b,), (),
                                         ("claude", "codex", "agy"), ()))
    review.write_resolutions(run, 2, (review.Resolution(b.id, "fixed", "c" * 40, True),))
    answer, why = review.terminal_from_record(run, rounds_run=2, events=())
    assert answer == review.DEGRADED
    assert review.VERIFIED_NOT_INDEPENDENTLY_REVIEWED in why


def test_a_fix_that_broke_verify_leaves_the_finding_unresolved(tmp_path):
    """§13: 'a fix that breaks verify is reverted and the finding reported unresolved'."""
    run = _run_dir(tmp_path)
    b = _blocker()
    review.write_round(run, review.Round(1, "a" * 40, (b,), (),
                                         ("claude", "codex", "agy"), ()))
    review.write_resolutions(run, 1, (review.Resolution(b.id, "unresolved", None, False),))
    assert review.terminal_from_record(run, rounds_run=1, events=())[0] \
        == review.REVIEW_BLOCKED


def test_a_non_blocker_finding_does_not_block(tmp_path):
    run = _run_dir(tmp_path)
    minor = review.Finding(id=review.finding_id(1, "agy", "minor", "typo"), round=1,
                           seat="agy", severity="minor", claim="typo", resolution="open")
    review.write_round(run, review.Round(1, "a" * 40, (minor,), (),
                                         ("claude", "codex", "agy"), ()))
    assert review.terminal_from_record(run, rounds_run=1, events=())[0] == review.READY


def test_a_missing_round_record_refuses_rather_than_classifying(tmp_path):
    run = _run_dir(tmp_path)
    _clean_round(run, 1)
    with pytest.raises(review.ReviewError):
        review.terminal_from_record(run, rounds_run=2, events=())


def test_an_orphaned_council_round_refuses_rather_than_classifying(tmp_path):
    """§14.1: a start with no receipt is `outcome_unknown` and is never silently retried."""
    run = _run_dir(tmp_path)
    _clean_round(run, 1)
    log = journal.Journal(storage.journal_path(run))
    log.record(journal.intent(review.COUNCIL_KIND), operation_id="review-round-1", round=1)
    with pytest.raises(review.ReviewError) as e:
        review.terminal_from_record(run, rounds_run=1, events=log.read())
    assert "review-round-1" in str(e.value)


def test_the_resolutions_record_is_written_once(tmp_path):
    run = _run_dir(tmp_path)
    rows = (review.Resolution("x" * 12, "fixed", "b" * 40, True),)
    review.write_resolutions(run, 1, rows)
    with pytest.raises(review.ReviewError):
        review.write_resolutions(run, 1, rows)


def test_a_resolution_naming_a_fixed_finding_with_no_checkpoint_is_refused():
    with pytest.raises(review.ReviewError):
        review.Resolution("x" * 12, "fixed", None, True)


def test_a_resolution_claiming_fixed_and_unverified_is_refused():
    with pytest.raises(review.ReviewError):
        review.Resolution("x" * 12, "fixed", "b" * 40, False)


def test_the_loop_stops_at_two_rounds_and_never_buys_a_third(tmp_path, global_identity):
    run = _run_dir(tmp_path)
    _ledger(run)
    co = _checkout(tmp_path)
    log = journal.Journal(storage.journal_path(run))
    rounds = []

    def fake_round(run_dir, *, round_, checkpoint, **kw):
        b = _blocker(round_=round_)
        r = review.Round(round_, checkpoint, (b,), (), ("claude", "codex", "agy"), ())
        review.write_round(run_dir, r)
        rounds.append(round_)
        return r

    def fix(findings, checkpoint):
        return checkpoint[:-1] + "f", True

    answer, why = review.loop(run, checkout=co, checkpoint="a" * 40,
                              baseline_commit="b" * 40, baseline_tree="c" * 40,
                              artifact_manifest=None, log=log, manifest=_Cap(),
                              fix=fix, run=fake_round)
    assert rounds == [1, 2]
    assert answer == review.REVIEW_BLOCKED


def test_the_loop_stops_when_the_synthesis_fix_cap_is_exhausted(tmp_path, global_identity):
    run = _run_dir(tmp_path)
    _ledger(run)
    co = _checkout(tmp_path)
    log = journal.Journal(storage.journal_path(run))
    calls = []

    def fake_round(run_dir, *, round_, checkpoint, **kw):
        b = _blocker(round_=round_)
        r = review.Round(round_, checkpoint, (b,), (), ("claude", "codex", "agy"), ())
        review.write_round(run_dir, r)
        return r

    def fix(findings, checkpoint):
        calls.append(checkpoint)
        return checkpoint[:-1] + "f", True

    answer, why = review.loop(run, checkout=co, checkpoint="a" * 40,
                              baseline_commit="b" * 40, baseline_tree="c" * 40,
                              artifact_manifest=None, log=log, manifest=_Cap(cap=0),
                              fix=fix, run=fake_round)
    assert calls == [], "a cap of zero funds no fix at all"
    assert answer == review.REVIEW_BLOCKED and "cap" in why


def test_the_loop_records_a_fix_pair_on_the_journal(tmp_path, global_identity):
    run = _run_dir(tmp_path)
    _ledger(run)
    co = _checkout(tmp_path)
    log = journal.Journal(storage.journal_path(run))

    def fake_round(run_dir, *, round_, checkpoint, **kw):
        findings = (_blocker(round_=round_),) if round_ == 1 else ()
        r = review.Round(round_, checkpoint, findings, (), ("claude", "codex", "agy"), ())
        review.write_round(run_dir, r)
        return r

    head = subprocess.run(["git", "-C", str(co), "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()

    def fix(findings, checkpoint):
        return head, True

    answer, _ = review.loop(run, checkout=co, checkpoint=head, baseline_commit="b" * 40,
                            baseline_tree="c" * 40, artifact_manifest=None, log=log,
                            manifest=_Cap(), fix=fix, run=fake_round)
    from forge import progress
    kinds = [e.event for e in log.read()]
    assert journal.intent(progress.FIX_KIND) in kinds
    assert journal.done(progress.FIX_KIND) in kinds
    assert journal.orphans(log.read()) == ()
    assert answer == review.READY


def test_settle_advances_the_run_state_through_a_declared_edge(tmp_path):
    from forge import runstate
    run = _run_dir(tmp_path)
    _clean_round(run, 1)
    state = runstate.State(phase="reviewing", round=1, attempt=0,
                           verified_checkpoint="a" * 40, deliverable_checkpoint="a" * 40)
    new, why = review.settle(run, state, rounds_run=1, events=())
    assert new.phase == review.READY
    assert runstate.read_state(run).phase == review.READY and why
```

- [ ] **Step 2: Run them to verify they fail**

```bash
uvx --with pytest pytest -q tests/test_forge_review.py -k "terminal or loop or resolution or settle or unreviewed"
```

Expected: FAIL — `AttributeError: module 'forge.review' has no attribute 'VERIFIED_NOT_INDEPENDENTLY_REVIEWED'`.

- [ ] **Step 3: Write the loop and the transition**

Append to `shared/lib/forge/review.py`:

```python
# ONE STRING FOR ONE PREDICATE. §13 and §14.2 say "verified but not independently REVIEWED";
# §13.1 says "RE-reviewed". Two spellings of one fact is two things a reader has to notice
# are the same, and a grep for either finds half the run's states. Every module that reports
# this imports the constant.
VERIFIED_NOT_INDEPENDENTLY_REVIEWED = "verified but not independently reviewed"

READY = "ready"
DEGRADED = "degraded"
REVIEW_BLOCKED = "review_blocked"
TERMINALS = (READY, DEGRADED, REVIEW_BLOCKED)

_BLOCKER = "blocker"
_OPEN = ("open", "unresolved")


@dataclass(frozen=True)
class Resolution:
    """What happened to one finding, and whether the fix survived the gate.

    `verified` IS NOT DERIVABLE FROM `resolution`, and the pair is checked here. §13 requires
    a fix to be re-verified in a fresh clone and a fix that BREAKS verify to be reverted with
    the finding reported unresolved — so `fixed` with `verified=False` describes a state §13
    forbids, and a record carrying it would let a reverted change count as a repair.
    """
    finding_id: str
    resolution: str
    checkpoint: str | None
    verified: bool

    def __post_init__(self) -> None:
        if self.resolution not in RESOLUTIONS:
            raise ReviewError(f"resolution is one of {list(RESOLUTIONS)}, "
                              f"not {self.resolution!r}")
        if not isinstance(self.verified, bool):
            raise ReviewError(f"verified is a bool, not {self.verified!r}")
        if self.resolution == "fixed":
            if not self.verified:
                raise ReviewError(
                    "a finding recorded `fixed` was verified: §13 reverts a fix that breaks "
                    "verify and reports the finding unresolved, so `fixed` and unverified is "
                    "a state this run may not be in")
            if not isinstance(self.checkpoint, str) or not self.checkpoint.strip():
                raise ReviewError(
                    "a fix names the checkpoint it produced; §13 cuts a fresh checkpoint "
                    "after every fix and §14.1 makes git the ordering of record")


def resolutions_path(run_dir, round_: int) -> Path:
    return round_dir(run_dir, round_) / "resolutions.json"


def write_resolutions(run_dir, round_: int, rows) -> str:
    """Record what one round's fix pass did. Write-once, for `write_round`'s reason."""
    rows = tuple(rows)
    wrong = sorted({type(r).__name__ for r in rows if not isinstance(r, Resolution)})
    if wrong:
        raise ReviewError(f"resolutions are Resolution records, not {wrong}")
    path = resolutions_path(run_dir, round_)
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = (json.dumps([{f.name: getattr(r, f.name) for f in fields(Resolution)}
                        for r in rows], sort_keys=True, indent=2) + "\n").encode("utf-8")
    try:
        storage.exclusive_write(path, blob)
    except FileExistsError as e:
        raise ReviewError(f"{path} already records round {round_}'s resolutions and is never "
                          "rewritten") from e
    return hashlib.sha256(blob).hexdigest()


def read_resolutions(run_dir, round_: int):
    """This round's resolutions, or `None` when no fix pass ran.

    `None` RATHER THAN `()`. A round that recorded no fix and a round whose fix resolved
    nothing are different facts, and only one of them means the blockers are still open
    because nobody tried. `terminal_from_record` reads both as blocking, but it says which.
    """
    path = resolutions_path(run_dir, round_)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        rows = json.loads(raw)
    except ValueError as e:
        raise ReviewError(f"{path} is not readable as JSON: {e}") from e
    if not isinstance(rows, list):
        raise ReviewError(f"{path} holds a list of resolutions, not "
                          f"{type(rows).__name__}")
    return tuple(Resolution(**r) for r in rows)


def terminal_from_record(run_dir, *, rounds_run: int, events) -> tuple:
    """§13's terminal, read off the RECORD rather than off any return value.

    §13's named failure is exactly what this function exists to prevent: "a compaction
    between 'round 2 returned' and 'the orchestrator classified it' leaves `--collect` unable
    to tell those two OPPOSITE terminal states apart, and the wrong one ships a clean header
    over an unresolved blocker." So a round whose record is missing, or whose
    `council_round_start` has no matching `_done`, is a REFUSAL — never a round classified
    out of what happens to be on disk.

    PRECEDENCE: `review_blocked` > `degraded` > `ready`.

      * `review_blocked` — any blocker whose effective resolution is `open` or `unresolved`,
        including every blocker in a round with no resolutions record at all. Absence of a fix
        record is an unfixed blocker.
      * `degraded` — every blocker resolved, but something about the review is weaker than a
        clean one: a blocker fixed in the LAST round (nothing re-reviewed the fix), or any
        round in which a reviewer was silent. `VERIFIED_NOT_INDEPENDENTLY_REVIEWED` is the
        phrase, and CONTRADICTION 6 is settled here — the terminal follows the finding's
        resolution, not which reviewer raised it, so §13.1's ultrareview fix and §14.2's
        post-round-2 fix land in the same place on the same evidence.
      * `ready` — every blocker resolved and re-reviewed, and every round's panel whole.
    """
    if not isinstance(rounds_run, int) or isinstance(rounds_run, bool) or rounds_run < 1:
        raise ReviewError(f"a review ran at least one round, not {rounds_run!r}")
    orphaned = [e for e in journalmod.orphans(events)
                if e.event == journalmod.intent(COUNCIL_KIND)]
    if orphaned:
        raise ReviewError(
            "a council round started and recorded no result (operation "
            f"{', '.join(sorted(e.operation_id for e in orphaned))}), so whether it returned "
            "findings is unknown. §14.1 names that `outcome_unknown` and forbids retrying it "
            "silently; a terminal chosen here would be a clean header over a round nobody read.")
    blocked, degraded = [], []
    for n in range(1, rounds_run + 1):
        r = read_round(run_dir, n)               # raises when the record is missing
        res = read_resolutions(run_dir, n)
        by_id = {x.finding_id: x for x in (res or ())}
        for f in r.findings:
            if f.severity != _BLOCKER:
                continue
            fixed = by_id.get(f.id)
            if fixed is None:
                blocked.append(
                    f"round {n}: {f.seat} raised a blocker ({f.claim!r}) and "
                    + ("no fix pass was recorded for that round"
                       if res is None else "this round's fix pass did not resolve it"))
            elif fixed.resolution in _OPEN:
                blocked.append(f"round {n}: {f.seat}'s blocker ({f.claim!r}) is "
                               f"{fixed.resolution}")
            elif fixed.resolution == "fixed" and n == rounds_run:
                degraded.append(
                    f"round {n}: {f.seat}'s blocker ({f.claim!r}) was fixed at checkpoint "
                    f"{fixed.checkpoint} and no later round reviewed it — "
                    f"{VERIFIED_NOT_INDEPENDENTLY_REVIEWED}")
        if r.seats_silent:
            degraded.append(
                f"round {n} was answered by {len(r.seats_responded)} of "
                f"{len(r.seats_responded) + len(r.seats_silent)} reviewers; silent: "
                + ", ".join(f"{s} ({why})" for s, why in r.seats_silent))
    if blocked:
        return REVIEW_BLOCKED, "; ".join(blocked)
    if degraded:
        return DEGRADED, "; ".join(degraded)
    return READY, (f"{rounds_run} round(s), every panel whole and no blocker left open")


def settle(run_dir, state, *, rounds_run: int, events) -> tuple:
    """Read the record, take §14's edge, and persist the new position.

    `runstate.advance` already refuses an undeclared edge and `reviewing`'s successors already
    include all three terminals (`runstate.py:1029-1030`), so this adds no graph — it adds the
    rule that the terminal comes from the record.
    """
    from . import runstate                       # local: runstate imports nothing from here
    terminal, why = terminal_from_record(run_dir, rounds_run=rounds_run, events=events)
    new = runstate.advance(state, terminal)
    runstate.write_state(run_dir, new)
    return new, why


def _tree_of(checkout, commit):
    """The checkpoint's TREE oid, or None. §12.3's sighting is about content recurring.

    FAILS CLOSED TO `None`, never to `""`: an empty-string tree id used as a sighting key
    would make every unrecorded attempt "the same tree" and fire the oscillation stop signal
    on the second one.
    """
    r = gitcmd.git(checkout, "rev-parse", f"{commit}^{{tree}}",
                   env_extra=gitcmd.READONLY, check=False)
    out = r.stdout.strip()
    return out if r.returncode == 0 and out else None


def loop(run_dir, *, checkout, checkpoint: str, baseline_commit: str, baseline_tree: str,
         artifact_manifest, log, manifest, fix, run=None) -> tuple:
    """§13's bounded review loop. NOT a convergence loop, and it never buys a third round.

    §13: "Round-1 blocker → fix, verify, checkpoint, round 2. Round-2 blocker → terminal state
    `review_blocked`, regardless of verify." The bound is `manifest.review_rounds`, which is
    the number §5.2 priced, and the fix budget is `manifest.synthesis_fix_cap`, which is the
    number §5.2 priced separately — see `progress.cap_remaining` for why counting STARTS is
    what makes a crashed fix stay spent.

    `fix` IS INJECTED AND IS PLAN J'S. Its contract is
    `fix(findings, checkpoint) -> (new_checkpoint | None, verified: bool)`: apply the round's
    blockers, re-verify in a FRESH clone the builder never touched (§6), cut a checkpoint, and
    say whether verify passed. A `(None, False)` or a `(_, False)` answer is §13's "a fix that
    breaks verify is reverted and the finding reported unresolved" — this loop records that
    and stops rather than trying again, because a second attempt at one round's blockers is
    the convergence loop §13 refuses.
    """
    from . import progress                       # local: progress imports nothing from here
    runner = run or run_round
    rounds = getattr(manifest, "review_rounds", None)
    if not isinstance(rounds, int) or isinstance(rounds, bool) or rounds < 1:
        raise ReviewError(
            f"the run's manifest records review_rounds={rounds!r}; §5.2 priced the panel by "
            "that number and a default chosen here would convene rounds nobody paid for")
    current = checkpoint
    n = 0
    while n < rounds:
        n += 1
        r = runner(run_dir, round_=n, checkout=checkout, checkpoint=current,
                   baseline_commit=baseline_commit, baseline_tree=baseline_tree,
                   artifact_manifest=artifact_manifest, log=log)
        blockers = [f for f in r.findings if f.severity == _BLOCKER]
        if not blockers:
            break
        if n >= rounds:
            # §13's terminal case: a round-2 blocker is `review_blocked` regardless of
            # verify, and the loop does not spend a fix it cannot have re-reviewed.
            break
        remaining = progress.cap_remaining(manifest, log.read())
        if remaining <= 0:
            write_resolutions(run_dir, n, tuple(
                Resolution(f.id, "unresolved", None, False) for f in blockers))
            return REVIEW_BLOCKED, (
                f"round {n} raised {len(blockers)} blocker(s) and §12.3's synthesis-fix cap "
                "is exhausted, so no fix was attempted")
        op = f"review-fix-{n}"
        progress.record_fix_start(log, operation_id=op, tree_oid=_tree_of(checkout, current))
        new_checkpoint, verified = fix(tuple(blockers), current)
        progress.record_fix_done(
            log, operation_id=op,
            tree_oid=(_tree_of(checkout, new_checkpoint) if new_checkpoint else None),
            prog=progress.Progress(None, None))
        if not verified or not new_checkpoint:
            write_resolutions(run_dir, n, tuple(
                Resolution(f.id, "unresolved", None, False) for f in blockers))
            return REVIEW_BLOCKED, (
                f"round {n}'s fix did not pass verify, so it was reverted and its "
                f"{len(blockers)} blocker(s) are reported unresolved (§13)")
        write_resolutions(run_dir, n, tuple(
            Resolution(f.id, "fixed", new_checkpoint, True) for f in blockers))
        current = new_checkpoint
    return terminal_from_record(run_dir, rounds_run=n, events=log.read())
```

> `progress.Progress(None, None)` is what the loop records for a fix's failing-test set, and that is honest rather than lazy: this loop does not run the gate — `fix` does, in a fresh verifier clone — so the failing-test fingerprints are Plan J's to measure and thread back. When Plan J wires a real `fix`, the `Progress` it measured is what belongs here, and until then an unmeasured pair forms no sighting and `progress.oscillation` answers `unknown`. **Do not substitute `frozenset()`.**

- [ ] **Step 4: Run the whole file**

```bash
uvx --with pytest pytest -q tests/test_forge_review.py
```

Expected: `50 passed`.

- [ ] **Step 5: Assert the label has one spelling**

Append to `tests/test_forge_seams.py`:

```python
def test_the_unreviewed_label_is_spelled_in_exactly_one_place():
    """SEAM: §13/§14.2 say 'independently reviewed' and §13.1 says 'independently
    re-reviewed'. Two spellings of one predicate is two things a reader has to notice are
    the same, and a grep for either finds half the run's states."""
    root = Path(__file__).resolve().parents[1] / "shared" / "lib" / "forge"
    literal = '"verified but not independently reviewed"'
    holders = [p.name for p in sorted(root.glob("*.py"))
               if literal in p.read_text(encoding="utf-8")]
    assert holders == ["review.py"], holders
    assert not any("independently re-reviewed" in p.read_text(encoding="utf-8")
                   for p in root.glob("*.py"))
```

- [ ] **Step 6: Mutate the loop's branches**

```bash
scripts/mutate.py --file shared/lib/forge/review.py \
  --old '        if n >= rounds:' \
  --new '        if False:' \
  -- uvx --with pytest pytest -q tests/test_forge_review.py

scripts/mutate.py --file shared/lib/forge/review.py \
  --old '        if remaining <= 0:' \
  --new '        if False:' \
  -- uvx --with pytest pytest -q tests/test_forge_review.py

scripts/mutate.py --file shared/lib/forge/review.py \
  --old '            if fixed is None:' \
  --new '            if False:' \
  -- uvx --with pytest pytest -q tests/test_forge_review.py

scripts/mutate.py --file shared/lib/forge/review.py \
  --old '            elif fixed.resolution == "fixed" and n == rounds_run:' \
  --new '            elif False:' \
  -- uvx --with pytest pytest -q tests/test_forge_review.py

scripts/mutate.py --file shared/lib/forge/review.py \
  --old '        if r.seats_silent:' \
  --new '        if False:' \
  -- uvx --with pytest pytest -q tests/test_forge_review.py

scripts/mutate.py --file shared/lib/forge/review.py \
  --old '    if orphaned:' \
  --new '    if False:' \
  -- uvx --with pytest pytest -q tests/test_forge_review.py

scripts/mutate.py --file shared/lib/forge/review.py \
  --old '        if not verified or not new_checkpoint:' \
  --new '        if False:' \
  -- uvx --with pytest pytest -q tests/test_forge_review.py
```

Expected: every one exits 0 (CAUGHT). Run `git status` after the wave; it must be clean.

- [ ] **Step 7: Render, gate and commit**

```bash
make render
git add shared/lib/forge/review.py tests/test_forge_review.py tests/test_forge_seams.py \
        marketplaces
make verify; echo "verify rc=$?"
make precommit; echo "precommit rc=$?"
```

Expected: both `rc=0`. Then:

```bash
git commit -m "$(cat <<'EOF'
feat(forge): §13's terminal comes off the record, and a fixed-but-unreviewed blocker degrades

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UiV66Pt8cZVMq9t8WEAhpN
EOF
)"
```

---

### Task 6: Ultrareview — minutes, six reasons, and a diff nobody measured

**Files:**
- Create: `shared/lib/forge/ultra.py`
- Create: `tests/test_forge_ultra.py`
- Modify: `Makefile:21-32`

**Interfaces:**

- **Consumes:** `review.VERIFIED_NOT_INDEPENDENTLY_REVIEWED`, `review.SEVERITIES`, `review.Finding`, `review.finding_id` (Task 4/5); `gitcmd.git`, `gitcmd.NO_DAEMON_CACHE`, `gitcmd.READONLY`; `storage.atomic_write`; `journal.Journal`.
- **Produces:**
  - `ultra.UltraError(RuntimeError)`
  - `ultra.TIMEOUT_MINUTES_DEFAULT = 30`, `ultra.TIMEOUT_MINUTES_MAX = 120`
  - `ultra.DIFF_FILE_LIMIT = 500`, `ultra.DIFF_LINE_LIMIT = 8000`
  - `ultra.RAN = "ran"`, `UNAVAILABLE = "unavailable"`, `TIMED_OUT = "timeout"`, `SKIPPED = "skipped"`, `STATUSES`
  - `ultra.REASONS = ("no_auth", "zdr_org", "diff_too_large", "usage_credits_off", "exit_1", "unreadable_output")`
  - `ultra.DiffSize(files: int | None, lines: int | None, why: str)`
  - `ultra.measure_diff(checkout, base, head) -> DiffSize`
  - `ultra.argv(*, timeout_minutes: int, target: str | None) -> list[str]`
  - `ultra.classify(exit_code, stdout, stderr) -> str | None`
  - `ultra.session_url(stderr) -> str | None`
  - `ultra.Ultra(status, reason, bugs, session_url, diff_measured, detail)`
  - `ultra.run_ultra(run_dir, *, checkout, base, head, enabled=True, timeout_minutes=TIMEOUT_MINUTES_DEFAULT, target=None, run=subprocess.run) -> Ultra`

**The input that would make this read cleaner than its evidence — three of them.**
1. **A timeout value in seconds.** Measured 2026-08-03: `claude ultrareview --timeout <minutes>` (default 30). Every other timeout in forge and the council is seconds — `engine.MODE_TIMEOUT["deep"]` is 1200 and `verify.Step.timeout` defaults to 600. Passing 1200 here asks for a **20-hour** review. The guard is structural, not a comment: `argv` refuses anything outside `1..TIMEOUT_MINUTES_MAX`, so every seconds-shaped constant in this package is rejected at the call.
2. **An exit-0 whose `--json` payload does not parse, read as "ran, no bugs found".** §13.1 names five unavailability reasons and none covers it. This plan adds `unreadable_output` as a sixth and says so.
3. **A binary diff.** `git diff --numstat` prints `-` for binary files exactly as `git apply --numstat` does (measured). Summing them as 0 would report a huge diff as under §13.1's 8,000-line limit. `measure_diff` returns `lines=None`, and `run_ultra` then **runs the review anyway** — the remote is the authority on its own limits — while recording `diff_measured=False`, so the record never claims the diff was under a limit nobody measured.

**A measurement this plan could not take.** §M4 of the design pass flags it and it is still open: `claude ultrareview` reviews "the current branch (or a PR number / base branch)", and `fleet.clone_seat` removes `origin` (`fleet.py:264-270`). **Whether it resolves a base branch in a clone with no remote is UNTESTED**, because testing it spends money and this plan's Global Constraints forbid that. The module docstring must carry `NOT MEASURED:` for this exactly, and §13.1's degrade path (exit 1 → `unavailable`) is what stands under it. **Do not write a sentence claiming the path works.**

---

- [ ] **Step 1: Write the failing tests**

Create `tests/test_forge_ultra.py`:

```python
"""§13.1's ultrareview: minutes, the six reasons, and a diff nobody measured."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

from council import engine  # noqa: E402
from forge import review, ultra, verify  # noqa: E402

from forge_fixtures import commit_all, global_identity, make_repo, write  # noqa: E402,F401


def _repo(tmp_path):
    r = make_repo(tmp_path)
    write(r, "a.txt", "one\n")
    base = commit_all(r, "base")
    write(r, "a.txt", "one\ntwo\n")
    write(r, "b.txt", "new\n")
    head = commit_all(r, "head")
    return r, base, head


def _proc(rc=0, out="", err=""):
    def run(argv, **kw):
        run.calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, rc, out, err)
    run.calls = []
    return run


def test_the_timeout_is_in_minutes_and_a_seconds_value_is_refused():
    """MEASURED 2026-08-03: --timeout <minutes>, default 30. MODE_TIMEOUT['deep'] is 1200."""
    a = ultra.argv(timeout_minutes=30, target=None)
    assert a[:2] == ["claude", "ultrareview"]
    assert a[a.index("--timeout") + 1] == "30"
    assert "--json" in a
    with pytest.raises(ultra.UltraError):
        ultra.argv(timeout_minutes=engine.MODE_TIMEOUT["deep"], target=None)
    with pytest.raises(ultra.UltraError):
        ultra.argv(timeout_minutes=verify.Step(argv=("true",)).timeout, target=None)
    with pytest.raises(ultra.UltraError):
        ultra.argv(timeout_minutes=0, target=None)


def test_a_target_is_the_last_positional():
    assert ultra.argv(timeout_minutes=30, target="main")[-1] == "main"


def test_a_text_diff_is_measured(tmp_path, global_identity):
    r, base, head = _repo(tmp_path)
    d = ultra.measure_diff(r, base, head)
    assert (d.files, d.lines) == (2, 2) and d.why == ""


def test_a_binary_diff_leaves_the_line_count_unknown(tmp_path, global_identity):
    r, base, _ = _repo(tmp_path)
    (r / "blob.bin").write_bytes(b"\x00\x01\x02\x03")
    head = commit_all(r, "binary")
    d = ultra.measure_diff(r, base, head)
    assert d.lines is None and d.files == 3
    assert "blob.bin" in d.why


def test_an_oversized_diff_is_unavailable_without_spending(tmp_path, global_identity):
    r, base, head = _repo(tmp_path)
    run = _proc()
    u = ultra.run_ultra(tmp_path / "run", checkout=r, base=base, head=head,
                        run=run, file_limit=1)
    assert u.status == ultra.UNAVAILABLE and u.reason == "diff_too_large"
    assert run.calls == [], "the local pre-flight must refuse before the remote is paid for"


def test_an_unmeasurable_diff_runs_and_records_that_nobody_measured_it(tmp_path,
                                                                      global_identity):
    """The remote is the authority on its own limits. What must never happen is a record
    saying the diff was under the limit when its line count was never taken."""
    r, base, _ = _repo(tmp_path)
    (r / "blob.bin").write_bytes(b"\x00\x01\x02\x03")
    head = commit_all(r, "binary")
    run = _proc(0, json.dumps({"bugs": []}))
    u = ultra.run_ultra(tmp_path / "run", checkout=r, base=base, head=head, run=run)
    assert u.status == ultra.RAN and u.diff_measured is False
    assert len(run.calls) == 1


def test_a_clean_json_payload_parses_into_findings(tmp_path, global_identity):
    r, base, head = _repo(tmp_path)
    payload = {"bugs": [{"severity": "blocker", "description": "off-by-one",
                         "location": "a.txt:2"}]}
    u = ultra.run_ultra(tmp_path / "run", checkout=r, base=base, head=head,
                        run=_proc(0, json.dumps(payload)))
    assert u.status == ultra.RAN and len(u.bugs) == 1
    assert u.bugs[0].severity == "blocker" and "off-by-one" in u.bugs[0].claim


def test_an_exit_zero_with_unreadable_json_is_not_a_clean_review(tmp_path, global_identity):
    """THE FAIL-OPEN. §13.1's five reasons do not cover this; folding it into 'found no
    bugs' is the false green this whole project keeps finding."""
    r, base, head = _repo(tmp_path)
    u = ultra.run_ultra(tmp_path / "run", checkout=r, base=base, head=head,
                        run=_proc(0, "Review complete. Looks good!"))
    assert u.status == ultra.UNAVAILABLE and u.reason == "unreadable_output"
    assert u.bugs is None


def test_each_named_unavailability_reason_is_recognised():
    cases = {
        "no_auth": (1, "", "You are not logged in to claude.ai. Run /login."),
        "zdr_org": (1, "", "This organization has zero data retention enabled."),
        "usage_credits_off": (1, "", "Extra usage credits are disabled for this account."),
        "diff_too_large": (1, "", "The diff exceeds the 500 file limit."),
    }
    for expected, (rc, out, err) in cases.items():
        assert ultra.classify(rc, out, err) == expected, expected


def test_an_unrecognised_exit_one_is_exit_one_and_never_a_guess():
    assert ultra.classify(1, "", "something nobody has seen before") == "exit_1"


def test_a_zero_exit_classifies_as_nothing():
    assert ultra.classify(0, '{"bugs": []}', "") is None


def test_a_timeout_records_the_session_url_and_says_the_review_is_still_running(
        tmp_path, global_identity):
    r, base, head = _repo(tmp_path)

    def run(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw.get("timeout", 0), output="",
                                        stderr="session: https://claude.ai/review/abc123\n")

    u = ultra.run_ultra(tmp_path / "run", checkout=r, base=base, head=head, run=run)
    assert u.status == ultra.TIMED_OUT
    assert u.session_url == "https://claude.ai/review/abc123"
    assert "still running" in u.detail


def test_no_ultra_skips_without_spending(tmp_path, global_identity):
    r, base, head = _repo(tmp_path)
    run = _proc()
    u = ultra.run_ultra(tmp_path / "run", checkout=r, base=base, head=head, enabled=False,
                        run=run)
    assert u.status == ultra.SKIPPED and run.calls == [] and u.bugs is None


def test_the_bugs_payload_lands_in_the_run_directory(tmp_path, global_identity):
    r, base, head = _repo(tmp_path)
    d = tmp_path / "run"
    ultra.run_ultra(d, checkout=r, base=base, head=head,
                    run=_proc(0, json.dumps({"bugs": []})))
    assert json.loads((d / "ultrareview" / "bugs.json").read_text()) == {"bugs": []}


def test_the_unreviewed_label_is_imported_and_not_respelled():
    src = (ROOT / "shared" / "lib" / "forge" / "ultra.py").read_text()
    assert "independently re-reviewed" not in src
    assert "VERIFIED_NOT_INDEPENDENTLY_REVIEWED" in src


def test_the_module_says_what_it_did_not_measure():
    src = (ROOT / "shared" / "lib" / "forge" / "ultra.py").read_text()
    assert "NOT MEASURED" in src and "no remote" in src
```

- [ ] **Step 2: Run them to verify they fail**

```bash
uvx --with pytest pytest -q tests/test_forge_ultra.py
```

Expected: FAIL — `ModuleNotFoundError: No module named 'forge.ultra'`.

- [ ] **Step 3: Write the module**

Create `shared/lib/forge/ultra.py`:

```python
"""§13.1's ultrareview: a cloud-hosted multi-agent review of the final synthesis checkpoint.

IT ANSWERS A DIFFERENT QUESTION FROM THE COUNCIL. §13's panel checks requirement coverage
against the task; ultrareview hunts BUGS, and every finding it reports has been independently
reproduced in its cloud sandbox before reporting — the same verify-before-report discipline
this design demands of itself.

THE TIMEOUT IS IN MINUTES. Measured 2026-08-03 on this machine:

    claude ultrareview --help
    --timeout <minutes>  Maximum minutes to wait for the review to finish (default: 30)

Every other timeout in forge and the council is SECONDS — `engine.MODE_TIMEOUT["deep"]` is
1200, `verify.Step.timeout` defaults to 600 — so a constant copied from either asks for a
20-hour review. `argv` refuses anything outside 1..TIMEOUT_MINUTES_MAX, which makes the guard
structural rather than a comment somebody has to have read.

NOT MEASURED: whether `claude ultrareview` resolves a base branch in a clone with **no
remote**. `--help` describes it as reviewing "the current branch (or a PR number / base
branch)" and `fleet.clone_seat` removes `origin` (`fleet.py:264-270`). Measuring it spends
money, which this package's suite may not do, so it is stated here rather than assumed. §13.1's
degrade path — exit 1 becomes `unavailable`, the run proceeds to handover — is what stands
under that gap.

UNAVAILABILITY DEGRADES, NEVER FAILS. §13.1 names five reasons; this module declares SIX. The
sixth, `unreadable_output`, covers an exit-0 whose `--json` payload does not parse, which the
five do not — and folding that into "ran, found no bugs" is the false green every other module
in this package is written against. The extension is deliberate and is recorded in this plan.
"""
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import gitcmd, review, storage

TIMEOUT_MINUTES_DEFAULT = 30
# Above two hours a value is far likelier to be a seconds constant than an intention. 1200
# ("deep") and 600 (`verify.Step`'s default) are both refused by this ceiling, which is the
# whole reason it is here rather than being unbounded.
TIMEOUT_MINUTES_MAX = 120

# §13.1's stated remote limits.
DIFF_FILE_LIMIT = 500
DIFF_LINE_LIMIT = 8000

RAN = "ran"
UNAVAILABLE = "unavailable"
TIMED_OUT = "timeout"
SKIPPED = "skipped"
STATUSES = (RAN, UNAVAILABLE, TIMED_OUT, SKIPPED)

REASONS = ("no_auth", "zdr_org", "diff_too_large", "usage_credits_off", "exit_1",
           "unreadable_output")

# Declared phrases, in precedence order. A phrase list is a guess about someone else's
# strings, so an exit 1 matching NONE of them is `exit_1` — named for what was observed
# rather than for the nearest-looking cause.
_PHRASES = (
    ("no_auth", ("not logged in", "please run /login", "claude.ai auth", "unauthorized")),
    ("zdr_org", ("zero data retention", "zdr")),
    ("usage_credits_off", ("usage credits are disabled", "extra usage credits are disabled",
                           "usage credits")),
    ("diff_too_large", ("file limit", "line limit", "diff is too large", "too many files")),
)

_SESSION = re.compile(r"https?://[^\s'\"]*claude\.ai/[^\s'\"]+")

_SEVERITY_MAP = {"blocker": "blocker", "critical": "blocker", "high": "blocker",
                 "important": "important", "medium": "important",
                 "minor": "minor", "low": "minor", "info": "minor"}


class UltraError(RuntimeError):
    """An ultrareview this module will not run as asked."""


@dataclass(frozen=True)
class DiffSize:
    """The change ultrareview is being asked to look at, and what could not be counted."""
    files: int | None
    lines: int | None
    why: str


def measure_diff(checkout, base: str, head: str) -> DiffSize:
    """`git diff --numstat` between the two checkpoints, refusing every count it cannot take.

    NO_DAEMON_CACHE because `diff` LOADS AN INDEX — measured on git 2.53.0, `diff` (including
    `diff <commit> -- <pathspec>`) runs `core.fsmonitor`, and the value is a PROGRAM out of the
    repository's own config. The presets go BEFORE the subcommand; after it git answers
    `error: unknown switch 'c'`, rc 129.

    A `-` CELL IS BINARY AND IT IS NOT ZERO, exactly as in `strategy._numstat`. Summing it as
    zero would report a whole-blob rewrite as under §13.1's 8,000-line limit.
    """
    r = gitcmd.git(checkout, *gitcmd.NO_DAEMON_CACHE, "diff", "--numstat", "-z",
                   f"{base}..{head}", env_extra=gitcmd.READONLY, check=False, binary=True)
    if r.returncode != 0:
        return DiffSize(None, None,
                        f"git diff --numstat -> {r.returncode}: "
                        f"{r.stderr.decode('utf-8', 'replace').strip()}")
    files, lines, unmeasured = 0, 0, []
    for rec in r.stdout.decode("utf-8", "surrogateescape").split("\0"):
        if not rec:
            continue
        added, deleted, path = rec.split("\t", 2)
        files += 1
        if added == "-" or deleted == "-":
            unmeasured.append(path)
            continue
        lines += int(added) + int(deleted)
    if unmeasured:
        return DiffSize(files, None,
                        f"git reports a binary delta for {unmeasured[:5]}, so this diff's "
                        "changed-line count is not a number this run measured")
    return DiffSize(files, lines, "")


def argv(*, timeout_minutes: int, target) -> list:
    """`claude ultrareview`'s argv. THE TIMEOUT IS MINUTES AND THIS IS WHERE THAT IS ENFORCED.

    The range check is the guard, not the docstring: `MODE_TIMEOUT["deep"]` (1200) and
    `verify.Step`'s default (600) are both seconds constants a reader might reach for, and
    both are refused here rather than silently requesting a review measured in days.
    """
    if not isinstance(timeout_minutes, int) or isinstance(timeout_minutes, bool):
        raise UltraError(f"a timeout is a whole number of MINUTES, not {timeout_minutes!r}")
    if not 1 <= timeout_minutes <= TIMEOUT_MINUTES_MAX:
        raise UltraError(
            f"--timeout is in MINUTES (measured: `claude ultrareview --help` -> "
            f"'--timeout <minutes> ... (default: 30)'), and {timeout_minutes} is outside "
            f"1..{TIMEOUT_MINUTES_MAX}. Every other timeout in forge and the council is in "
            "seconds; a value from one of those asks for a review measured in days.")
    out = ["claude", "ultrareview", "--json", "--timeout", str(timeout_minutes)]
    if target:
        out.append(str(target))
    return out


def classify(exit_code, stdout, stderr):
    """§13.1's unavailability reason for this invocation, or `None` when it did not fail."""
    if exit_code == 0:
        return None
    low = f"{stdout or ''}\n{stderr or ''}".lower()
    for reason, phrases in _PHRASES:
        if any(p in low for p in phrases):
            return reason
    return "exit_1"


def session_url(stderr):
    """§13.1: "stderr's session URL recorded". `None` when there is none to record."""
    m = _SESSION.search(stderr or "")
    return m.group(0) if m else None


@dataclass(frozen=True)
class Ultra:
    """What §13.1's pass produced, and how much of it was measured.

    `bugs` is `None` — never `()` — for every status but `ran`. An empty tuple means "the
    review ran and reported nothing", which is precisely the sentence an unavailable review
    must not be able to write.

    `diff_measured` is False when the diff's line count could not be taken, so a record can
    never say the change was under §13.1's limits on a measurement nobody took.
    """
    status: str
    reason: str | None
    bugs: tuple | None
    session_url: str | None
    diff_measured: bool
    detail: str

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise UltraError(f"status is one of {list(STATUSES)}, not {self.status!r}")
        if self.reason is not None and self.reason not in REASONS:
            raise UltraError(f"reason is one of {list(REASONS)} or None, "
                             f"not {self.reason!r}")
        if self.status == RAN and self.bugs is None:
            raise UltraError("a review that ran reports its findings, even when there are "
                             "none; `None` is what an unavailable one carries")
        if self.status != RAN and self.bugs is not None:
            raise UltraError(
                f"a {self.status!r} review has no findings to report, and an empty tuple "
                "here would read as a review that ran and found nothing")
        if self.status == UNAVAILABLE and self.reason is None:
            raise UltraError("an unavailable review names which of §13.1's reasons it was")


def _bugs(payload, checkpoint_round: int) -> tuple:
    """`bugs.json`'s rows as `review.Finding`s, or raise if the payload is not readable."""
    rows = payload.get("bugs")
    if not isinstance(rows, list):
        raise ValueError("the payload carries no `bugs` list")
    out = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("a bug is an object")
        sev = _SEVERITY_MAP.get(str(row.get("severity", "")).lower())
        if sev is None:
            raise ValueError(f"unmapped severity {row.get('severity')!r}")
        claim = " ".join(str(row.get(k)) for k in ("description", "location")
                         if row.get(k)).strip()
        if not claim:
            raise ValueError("a bug carries a description")
        out.append(review.Finding(
            id=review.finding_id(checkpoint_round, "ultrareview", sev, claim),
            round=checkpoint_round, seat="ultrareview", severity=sev, claim=claim,
            resolution="open"))
    return tuple(out)


def run_ultra(run_dir, *, checkout, base: str, head: str, enabled: bool = True,
              timeout_minutes: int = TIMEOUT_MINUTES_DEFAULT, target=None,
              round_: int = 0, file_limit: int = DIFF_FILE_LIMIT,
              line_limit: int = DIFF_LINE_LIMIT, run=subprocess.run) -> Ultra:
    """§13.1's single pass, from the synthesis checkout, after the council loop terminated.

    ORDER: the local pre-flight first, so a diff that is definitely over §13.1's limits costs
    nothing; then one invocation; then the payload. `--no-ultra` (`enabled=False`) is priced
    by `gate.quote(ultrareview=False)` and spends nothing here.

    AN UNMEASURABLE DIFF RUNS. The remote is the authority on its own limits, and refusing
    locally on a line count nobody could take would report `diff_too_large` about a diff whose
    size is unknown — a reason stated more confidently than the evidence. What the record does
    instead is carry `diff_measured=False`.

    §13.1'S FINDINGS GET THE POST-ROUND-2 TREATMENT and this function does not apply it: fix →
    fresh-verifier verify → checkpoint → `review.VERIFIED_NOT_INDEPENDENTLY_REVIEWED`, with
    the terminal decided by `review.terminal_from_record` (a fixed-but-unreviewed blocker is
    `degraded`, an unresolved one is `review_blocked`). No new loop and no new state, exactly
    as §13.1 says — the record and the transition are `review`'s.
    """
    if not enabled:
        return Ultra(SKIPPED, None, None, None, False,
                     "--no-ultra: §13.1 is default on and this run opted out, so no cloud "
                     "review was requested and none is priced")
    d = measure_diff(checkout, base, head)
    if (d.files is not None and d.files > file_limit) or \
            (d.lines is not None and d.lines > line_limit):
        return Ultra(UNAVAILABLE, "diff_too_large", None, None, d.lines is not None,
                     f"the diff is {d.files} file(s) / {d.lines} line(s), over §13.1's "
                     f"{file_limit}/{line_limit}; refused locally so the review is not "
                     "requested and not charged")
    out_dir = Path(run_dir) / "ultrareview"
    out_dir.mkdir(parents=True, exist_ok=True)
    a = argv(timeout_minutes=timeout_minutes, target=target)
    # +60s of grace over the remote's own bound, so a review that finishes at its limit is
    # collected rather than killed one second early by the local wrapper.
    try:
        proc = run(a, cwd=str(checkout), capture_output=True, text=True,
                   timeout=timeout_minutes * 60 + 60)
    except subprocess.TimeoutExpired as e:
        url = session_url(e.stderr if isinstance(e.stderr, str) else "")
        return Ultra(TIMED_OUT, None, None, url, d.lines is not None,
                     f"the local wait of {timeout_minutes} minute(s) elapsed; the remote "
                     "review is still running and can be collected in the browser"
                     + (f" at {url}" if url else
                        " — no session URL was printed, so there is nothing to collect from"))
    url = session_url(proc.stderr)
    reason = classify(proc.returncode, proc.stdout, proc.stderr)
    if reason is not None:
        return Ultra(UNAVAILABLE, reason, None, url, d.lines is not None,
                     f"ultrareview: unavailable ({reason}) — the run proceeds to handover")
    try:
        payload = json.loads(proc.stdout)
        bugs = _bugs(payload, round_)
    except (ValueError, TypeError) as e:
        return Ultra(UNAVAILABLE, "unreadable_output", None, url, d.lines is not None,
                     f"ultrareview exited 0 and its --json payload could not be read ({e}); "
                     "that is not the same as a review that found nothing, and §13.1's five "
                     "named reasons do not cover it")
    storage.atomic_write(out_dir / "bugs.json",
                         (json.dumps(payload, sort_keys=True, indent=2) + "\n")
                         .encode("utf-8"))
    return Ultra(RAN, None, bugs, url, d.lines is not None,
                 f"{len(bugs)} finding(s) over a {d.files}-file diff"
                 + ("" if d.lines is not None else
                    f"; its changed-line count was not measured ({d.why})"))
```

- [ ] **Step 4: Run the tests**

```bash
uvx --with pytest pytest -q tests/test_forge_ultra.py
```

Expected: `15 passed`.

- [ ] **Step 5: Re-run under renamed test functions**

Rename every test to `test_zz0`…`test_zzN` and re-run. Expected: `15 passed`. **Restore the original names.**

- [ ] **Step 6: Mutate every new branch**

```bash
scripts/mutate.py --file shared/lib/forge/ultra.py \
  --old '    if not 1 <= timeout_minutes <= TIMEOUT_MINUTES_MAX:' \
  --new '    if False:' \
  -- uvx --with pytest pytest -q tests/test_forge_ultra.py

scripts/mutate.py --file shared/lib/forge/ultra.py \
  --old '        if added == "-" or deleted == "-":' \
  --new '        if False:' \
  -- uvx --with pytest pytest -q tests/test_forge_ultra.py

scripts/mutate.py --file shared/lib/forge/ultra.py \
  --old '        return Ultra(UNAVAILABLE, "unreadable_output", None, url, d.lines is not None,' \
  --new '        return Ultra(RAN, None, (), url, d.lines is not None,' \
  -- uvx --with pytest pytest -q tests/test_forge_ultra.py

scripts/mutate.py --file shared/lib/forge/ultra.py \
  --old '    return "exit_1"' \
  --new '    return "no_auth"' \
  -- uvx --with pytest pytest -q tests/test_forge_ultra.py

scripts/mutate.py --file shared/lib/forge/ultra.py \
  --old '        if self.status != RAN and self.bugs is not None:' \
  --new '        if False:' \
  -- uvx --with pytest pytest -q tests/test_forge_ultra.py

scripts/mutate.py --file shared/lib/forge/ultra.py \
  --old '    if not enabled:' \
  --new '    if False:' \
  -- uvx --with pytest pytest -q tests/test_forge_ultra.py
```

Expected: every one exits 0 (CAUGHT). Run `git status` after the wave; it must be clean.

- [ ] **Step 7: Add the test file to the Makefile, then run the whole forge suite**

Extend `FORGE_TESTS` with `tests/test_forge_ultra.py`.

```bash
uvx --with pytest pytest -q tests/test_forge_*.py tests/test_council_*.py
```

Expected: 1363 + the new tests, all passing, exit 0.

- [ ] **Step 8: Render, gate and commit**

```bash
make render
git add shared/lib/forge/ultra.py tests/test_forge_ultra.py Makefile marketplaces
make verify; echo "verify rc=$?"
make precommit; echo "precommit rc=$?"
```

Expected: both `rc=0`. Then:

```bash
git commit -m "$(cat <<'EOF'
feat(forge): §13.1's timeout is minutes, and an exit-0 nobody could read is not a clean review

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UiV66Pt8cZVMq9t8WEAhpN
EOF
)"
```

---

## What this plan produces and nothing yet calls

Stated rather than claimed as covered, because Plan H's first durable lesson is that *an approved rule with no caller is an untested rule, however well argued*.

- **`runner.run` still stops at `comparing`.** Nothing in this plan is reachable from it. The synthesis executor, the checkpoint writer, `--collect` and the CLI front end are Plan J's, and Plan J is what supplies `review.loop`'s `fix` callable, the real `progress.Progress` for each fix, and the `--no-ultra` flag that reaches `ultra.run_ultra(enabled=False)`.
- **`review.run_round` has never invoked a real provider.** `run_council`, `build_real_spec` and `fingerprint.build` are injected and every test passes a fake, per Global Constraints. The first real panel is Plan J's, and until then **nothing here proves the real reviewer path works** — the signatures are kept identical to the engine's so the shape is obviously the same.
- **`ultra.run_ultra` has never invoked `claude ultrareview`.** `run` is injected. Whether the CLI resolves a base branch in a clone with no remote is **NOT MEASURED** (§M4), stated in the module docstring, and covered by the degrade path only.
- **`strategy.recorded_seam_analysis` has no caller.** Above the size threshold this plan produces `unresolved` and offers the door; walking through it is a decision Plan J's operator surface takes.
- **`rubric.strongest` has no caller.** §12.4's fallback trigger and §12.5's rubric are consumed by the fallback executor, which is Plan J.
- **`gate.Quote.review_rounds` reaches the manifest and is read by `review.loop` only.** No CLI passes a non-default value yet.

---

## Self-review

**1. Spec coverage.**

| Spec | Task | Note |
| --- | --- | --- |
| §12.1 size gate | 1 | Thresholds exclusive at the boundary; above it no strategy is produced. |
| §12.1 "recorded `manual_trace_confirmed`, never a checked predicate" | 1 | `Decision.__post_init__` refuses `mechanically_checked` for anything but `from_scratch`. |
| §12.2 seam claims are mandatory when partitioning | **NOT COVERED** | `ledger.KINDS` already carries `"seam"` (Plan I). Freezing seam rows before component synthesis belongs to the synthesis executor, which is Plan J. Flagged, not silently dropped. |
| §12.3 three-way failure classification | 1 | Plus `fallback_disposition`'s three values. |
| §12.3 progress tuple, strict ordering, three outcomes | 2 | |
| §12.3 oscillation over `(tree_oid, fingerprints)` from `events.jsonl` | 2 | |
| §12.3 hard cap on synthesis-fix attempts | 2 | New `Quote`/`Confirmation`/`Manifest` field, derived from what was priced. |
| §12.4 coverage as fallback trigger | 3 | Three-valued; `unresolved` is `undecidable`. |
| §12.5 rubric, deterministic tie-break | 3 | Total by construction; `strongest` refuses while any seat is unmeasured. |
| §13 reviewer input set | 4 | §16's artifact manifest is `None` and **stated to the reviewer**. |
| §13 in-process `run_council`, all three hazards | 4 | `workdir`, `install_signal_handler=False`, the sentinel. |
| §13 codex through `codex exec --json` | 4 | Recorded deviation; `build_real_spec` already produces it. |
| §13 the ledger path is not passed | 4 | Structural: path containment **and** a byte scan, with `os.walk(onerror=)`. |
| §13 content-addressed `review_findings`, on receipt | 4 | Write-once; `finding_id` is content-derived. |
| §13 bounded round-1/round-2 loop | 5 | Never a third round; the cap bounds fixes independently. |
| §13 `ready`/`review_blocked` transition reads the record | 5 | Plus `degraded`; a missing record or an orphan **raises**. |
| §13.1 ultrareview, minutes, five (now six) reasons, `--no-ultra` | 6 | |
| §14.1 write-ahead journalling | 2, 4, 5 | `synthesis_fix_*` and `council_round_*` pairs. |

**2. Placeholder scan.** No "TBD", no "similar to Task N", no "add appropriate error handling". Every code step carries the full body. Three places name something deliberately absent and each says so explicitly and why: §12.2's seam-claim freezing (Plan J), the `NOT MEASURED` ultrareview-in-a-clone-with-no-remote line, and `review.loop`'s `Progress(None, None)` — which is flagged in prose as the value Plan J must replace with a real measurement and **must not** be replaced with `frozenset()`.

**3. Type consistency.** `Decision.method` draws from `coverage.METHODS` (the same tuple `coverage.Result` validates against). `Size` is produced by Task 1 and consumed by Task 3's `dimensions_from`. `Progress` is produced by Task 2 and consumed by Task 2's `record_fix_done` and Task 5's `loop`. `Round`/`Finding`/`Resolution` are produced by Task 4/5 and consumed by Task 5's `terminal_from_record` and Task 6's `_bugs` (via `review.finding_id` and `review.Finding`, same severities). `review.READY`/`DEGRADED`/`REVIEW_BLOCKED` are the exact strings in `runstate._EDGES` (`runstate.py:1031-1033`), which is what makes `runstate.advance` accept them. `GATE_RANK`'s keys are asserted equal to `verify.OUTCOMES`. The three-valued comparisons use three different constant sets on purpose — `progress.COMPARISONS`, `strategy.DISPOSITIONS`, `rubric.TRIGGERS`, `ultra.STATUSES` — because folding them into one vocabulary would let a caller pass one module's "undecidable" where another's is read.




