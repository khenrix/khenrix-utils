# llm-forge Plan J — handover, the CLI, and the skill

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give llm-forge a front end, a deliverable and a shipped skill — §16/§16.1 handover and
the provenance header, §15's `--gc` and disk accounting, the `--start`/`--collect`/`--gc`/
`--no-ultra` CLI, `shared/skills/llm-forge/SKILL.md`, §18's evals, and §19's council-engine
items — and close the inherited debts that only a production caller can close.

**Architecture:** Everything below either *produces a caller* for machinery eleven plans built
and never invoked, or *produces a record* the operator reads. Two new forge modules
(`handover.py`, `gc.py`) and one new entry point (`cli.py`) sit on top of the existing ~24;
nothing existing is restructured. Three changes land outside `shared/lib/forge/` and each one
is its own commit for its own gate reason: `scripts/lib/reconcile.py` (stales all eleven eval
receipts), `shared/lib/council/engine.py` (stales `llm-council`'s receipt, whose gate is
`fanout.py --self-test` plus a live paid smoke, not the judge harness), and
`scripts/eval_trigger.py` (measured: in no skill's source closure, so it stales nothing).

**Tech Stack:** Python 3.11+ stdlib only. `argparse`, `json`, `subprocess` (argv lists),
`pathlib`, `hashlib`, `dataclasses`. Tests: `uvx --with pytest pytest -q tests/`. Mutation
testing: `scripts/mutate.py`. Gates: `make render`, `make verify`, `make precommit`,
`make eval SKILL=llm-forge`.

---

## Revisions

Revision 1 (2026-08-03), after an adversarial review. Every change below is to the **draft
code**, not to the prose beside it: a corrected sentence next to uncorrected code is worse than
neither. The git measurements (M1–M8) all reproduced under review and are unchanged except for
one count.

| # | What was wrong | What it is now |
|---|---|---|
| 1 | Task 11's eval set was six inline Q&A whose assertions Step 6 derived **from the SKILL.md the same task writes** — a closed loop graded on project vocabulary a baseline cannot know. The fixture repository was referenced by none of them. | Two evals now grade **against the fixture**, on engine facts a baseline gets wrong (what enters B1, what happens to an ignored directory, what a handover header entitles you to claim). Assertions are written from the **engine source**, not from SKILL.md. Step 6 says which file each fact comes from. |
| 2 | `cli.start` gave no injection point for the launcher, and two tests called `_drive_a_start(tmp_path)` with no `monkeypatch` — **real provider calls** and writes to the real `~/.local/state`. | `start` and `main` take `make_launcher=None`, resolved at call time. `_drive_a_start` requires `monkeypatch` and every test that calls it requests the fixture. |
| 3 | `--collect` took `synthesis_outcome`/`seconds`/`strategy` **from argv** and printed them under `_VERIFIED_MEANS`. Nothing ran the verify command. | `Provenance` carries `synthesis_measured`. An operator-asserted verdict renders as *reported by the orchestrator, not measured here*, and `_VERIFIED_MEANS` is **not printed beside it**. Measuring the synthesis in-engine is named as its own task, with its cost. |
| 4 | `create_synthesis_worktree`'s docstring contained the literal `--detach` while its own test asserted the module does not, and claimed "the flag is absent from this file". | The docstring names the flag in words. The test is unchanged and now passes. |
| 5 | `mergeability` null-checked `synthesis_tree_oid` and then never read it, so an **empty synthesis worktree returned `MERGE_READY`**; the docstring described a `!=` that did not exist. | The synthesis tree is compared against B1's tracked tree and an unfused worktree **raises**. The docstring describes the comparison the code performs. A test and a mutation cover it. |
| 6 | `test_collect_refuses_a_run_directory_it_cannot_read_whole` could not pass: `reconstruct` raises four classes, none in `cli.main`'s except tuple. | All four are in the tuple, with the rationale that keeps the narrow-except argument intact. |
| 7 | §20's ambient-skill bar was `if named and bundle is None`, and `cli.start` always passes a bundle — so the three-closure check never ran, and `taskbundle.ambient_note` had no caller. | The bar no longer looks at the bundle: a bundle carries files, it does not make `/markitdown` identical across three CLIs. `preflight.ambient_notes` gives `ambient_note` its caller and `cli.start` adds the notes to the prompt. |
| 8 | The header test asserted `"2 artifact sets usable"` against a renderer emitting `artifact set(s)`. | Test matches the renderer. |
| 9 | `"built"` appeared **four** times in the drafted `handover.py` — including inside the docstring sentence asserting the word appears nowhere — against a source-level assertion. (The review found two; measured, it is four.) | All four rewritten. |
| 10 | The receipt would have recorded `deterministic_gate="wikisync-unittests"` for llm-forge: `eval_harness.py:452` hardcodes that literal for **every** `DETERMINISTIC_GATED` skill. | A `DETERMINISTIC_GATE_NAMES` table; the receipt records the gate that actually ran. |
| 11 | The weight split is correct (22 + 9 = 31 files, no gaps) but left `baseline`, `fleet`, `harvest`, `bundle`, `verify`, `runner` and `review` in **no** commit-boundary gate. | `precommit` depends on `forge-test-slow`. `verify` stays free of it for the forge-inside-forge reason the plan already argues. |
| 12 | `gc._run_dirs` re-derived `storage.run_root`'s formula by hand (measured byte-identical), which the plan condemns three times elsewhere. | `storage.run_dirs` is the one home; a seam test asserts `gc.py` spells neither `sha256` nor `XDG_STATE_HOME`. |
| 13 | An **unregistered** synthesis worktree fell through to `shutil.rmtree`, leaving `.git/worktrees/<name>` registered and unreclaimable, since §9 forbids the prune. | An unknown-but-present synthesis path refuses. |
| 14 | Task 8 required `PYTHONPYCACHEPREFIX` set to "a per-round directory under the run directory", which the produced signature had no way to name — **and the premise was stale**: `review.py:1177` already does `env.pop("PYTHONPYCACHEPREFIX", None)`, with a docstring arguing why dropping beats pinning. | Task 8 keeps the drop and says so. The drafted assertion, which forbade the drop, is corrected; the planted-`.pyc` behavioural test stands. |
| 15 | The Interfaces block and the draft disagreed on three `cli` signatures. | Interfaces match the draft. |
| 16 | Task 4 Step 3 added `worktree`/`fetch` to `_DIFF_DRIVER_SAFE` before Step 4 created the calls; `test_every_verb_the_allow_lists_clear_is_one_this_package_calls` asserts `allowed <= verbs` and fails in the window. | Step 3 states the two edits are one change and must land together. |
| 17 | Minor: `(closures.get(c) or 'not installed')[:12]` prints `not installe`; `usage()` discarded `why_h` on the `OSError` path and reported an unreadable handover record as "NOT handed over"; the façade's second path candidate is dead and its comment mislabels it; Task 1 Step 5's grep does not glob `scripts/lib/*.py`. | All four fixed. |
| 18 | The sweep headline said **9** modules. | Measured: 17 line-matches with `(?!-)` across 9 modules, **plus** `harvest.py`'s flattened-only hit, which is in no line-match — 18 citations across **10** modules. The Files list already named 10. |
| 19 | The File Structure table claimed `runstate.py` gains `handover_target` on `State`. No task implements it, and none needs to. | Row removed: the record lives in `handover.json`, which is what makes §15's "unmerged" well-defined. `storage.py`, `gate.py` and `taskbundle.py` — all modified by tasks — were missing from the table and are added. |

**Not changed, because the review confirmed them:** every git measurement (`worktree add` fires
FSMONITOR ×1, post-checkout ×1, post-index-change ×1, reference-transaction ×14, and the two
presets reduce all of it to `fired: []`; `--no-ext-diff` is rc=129 in every argv position;
`fetch` fires FSMONITOR ×1 and reference-transaction ×2; **neither verb runs a diff driver** —
the control `git diff` fired EXTDIFF; `worktree unlock` on an unlocked tree is rc=128;
`worktree remove` leaves the branch), the eleven-receipt count, and the two fail-open shapes.

---

## THIS PLAN DOES NOT FIT IN SIX TASKS — the honest split

Eleven tasks, in four commit groups. The brief asked me to say so at the top if six would not
hold it, and six will not. Here is why the boundaries fall where they do rather than being
compressed:

| Group | Tasks | Why it cannot merge into its neighbour |
|---|---|---|
| **A — foreign gates** | 1, 2 | Task 1 edits `scripts/lib/reconcile.py`, which `checks.LIB_SCRIPTS` puts in **every** skill's source closure — measured: eleven receipts go stale at once, and re-blessing them is a documented ritual with its own rationale. Nothing else may ride that commit. Task 2 is separable: measured, `scripts/eval_trigger.py` is in **no** skill's closure. |
| **B — the deliverable** | 3, 4, 5 | §20's bundle wiring (3) must land before §16 (4) because the synthesis worktree is the first tree whose git dir is a *file*, which is what `taskbundle.task_dir` exists to survive. §16.1's header (5) is rejectable on its own: a reviewer can accept the worktree and reject the sentence it prints. |
| **C — the front end** | 6, 7, 8 | The CLI (6) is `make_launcher`/`forge_spec`/`run_ultra`'s first production caller — the thing three plans deferred. `--gc` (7) is a subcommand of the CLI from 6 and needs `handover_target` from 4. `reviewer_env` (8) is a security change to `review.py` a reviewer must be able to reject without losing the CLI. |
| **D — shipping** | 9, 10, 11 | §19 (9) touches `shared/lib/council/engine.py` and therefore needs its **own commit and its own gate** (`--self-test` + `make smoke-llm-council`, ~$0.22). The prose sweep (10) rewrites eighteen sites across ten modules and adds the packaging test that keeps them closed — a whole-package diff nobody should read interleaved with logic. Task 11 is the hard gate the plan ends on. |

Tasks 1, 2, 9 and 10 are independent of the rest and of each other. Tasks 3 → 4 → 5 → 6 → 7 are
a chain. Task 8 depends on nothing after Task 3. Task 11 depends on everything.

---

## Global Constraints

Every task's requirements implicitly include this section. Copied verbatim from the brief.

- Python **stdlib only**. No pip dependencies.
- **Argv lists, never a shell.** No `shell=True`, no string commands.
- **Git only via `gitcmd`**, located by asking git, `-c` presets **before** the subcommand.
- **Fail closed.**
- **A verdict must never read cleaner than its evidence.**
- **A comment asserting something the code does not do is a defect.**
- **No test may invoke a real provider or spend money.**
- **Shipped forge prose may not cite plan documents.**
- `shared/lib/forge/**` is source of truth; `marketplaces/**` is render output.
- Every task ends with `make render`, explicit-pathspec `git add` **including `marketplaces`**,
  then `make verify` and `make precommit` **unpiped with `$?` captured**.
- `scripts/mutate.py` refuses any test-command status other than 1 and does not decode `\n` in
  `--old`/`--new`.
- Tests run `uvx --with pytest pytest -q tests/` — **bare `pytest -q` cannot collect**, because
  leaked agy worktrees under `evals/*/workspace/` collide on module names.

**Two additional constraints this plan's own history imposes:**

- **Experiments run in a temp clone or throwaway worktree, never in this checkout.** An agent
  that finds unexpected state in the checkout **reports it and stops**; it does not repair it
  silently.
- **Check `git status` before and after any mutation wave.** A killed mutation run leaves the
  tree mutated and the next suite is green for the wrong reason.

### The two fail-open shapes to ask of every collection and comparison in this plan

Both recurred three or more times in Plan I₂. Ask both of **every** aggregate, every roll-up,
every equality test you write below:

1. **Does nothing leave the same record as nobody?** An empty result, a zero count and an
   absent measurement must be three distinguishable records, not one.
2. **Do two different failures compare equal?** Two absences, two sentinels, two `None`s and
   two empty tuples must not collapse into one value that a later comparison calls agreement.

---

## File Structure

**New files:**

| Path | Responsibility |
|---|---|
| `shared/lib/forge/handover.py` | §16: synthesis worktree, seat-branch transport, mergeability decision, out-of-band enumeration, B₁ file list, `handover_target` record. §16.1: the provenance header renderer. |
| `shared/lib/forge/gc.py` | §15: the `--gc <run-id>` walk, automatic removal of known-failed temporary clones, the refusal to delete a not-handed-over synthesis worktree/branch, and the total-disk-held report. |
| `shared/lib/forge/cli.py` | The `--start` / `--collect` / `--gc` / `--no-ultra` entry point. The production caller of `launch.make_launcher`, `seat.forge_spec` and `ultra.run_ultra`. |
| `shared/skills/llm-forge/SKILL.md` | The shipped skill body. Rendered into all three plugins by `render.py`'s `shared/skills/` glob. |
| `shared/skills/llm-forge/scripts/forge.py` | Thin executable façade that adds the plugin's `lib/` to `sys.path` and calls `forge.cli.main`. |
| `evals/llm-forge/evals.json` | §18's eval set. |
| `evals/llm-forge/fixtures/` | The fixture repository the evals grade against. |
| `tests/test_forge_handover.py` | Task 4/5 suite. |
| `tests/test_forge_gc.py` | Task 7 suite. |
| `tests/test_forge_cli.py` | Task 6 suite. |

**Modified files:**

| Path | Change |
|---|---|
| `scripts/lib/reconcile.py` | `read_json_object` distinguishes unparseable from absent. |
| `scripts/eval_trigger.py` | A dead judge is `unreadable`, not a correct abstain. |
| `shared/lib/forge/preflight.py` | §20's `task_refusals`. |
| `shared/lib/forge/taskbundle.py` | `read_task_bundle_if_recorded`. |
| `shared/lib/forge/runner.py` | Materialize the run's task bundle into every seat. |
| `shared/lib/forge/review.py` | `reviewer_env` neutralizes `PATH`, `PYTHONPATH`, `PYTHONHOME`, `NODE_OPTIONS` and applies the `HOSTILE_ENV` scrub. |
| `shared/lib/forge/storage.py` | `task_source_path`, `handover_path`, `run_dirs`. |
| `shared/lib/forge/gate.py` | `Confirmation.ultrareview`; `GC_UNBUILT` leaves `ACCEPTABLE_GAPS`. |
| `shared/lib/council/engine.py` | §19: `MODE_TIMEOUT["forge"]`, agy timeout-wording mapping. |
| `tests/test_forge_seams.py` | `worktree` and `fetch` onto `_DIFF_DRIVER_SAFE`, with the measurements. |
| `tests/test_forge_packaging.py` | The plan-citation pattern, after the sweep clears it. |
| `scripts/eval_harness.py` | `DETERMINISTIC_GATED["llm-forge"]` using `sys.executable`, and a gate NAME the receipt can record truthfully. |
| `Makefile` | `FORGE_TESTS` split by weight; `forge-test-slow` target; `precommit` depends on it. |
| `capabilities.toml` | `[[skills]]` discoverability entry for `llm-forge`. |
| `ten forge modules` | The plan-document prose sweep (Task 10). |

There is deliberately **no** `runstate.State.handover_target`. §15 asks that the target (or an
explicit acceptance) be recorded so "unmerged" is well-defined; `handover.json` is where this
plan records it, and a second copy on `State` would be two spellings of one fact.

---

## Measurements taken while authoring this plan

Taken in a throwaway directory under the scratchpad, never in this checkout. **Do not
re-derive these; they are the inputs to Tasks 4, 7 and 10.** git 2.53.0.

**M1 — `git worktree add -b` against a repo with all hooks planted and `core.fsmonitor` armed:**
fires `FSMONITOR`, `post-checkout`, `post-index-change` and `reference-transaction`.
`-c core.fsmonitor=false -c core.untrackedCache=false -c core.hooksPath=/dev/null` silences
**all four** (`fired: []`). Counted without deduplication on a single-commit repository:
`FSMONITOR` ×1, `post-checkout` ×1, `post-index-change` ×1, `reference-transaction` ×**14**.
§16's implementation note says the monitor fires **twice**; here it fired once. The note's
*conclusion* — that the two presets are necessary and sufficient for all four programs — is
confirmed exactly. See "What this plan could NOT specify" for how to read the discrepancy.

**M2 — `--no-ext-diff` and `worktree`:** rc=**129** in all three argv positions (before `add`,
after `add`, and top-level before `worktree`). `NO_DIFF_DRIVERS` cannot be passed.

**M3 — `git fetch <path> +refs/heads/<b>:refs/khenrix-forge/<r>/<s>`:** fires `FSMONITOR` and
`reference-transaction`. The same two `-c` preset pairs silence both. `--no-ext-diff` is rc=129
after `fetch` and top-level.

**M4 — do `worktree` and `fetch` run a diff driver?** **No.** With `diff.external`,
`diff.mydrv.command` and `diff.mydrv.textconv` all planted and `.gitattributes` selecting the
driver, `worktree add` and `fetch` fired **none** of them; the control `git diff HEAD~1` fired
`EXTDIFF`. So both verbs are **measured onto `_DIFF_DRIVER_SAFE`**, which is the route
`tests/test_forge_seams.py` itself prescribes ("cleared by measuring it onto this list **or** by
an exemption naming what it actually runs") — an exemption would be the weaker of the two.

**M5 — `git worktree remove`** fires `FSMONITOR` and `post-index-change`; the presets silence
both. **`git worktree unlock` on an unlocked worktree is rc=128.** An unconditional
unlock-then-remove therefore fails; the unlock is conditional on `worktree list --porcelain`
reporting `locked`. **`worktree remove` does not delete the branch** — measured: the branch
survived every removal, which is what makes §15's "refuses to delete a synthesis
worktree/**branch** not marked handed over" two separate refusals rather than one.

**M6 — `git worktree list --porcelain`** fires nothing and loads no index. Its output is
`worktree <path>` / `HEAD <oid>` / `branch <ref>` records separated by blank lines, with
`locked` / `prunable` as bare or valued lines.

**M7 — the package-wide plan-citation sweep is smaller than the brief states.** Running
`.superpowers/sdd/i2-branch-fix-report.md`'s regex over `shared/lib/forge/*.py` today gives
**18 line-matches across 9 modules** — not "~27 across 12". The report's own itemisation sums
to 19 + 2 already-fixed = 21, so its headline 27 disagreed with its own list. **And one of the
18 is a false positive:** `taskbundle.py:48` reads "Both the **plan-mode** and JSON forms" —
agy's mode, not a plan document; the regex's `(?:the) plan\b` matches because `-` is a word
boundary. Adding `(?!-)` suppresses **exactly** that one and damages nothing else, leaving
**17 line-matches across 9 modules**.

`harvest.py` contributes one hit that only a **flattened** scan finds — its referent is wrapped
across two comment lines, so it appears in no line-match and `harvest.py` is in neither of the
counts above. **Real count: 17 line citations + 1 flattened-only = 18 citations across 10
modules** (the nine plus `harvest.py`). The exact list is in Task 10, and it names all ten.

**M8 — eleven eval receipts, not 33.** `checks._evald_skills(ROOT)` returns 11 skills and
`evals/*/receipt.json` is 11 files. `scripts/eval_trigger.py` is in **no** skill's
`_skill_source_files` closure (verified programmatically); `scripts/lib/reconcile.py` is in
**every** one via `checks.LIB_SCRIPTS`.

---

## Task 1: `read_json_object` — an unparseable file is not an absent one

`scripts/lib/reconcile.py:114-121` returns `{}` for a settings file that **exists and does not
parse**, identically to one that is **absent**. Its callers (`claude_settings` at :644,
`agy_settings` at :537/:549, `claude_mcp_load` at :169) then read every desired key as `ADD`,
and the apply path writes a fresh object through `write_json_object`, which does
`path.write_text(...)` over the whole file. That is real user-config data loss in the engine
behind `khenrix-setup`.

**This is defect shape (1) exactly: nothing leaves the same record as nobody.**

**Files:**
- Modify: `scripts/lib/reconcile.py:114-121`
- Test: `scripts/lib/reconcile_test.py` (already run by `make eval-test`, `Makefile:163`)

**Interfaces:**
- Consumes: nothing from this plan.
- Produces: `reconcile.ReconcileReadError(RuntimeError)`; `read_json_object(path: Path) -> dict`
  now **raises** `ReconcileReadError` for a file that exists, is non-empty and does not parse as
  a JSON object. Absent and zero-length still return `{}`. No other module in this plan calls it.

**The specific fail-open this must not have:** a `except json.JSONDecodeError: return {}` that
has merely moved — e.g. catching the raise at the call site and continuing with `{}`. The
refusal has to reach the operator, or it is the same silence one frame up.

**What input would make this produce a result cleaner than its evidence:** a settings file
containing a bare JSON **array** or **string** (`[1,2]`, `"x"`). It parses, so the
`JSONDecodeError` branch never fires, and the existing `return data if isinstance(data, dict)
else {}` turns it into "absent" with no error at all. That input is in the test set below.

- [ ] **Step 1: Write the failing tests**

Append to `scripts/lib/reconcile_test.py` (inside its existing test-collection style — read the
file first and match how it registers checks; if it uses a plain `def main()` with `ok.append`
tuples, use that form):

```python
def _test_unparseable_settings_is_not_read_as_absent(tmp: Path) -> list:
    """A file that exists and does not parse must REFUSE, not read as {}.

    {} is what an ABSENT file returns, and every caller turns "absent" into a full ADD list
    that write_json_object then writes over the whole file. The three inputs below are the
    three ways a real settings file stops being a JSON object; only the first is a decode
    error, and the other two used to reach the same `{}` by a different line.
    """
    ok = []
    for name, body in (("truncated", '{"a": 1'),
                       ("array", "[1, 2]"),
                       ("string", '"hello"')):
        p = tmp / f"{name}.json"
        p.write_text(body)
        try:
            reconcile.read_json_object(p)
        except reconcile.ReconcileReadError as e:
            ok.append((f"{name} refuses", str(p) in str(e)))
        else:
            ok.append((f"{name} refuses", False))
    # The two states that are genuinely "nothing here" still answer {}.
    absent = tmp / "nope.json"
    ok.append(("absent is {}", reconcile.read_json_object(absent) == {}))
    empty = tmp / "empty.json"
    empty.write_text("")
    ok.append(("zero-length is {}", reconcile.read_json_object(empty) == {}))
    # And a real object still round-trips.
    good = tmp / "good.json"
    good.write_text('{"mcpServers": {}}')
    ok.append(("object reads", reconcile.read_json_object(good) == {"mcpServers": {}}))
    return ok
```

- [ ] **Step 2: Run it to verify it fails**

```
cd /home/khenrix/git/khenrix-utils
python3 scripts/lib/reconcile_test.py
```

Expected: FAIL — `truncated refuses`, `array refuses` and `string refuses` are all `False`,
because `read_json_object` currently returns `{}` for each.

- [ ] **Step 3: Implement the refusal**

Replace `scripts/lib/reconcile.py:114-121` with:

```python
class ReconcileReadError(RuntimeError):
    """A config file this engine will not treat as data it may replace."""


def read_json_object(path: Path) -> dict:
    """The JSON object at `path`, or `{}` when there is genuinely nothing there.

    ABSENT AND UNPARSEABLE ARE DIFFERENT ANSWERS AND THIS IS WHERE THEY DIVERGE. Every
    caller reads `{}` as "this CLI has none of the desired keys yet" and builds a full ADD
    list from it; the apply path then hands that list to `write_json_object`, which does an
    unconditional `write_text` over the whole file. So a settings file that exists and does
    not parse — a half-written save, a merge conflict marker, a hand edit — used to be
    silently replaced with a file containing only what khenrix wanted, and the user's
    machine-specific configuration was gone. Reconcile is non-destructive by design; the
    only way to keep that promise here is to refuse a file this engine cannot read.

    ZERO-LENGTH IS DELIBERATELY STILL `{}`. `claude mcp add` and the CLIs' own first runs
    create the file before writing to it, so an empty file is an ordinary intermediate state
    and refusing it would refuse a fresh machine. A file with bytes in it that are not a JSON
    object is not that state.

    A NON-OBJECT REFUSES THROUGH THE SAME DOOR, and it is the input that made the old
    `isinstance` line load-bearing in the wrong direction: `[1, 2]` and `"hello"` PARSE, so
    the decode-error branch never saw them, and the old `return ... else {}` turned a file
    with contents into the same `{}` an absent one returns. One refusal, both routes.
    """
    if not path.exists() or path.stat().st_size == 0:
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise ReconcileReadError(
            f"{path} exists but is not readable as JSON ({e}). Reconcile will not treat an "
            "unreadable config as an empty one: the caller would report every desired key as "
            "missing and the apply path would write over the whole file. Fix or move the file, "
            "then re-run.") from e
    if not isinstance(data, dict):
        raise ReconcileReadError(
            f"{path} holds a JSON {type(data).__name__}, not an object. The same refusal as an "
            "unparseable file and for the same reason — an empty object here is what an ABSENT "
            "file returns, and the caller cannot tell the two apart.")
    return data
```

- [ ] **Step 4: Run the tests to verify they pass**

```
cd /home/khenrix/git/khenrix-utils
python3 scripts/lib/reconcile_test.py
```

Expected: PASS, all six checks.

- [ ] **Step 5: Prove the refusal reaches the operator rather than being caught one frame up**

```
cd /home/khenrix/git/khenrix-utils
grep -rn "read_json_object" scripts/ shared/skills/
```

(`-r` over the two trees, not a glob list: the drafted glob `scripts/lib/reconcile.py
scripts/*.py shared/skills/*/scripts/*.py` **does not match `scripts/lib/*.py`**, so it could
not have proved the claim it is here to prove. Measured independently: the only call sites are
`reconcile.py:169`, `:537`, `:549`, `:644` — the count below is right, the drafted command was
not.)

Expected: exactly the four call sites at `reconcile.py:169`, `:537`, `:549`, `:644` and the
definition. **If any call site is inside a `try:` that swallows `RuntimeError` or
`Exception`, the fix is defeated and the swallow must be narrowed in this same commit.**
Verify by reading each of the four; record in the commit message that you did.

- [ ] **Step 6: Mutation-test the new branch**

```
cd /home/khenrix/git/khenrix-utils
git status --short
python3 scripts/mutate.py --file scripts/lib/reconcile.py \
  --old 'if not isinstance(data, dict):' --new 'if False:' \
  --test 'python3 scripts/lib/reconcile_test.py'
python3 scripts/mutate.py --file scripts/lib/reconcile.py \
  --old 'if not path.exists() or path.stat().st_size == 0:' --new 'if not path.exists():' \
  --test 'python3 scripts/lib/reconcile_test.py'
git status --short
```

Expected: both `CAUGHT`. `git status --short` identical before and after (a killed mutation run
leaves the tree mutated). If `mutate.py` reports anything other than a clean CAUGHT/SURVIVED —
it refuses a test-command status other than 1 — treat the whole wave's verdicts as void and
re-run.

- [ ] **Step 7: Render, verify, and re-bless the eleven receipts**

```
cd /home/khenrix/git/khenrix-utils
make render
make verify
echo "verify rc=$?"
```

Expected: `verify rc=0`, with eleven **advisory** receipt warnings (`(advisory) receipt: <skill>
changed since last eval`), because `scripts/lib/reconcile.py` is in `checks.LIB_SCRIPTS` and
therefore in every skill's source closure.

```
cd /home/khenrix/git/khenrix-utils
python3 scripts/eval_harness.py --seed-receipt
```

Expected: `seeded receipt: <skill>` eleven times.

**The rationale that MUST go in the commit message**, or a reviewer should reject eleven
reseeded receipts: this change alters `read_json_object`'s behaviour for exactly one input
class — a config file that exists and does not parse — which **no skill's eval set exercises
and no rendered SKILL.md body mentions**. The eleven receipts are stale because
`reconcile.py` is bundled into every skill's `scripts/`, not because eleven skills changed
behaviour. `khenrix-setup` and `khenrix-upgrade` are the two whose engine this *is*; their
receipts are reseeded on the same reading and the deterministic evidence is
`scripts/lib/reconcile_test.py`, which `make eval-test` runs inside `make verify`.

- [ ] **Step 8: Commit**

```
cd /home/khenrix/git/khenrix-utils
make verify
echo "verify rc=$?"
make precommit
echo "precommit rc=$?"
git add scripts/lib/reconcile.py scripts/lib/reconcile_test.py evals marketplaces
git commit -m "$(cat <<'EOF'
fix(reconcile): a settings file nobody could read was read as a settings file nobody had

read_json_object returned {} for a file that exists and does not parse, which is what an
ABSENT file returns. Every caller turns {} into a full ADD list and the apply path writes
that over the whole file, so an unparseable ~/.claude/settings.json was silently replaced
with one holding only khenrix's keys. Two routes, one refusal: a decode error and a
non-object (`[1,2]`, `"x"`) both raise ReconcileReadError. Zero-length stays {} — the CLIs
create the file before writing it, and refusing that refuses a fresh machine.

Eleven eval receipts reseeded: reconcile.py is in checks.LIB_SCRIPTS and therefore in every
skill's source closure, so all eleven went stale for a change no eval set exercises. The
deterministic evidence is scripts/lib/reconcile_test.py, run by make eval-test inside verify.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UiV66Pt8cZVMq9t8WEAhpN
EOF
)"
```

Expected: `verify rc=0`, `precommit rc=0`.

---

## Task 2: `eval_trigger` — a dead judge is not a correct abstain

`scripts/eval_trigger.py:127-131`:

```python
raw = Path(rec["result_file"]).read_text() if rec.get("valid") else ""
got = parse_verdict(raw)
```

An invalid judge run (timeout, quota wall, missing binary) yields `raw = ""`;
`parse_verdict("")` returns `False`; and for every `near_miss` case `expected` is `False`, so
`got == expected` and the case scores **correct**. A judge that never ran scores 100% on the
near-miss axis. That is defect shape (2) — two different failures comparing equal — with the
comparison landing on the flattering side.

**Files:**
- Modify: `scripts/eval_trigger.py:74-155`
- Test: `scripts/eval_trigger.py` `_self_test()` (already run by `make eval-test`, `Makefile:166`)

**Interfaces:**
- Consumes: nothing from this plan.
- Produces: `parse_verdict(raw: str) -> bool | None` — `None` when no verdict could be read;
  `score(cases: list) -> dict` gains `"unreadable": int` and refuses to count an unreadable
  case as correct on either axis. Case dicts gain `"readable": bool`. No other module in this
  plan calls either.

**The specific fail-open this must not have:** scoring an unreadable case as *incorrect*
instead. That is the mirror error — it manufactures a failure out of an absence and makes a
dead judge look like a badly-triggering skill. Unreadable is a **third** answer that is
excluded from both numerators and both denominators, and reported.

**What input would make this produce a result cleaner than its evidence:** a judge that returns
valid output which is not JSON at all (a prose apology). `rec["valid"]` is `True`, so the
`valid` guard passes, and today `parse_verdict` swallows the `JSONDecodeError` into `False` —
another silent abstain. `parse_verdict` must return `None` for that too, not just for `""`.

- [ ] **Step 1: Write the failing self-test checks**

Insert into `scripts/eval_trigger.py`'s `_self_test()` (after the existing `score` checks
around line 236):

```python
    # A judge that did not answer is not a judge that answered "no". Before this, an
    # invalid run read "" -> parse_verdict("") -> False, and every near_miss case
    # (expected False) scored CORRECT — a dead judge measured 100% on that axis.
    ok.append(("no verdict text is None, not False", parse_verdict("") is None))
    ok.append(("prose with no JSON is None", parse_verdict("Sorry, I can't help.") is None))
    ok.append(("a real verdict still reads", parse_verdict('{"activate": true}') is True))
    ok.append(("a real negative still reads", parse_verdict('{"activate": false}') is False))
    ok.append(("a fenced verdict still reads",
               parse_verdict('```json\n{"activate": true}\n```') is True))
    dead = score([{"kind": "near_miss", "expected": False, "got": None, "readable": False},
                  {"kind": "near_miss", "expected": False, "got": None, "readable": False}])
    ok.append(("a dead judge scores 0, not 1.0", dead["accuracy"] == 0.0))
    ok.append(("a dead judge's cases are counted unreadable", dead["unreadable"] == 2))
    ok.append(("an unreadable case is in no denominator",
               dead["near_miss"]["total"] == 0 and dead["should_trigger"]["total"] == 0))
    mixed = score([{"kind": "near_miss", "expected": False, "got": False, "readable": True},
                   {"kind": "should_trigger", "expected": True, "got": None, "readable": False}])
    ok.append(("a mixed run scores only what it read",
               mixed["accuracy"] == 1.0 and mixed["unreadable"] == 1
               and mixed["should_trigger"]["total"] == 0))
```

- [ ] **Step 2: Run it to verify it fails**

```
cd /home/khenrix/git/khenrix-utils
python3 scripts/eval_trigger.py --self-test
```

Expected: FAIL on `no verdict text is None, not False` (currently `False`), and on the
`score`-shape checks (`unreadable` is not a key).

- [ ] **Step 3: Implement**

Replace `parse_verdict` and `score` in `scripts/eval_trigger.py`:

```python
def parse_verdict(raw: str):
    """True/False if the judge said whether the skill should activate; None if it did not.

    THREE ANSWERS, BECAUSE THERE ARE THREE STATES. This returned `False` for text it could
    not read, and `False` is also a real verdict — so a judge that timed out, hit a quota
    wall or answered in prose was recorded as having said "do not activate". Every
    `near_miss` case expects exactly that, so a judge that never ran scored 100% on the
    near-miss axis and the run wrote a receipt over it.
    """
    s = (raw or "").strip()
    if not s:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", s, re.DOTALL)
    if fence:
        s = fence.group(1)
    cand = s[s.find("{"): s.rfind("}") + 1] if "{" in s and "}" in s else s
    try:
        payload = json.loads(cand)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or "activate" not in payload:
        return None
    return bool(payload["activate"])


def score(cases: list) -> dict:
    """cases: list of {kind, expected, got, readable}. Accuracy over the cases that WERE read.

    AN UNREADABLE CASE IS IN NEITHER NUMERATOR NOR DENOMINATOR, and it is reported. Counting
    it correct is the fail-open this function was written with; counting it INCORRECT is the
    mirror error, which manufactures a triggering failure out of a judge that never spoke.
    The caller decides what a run with unreadable cases is worth — `run` below refuses to
    report an accuracy at all when every case is unreadable.
    """
    readable = [c for c in cases if c.get("readable", c.get("got") is not None)]
    unreadable = len(cases) - len(readable)
    fires = [c for c in readable if c["kind"] == "should_trigger"]
    misses = [c for c in readable if c["kind"] == "near_miss"]
    tp = sum(1 for c in fires if c["got"] is True)
    tn = sum(1 for c in misses if c["got"] is False)
    total = len(readable)
    return {
        "should_trigger": {"correct": tp, "total": len(fires)},
        "near_miss": {"correct": tn, "total": len(misses)},
        "unreadable": unreadable,
        "accuracy": round((tp + tn) / total, 4) if total else 0.0,
    }
```

- [ ] **Step 4: Wire the reading into `run`**

In `scripts/eval_trigger.py`'s `run()`, replace the three lines that build a case:

```python
            rec = m["providers"][0]
            raw = Path(rec["result_file"]).read_text() if rec.get("valid") else ""
            got = parse_verdict(raw)
            # `valid` and a readable verdict are TWO conditions and the second was assumed
            # from the first: a valid run whose text is prose parses to no verdict at all.
            readable = got is not None
            cases.append({"kind": kind, "prompt": prompt, "expected": kind == "should_trigger",
                          "got": got, "readable": readable,
                          "why": ("" if readable else
                                  ("the judge run was invalid: "
                                   + str(rec.get("reason", "no reason recorded"))
                                   if not rec.get("valid")
                                   else "the judge answered, and no activate verdict could be "
                                        "read from what it said"))})
            mark = "✓" if readable and got == (kind == "should_trigger") else \
                   ("?" if not readable else "✗")
            print(f"  {mark} [{kind}] {prompt[:60]}")
```

and immediately after `result = score(cases)`, add:

```python
    if result["unreadable"]:
        print(f"  ⚠ {result['unreadable']} of {len(cases)} case(s) produced no readable "
              "verdict and are counted in neither axis")
    if result["unreadable"] == len(cases):
        print("  ✗ no case produced a readable verdict — there is no measurement here")
        return 1
```

- [ ] **Step 5: Run the self-test to verify it passes**

```
cd /home/khenrix/git/khenrix-utils
python3 scripts/eval_trigger.py --self-test
```

Expected: PASS on all checks including the eight new ones.

- [ ] **Step 6: Mutation-test the new branches**

```
cd /home/khenrix/git/khenrix-utils
git status --short
python3 scripts/mutate.py --file scripts/eval_trigger.py \
  --old '    if not s:' --new '    if False:' \
  --test 'python3 scripts/eval_trigger.py --self-test'
python3 scripts/mutate.py --file scripts/eval_trigger.py \
  --old '    readable = [c for c in cases if c.get("readable", c.get("got") is not None)]' \
  --new '    readable = list(cases)' \
  --test 'python3 scripts/eval_trigger.py --self-test'
python3 scripts/mutate.py --file scripts/eval_trigger.py \
  --old '    if not isinstance(payload, dict) or "activate" not in payload:' \
  --new '    if False:' \
  --test 'python3 scripts/eval_trigger.py --self-test'
git status --short
```

Expected: all three `CAUGHT`; `git status --short` identical before and after.

- [ ] **Step 7: Commit**

```
cd /home/khenrix/git/khenrix-utils
make render
make verify
echo "verify rc=$?"
make precommit
echo "precommit rc=$?"
git add scripts/eval_trigger.py marketplaces
git commit -m "$(cat <<'EOF'
fix(eval-trigger): a judge that never answered was scored as having answered "do not activate"

parse_verdict returned False for text it could not read, and False is also a real verdict.
Every near_miss case expects False, so a dead judge — timeout, quota wall, prose reply —
measured 100% on that axis and the run wrote a receipt over it. parse_verdict now returns
None for "no verdict here" and score() counts those cases in neither numerator nor
denominator, reports the count, and run() exits 1 when nothing was readable at all.

Counting an unreadable case INCORRECT would be the mirror error: it manufactures a
triggering failure out of a judge that never spoke.

Measured: scripts/eval_trigger.py is in no skill's _skill_source_files closure, so no eval
receipt goes stale here.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UiV66Pt8cZVMq9t8WEAhpN
EOF
)"
```

Expected: `verify rc=0`, `precommit rc=0`, **no** receipt warnings (measured M8).

---

## Task 3: §20 — the preflight refusal, and a bundle hash that is a measurement

Two deliverables, one commit, because neither is meaningful alone: §20's refusal exists so that
a task that cannot be made portable is stopped **before** a bundle is claimed for it, and the
bundle wiring exists so that `fingerprint.PromptIdentity.bundle_sha256` — `None` for every real
seat since Plan I — carries a value §11's agreement label can read.

**Files:**
- Modify: `shared/lib/forge/preflight.py` (add `task_refusals`; nothing existing changes)
- Modify: `shared/lib/forge/taskbundle.py` (add `read_task_bundle_if_recorded`)
- Modify: `shared/lib/forge/runner.py` (`run_seat` materializes the run's bundle)
- Test: `tests/test_forge_preflight.py`, `tests/test_forge_taskbundle.py`,
  `tests/test_forge_runner.py`, `tests/test_forge_seams.py`

**Interfaces:**
- Consumes: `taskbundle.TaskBundle`, `taskbundle.bundle_hash(b) -> str`,
  `taskbundle.read_task_bundle(run_dir) -> TaskBundle`,
  `taskbundle.materialize(b, source_root, seat_path) -> Path`,
  `taskbundle.verify_materialized(b, seat_path) -> None`,
  `taskbundle.installed_closure(cli) -> str | None`,
  `taskbundle.ambient_verdict(closures: dict) -> bool`,
  `taskbundle.ambient_note(skill: str) -> str`,
  `storage.task_bundle_path(run_dir) -> Path`,
  `fleet.clone_seat(repo, baseline, dest, *, name, identity, template_dir=None) -> fleet.Seat`,
  `preflight.PreflightError`.
- Produces:
  - `preflight.task_refusals(instruction: str, *, bundle=None, closures=None) -> tuple[str, ...]`
  - `preflight.ambient_notes(instruction: str, *, closures) -> tuple[str, ...]`
  - `taskbundle.read_task_bundle_if_recorded(run_dir) -> TaskBundle | None`
  - `runner.run_seat` materializes the run's bundle into `<seat git-dir>/khenrix-forge/task`
    immediately after `clone_seat` and before `F0`, and calls `verify_materialized`.
  - Task 6's CLI consumes all three.

**The specific fail-opens these must not have:**
1. `task_refusals` returning `()` for an instruction it could not read (empty, `None`,
   non-`str`). It raises `PreflightError` instead — an unexamined task is not a clean task.
2. `read_task_bundle_if_recorded` catching `TaskBundleError` and answering `None`. That
   collapses **corrupt** into **not recorded**, and three seats would then be launched with
   nothing materialized while `bundle_sha256` claims a bundle. `None` is returned for
   `not path.exists()` and for **nothing else**.
3. A bundle being supplied clearing **any** refusal. §20 forbids automatic translation; a
   bundle carries files, and files make neither a `subagent_type` portable **nor `/markitdown`
   identical across three CLIs**. The ambient-skill bar therefore does not consult `bundle` at
   all — a `bundle is None` condition on it would be dead code in the only production caller,
   because `cli.start` always builds and passes one, and the three-closure check §20 asks for
   would never run outside the test suite.
4. Two spellings of the bundle hash. The CLI's `make_launcher(bundle_sha256=…)` and
   `run_seat`'s materialization must both derive from `storage.task_bundle_path(run_dir)`
   through `taskbundle._decode` — one file, one decoder. A seam test names that property.

**What input would make each produce a result cleaner than its evidence:**
- `task_refusals`: an instruction that names a skill only in a **fenced code block** or a URL
  path segment (`https://x/y/khenrix-audit`). The ambient-skill pattern matches, a refusal is
  raised for a skill nobody asked for, and the operator learns to write around the detector —
  which is the same as not having it. The detector therefore reports the matched **referent**
  in the refusal line so the operator can see what was matched, and the test set contains both
  a genuine reference and a URL that must not match.
- `read_task_bundle_if_recorded`: a **zero-length** `task-bundle.json`. It exists, so `None` is
  wrong; `json.loads(b"")` raises `ValueError`, which `read_task_bundle` already turns into
  `TaskBundleError`. Verified by test rather than assumed.
- `run_seat`'s materialization: a run whose bundle was written **after** a seat's clone. The
  seat materializes a bundle the launcher never hashed. The seam test below pins that both
  readers open the same path; the ordering is the CLI's (Task 6), which writes the bundle
  before `runner.run`.

- [ ] **Step 1: Write the failing tests for `task_refusals`**

Append to `tests/test_forge_preflight.py`:

```python
def test_a_task_naming_provider_specific_machinery_is_refused():
    """§20: fail preflight for irreducibly provider-specific workflows, and ask for a
    portable task bundle instead. Each string below names a thing that exists on ONE of the
    three CLIs and cannot be carried by a file."""
    for text, referent in (
            ("Dispatch a subagent with subagent_type: Explore and merge its findings.",
             "subagent_type"),
            ("Read ${CLAUDE_PLUGIN_ROOT}/skills/x/SKILL.md and follow it.",
             "CLAUDE_PLUGIN_ROOT"),
            ("Run codex exec --json over the diff.", "codex exec"),
            ("Call mcp__chrome-devtools__take_snapshot and describe the page.",
             "mcp__chrome-devtools__take_snapshot"),
            ("Look at ~/.codex/config.toml first.", "~/.codex")):
        out = preflight.task_refusals(text)
        assert out, f"not refused: {text!r}"
        assert any(referent in line for line in out), (referent, out)
        assert any("portable task bundle" in line for line in out), out


def test_a_bundle_does_not_make_a_provider_specific_referent_portable():
    """§20 forbids automatic translation. A bundle carries FILES; it cannot turn a named
    subagent type into something codex or agy has."""
    b = _a_task_bundle()          # helper below
    out = preflight.task_refusals("Use subagent_type: Explore.", bundle=b)
    assert out, "a supplied bundle cleared a refusal it cannot answer"


def test_an_ambient_skill_is_refused_unless_all_three_closures_hash_identically():
    """§20: use a named skill only when all three hash identically. `installed_closure`
    answers None for a CLI that is not installed, and `ambient_verdict` reads None as False —
    so three ABSENCES must not read as agreement."""
    text = "Follow the /markitdown skill to convert the file."
    assert preflight.task_refusals(text, closures={"claude": "a", "codex": "a", "agy": "a"}) == ()
    out = preflight.task_refusals(text, closures={"claude": "a", "codex": "b", "agy": "a"})
    assert out and any("markitdown" in line for line in out), out
    three_absences = {"claude": None, "codex": None, "agy": None}
    out2 = preflight.task_refusals(text, closures=three_absences)
    assert out2, "three uninstalled CLIs hashed identically and licensed an ambient skill"
    # The refusal names the mismatching hashes readably. `"not installed"[:12]` is
    # `"not installe"`, which is the truncation reading as a hash prefix.
    assert "not installed" in " ".join(out2), out2


def test_a_bundle_does_not_clear_the_ambient_skill_bar_either(tmp_path):
    """The bar must not consult `bundle`. `cli.start` ALWAYS builds one and passes it, so a
    `bundle is None` condition would make §20's three-closure check dead in the only
    production caller — and a directory of files does not make `/markitdown` the same skill on
    claude, codex and agy, which was the entire argument for the check."""
    b = _a_task_bundle(tmp_path)
    text = "Follow the /markitdown skill to convert the file."
    out = preflight.task_refusals(text, bundle=b,
                                  closures={"claude": "a", "codex": "b", "agy": "a"})
    assert out and any("markitdown" in line for line in out), (
        "a supplied bundle cleared the ambient-skill bar, which is the shape that made the "
        "check unreachable from the CLI")


def test_a_cleared_ambient_skill_produces_the_note_the_prompt_carries(tmp_path):
    """`ambient_note` is what §20 asks the caller to add to the prompt, and until this
    function existed it had no caller anywhere. A cleared skill yields a note; a skill that
    did NOT clear yields none, because the refusal is the answer in that case."""
    text = "Follow the /markitdown skill to convert the file."
    agreed = {"claude": "a", "codex": "a", "agy": "a"}
    notes = preflight.ambient_notes(text, closures=agreed)
    assert len(notes) == 1 and "markitdown" in notes[0], notes
    assert preflight.ambient_notes(text,
                                   closures={"claude": "a", "codex": "b", "agy": "a"}) == ()
    assert preflight.ambient_notes("no skills named here.", closures=agreed) == ()


def test_a_skill_name_inside_a_url_is_not_read_as_an_ambient_skill_reference():
    """The detector reports what it matched so an operator can see it — and a detector that
    fires on a URL path segment teaches the operator to write around it, which is the same as
    not having one."""
    text = "See https://example.com/docs/markitdown for background; write your own converter."
    assert preflight.task_refusals(text, closures={}) == ()


def test_an_instruction_this_engine_could_not_read_is_refused_rather_than_cleared():
    """() means 'examined and nothing stands in the way'. An unexamined task must not
    borrow that sentence."""
    for bad in ("", "   ", None, 5, b"bytes"):
        try:
            preflight.task_refusals(bad)
        except preflight.PreflightError:
            pass
        else:
            raise AssertionError(f"{bad!r} was not refused")
```

Add the fixture helper near the top of the same file:

```python
def _a_task_bundle(tmp_path=None):
    """A minimal real TaskBundle, built through `scan` so it carries real hashes."""
    import tempfile
    root = Path(tmp_path or tempfile.mkdtemp())
    (root / "TASK.md").write_text("Do the thing.\n")
    return taskbundle.scan(root, entrypoint="TASK.md")
```

- [ ] **Step 2: Run them to verify they fail**

```
cd /home/khenrix/git/khenrix-utils
uvx --with pytest pytest -q tests/test_forge_preflight.py -k task_refusals
```

Expected: FAIL — `AttributeError: module 'forge.preflight' has no attribute 'task_refusals'`.

- [ ] **Step 3: Implement `task_refusals`**

Append to `shared/lib/forge/preflight.py` (after `refusals`), and add
`from . import taskbundle` to the imports **only if it does not create a cycle** — verify with
`python3 -c "import forge.preflight"` after; `taskbundle` imports `gitcmd`, `snapshot` and
`storage`, none of which import `preflight`, so it does not:

```python
# §20's referents: things that exist on ONE of the three CLIs and that no file can carry. The
# patterns are NARROW on purpose and each one names what it matched, because a detector whose
# refusal does not say what it saw teaches an operator to rewrite around it — which is the same
# as not having a detector. These are matched against the OPERATOR'S OWN task text, not against
# a seat's merged stderr, so the phantom-match hazard `council.engine` documents at
# TOOL_PERMISSION_SENTINELS does not apply here: nobody is echoing a file into this string.
_PROVIDER_SPECIFIC = (
    (re.compile(r"\bsubagent_type\b"), "a named subagent type"),
    (re.compile(r"\$\{?CLAUDE_PLUGIN_ROOT\}?"), "a Claude plugin-root variable"),
    (re.compile(r"\$\{?PLUGIN_ROOT\}?"), "a Codex plugin-root variable"),
    (re.compile(r"\bcodex\s+exec\b"), "a codex-only subcommand"),
    (re.compile(r"--dangerously-skip-permissions"), "a provider-only permission flag"),
    (re.compile(r"\bmcp__[A-Za-z0-9_]+"), "an MCP tool name"),
    (re.compile(r"~/\.(?:claude|codex|gemini)\b"), "a provider configuration directory"),
)

# A named skill, in the two forms an operator writes one. The leading boundary is a LINE START
# or whitespace and never a `/`, so a URL path segment (`https://x/docs/markitdown`) does not
# match — measured against the test that exists for it.
_AMBIENT_SKILL = (
    re.compile(r"(?:^|(?<=\s))/([a-z][a-z0-9-]{2,63})\b"),
    re.compile(r"\buse (?:the )?`?([a-z][a-z0-9-]{2,63})`? skill\b", re.I),
)

_PORTABLE_ASK = ("Supply a portable task bundle instead — a directory whose entrypoint states "
                 "the task in provider-neutral terms, with every file it references beside it.")


def task_refusals(instruction, *, bundle=None, closures=None) -> tuple[str, ...]:
    """§20's refusal, about the TASK. `refusals` above answers about the REPOSITORY.

    NEITHER SUBSUMES THE OTHER AND BOTH ARE READ, which is why this is a second function and
    not a field on `Report`. `Report` describes one static look at a repository and `gate.open_run`
    reads `refusals` off it; the task is not in scope there — it arrives with the front end. A
    caller runs both before opening a run.

    `()` MEANS EXAMINED AND NOTHING STANDS IN THE WAY. An instruction this function could not
    read may not borrow that sentence, so a non-string, an empty string and whitespace all
    RAISE rather than return the clean answer.

    A BUNDLE CLEARS NOTHING HERE, AND `bundle` IS READ BY NO CONDITION BELOW. §20: "Do not
    automatically translate provider-specific tools or subagent semantics." A bundle carries
    files; it cannot give codex a subagent type, and it cannot make `/markitdown` the same
    skill on three CLIs — which is the whole reason §20 asks for a three-way closure hash. The
    parameter is kept because a caller reads more naturally passing what it has, and because a
    future referent might genuinely be answerable by a file; it is NOT a licence, and a
    `bundle is None` guard on the ambient bar would be dead code in the only production caller,
    since `cli.start` always builds one. What the caller does with a CLEARED ambient skill is
    `ambient_notes` below.

    `closures` IS THE THREE LIVE INSTALLED HASHES, `{cli: sha or None}`, and it is an ARGUMENT
    because resolving it walks three plugin caches and this function is called at a gate.
    `None` for a CLI is what `taskbundle.installed_closure` returns for one that is not
    installed, and `ambient_verdict` reads any `None` as False — so three ABSENCES do not hash
    identically. Passing `closures=None` means nobody resolved them, which is not the same as
    resolving them and finding a mismatch: it is treated as the same refusal but says so in
    different words, because a named skill relied on without the check is exactly what §20
    forbids.
    """
    if not isinstance(instruction, str):
        raise PreflightError(
            f"§20 resolves a task into a portable instruction, and this one is "
            f"{type(instruction).__name__}. An empty answer here would say 'examined, nothing "
            "stands in the way' about a task nobody examined.")
    if not instruction.strip():
        raise PreflightError(
            "§20's resolution has nothing to resolve: this instruction is empty or whitespace. "
            "A run opened over it would launch three seats with no task.")
    out = []
    for pattern, what in _PROVIDER_SPECIFIC:
        m = pattern.search(instruction)
        if m:
            out.append(
                f"this task is not portable: it names {what} ({m.group(0)!r}), which two of the "
                f"three seats do not have. §20 refuses to translate it automatically. "
                f"{_PORTABLE_ASK}")
    named = _named_skills(instruction)
    if named and not _ambient_ok(closures):
        why = ("the three installed closures were never resolved" if not closures else
               "the three installed closures do not hash identically: " + _closure_line(closures))
        for skill in named:
            out.append(
                f"this task relies on the ambient `{skill}` skill and {why}. §20 permits a "
                "named skill only when all three installed copies hash identically and it "
                f"is declared provider-neutral. {_PORTABLE_ASK}")
    return tuple(out)


def _named_skills(instruction: str) -> tuple[str, ...]:
    named = []
    for pattern in _AMBIENT_SKILL:
        named += [m.group(1) for m in pattern.finditer(instruction)]
    return tuple(sorted(set(named)))


def _ambient_ok(closures) -> bool:
    """`closures` unresolved is NOT `closures` resolved and disagreeing, and neither licenses
    an ambient skill — but they are different facts and the refusal says which."""
    return bool(closures) and taskbundle.ambient_verdict(closures)


def _closure_line(closures) -> str:
    # NOT `(closures.get(c) or "not installed")[:12]`: that truncates to `not installe`, which
    # reads as a twelve-character hash prefix. A CLI with no installed copy is a different fact
    # from a CLI whose copy hashes differently, and the operator has to be able to see which.
    return ", ".join(
        f"{c}=" + ("not installed" if not closures.get(c) else str(closures[c])[:12])
        for c in ("claude", "codex", "agy"))


def ambient_notes(instruction, *, closures) -> tuple[str, ...]:
    """§20's note for every named skill this task may rely on, for the caller to add to the
    prompt. `()` when nothing was named, and `()` when the bar was not cleared.

    THIS IS `ambient_note`'s CALLER, and until it existed there was none: `task_refusals`
    answers what STOPS a run, and §20's other half — "declare it provider-neutral in the
    prompt" — needs a producer or the check clears a skill and then says nothing about it.

    NOT CLEARED MEANS NO NOTE, not a hedged one. `task_refusals` has already refused that run;
    emitting a note beside a refusal would put a sentence licensing the skill into the same
    output that says it may not be used.
    """
    if not isinstance(instruction, str):
        raise PreflightError(
            f"§20's ambient note is about a task's text, and this one is "
            f"{type(instruction).__name__}")
    if not _ambient_ok(closures):
        return ()
    return tuple(taskbundle.ambient_note(s) for s in _named_skills(instruction))
```

Add `import re` to `preflight.py`'s imports if it is not already there. **Read
`taskbundle.ambient_note`'s actual return shape before wiring it** — it is drafted here as a
`str` per this task's Interfaces block, and the standing brief is that this plan's draft code
has been wrong in every task of every plan.

- [ ] **Step 4: Run the tests to verify they pass**

```
cd /home/khenrix/git/khenrix-utils
uvx --with pytest pytest -q tests/test_forge_preflight.py
python3 -c "import sys; sys.path.insert(0,'shared/lib'); import forge.preflight"
```

Expected: PASS, and the import line prints nothing (no cycle).

- [ ] **Step 5: Write the failing test for `read_task_bundle_if_recorded`**

Append to `tests/test_forge_taskbundle.py`:

```python
def test_a_bundle_that_is_not_recorded_and_one_that_cannot_be_read_are_different_answers(tmp_path):
    """None means THIS RUN RECORDED NONE. It may not also mean 'the file is there and this
    engine could not read it' — three seats would then be launched with nothing materialized
    while the launcher's prompt_identity claims a bundle hash."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    assert taskbundle.read_task_bundle_if_recorded(run_dir) is None

    path = storage.task_bundle_path(run_dir)
    path.write_bytes(b"")                      # exists, zero-length
    with pytest.raises(taskbundle.TaskBundleError):
        taskbundle.read_task_bundle_if_recorded(run_dir)

    path.write_bytes(b'{"version": 1}')        # exists, parses, is not a bundle
    with pytest.raises(taskbundle.TaskBundleError):
        taskbundle.read_task_bundle_if_recorded(run_dir)

    src = tmp_path / "src"
    src.mkdir()
    (src / "TASK.md").write_text("do it\n")
    b = taskbundle.scan(src, entrypoint="TASK.md")
    taskbundle.write_task_bundle(run_dir, b)
    assert taskbundle.read_task_bundle_if_recorded(run_dir) == b
```

- [ ] **Step 6: Run it to verify it fails, then implement**

```
cd /home/khenrix/git/khenrix-utils
uvx --with pytest pytest -q tests/test_forge_taskbundle.py -k not_recorded
```

Expected: FAIL — no such attribute.

Append to `shared/lib/forge/taskbundle.py`, immediately after `read_task_bundle`:

```python
def read_task_bundle_if_recorded(run_dir):
    """The run's bundle, or `None` because this run recorded none — and `None` for NOTHING ELSE.

    `read_task_bundle`'s raise is right for a caller that knows a bundle exists; `runner.run_seat`
    is a caller that does not, because the whole suite drives runs that predate §20. So the
    ABSENCE of the file is an answer here.

    WHAT IS NOT AN ANSWER: a file that exists and this engine cannot read. Folding that into
    `None` would launch three seats with nothing materialized while the launcher's
    `prompt_identity` carries a `bundle_sha256` the CLI computed from the same path a moment
    earlier — a record claiming a bundle over seats that got none. Every failure but
    "the file is not there" therefore propagates as `TaskBundleError`.

    A FILE THAT VANISHES BETWEEN THE TWO CALLS RAISES, and that is correct rather than a race
    to smooth over: something removed a run's task bundle while the run was reading it, and
    `read_task_bundle`'s own FileNotFoundError message names the path.
    """
    if not storage.task_bundle_path(run_dir).exists():
        return None
    return read_task_bundle(run_dir)
```

- [ ] **Step 7: Run to verify it passes**

```
cd /home/khenrix/git/khenrix-utils
uvx --with pytest pytest -q tests/test_forge_taskbundle.py
```

Expected: PASS.

- [ ] **Step 8: Write the failing test for `run_seat`'s materialization**

Append to `tests/test_forge_runner.py` (match the file's existing fixture style for building a
manifest + baseline + run dir; reuse whatever helper the file already has — do not invent a
second one):

```python
def test_a_run_with_a_task_bundle_materializes_it_into_every_seat(tmp_path, ...):
    """§20: 'materialize it identically in every clone'. The bundle lands in the seat's GIT
    DIRECTORY (`taskbundle.task_dir`), which `harvest.record` never walks — `snapshot.take`
    skips `.git` — so it is invisible to the artifact set by construction rather than by a
    name rule."""
    run_dir, manifest, base = _a_confirmed_run(tmp_path)      # existing helper
    src = tmp_path / "task"
    src.mkdir()
    (src / "TASK.md").write_text("Refactor the thing.\n")
    b = taskbundle.scan(src, entrypoint="TASK.md")
    taskbundle.write_task_bundle(run_dir, b)

    out = runner.run_seat(manifest, run_dir, base, name="claude", attempt=1,
                          identity=("A", "a@b"), launch=_a_fake_launch())
    laid = taskbundle.task_dir(out.seat.path)
    assert (laid / "TASK.md").read_text() == "Refactor the thing.\n"
    taskbundle.verify_materialized(b, out.seat.path)          # raises if a byte moved
    # The bundle is NOT in the artifact set: it is engine-supplied, not the agent's work.
    assert "TASK.md" not in out.artifacts.paths


def test_a_run_that_recorded_no_task_bundle_still_builds_its_seats(tmp_path, ...):
    """The whole suite drives runs that predate §20; an absent bundle is not a refusal."""
    run_dir, manifest, base = _a_confirmed_run(tmp_path)
    out = runner.run_seat(manifest, run_dir, base, name="claude", attempt=1,
                          identity=("A", "a@b"), launch=_a_fake_launch())
    assert out.seat is not None


def test_a_task_bundle_that_cannot_be_read_stops_the_seat_before_the_provider_is_paid(
        tmp_path, ...):
    """The refusal has to land before `launch`, or a corrupt bundle costs a provider call and
    the seat answers a task it was never given."""
    run_dir, manifest, base = _a_confirmed_run(tmp_path)
    storage.task_bundle_path(run_dir).write_bytes(b"{ not json")
    calls = []
    with pytest.raises(taskbundle.TaskBundleError):
        runner.run_seat(manifest, run_dir, base, name="claude", attempt=1,
                        identity=("A", "a@b"),
                        launch=lambda **kw: calls.append(kw) or {})
    assert calls == [], "a provider was launched over a bundle nobody could read"
```

- [ ] **Step 9: Run to verify it fails, then implement**

```
cd /home/khenrix/git/khenrix-utils
uvx --with pytest pytest -q tests/test_forge_runner.py -k task_bundle
```

Expected: FAIL — no `TASK.md` in the seat's git directory.

In `shared/lib/forge/runner.py`, add the helper above `run_seat`:

```python
def _materialize_the_task(run_dir: Path, seat_path) -> None:
    """§20's bundle into this seat, before anything else runs in it.

    BEFORE `F0`, AND THE ORDER IS NOT WHAT KEEPS IT OUT OF THE ARTIFACT SET. `taskbundle.task_dir`
    puts the bundle under the seat's GIT DIRECTORY, and `harvest.record` walks with
    `snapshot.take`'s `.git` skip — so the inventory never sees it whatever the order is. What
    the order buys is that the bundle is in place before the confirmed SETUP command runs, which
    is the first thing in the seat that could read it.

    A BUNDLE THAT CANNOT BE READ STOPS THE SEAT HERE, before `launch`. §5.2 quotes the provider
    calls; spending one on a seat that was handed no task is the expensive half of this
    refusal, and `read_task_bundle_if_recorded` is what keeps 'not recorded' and 'not readable'
    from arriving at this line as the same value.
    """
    b = taskbundle.read_task_bundle_if_recorded(run_dir)
    if b is None:
        return
    taskbundle.materialize(b, storage.task_source_path(run_dir), seat_path)
    # Read back what was written rather than trusting the writer: `materialize` and
    # `verify_materialized` walk the same manifest from opposite ends, which is the only way
    # 'laid down' and 'laid down correctly' stop being one claim.
    taskbundle.verify_materialized(b, seat_path)
```

Add `from . import taskbundle` to `runner.py`'s imports. Insert the call in `run_seat`
immediately after the clone:

```python
    st = fleet.clone_seat(repo, baseline, path, name=name, identity=identity)
    _materialize_the_task(run_dir, st.path)
    child_env = fleet.forge_child_env(repo)
```

Add to `shared/lib/forge/storage.py`, beside the other run-directory names:

```python
def task_source_path(run_dir) -> Path:
    """Where the run keeps the BYTES its task-bundle manifest describes.

    `task_bundle_path` holds the manifest — paths, hashes, modes. `materialize` needs the
    content, and reading it from wherever the operator's directory happened to be would make a
    resume depend on a tree that may be gone: §20 requires the resolved instruction be
    persisted so `--collect` never depends on vanished conversation context. One name here, so
    the writer (the CLI) and the reader (`runner`) cannot drift.
    """
    return Path(run_dir) / "task"
```

- [ ] **Step 10: Add the one-file-one-decoder seam test**

Append to `tests/test_forge_seams.py`:

```python
def test_the_bundle_hash_the_launcher_claims_and_the_one_the_seat_gets_have_one_source():
    """Two spellings of one predicate will eventually disagree (Plan-independent lesson).

    `launch.make_launcher(bundle_sha256=…)` is what reaches `fingerprint.PromptIdentity`, and
    `runner._materialize_the_task` is what reaches the seat's disk. This asserts both are
    derived from `storage.task_bundle_path(run_dir)` through `taskbundle`'s own decoder and
    from nothing else — read off the SOURCE, because a behavioural test would need a real CLI.
    """
    src = (ROOT / "shared" / "lib" / "forge" / "runner.py").read_text()
    body = src[src.index("def _materialize_the_task"):]
    body = body[:body.index("\ndef ")]
    assert "read_task_bundle_if_recorded(run_dir)" in body
    assert "storage.task_bundle_path" not in body, (
        "the materializer opens the manifest path itself instead of going through "
        "taskbundle's decoder — that is the second spelling")
    cli = (ROOT / "shared" / "lib" / "forge" / "cli.py")
    if cli.exists():          # Task 6 lands this; the assertion arms itself when it does
        t = cli.read_text()
        assert "read_task_bundle(" in t or "read_task_bundle_if_recorded(" in t
        assert "bundle_hash(" in t, (
            "the CLI computes a bundle hash some other way than taskbundle.bundle_hash")
```

- [ ] **Step 11: Run the whole suite**

```
cd /home/khenrix/git/khenrix-utils
uvx --with pytest pytest -q tests/
```

Expected: PASS. Record the count; it must be ≥ 1641 + the new tests, with **no deletions**.

- [ ] **Step 12: Mutation-test the three fail-closed branches**

```
cd /home/khenrix/git/khenrix-utils
git status --short
python3 scripts/mutate.py --file shared/lib/forge/taskbundle.py \
  --old '    if not storage.task_bundle_path(run_dir).exists():' --new '    if True:' \
  --test 'uvx --with pytest pytest -q tests/test_forge_taskbundle.py'
python3 scripts/mutate.py --file shared/lib/forge/preflight.py \
  --old '    if not instruction.strip():' --new '    if False:' \
  --test 'uvx --with pytest pytest -q tests/test_forge_preflight.py'
python3 scripts/mutate.py --file shared/lib/forge/preflight.py \
  --old '    return bool(closures) and taskbundle.ambient_verdict(closures)' \
  --new '    return True' \
  --test 'uvx --with pytest pytest -q tests/test_forge_preflight.py'
python3 scripts/mutate.py --file shared/lib/forge/preflight.py \
  --old '    if named and not _ambient_ok(closures):' \
  --new '    if named and bundle is None and not _ambient_ok(closures):' \
  --test 'uvx --with pytest pytest -q tests/test_forge_preflight.py'
python3 scripts/mutate.py --file shared/lib/forge/runner.py \
  --old '    taskbundle.verify_materialized(b, seat_path)' --new '    pass' \
  --test 'uvx --with pytest pytest -q tests/test_forge_runner.py'
git status --short
```

Expected: all five `CAUGHT`. `git status --short` identical before and after. The last one is
the dead-bar mutation in reverse: re-adding `bundle is None` must fail
`test_a_bundle_does_not_clear_the_ambient_skill_bar_either`, or the condition that made §20's
check unreachable from the CLI can come back unnoticed.

- [ ] **Step 13: Render, gate, commit**

```
cd /home/khenrix/git/khenrix-utils
make render
make verify
echo "verify rc=$?"
make precommit
echo "precommit rc=$?"
git add shared/lib/forge/preflight.py shared/lib/forge/taskbundle.py \
        shared/lib/forge/runner.py shared/lib/forge/storage.py \
        tests/test_forge_preflight.py tests/test_forge_taskbundle.py \
        tests/test_forge_runner.py tests/test_forge_seams.py marketplaces
git commit -m "$(cat <<'EOF'
feat(forge): §20 refuses a task no bundle can carry, and a bundle a seat actually gets

preflight.task_refusals answers about the TASK where refusals answers about the REPOSITORY;
neither subsumes the other and a caller reads both. An unexamined instruction raises rather
than borrowing the clean () answer, a supplied bundle does not clear a provider-specific
referent (§20 forbids automatic translation), and three uninstalled CLIs do not hash
identically into a licence for an ambient skill.

run_seat now materializes the run's bundle into the seat's git directory before anything
runs there, and verify_materialized reads it back. read_task_bundle_if_recorded answers
None for "this run recorded none" and for nothing else: folding an unreadable bundle into
that None would launch three paid seats with nothing laid down while prompt_identity
carried a hash computed from the same path.

PromptIdentity.bundle_sha256 has been None for every real seat since it was written. It now
has a producer.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UiV66Pt8cZVMq9t8WEAhpN
EOF
)"
```

Expected: `verify rc=0`, `precommit rc=0`.

---

## Task 4: §16 — the synthesis worktree, the seat transport, and what is mergeable

This is the plan's **first git call into the user's own repository since the confirmation
gate**, and the closures Plan G inverted refuse both verbs it needs until this task adds them
with the measurements beside them.

**Files:**
- Create: `shared/lib/forge/handover.py`
- Modify: `shared/lib/forge/storage.py` (`handover_path`)
- Modify: `tests/test_forge_seams.py` (`_DIFF_DRIVER_SAFE` gains `worktree` and `fetch`)
- Test: `tests/test_forge_handover.py` (new)

**Interfaces:**
- Consumes: `gitcmd.git(repo, *args, env_extra=None, check=True, binary=False, timeout=60,
  user_config=False)`, `gitcmd.READONLY`, `gitcmd.NO_DAEMON_CACHE`, `gitcmd.NO_HOOKS`,
  `gitcmd.GitError`; `runstate.Manifest` fields `run_id`, `repo_path`, `base_commit`,
  `baseline_ref`, `baseline_commit`, `tracked_tree_oid`, `selected_paths`;
  `bundle.CandidateBundle` fields `tracked_patch`, `sidecars`, `omitted`, `baseline_commit`;
  `bundle.SidecarEntry(path, kind, mode, payload)`; `storage.atomic_write(path, data: bytes)`;
  `storage.run_root(repo_path, run_id, must_be_new=False) -> Path`.
- Produces:
  - `handover.HandoverError(RuntimeError)`
  - `handover.SYNTHESIS = "synthesis"`; `handover.branch(run_id, name) -> str`
  - `handover.create_synthesis_worktree(repo, dest, *, run_id, at) -> str` → the branch name
  - `handover.transport_seat(repo, clone_path, *, run_id, seat) -> str` → the fetched OID
  - `handover.MERGE_READY = "merge-ready-branch"`, `handover.PATCH_ONLY = "patch"`
  - `handover.Mergeability(kind: str, why: str, integration: tuple[str, ...])`
  - `handover.mergeability(manifest, *, synthesis_tree_oid, sidecars) -> Mergeability`
  - `handover.OutOfBand(path: str, sha256: str, size: int, copy_command: tuple[str, ...])`
  - `handover.out_of_band(sidecars, *, synthesis_path, run_dir) -> tuple[OutOfBand, ...]`
  - `handover.Handover(...)` frozen record + `write_handover(run_dir, h)` /
    `read_handover(run_dir) -> Handover | None`
  - `storage.handover_path(run_dir) -> Path`
  - Task 5 renders the header from `Handover`; Task 7's `--gc` reads it.

**The specific fail-opens this must not have:**
1. `git worktree add --detach`. §16 forbids it in one sentence with the reason: a detached HEAD
   leaves commits unreachable and the next `git gc` deletes the deliverable. A test asserts
   `--detach` appears nowhere in the module.
2. A bare `git fetch <path>`. Default refspecs pull whatever refs the seat created into the
   user's repository — reintroducing through the back door the write path §4 closed at the
   front. The refspec is always explicit and a test pins its exact shape.
3. `force-add`ing an ignored artifact. §16: "Never force-added — that violates the originating
   skill's contract and would put `node_modules` in the object store forever." A test asserts
   `-f` / `--force` appears in no call in the module.
4. `mergeability` answering `MERGE_READY` on an **unmeasured** tree, or on an **unfused** one.
   `synthesis_tree_oid` must be *read*, not merely liveness-checked: `None` refuses (unmeasured
   and dirty are two different records), and a value EQUAL to `manifest.tracked_tree_oid` is a
   synthesis worktree nobody fused into and refuses too. A parameter that is validated and then
   discarded is the shape that lets an empty delivery render as "the branch merges as it
   stands", and its docstring then describes a comparison the code does not perform.
5. `out_of_band` returning `()` for both "there were no ignored artifacts" and "nobody
   enumerated them". `sidecars=None` raises; `sidecars=()` returns `()` and the caller's record
   says which.

**What input would make each produce a result cleaner than its evidence:**
- `mergeability`: a **matching tracked tree that still carries one sidecar**. §16's table is
  two conditions — `tracked_tree_oid == base_commit^{tree}` **and** no sidecars — and a naive
  implementation reads them as one, reporting a merge-ready branch for a delivery that is
  silently incomplete in exactly the way §16's own "merging the branch alone does not install
  out-of-band artifacts" sentence exists to warn about. It is in the test set, and the mutation
  in Step 9 removes the second condition specifically.
- `out_of_band`: a sidecar whose payload is empty (`b""`). Its sha256 is the sha256 of nothing,
  which is a real, stable, valid-looking hash — and a consumer diffing hashes cannot tell it
  from a file that was never read. The record carries `size` beside `sha256` for exactly that.
- `transport_seat`: a seat branch that does not exist in the clone. `git fetch` with an explicit
  refspec naming a missing source exits non-zero, so `check=True` raises — but a caller that
  passed `check=False` would get rc≠0 and an empty stdout and could report "nothing to
  transport". The function never passes `check=False`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_forge_handover.py`:

```python
"""§16 — handover. Every git call in this module runs against the USER's own repository."""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "lib"))

from forge import bundle, gitcmd, handover, inspect as finspect, runstate, verify  # noqa: E402


def _repo(tmp_path) -> Path:
    r = tmp_path / "user-repo"
    r.mkdir()
    for argv in (["init", "-q", "-b", "main"], ["config", "user.email", "u@e"],
                 ["config", "user.name", "U"]):
        subprocess.run(["git", "-C", str(r), *argv], check=True, capture_output=True)
    (r / "f.txt").write_text("hi\n")
    subprocess.run(["git", "-C", str(r), "add", "f.txt"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(r), "commit", "-qm", "base"], check=True,
                   capture_output=True)
    return r


def _rev(r: Path, spec: str) -> str:
    return subprocess.run(["git", "-C", str(r), "rev-parse", spec], check=True,
                          capture_output=True, text=True).stdout.strip()


def test_the_synthesis_worktree_is_created_on_a_branch_and_never_detached(tmp_path):
    """§16: 'never --detach: a detached HEAD leaves commits unreachable and the next git gc
    deletes them.' Asserted twice — the tree really is on a branch, and the FORBIDDEN FLAG
    appears nowhere in the module, because a caller could otherwise add it later."""
    r = _repo(tmp_path)
    dest = tmp_path / "synth"
    name = handover.create_synthesis_worktree(r, dest, run_id="abc123", at=_rev(r, "HEAD"))
    assert name == "forge/abc123/synthesis"
    out = subprocess.run(["git", "-C", str(dest), "symbolic-ref", "HEAD"],
                         check=True, capture_output=True, text=True).stdout.strip()
    assert out == "refs/heads/forge/abc123/synthesis", out
    src = (ROOT / "shared" / "lib" / "forge" / "handover.py").read_text()
    assert "--detach" not in src


def test_the_worktree_call_carries_the_two_presets_that_were_measured_to_silence_it(tmp_path):
    """Measured (git 2.53.0): `worktree add -b` fires core.fsmonitor, post-checkout,
    post-index-change and reference-transaction; NO_DAEMON_CACHE + NO_HOOKS silence all four.
    This runs it against a repo with all of them armed and asserts nothing fired."""
    r = _repo(tmp_path)
    log = tmp_path / "fired.log"
    hooks = r / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    for h in ("post-checkout", "post-index-change", "reference-transaction"):
        (hooks / h).write_text(f'#!/bin/sh\necho {h} >> {log}\n')
        (hooks / h).chmod(0o755)
    fsm = tmp_path / "fsm.sh"
    fsm.write_text(f'#!/bin/sh\necho fsmonitor >> {log}\nexit 0\n')
    fsm.chmod(0o755)
    subprocess.run(["git", "-C", str(r), "config", "core.fsmonitor", str(fsm)],
                   check=True, capture_output=True)
    handover.create_synthesis_worktree(r, tmp_path / "synth", run_id="abc123",
                                       at=_rev(r, "HEAD"))
    assert not log.exists() or log.read_text() == "", log.read_text()


def test_seat_work_is_fetched_with_an_explicit_refspec_and_nothing_else_crosses(tmp_path):
    """§16: never a bare `git fetch <path>` — default refspecs would pull whatever refs the
    seat created into the user's repository, reintroducing the write path §4 closed."""
    r = _repo(tmp_path)
    clone = tmp_path / "seat"
    subprocess.run(["git", "clone", "-q", str(r), str(clone)], check=True, capture_output=True)
    for argv in (["config", "user.email", "s@e"], ["config", "user.name", "S"],
                 ["checkout", "-q", "-b", "forge/abc123/claude"]):
        subprocess.run(["git", "-C", str(clone), *argv], check=True, capture_output=True)
    (clone / "w.txt").write_text("work\n")
    subprocess.run(["git", "-C", str(clone), "add", "w.txt"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(clone), "commit", "-qm", "w"], check=True,
                   capture_output=True)
    # A ref the seat created that MUST NOT cross.
    subprocess.run(["git", "-C", str(clone), "update-ref", "refs/heads/sneaky", "HEAD"],
                   check=True, capture_output=True)

    oid = handover.transport_seat(r, clone, run_id="abc123", seat="claude")
    refs = subprocess.run(["git", "-C", str(r), "show-ref"], capture_output=True,
                          text=True).stdout
    assert "refs/khenrix-forge/abc123/claude" in refs
    assert "sneaky" not in refs, refs
    assert oid == _rev(clone, "HEAD")


def test_a_seat_branch_that_is_not_there_refuses_rather_than_reporting_nothing(tmp_path):
    r = _repo(tmp_path)
    clone = tmp_path / "seat"
    subprocess.run(["git", "clone", "-q", str(r), str(clone)], check=True, capture_output=True)
    with pytest.raises(gitcmd.GitError):
        handover.transport_seat(r, clone, run_id="abc123", seat="claude")


def _manifest(repo, base_oid, tree_oid):
    """A Manifest carrying only the fields mergeability reads, built through the REAL
    constructor so a field rename fails here rather than silently reading None."""
    return runstate.Manifest(
        run_id="abc123", repo_path=str(repo), base_commit=base_oid,
        baseline_ref="refs/khenrix-forge/abc123/base", baseline_commit=base_oid,
        tracked_tree_oid=tree_oid, selected_paths=(),
        generator_contract=finspect.GeneratorContract(id="", relations=()),
        setup=(), verify=(verify.Step(argv=("true",), cwd="", env={}, timeout=600),),
        protected_refs={}, forge_refs={}, status_digest="d", index_digest="i",
        created_at="2026-08-03T00:00:00Z", seats=3, attempts=3, review_rounds=2,
        synthesis_fix_cap=3)


# A synthesis tree DISTINCT from B1's own. `mergeability` reads the two against each other —
# an equal pair is a worktree nobody fused into — so every fixture below that means "the
# orchestrator did fuse something" has to say so with a different oid.
_FUSED = "f" * 40


def test_mergeability_separates_a_clean_baseline_from_a_dirty_one(tmp_path):
    r = _repo(tmp_path)
    base = _rev(r, "HEAD")
    tree = _rev(r, f"{base}^{{tree}}")
    ready = handover.mergeability(_manifest(r, base, tree), synthesis_tree_oid=_FUSED,
                                  sidecars=())
    assert ready.kind == handover.MERGE_READY

    out = handover.mergeability(_manifest(r, base, "0" * len(tree)),
                                synthesis_tree_oid=_FUSED, sidecars=())
    assert out.kind == handover.PATCH_ONLY
    assert "baseline" in out.why
    assert out.integration, "a patch handover with no integration command is a dead end"


def test_a_tree_nobody_measured_is_not_a_tree_that_matched(tmp_path):
    """`synthesis_tree_oid=None` compares unequal to every real oid, so a naive `!=` would
    answer PATCH_ONLY and read as a measured mismatch. Unmeasured is its own refusal."""
    r = _repo(tmp_path)
    base = _rev(r, "HEAD")
    with pytest.raises(handover.HandoverError) as e:
        handover.mergeability(_manifest(r, base, "a" * 40), synthesis_tree_oid=None,
                              sidecars=())
    assert "not measured" in str(e.value)


def test_a_synthesis_worktree_nobody_fused_into_is_not_a_merge_ready_delivery(tmp_path):
    """THE PARAMETER IS READ, and this is the input that proves it. A run whose orchestrator
    fused nothing leaves the synthesis worktree at B1: its tree IS `tracked_tree_oid`, there
    are no sidecars, and a `mergeability` that only liveness-checked the oid would report
    'the branch merges as it stands' over an empty delivery — nothing and nobody, one record,
    in the artifact this whole task exists to produce."""
    r = _repo(tmp_path)
    base = _rev(r, "HEAD")
    tree = _rev(r, f"{base}^{{tree}}")
    with pytest.raises(handover.HandoverError) as e:
        handover.mergeability(_manifest(r, base, tree), synthesis_tree_oid=tree, sidecars=())
    assert "nothing was fused" in str(e.value)
    # And it is refused on a DIRTY baseline too: the comparison is against B1's tracked tree,
    # not against `base^{tree}`, so it does not accidentally depend on the baseline being clean.
    with pytest.raises(handover.HandoverError):
        handover.mergeability(_manifest(r, base, "0" * len(tree)),
                              synthesis_tree_oid="0" * len(tree), sidecars=())


def test_one_changed_sidecar_keeps_a_matching_tree_out_of_merge_ready(tmp_path):
    """§16's table: merge-ready needs `tracked_tree_oid == base^{tree}` AND NO SIDECARS. A
    tree that matches while carrying an out-of-band artifact is the input that makes those two
    conditions look like one."""
    r = _repo(tmp_path)
    base = _rev(r, "HEAD")
    tree = _rev(r, f"{base}^{{tree}}")
    side = (bundle.SidecarEntry(path="dist/app.js", kind="file", mode=0o644, payload=b"x"),)
    out = handover.mergeability(_manifest(r, base, tree), synthesis_tree_oid=_FUSED,
                                sidecars=side)
    assert out.kind == handover.PATCH_ONLY
    assert "dist/app.js" in out.why


def test_out_of_band_never_force_adds_and_carries_a_size_beside_every_hash(tmp_path):
    """An empty payload hashes to a real, stable, valid-looking digest. `size` beside it is
    what keeps 'a file with no bytes' and 'a file nobody read' from comparing equal."""
    side = (bundle.SidecarEntry(path="dist/empty.js", kind="file", mode=0o644, payload=b""),
            bundle.SidecarEntry(path="dist/app.js", kind="file", mode=0o644, payload=b"abc"))
    out = handover.out_of_band(side, synthesis_path=tmp_path / "synth",
                               run_dir=tmp_path / "run")
    assert [o.size for o in out] == [3, 0]        # sorted by path: app.js, empty.js
    assert len({o.sha256 for o in out}) == 2
    for o in out:
        assert o.copy_command and o.copy_command[0] == "cp"
        assert "-f" not in o.copy_command and "--force" not in o.copy_command
    src = (ROOT / "shared" / "lib" / "forge" / "handover.py").read_text()
    assert '"-f"' not in src and '"--force"' not in src


def test_an_unenumerated_out_of_band_set_is_not_an_empty_one(tmp_path):
    with pytest.raises(handover.HandoverError):
        handover.out_of_band(None, synthesis_path=tmp_path, run_dir=tmp_path)
    assert handover.out_of_band((), synthesis_path=tmp_path, run_dir=tmp_path) == ()


def test_a_handover_naming_neither_a_target_nor_an_acceptance_is_refused():
    """§15 makes 'unmerged' well-defined off this record, and a record carrying neither field
    makes it undefined again — with `--gc` then holding a synthesis tree it may never delete."""
    with pytest.raises(handover.HandoverError):
        handover.Handover(run_id="r", branch="forge/r/synthesis", kind=handover.PATCH_ONLY,
                          handover_target=None, accepted=False, out_of_band=(),
                          baseline_owned=(), b1_files=(), why="x")
    ok = handover.Handover(run_id="r", branch="forge/r/synthesis", kind=handover.PATCH_ONLY,
                           handover_target=None, accepted=True, out_of_band=(),
                           baseline_owned=(), b1_files=(), why="user accepted the patch")
    assert ok.accepted is True


def test_a_handover_record_round_trips_and_a_damaged_one_refuses(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    assert handover.read_handover(run_dir) is None
    h = handover.Handover(run_id="r", branch="forge/r/synthesis", kind=handover.MERGE_READY,
                          handover_target="refs/heads/main", accepted=False,
                          out_of_band=(handover.OutOfBand("d/a.js", "0" * 64, 3,
                                                          ("cp", "-p", "/x/d/a.js", "d/a.js")),),
                          baseline_owned=("scratch/notes.md",), b1_files=("f.txt",),
                          why="clean baseline")
    handover.write_handover(run_dir, h)
    assert handover.read_handover(run_dir) == h
    from forge import storage as st
    st.handover_path(run_dir).write_bytes(b"{ not json")
    with pytest.raises(handover.HandoverError):
        handover.read_handover(run_dir)
```

**Before running these, read `shared/lib/forge/inspect.py:443-470` and
`shared/lib/forge/verify.py:216-311` and construct `GeneratorContract` and `Step` with the
fields they actually declare.** These two constructor calls are the plan's own draft code, and
the standing brief says the plan's draft code has been wrong in every task of every plan.
Verify; do not copy.

- [ ] **Step 2: Run to verify they fail**

```
cd /home/khenrix/git/khenrix-utils
uvx --with pytest pytest -q tests/test_forge_handover.py
```

Expected: FAIL at collection — `ImportError: cannot import name 'handover'`.

- [ ] **Step 3: Add the git-closure entries with their measurements — in ONE edit with Step 4**

**These two steps are a single change and neither may be run against the suite alone.**
`test_every_verb_the_allow_lists_clear_is_one_this_package_calls` asserts `allowed <= verbs`
over every verb this package actually calls, so a closure entry whose call site does not exist
yet fails the seams suite for the whole window between the steps. Make both edits, then run.

In `tests/test_forge_seams.py`, extend `_DIFF_DRIVER_SAFE` and append this measurement to the
comment block above it:

```python
# `worktree` and `fetch` were measured ONTO this list rather than exempted, which is the
# stronger of the two routes this comment offers. Measured on git 2.53.0 with `diff.external`,
# `diff.<d>.command` and `diff.<d>.textconv` all planted and an in-tree `.gitattributes`
# selecting the driver: `worktree add -b` and `fetch <path> <refspec>` fired NONE of the three,
# while the control `git diff` fired `diff.external`. They are absent from `_INDEX_SAFE` and
# `_HOOK_SAFE` on the opposite measurement — `worktree add` fires core.fsmonitor plus
# post-checkout, post-index-change and reference-transaction, `worktree remove` fires
# core.fsmonitor and post-index-change, and `fetch` fires core.fsmonitor and
# reference-transaction — so every call to either carries NO_DAEMON_CACHE and NO_HOOKS, which
# were measured necessary and sufficient for all of them. `--no-ext-diff` could not have been
# used in any case: it is rc=129 for both verbs in every argv position.
_DIFF_DRIVER_SAFE = frozenset({
    "rev-parse", "show-ref", "symbolic-ref",
    "config", "check-ref-format", "check-attr", "ls-files", "status",
    "add", "write-tree", "commit-tree", "update-ref", "checkout", "clone", "remote",
    "apply",              # both forms: `--numstat -z` and `--index --binary`
    "worktree",           # add -b / list --porcelain / remove / unlock — see above
    "fetch",              # `<path> +refs/heads/<b>:refs/khenrix-forge/<r>/<s>` — see above
})
```

**Do not add either verb to `_INDEX_SAFE` or `_HOOK_SAFE`.** They are measured to fire, so
those two closures must keep asking for the presets at every call site.

- [ ] **Step 4: Write `handover.py` — the git half**

Create `shared/lib/forge/handover.py`:

```python
"""§16 — handover: what leaves this engine and how the user takes delivery of it.

THE TREES THIS MODULE TOUCHES ARE THE USER'S OWN, which is the difference between it and every
other module here. `fleet`, `verify` and `runner` run git in clones this engine created; every
call below runs in the repository the operator opened the run against, or in a worktree sharing
its object store. So each one carries `NO_DAEMON_CACHE` and `NO_HOOKS`, on the measurement
recorded in `tests/test_forge_seams.py`, and `NO_DIFF_DRIVERS` is not spelled anywhere:
`--no-ext-diff` is rc=129 for both verbs in every argv position, and both were measured to run
no diff driver.

THE SYNTHESIS TREE MAY SHARE THE USER'S `.git` WHERE A SEAT CLONE MUST NOT, and §16 states the
reason as a property of who is writing rather than of what is written: §4's hazard analysis is
about unattended full-permission agents, and the synthesis author is the trusted invoking
orchestrator under its normal approval boundary. It is not a fourth seat.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields
from pathlib import Path

from . import gitcmd, storage

SYNTHESIS = "synthesis"

MERGE_READY = "merge-ready-branch"
PATCH_ONLY = "patch"
KINDS = (MERGE_READY, PATCH_ONLY)


class HandoverError(RuntimeError):
    """A delivery this module will not describe on the evidence it was given."""


def branch(run_id: str, name: str) -> str:
    """`forge/<run-id>/<name>` — one spelling, so `fleet`'s seat branch, the transport refspec
    and `--gc`'s walk cannot drift about where a seat's work lives."""
    if not run_id or not name:
        raise HandoverError(f"a branch needs a run id and a name, not {run_id!r}/{name!r}")
    return f"forge/{run_id}/{name}"


def create_synthesis_worktree(repo, dest, *, run_id: str, at: str) -> str:
    """§16's git-deliverable surface: a worktree on a NEW BRANCH at `at`. Returns the branch.

    `-b`, NEVER THE DETACH FLAG, and §16 gives the reason in one sentence: a detached HEAD
    leaves the commits unreachable and the next `git gc` deletes the deliverable. A test
    asserts the flag's spelling appears nowhere in this file, because the argument is about a
    caller who adds it later and not about this line today — which is also why the flag is
    named in words here rather than written out. A docstring that spelled it would fail that
    test, and a test weakened to let the docstring through would stop catching the call site
    it exists for.

    THE TWO PRESETS ARE NOT DECORATION. Measured on git 2.53.0 against a repository with all
    hooks planted and `core.fsmonitor` armed, `worktree add -b` fires the monitor plus
    `post-checkout`, `post-index-change` and `reference-transaction`; `NO_DAEMON_CACHE` and
    `NO_HOOKS` together silence all four. Both are the user's own programs, and §5 step 1
    admits no repository-supplied code the operator did not authorize.
    """
    b = branch(run_id, SYNTHESIS)
    gitcmd.git(repo, *gitcmd.NO_DAEMON_CACHE, *gitcmd.NO_HOOKS,
               "worktree", "add", "-b", b, str(dest), at,
               env_extra=gitcmd.READONLY, timeout=300)
    return b


def transport_seat(repo, clone_path, *, run_id: str, seat: str) -> str:
    """Bring one seat's committed work out of its remote-less clone. Returns the fetched OID.

    THE REFSPEC IS ALWAYS EXPLICIT AND THE DIRECTION IS ALWAYS OUT OF THE CLONE. §16 spells
    the whole call, and both halves are load-bearing. A bare `git fetch <path>` applies git's
    DEFAULT refspecs, which pull whatever refs the seat created into the user's repository —
    the write path §4 closed at the front, reopened at the back. And the fetch runs from the
    USER's side rather than as a push from the seat's, so nothing inside a bypass-permissions
    clone ever names a destination in the operator's repository.

    `check=True` IS THE DEFAULT AND IS NOT OVERRIDDEN. A missing seat branch makes `fetch`
    exit non-zero; with `check=False` the caller would get rc≠0, empty stdout, and no way to
    tell "the seat produced nothing" from "the fetch failed" — the same collapse the rest of
    this package refuses.
    """
    src = branch(run_id, seat)
    dst = f"refs/khenrix-forge/{run_id}/{seat}"
    gitcmd.git(repo, *gitcmd.NO_DAEMON_CACHE, *gitcmd.NO_HOOKS,
               "fetch", str(clone_path), f"+refs/heads/{src}:{dst}",
               env_extra=gitcmd.READONLY, timeout=300)
    oid = gitcmd.git(repo, "rev-parse", dst, env_extra=gitcmd.READONLY).stdout.strip()
    if not oid:
        raise HandoverError(
            f"{dst} was fetched and names no object. The fetch reported success, so this is "
            "the state where the record would say a seat's work crossed and nothing did.")
    return oid
```

- [ ] **Step 5: Write `handover.py` — the mergeability and out-of-band half**

Append to `shared/lib/forge/handover.py`:

```python
@dataclass(frozen=True)
class Mergeability:
    """§16's table as a value, with the reason attached rather than left re-derivable.

    `integration` is the argv sequence the user runs, as a tuple of tokens and never a shell
    string: this package's own rule, and a handover line a user pastes is exactly where a
    string would acquire a `&&`.

    A `MERGE_READY` carrying an `integration` is not a contradiction — the merge command IS
    the integration — but either kind carrying an EMPTY one leaves the user holding a
    deliverable with no next step, so `__post_init__` refuses it.
    """
    kind: str
    why: str
    integration: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise HandoverError(f"kind is one of {list(KINDS)}, not {self.kind!r}")
        if not self.why.strip():
            raise HandoverError("a mergeability decision states the condition it read")
        if not self.integration:
            raise HandoverError(
                f"a {self.kind!r} handover with no integration command tells the user what "
                "they have and not what to do with it")


def mergeability(manifest, *, synthesis_tree_oid, sidecars) -> Mergeability:
    """§16's table: which of the two deliverable shapes this run can offer.

    THREE READINGS, AND `synthesis_tree_oid` IS READ BY ONE OF THEM RATHER THAN ONLY
    LIVENESS-CHECKED. In order:

    1. THE SYNTHESIS TREE AGAINST B1's TRACKED TREE. `cli.start` creates the worktree at
       `manifest.baseline_commit`, whose tree is `manifest.tracked_tree_oid`, so a synthesis
       oid EQUAL to it is a worktree nobody fused into. Without this reading the parameter is
       validated and then discarded, and a run in which the orchestrator produced nothing
       reports "the branch merges as it stands" over an empty delivery — *does nothing leave
       the same record as nobody?* answered wrong in the flagship artifact. It raises, because
       there is no delivery to describe and a third `kind` would put an empty one into
       `Handover` and thence into `--gc`'s licence to delete.
    2. TWO CONDITIONS FOR MERGE-READY, NOT ONE. `tracked_tree_oid == base_commit^{tree}` AND
       no sidecars. A tree that matches while carrying an out-of-band artifact is merge-ready
       for its tracked half and silently incomplete for the other, which is the reading the
       second condition exists to forbid — and §16 closes the same hole in prose one paragraph
       on ("merging the branch alone does not install out-of-band artifacts").
    3. Everything else is `PATCH_ONLY`, with every reason it read attached.

    A TREE NOBODY MEASURED IS NOT A TREE THAT DID NOT MATCH. `synthesis_tree_oid=None` compares
    unequal to every real oid, so reading (1) with a plain `!=` would call an unmeasured tree
    a fused one and reading (2) would then answer over evidence nobody gathered. It raises
    first, before either comparison.

    `sidecars` IS THE CANDIDATE'S OUT-OF-BAND SET AND `None` IS NOT `()`. An empty tuple is
    "this candidate produced no ignored artifacts"; `None` is "nobody enumerated them", and
    the second may not be read as the first.
    """
    if synthesis_tree_oid is None:
        raise HandoverError(
            "the synthesis tree's oid was not measured, so §16's conditions cannot be read. "
            "An unmeasured tree compares unequal to every oid, which would report a fused "
            "delivery about a tree nobody looked at.")
    if sidecars is None:
        raise HandoverError(
            "the out-of-band set was not enumerated. `()` is a candidate that produced no "
            "ignored artifacts; `None` is nobody having looked, and §16's second condition "
            "cannot be read off it.")
    if synthesis_tree_oid == manifest.tracked_tree_oid:
        raise HandoverError(
            f"the synthesis worktree's tree is {synthesis_tree_oid}, which is B1's own tracked "
            "tree: nothing was fused into it. There is no delivery to describe, and describing "
            "one would report a merge-ready branch over an empty worktree — a run in which "
            "NOTHING happened leaving the same record as one in which NOBODY did. Fuse in the "
            "synthesis worktree and commit there, then re-run `--collect`.")
    base_tree = gitcmd.git(manifest.repo_path, "rev-parse",
                           f"{manifest.base_commit}^{{tree}}",
                           env_extra=gitcmd.READONLY).stdout.strip()
    b = branch(manifest.run_id, SYNTHESIS)
    if manifest.tracked_tree_oid == base_tree and not sidecars:
        return Mergeability(
            MERGE_READY,
            f"the run's baseline tracked tree is exactly {manifest.base_commit}^{{tree}} and "
            "this candidate carries no out-of-band artifacts, so the branch merges as it "
            "stands",
            ("git", "merge", "--no-ff", b))
    reasons = []
    if manifest.tracked_tree_oid != base_tree:
        reasons.append(
            f"the baseline was dirty: B1's tracked tree {manifest.tracked_tree_oid} is not "
            f"{manifest.base_commit}^{{tree}} ({base_tree}), so a merge would carry the "
            "user's uncommitted work as though forge had authored it")
    if sidecars:
        named = ", ".join(sorted(s.path for s in sidecars)[:5])
        reasons.append(
            f"{len(sidecars)} out-of-band artifact(s) are not in the tracked tree at all "
            f"({named}); merging the branch would not install them")
    return Mergeability(
        PATCH_ONLY, "; ".join(reasons),
        ("git", "apply", "--binary", "--index", "<run-dir>/handover/B-to-S.patch"))


@dataclass(frozen=True)
class OutOfBand:
    """One ignored artifact, retained rather than committed.

    `size` SITS BESIDE `sha256` BECAUSE AN EMPTY FILE HASHES TO SOMETHING. `sha256(b"")` is a
    real, stable, valid-looking digest, so a consumer comparing hashes cannot tell a file with
    no bytes from a file nobody read. The pair can.

    `copy_command` is argv tokens. §16 asks for "an explicit copy command"; a string would be
    the one line in this package a user is invited to paste into a shell.
    """
    path: str
    sha256: str
    size: int
    copy_command: tuple[str, ...]


def out_of_band(sidecars, *, synthesis_path, run_dir) -> tuple[OutOfBand, ...]:
    """§16's out-of-band deliverables, enumerated with hashes and a copy command each.

    NEVER FORCE-ADDED. §16: "that violates the originating skill's contract and would put
    `node_modules` in the object store forever." Nothing here stages anything; the artifacts
    stay in the synthesis tree and the run directory, and the user copies them. `-f` and
    `--force` appear nowhere in this module and a test reads the source to say so.

    `None` RAISES AND `()` DOES NOT. See `mergeability` for the same distinction one condition
    over: an empty enumeration and an absent one are the states §16's second table row turns
    on.
    """
    if sidecars is None:
        raise HandoverError(
            "the out-of-band set was not enumerated; `()` is what a candidate with no ignored "
            "artifacts carries, and reporting that here for a set nobody assembled would tell "
            "the user there is nothing to copy")
    out = []
    for s in sidecars:
        payload = s.payload if isinstance(s.payload, bytes) else str(s.payload).encode()
        out.append(OutOfBand(
            path=s.path,
            sha256=hashlib.sha256(payload).hexdigest(),
            size=len(payload),
            copy_command=("cp", "-p", str(Path(synthesis_path) / s.path), s.path)))
    return tuple(sorted(out, key=lambda o: o.path))
```

- [ ] **Step 6: Write the `Handover` record and its storage**

Append to `shared/lib/forge/handover.py`:

```python
@dataclass(frozen=True)
class Handover:
    """What was delivered, recorded so `--gc` can say what "unmerged" means.

    `handover_target` IS §15's WHOLE POINT. "It refuses to delete a synthesis worktree/branch
    not marked handed over, and `handover_target` (or explicit user acceptance) is recorded so
    'unmerged' is well-defined — a patch-based handover may intentionally never merge the
    internal branch." So a run whose delivery was a PATCH is handed over even though its
    branch will never appear in the user's history, and `--gc` must not read the absent merge
    as unfinished work.

    `accepted` is the explicit-user-acceptance half. Both fields exist because §15 names both
    and they answer different questions: `handover_target` is where the work went, `accepted`
    is whether the user said so. `__post_init__` refuses a record carrying NEITHER, because
    that is the state in which "unmerged" is undefined again.

    `baseline_owned` is §16's other sentence: unchanged selected untracked/ignored files are
    the BASELINE's, and only their B→S changes are forge-authored. Carried as the explicit
    path list rather than derived at render time, so the handover text and this record cannot
    disagree about which files the run claims authorship of.

    `b1_files` is §16's "the B1 file list is enumerated in the handover text, not only at a
    confirmation gate an hour earlier".
    """
    run_id: str
    branch: str
    kind: str
    handover_target: str | None
    accepted: bool
    out_of_band: tuple
    baseline_owned: tuple[str, ...]
    b1_files: tuple[str, ...]
    why: str

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise HandoverError(f"kind is one of {list(KINDS)}, not {self.kind!r}")
        if self.handover_target is None and not self.accepted:
            raise HandoverError(
                "a handover names where the work went or records that the user accepted it "
                "without one. Neither is `--gc`'s licence to delete a synthesis tree, and a "
                "record carrying neither makes 'unmerged' undefined — which is exactly what "
                "§15 says this field exists to prevent.")


def _row(h: Handover) -> dict:
    row = {f.name: getattr(h, f.name) for f in fields(Handover)}
    row["out_of_band"] = [{"path": o.path, "sha256": o.sha256, "size": o.size,
                           "copy_command": list(o.copy_command)} for o in h.out_of_band]
    row["baseline_owned"] = list(h.baseline_owned)
    row["b1_files"] = list(h.b1_files)
    return row


def write_handover(run_dir, h: Handover) -> None:
    """Persist the delivery. `atomic_write`, not `exclusive_write`: an acceptance recorded
    later rewrites this record, and the manifest is the run's write-once identity, not this.

    THE ROUND TRIP IS CHECKED BEFORE THE WRITE, on `taskbundle.write_task_bundle`'s precedent
    and for its reason: JSON has one sequence type, so a tuple of `OutOfBand` written naively
    reads back as a list of dicts and compares unequal in silence.
    """
    if not isinstance(h, Handover):
        raise HandoverError(f"a Handover is required, not {type(h).__name__}")
    blob = json.dumps(_row(h), sort_keys=True, indent=2).encode("utf-8") + b"\n"
    restored = _decode(json.loads(blob))
    if restored != h:
        differing = [f.name for f in fields(Handover)
                     if getattr(restored, f.name) != getattr(h, f.name)]
        raise HandoverError(
            f"this handover does not survive its own round trip; {differing} come back as a "
            "different type")
    storage.atomic_write(storage.handover_path(run_dir), blob)


def _decode(row) -> Handover:
    if not isinstance(row, dict):
        raise HandoverError(f"a handover record is an object, not a {type(row).__name__}")
    try:
        oob = tuple(OutOfBand(path=r["path"], sha256=r["sha256"], size=r["size"],
                              copy_command=tuple(r["copy_command"]))
                    for r in row["out_of_band"])
        return Handover(run_id=row["run_id"], branch=row["branch"], kind=row["kind"],
                        handover_target=row["handover_target"], accepted=row["accepted"],
                        out_of_band=oob,
                        baseline_owned=tuple(row["baseline_owned"]),
                        b1_files=tuple(row["b1_files"]), why=row["why"])
    except (KeyError, TypeError) as e:
        raise HandoverError(
            f"this is not a handover record this engine can read ({e}); a partial read would "
            "hand `--gc` a decision about a tree it cannot describe") from e


def read_handover(run_dir):
    """The delivery record, or `None` because this run has not been handed over.

    `None` FOR THAT AND FOR NOTHING ELSE, on `taskbundle.read_task_bundle_if_recorded`'s rule.
    `--gc` reads this to decide whether it may delete a synthesis worktree. Folding a corrupt
    record into `None` would look exactly like a run that was never delivered, whose tree
    `--gc` then refuses to touch — fail-closed for safety, and silent for the operator, who
    would find a run they did hand over become undeletable with no message saying why.
    """
    path = storage.handover_path(run_dir)
    if not path.exists():
        return None
    try:
        row = json.loads(path.read_bytes())
    except ValueError as e:
        raise HandoverError(f"{path} is not readable as JSON: {e}") from e
    return _decode(row)
```

Add to `shared/lib/forge/storage.py`, beside the other run-directory names:

```python
def handover_path(run_dir) -> Path:
    return Path(run_dir) / "handover.json"
```

- [ ] **Step 7: Run the tests**

```
cd /home/khenrix/git/khenrix-utils
uvx --with pytest pytest -q tests/test_forge_handover.py
uvx --with pytest pytest -q tests/test_forge_seams.py
```

Expected: both PASS. The seams suite is what fails if `worktree`/`fetch` went onto the wrong
list or if either call is missing a preset.

- [ ] **Step 8: Add the branch-name seam test**

Append to `tests/test_forge_seams.py`:

```python
def test_the_branch_name_has_one_spelling():
    """`fleet.clone_seat` computes `forge/{run_id}/{name}` for a seat and `handover.branch`
    computes it for the transport and the synthesis tree. Two spellings of one predicate will
    eventually disagree, and this one decides whether a seat's work can be found at all."""
    from forge import handover
    assert handover.branch("r1", "claude") == "forge/r1/claude"
    assert handover.branch("r1", handover.SYNTHESIS) == "forge/r1/synthesis"
    src = (ROOT / "shared" / "lib" / "forge" / "fleet.py").read_text()
    assert 'f"forge/{run_id}/{name}"' in src or "handover.branch(" in src, (
        "fleet no longer spells the seat branch the way this test reads it — re-derive the "
        "pair rather than widening the pattern")
```

**Read `shared/lib/forge/fleet.py:141` first and make the literal match what is actually
there.** If `fleet` spells it differently, the right fix is to have `fleet` import
`handover.branch` — measure for an import cycle first (`handover` imports `gitcmd` and
`storage`; `fleet` imports `gitcmd`, `bundle` and `snapshot`, so it should be clean), and if it
is, do it in this commit and delete the source-reading half of this test.

- [ ] **Step 9: Mutation-test the fail-closed branches**

```
cd /home/khenrix/git/khenrix-utils
git status --short
python3 scripts/mutate.py --file shared/lib/forge/handover.py \
  --old '    if synthesis_tree_oid is None:' --new '    if False:' \
  --test 'uvx --with pytest pytest -q tests/test_forge_handover.py'
python3 scripts/mutate.py --file shared/lib/forge/handover.py \
  --old '    if synthesis_tree_oid == manifest.tracked_tree_oid:' --new '    if False:' \
  --test 'uvx --with pytest pytest -q tests/test_forge_handover.py'
python3 scripts/mutate.py --file shared/lib/forge/handover.py \
  --old '    if sidecars is None:' --new '    if False:' \
  --test 'uvx --with pytest pytest -q tests/test_forge_handover.py'
python3 scripts/mutate.py --file shared/lib/forge/handover.py \
  --old '    if manifest.tracked_tree_oid == base_tree and not sidecars:' \
  --new '    if manifest.tracked_tree_oid == base_tree:' \
  --test 'uvx --with pytest pytest -q tests/test_forge_handover.py'
python3 scripts/mutate.py --file shared/lib/forge/handover.py \
  --old '        if self.handover_target is None and not self.accepted:' \
  --new '        if False:' \
  --test 'uvx --with pytest pytest -q tests/test_forge_handover.py'
git status --short
```

Expected: all five `CAUGHT`. If the sidecar-condition one SURVIVES, the "one changed sidecar"
test is not reaching it — that is the two-conditions-look-like-one input and it must be closed
here, not noted. If the `tracked_tree_oid` one SURVIVES, the parameter is back to being
liveness-checked and discarded, and an empty synthesis worktree is reporting a merge-ready
branch again.

- [ ] **Step 10: Whole suite, render, gate, commit**

```
cd /home/khenrix/git/khenrix-utils
uvx --with pytest pytest -q tests/
make render
make verify
echo "verify rc=$?"
make precommit
echo "precommit rc=$?"
git add shared/lib/forge/handover.py shared/lib/forge/storage.py \
        tests/test_forge_handover.py tests/test_forge_seams.py marketplaces
git commit -m "$(cat <<'EOF'
feat(forge): §16 — a branch the next git gc cannot delete, and a fetch that carries one ref

create_synthesis_worktree uses `worktree add -b` and the module contains no `--detach`: a
detached HEAD leaves the deliverable unreachable. transport_seat always names an explicit
refspec, because a bare `git fetch <path>` applies default refspecs and pulls whatever refs
a bypass-permissions seat created into the user's repository.

Both verbs were refused by all three gitcmd closures. Measured (git 2.53.0): `worktree add
-b` fires core.fsmonitor plus post-checkout, post-index-change and reference-transaction;
`fetch` fires core.fsmonitor and reference-transaction; NO_DAEMON_CACHE + NO_HOOKS silence
every one. Neither runs a diff driver (control: `git diff` fires diff.external), so both are
MEASURED onto _DIFF_DRIVER_SAFE rather than exempted — `--no-ext-diff` is rc=129 for both in
every argv position and could not have been used.

mergeability reads §16's table as TWO conditions. A tree nobody measured raises rather than
comparing unequal to every oid and reporting a dirty baseline about a tree nobody looked at.
out_of_band carries a size beside every sha256, because sha256(b"") is a real digest and a
file with no bytes must not compare equal to a file nobody read.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UiV66Pt8cZVMq9t8WEAhpN
EOF
)"
```

Expected: `verify rc=0`, `precommit rc=0`.

---

## Task 5: §16.1 — the provenance header, and two headline judgements that usually say "I can't tell"

**The brief's hardest requirement lands here.** `fingerprint.agreement_label` cannot use
`bundle_sha256` (fixed in Task 3, so it now can) and `rubric.strongest` "will rarely name a
seat", because `coverage._schema` is `unresolved` **by construction** and `coverage._prose` is
`unresolved` for any criterion with no recorded trace. Both mechanisms are correct and fail
closed. **This task presents `(None, why)` as the ORDINARY outcome** — a named, rendered line
with the reason in it — rather than as a missing line a reader interprets as absence of
finding.

A fusion tool whose two headline judgements usually answer "I can't tell" is a design problem,
not a bug; the design answer is that the header **says so out loud, every time, in the same
place**, so the operator learns what the tool did and did not measure rather than inferring it
from a blank.

**Files:**
- Modify: `shared/lib/forge/handover.py` (the `Provenance` record and `header`)
- Test: `tests/test_forge_handover.py`

**Interfaces:**
- Consumes: `handover.Handover`, `handover.MERGE_READY`/`PATCH_ONLY`;
  `seat.Status` fields `process`, `artifacts`, `proven_read`, `forge`, `setup`, `verify`;
  `seat` forge values `("completed", "partial", "no_change", "failed")`;
  `verify.OUTCOMES` and `verify.PASS`; `review.TERMINALS` = `("ready", "degraded",
  "review_blocked")`; `ultra.Ultra(status, reason, bugs, session_url, diff_measured, detail)`
  and `ultra.RAN`/`UNAVAILABLE`/`TIMED_OUT`/`SKIPPED`; `rubric.strongest(dims) -> tuple`;
  `fingerprint.agreement_label(ids) -> str` and `fingerprint.LABELS`;
  `strategy.STRATEGIES`.
- Produces:
  - `handover.SeatLine(name: str, forge: str, artifacts: str, verify_outcome: str | None)`
  - `handover.Provenance(...)` frozen record, including `synthesis_measured: bool`
  - `handover.header(p: Provenance) -> str`
  - `handover.text(h: Handover, p: Provenance) -> str` — the full handover message
  - `handover._VERIFIED_MEANS` and `handover._ASSERTED_MEANS`
  - Task 6's CLI prints `handover.text(...)`; Task 11's SKILL.md quotes the "Verified means"
    sentence.

**The specific fail-opens this must not have:**
1. **A failure rendering a success header.** §18 asks this be tested by name. `Provenance`
   refuses a record whose seat lines say nothing completed while the synthesis line says
   `PASS`; and a `review_terminal` of `review_blocked` may not appear beside a header with no
   unresolved-finding line.
2. **The word "built" for a seat that produced artifacts and failed verify.** §16.1 forbids it
   outright. `header` never emits the word; a test asserts the rendered text contains no
   case-insensitive `built` for such a seat, **and** that the module source contains no
   `"built"` literal, because the first check only covers the fixtures it runs. **The
   source-level half binds the docstrings and the rendered headings too**, and Task 4's draft
   tripped it four times — "clones the engine built", "a set nobody built", "B1 … built from",
   and, most instructively, the sentence in `header`'s own docstring asserting the word appears
   nowhere. When this test fails on a docstring the assertion is right and the prose is wrong.
3. **A missing line reading as a clean answer.** `strongest` returning `(None, why)` and
   `agreement_label` returning `not-comparable` must each render a line **naming the
   judgement and its reason**. Omitting the line is the failure this task exists to prevent.
4. **"Verified" overclaiming, in two directions.** The scope map's debt 3: `verify.classify`
   never reads `baseline_run` on the `PASS` path, so nothing proves the calibration a `PASS`
   rests on came from this run's baseline. §16.1's sentence must therefore say what a PASS
   *is* — the confirmed verify command exited 0 on a fresh verifier clone at the final
   checkpoint — and nothing more. **And the sentence may not be printed beside a verdict this
   engine did not measure at all.** `--collect` takes the synthesis outcome from the
   orchestrator (see Task 6), and a header that renders an *asserted* verdict in the same
   words as a measured one, under a paragraph beginning "Verified here means", is the whole
   project's rule — a verdict must never read cleaner than its evidence — broken by the
   artifact whose job is to say what was verified. `Provenance` therefore carries
   `synthesis_measured`, the two render differently, and `_VERIFIED_MEANS` appears only beside
   the measured one.
5. **`ultrareview: unavailable` collapsing with `skipped` and `timeout`.** Four statuses, four
   renderings. §16.1's example shows only the `ran` one.

**What input would make this produce a result cleaner than its evidence:** a run in which
**every** seat failed and the synthesis was never attempted — `seats=()`, `synthesis_outcome
=None`. A renderer that skips empty sections emits a four-line header consisting entirely of
zeros and blanks, which reads as a completed, clean run. `Provenance.__post_init__` refuses an
empty seat tuple, and `header` renders `no seat produced a candidate` explicitly.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_forge_handover.py`:

```python
from forge import fingerprint, seat as seatmod, ultra  # noqa: E402


def _prov(**kw):
    base = dict(
        seats=(handover.SeatLine("claude", "completed", "usable", "PASS"),
               handover.SeatLine("codex", "completed", "usable", "FAIL"),
               handover.SeatLine("agy", "partial", "unusable", None)),
        synthesis_outcome="PASS", synthesis_measured=True,
        verify_command="make verify", verify_seconds=47.0, strategy="from_scratch",
        strongest=(None, "no strongest seat can be named while agy has no measured "
                         "requirement_coverage"),
        agreement="differently-prompted",
        review_terminal="review_blocked", review_rounds=2, unresolved_findings=1,
        ultra=ultra.Ultra(ultra.RAN, None, (), None, True, "0 finding(s)"))
    base.update(kw)
    return handover.Provenance(**base)


def test_the_header_is_four_lines_and_names_every_number_it_reports():
    text = handover.header(_prov())
    lines = [l for l in text.splitlines() if l.strip()]
    assert len(lines) >= 4
    assert lines[0].startswith("**Forge:")
    assert "2 of 3 seats completed" in lines[0]
    assert "2 artifact set(s) usable" in lines[0]     # the renderer says `set(s)`; match it
    assert "1 of 3 passed verify" in lines[0]
    assert text.rstrip().endswith("**")


def test_a_seat_that_produced_artifacts_and_failed_verify_is_never_called_built():
    """§16.1 forbids the word outright. Checked on the rendered text AND on the source, because
    the first only covers the fixtures this test runs."""
    text = handover.header(_prov())
    assert "built" not in text.lower()
    src = (ROOT / "shared" / "lib" / "forge" / "handover.py").read_text()
    assert "built" not in src.lower().replace("rebuilt", "")


def test_a_run_in_which_nothing_completed_cannot_render_a_success_header():
    """§18 asks for this by name: 'failure cannot render a success header'."""
    dead = _prov(seats=(handover.SeatLine("claude", "failed", "unusable", None),
                        handover.SeatLine("codex", "failed", "unusable", None),
                        handover.SeatLine("agy", "failed", "unusable", None)),
                 synthesis_outcome=None, synthesis_measured=False, verify_seconds=None,
                 strategy=None,
                 review_terminal=None, review_rounds=0, unresolved_findings=0,
                 ultra=ultra.Ultra(ultra.SKIPPED, None, None, None, False, "no synthesis"))
    text = handover.header(dead)
    assert "0 of 3 seats completed" in text
    assert "no seat produced a candidate" in text
    assert "PASS" not in text
    with pytest.raises(handover.HandoverError):
        _prov(seats=(handover.SeatLine("claude", "failed", "unusable", None),),
              synthesis_outcome="PASS")


def test_a_run_with_no_reported_verdict_is_not_a_run_with_no_candidate():
    """Two absences that must not compare equal. Seats completed and nobody reported an outcome
    for the fusion — `--collect` run without one — is a MISSING ARGUMENT. Rendering it as "no
    seat produced a candidate" would have the header invent a fleet failure out of it, and the
    operator would read a working three-seat run as a dead one."""
    quiet = _prov(synthesis_outcome=None, synthesis_measured=False, verify_seconds=None)
    text = handover.header(quiet)
    assert "no seat produced a candidate" not in text
    assert "no verify verdict was reported" in text
    assert "2 of 3 seats produced a candidate" in text
    assert handover._VERIFIED_MEANS not in handover.text(_a_handover(), quiet)


def test_a_run_with_no_seats_at_all_is_refused_rather_than_rendered_as_zeros():
    """An empty seat tuple renders as a four-line header of zeros and blanks, which reads as a
    completed clean run. It is the input that makes 'nothing' and 'nobody' the same record."""
    with pytest.raises(handover.HandoverError):
        _prov(seats=())


def test_a_strongest_seat_nobody_could_name_is_a_LINE_and_not_a_missing_one():
    """§12.5's `strongest` is `(None, why)` for any ledger carrying a schema or untraced-prose
    criterion, which is the ORDINARY case. A header that omits the line when there is no winner
    reads as a header that had nothing to say about strength."""
    text = handover.header(_prov())
    assert "Fusion:" in text
    assert "no strongest seat" in text
    assert "requirement_coverage" in text, "the reason is not carried into the header"
    named = handover.header(_prov(strongest=("claude", "§12.5's order over 3 seats: claude, "
                                                       "codex, agy")))
    assert "strongest seat: claude" in named


def test_the_agreement_label_is_always_rendered_including_when_it_is_not_comparable():
    for label in fingerprint.LABELS:
        text = handover.header(_prov(agreement=label))
        assert f"agreement: {label}" in text, label
    with pytest.raises(handover.HandoverError):
        _prov(agreement="strong")          # not one of §11's three


def test_every_ultrareview_status_renders_differently():
    seen = set()
    for u in (ultra.Ultra(ultra.RAN, None, (), None, True, "0 finding(s)"),
              ultra.Ultra(ultra.UNAVAILABLE, "no_auth", None, None, True, "d"),
              ultra.Ultra(ultra.TIMED_OUT, None, None, "https://claude.ai/x", True, "d"),
              ultra.Ultra(ultra.SKIPPED, None, None, None, False, "--no-ultra")):
        line = [l for l in handover.header(_prov(ultra=u)).splitlines()
                if l.lstrip().startswith("Ultrareview:")]
        assert len(line) == 1, u.status
        seen.add(line[0])
    assert len(seen) == 4, seen
    unavailable = handover.header(_prov(
        ultra=ultra.Ultra(ultra.UNAVAILABLE, "zdr_org", None, None, True, "d")))
    assert "unavailable (zdr_org)" in unavailable


def test_the_verified_sentence_says_what_a_pass_is_and_no_more():
    """Debt: `verify.classify` never reads `baseline_run` on the PASS path, so nothing proves
    the calibration a PASS rests on came from this run's baseline. The sentence must not
    overclaim past that."""
    text = handover.text(_a_handover(), _prov())
    assert ("the confirmed verify command exited 0 on a fresh verifier clone at the final "
            "checkpoint") in text
    assert "no new defects" in text and "does not mean" in text


def test_an_asserted_synthesis_verdict_never_prints_the_verified_sentence():
    """A VERDICT MUST NEVER READ CLEANER THAN ITS EVIDENCE, and this is the input that breaks
    it in the artifact whose whole job is to say what was verified. `--collect` takes the
    synthesis outcome from the orchestrator; nothing in this engine runs the confirmed command
    over the fusion. Rendering that in the same words as a measured PASS, under a paragraph
    beginning "Verified here means", asserts a verification that did not happen."""
    asserted = _prov(synthesis_measured=False, verify_seconds=None)
    text = handover.text(_a_handover(), asserted)
    assert handover._VERIFIED_MEANS not in text
    assert "reported by the orchestrator" in text
    assert "this engine did not run it" in text
    head = handover.header(asserted)
    assert "verify PASS" not in head, (
        "an asserted verdict is rendered in the words of a measured one")
    # And the measured rendering is still the measured rendering.
    assert handover._VERIFIED_MEANS in handover.text(_a_handover(), _prov())


def test_a_measured_synthesis_verdict_must_carry_the_measurement():
    """`synthesis_measured=True` is a claim that this engine ran the command, and a run it
    timed is the evidence for that claim. A measured verdict with no duration is the record
    saying it measured something it did not time — which is the same fail-open one field over,
    and it is how an asserted verdict would be re-labelled as a measured one by a caller
    passing the wrong flag."""
    with pytest.raises(handover.HandoverError):
        _prov(synthesis_measured=True, verify_seconds=None)
    with pytest.raises(handover.HandoverError):
        _prov(synthesis_measured=True, synthesis_outcome=None)


def _a_handover():
    return handover.Handover(
        run_id="abc123", branch="forge/abc123/synthesis", kind=handover.PATCH_ONLY,
        handover_target=None, accepted=True,
        out_of_band=(handover.OutOfBand("dist/app.js", "0" * 64, 3,
                                        ("cp", "-p", "/s/dist/app.js", "dist/app.js")),),
        baseline_owned=("scratch/notes.md",), b1_files=("f.txt", "scratch/notes.md"),
        why="the baseline was dirty")


def test_the_handover_text_enumerates_b1_and_says_a_merge_does_not_install_the_out_of_band_set():
    """§16: the B1 file list is enumerated in the handover TEXT, not only at a confirmation
    gate an hour earlier — and 'merging the branch alone does not install out-of-band
    artifacts' is stated plainly."""
    text = handover.text(_a_handover(), _prov())
    assert "f.txt" in text and "scratch/notes.md" in text
    assert "merging the branch alone does not install" in text.lower()
    assert "cp -p /s/dist/app.js dist/app.js" in text
    assert "baseline-owned" in text.lower()
```

- [ ] **Step 2: Run to verify they fail**

```
cd /home/khenrix/git/khenrix-utils
uvx --with pytest pytest -q tests/test_forge_handover.py -k "header or provenance or verified or b1"
```

Expected: FAIL — `handover` has no `SeatLine`, `Provenance`, `header` or `text`.

- [ ] **Step 3: Implement `SeatLine` and `Provenance`**

Append to `shared/lib/forge/handover.py`:

```python
from . import fingerprint, seat as seatmod, ultra as ultramod, verify as verifymod  # noqa: E402


@dataclass(frozen=True)
class SeatLine:
    """One seat's contribution to §16.1's first line.

    `verify_outcome` is `None` for a seat that was never verified, and that is DIFFERENT from
    every one of `verify.OUTCOMES`: §8 fixes a `failed` seat's verdict whatever §6 would find,
    and a seat whose verification was refused keeps its pre-verification verdict. Counting
    `None` as a non-PASS is correct for "how many passed"; reporting it as FAIL would be a
    verdict about a gate that never ran.
    """
    name: str
    forge: str
    artifacts: str
    verify_outcome: str | None

    def __post_init__(self) -> None:
        if self.forge not in seatmod._FORGE:
            raise HandoverError(f"forge is one of {list(seatmod._FORGE)}, not {self.forge!r}")
        if self.artifacts not in seatmod._ARTIFACTS:
            raise HandoverError(
                f"artifacts is one of {list(seatmod._ARTIFACTS)}, not {self.artifacts!r}")
        if self.verify_outcome is not None and self.verify_outcome not in verifymod.OUTCOMES:
            raise HandoverError(
                f"verify_outcome is one of {list(verifymod.OUTCOMES)} or None, not "
                f"{self.verify_outcome!r}; None is a seat §6 never verified and is not a "
                "sixth verdict")


@dataclass(frozen=True)
class Provenance:
    """Everything §16.1's header reports, validated before a line of it is rendered.

    THE VALIDATION IS THE POINT. §18 asks that "failure cannot render a success header" be
    tested, and the only way that holds for inputs no fixture reaches is for the RECORD to
    refuse the combination rather than for the renderer to remember not to print it.

    `strongest` IS §12.5's `(seat | None, why)` PAIR AND BOTH HALVES ARE REQUIRED. `None` is
    the ordinary answer, not an error: `coverage._schema` is `unresolved` by construction and
    `coverage._prose` is `unresolved` for any criterion with no recorded trace, so a real
    ledger carrying either kind makes the report incomplete and `rubric.strongest` names
    nobody. §16.1's header therefore renders the reason as a LINE. A header that dropped the
    line when there was no winner would read as a header with nothing to say about strength,
    which is the fail-open this whole record exists to close one field over.

    `agreement` is §11's label and is likewise always rendered. §11 makes agreement provenance
    and never a correctness argument, so `differently-prompted` — the ordinary answer for a
    real fleet, since three seats are three different CLIs and `cli_version` differs — is
    information, not a defect.

    `synthesis_measured` IS THE FIELD THAT KEEPS THE HEADER HONEST, and it exists because
    §16's synthesis author is the ORCHESTRATOR and not this engine. `--collect` is handed a
    verdict; it does not build a verifier clone for the fusion and does not run the confirmed
    command over it. So `synthesis_outcome` has two provenances that must never render alike:
    MEASURED by this engine, or ASSERTED by whoever fused. `header` renders them as different
    sentences and `text` prints the "Verified means" paragraph only beside the first —
    otherwise the one artifact whose job is to say what was verified would be the one asserting
    a verification that never happened.

    A MEASURED VERDICT CARRIES ITS MEASUREMENT. `synthesis_measured=True` with no outcome, or
    with no `verify_seconds`, is a record claiming a measurement it cannot show, and it is the
    exact route by which a caller passing the wrong flag would re-label an asserted verdict as
    a measured one.
    """
    seats: tuple
    synthesis_outcome: str | None
    synthesis_measured: bool
    verify_command: str
    verify_seconds: float | None
    strategy: str | None
    strongest: tuple
    agreement: str
    review_terminal: str | None
    review_rounds: int
    unresolved_findings: int
    ultra: object

    def __post_init__(self) -> None:
        if not self.seats:
            raise HandoverError(
                "a run reports the seats it ran. An empty tuple renders §16.1's first line as "
                "'0 of 0', which reads as a completed run rather than as one nobody described.")
        wrong = sorted({type(s).__name__ for s in self.seats if not isinstance(s, SeatLine)})
        if wrong:
            raise HandoverError(f"a provenance record's seats are SeatLine rows, not {wrong}")
        if self.agreement not in fingerprint.LABELS:
            raise HandoverError(
                f"agreement is one of {list(fingerprint.LABELS)}, not {self.agreement!r}")
        if not (isinstance(self.strongest, tuple) and len(self.strongest) == 2):
            raise HandoverError(
                "strongest is §12.5's (seat | None, why) pair; a bare name would leave the "
                "header unable to say why nobody was named, which is the ordinary outcome")
        if not str(self.strongest[1]).strip():
            raise HandoverError("§12.5's answer carries the reason it reached it")
        if self.synthesis_outcome is not None and \
                self.synthesis_outcome not in verifymod.OUTCOMES:
            raise HandoverError(
                f"synthesis_outcome is one of {list(verifymod.OUTCOMES)} or None, not "
                f"{self.synthesis_outcome!r}")
        if not isinstance(self.synthesis_measured, bool):
            raise HandoverError(
                f"synthesis_measured is a bool, not {self.synthesis_measured!r}: a truthy "
                "string here would print an asserted verdict under the Verified sentence")
        if self.synthesis_measured and (self.synthesis_outcome is None
                                        or self.verify_seconds is None):
            raise HandoverError(
                "this record claims the synthesis verdict was MEASURED by this engine and "
                "carries no outcome or no duration. A measurement this engine cannot show is "
                "not one, and the Verified sentence is printed on the strength of this flag.")
        completed = [s for s in self.seats if s.forge == "completed"]
        if self.synthesis_outcome is not None and not completed:
            raise HandoverError(
                "this record reports a synthesis verdict over a fleet in which no seat "
                "completed. §18 asks that a failure cannot render a success header, and this "
                "is the combination that does it.")
        if not isinstance(self.ultra, ultramod.Ultra):
            raise HandoverError(f"ultra is an ultra.Ultra, not {type(self.ultra).__name__}")
        if self.review_terminal is not None and self.review_terminal not in _TERMINALS:
            raise HandoverError(
                f"review_terminal is one of {list(_TERMINALS)} or None, not "
                f"{self.review_terminal!r}")
        if self.review_terminal == _REVIEW_BLOCKED and self.unresolved_findings < 1:
            raise HandoverError(
                "§13's `review_blocked` is a terminal a run reaches BECAUSE a blocker is "
                "unresolved, so a record carrying it beside zero unresolved findings would "
                "render a clean provenance header with an unresolved blocker — the one thing "
                "§13 says must never happen")
```

Add near the top of the module, after `KINDS`:

```python
# §13's three terminals, IMPORTED rather than respelled — `review` owns the vocabulary and a
# second copy here is a second place to be right about which strings end a review.
def _review_terminals():
    from . import review          # local: `review` imports nothing from this module
    return review.TERMINALS, review.REVIEW_BLOCKED


_TERMINALS, _REVIEW_BLOCKED = _review_terminals()
```

- [ ] **Step 4: Implement `header` and `text`**

Append to `shared/lib/forge/handover.py`:

```python
def _ultra_line(u) -> str:
    """§13.1's four statuses, four renderings. §16.1's example shows only the first, and the
    other three are not decoration: an operator reading `Ultrareview:` needs to know whether
    the cloud review found nothing, was never asked, could not be asked, or is still running
    somewhere with a URL they can open."""
    if u.status == ultramod.RAN:
        n = len(u.bugs)
        return (f"Ultrareview: {n} finding(s) reported"
                + ("" if u.diff_measured else " (over a diff whose size could not be measured)"))
    if u.status == ultramod.UNAVAILABLE:
        return f"Ultrareview: unavailable ({u.reason}) — the run proceeded to handover"
    if u.status == ultramod.TIMED_OUT:
        where = f" and can be collected at {u.session_url}" if u.session_url else \
                " and no session URL was read from its output, so there is nothing to collect"
        return f"Ultrareview: the local wait elapsed; the remote review is still running{where}"
    return "Ultrareview: not requested (--no-ultra)"


def header(p: Provenance) -> str:
    """§16.1's provenance header.

    §16.1'S FORBIDDEN VERB APPEARS NOWHERE IN THIS MODULE — not in the rendered text, not in a
    docstring, and not in this sentence. It is forbidden for a seat that produced artifacts and
    failed verify, and the enforcement is a source-level assertion over the whole file rather
    than this function remembering which seats it may use it for: a rule that holds only on the
    branches its author remembered is the shape this package refuses everywhere else. That
    assertion is also why the word is described here instead of quoted.

    THE FUSION AND AGREEMENT LINES ARE ALWAYS PRESENT. §12.5's `strongest` names nobody
    whenever any seat is unrankable, and a real ledger carrying a `schema` or untraced `prose`
    criterion makes that the ORDINARY answer. A header that printed the line only when there
    was a winner would leave the operator reading a document with no strength claim in it and
    no way to tell that from a tool that had not looked.
    """
    n = len(p.seats)
    completed = sum(1 for s in p.seats if s.forge == "completed")
    usable = sum(1 for s in p.seats if s.artifacts == "usable")
    passed = sum(1 for s in p.seats if s.verify_outcome == verifymod.PASS)
    unverified = sum(1 for s in p.seats if s.verify_outcome is None)
    first = (f"**Forge: {completed} of {n} seats completed; {usable} artifact set(s) usable; "
             f"{passed} of {n} passed verify")
    if unverified:
        # "did not pass" and "was never verified" are different facts about a paid clone, and
        # a bare `passed/n` spells them the same way.
        first += f" ({unverified} never verified)"
    first += "."

    strat = p.strategy or "strategy not recorded"
    if p.synthesis_outcome is None and not completed:
        second = ("Synthesis: no seat produced a candidate, so no synthesis was attempted and "
                  "there is no verify result to report.")
    elif p.synthesis_outcome is None:
        # NO VERDICT IS NOT NO CANDIDATE. Seats completed here; nobody reported an outcome for
        # the fusion — `--collect` was run without one. Saying "no seat produced a candidate"
        # would be this header inventing a fleet failure out of a missing argument.
        second = (f"Synthesis: {completed} of {n} seats produced a candidate and no verify "
                  "verdict was reported for the fusion. This engine did not run one either, "
                  "so there is nothing here about whether the fused work passes.")
    elif p.synthesis_measured:
        secs = f", {p.verify_seconds:.0f}s" if p.verify_seconds is not None else \
               ", duration not measured"
        second = (f"Synthesis: verify {p.synthesis_outcome} (`{p.verify_command}`{secs}) "
                  f"— {strat}.")
    else:
        # THE ASSERTED RENDERING, AND IT DOES NOT BORROW THE MEASURED ONE'S WORDS. §16 makes
        # the orchestrator the synthesis author; `--collect` is handed this verdict and never
        # builds a verifier clone for the fusion. "verify PASS" would be this engine's own
        # vocabulary for something it ran, spent on something it did not.
        second = (f"Synthesis: the orchestrator reports {p.synthesis_outcome} for "
                  f"`{p.verify_command}` — {strat}. This engine did not run it: no verifier "
                  "clone was built for the fusion, so this line is a report and not a "
                  "verification.")

    name, why = p.strongest
    fusion = (f"Fusion: strongest seat: {name} — {why}." if name else f"Fusion: {why}.")
    fusion += f" §11 agreement: {p.agreement}."

    if p.review_terminal is None:
        third = "Council: no review round was convened."
    else:
        third = (f"Council: {p.review_rounds} round(s), {p.unresolved_findings} finding(s) "
                 f"unresolved ({p.review_terminal}).")

    return "\n".join([first, second, fusion, third, _ultra_line(p.ultra) + "**"])


_VERIFIED_MEANS = (
    "\"Verified\" here means exactly this and no more: the confirmed verify command exited 0 "
    "on a fresh verifier clone at the final checkpoint. It does not mean the change has no "
    "new defects, and it is not a review."
)

# THE OTHER PARAGRAPH, AND THE REASON THERE ARE TWO. The sentence above describes a
# measurement THIS ENGINE TOOK. A synthesis verdict reaches `--collect` from the orchestrator
# that fused, and printing that under the sentence above would claim a verifier clone nobody
# built. Neither paragraph is a hedge of the other: one says what a PASS is, the other says
# who said so.
_ASSERTED_MEANS = (
    "This run's synthesis verdict was reported by the orchestrator that fused the candidates. "
    "This engine did not run it: no verifier clone was built for the fusion and the confirmed "
    "verify command was not executed here, so nothing above is a verification. To get one, run "
    "the confirmed command yourself in a fresh clone of the synthesis branch."
)

_NO_VERDICT_MEANS = (
    "There is no synthesis verdict here at all — not a failing one, and not one somebody else "
    "reported. Nothing above says whether the fused work passes this repository's gate, and "
    "the absence is not a pass: run the confirmed verify command yourself in a fresh clone of "
    "the synthesis branch before you treat any of this as working."
)


def _means(p: Provenance) -> str:
    """The one provenance paragraph this run has earned. THREE STATES, THREE RECORDS: a verdict
    this engine measured, a verdict somebody else reported, and no verdict. Folding the third
    into the second would say the orchestrator reported something over a run where nobody
    reported anything."""
    if p.synthesis_outcome is None:
        return _NO_VERDICT_MEANS
    return _VERIFIED_MEANS if p.synthesis_measured else _ASSERTED_MEANS


def text(h: Handover, p: Provenance) -> str:
    """The whole handover message: the header, what was delivered, and what merging does not do.

    §16 requires the B1 file list HERE rather than only at a confirmation gate an hour earlier,
    and it requires the sentence about out-of-band artifacts to be stated plainly rather than
    left to be inferred from the fact that they are listed separately.

    EXACTLY ONE OF THE THREE PROVENANCE PARAGRAPHS, NEVER TWO AND NEVER NONE — measured,
    asserted, or no verdict at all. Three states, three records; collapsing the third into the
    second would tell the user the orchestrator reported something when nobody reported
    anything. Which one is `_means`'s decision and not this line's discretion, and `Provenance`
    has already refused a `synthesis_measured=True` it cannot show a measurement for.
    """
    if not isinstance(h, Handover):
        raise HandoverError(f"a Handover is required, not {type(h).__name__}")
    if not isinstance(p, Provenance):
        raise HandoverError(f"a Provenance is required, not {type(p).__name__}")
    parts = [header(p), "", _means(p), "",
             f"Branch: `{h.branch}` ({h.kind}) — {h.why}"]
    if h.handover_target:
        parts.append(f"Handed over to: {h.handover_target}")
    elif h.accepted:
        parts.append("Handed over: accepted by the user without a merge target — a "
                     "patch-based handover may intentionally never merge this branch.")
    parts += ["", "B1 — the baseline this run started from:"]
    parts += [f"  {f}" for f in h.b1_files] or ["  (no files)"]
    if h.baseline_owned:
        parts += ["", "Baseline-owned — unchanged selected untracked/ignored files. These are "
                      "yours, not forge's; only their B→S changes are forge-authored:"]
        parts += [f"  {f}" for f in h.baseline_owned]
    if h.out_of_band:
        parts += ["", "Out-of-band artifacts. MERGING THE BRANCH ALONE DOES NOT INSTALL THESE "
                      "— they are ignored files and were never added to the object store:"]
        for o in h.out_of_band:
            parts.append(f"  {o.path}  sha256={o.sha256}  {o.size} byte(s)")
            parts.append(f"    {' '.join(o.copy_command)}")
    else:
        parts += ["", "No out-of-band artifacts: this delivery is entirely in the branch."]
    return "\n".join(parts) + "\n"
```

- [ ] **Step 5: Run the tests**

```
cd /home/khenrix/git/khenrix-utils
uvx --with pytest pytest -q tests/test_forge_handover.py
```

Expected: PASS. If `test_a_seat_that_produced_artifacts_and_failed_verify_is_never_called_built`
fails on the source check, a docstring above used the word — rewrite it; the assertion is
correct.

- [ ] **Step 6: Mutation-test the refusals that keep a failure from rendering a success**

```
cd /home/khenrix/git/khenrix-utils
git status --short
python3 scripts/mutate.py --file shared/lib/forge/handover.py \
  --old '        if self.synthesis_outcome is not None and not completed:' \
  --new '        if False:' \
  --test 'uvx --with pytest pytest -q tests/test_forge_handover.py'
python3 scripts/mutate.py --file shared/lib/forge/handover.py \
  --old '        if not self.seats:' --new '        if False:' \
  --test 'uvx --with pytest pytest -q tests/test_forge_handover.py'
python3 scripts/mutate.py --file shared/lib/forge/handover.py \
  --old '        if self.review_terminal == _REVIEW_BLOCKED and self.unresolved_findings < 1:' \
  --new '        if False:' \
  --test 'uvx --with pytest pytest -q tests/test_forge_handover.py'
python3 scripts/mutate.py --file shared/lib/forge/handover.py \
  --old '    fusion = (f"Fusion: strongest seat: {name} — {why}." if name else f"Fusion: {why}.")' \
  --new '    fusion = (f"Fusion: strongest seat: {name}." if name else "")' \
  --test 'uvx --with pytest pytest -q tests/test_forge_handover.py'
python3 scripts/mutate.py --file shared/lib/forge/handover.py \
  --old '    return _VERIFIED_MEANS if p.synthesis_measured else _ASSERTED_MEANS' \
  --new '    return _VERIFIED_MEANS' \
  --test 'uvx --with pytest pytest -q tests/test_forge_handover.py'
python3 scripts/mutate.py --file shared/lib/forge/handover.py \
  --old '        if self.synthesis_measured and (self.synthesis_outcome is None' \
  --new '        if False and (self.synthesis_outcome is None' \
  --test 'uvx --with pytest pytest -q tests/test_forge_handover.py'
git status --short
```

Expected: all six `CAUGHT`. The fourth is the brief's requirement in mutation form: dropping
the reason when nobody is named must fail the suite. The fifth and sixth are the overclaim in
mutation form — printing "Verified here means" beside an unmeasured verdict, and letting a
record claim a measurement it cannot show — and if either SURVIVES the header can still state a
verdict it has no evidence for.

- [ ] **Step 7: Whole suite, render, gate, commit**

```
cd /home/khenrix/git/khenrix-utils
uvx --with pytest pytest -q tests/
make render
make verify
echo "verify rc=$?"
make precommit
echo "precommit rc=$?"
git add shared/lib/forge/handover.py tests/test_forge_handover.py marketplaces
git commit -m "$(cat <<'EOF'
feat(forge): §16.1 — the two judgements that usually cannot be made are lines, not blanks

§12.5's strongest names nobody whenever any seat is unrankable, and coverage._schema is
unresolved BY CONSTRUCTION while coverage._prose is unresolved for any criterion with no
recorded trace — so a real ledger makes (None, why) the ORDINARY outcome. §11's label is
differently-prompted for any real fleet, since three seats are three different CLIs. The
header renders both every time, with the reason, because a line printed only when there is
a winner leaves the operator unable to tell a tool that found no winner from one that never
looked.

"Built" appears nowhere in the module: §16.1 forbids it for a seat that produced artifacts
and failed verify, and the enforcement is the absence of the word rather than a renderer
remembering which branches may use it.

Provenance refuses the combinations that would render a failure as a success — a synthesis
verdict over a fleet in which nothing completed, an empty seat tuple, and review_blocked
beside zero unresolved findings. The "Verified means" sentence says a PASS is the confirmed
verify command exiting 0 on a fresh verifier clone at the final checkpoint and stops there,
because verify.classify never reads baseline_run on that path.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UiV66Pt8cZVMq9t8WEAhpN
EOF
)"
```

Expected: `verify rc=0`, `precommit rc=0`.

---

## Task 6: the CLI — `--start` and `--collect`, and the callers three plans deferred

This task closes the debt the ledger has carried since Plan H: **`make_launcher`/`forge_spec`
have no production caller, so §8.1's validator — which stops a seat being re-run in the same
cwd on top of its own half-finished work — has never run outside the test suite.** It also
gives `run_ultra` its first caller.

**Two commits.** The first is a schema change (`Confirmation` gains `ultrareview`); the second
is the CLI. Plan I₂'s precedent: a schema change rides alone so a reviewer can reject the
shape without rejecting the feature.

**Files:**
- Modify: `shared/lib/forge/gate.py` (`Confirmation.ultrareview`, `_ANSWERS`, `_REQUIRED`,
  `confirm`, `open_run`'s journalled policies)
- Create: `shared/lib/forge/cli.py`
- Test: `tests/test_forge_gate.py` (commit 1), `tests/test_forge_cli.py` (commit 2, new)

**Interfaces:**
- Consumes: `preflight.inspect_repo(repo, selected_untracked=()) -> Report`;
  `preflight.refusals(report)`; `preflight.task_refusals(instruction, *, bundle, closures)`
  (Task 3); `gate.quote(report, *, seats, attempts, review_rounds, ultrareview) -> Quote`;
  `gate.must_show(report, quote_, command) -> tuple[str, ...]`;
  `gate.propose_identity(repo)`; `gate.confirm(report, quote_, answers) -> Confirmation`;
  `gate.open_run(report, confirmation, run_id) -> Path`; `storage.new_run_id() -> str`;
  `storage.run_root(repo_path, run_id, must_be_new=False) -> Path`;
  `storage.task_source_path(run_dir)` (Task 3); `taskbundle.scan(root, *, entrypoint, quota)`,
  `bundle_hash(b)`, `write_task_bundle(run_dir, b)`, `read_task_bundle(run_dir)`,
  `installed_closure(cli)`; `launch.make_launcher(*, prompt, timeout, cfg=None,
  bundle_sha256=None, retries=0, backoff=0.0, run_provider=…, probe=…)`;
  `runner.run(run_dir, repo, *, identity, launch) -> tuple[SeatResult, ...]`;
  `runner.SeatResult` fields (`name`, `status`, `artifacts`, `candidate`, `verification`,
  `verification_refused`, `launch_result`, `seat`, `path`);
  `runstate.reconstruct(run_dir, repo) -> Reconstruction`; `runstate.read_manifest(run_dir)`;
  `handover.*` (Tasks 4–5); `ultra.run_ultra(run_dir, *, checkout, base, head, round_,
  enabled=True, timeout_minutes=30, target=None, file_limit=500, line_limit=8000,
  run=subprocess.run) -> Ultra`; `fingerprint.from_row(row)`,
  `fingerprint.agreement_label(ids)`; `rubric.strongest(dims)`;
  `engine.MODE_TIMEOUT` (Task 9 adds `"forge"`).
- Produces:
  - `cli.CliError(RuntimeError)`
  - `cli.main(argv=None, *, out=None, make_launcher=None) -> int`
  - `cli.start(args, *, out, make_launcher=None) -> int`
  - `cli.collect(args, *, out) -> int` (`cli._gc` arrives in Task 7)
  - `gate.Confirmation.ultrareview: bool`
  - Task 7 adds the `--gc` subcommand to this parser; Task 11's SKILL.md drives this CLI.

**`make_launcher` is a seam and not a convenience.** It is `None`-defaulted and resolved at
call time (`make_launcher or launch.make_launcher`), which is the only shape that serves both
callers the tests need: an explicit fake passed through `main`, and a `monkeypatch.setattr` on
`cli.launch.make_launcher`. A module-level default bound at `def` time would silently ignore the
second. Without the seam there is no way to drive `--start` from a test without paying three
providers, and **no test in this project may invoke a real provider or spend money**.

**The specific fail-opens this must not have:**
1. **A `--start` that reports success after a refusal.** Every refusal path returns a non-zero
   exit code and prints the refusal lines. A test drives a repository preflight refuses and
   asserts the exit code, the absence of a run directory, and that no launcher was built.
2. **Two spellings of `--no-ultra`.** `gate.quote(ultrareview=…)` prices it and
   `ultra.run_ultra(enabled=…)` obeys it, and they run in two different processes. The
   decision is a **confirmed answer**, recorded once, and `confirm` refuses an answer that
   disagrees with the `Quote` the operator was shown.
3. **A launcher built with a `bundle_sha256` from anywhere but `taskbundle.bundle_hash` over
   the run's own recorded bundle.** Task 3's seam test arms itself on this file's existence.
4. **`--collect` printing a handover for a run it could not read.** `reconstruct` raises for
   a run directory it cannot read whole, and the CLI does not catch that into a "nothing to
   collect" message. **Measured: `reconstruct` raises `runstate.ManifestError`,
   `runstate.StateError`, `journal.JournalError` or `storage.StorageError`, and all four are
   direct `RuntimeError` subclasses.** None of them is in the drafted narrow tuple, so as first
   written `--collect` on a damaged run *crashed with a traceback* rather than refusing — and
   the test for it could not pass. All four go in the tuple; the narrow-except rationale
   survives intact, because every one of them is this package saying "this run's state is
   unknown", which §14.1 names `outcome_unknown` and is exactly the refusal a caller wants
   printed.
5. **A synthesis verdict presented as a verification.** `--collect` is handed the outcome; it
   builds no verifier clone. It passes `synthesis_measured=False` and the header says who
   reported it — see Task 5.
6. **A default `--timeout` invented here.** §19 forbids a second timeout mechanism. The seat
   timeout comes from `engine.MODE_TIMEOUT["forge"]` (Task 9) and from nowhere else; until
   Task 9 lands, `--start` refuses rather than picking a number. **Order Task 9 before Task 6
   if you would rather not write that refusal** — either order is correct, but the refusal
   must exist if 6 lands first.

**What input would make this produce a result cleaner than its evidence:** a `--collect` on a
run whose `runner.run` returned **fewer** `SeatResult`s than `manifest.seats`, because a seat
every one of whose attempts was refused has no verdict to return (`runner.run`'s docstring says
so). Building `Provenance.seats` from the returned tuple reports "2 of 2 seats completed" for a
three-seat run. `collect` builds the seat lines from `storage.seat_names(run_dir)` — the
records on disk — and never from the length of a results tuple. That is in the test set.

### Commit 1 — `ultrareview` becomes a confirmed answer

- [ ] **Step 1: Write the failing test**

Append to `tests/test_forge_gate.py`:

```python
def test_the_ultrareview_decision_is_answered_once_and_must_match_what_was_priced():
    """§13.1's --no-ultra moves three scalars on the quote AND decides whether a cloud review
    runs an hour later, in another process. Two spellings of one decision will disagree, so it
    is a §5 step 2 ANSWER recorded once — and `confirm` refuses an answer that contradicts the
    Quote the operator was shown, which is the invariant living in the value."""
    report = _a_report()                       # existing helper in this file
    priced_on = gate.quote(report)
    priced_off = gate.quote(report, ultrareview=False)
    assert priced_on.provider_calls > priced_off.provider_calls

    answers = dict(_valid_answers(), ultrareview=True)      # existing helper
    assert gate.confirm(report, priced_on, answers).ultrareview is True
    assert gate.confirm(report, priced_off,
                        dict(answers, ultrareview=False)).ultrareview is False

    with pytest.raises(gate.GateError) as e:
        gate.confirm(report, priced_off, answers)           # priced off, answered on
    assert "priced" in str(e.value)
    with pytest.raises(gate.GateError):
        gate.confirm(report, priced_on, dict(answers, ultrareview=False))
    with pytest.raises(gate.GateError):
        gate.confirm(report, priced_on, {k: v for k, v in answers.items()
                                         if k != "ultrareview"})
    with pytest.raises(gate.GateError):
        gate.confirm(report, priced_on, dict(answers, ultrareview="yes"))
```

- [ ] **Step 2: Run to verify it fails, then implement**

```
cd /home/khenrix/git/khenrix-utils
uvx --with pytest pytest -q tests/test_forge_gate.py -k ultrareview_decision
```

Expected: FAIL — `Confirmation` has no `ultrareview`.

In `shared/lib/forge/gate.py`:

1. Add `ultrareview: bool` as a field on `Confirmation`, and to its `__post_init__`:

```python
        if not isinstance(self.ultrareview, bool):
            raise GateError(
                f"ultrareview is §13.1's opt-in/out and is a bool, not {self.ultrareview!r}. "
                "A truthy string here would price one run and collect another.")
```

2. Extend the two tuples:

```python
_ANSWERS = ("setup", "verify", "on_calibration_failure", "strategy", "author",
            "accepted_gaps", "ultrareview")
_REQUIRED = ("setup", "verify", "on_calibration_failure", "strategy", "author", "ultrareview")
```

3. In `confirm`, after the `unknown` check, add the agreement check and pass the field
   through:

```python
    # THE INVARIANT LIVES IN THE VALUE, not in a second check downstream. `quote` prices
    # §13.1 on or off and moves three scalars doing it; `ultra.run_ultra` obeys the decision
    # an hour later in another process. If those were two readings of one intent they would
    # eventually disagree, and the disagreement is money: a run priced without the cloud
    # review that then requests one, or the reverse — a user shown $5-25 they were never
    # charged and a review they were told they would get.
    #
    # `Quote.ultrareview` is a STRING by design (§13.1 prices it in usage credits, not calls),
    # so the comparison is against what `quote` recorded rather than a second boolean field.
    priced_on = not quote_.ultrareview.startswith("not run")
    if bool(answers["ultrareview"]) is not priced_on:
        raise GateError(
            f"this run was priced with ultrareview {'ON' if priced_on else 'OFF'} and the "
            f"answer says {'ON' if answers['ultrareview'] else 'OFF'}. §5 step 5 forbids "
            "re-asking, so the quote the operator saw is the one this run may spend — "
            "re-price with `quote(..., ultrareview=...)` and show it again.")
```

then add `ultrareview=answers["ultrareview"],` to the `Confirmation(...)` construction.

4. In `open_run`, add `ultrareview` to the payload journalled on the `confirm` done record.
   **Read the existing payload construction and `runner._confirmed_policy` first, and mirror
   the key naming exactly** — a policy read back by a name the writer does not use is the same
   silence one field over.

- [ ] **Step 3: Run the gate suite and the whole suite**

```
cd /home/khenrix/git/khenrix-utils
uvx --with pytest pytest -q tests/test_forge_gate.py
uvx --with pytest pytest -q tests/
```

Expected: `test_forge_gate.py` PASSES; the whole suite will fail wherever a test constructs a
`Confirmation` or calls `confirm` without the new answer. **Fix each by supplying the answer
that matches the quote that test built — never by giving the field a default.** `Confirmation`
has no defaults on purpose (`runstate.State`'s rule: a field the constructor supplies is a
fact nobody answered for).

- [ ] **Step 4: Mutation-test, then commit**

```
cd /home/khenrix/git/khenrix-utils
git status --short
python3 scripts/mutate.py --file shared/lib/forge/gate.py \
  --old '    if bool(answers["ultrareview"]) is not priced_on:' --new '    if False:' \
  --test 'uvx --with pytest pytest -q tests/test_forge_gate.py'
python3 scripts/mutate.py --file shared/lib/forge/gate.py \
  --old '        if not isinstance(self.ultrareview, bool):' --new '        if False:' \
  --test 'uvx --with pytest pytest -q tests/test_forge_gate.py'
git status --short
make render
make verify
echo "verify rc=$?"
make precommit
echo "precommit rc=$?"
git add shared/lib/forge/gate.py tests/ marketplaces
git commit -m "$(cat <<'EOF'
feat(forge): §13.1's opt-out is an answer the run records, not a parameter two processes guess

--no-ultra moves three scalars on §5.2's quote and decides whether a cloud review runs an
hour later in another process. As a keyword parameter on quote() it was two readings of one
intent, free to disagree — and the disagreement is money in both directions. It is now a
required §5 step 2 answer, and confirm refuses an answer that contradicts the Quote the
operator was shown, so the priced decision and the spent decision are one decision.

Confirmation gains no default: a field the constructor supplies is a fact nobody answered.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UiV66Pt8cZVMq9t8WEAhpN
EOF
)"
```

Expected: both mutations `CAUGHT`; `verify rc=0`, `precommit rc=0`.

### Commit 2 — the CLI

- [ ] **Step 5: Write the failing tests**

Create `tests/test_forge_cli.py`. **No test in this file may invoke a real provider, and none
may write outside `tmp_path`.** Two rules make that structural rather than remembered:

- **`_drive_a_start` takes `monkeypatch` as a required positional argument, and every test that
  calls it requests the fixture.** It needs it twice: to point `XDG_STATE_HOME` at `tmp_path`
  so `storage.run_root` never writes under the developer's `~/.local/state`, and to keep the
  provider fake in place. A helper that quietly did neither — driven from a test with no
  `monkeypatch` in its signature — is the shape that spends money and writes to a real home
  directory while looking like an ordinary unit test.
- **The launcher reaches `cli` through the `make_launcher=` seam**, either passed explicitly
  through `cli.main(...)` or monkeypatched onto `cli.launch.make_launcher`. Injecting
  `run_provider`/`probe` into `make_launcher` is what keeps the real launcher path under test,
  exactly as `tests/test_forge_launch.py` already does — read that file and reuse its fakes
  rather than writing new ones.

`_drive_a_start` must also neutralise `cli._closures`, which otherwise walks the developer's
three real plugin caches: `monkeypatch.setattr(cli, "_closures", lambda: {"claude": "a",
"codex": "a", "agy": "a"})`. A test whose result depends on which CLIs happen to be installed
on the machine running it is not a test of this code.

```python
"""The front end. Nothing here spends money: every provider call is an injected fake."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "lib"))

from forge import cli, handover, preflight, storage, taskbundle  # noqa: E402


def test_the_parser_offers_exactly_the_four_verbs_the_spec_names(capsys):
    """--start / --collect / --gc / --no-ultra. `--gc` arrives in the next task and its
    absence here would be found by an operator rather than by this suite."""
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    for flag in ("--start", "--collect", "--gc", "--no-ultra"):
        assert flag in out, flag


def test_a_repository_preflight_refuses_costs_nothing_and_exits_non_zero(tmp_path, capsys):
    """A refusal must not read as a run. Three assertions, because 'it printed something' is
    not one of them: the exit code, the absence of a run directory, and that no launcher was
    ever built."""
    repo = _a_repo_with_a_secret(tmp_path)
    rc = cli.main(["--start", "--repo", str(repo), "--task", str(_a_task(tmp_path)),
                   "--answers", str(_answers(tmp_path))])
    assert rc != 0
    err = capsys.readouterr().out
    assert "refuse" in err.lower()
    assert not list((tmp_path / "state").rglob("manifest.json"))


def test_a_task_naming_provider_specific_machinery_is_refused_before_the_gate(tmp_path, capsys):
    """§20's refusal has a home and this is its first caller."""
    repo = _a_clean_repo(tmp_path)
    task = _a_task(tmp_path, body="Dispatch a subagent with subagent_type: Explore.")
    rc = cli.main(["--start", "--repo", str(repo), "--task", str(task),
                   "--answers", str(_answers(tmp_path))])
    assert rc != 0
    assert "not portable" in capsys.readouterr().out


def test_start_records_the_task_bundle_and_hands_the_launcher_its_hash(tmp_path, monkeypatch):
    """The debt three plans deferred: make_launcher/forge_spec had no production caller, so
    §8.1's validator never ran outside the suite. This asserts the CLI builds the launcher and
    that the hash it passes is taskbundle.bundle_hash over the run's OWN recorded bundle.

    The spy wraps the REAL make_launcher with fake transport, so the seam under test is the
    CLI's argument list and not a stand-in for it."""
    seen = {}
    real = cli.launch.make_launcher

    def spy(**kw):
        seen.update(kw)
        return real(**kw, run_provider=_fake_provider, probe=_fake_probe) \
            if "run_provider" not in kw else real(**kw)
    monkeypatch.setattr(cli.launch, "make_launcher", spy)
    run_dir = _drive_a_start(tmp_path, monkeypatch, make_launcher=None)   # resolve via module
    b = taskbundle.read_task_bundle(run_dir)
    assert seen["bundle_sha256"] == taskbundle.bundle_hash(b)
    assert seen["bundle_sha256"] is not None


def test_every_seats_recorded_fingerprint_carries_the_bundle_hash(tmp_path, monkeypatch):
    """§11's bundle_sha256 was None for every real seat until §20 had a producer."""
    run_dir = _drive_a_start(tmp_path, monkeypatch)
    from forge import runstate
    for name in storage.seat_names(run_dir):
        rec = runstate.read_seat(run_dir, name)
        for attempt in rec["attempts"]:
            pi = attempt.get("prompt_identity") or {}
            assert pi.get("bundle_sha256"), (name, pi)


def test_collect_builds_its_seat_lines_from_the_records_and_not_from_a_results_tuple(
        tmp_path, monkeypatch):
    """`runner.run` returns one SeatResult per seat that produced one, which is not always
    `manifest.seats` of them — a seat every attempt of which was refused has no verdict to
    return. Counting the tuple reports '2 of 2 seats completed' for a three-seat run."""
    run_dir = _drive_a_start(tmp_path, monkeypatch, refuse_seat="agy")
    text = _collect_text(tmp_path, run_dir)
    assert "of 3 seats completed" in text
    assert "of 2 seats" not in text


def test_collect_refuses_a_run_directory_it_cannot_read_whole(tmp_path, monkeypatch, capsys):
    """MEASURED: `runstate.reconstruct` raises `runstate.ManifestError`, `runstate.StateError`,
    `journal.JournalError` or `storage.StorageError` — four direct RuntimeError subclasses, and
    not one of them was in `main`'s first draft of the narrow except tuple. As drafted this
    test could not pass: `cli.main` would propagate the raise and the test would ERROR rather
    than read a return code. The tuple carries all four; the narrow-except argument survives,
    because each is this package refusing a run whose state is unknown."""
    run_dir = _drive_a_start(tmp_path, monkeypatch)
    storage.manifest_path(run_dir).write_bytes(b"{ not json")
    rc = cli.main(["--collect", _run_id(run_dir), "--repo", str(_repo_of(run_dir))])
    assert rc != 0
    out = capsys.readouterr().out.lower()
    assert "collected" not in out
    assert "manifest" in out, "the refusal did not name what could not be read"


def test_no_ultra_reaches_run_ultra_as_well_as_the_quote(tmp_path, monkeypatch):
    """Two spellings of one flag, in two processes. The answer is recorded at the gate and
    read back here; this asserts the READ, not the write."""
    calls = []
    monkeypatch.setattr(cli.ultra, "run_ultra",
                        lambda *a, **kw: calls.append(kw) or _skipped_ultra())
    run_dir = _drive_a_start(tmp_path, monkeypatch, no_ultra=True)
    _collect_text(tmp_path, run_dir)
    assert calls and calls[-1]["enabled"] is False


def test_the_handover_a_collect_prints_does_not_call_an_asserted_verdict_verified(
        tmp_path, monkeypatch, capsys):
    """`--collect --synthesis-outcome PASS` renders a verdict THIS ENGINE DID NOT MEASURE. It
    builds no verifier clone for the fusion and runs no confirmed command over it, so the
    §16.1 "Verified here means" paragraph may not appear beside it."""
    run_dir = _drive_a_start(tmp_path, monkeypatch)
    _fuse_something(run_dir)          # helper: a commit in the synthesis worktree
    rc = cli.main(["--collect", _run_id(run_dir), "--repo", str(_repo_of(run_dir)),
                   "--accept", "--synthesis-outcome", "PASS", "--strategy", "from_scratch"])
    assert rc == 0
    out = capsys.readouterr().out
    assert handover._VERIFIED_MEANS not in out
    assert "the orchestrator reports PASS" in out
    assert "This engine did not run it" in out
```

Write the helpers (`_a_repo_with_a_secret`, `_a_clean_repo`, `_a_task`, `_answers`,
`_drive_a_start`, `_fuse_something`, `_collect_text`, `_run_id`, `_repo_of`, `_fake_provider`,
`_fake_probe`, `_skipped_ultra`) in this file. **Reuse the repository and answer-sheet fixtures
`tests/test_forge_runner.py` and `tests/test_forge_gate.py` already define wherever they fit —
read both first.**

`_drive_a_start(tmp_path, monkeypatch, **kw)` takes `monkeypatch` **positionally and required**,
and does three things before it drives anything:

```python
def _drive_a_start(tmp_path, monkeypatch, *, no_ultra=False, refuse_seat=None,
                   make_launcher=_a_fake_make_launcher):
    """§5's gate and §7's fleet, with nothing paid and nothing written outside tmp_path.

    THE THREE NEUTRALISATIONS ARE NOT OPTIONAL AND THIS IS WHY THE FIXTURE IS REQUIRED RATHER
    THAN DEFAULTED. Without the first, `storage.run_root` writes run directories under the
    developer's real `~/.local/state` and every test run leaks one. Without the second, three
    real CLIs are launched and the suite spends money. Without the third, the result depends on
    which plugins happen to be installed on the machine running it.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "_closures",
                        lambda: {"claude": "a", "codex": "a", "agy": "a"})
    ...
    rc = cli.main(argv, out=buf, make_launcher=make_launcher)
```

`_a_fake_make_launcher` matches `launch.make_launcher`'s keyword-only signature and returns a
callable satisfying `launch(*, name, seat_path, token, env)` — read
`tests/test_forge_launch.py` for the fakes and the exact contract before writing it.

- [ ] **Step 6: Run to verify they fail**

```
cd /home/khenrix/git/khenrix-utils
uvx --with pytest pytest -q tests/test_forge_cli.py
```

Expected: FAIL at collection — no `forge.cli`.

- [ ] **Step 7: Write `cli.py`**

Create `shared/lib/forge/cli.py`:

```python
"""The front end: `--start`, `--collect`, `--gc`.

WHAT THIS IS AND IS NOT. `--start` drives §5's gate, §7's fleet and §6's verification, and it
STOPS where `runner.run` stops — at `comparing`, with the candidates verified on disk and
nothing choosing between them. The choosing is §10 through §13, and §16 says who does it: "the
synthesis author is the trusted invoking orchestrator under its normal approval boundary — not
a fourth unattended bypass-permissions seat." That orchestrator is the agent running SKILL.md,
not this module, which is why there is no `--synthesize`.

`--collect` is the other half: it reads a run back off the disk, measures the synthesis the
orchestrator built, runs §13.1's cloud review once, decides §16's mergeability and prints the
handover. §14 makes "always from disk and never from conversation state" a requirement rather
than a style, so `--collect` takes a run id and a repository and nothing else.

NOTHING HERE INVENTS A TIMEOUT. §19 forbids a second timeout mechanism; the seat window is
`council.engine.MODE_TIMEOUT["forge"]` and there is no `--timeout`.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from council import engine

from . import (fingerprint, gate, handover, launch, preflight, runstate, storage,
               taskbundle, ultra, verify)
from . import runner as runnermod

_CLIS = ("claude", "codex", "agy")


class CliError(RuntimeError):
    """A run this front end will not start or collect."""


def _fail(out, lines) -> int:
    for line in lines:
        print(f"  ✗ {line}", file=out)
    return 1


def _resolve_seat_timeout() -> int:
    """§19's window, from the engine's own table and from nowhere else.

    A NUMBER CHOSEN HERE WOULD BE THE SECOND TIMEOUT MECHANISM §19 FORBIDS, and §19 exists
    because the first one silently degraded a three-seat panel to two. So a missing entry is a
    refusal rather than a fallback: `deep`'s 1200s is a review window, not a build window, and
    reaching for it here would put a forty-minute seat under a twenty-minute cap.
    """
    t = engine.MODE_TIMEOUT.get("forge")
    if not isinstance(t, int) or isinstance(t, bool) or t < 1:
        raise CliError(
            "council.engine.MODE_TIMEOUT has no usable `forge` entry, so there is no window "
            "this run may build under. §19 forbids a second timeout mechanism, so this does "
            "not pick one — add the entry (§19 asks for >= 3600) and re-run.")
    return t


def _closures() -> dict:
    """The three live installed plugin closures, resolved once. `None` for a CLI that is not
    installed, which `taskbundle.ambient_verdict` reads as False — three absences do not hash
    identically."""
    return {c: taskbundle.installed_closure(c) for c in _CLIS}


def start(args, *, out, make_launcher=None) -> int:
    """§5's gate, §7's fleet, §6's verification — and it stops at `comparing`.

    `make_launcher` IS A SEAM AND `None` MEANS "ASK THE MODULE", NOT "USE A DEFAULT". Resolved
    at call time rather than bound as a default at `def` time, because the two callers that
    need it want different things: a test passing an explicit fake through `main`, and a test
    that has monkeypatched `cli.launch.make_launcher` to spy on the real one. A `def`-time
    default would silently ignore the second, and without the seam at all there is no way to
    exercise this function without paying three providers — which no test in this project may
    do.
    """
    mk = make_launcher or launch.make_launcher
    repo = Path(args.repo).resolve()
    task_root = Path(args.task).resolve()
    entrypoint = args.entrypoint
    instruction = (task_root / entrypoint).read_text(encoding="utf-8", errors="surrogateescape")

    report = preflight.inspect_repo(repo, args.select or ())
    blocked = preflight.refusals(report)
    if blocked:
        return _fail(out, ["preflight refuses this repository, so nothing is spent:", *blocked])

    b = taskbundle.scan(task_root, entrypoint=entrypoint)
    # §20's refusal. The bundle is passed for the record, and it CLEARS NOTHING: it does not
    # make a provider-specific referent portable and it does not make an ambient skill
    # identical across three CLIs. The ambient bar is the three-closure hash and nothing else —
    # a `bundle is None` guard on it would be dead here, because this line always has one.
    closures = _closures()
    not_portable = preflight.task_refusals(instruction, bundle=b, closures=closures)
    if not_portable:
        return _fail(out, ["§20 refuses this task:", *not_portable])
    # §20's other half, and `taskbundle.ambient_note`'s only caller: a named skill that DID
    # clear the three-way hash is declared provider-neutral in the prompt the seats receive.
    # Clearing it and then saying nothing would use the skill on all three CLIs on the strength
    # of a check whose result never left this function.
    notes = preflight.ambient_notes(instruction, closures=closures)
    if notes:
        instruction = instruction + "\n\n" + "\n".join(notes)

    timeout = _resolve_seat_timeout()
    quote_ = gate.quote(report, seats=args.seats, attempts=args.attempts,
                        review_rounds=args.review_rounds, ultrareview=not args.no_ultra)
    command = verify.Command(steps=_steps(json.loads(Path(args.answers).read_text())["verify"]))
    for line in gate.must_show(report, quote_, command):
        print(f"  {line}", file=out)

    answers = json.loads(Path(args.answers).read_text())
    answers = _decode_answers(answers)
    answers["ultrareview"] = not args.no_ultra
    confirmation = gate.confirm(report, quote_, answers)

    run_id = storage.new_run_id()
    run_dir = gate.open_run(report, confirmation, run_id)

    # §20: "Persist the fully resolved instruction plus resource hashes so `--collect` never
    # depends on vanished conversation context." The BYTES go beside the manifest, because a
    # resume must not depend on the operator's own directory still existing.
    shutil.copytree(task_root, storage.task_source_path(run_dir))
    taskbundle.write_task_bundle(run_dir, b)

    launcher = mk(prompt=instruction, timeout=timeout,
                  bundle_sha256=taskbundle.bundle_hash(b))
    results = runnermod.run(run_dir, repo, identity=confirmation.author, launch=launcher)

    # §16: seat work is transported out of each remote-less clone by the ENGINE, from the
    # user's side, with an explicit refspec — before anything else can fail and leave it in a
    # directory `--gc` is allowed to delete.
    for r in results:
        if r.seat is not None:
            handover.transport_seat(repo, r.seat.path, run_id=run_id, seat=r.name)

    synth = run_dir / "synthesis"
    handover.create_synthesis_worktree(repo, synth, run_id=run_id,
                                       at=runstate.read_manifest(run_dir).baseline_commit)
    print(f"run: {run_id}", file=out)
    print(f"synthesis worktree: {synth}", file=out)
    for line in _seat_table(run_dir):
        print(f"  {line}", file=out)
    print("Next: fuse in the synthesis worktree, then `--collect " + run_id + "`.", file=out)
    return 0


def _seat_lines(run_dir) -> tuple:
    """§16.1's first line, built from the RECORDS ON DISK.

    NEVER FROM A RESULTS TUPLE. `runner.run` returns one `SeatResult` per seat that produced
    one, "which is not always `manifest.seats` of them: a seat every one of whose attempts was
    REFUSED has no verdict to return, and the loop reports that by absence rather than by
    inventing one." Counting the tuple therefore reports "2 of 2 seats completed" for a
    three-seat run in which one seat was refused outright — a denominator that shrank to fit
    the numerator.
    """
    lines = []
    for name in storage.seat_names(run_dir):
        rec = runstate.read_seat(run_dir, name) or {}
        attempts = rec.get("attempts") or []
        last = attempts[-1] if attempts else {}
        status = last.get("status") or {}
        lines.append(handover.SeatLine(
            name=name,
            forge=status.get("forge", "failed"),
            artifacts=status.get("artifacts", "unusable"),
            verify_outcome=(last.get("verification") or [None])[0]))
    return tuple(lines)
```

**Read `runner._record` and `runner._payload` before writing `_seat_lines`** and take the key
names from what they actually write. A reader that guesses a key gets `None` and renders a
`failed` seat for a run that succeeded — the fail-open this function's own docstring is about,
arriving through the dictionary rather than through the tuple.

- [ ] **Step 8: Write `collect` and `main`**

Append to `shared/lib/forge/cli.py`:

```python
def collect(args, *, out) -> int:
    repo = Path(args.repo).resolve()
    run_dir = storage.run_root(repo, args.collect, must_be_new=False)
    # RAISES rather than reporting nothing. `reconstruct`'s own docstring lists what it refuses
    # a run directory for, and every one of them is a run whose state is unknown — which §14.1
    # names `outcome_unknown` and says is never silently retried. A `except: return 1` here
    # would print "nothing to collect" for a run that spent three provider calls.
    recon = runstate.reconstruct(run_dir, repo)
    manifest = recon.manifest
    synth = run_dir / "synthesis"
    if not synth.exists():
        return _fail(out, [f"{synth} does not exist: this run has no synthesis worktree, so "
                           "there is nothing to hand over. Re-run `--start`, or create it and "
                           "fuse there."])

    head = _rev(synth, "HEAD")
    tree = _rev(synth, "HEAD^{tree}")
    enabled = _confirmed_ultrareview(run_dir)
    u = ultra.run_ultra(run_dir, checkout=synth, base=manifest.baseline_commit, head=head,
                        round_=max(1, manifest.review_rounds), enabled=enabled)

    sidecars = _sidecars_of(run_dir)
    merge = handover.mergeability(manifest, synthesis_tree_oid=tree, sidecars=sidecars)
    oob = handover.out_of_band(sidecars, synthesis_path=synth, run_dir=run_dir)
    h = handover.Handover(
        run_id=manifest.run_id, branch=handover.branch(manifest.run_id, handover.SYNTHESIS),
        kind=merge.kind, handover_target=args.handover_target,
        accepted=bool(args.accept), out_of_band=oob,
        baseline_owned=_baseline_owned(manifest, sidecars),
        b1_files=tuple(manifest.selected_paths), why=merge.why)
    handover.write_handover(run_dir, h)

    p = handover.Provenance(
        seats=_seat_lines(run_dir), synthesis_outcome=args.synthesis_outcome,
        # `False`, AND IT IS A CONSTANT HERE RATHER THAN A FLAG. Nothing in this function runs
        # the confirmed verify command over the fusion: §16 makes the orchestrator the
        # synthesis author, and building a verifier clone here would be a fourth §6 pass that
        # §5.2 never quoted. So the outcome above arrives from argv, which is a REPORT, and
        # `handover.header` renders it in the words of one. There is no `--synthesis-measured`
        # flag, because a flag is exactly how an asserted verdict would come to be printed
        # under the "Verified here means" sentence.
        synthesis_measured=False,
        verify_command=" ".join(manifest.verify[0].argv) if manifest.verify else "",
        verify_seconds=args.synthesis_seconds, strategy=args.strategy,
        strongest=_strongest(run_dir), agreement=_agreement(run_dir),
        review_terminal=_review_terminal(run_dir), review_rounds=manifest.review_rounds,
        unresolved_findings=_unresolved(run_dir), ultra=u)
    print(handover.text(h, p), file=out)
    return 0


def _agreement(run_dir) -> str:
    """§11's label over the seats' recorded fingerprints.

    A SEAT WITH NO RECORDED FINGERPRINT IS NOT A SEAT THAT MATCHED. `agreement_label` compares
    recorded values; dropping an unrecorded seat would compute a label over a smaller fleet
    than the one that ran, and report it as the fleet's. So a missing row makes the label
    `not-comparable` — which is one of §11's three and is what a comparison nobody could make
    is called.
    """
    ids = []
    for name in storage.seat_names(run_dir):
        rec = runstate.read_seat(run_dir, name) or {}
        rows = [a.get("prompt_identity") for a in (rec.get("attempts") or [])]
        rows = [r for r in rows if r]
        if not rows:
            return "not-comparable"
        ids.append(fingerprint.from_row(rows[-1]))
    return fingerprint.agreement_label(ids) if ids else "not-comparable"


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="llm-forge", description=__doc__.splitlines()[0])
    verb = ap.add_mutually_exclusive_group(required=True)
    verb.add_argument("--start", action="store_true",
                      help="open a run: preflight, §5's gate, the fleet, §6's verification")
    verb.add_argument("--collect", metavar="RUN_ID",
                      help="read a run back off disk, run §13.1, and print §16's handover")
    verb.add_argument("--gc", metavar="RUN_ID",
                      help="§15's cleanup for one run, or `--gc all` for the disk report")
    ap.add_argument("--repo", default=".", help="the repository the run is about")
    ap.add_argument("--task", help="--start: the directory holding the task bundle")
    ap.add_argument("--entrypoint", default="TASK.md",
                    help="--start: the bundle entry the seats are told to read")
    ap.add_argument("--answers", help="--start: the JSON answer sheet for §5 step 2")
    ap.add_argument("--select", action="append", default=[],
                    help="--start: an untracked path to carry into the baseline (repeatable)")
    ap.add_argument("--seats", type=int, default=3)
    ap.add_argument("--attempts", type=int, default=3)
    ap.add_argument("--review-rounds", type=int, default=2, dest="review_rounds")
    ap.add_argument("--no-ultra", action="store_true",
                    help="§13.1 is default on; this opts out and re-prices the run")
    ap.add_argument("--handover-target", dest="handover_target",
                    help="--collect: where the work went, so §15 can define 'unmerged'")
    ap.add_argument("--accept", action="store_true",
                    help="--collect: the user accepts delivery with no merge target")
    ap.add_argument("--synthesis-outcome", dest="synthesis_outcome",
                    help="--collect: the verify verdict the orchestrator REPORTS. This engine "
                         "does not run it, and the handover says so rather than calling it "
                         "verified")
    ap.add_argument("--synthesis-seconds", dest="synthesis_seconds", type=float,
                    help="--collect: how long the orchestrator's own verify run took, if it "
                         "timed one")
    ap.add_argument("--strategy", help="--collect: the §12 rule the fusion followed")
    ap.add_argument("--force", action="store_true", help="--gc: see §15")
    return ap


def main(argv=None, *, out=None, make_launcher=None) -> int:
    out = out or sys.stdout
    args = build_parser().parse_args(argv)
    try:
        if args.start:
            for required in ("task", "answers"):
                if not getattr(args, required):
                    return _fail(out, [f"--start needs --{required}"])
            return start(args, out=out, make_launcher=make_launcher)
        if args.collect:
            return collect(args, out=out)
        return _gc(args, out=out)          # replaced by gc.py's caller in the next task
    except (CliError, preflight.PreflightError, gate.GateError, taskbundle.TaskBundleError,
            handover.HandoverError,
            # MEASURED, and the first draft of this tuple was missing all four.
            # `runstate.reconstruct` — the only thing `--collect` does before it can say
            # anything — raises exactly these for a run directory it cannot read whole, and
            # every one is a direct RuntimeError subclass that would otherwise leave the CLI
            # with a traceback instead of a refusal. They belong here for the SAME reason as
            # the five above and not as a widening: each is this package saying "this run's
            # state is unknown", which §14.1 names `outcome_unknown` — the one thing a
            # `--collect` must be able to tell an operator.
            runstate.ManifestError, runstate.StateError,
            journal.JournalError, storage.StorageError) as e:
        # NARROW ON PURPOSE. Every class here is one this package raises to say "I will not
        # do that", and printing it is the right end. A bare `except Exception` would turn a
        # crash mid-fleet — three paid seats, a half-written run directory — into the same
        # one-line message as a rejected answer sheet, which is the state §14.1 calls
        # `outcome_unknown` and says must be distinguishable.
        return _fail(out, [str(e)])
```

Add `journal` to `cli.py`'s `from . import (...)` list. **`reconstruct`'s own docstring names a
fifth case it does not cover** — a manifest path carrying a NUL reaches `os.lstat` and raises
`ValueError`, which is deliberately left to raise. Do not add `ValueError` to the tuple: it is
not this package refusing anything, and catching it would turn a genuine crash into a refusal
that reads the same as a rejected answer sheet.

**`--gc` is advertised by this parser and built in the next task, so define the stub now** —
a flag that raises `AttributeError` is worse than one that says it is not built:

```python
def _gc(args, *, out) -> int:
    """§15's cleanup. Replaced wholesale by `gc.py`'s caller in the next task.

    ADVERTISED AND REFUSED, rather than absent from the parser. `--gc` is in `--help` because
    `gate.py` tells the operator at the confirmation gate that it is mandatory, and a flag
    that raises `AttributeError` when they take that advice is worse than one that says what
    state it is in.
    """
    return _fail(out, ["--gc is not built in this build; the run directory can be removed by "
                       "hand, but the synthesis worktree and branch must be removed with "
                       "`git worktree remove` and `git update-ref -d` first"])
```

Write `_rev`, `_steps`, `_decode_answers`, `_sidecars_of`, `_baseline_owned`, `_strongest`,
`_review_terminal`, `_unresolved`, `_confirmed_ultrareview` and `_seat_table` in the same file.
For each, **the fail-closed value is the one the consumer already defines**:
`_strongest` returns `(None, "<why>")` when no ledger or coverage report exists — never a seat
name; `_review_terminal` returns `None` when no round was written — never `"ready"`;
`_unresolved` counts rows in `review.read_resolutions`, and a round whose resolutions cannot be
read raises rather than counting zero. `_confirmed_ultrareview` reads the confirm journal
record the way `runner._confirmed_policy` reads `on_calibration_failure` — **read that function
and mirror it**; a missing record raises, because a run that cannot say what it priced may not
be charged for a cloud review on this process's default.

- [ ] **Step 9: Run the CLI suite and the whole suite**

```
cd /home/khenrix/git/khenrix-utils
uvx --with pytest pytest -q tests/test_forge_cli.py
uvx --with pytest pytest -q tests/
```

Expected: both PASS.

- [ ] **Step 10: Prove §8.1's validator now runs outside the suite**

The debt this task closes is that `forge_spec` installs `_forge_validator` and nothing in
production ever built a spec through it. Assert the wiring by reading it:

```python
def test_the_production_launcher_installs_section_8_1s_validator():
    """Three plans deferred this: make_launcher/forge_spec had no production caller, so the
    validator that stops a seat being re-run in the same cwd on top of its own half-finished
    work had never run outside the test suite. The CLI is that caller — asserted through the
    real `make_launcher`, with only the transport faked."""
    seen = {}

    def fake_run_provider(spec, retries, timeout, backoff, workdir, *, env=None):
        seen["validator"] = spec.validator
        seen["min_chars"] = spec.min_chars
        seen["cwd"] = spec.cwd
        return {"valid": True, "result_text": "ok", "reason": "ok", "exit_code": 0}

    from forge import seat as seatmod
    fn = cli.launch.make_launcher(prompt="do it", timeout=3600, bundle_sha256="a" * 64,
                                  run_provider=fake_run_provider, probe=_fake_probe)
    fn(name="claude", seat_path=".", token="SENTINEL-x", env={})
    assert seen["validator"] is seatmod._forge_validator
    assert seen["min_chars"] == 0
    assert seen["cwd"] == "."
```

Add it to `tests/test_forge_cli.py`, run it, and confirm it passes.

- [ ] **Step 11: Mutation-test the CLI's refusals**

```
cd /home/khenrix/git/khenrix-utils
git status --short
python3 scripts/mutate.py --file shared/lib/forge/cli.py \
  --old '    if not_portable:' --new '    if False:' \
  --test 'uvx --with pytest pytest -q tests/test_forge_cli.py'
python3 scripts/mutate.py --file shared/lib/forge/cli.py \
  --old '    if blocked:' --new '    if False:' \
  --test 'uvx --with pytest pytest -q tests/test_forge_cli.py'
python3 scripts/mutate.py --file shared/lib/forge/cli.py \
  --old '    for name in storage.seat_names(run_dir):' \
  --new '    for name in []:' \
  --test 'uvx --with pytest pytest -q tests/test_forge_cli.py'
python3 scripts/mutate.py --file shared/lib/forge/cli.py \
  --old '        synthesis_measured=False,' --new '        synthesis_measured=True,' \
  --test 'uvx --with pytest pytest -q tests/test_forge_cli.py'
git status --short
```

Expected: all four `CAUGHT`. The third is the seat-line fail-open: an empty enumeration must
not render as a clean fleet, and `Provenance.__post_init__` (Task 5) is what catches it. The
fourth is the overclaim at its source — a `--collect` that labelled an argv verdict as measured
would print §16.1's "Verified here means" beside a command nobody ran.

- [ ] **Step 12: Render, gate, commit**

```
cd /home/khenrix/git/khenrix-utils
make render
make verify
echo "verify rc=$?"
make precommit
echo "precommit rc=$?"
git add shared/lib/forge/cli.py tests/test_forge_cli.py marketplaces
git commit -m "$(cat <<'EOF'
feat(forge): the front end, and the caller three plans deferred

make_launcher and forge_spec had no production caller since Plan H, so §8.1's validator —
which stops a seat being re-run in the same cwd on top of its own half-finished work — had
never run outside the test suite. run_ultra had none either. --start is the first; --collect
is run_ultra's.

--start stops where runner.run stops, at `comparing`. The choosing is §10-§13 and §16 names
who does it: the synthesis author is the trusted invoking orchestrator, not a fourth
unattended seat. So there is no --synthesize.

The seat table is built from the records on disk and never from runner.run's results tuple:
a seat every attempt of which was refused has no verdict to return, and counting the tuple
reports "2 of 2 seats completed" for a three-seat run — a denominator that shrank to fit its
numerator.

No timeout is invented here. §19 forbids a second mechanism, so a missing
MODE_TIMEOUT["forge"] is a refusal rather than a fallback to deep's review window.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UiV66Pt8cZVMq9t8WEAhpN
EOF
)"
```

Expected: `verify rc=0`, `precommit rc=0`.

---

## Task 7: §15 — `--gc`, and the disk nobody was accounting for

`storage.py:77`, `:91`, `:114` and `:217` all reference a `--gc` walk as an existing contract
and no such code exists; `gate.py:1171` renders the operator-facing sentence *"so `--gc` is
mandatory rather than tidy — and it is not built."* This builds it and deletes that clause.

**Files:**
- Create: `shared/lib/forge/gc.py`
- Modify: `shared/lib/forge/cli.py` (`_gc`)
- Modify: `shared/lib/forge/gate.py` (`GC_UNBUILT` leaves `ACCEPTABLE_GAPS`; the sentence at
  `gate.py:1171` loses "and it is not built")
- Test: `tests/test_forge_gc.py` (new), `tests/test_forge_gate.py`

**Interfaces:**
- Consumes: `storage.run_root(repo_path, run_id, must_be_new=False)`;
  `storage.seat_names(run_dir)`; `storage.manifest_path/journal_path/state_path/
  task_bundle_path/ledger_path/handover_path`; `runstate.read_manifest(run_dir)`;
  `runstate.read_seat(run_dir, name)`; `handover.read_handover(run_dir) -> Handover | None`;
  `handover.branch(run_id, name)`; `gitcmd.git(...)`, `gitcmd.NO_DAEMON_CACHE`,
  `gitcmd.NO_HOOKS`, `gitcmd.READONLY`, `gitcmd.GitError`.
- Produces:
  - `gc.GcError(RuntimeError)`
  - `gc.Usage(run_id: str, path: str, bytes_: int, files: int, handed_over: bool)`
  - `gc.usage(repo) -> tuple[Usage, ...]` — §15's "report total disk held by past runs"
  - `gc.worktrees(repo) -> tuple[dict, ...]` — `git worktree list --porcelain`, parsed
  - `gc.collect(repo, run_id, *, force=False) -> tuple[str, ...]` — what was removed, or the
    refusal
  - `cli._gc(args, *, out) -> int`
- Consumed by: nothing later in this plan; Task 11's SKILL.md documents it.

**The specific fail-opens this must not have:**
1. **Deleting a synthesis worktree or branch that was never handed over.** §15 says so
   outright, and `handover.read_handover` returning `None` means *not handed over* — never
   *could not tell*. A `None` refuses.
2. **`git worktree prune`.** §9 forbids a repo-wide prune. `gc` removes only the worktrees
   whose paths it recognises as this run's, one at a time.
3. **`git worktree unlock` unconditionally.** Measured: rc=128 on an unlocked worktree, so an
   unconditional unlock-then-remove fails on the ordinary case. The unlock is conditional on
   `worktree list --porcelain` reporting `locked` for that path.
4. **A disk report that reads zero for a directory it could not walk.** `OSError` during the
   walk makes that run's `bytes_` unreportable, and an unreportable run is named in the report
   rather than summed as 0 — a total silently missing a 40 GB run is exactly the record §15
   asks for, inverted.
5. **`worktree remove` being read as having removed the branch.** Measured: it does not. The
   branch is a second deletion and a second refusal.
6. **`rmtree`-ing a synthesis worktree git does not have registered.** The `shutil.rmtree` at
   the end deletes the whole run directory, `synthesis/` included. If `worktree list` never
   named that path, removing the directory leaves `.git/worktrees/<name>` in the user's
   repository — and §9 forbids the `worktree prune` that is the only thing that reclaims it, so
   the leak is permanent and nothing says it happened. An unknown-but-present synthesis path
   refuses, and `--force` does not clear it: `force` waives the not-handed-over decision, which
   is the operator's to make, and this is not.
7. **`Usage.handed_over` collapsing "not handed over" with "the record could not be read".**
   The first is an answer, the second is the absence of one, and `--gc all` prints them.

**What input would make this produce a result cleaner than its evidence:** a run directory
whose `handover.json` **exists and is corrupt**. `read_handover` raises (Task 4), and a `gc`
that caught it into `None` would refuse — which looks safe but tells the operator a run they
did hand over is undeletable, with no message saying why. `gc` lets the raise through and
`cli._gc` prints it.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_forge_gc.py`:

```python
"""§15 — cleanup. Every refusal here protects a deliverable that cannot be rebuilt."""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "lib"))

from forge import gc, gitcmd, handover, storage  # noqa: E402


def test_a_synthesis_worktree_that_was_never_handed_over_is_refused(tmp_path, monkeypatch):
    """§15: 'it refuses to delete a synthesis worktree/branch not marked handed over.'"""
    repo, run_dir, synth = _a_run_with_a_synthesis_tree(tmp_path, monkeypatch)
    assert handover.read_handover(run_dir) is None
    with pytest.raises(gc.GcError) as e:
        gc.collect(repo, _run_id(run_dir))
    assert "handed over" in str(e.value)
    assert synth.exists()
    assert "forge/" in _show_ref(repo)


def test_a_patch_handover_the_user_accepted_is_deletable_though_nothing_merged(tmp_path,
                                                                              monkeypatch):
    """§15's own sentence: 'a patch-based handover may intentionally never merge the internal
    branch.' A gc that read the absent merge as unfinished work would make every patch
    delivery permanent."""
    repo, run_dir, synth = _a_run_with_a_synthesis_tree(tmp_path, monkeypatch)
    handover.write_handover(run_dir, handover.Handover(
        run_id=_run_id(run_dir), branch=handover.branch(_run_id(run_dir), "synthesis"),
        kind=handover.PATCH_ONLY, handover_target=None, accepted=True,
        out_of_band=(), baseline_owned=(), b1_files=(), why="user took the patch"))
    removed = gc.collect(repo, _run_id(run_dir))
    assert not synth.exists()
    assert any("worktree" in r for r in removed)
    assert any("branch" in r for r in removed)
    assert "forge/" not in _show_ref(repo)


def test_gc_never_prunes_the_whole_repository(tmp_path):
    """§9 forbids a repo-wide prune. Read off the source, because a behavioural test can only
    show that this fixture's other worktrees survived."""
    src = (ROOT / "shared" / "lib" / "forge" / "gc.py").read_text()
    assert '"prune"' not in src


def test_the_unlock_is_conditional_because_unlocking_an_unlocked_tree_is_rc_128(tmp_path,
                                                                               monkeypatch):
    """Measured on git 2.53.0: `git worktree unlock` on an unlocked worktree exits 128. §9
    prescribes unlock-then-remove for forge-owned trees, and doing it unconditionally fails on
    the ordinary case — so the lock state is read first."""
    repo, run_dir, synth = _a_run_with_a_synthesis_tree(tmp_path, monkeypatch)
    _hand_over(run_dir)
    subprocess.run(["git", "-C", str(repo), "worktree", "lock", str(synth)],
                   check=True, capture_output=True)
    removed = gc.collect(repo, _run_id(run_dir))       # must not raise
    assert not synth.exists()
    assert any("unlock" in r for r in removed)


def test_removing_the_worktree_is_not_removing_the_branch(tmp_path, monkeypatch):
    """Measured: `git worktree remove` leaves the branch. A gc that stopped there would report
    a run cleaned while its branch — and every object under it — stayed."""
    repo, run_dir, synth = _a_run_with_a_synthesis_tree(tmp_path, monkeypatch)
    _hand_over(run_dir)
    gc.collect(repo, _run_id(run_dir))
    assert "forge/" not in _show_ref(repo)


def test_the_disk_report_names_a_run_it_could_not_measure_rather_than_summing_it_as_zero(
        tmp_path, monkeypatch):
    """A total silently missing a 40 GB run is §15's report inverted."""
    repo, run_dir, _ = _a_run_with_a_synthesis_tree(tmp_path, monkeypatch)
    rows = gc.usage(repo)
    assert rows and rows[0].bytes_ > 0 and rows[0].files > 0

    def boom(*a, **kw):
        raise PermissionError("nope")
    monkeypatch.setattr(gc, "_walk_bytes", boom)
    rows = gc.usage(repo)
    assert rows[0].bytes_ is None, "an unwalkable run was reported as holding 0 bytes"


def test_an_unreadable_handover_record_is_not_reported_as_not_handed_over(tmp_path,
                                                                         monkeypatch):
    """"This run was not handed over" is an answer; "this engine could not read the record" is
    the absence of one. Printing them the same way tells an operator that a delivery they made
    is unfinished work, which is the one sentence that gets a deliverable deleted. And both
    reasons survive when the walk ALSO fails — a row that dropped one would name a size problem
    and silently lose a record problem."""
    repo, run_dir, _ = _a_run_with_a_synthesis_tree(tmp_path, monkeypatch)
    storage.handover_path(run_dir).write_bytes(b"{ not json")
    rows = gc.usage(repo)
    assert rows[0].handed_over is None, "an unreadable record answered the licence question"
    assert "could not be read" in rows[0].why

    def boom(*a, **kw):
        raise PermissionError("nope")
    monkeypatch.setattr(gc, "_walk_bytes", boom)
    rows = gc.usage(repo)
    assert rows[0].handed_over is None
    assert "could not be walked" in rows[0].why and "could not be read" in rows[0].why


def test_a_corrupt_handover_record_reaches_the_operator(tmp_path, monkeypatch):
    repo, run_dir, _ = _a_run_with_a_synthesis_tree(tmp_path, monkeypatch)
    storage.handover_path(run_dir).write_bytes(b"{ not json")
    with pytest.raises(handover.HandoverError):
        gc.collect(repo, _run_id(run_dir))


def test_a_synthesis_directory_git_does_not_know_about_is_refused_not_rmtreed(tmp_path,
                                                                             monkeypatch):
    """The `rmtree` at the end of `collect` takes the whole run directory, `synthesis/`
    included. If `worktree list` never named that path, the directory goes and
    `.git/worktrees/<name>` stays registered — and §9 forbids the repo-wide `worktree prune`
    that is the only thing that reclaims it. Permanent, and silent."""
    repo, run_dir, synth = _a_run_with_a_synthesis_tree(tmp_path, monkeypatch)
    _hand_over(run_dir)
    # Deregister the tree without removing the directory, which is exactly the state a killed
    # `--gc` or a hand-edited `.git` leaves behind.
    monkeypatch.setattr(gc, "worktrees", lambda repo_: ())
    with pytest.raises(gc.GcError) as e:
        gc.collect(repo, _run_id(run_dir))
    assert "worktree list" in str(e.value)
    assert synth.exists(), "an unregistered synthesis worktree was deleted anyway"
    assert run_dir.exists()
    # And --force does not clear THIS refusal: it waives the handover decision and nothing else.
    with pytest.raises(gc.GcError):
        gc.collect(repo, _run_id(run_dir), force=True)
```

Write `_a_run_with_a_synthesis_tree`, `_run_id`, `_show_ref` and `_hand_over` in this file;
`_a_run_with_a_synthesis_tree` must `monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))`
before calling `storage.run_root`.

- [ ] **Step 2: Run to verify they fail, then implement `gc.py`**

```
cd /home/khenrix/git/khenrix-utils
uvx --with pytest pytest -q tests/test_forge_gc.py
```

Expected: FAIL at collection.

Create `shared/lib/forge/gc.py`:

```python
"""§15 — cleanup, and the disk accounting that makes it decidable.

`storage.py` has referred to "the `--gc` walk" as an existing contract since it was written,
and `gate.py` renders an operator-facing sentence saying it is mandatory and not built. This is
it. The naming scheme it walks is `storage.run_root`'s, which exists so that "a resume and a
`--gc` walk look for the same names".

WHAT THIS WILL NOT DO. §9 forbids a repo-wide `git worktree prune`, so nothing here prunes: the
worktrees this removes are the ones it can name from a run directory it read. And §15 refuses
to delete a synthesis worktree or branch not marked handed over, which is a refusal on a
MISSING record rather than on a negative one — `handover.read_handover` answers `None` for "not
handed over" and raises for everything else, so an unreadable record reaches the operator
instead of quietly becoming a refusal they cannot explain.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import gitcmd, handover as handovermod, storage


class GcError(RuntimeError):
    """A run this walk will not delete."""


@dataclass(frozen=True)
class Usage:
    """One past run's footprint. §15: "Report total disk held by past runs."

    `bytes_` AND `files` ARE NULLABLE AND A NULL IS NOT A ZERO. A run directory this walk could
    not read is not a run holding nothing — and a total that silently omits a 40 GB run is this
    report inverted. `why` carries every reason the row is incomplete, joined, because a walk
    failure and an unreadable handover record are two independent facts about one run.

    `handed_over` IS NULLABLE FOR THE SAME REASON ONE FIELD OVER. `True` and `False` are
    answers `read_handover` gave; `None` is the state where it raised and there is no answer.
    Reporting an unreadable record as `False` prints "NOT handed over" about a delivery the
    operator may well have made, which is the one sentence that would make them delete it.
    """
    run_id: str
    path: str
    bytes_: int | None
    files: int | None
    handed_over: bool | None
    why: str = ""


def _walk_bytes(root: Path) -> tuple:
    """(bytes, files) under `root`, following no symlink. Raises `OSError` — the caller decides
    what an unreadable run means, because only the caller knows whether it is reporting or
    deleting."""
    total = count = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for f in filenames:
            p = Path(dirpath) / f
            st = p.lstat()
            total += st.st_size
            count += 1
    return total, count


def usage(repo) -> tuple:
    """§15's disk report, one row per past run plus whatever could not be measured.

    THE ENUMERATION IS `storage.run_dirs`'s AND NOT A SECOND COPY OF IT. The naming scheme —
    `XDG_STATE_HOME/khenrix-forge/<sha256(resolved repo)[:12]>-<run-id>` — has exactly one
    home, because two spellings of one predicate will eventually disagree and this one fails
    SILENTLY: if `run_root` moves and this walk does not, `usage()` returns `()` and `--gc all`
    prints "no forge runs are on disk for this repository" over a disk holding all of them.
    """
    rows = []
    for d in storage.run_dirs(repo):
        run_id = d.name.split("-", 1)[1]
        try:
            handed = handovermod.read_handover(d) is not None
        except handovermod.HandoverError as e:
            # `None`, NOT `False`. "This run was not handed over" is a licence question with a
            # real answer; "this engine could not read the record" is not an answer to it, and
            # printing the two the same way tells an operator a delivery they made is
            # unfinished work.
            handed, why_h = None, f"the handover record could not be read ({e})"
        else:
            why_h = ""
        try:
            total, count = _walk_bytes(d)
        except OSError as e:
            # BOTH REASONS SURVIVE. The walk failing does not un-fail the handover read, and a
            # row that dropped `why_h` here would name the size problem and silently lose the
            # record problem — one report, one cause, in a function whose whole job is to name
            # what it could not measure.
            why = (f"this run's directory could not be walked ({e}), so its size is unknown — "
                   "it is named here rather than summed as zero")
            rows.append(Usage(run_id, str(d), None, None, handed,
                              "; ".join(x for x in (why, why_h) if x)))
            continue
        rows.append(Usage(run_id, str(d), total, count, handed, why_h))
    return tuple(rows)


def worktrees(repo) -> tuple:
    """`git worktree list --porcelain`, parsed into one dict per tree.

    Measured (git 2.53.0): the verb fires no hook and loads no index, but it is not on
    `_INDEX_SAFE` or `_HOOK_SAFE` — those sets are read by the FIRST POSITIONAL WORD, and
    `worktree add`/`remove` both fire, so every `worktree` call in this package carries the two
    presets. That costs this read a flag pair it does not need, which is the fail-closed
    direction and is exactly what the closure's own comment says it prefers.
    """
    out = gitcmd.git(repo, *gitcmd.NO_DAEMON_CACHE, *gitcmd.NO_HOOKS,
                     "worktree", "list", "--porcelain",
                     env_extra=gitcmd.READONLY).stdout
    trees, cur = [], {}
    for line in out.splitlines():
        if not line.strip():
            if cur:
                trees.append(cur)
            cur = {}
            continue
        key, _, value = line.partition(" ")
        cur[key] = value
    if cur:
        trees.append(cur)
    return tuple(trees)


def collect(repo, run_id: str, *, force: bool = False) -> tuple:
    """§15's cleanup for one run. Returns what was removed, or raises rather than removing it.

    THE REFUSAL IS ON A MISSING RECORD, NOT A NEGATIVE ONE. `handover.read_handover` answers
    `None` only for a run that has no `handover.json`; every other failure raises out of this
    function and reaches the operator. A `gc` that caught that raise would refuse — which looks
    like the safe direction and is not: the operator would find a run they did hand over become
    undeletable, with nothing saying why.

    `force` IS NOT AN OVERRIDE OF THAT. It skips the not-handed-over refusal and nothing else,
    because §15's refusal exists to protect a deliverable that cannot be rebuilt and an
    operator who types `--force` has said they know that. Every other refusal below is about
    this engine not being able to describe what it would delete, and no flag clears one.
    """
    run_dir = storage.run_root(repo, run_id, must_be_new=False)
    if not storage.manifest_path(run_dir).exists():
        raise GcError(
            f"{run_dir} records no manifest, so this walk cannot say what run it would be "
            "deleting. §15's cleanup is per-run and a directory with no identity is not one.")
    h = handovermod.read_handover(run_dir)
    if h is None and not force:
        raise GcError(
            f"run {run_id} has no handover record, so its synthesis worktree and branch are "
            "not marked handed over and §15 refuses to delete them. A patch-based handover "
            "that will never merge is still a handover — record it with "
            "`--collect <run-id> --accept`, or pass --force if the work is genuinely "
            "abandoned.")
    removed = []
    b = handovermod.branch(run_id, handovermod.SYNTHESIS)
    known = {t.get("worktree") for t in worktrees(repo)}
    synth = run_dir / "synthesis"
    if synth.exists() and str(synth) not in known:
        # A PATH THIS ENGINE CANNOT NAME IS NOT A PATH IT MAY DELETE. Falling through to the
        # `rmtree` below would remove the directory while `.git/worktrees/<name>` stays
        # registered in the user's repository — and §9 forbids the `worktree prune` that is the
        # only thing that reclaims it, so the leak is permanent and silent. `worktree list`
        # not naming a directory that exists is a state this walk does not understand, and
        # `--force` does not clear it: `force` waives the not-handed-over refusal, which is a
        # decision the operator can make, and this is not one.
        raise GcError(
            f"{synth} exists and `git worktree list` in {repo} does not name it. This walk "
            "will not delete a worktree it cannot see registered: removing the directory "
            "would leave the admin entry behind, and §9 forbids the repo-wide `worktree "
            "prune` that would reclaim it. Re-register or remove it by hand, then re-run.")
    if str(synth) in known:
        # UNLOCK ONLY IF LOCKED. Measured on git 2.53.0: `worktree unlock` on an unlocked tree
        # is rc=128, so §9's unlock-then-remove done unconditionally fails on the ordinary
        # case. The lock state comes from the porcelain listing rather than from a try/except,
        # because catching a 128 would also catch the ones that mean something else.
        locked = any(t.get("worktree") == str(synth) and "locked" in t
                     for t in worktrees(repo))
        if locked:
            gitcmd.git(repo, *gitcmd.NO_DAEMON_CACHE, *gitcmd.NO_HOOKS,
                       "worktree", "unlock", str(synth), env_extra=gitcmd.READONLY)
            removed.append(f"unlock {synth}")
        gitcmd.git(repo, *gitcmd.NO_DAEMON_CACHE, *gitcmd.NO_HOOKS,
                   "worktree", "remove", str(synth), env_extra=gitcmd.READONLY)
        removed.append(f"worktree {synth}")
    # A SECOND DELETION, BECAUSE `worktree remove` DOES NOT DO IT. Measured: the branch survives
    # every removal. A walk that stopped above would report a run cleaned while its branch — and
    # every object reachable from it — stayed in the user's repository forever.
    try:
        gitcmd.git(repo, *gitcmd.NO_DAEMON_CACHE, *gitcmd.NO_HOOKS,
                   "update-ref", "-d", f"refs/heads/{b}", env_extra=gitcmd.READONLY)
        removed.append(f"branch {b}")
    except gitcmd.GitError:
        # The branch is already gone. Recorded rather than silent: "there was nothing to
        # delete" and "the deletion happened" are different facts about this run.
        removed.append(f"branch {b} was already absent")
    shutil.rmtree(run_dir)
    removed.append(f"run directory {run_dir}")
    return tuple(removed)
```

Add to `shared/lib/forge/storage.py`, **immediately beside `run_root` and sharing its
arithmetic** — read `run_root` first and refactor both onto one digest helper rather than
writing a second expression that happens to agree today:

```python
def run_digest(repo_path) -> str:
    """The twelve hex characters that separate one repository's runs from another's.

    ONE SPELLING, READ BY THE WRITER AND BY THE WALK. `run_root` builds a path with it and
    `run_dirs` enumerates with it, and if those were two expressions the failure mode would be
    silent in the worst direction: the walk would find nothing, `--gc all` would report "no
    forge runs are on disk for this repository", and every run would stay on disk unnamed.
    """
    return hashlib.sha256(str(Path(repo_path).resolve()).encode()).hexdigest()[:12]


def run_dirs(repo_path) -> tuple[Path, ...]:
    """Every run directory this engine has recorded for `repo_path`, oldest name first.

    `()` for a state directory that does not exist, which is genuinely "no runs" — the walk
    over an EXISTING directory that cannot be read raises, because that is a different fact
    and `--gc all` has to be able to say which one it met.
    """
    base = state_root() / "khenrix-forge"
    if not base.is_dir():
        return ()
    return tuple(sorted(p for p in base.iterdir()
                        if p.is_dir() and p.name.startswith(run_digest(repo_path) + "-")))
```

and factor `state_root()` (`XDG_STATE_HOME` or `~/.local/state`) out of `run_root` so all three
read it. **`run_root`'s body must then call these**, or the split has added a spelling instead
of removing one — the whole point of this change.

Append to `tests/test_forge_seams.py`:

```python
def test_the_run_directory_naming_scheme_has_one_home():
    """`gc` walks the directories `storage` writes. A second copy of the formula in `gc.py`
    was measured byte-identical to `storage.run_root`'s — which is exactly when a duplicate is
    most dangerous, because nothing fails until one of them moves and then `--gc all` reports
    an empty disk over a full one."""
    src = (ROOT / "shared" / "lib" / "forge" / "gc.py").read_text()
    assert "sha256" not in src, "gc re-derives the run-directory digest instead of asking"
    assert "XDG_STATE_HOME" not in src, "gc re-derives the state root instead of asking"
    assert "storage.run_dirs(" in src
```

- [ ] **Step 3: Wire `_gc` into the CLI**

Append to `shared/lib/forge/cli.py`:

```python
def _gc(args, *, out) -> int:
    """§15's cleanup, or its disk report when the run id is `all`."""
    from . import gc as gcmod
    repo = Path(args.repo).resolve()
    if args.gc == "all":
        rows = gcmod.usage(repo)
        if not rows:
            print("no forge runs are on disk for this repository", file=out)
            return 0
        total = sum(r.bytes_ for r in rows if r.bytes_ is not None)
        unknown = [r for r in rows if r.bytes_ is None]
        for r in rows:
            size = "unknown" if r.bytes_ is None else f"{r.bytes_ / 1e9:.2f} GB"
            # THREE MARKS, BECAUSE THERE ARE THREE STATES. `handed_over is None` is a record
            # this engine could not read, and printing it as "NOT handed over" would tell an
            # operator that a delivery they made is unfinished work — the one sentence that
            # gets a deliverable deleted.
            mark = ("handed over" if r.handed_over is True else
                    "NOT handed over" if r.handed_over is False else
                    "handover record UNREADABLE")
            print(f"  {r.run_id}  {size:>12}  {mark}"
                  + (f"  — {r.why}" if r.why else ""), file=out)
        print(f"  total: {total / 1e9:.2f} GB over {len(rows) - len(unknown)} run(s)", file=out)
        if unknown:
            # THE TOTAL IS NOT THE WHOLE ANSWER AND SAYS SO. A sum that silently omitted an
            # unwalkable run would be the one number an operator acts on, quietly short.
            print(f"  {len(unknown)} run(s) could not be measured and are NOT in that total",
                  file=out)
        return 0
    for line in gcmod.collect(repo, args.gc, force=args.force):
        print(f"  removed: {line}", file=out)
    return 0
```

Add `gc.GcError` to `main`'s narrow `except` tuple.

- [ ] **Step 4: Close the acknowledged gap in `gate.py`**

`gate.GC_UNBUILT` is in `ACCEPTABLE_GAPS` and `gate.py:1171` renders *"so `--gc` is mandatory
rather than tidy — and it is not built."* Both are now false.

1. Remove `GC_UNBUILT` from `ACCEPTABLE_GAPS`. **Leave the constant defined** with a comment
   saying it was retired and when — a run confirmed before this commit has the string in its
   journalled `accepted_gaps` and `--collect` must still be able to read it back.
2. Rewrite the sentence at `gate.py:1171` to drop "and it is not built" and to name the
   command: ``so `--gc <run-id>` is mandatory rather than tidy``.
3. Update the `test_forge_gate.py` test that asserts the sentence — **find it by searching for
   the phrase, do not assume its name.**

- [ ] **Step 5: Run everything, mutation-test, commit**

```
cd /home/khenrix/git/khenrix-utils
uvx --with pytest pytest -q tests/
git status --short
python3 scripts/mutate.py --file shared/lib/forge/gc.py \
  --old '    if h is None and not force:' --new '    if False:' \
  --test 'uvx --with pytest pytest -q tests/test_forge_gc.py'
python3 scripts/mutate.py --file shared/lib/forge/gc.py \
  --old '        if locked:' --new '        if True:' \
  --test 'uvx --with pytest pytest -q tests/test_forge_gc.py'
python3 scripts/mutate.py --file shared/lib/forge/gc.py \
  --old '            rows.append(Usage(run_id, str(d), None, None, handed,' \
  --new '            rows.append(Usage(run_id, str(d), 0, 0, handed,' \
  --test 'uvx --with pytest pytest -q tests/test_forge_gc.py'
python3 scripts/mutate.py --file shared/lib/forge/gc.py \
  --old '            handed, why_h = None, f"the handover record could not be read ({e})"' \
  --new '            handed, why_h = False, f"the handover record could not be read ({e})"' \
  --test 'uvx --with pytest pytest -q tests/test_forge_gc.py'
python3 scripts/mutate.py --file shared/lib/forge/gc.py \
  --old '    if synth.exists() and str(synth) not in known:' --new '    if False:' \
  --test 'uvx --with pytest pytest -q tests/test_forge_gc.py'
git status --short
make render
make verify
echo "verify rc=$?"
make precommit
echo "precommit rc=$?"
git add shared/lib/forge/gc.py shared/lib/forge/cli.py shared/lib/forge/gate.py \
        shared/lib/forge/storage.py tests/test_forge_gc.py tests/test_forge_gate.py \
        tests/test_forge_seams.py marketplaces
git commit -m "$(cat <<'EOF'
feat(forge): §15's --gc, which storage.py has cited as an existing contract since it was written

gate.py rendered "so --gc is mandatory rather than tidy — and it is not built" to the
operator. It is built. GC_UNBUILT leaves ACCEPTABLE_GAPS; the constant stays defined,
because a run confirmed before this commit carries the string in its journalled
accepted_gaps and --collect must still read it back.

Three measurements shape it. `git worktree unlock` on an UNLOCKED tree is rc=128, so §9's
unlock-then-remove done unconditionally fails on the ordinary case — the lock state comes
from `worktree list --porcelain`, not from catching a 128 that could mean something else.
`worktree remove` does NOT delete the branch, so that is a second deletion; a walk that
stopped at the tree would report a run cleaned while every object under its branch stayed.
And nothing prunes: §9 forbids a repo-wide prune, so this removes only trees it can name
from a run directory it read.

The disk report reports a run it could not walk as UNKNOWN and says the total excludes it.
A sum silently short by one 40 GB run is the report §15 asks for, inverted. Its
handed_over is likewise nullable: an unreadable handover record is not a run that was never
handed over, and printing the two alike is the sentence that gets a deliverable deleted.

Two things this walk refuses to touch. A synthesis directory git does not have registered:
rmtree'ing it would leave the .git/worktrees entry behind, and §9 forbids the prune that
reclaims it — --force does not clear that, since force waives the handover decision and
nothing else. And the run-directory naming scheme is storage.run_dirs's alone; the second
copy this task first drafted was byte-identical, which is when a duplicate is most
dangerous — nothing fails until one moves, and then --gc all reports an empty disk.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UiV66Pt8cZVMq9t8WEAhpN
EOF
)"
```

Expected: all three mutations `CAUGHT`; `verify rc=0`, `precommit rc=0`.

---

## Task 8: `reviewer_env` — the three variables the docstring names and does not neutralise

`review.reviewer_env`'s own docstring records the residual: `gitcmd.HOSTILE_ENV` is not
stripped for reviewers and `LLM_FORGE_DEPTH` is not set.

**`PYTHONPYCACHEPREFIX` IS ALREADY CLOSED AND THIS TASK MUST NOT REOPEN IT.** Measured on the
current tree: `review.py:1177` is `env.pop("PYTHONPYCACHEPREFIX", None)`, and the docstring
above it argues the choice — CPython treats an unset value as "use the default beside the
source", which is the one location `_SKIP_DIRS` no longer exempts, so **dropping is stronger
than pinning**. An earlier draft of this task required the variable be *set* to "a per-round
directory this engine created under the run directory", which is both a regression and
unimplementable in the signature this task produces, since `reviewer_env` is never handed a run
directory. Keep the drop. The behavioural test below — plant a `.pyc` in an ambient prefix tree
and prove the reviewer loads source — is still exactly the right test; only the environment
assertion beside it changes, from "set to something else" to "not present".

**What is genuinely open is the brief's three: `PATH`, `PYTHONPATH`/`PYTHONHOME`, and
`NODE_OPTIONS`**, plus `gitcmd.HOSTILE_ENV`. Note that `claude` is Node, `codex` is a static
musl binary and `agy` is Go — **so `NODE_OPTIONS` reaches one of three reviewers, not three.** A
comment claiming it protects all three would be a comment asserting something the code does not
do. (`review.py`'s current docstring already gets this right, per-seat and measured; the
rewrite must keep that specificity rather than replacing it with a class claim.)

**Files:**
- Modify: `shared/lib/forge/review.py` (`reviewer_env`)
- Test: `tests/test_forge_review.py`

**Interfaces:**
- Consumes: `gitcmd.HOSTILE_ENV`, `gitcmd.NO_USER_CONFIG`; `fleet.forge_child_env(repo_path,
  env=None) -> dict` and `fleet.scrub_env(env, repo_path) -> dict` — **read both before
  writing this; if `scrub_env` already does what this task needs, call it rather than
  restating it.** A second spelling of the scrub is the defect this task is fixing.
- Produces: `review.reviewer_env(base=None, *, repo_path=None) -> dict` with `PATH` rebuilt,
  `PYTHONPATH`/`NODE_OPTIONS` emptied, `PYTHONHOME` deleted and `gitcmd.HOSTILE_ENV` stripped,
  plus the existing `PYTHONDONTWRITEBYTECODE`, `PYTEST_ADDOPTS` and **`PYTHONPYCACHEPREFIX`
  drop** behaviour unchanged. `repo_path` is optional and exists only so the `HOSTILE_ENV`
  strip can go through `fleet.scrub_env` where that function already does the right thing —
  **read `fleet.scrub_env`'s body first**; it drops values pointing back into the ORIGINAL
  checkout, which is a different direction from a reviewer being redirected somewhere else, so
  it may turn out to be the wrong function to call and the strip belongs here spelled once.

**The specific fail-opens this must not have:**
1. **Deleting `PATH`.** A reviewer with no `PATH` cannot find `git`, `make` or its own
   toolchain, and the round fails for a reason that reads as a provider failure. `PATH` is
   **rebuilt from a measured absolute set**, not removed — and the function refuses if it
   cannot resolve the three CLI binaries under the rebuilt value, because a silently
   unusable `PATH` costs a paid round.
2. **A comment claiming `NODE_OPTIONS` protects all three reviewers.** It reaches `claude`
   only. Say so.
3. **Two spellings of the scrub.** `fleet.forge_child_env` exists for seats; if this function
   restates its list, the two will diverge. Import the list.
4. **A test that asserts a signature rather than a behaviour.** Plan I₂'s own lesson: "a
   signature check cannot see 'accepted and dropped'". Every assertion below watches what a
   child was **handed**.

**What input would make this produce a result cleaner than its evidence:** an ambient
`PYTHONPYCACHEPREFIX` pointing at a tree the reviewer can write. `PYTHONDONTWRITEBYTECODE=1`
stops **writes, not reads**, so a `.pyc` already planted in that mirrored tree is still loaded
in preference to untouched source — and the in-tree bracket `worktree_identity` takes cannot
see it, because the tree is outside the checkout. **The variable being DROPPED is the fix and
it is already in the tree**; what this task adds is the test that keeps it there. **The test
must plant a `.pyc` in the ambient prefix and prove the reviewer loads source instead** —
because an assertion about the environment dict alone cannot tell a drop from a redirect to
another writable tree, and only one of those is safe.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_forge_review.py`:

```python
def test_a_reviewer_cannot_inherit_a_path_that_puts_a_planted_binary_first(tmp_path,
                                                                          monkeypatch):
    """A write-capable reviewer with an inherited PATH runs whatever is first on it. The fix
    is a REBUILT PATH, not a deleted one: a reviewer that cannot find git fails the round for
    a reason that reads as a provider failure."""
    evil = tmp_path / "evil"
    evil.mkdir()
    (evil / "git").write_text("#!/bin/sh\nexit 0\n")
    (evil / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{evil}:{os.environ['PATH']}")
    env = review.reviewer_env()
    assert str(evil) not in env["PATH"]
    assert env["PATH"], "PATH was removed rather than rebuilt"
    assert shutil.which("git", path=env["PATH"]), "the rebuilt PATH cannot find git"


def test_the_three_interpreter_variables_are_neutralised(monkeypatch, tmp_path):
    for var, value in (("PYTHONPATH", str(tmp_path)), ("PYTHONHOME", str(tmp_path)),
                       ("NODE_OPTIONS", "--require /tmp/evil.js")):
        monkeypatch.setenv(var, value)
    env = review.reviewer_env()
    assert env.get("PYTHONPATH", "") == ""
    assert "PYTHONHOME" not in env
    assert env.get("NODE_OPTIONS", "") == ""


def test_an_ambient_pycache_prefix_cannot_serve_a_planted_pyc_to_a_reviewer(tmp_path,
                                                                           monkeypatch):
    """Measured on this project: with PYTHONDONTWRITEBYTECODE=1 set, `-B` stops WRITES, not
    READS — a .pyc planted in the mirrored prefix tree, entirely outside the checkout, is
    still loaded in preference to untouched source, and the in-tree bracket cannot see it.

    DROPPED, NOT REPOINTED. `reviewer_env` already pops the variable and its docstring gives
    the reason: CPython reads an unset value as "use the default beside the source", which is
    the one location `_SKIP_DIRS` no longer exempts. Pinning it to some other directory would
    put the reviewers back on a shared tree, differing from the ambient one only in who chose
    it. The env assertion below is therefore ABSENCE, and the behavioural half underneath it is
    what makes the difference between the two visible at all."""
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    monkeypatch.setenv("PYTHONPYCACHEPREFIX", str(prefix))
    env = review.reviewer_env()
    assert "PYTHONPYCACHEPREFIX" not in env

    src = tmp_path / "m.py"
    src.write_text("VALUE = 'SOURCE'\n")
    _plant_a_pyc_under(prefix, src, "VALUE = 'PLANTED'\n")   # helper below
    got = subprocess.run([sys.executable, "-c",
                          "import sys; sys.path.insert(0, %r); import m; print(m.VALUE)"
                          % str(tmp_path)],
                         env={**env, "PATH": env["PATH"]}, capture_output=True, text=True)
    assert got.stdout.strip() == "SOURCE", got


def test_the_hostile_git_variables_are_stripped_for_a_reviewer_too(monkeypatch, tmp_path):
    """`fleet.forge_child_env` closes this for SEATS. A write-capable reviewer with a shell
    under an ambient GIT_DIR is the same hazard, and reviewer_env's own docstring said so
    while not doing it."""
    monkeypatch.setenv("GIT_DIR", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    env = review.reviewer_env()
    for var in ("GIT_DIR", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0"):
        assert var not in env, var


def test_node_options_reaches_one_reviewer_of_three_and_the_comment_says_so():
    """`claude` is Node; `codex` is a static musl binary and `agy` is Go. A comment claiming
    this variable protects all three would be a comment asserting something the code does not
    do — which this project calls a defect."""
    src = (ROOT / "shared" / "lib" / "forge" / "review.py").read_text()
    i = src.index("NODE_OPTIONS")
    window = src[max(0, i - 1200): i + 1200]
    assert "one of the three" in window or "claude" in window.lower()


def test_the_environment_reaches_all_three_children_rather_than_being_accepted_and_dropped(
        monkeypatch):
    """A signature check cannot see 'accepted and dropped'. This watches what each of three
    children is handed at run_member."""
    handed = []
    monkeypatch.setattr(engine, "run_member",
                        lambda argv, **kw: handed.append(kw.get("env")) or _ok_cp())
    review.run_round(...)      # drive the real run_round with the panel faked, as
                               # test_the_panel_is_launched_under_that_environment already does
    assert len(handed) == 3
    for env in handed:
        assert env.get("PYTHONPATH", "") == ""
        assert "GIT_DIR" not in env
```

**Read `tests/test_forge_review.py`'s existing
`test_the_panel_is_launched_under_that_environment` and
`test_run_council_hands_its_callers_environment_to_every_member` first and reuse their driving
machinery for the last test** — they already solve the "watch what the child was handed"
problem, and a second solution is a second thing to keep right.

- [ ] **Step 2: Run to verify they fail, then implement**

```
cd /home/khenrix/git/khenrix-utils
uvx --with pytest pytest -q tests/test_forge_review.py -k "path or interpreter or pycache or hostile or node_options"
```

Expected: FAIL on all of them.

Rewrite `review.reviewer_env` to:

1. Start from `{k: v for k, v in (base or os.environ).items() if k not in gitcmd.HOSTILE_ENV}`
   and apply `gitcmd.NO_USER_CONFIG`. **If `fleet.scrub_env` already produces exactly this,
   call it** — the scrub has one home.
2. Rebuild `PATH` from `os.defpath` plus the resolved parent directory of each of the three CLI
   binaries as they resolve **under the caller's own PATH at call time** (that resolution is
   the engine's, not the reviewer's), and **raise** if none of the three resolves — a `PATH`
   that cannot find a reviewer's own binary costs a paid round to discover.
3. `PYTHONPATH=""`, `NODE_OPTIONS=""`, and **delete** `PYTHONHOME` (an empty `PYTHONHOME`
   is not neutral — it is a prefix of `""`; measure this before choosing, and record what you
   measured in the comment).
4. **Leave `PYTHONPYCACHEPREFIX` exactly as it is** — `env.pop(..., None)`, dropped and not
   repointed. It is already closed; the change here is the test, not the code. Repointing it
   at a directory this engine chose would put the three reviewers back on one shared tree,
   differing from the ambient hazard only in who picked the path.
5. Keep `PYTHONDONTWRITEBYTECODE=1` and the `PYTEST_ADDOPTS` **append** exactly as they are.
6. Update the docstring: delete only the clauses that are no longer true — the `HOSTILE_ENV`
   sentence and the `PATH`/`PYTHONPATH`/`NODE_OPTIONS` "none of them is closed here" — and
   **keep** the `PYTHONPYCACHEPREFIX` paragraph and the per-seat `NODE_OPTIONS` measurement,
   which are correct and stay correct. State plainly that `NODE_OPTIONS` reaches **claude
   only**, because codex is a static musl binary and agy is Go.

- [ ] **Step 3: Run, mutate, commit**

```
cd /home/khenrix/git/khenrix-utils
uvx --with pytest pytest -q tests/
git status --short
python3 scripts/mutate.py --file shared/lib/forge/review.py \
  --old 'env["PYTHONPATH"] = ""' --new 'pass' \
  --test 'uvx --with pytest pytest -q tests/test_forge_review.py'
python3 scripts/mutate.py --file shared/lib/forge/review.py \
  --old 'env["NODE_OPTIONS"] = ""' --new 'pass' \
  --test 'uvx --with pytest pytest -q tests/test_forge_review.py'
git status --short
make render
make verify
echo "verify rc=$?"
make precommit
echo "precommit rc=$?"
git add shared/lib/forge/review.py tests/test_forge_review.py marketplaces
git commit -m "$(cat <<'EOF'
fix(forge): reviewer_env named three variables it did not neutralise, in its own docstring

PATH, PYTHONPATH/PYTHONHOME and NODE_OPTIONS all reached a write-capable reviewer, alongside
gitcmd.HOSTILE_ENV, which fleet.forge_child_env closes for seats and this closed for nobody.
PATH is REBUILT rather than removed and the function raises if the rebuilt value resolves
none of the three CLIs — a reviewer that cannot find git fails the round for a reason that
reads as a provider failure, and the round is paid for.

PYTHONPYCACHEPREFIX is NOT changed: it was already dropped, and dropping beats pinning
because CPython reads an unset value as "use the default beside the source", which is the
one location _SKIP_DIRS no longer exempts. What is new is the behavioural test — plant a
.pyc in an ambient prefix tree and prove the reviewer loads source — because -B stops
writes, not reads, and an assertion about the env dict alone cannot tell a drop from a
redirect to another writable tree.

NODE_OPTIONS reaches claude and no other reviewer: codex is a static musl binary and agy is
Go. The comment says so, because a comment asserting something the code does not do is a
defect.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UiV66Pt8cZVMq9t8WEAhpN
EOF
)"
```

Expected: both mutations `CAUGHT`; `verify rc=0`, `precommit rc=0`.

---

## Task 9: §19 — the forge timeout and agy's own word for a wall

**This task is its own commit and its own gate, and neither is the judge harness.**
`shared/lib/council/engine.py` is in `llm-council`'s source closure
(`checks.SKILL_EXTRA_DIRS["llm-council"] = ["shared/lib/council"]`), so `make precommit` fails
on a stale receipt, and that receipt is earned by `fanout.py --self-test` **plus a live
`--smoke` that spends real provider calls** — measured at **$0.2172** on 2026-08-03. Reseeding
it instead is the debt the last plan left owed and had to pay.

**Files:**
- Modify: `shared/lib/council/engine.py`
- Test: `shared/lib/council/engine.py`'s own `_self_test()`, plus `tests/test_council_seams.py`

**Interfaces:**
- Consumes: `engine.MODE_TIMEOUT`, `engine.STRUCTURED_TERMINAL_REASONS`,
  `engine.classify_sentinel`, `engine.evaluate`'s `agy_structured_error` branch
  (`engine.py:1096-1107`), `engine.REASON_HINTS`.
- Produces: `engine.MODE_TIMEOUT["forge"] = 3600`; `engine.AGY_STRUCTURED_TIMEOUT` (a list of
  agy's own timeout phrasings); the `agy_structured_error` branch maps them to reason
  `timeout` with `structured=True`. Task 6's `cli._resolve_seat_timeout` reads the first.

**The specific fail-opens this must not have:**
1. **Adding `timeout` to `STRUCTURED_TERMINAL_REASONS`.** §19 states the consequence: it would
   **silently remove council's timeout retries**. The spec requires a comment at the mapping
   site saying so, and this task writes it. A test asserts the set still has exactly one
   member.
2. **Putting agy's timeout phrases on `TOOL_PERMISSION_SENTINELS` or any merged-stderr list.**
   The engine has already lost a seat to a phantom that way — twice, recorded at
   `AGY_STRUCTURED_TOOL_PERMISSION`. These are **structured-only**, scanned against agy's own
   `error` field.
3. **Re-adding a cap.** §19: "An implementer must **not** re-add a cap or build a second
   timeout mechanism." A test asserts no new integer literal near the agy print-timeout site.

**What input would make this produce a result cleaner than its evidence:** an agy `error` field
whose text contains a timeout phrase **and** an auth phrase (a quota wall reported as a timeout
waiting for a response). `classify_sentinel` is consulted first in the existing branch and
returns `auth_or_quota`, which **is** terminal — so ordering the timeout check before it turns
a real quota wall into a retried timeout, spending three attempts on a wall. **The timeout
mapping goes after `classify_sentinel`, not before**, and a test carries both phrases in one
payload.

- [ ] **Step 1: Write the failing checks in `engine._self_test()`**

```python
    # §19 — forge's window and agy's own word for a wall.
    check("MODE_TIMEOUT has a forge entry of at least 3600",
          MODE_TIMEOUT.get("forge", 0) >= 3600)
    check("deep is unchanged", MODE_TIMEOUT["deep"] == 1200)
    _agy_timeout = json.dumps({"status": "ERROR",
                               "error": "timeout waiting for response from the model"})
    _v, _r, _t, _s = evaluate(0, _agy_timeout, "", _agy_spec)
    check("agy: a structured timeout maps to reason `timeout` with provenance",
          _v is False and _r == "timeout" and _s is True)
    check("agy: a structured timeout is NOT terminal, so council keeps its retries",
          "timeout" not in STRUCTURED_TERMINAL_REASONS)
    check("STRUCTURED_TERMINAL_REASONS still has exactly one member",
          STRUCTURED_TERMINAL_REASONS == {"auth_or_quota"})
    # A quota wall REPORTED as a timeout is a quota wall. classify_sentinel runs first and
    # auth_or_quota IS terminal; ordering the timeout map ahead of it would retry a wall
    # three times.
    _both = json.dumps({"status": "ERROR",
                        "error": "timeout waiting for response: RESOURCE_EXHAUSTED, "
                                 "individual quota reached"})
    _v2, _r2, _t2, _s2 = evaluate(0, _both, "", _agy_spec)
    check("agy: a wall reported as a timeout is still auth_or_quota", _r2 == "auth_or_quota")
    check("every reason the engine can emit carries a hint", "timeout" in REASON_HINTS)
```

**Read the existing `_agy_spec` fixture in `_self_test` and reuse it**; if it does not exist
under that name, find the one the D6 checks around `engine.py:1902` use.

- [ ] **Step 2: Run to verify it fails, then implement**

```
cd /home/khenrix/git/khenrix-utils
python3 shared/skills/llm-council/scripts/fanout.py --self-test
```

Expected: FAIL on the forge-entry and timeout-mapping checks.

Implement:

```python
MODE_TIMEOUT = {"normal": 300, "deep": 1200, "forge": 3600}  # per-attempt seconds
```

with a comment recording why `forge` is an hour: §19's history — during round 1 of the design's
own review claude ran 533 s and codex 876 s on a review prompt, and a forge seat does the whole
task, not a review of it. And beside `AGY_STRUCTURED_TOOL_PERMISSION`:

```python
# agy's own words for "I gave up waiting", read out of its structured `error` field and
# NOWHERE ELSE. STRUCTURED-ONLY on the same argument as the tool-permission list above: these
# are ordinary English phrases, and a seat reviewing this repository echoes this very list into
# its merged stderr — which is how this engine lost a seat to a phantom, twice.
AGY_STRUCTURED_TIMEOUT = [
    "timeout waiting for response",
    "deadline exceeded",
]
```

and in `evaluate`'s `agy_structured_error` branch, **after** the `classify_sentinel` call and
**before** the `agy_error` fallback:

```python
        sent = classify_sentinel(result_text)
        if sent:
            # A QUOTA WALL REPORTED AS A TIMEOUT IS A QUOTA WALL, and this ordering is why.
            # `auth_or_quota` IS in STRUCTURED_TERMINAL_REASONS; a timeout is not. Mapping the
            # timeout phrase first would turn a wall into three retried attempts.
            return False, sent, result_text, True
        if any(p in low for p in AGY_STRUCTURED_TIMEOUT):
            # §19: DO NOT ADD `timeout` TO `STRUCTURED_TERMINAL_REASONS`. `run_provider`
            # terminates only on `structured and reason in STRUCTURED_TERMINAL_REASONS`, so
            # putting it there would SILENTLY REMOVE COUNCIL'S TIMEOUT RETRIES — a seat that
            # rode a slow window once would lose its remaining attempts, which is exactly the
            # failure the 120 s cap caused before it was removed. `structured=True` here buys
            # the PROVENANCE (this is agy speaking about itself, not a file it read), not
            # terminality.
            return False, "timeout", result_text, True
        return False, "agy_error", result_text, True
```

**Read `engine.py:1096-1107` and adapt this to the branch that is actually there** — the
existing code already calls `classify_sentinel(result_text) or "agy_error"` in one expression,
so this is a restructuring of that line, not an insertion above it.

- [ ] **Step 3: Add the no-second-cap regression to `tests/test_council_seams.py`**

```python
def test_no_second_timeout_mechanism_was_added_beside_agys_print_timeout():
    """§19: 'An implementer must not re-add a cap or build a second timeout mechanism.' The
    engine computes `pt = max(5, int(timeout) - 5)`; this asserts that is still the only
    arithmetic deciding agy's print-timeout."""
    src = (ROOT / "shared" / "lib" / "council" / "engine.py").read_text()
    assert "max(5, int(timeout) - 5)" in src
    assert "print-timeout" in src
    i = src.index("max(5, int(timeout) - 5)")
    window = src[max(0, i - 400): i + 400]
    assert not re.search(r"min\(\s*\d{2,}", window), (
        "a numeric ceiling reappeared beside the print-timeout computation")
```

- [ ] **Step 4: Run the self-test, the suite, and the LIVE smoke**

```
cd /home/khenrix/git/khenrix-utils
python3 shared/skills/llm-council/scripts/fanout.py --self-test
echo "self-test rc=$?"
uvx --with pytest pytest -q tests/
```

Expected: self-test rc=0 with the six new checks green; suite PASS.

**Then the paid gate. This costs roughly $0.22 and needs auth; it is not optional and it is
not the judge harness.** `docs/skill-eval-process.md` makes it llm-council's receipt gate, and
the previous plan left it owed and the controller had to pay it.

```
cd /home/khenrix/git/khenrix-utils
make smoke-llm-council
echo "smoke rc=$?"
```

Expected: `smoke rc=0` with a PASS line naming the model and the cost. **Record the model, the
duration and the dollar figure in the commit message** — a documented gate left owed is a debt.

- [ ] **Step 5: Re-earn the receipt and commit**

```
cd /home/khenrix/git/khenrix-utils
make verify
echo "verify rc=$?"
```

Expected: rc=0 with one advisory warning: `(advisory) receipt: llm-council changed since last
eval`.

```
cd /home/khenrix/git/khenrix-utils
python3 scripts/eval_harness.py --seed-receipt --skill llm-council
```

`_write_receipt` runs `fanout.py --self-test` itself and refuses to write on a failure, so the
receipt is earned by the self-test; the live smoke you just ran is the other half and its
result belongs in the commit message, not in the receipt file.

```
cd /home/khenrix/git/khenrix-utils
make render
make verify
echo "verify rc=$?"
make precommit
echo "precommit rc=$?"
git add shared/lib/council/engine.py tests/test_council_seams.py evals/llm-council marketplaces
git commit -m "$(cat <<'EOF'
feat(council): §19 — a forge window, and agy's own word for a wall read as a timeout

MODE_TIMEOUT gains `forge: 3600`. §19's history is why: a review prompt ran claude 533s and
codex 876s, and a forge seat does the whole task rather than a review of it.

agy's structured timeout phrasings map to reason `timeout` with structured provenance. They
are STRUCTURED-ONLY — scanned against agy's own `error` field, never the merged stderr —
because these are ordinary English phrases and a seat reviewing this repository echoes this
very list into that stream, which is how the engine lost a seat to a phantom twice.

`timeout` is deliberately NOT in STRUCTURED_TERMINAL_REASONS, with the comment §19 asks for
at the mapping site: run_provider terminates only on a structured reason in that set, so
adding it would silently remove council's timeout retries — the failure the removed 120s cap
caused. The mapping runs AFTER classify_sentinel, so a quota wall reported as a timeout is
still auth_or_quota and is still terminal; ordering it first would retry a wall three times.

No second timeout mechanism and no cap: `pt = max(5, int(timeout) - 5)` is unchanged and a
seam test now pins it.

GATE: fanout.py --self-test PASS, and the live `make smoke-llm-council` PASS —
<model>/<effort>, <duration>, $<cost>. The judge harness is not this skill's gate.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UiV66Pt8cZVMq9t8WEAhpN
EOF
)"
```

Fill in the model, duration and cost from the smoke output. Expected: `verify rc=0`,
`precommit rc=0`.

---

## Task 10: the package-wide prose sweep, and the test that keeps it closed

**Measured while authoring this plan (M7), and the brief's number is wrong.** The
`i2-branch-fix-report.md` regex finds **18 line-matches across 9 modules** today, not "~27
across 12" — and the report's own itemisation summed to 21, so its headline disagreed with its
list. **One of the 18 is a false positive**: `taskbundle.py:48` reads "Both the **plan-mode**
and JSON forms", which is agy's mode, not a plan document; the pattern's `(?:the) plan\b`
matches because `-` is a word boundary. With `(?!-)` that one is suppressed and nothing else
is: **17 line citations across 9 modules**.

`harvest.py` adds one hit that only the FLATTENED scan finds — its referent wraps across two
comment lines, so it appears in no line-match and `harvest.py` is in neither count above.

**Real work: 17 line citations + 1 flattened-only = 18 rewrites across 10 modules.** The Files
list below names all ten; a headline saying nine would leave `harvest.py` out of the sweep
that has to touch it.

**Files:**
- Modify: `shared/lib/forge/{baseline,fleet,harvest,inspect,launch,ledger,runner,screen,
  taskbundle,verify}.py`
- Modify: `tests/test_forge_packaging.py`

**Interfaces:**
- Consumes: `test_forge_packaging._prose_blocks(source) -> list`,
  `test_forge_packaging._flat(prose) -> str`, `test_forge_packaging._TEMPORAL`.
- Produces: a `_PLAN_CITATION` pattern in `test_forge_packaging.py` and a test running it over
  **both** `_prose_blocks` + `_flat` **and** the whole file flattened, so runtime string
  literals are caught. Nothing later consumes it.

**The exact sites, measured today:**

| File:line | The referent |
|---|---|
| `baseline.py:74` | "has no producer yet, and a later plan consumes this field" |
| `baseline.py:363` | "a link is its target text, everywhere (Plan D, D-1)" |
| `fleet.py:351` | "since Plan D's D-1 the manifest holds the target" |
| `harvest.py` (flattened only) | "that decision belongs to a later plan, and discarding it here…" |
| `inspect.py:375` | "That was NOT the reason recorded here until Plan D." |
| `launch.py:10` | "CALLER since Plan H, so §8.1's validator has never run outside the suite" |
| `launch.py:50` | "undoes three defences a previous plan paid for" |
| `launch.py:68` | "Nothing in this plan calls `make_launcher`" |
| `launch.py:69` | "the CLI is a later plan" |
| `ledger.py:323` | "Plan I2's partitioned synthesis is its first consumer" |
| `runner.py:1115` | "as far as this plan drives it" |
| `runner.py:1561` | "`_confirmed_policy` shows how a later plan reads it back" |
| `runner.py:1565` | "belongs with the plan that has a front end to fail at" |
| `screen.py:181` | "(Plan D, D-2), for two measured reasons" |
| `taskbundle.py:56` | "Nothing in Plan I puts this path into a prompt" |
| `taskbundle.py:57` | "Plan J is the first plan that hands a seat the pointer" |
| `verify.py:44` | "the two options the plan offered are NOT equivalent" |
| `verify.py:381` | "not the `(Run, admitted)` pair the plan sketched" |

**`taskbundle.py:48` ("the plan-mode and JSON forms") is a FALSE POSITIVE and must not be
rewritten.** Rewriting it would corrupt a correct sentence about agy's mode.

**The specific fail-open this must not have:** rewriting a citation into a sentence that
*sounds* timeless while asserting something the code no longer does. Four of these sites
(`launch.py:10`, `:68`, `:69`, `runner.py:1565`) say **there is no production caller** — and
Task 6 gave them one. Rewriting them to timeless prose that keeps the claim would leave four
comments asserting something the code does not do, which Global Constraints calls a defect.
**Those four are rewritten to describe the caller that now exists.**

**What input would make this produce a result cleaner than its evidence:** the sweep itself.
`test_forge_packaging` is extended in this commit, and a pattern that passes only because the
citations were rewritten to say "an earlier iteration" instead of "an earlier plan" is a
detector defeated by a synonym. The test therefore also asserts the **substance** rule for the
four caller sites: `launch.py` must not claim it has no production caller.

- [ ] **Step 1: Re-measure before rewriting anything**

```
cd /home/khenrix/git/khenrix-utils
python3 - <<'PY'
import re, pathlib
pat = re.compile(r"\b(?:Decision \d+|Contradiction \d+|Plan [A-Z]\d?"
                 r"|(?:this|the|a later|a previous|an earlier|the next|a next) plan)\b", re.I)
for p in sorted(pathlib.Path("shared/lib/forge").glob("*.py")):
    txt = p.read_text()
    for i, l in enumerate(txt.splitlines(), 1):
        if pat.search(l):
            print(f"{p.name}:{i}: {l.strip()[:110]}")
    flat = re.sub(r"\s+", " ", txt)
    print(f"# {p.name} flattened total: {len(pat.findall(flat))}")
PY
```

Expected: the table above, plus the `taskbundle.py:48` false positive. **If the list differs
from the table, the list is right and the table is stale** — Tasks 1–9 added prose. Sweep what
you measure.

- [ ] **Step 2: Rewrite all eighteen**

Each rewrite states the **property**, not the history. Examples for the four that changed
meaning in Task 6:

```python
# launch.py module docstring — was: "…NO PRODUCTION CALLER since Plan H, so §8.1's validator
# has never run outside the suite."
"""…`forge.cli.start` is this module's production caller: it builds a launcher per run and
hands it to `runner.run`, which is what puts §8.1's validator on a real provider spec.
`run_provider` and `probe` stay injectable so the suite can drive the whole path without
spending anything."""
```

```python
# launch.py make_launcher — was: "NO PRODUCTION CALLER YET. Nothing in this plan calls
# `make_launcher`; `runner.run(..., launch=)` is injected and the CLI is a later plan. So
# `seat.forge_spec`'s 'production caller' is itself uncalled in production until then, and
# `bundle_sha256` is `None` for every seat a caller does not supply one for."
    """…`bundle_sha256` is `None` only for a caller that supplies none. `forge.cli.start`
    supplies `taskbundle.bundle_hash` over the run's own recorded bundle, so a real seat's
    `PromptIdentity` carries it and §11's label can compare it."""
```

For the historical ones, name the behaviour rather than the plan:

```python
# baseline.py:363 — was: "a link is its target text, everywhere (Plan D, D-1)."
# now:              "a link is its target text, everywhere — `baseline`, `snapshot`, `fleet`
#                    and `bundle` all give one shape one identity, and `fleet` verifies the
#                    entry rather than skipping it."
```

```python
# screen.py:181 — was: "(Plan D, D-2), for two measured reasons:"
# now:            "Discrimination stays OUT of screen, for two measured reasons:"
```

**Do not rewrite `taskbundle.py:48`.** Add a one-line comment beside it noting that "plan-mode"
is agy's mode so a future sweep does not touch it.

- [ ] **Step 3: Add the detector, now that it can pass**

Append to `tests/test_forge_packaging.py`:

```python
# A shipped module may not cite a plan document: the plans are not shipped, so a reader of the
# installed plugin is pointed at a file they do not have — and a citation is a claim about a
# moment rather than about the code, which is the same defect `_TEMPORAL` catches one tense
# over.
#
# RUN OVER BOTH VIEWS. `_prose_blocks` + `_flat` catches a referent wrapped across two comment
# lines, which no line-oriented grep finds; the WHOLE FILE flattened additionally catches a
# runtime string literal, which the prose extraction misses — and the sharpest instance this
# project found was inside a raised exception's message.
_PLAN_CITATION = re.compile(
    r"\b(?:Decision \d+|Contradiction \d+|Plan [A-Z]\d?"
    r"|(?:this|the|a later|a previous|an earlier|the next|a next) plan)\b(?!-)", re.I)


def test_no_shipped_forge_module_cites_a_plan_document():
    """`(?!-)` is not a convenience: without it the pattern matches `the plan-mode` in
    `taskbundle`'s agy probe note, which is agy's mode and not a plan. A detector that fires on
    a correct sentence is one an author learns to write around."""
    bad = []
    for p in sorted((ROOT / "shared" / "lib" / "forge").glob("*.py")):
        text = p.read_text()
        for flat in map(_flat, _prose_blocks(text)):
            if _PLAN_CITATION.search(flat):
                bad.append(f"{p.name}: {_PLAN_CITATION.search(flat).group(0)!r} in prose")
        whole = re.sub(r"\s+", " ", text)
        for m in _PLAN_CITATION.finditer(whole):
            bad.append(f"{p.name}: {m.group(0)!r} in {whole[max(0, m.start()-50):m.end()+30]!r}")
    assert not bad, "\n".join(sorted(set(bad)))


def test_launch_does_not_claim_it_has_no_production_caller():
    """The substance half. A sweep that rewrote 'no production caller since Plan H' into 'no
    production caller' would pass the pattern above while leaving a comment asserting
    something the code does not do."""
    src = (ROOT / "shared" / "lib" / "forge" / "launch.py").read_text().lower()
    assert "no production caller" not in src
    assert "cli" in src, "the module no longer names the caller that exists"
```

- [ ] **Step 4: Run, and check the rendered copies**

```
cd /home/khenrix/git/khenrix-utils
uvx --with pytest pytest -q tests/
make render
git diff --stat -- marketplaces/
```

Expected: suite PASS; `git diff --stat` shows every touched module's rendered copy moved in all
three marketplaces. **Verify byte-equality of at least one rendered copy against its source**
rather than trusting the diff:

```
cd /home/khenrix/git/khenrix-utils
for cli in claude codex agy; do
  diff -q shared/lib/forge/launch.py \
       marketplaces/$cli/plugins/khenrix-utils/lib/forge/launch.py
done
echo "rc=$?"
```

Expected: no output, `rc=0`.

- [ ] **Step 5: Commit**

```
cd /home/khenrix/git/khenrix-utils
make verify
echo "verify rc=$?"
make precommit
echo "precommit rc=$?"
git add shared/lib/forge tests/test_forge_packaging.py marketplaces
git commit -m "$(cat <<'EOF'
docs(forge): the shipped package pointed eighteen times at documents it does not ship

Measured today rather than carried forward: 17 line-oriented citations across 9 modules plus
1 that only a flattened scan finds, in a tenth (harvest) — 18 across 10, not the 27 across 12
the carried note claimed,
whose own itemisation summed to 21. One apparent hit was a FALSE POSITIVE: taskbundle's
"the plan-mode and JSON forms" is agy's mode, and the pattern matched because `-` is a word
boundary. The detector added here carries `(?!-)` so it does not fire on a correct sentence.

Four of the eighteen said there is no production caller. There is one now, so they were
rewritten to describe it rather than to timeless prose that would have kept a false claim —
a comment asserting something the code does not do is a defect whether or not it cites a
plan. A second test asserts that substance directly, because a pattern is defeated by a
synonym.

The detector runs over both views: prose blocks flattened (a referent wrapped across two
comment lines, which no line-oriented grep finds) and the whole file flattened (a runtime
string literal, which the prose extraction misses).

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UiV66Pt8cZVMq9t8WEAhpN
EOF
)"
```

Expected: `verify rc=0`, `precommit rc=0`.

---

## Task 11: the skill, §18's evals, the suite split, and the hard gate

`CLAUDE.md`: **any skill change must be eval-tested for every provider before commit.**
`make eval SKILL=llm-forge` must show `run_summary.delta.pass_rate >= 0` and `make precommit`
must see a matching receipt. The blind A/B winner is recorded and advisory.

**Plan the eval set as real work with real tasks. Do not tune it until it passes** — an eval
set edited to clear the gate measures the editor, not the skill.

**Files:**
- Create: `shared/skills/llm-forge/SKILL.md`
- Create: `shared/skills/llm-forge/scripts/forge.py`
- Create: `evals/llm-forge/evals.json`
- Create: `evals/llm-forge/fixtures/` (a small fixture repository)
- Modify: `capabilities.toml` (`[[skills]]` discoverability entry)
- Modify: `scripts/eval_harness.py` (`DETERMINISTIC_GATED["llm-forge"]`)
- Modify: `Makefile` (`FORGE_TESTS` split by weight; `forge-test-slow`)
- Test: `tests/test_forge_packaging.py`

**Interfaces:**
- Consumes: `cli.main`; `checks.SKILL_EXTRA["llm-forge"] = ["scripts/lib/checks.py"]` and
  `checks.SKILL_EXTRA_DIRS["llm-forge"] = ["shared/lib/forge", "shared/lib/council"]` — both
  already wired and **inert until `evals/llm-forge/evals.json` exists**;
  `checks._evald_skills(root)`; `checks.eval_set_hash(root, skill)`;
  `eval_harness.DETERMINISTIC_GATED`; `render.py`'s `shared/skills/` glob.
- Produces: `evals/llm-forge/receipt.json` with a `source_hash` matching the tree.

**The specific fail-opens this must not have:**
1. **`evals/llm-forge/evals.json` missing.** `checks._evald_skills()` globs `evals/*/` gated on
   that file, and `eval_set_hash` does an unconditional `read_bytes()` — so without it the
   skill is invisible to `receipt_gate` **and the §18 exemption silently becomes no gate at
   all**. Creating it is what arms everything else.
2. **`DETERMINISTIC_GATED["llm-forge"]` hardcoding `"python3"`.** §18 says use
   `sys.executable`; the two existing entries hardcode it and this machine's `python3` is 3.14
   against a stated 3.11 floor.
3. **A forge test marked `slow` running nowhere.** Today `Makefile:96` names only the two
   council files under `-m slow`, so a forge test given that marker would be run by **nothing**
   — a suite that silently stops running is the vacuous green this whole project exists to
   close. The `forge-test-slow` target and a `make test` dependency land in the same commit as
   the first marker.
4. **Nine suites leaving the commit-boundary gate.** `make verify` runs `council-test`, which
   runs `$(FORGE_TESTS)`, and `make precommit` is `verify` plus the receipt check — so moving a
   file out of `FORGE_TESTS` moves it out of `precommit`. Measured: the split removes
   `baseline`, `fleet`, `harvest`, `bundle`, `verify`, `runner`, `review`, `gc` and `cli`, and
   `DETERMINISTIC_GATED["llm-forge"]` re-covers only the last two plus `handover` — leaving
   **`baseline`, `fleet`, `harvest`, `bundle`, `verify`, `runner` and `review` in no gate at
   all**, including the two modules Tasks 3 and 8 change. The forge-inside-forge argument is
   about `verify` specifically, because `make verify` is this repository's own obvious
   confirmed verify command; it says nothing about `precommit`, which no forge run executes.
   **`precommit` therefore depends on `forge-test-slow`**, and that dependency lands in the
   same commit as the split.
5. **A receipt naming a gate that did not run.** `eval_harness.py:452` hardcodes
   `deterministic_gate="wikisync-unittests"` for **every** `DETERMINISTIC_GATED` skill, so
   llm-forge's receipt would record the wikisync unit tests as its evidence — a false
   provenance string on the very artifact that exists to say which gate ran.
4. **A SKILL.md that lets "verified" read as the stronger claim.** §16.1 requires one sentence
   saying what it means, and §18 requires one saying the self-test gates wiring, not judgment.
   Both are asserted by a packaging test, not left to review.

**What input would make this produce a result cleaner than its evidence:** the eval harness
itself. `eval_harness.run()` computes `gate_ok = (d is not None and d >= 0 and not invalid)`
and then **overrides it to `True`** for anything in `DETERMINISTIC_GATED` — and the judge run
executes **before** the override, so routing makes the delta advisory but not free. A receipt
written after an override is not evidence the skill is good; the evidence is the
`DETERMINISTIC_GATED` command, which `_write_receipt` runs and refuses to write on. **So the
command must be a real suite, not `--help`.**

- [ ] **Step 1: Split the forge suite by weight**

In `Makefile`, split `FORGE_TESTS` into a fast set and a heavy set, and give the heavy one a
home:

```makefile
# §18: split by weight. The fast subset — schema, state machine, classification, journal
# parsing — is in `verify` and therefore in `precommit`. The clone- and process-heavy subset is
# NOT, because `make verify` is the obvious confirmed verify command for this very repository:
# a forge run would then spawn clone fleets four-plus times inside its own verifier clones,
# inflating wall clock and manufacturing FLAKY verdicts under §6's contended execution.
FORGE_TESTS := tests/test_forge_storage.py tests/test_forge_inspect.py \
               tests/test_forge_screen.py tests/test_forge_packaging.py \
               tests/test_forge_snapshot.py tests/test_forge_seams.py \
               tests/test_forge_journal.py tests/test_forge_runstate.py \
               tests/test_forge_preflight.py tests/test_forge_gate.py \
               tests/test_forge_seat.py tests/test_forge_taskbundle.py \
               tests/test_forge_ledger.py tests/test_forge_coverage.py \
               tests/test_forge_fingerprint.py tests/test_forge_launch.py \
               tests/test_forge_seatrecord.py tests/test_forge_strategy.py \
               tests/test_forge_progress.py tests/test_forge_rubric.py \
               tests/test_forge_ultra.py tests/test_forge_handover.py

FORGE_SLOW_TESTS := tests/test_forge_baseline.py tests/test_forge_fleet.py \
                    tests/test_forge_harvest.py tests/test_forge_bundle.py \
                    tests/test_forge_verify.py tests/test_forge_runner.py \
                    tests/test_forge_review.py tests/test_forge_gc.py \
                    tests/test_forge_cli.py
```

and:

```makefile
# A HEAVY SUITE WITH NO TARGET IS A SUITE THAT ROTS, which is the failure `bats-test`'s own
# comment is about one gate over. `make test` runs it, the receipt gate runs it through
# DETERMINISTIC_GATED, and `make precommit` depends on it — three homes, and none of them is
# `make verify`.
forge-test-slow: ## Clone- and process-heavy forge suites (no token cost, slower)
	$(call RUN_PYTEST,$(FORGE_SLOW_TESTS))

test: council-test forge-test-slow ## …

# THE SPLIT MOVES NINE SUITES OUT OF `verify`, AND `precommit` IS WHERE THEY LAND. `verify` is
# reached by `precommit` and also by whoever picks a confirmed verify command for THIS
# repository, which is the whole forge-inside-forge argument above — a forge run would spawn
# clone fleets inside its own verifier clones. `precommit` is neither: nothing runs it inside a
# verifier. Without this line `baseline`, `fleet`, `harvest`, `bundle`, `verify`, `runner` and
# `review` are in no commit-boundary gate at all, which is the vacuous green this repository
# spends eleven plans closing.
precommit: verify forge-test-slow ## …
```

Add `forge-test-slow` to `.PHONY`. **Run `make council-test`, `make forge-test-slow`,
`make test` and `make precommit` and confirm every file appears in exactly one of the two
lists** — a file in neither is a suite nothing runs, which is precisely the defect this split
could introduce. Then confirm the coverage claim directly:

```
cd /home/khenrix/git/khenrix-utils
python3 - <<'PY'
import pathlib, re
mk = pathlib.Path("Makefile").read_text()
def names(var):
    m = re.search(rf"^{var} :?=(.*?)(?=\n[A-Z_]+ :?=|\n\n)", mk, re.S | re.M)
    return set(re.findall(r"tests/\S+\.py", m.group(1)))
gated = names("FORGE_TESTS") | names("FORGE_SLOW_TESTS")
on_disk = {str(p) for p in pathlib.Path("tests").glob("test_forge_*.py")}
print("reached by no precommit target:", sorted(on_disk - gated))
print("precommit deps:", re.search(r"^precommit: (.*?) ", mk, re.M).group(1))
PY
```

Expected: an empty list, and `precommit` naming both `verify` and `forge-test-slow`.

```
cd /home/khenrix/git/khenrix-utils
python3 - <<'PY'
import pathlib, re
mk = pathlib.Path("Makefile").read_text()
def names(var):
    m = re.search(rf"^{var} :?=(.*?)(?=\n[A-Z_]+ :?=|\n\n)", mk, re.S | re.M)
    return set(re.findall(r"tests/\S+\.py", m.group(1)))
fast, slow = names("FORGE_TESTS"), names("FORGE_SLOW_TESTS")
on_disk = {str(p) for p in pathlib.Path("tests").glob("test_forge_*.py")}
print("in neither:", sorted(on_disk - fast - slow))
print("in both:", sorted(fast & slow))
PY
```

Expected: both lists empty.

- [ ] **Step 2: Write `SKILL.md`**

Create `shared/skills/llm-forge/SKILL.md`. Frontmatter: `name: llm-forge`, a `description`
under 1024 characters covering the triggers, and `allowed-tools: Bash, Read`. Body under 500
lines (`render.py --check` enforces both).

The body must contain, at minimum:

- What forge is: one task, three CLI agents, isolated clones, **fusion — a new best-of-all
  answer, not a winner**.
- The cost sentence, and that `--start` shows §5.2's quote before anything is spent.
- The three commands, with the plugin-root `$FORGE` resolution loop **copied verbatim from
  `shared/skills/llm-council/SKILL.md`'s `$FANOUT` idiom plus its `PYTHONPATH` export** — read
  that file and copy it; do not re-derive it.
- **The orchestrator's job**: `--start` stops at `comparing`; you fuse in the synthesis
  worktree; then `--collect`.
- §16.1's sentence, verbatim from `handover._VERIFIED_MEANS`, so the header and the skill
  cannot drift: *"Verified" here means exactly this and no more: the confirmed verify command
  exited 0 on a fresh verifier clone at the final checkpoint. It does not mean the change has
  no new defects, and it is not a review.*
- §18's sentence: **the self-test gates wiring, not judgment.**
- §15: `--gc <run-id>` is mandatory, not tidy; `--gc all` reports total disk held.
- §20: what a portable task bundle is, and that a task naming provider-specific machinery is
  refused rather than translated.

- [ ] **Step 3: Write the executable façade**

Create `shared/skills/llm-forge/scripts/forge.py`:

```python
#!/usr/bin/env python3
"""Executable entry point for the llm-forge skill.

The engine is `forge.cli`, bundled at the plugin's `lib/forge/`. This resolves that directory
whether it is being run from the repository source tree or from an installed plugin copy, then
delegates. It contains no logic of its own, on `fanout.py`'s precedent: a façade that decides
anything is a second implementation.
"""
import sys
from pathlib import Path

_here = Path(__file__).resolve()

# ONE EXPRESSION, BOTH LIVE LAYOUTS — measured, not assumed. This script sits at
# `<root>/<skills-dir>/llm-forge/scripts/forge.py` in each of them, so `parents[3]` is the root
# and the engine is at `<root>/lib/forge/`:
#
#   installed plugin  .../plugins/khenrix-utils/skills/llm-forge/scripts/forge.py
#                     -> parents[3] = .../plugins/khenrix-utils   -> lib/forge/cli.py      ✓
#   repo source tree  shared/skills/llm-forge/scripts/forge.py
#                     -> parents[3] = shared                      -> shared/lib/forge/cli.py ✓
#
# An earlier draft carried two more candidates. `parents[3]/"shared"/"lib"` resolves to
# `shared/shared/lib` in the repo and `.../khenrix-utils/shared/lib` in a plugin — neither
# exists in either layout, and its "# repo source tree" comment named a directory it never
# points at. `parents[4]/"shared"/"lib"` does reach the repo's `shared/lib`, but only from the
# repo, where the line above already matched. A dead candidate with a wrong comment is worse
# than none: it reads as coverage of a case nobody has.
_lib = _here.parents[3] / "lib"
if not (_lib / "forge" / "cli.py").is_file():
    sys.exit(f"could not locate the forge engine: no forge/cli.py under {_lib}")
sys.path.insert(0, str(_lib))

from forge import cli  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(cli.main())
```

**Verify the resolution from BOTH locations before relying on it** — run the source-tree copy
and a rendered plugin copy, and record what you observed; `parents[N]` indices are the plan's
own draft code and this project's standing brief says that draft has been wrong in every task
of every plan. If `render.py` places the skill at a different depth than the source tree does,
the single expression is wrong and the fix is a second candidate **with a comment naming the
layout it was measured against**, never a speculative one.

- [ ] **Step 4: Add the discoverability entry and render**

Append a `[[skills]]` entry for `llm-forge` to `capabilities.toml`, matching the shape of the
`llm-council` entry (which the comment at `capabilities.toml:132-134` says is for
discoverability only — `render.py` globs `shared/skills/`).

```
cd /home/khenrix/git/khenrix-utils
make render
ls marketplaces/claude/plugins/khenrix-utils/skills/llm-forge/
diff -q shared/skills/llm-forge/SKILL.md \
        marketplaces/agy/plugins/khenrix-utils/skills/llm-forge/SKILL.md
echo "rc=$?"
```

Expected: the skill directory exists in all three marketplaces; the diff is silent, `rc=0`.

- [ ] **Step 5: Write the packaging assertions**

Append to `tests/test_forge_packaging.py`:

```python
def test_the_skill_states_what_verified_means_and_what_the_self_test_gates():
    """§16.1 and §18 each require one sentence, and a provenance header will otherwise be read
    as the stronger claim. Asserted against handover's own constant so the two cannot drift."""
    from forge import handover
    body = (ROOT / "shared" / "skills" / "llm-forge" / "SKILL.md").read_text()
    assert handover._VERIFIED_MEANS.strip('"') in body or \
        "exited 0 on a fresh verifier clone at the final checkpoint" in body
    assert "gates wiring, not judgment" in body


def test_the_rendered_skill_can_find_the_engine_from_a_plugin_path():
    """The assertion nobody made for llm-council until §17 asked for it: run the façade from a
    RENDERED plugin directory and prove it resolves the bundled engine."""
    for cli_name in ("claude", "codex", "agy"):
        script = (ROOT / "marketplaces" / cli_name / "plugins" / "khenrix-utils"
                  / "skills" / "llm-forge" / "scripts" / "forge.py")
        assert script.is_file(), script
        r = subprocess.run([sys.executable, str(script), "--help"],
                           capture_output=True, text=True)
        assert r.returncode == 0, (cli_name, r.stderr)
        for flag in ("--start", "--collect", "--gc", "--no-ultra"):
            assert flag in r.stdout, (cli_name, flag)
```

- [ ] **Step 6: Write the eval set — real work, graded against the fixture**

**READ THIS BEFORE WRITING A LINE OF IT.** An earlier draft of this step said to write the
assertions "from the SKILL.md you wrote in Step 2", with six inline Q&A and a fixture directory
**none of them referenced**. That is a closed loop: assertions derived from the body, graded
against the body. A with-skill condition cannot fail it, and a no-skill baseline cannot know
project vocabulary like `--gc all` or "re-prices the run" — so `delta.pass_rate >= 0` would be
guaranteed by construction rather than earned, and `make eval SKILL=llm-forge` would cost
tokens to certify prose. **A gate that cannot fail measures nothing, and is worse than a
failing one because it reports success.**

So:

- **The assertions come from the ENGINE, not from `SKILL.md`.** Each one below names the file
  it is a fact about. Read that file, confirm the fact, then write the assertion. If `SKILL.md`
  does not support an assertion the engine does, **fix `SKILL.md`** — that is the loop running
  in the right direction. If the ENGINE does not support an assertion, the assertion is wrong
  and you found it by reading code, which is the only legitimate reason to edit this file.
- **At least two evals grade against the fixture**, on facts where a baseline answer can be
  factually wrong, not merely differently-worded.

**The fixture.** Create `evals/llm-forge/fixtures/repo-state/` describing one small repository —
captured text, not a live git directory, on `evals/khenrix-audit/fixtures/scaffold_home.py`'s
precedent (the executor reads it; the harness copies it into both conditions identically):

| File | Contents |
|---|---|
| `Makefile` | a `verify:` target running a two-line test command |
| `gitignore.txt` | the repository's `.gitignore`: `dist/`, `.venv/` |
| `git-status.txt` | `git status --porcelain` output: ` M src/app.py`, `?? scratch/notes.md` |
| `git-ls-files.txt` | `Makefile`, `src/app.py`, `.gitignore` |
| `tree.txt` | the working tree including the ignored `dist/bundle.js` |

**Put no credential-shaped literal in this fixture.** This repository screens its own tree; a
realistic-looking key would trip `make verify` and the fixture would fail a gate it is not
about.

**Generate `fixtures/handover-header.txt` by RUNNING the renderer**, never by hand:
`handover.text(h, p)` over a `Handover`/`Provenance` describing a run against this same fixture
repo — three seats, two completed, one passing verify, `PATCH_ONLY` because the baseline was
dirty, one out-of-band artifact, `strongest=(None, why)`, `agreement="differently-prompted"`,
and `synthesis_measured=False`. Record the snippet that produced it beside the file. A
hand-written header drifts from the renderer the first time either moves, and then the eval
grades an artifact this engine does not emit.

Then create `evals/llm-forge/evals.json` in the shape `evals/markitdown/evals.json` uses:
`{skill_name, notes, evals: [{id, name, prompt, files, assertions}]}`. Six evals; the first two
carry `"files": ["repo-state"]` / `["handover-header.txt"]` and reference `{fixture_dir}` in
the prompt, and must **permit reading those files** (say so in the prompt, as khenrix-audit's
eval 0 does). The other four forbid plan mode and tools and are answered inline.

| id | name | Graded on | A baseline plausibly answers |
|---|---|---|---|
| 0 | `fixture-what-enters-b1-and-what-comes-back` | **the fixture repo** | "it runs on your working tree"; "`git add -f dist/`"; "just merge the branch" |
| 1 | `fixture-handover-what-may-i-claim` | **the fixture header** | "verified means it works"; "merge and you're done"; "no strongest seat = they tied" |
| 2 | `fusion-not-selection` | `runner`/§16 | "pick the best of the three" |
| 3 | `provider-specific-task-is-refused` | `preflight.task_refusals` | "forge will adapt it for each CLI" |
| 4 | `gc-is-mandatory` | `gc.py` | "delete the run directory" |
| 5 | `no-ultra-is-priced-not-just-skipped` | `gate.confirm` | "pass `--no-ultra` when you collect" |

**Eval 0** — prompt: the executor may read `{fixture_dir}/repo-state/`; it is asked what
`llm-forge --start --select scratch/notes.md` does here, what B1 contains, what happens to
`dist/`, and what shape the handover will be. Four assertions, each a fact about a named file:

1. `src/app.py` is **dirty tracked work**, so B1's tracked tree is not `base^{tree}` and the
   handover is **patch-only, not a merge-ready branch** — merging would carry the user's
   uncommitted work as though forge had authored it. (`handover.mergeability`)
2. `scratch/notes.md` enters the baseline **only because it was `--select`ed**; untracked paths
   are not carried otherwise, and if it comes back unchanged it is **baseline-owned** — the
   user's, not forge's, with only its B→S changes forge-authored. (`preflight.inspect_repo`,
   `handover.Handover.baseline_owned`)
3. `dist/bundle.js` is ignored and is **never force-added**; it returns as an **out-of-band
   artifact** with a sha256, a size and an explicit `cp` command, and **merging the branch does
   not install it**. (`handover.out_of_band`, `handover.text`)
4. The credential screen covers **the selected paths only** — `screen_tree(facts.root,
   contained)` — so it is not "forge scans your repository for secrets", and an escaping
   selection is refused before anything is spent. (`preflight.inspect_repo`, `refusals`)

**Eval 1** — prompt: here is what forge printed (`{fixture_dir}/handover-header.txt`); what may
I claim, and what must I do to actually have this change? Four assertions:

1. "verify PASS" here is a **report from the orchestrator, not a verification this engine
   performed** — the header says so, and the "Verified here means" paragraph is absent for
   exactly that reason. (`handover._ASSERTED_MEANS`)
2. Even a measured PASS would mean only that the confirmed command exited 0 on a fresh verifier
   clone at the final checkpoint — **not** that the change has no new defects, and not a
   review. (`handover._VERIFIED_MEANS`)
3. Merging the branch does **not** install the out-of-band artifact; the printed `cp` command
   is a second step. (`handover.text`)
4. "no strongest seat" is a measured **cannot-tell** with its reason attached, not a tie or an
   absence of finding; and `differently-prompted` is §11 provenance, not a correctness claim.
   (`rubric.strongest`, `fingerprint.LABELS`)

Evals 2–5 keep four checkable claims each, drawn the same way — from `cli.py`, `preflight.py`,
`gc.py` and `gate.py` respectively.

**Cost note.** Two evals now read fixture files, so the executor does real work rather than
recall. That is the point, and it is also why the other four stay inline: §18's exemption
already makes the delta advisory, and the eval set's job is to be honest, not expensive.

- [ ] **Step 7: Route the receipt through `DETERMINISTIC_GATED`**

In `scripts/eval_harness.py`, add to the dict:

```python
    # §18: a read-only with-skill/baseline harness cannot exercise forge's defining behaviour —
    # a clone fleet, three providers, a fresh verifier — and an ordinary judge receipt would
    # certify prose while leaving the dangerous mechanics untouched. The gate is the hermetic
    # forge suite, which `_write_receipt` runs and refuses to write on.
    #
    # NOTE the judge run still EXECUTES: `gate_ok = True` is applied AFTER it in `run()`, so
    # routing makes the delta advisory, not the run free. The cost control is the cheap eval
    # set beside this file.
    #
    # `sys.executable`, NOT a hardcoded "python3": this machine's python3 is 3.14 against a
    # stated 3.11 floor, and the two entries above get this wrong.
    "llm-forge": [sys.executable, "-m", "pytest", "-q",
                  str(ROOT / "tests" / "test_forge_handover.py"),
                  str(ROOT / "tests" / "test_forge_cli.py"),
                  str(ROOT / "tests" / "test_forge_gc.py")],
```

and **fix the gate NAME the receipt records**, which is a defect for the two existing entries
too. `eval_harness.py:452` is:

```python
        rec.update(deterministic_gate="wikisync-unittests", self_test=True)
```

— a hardcoded literal applied to **every** `DETERMINISTIC_GATED` skill, so llm-forge's receipt
would name the wikisync unit tests as the evidence that gated it. Add a parallel table and read
it:

```python
# WHICH gate earned this receipt, as a name a reader can check against the command above. A
# single literal here was a false provenance string the moment a third skill was routed through
# this dict — and a receipt exists to say what ran, so being wrong about that is worse than
# recording nothing.
DETERMINISTIC_GATE_NAMES = {
    "khenrix-wiki-add":  "wikisync-unittests",
    "khenrix-wiki-sync": "wikisync-unittests",
    "llm-forge":         "forge-handover-cli-gc-suites",
}
```

```python
        rec.update(deterministic_gate=DETERMINISTIC_GATE_NAMES[skill], self_test=True)
```

A `KeyError` here is the right failure: a skill routed through `DETERMINISTIC_GATED` with no
name is one whose receipt cannot say what gated it, and `.get(skill, "unknown")` would write
that receipt anyway. **Measured: `scripts/eval_harness.py` is in no skill's source closure**
(`checks.LIB_SCRIPTS` is `reconcile.py` + `inventory.py`, `GLOBAL_INPUTS` is `render.py`), so
editing it stales no receipt — including the two whose recorded name is unchanged.

**Verify this command runs standalone before relying on it** — `pytest` must be importable by
`sys.executable`, and if it is not, the entry must use the `uvx` form the Makefile falls back
to. Measure:

```
cd /home/khenrix/git/khenrix-utils
python3 -c "import pytest; print(pytest.__version__)"
echo "rc=$?"
```

If `rc` is non-zero, use `["uvx", "--with", "pytest", "pytest", "-q", …]` and say in the
comment that `sys.executable` could not import pytest on this machine — that is a measurement,
not a preference.

- [ ] **Step 8: Confirm the gate is now armed**

```
cd /home/khenrix/git/khenrix-utils
python3 -c "import sys; sys.path.insert(0,'scripts/lib'); import checks; print(checks._evald_skills(checks.ROOT))"
make verify
echo "verify rc=$?"
```

Expected: `llm-forge` now appears in the skill list (it did not before `evals.json` existed),
and `make verify` prints `(advisory) receipt: llm-forge has no receipt`.

- [ ] **Step 9: Run the eval — the hard gate**

```
cd /home/khenrix/git/khenrix-utils
make eval SKILL=llm-forge
echo "eval rc=$?"
```

Expected: `eval rc=0`, and `run_summary.delta.pass_rate >= 0` in
`evals/llm-forge/workspace/iteration-*/benchmark.json`. Read the number and **state it**.

**If the delta is negative, the skill body is wrong, not the eval set.** Read the judge's
per-assertion output, find which decision the with-skill condition failed to encode, and fix
`SKILL.md`. Re-render and re-run. **Do not edit `evals.json` to make the number move** — an
eval set tuned until it passes measures the editor. The one legitimate reason to edit it is an
assertion that is factually wrong about the engine, and that is a bug in the assertion found by
reading the code, not by reading the score.

The blind A/B winner is **recorded and advisory**. A tie or a loss beside a non-negative delta
does not fail this run; `CLAUDE.md` says so, and on a strong executor it rewards the tighter
baseline over a correct-but-thorough skill answer.

**And say plainly, in the commit message, what this number is worth.** §18 exempts llm-forge
from the judge gate because a read-only harness cannot drive a clone fleet, so `gate_ok` is
overridden to `True` for anything in `DETERMINISTIC_GATED` and the real evidence is the
hermetic suite. The delta is therefore **advisory here even when it is positive**. What the
eval set buys is that the two fixture-graded evals can actually be **wrong**: if the with-skill
condition tells an operator to merge a branch that does not install its out-of-band artifacts,
or calls an orchestrator-reported verdict verified, the judge should catch it — and a negative
delta on those two is a real signal about the body, not about the eval set.

- [ ] **Step 10: Confirm the receipt and commit**

```
cd /home/khenrix/git/khenrix-utils
python3 -c "import json;print(json.load(open('evals/llm-forge/receipt.json')))"
make render
make verify
echo "verify rc=$?"
make precommit
echo "precommit rc=$?"
```

Expected: the receipt carries `deterministic_gate` and `self_test: true`, `verify rc=0` with
**no** advisory receipt lines, and `precommit rc=0`.

```
cd /home/khenrix/git/khenrix-utils
uvx --with pytest pytest -q tests/
git add shared/skills/llm-forge capabilities.toml scripts/eval_harness.py Makefile \
        evals/llm-forge tests/test_forge_packaging.py marketplaces
git commit -m "$(cat <<'EOF'
feat(llm-forge): the skill ships, and §18's gate is armed rather than exempt

evals/llm-forge/evals.json is what arms everything: checks._evald_skills globs evals/*/
gated on that file, so without it the llm-forge entries already sitting in SKILL_EXTRA and
SKILL_EXTRA_DIRS were inert and §18's judge-gate exemption was no gate at all.

The receipt routes through DETERMINISTIC_GATED with sys.executable rather than a hardcoded
python3 — this machine's python3 is 3.14 against a stated 3.11 floor, and the two existing
entries get that wrong. The judge run still executes: gate_ok is overridden AFTER it, so the
delta is advisory and the run is not free; the cost control is the cheap eval set.

The receipt also records WHICH gate ran. eval_harness hardcoded
deterministic_gate="wikisync-unittests" for every DETERMINISTIC_GATED skill, so llm-forge's
receipt would have named the wikisync unit tests as its evidence — a false provenance string
on the one artifact that exists to say what gated it.

The forge suite is split by weight. The heavy subset leaves `make verify`, because verify is
the obvious confirmed verify command for this very repository and a forge run would then
spawn clone fleets inside its own verifier clones. It gains a `forge-test-slow` target in
the same commit, since Makefile:96 named only the two council files under -m slow and a
forge test given that marker would have been run by nothing at all — and `precommit` depends
on that target, or the split would leave baseline, fleet, harvest, bundle, verify, runner and
review in no commit-boundary gate at all.

SKILL.md carries §16.1's "Verified means" sentence and §18's "the self-test gates wiring,
not judgment", both asserted by a packaging test against handover's own constant so the
header and the skill cannot drift.

make eval SKILL=llm-forge: delta.pass_rate = <N>, blind winner <W> (advisory). The delta is
advisory even when positive: §18 exempts this skill from the judge gate and gate_ok is
overridden after the run, so the evidence in the receipt is the hermetic forge suite. What
the eval set buys is that two of its six grade against a fixture repository on facts an
answer can be WRONG about — what enters B1, what an ignored directory does, and what a
handover header entitles the reader to claim — rather than on vocabulary SKILL.md supplies.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UiV66Pt8cZVMq9t8WEAhpN
EOF
)"
```

Fill in `<N>` and `<W>` from the eval output. Expected: `verify rc=0`, `precommit rc=0`.

---

## Self-review

Run against the spec (`docs/superpowers/specs/2026-07-30-llm-forge-design.md`) and the brief.

**1. Spec coverage.**

| Spec section | Task | Note |
|---|---|---|
| §15 storage — `--gc`, `handover_target`, disk report | 7 (+4 for the record) | Keep-last-N stays deferred, per §15. |
| §16 handover | 4 | All six requirements: worktree `-b`, explicit refspec, mergeability table, out-of-band never force-added, baseline-owned, B₁ in the text. |
| §16.1 provenance header | 5 | Four lines, "built" forbidden, the `unavailable` variant, failure-cannot-render-success, and the measured/asserted split that keeps "Verified means" off an unmeasured verdict. **Measuring the synthesis in-engine is NOT planned — see "could not specify" below.** |
| §18 evals | 11 | `evals.json` (two of six graded against the fixture), `DETERMINISTIC_GATED` with `sys.executable` and a truthful gate name, the weight split with `precommit` covering the heavy half. **The live three-provider write smoke and the 180 s silent step are NOT planned — see below.** |
| §19 agy print-timeout | 9 | Items 1 and 2. **Item 3, the hour-long three-adapter probe, is NOT planned — see below.** |
| §20 portability | 3 | The preflight refusal, the ambient bar, bundle materialization, persistence for `--collect`. The live-installed-closure resolver and three-way hash check already exist in `taskbundle`; this wires them. |
| CLI | 6, 7 | `--start` / `--collect` / `--gc` / `--no-ultra`. |
| SKILL.md | 11 | |
| `make_launcher`/`forge_spec` caller | 6 | Step 10 asserts §8.1's validator on a real spec. |
| `run_ultra` caller | 6 | `collect`. |
| `bundle_sha256` populated | 3, 6 | |
| `strongest` / agreement as ordinary outcomes | 5 | |
| `reviewer_env` scrub | 8 | |
| prose sweep | 10 | |
| `reconcile.read_json_object` | 1 | |
| `eval_trigger` dead judge | 2 | |

**2. Placeholder scan.** No "TBD", no "add appropriate error handling", no "similar to Task N".
Seven steps say *read this file first and match what is there* — those are instructions to
verify a real interface before writing against it, which the standing defect brief requires
("the plan's own draft code has been wrong in every task of every plan"); each names the exact
file and line range and the code it governs is written out in full.

**3. Type consistency.** `handover.branch` is one function used by Tasks 4, 6 and 7.
`handover.SeatLine`/`Provenance` are produced in Task 5 and consumed in Task 6's `_seat_lines`
and `collect` with the same field names — including `synthesis_measured`, which Task 5 refuses
a `True` for without a measurement and Task 6 writes as a constant `False`. `handover.Handover`
is produced in Task 4, written in Task 6's `collect`, read in Task 7's `gc.collect` and in
Task 7's `usage`. `preflight.ambient_notes` is produced in Task 3 and consumed in Task 6's
`start`, and is `taskbundle.ambient_note`'s only caller anywhere.
`taskbundle.read_task_bundle_if_recorded` is produced
in Task 3 and consumed in Task 3's `runner._materialize_the_task` and Task 6's seam test.
`storage.task_source_path` is produced in Task 3 and consumed in Tasks 3 and 6;
`storage.run_dirs`/`run_digest` are produced in Task 7 and consumed by Task 7's `gc.usage` and
by `run_root` itself, which is what makes them one spelling rather than two.
`cli.main`/`cli.start` carry `make_launcher`, which only `tests/test_forge_cli.py` passes and
which is `None` in production — the seam that keeps the CLI suite from spending money.
`gate.Confirmation.ultrareview` is produced in Task 6 commit 1 and read in Task 6 commit 2's
`_confirmed_ultrareview`. `cli._gc` is referenced in Task 6's `main` and defined in Task 7 —
**Task 6 will not import cleanly until Task 7 lands**, so Task 6 defines a stub `_gc` that
returns `_fail(out, ["--gc is not built yet"])` and Task 7 replaces it. That stub is the one
forward reference in the plan and it is deliberate; without it Task 6's parser could advertise
a flag that raises `AttributeError`.

**4. Ordering.** 1 and 2 are independent. 3 → 4 → 5 → 6 → 7 is a chain. 8 depends on nothing
after 3. 9 is independent, and Task 6 Step 7's `_resolve_seat_timeout` refuses until it lands —
**running 9 before 6 removes that refusal from the happy path**, which is the better order if
the implementer wants the CLI's tests to exercise a real timeout. 10 is independent but must
run after 6, because four of its rewrites describe the caller Task 6 creates. 11 last.

**5. Import edges introduced.** `handover` imports `gitcmd`, `storage`, `fingerprint`, `seat`,
`ultra`, `verify` and — at module scope, through `_review_terminals()` — `review`. None of
those imports `handover` today, so there is no cycle; **the implementer must re-check with
`python3 -c "import sys; sys.path.insert(0,'shared/lib'); import forge.handover"` after Task 4
and again after Task 5**, because "no cycle today" is a property of the current tree and not of
the design. `cli` imports `handover`, `gc`, `runner`, `launch`, `gate`, `preflight`,
`runstate`, `storage`, `taskbundle`, `ultra`, `verify`, `fingerprint` and `council.engine`; it
is the leaf and nothing imports it.

---

## What this plan could NOT specify, and why

Four items are **named here and deliberately not planned as steps**, because specifying them
would mean writing instructions to take a measurement this authoring pass may not take, or to
spend money §5.2 never quoted. Each is a real gap; none is silently dropped.

**1. §18's live three-provider write smoke, and its 180 s silent step.** §18 asks for a smoke
whenever adapter wiring changes: a tiny disposable repo, each provider writing a distinct
marker in its own clone and quoting the proof token, the engine harvesting, a trivial verify,
and a demonstration that the original checkout is unchanged — producing a source-hashed smoke
receipt (adapter hash, CLI versions, provider results, timestamp). **This spends real provider
calls on all three CLIs**, and Global Constraints forbid a test that does. It cannot be a
hermetic step, and I could not size it: I do not know what three full write-enabled forge seats
cost on this machine, and guessing a figure into a plan is the kind of unmeasured number this
project keeps finding. **It is a task of its own, budgeted by the owner, after Task 11.** The
180 s silent step belongs to it.

**2. §19's hour-long three-adapter probe.** §19's third item is "probe all three adapters over
an hour of silent subprocess waits before shipping" — an hour of wall clock and three live
sessions. §19 exists *because* an unmeasured timeout wall silently degraded a three-seat panel
to two, so the probe is not optional; but it is a measurement, not code, and Task 9 ships the
`MODE_TIMEOUT["forge"] = 3600` entry the probe would validate. **If the probe finds claude's or
codex's streaming path unhealthy across that duration, the entry is wrong and Task 9 must be
revisited.** Budget it as its own task and record the result.

**3. Measuring the synthesis in-engine, so `--collect` can say "verified" and mean it.** The
CLI is handed the fusion's verify verdict; it builds no verifier clone for the synthesis and
runs no confirmed command over it. Revision 1 makes the header say so — `synthesis_measured` is
`False`, the line reads *the orchestrator reports*, and §16.1's "Verified here means" paragraph
is not printed beside it — which closes the overclaim but does not produce the measurement.
Producing it is a whole §6 pass: a `bundle.CandidateBundle` over the synthesis worktree, a
`verify.Calibration` over B1 (which means **re-running the baseline gate**), and
`runner.verify_candidate`. That is a fourth verification `gate.quote` never priced, and §5.2
forbids spending an operator has not been shown. **It is its own task: extend the quote first,
then the flag.** Until then `synthesis_measured` has one producer and it writes `False`, which
is a true statement about this build rather than a placeholder.

**4. §22 Q5's peak-memory measurement and the concurrent-seat cap.** Still outstanding from the
scope map: nothing measures or caps memory, and §22 asks for a three-seat load, a derived cap
(plausibly 2 on this ~7.9 GB box), and an OOM classified as an **infrastructure** failure so
§12.3 never reads it as non-progress. The third clause is code and could have been planned; the
first two are a measurement that runs a real fleet. Planning the classifier without the number
would ship a cap of `None` — which is the "nothing leaves the same record as nobody" shape this
plan spends eleven tasks closing. **Its own task, after the smoke.**

**Two corrections to the brief, both measured (M7, M8):**

- The prose sweep is **18 citations across 10 modules** — 17 line-oriented across nine, plus
  one that only a flattened scan finds, in `harvest.py`, which appears in no line-match at all.
  Not "~27 line-matches across 12 modules". The `i2-branch-fix-report.md` headline disagreed
  with its own itemisation, which summed to 21 including the two already fixed. **And the
  report's regex over-matches**: it fires on `taskbundle.py:48`'s "the **plan-mode** and JSON
  forms", which is agy's mode. A sweep that mechanically rewrote everything the pattern found
  would have corrupted a correct sentence.
- There are **11 eval receipts, not 33**. And only `scripts/lib/reconcile.py` stales them —
  it is in `checks.LIB_SCRIPTS`, which is added to every skill's closure. **`scripts/eval_trigger.py`
  is in no skill's closure at all** (verified programmatically), so the brief's "each needing
  its own pass" holds for the first debt and not the second. Task 2 is still a separate task,
  because a reviewer can reject one and approve the other — but not for a receipt reason.

**One place the spec may be wrong, stated with the limit of what I measured:** §16's
implementation note says `git worktree add -b` "fires `core.fsmonitor` **twice**". Counted here
without deduplication, against a monitor script that appends one line per invocation, it fired
**once** — and `reference-transaction` fired **fourteen** times, which the note does not
mention at all. **I cannot say the note is wrong**, only that it does not reproduce on this
fixture: mine was a single-commit repository with no untracked-cache state, and firing counts
for an index-loading program plausibly depend on what the index holds. What is confirmed
exactly is the note's *conclusion* — `NO_DAEMON_CACHE` and `NO_HOOKS` together are necessary
and sufficient for every program that fires. **An implementer counting firings to check their
work should count the presets' effect (`fired: []`), not the unsuppressed baseline**, which is
what Task 4's test does.
</content>
</invoke>
