# llm-forge: the council replaces the cloud review — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** llm-forge's post-fusion deep review runs as a local llm-council fan-out instead of the billed cloud review, leaving **zero ultra traces** in both `llm-forge` and `llm-council`.

**Status:** GRADUATED from `2026-08-02-skill-charts-and-feedback-loop.md` Task 11 on 2026-08-07, under that task's own scope-honesty clause. Measured then: `shared/lib/forge/ultra.py` is 564 lines with a strict-invariant dataclass, imported by `handover.py`, `cli.py`, `gate.py`, `review.py`, and referenced by five suites (`test_forge_gate.py` alone is 1,303 lines). Estimated 800–1500 changed lines in a subsystem that took 4,300 insertions in the two days before graduation. That is not a task to absorb into a docs-and-lint plan, and it cannot be validated cheaply — a real forge run is ~19 provider calls across 19 clones peaking near 63.3 GB.

**Already true (verified 2026-08-07):** `llm-council`'s sources carry **no** ultra references. Only the forge half of the zero-trace requirement is outstanding.

## Global Constraints

- **Python is stdlib-only.** Never edit `scripts/render.py` or `marketplaces/**`.
- **`scripts/lib/checks.py` is in llm-forge's closure** (`SKILL_EXTRA`) *and* bundled via `SHARED_LIB_FILES` — any edit to it owes `make eval SKILL=llm-forge` plus staging the three plugin copies.
- **The forge suites ARE the receipt gate** (`deterministic_gate: forge-handover-cli-gc-suites`). TDD is not optional here: extend the suites first.
- **Precondition 0:** `git status --porcelain` clean, and no forge work in flight from another session — this plan rewrites files another session may be editing. Check `git log --oneline -5 -- shared/lib/forge/` before starting.
- **Precondition 1 — no ultra-era runs may survive the change.** Existing handover records carry the ultra status vocabulary; code that no longer knows it cannot honestly `--collect`/`--gc` them. Run `--collect` on anything uncollected and `--gc` every finished run BEFORE Step 1, and confirm `git branch --list 'forge-c*'` is empty.

## Design (decided, ready to implement)

**Delete `ultra.py`; add `shared/lib/forge/deepreview.py`.** Not a rename — the failure vocabulary genuinely changes and a renamed module carrying cloud reasons would be the "documentation asserting behaviour the code does not have" defect in module form.

| Concern | Cloud review (today) | Council review (target) |
|---|---|---|
| invocation | `claude ultrareview` subprocess | `engine.run_council` over the fused diff, `--mode deep` |
| statuses | `ran / unavailable / timeout / skipped` | **keep these four** — they describe the outcome, not the mechanism |
| reasons | `no_auth, zdr_org, diff_too_large, usage_credits_off, exit_1, …` | `no_valid_seats, engine_missing, diff_too_large, depth_guard, exit_1` |
| findings | `bugs.json` payload | parsed from each valid seat's answer under a JSON output contract |
| `session_url` | claude.ai session link | **drop**; replace with `manifest_path` (the fan-out manifest) |
| cost | \$5–25 usage credits | ~3 provider calls at deep mode |
| depth | remote fleet + verification | 3 independent families; verification is future work (see the council study addendum) |

`Ultra`'s invariants are the valuable part and transfer verbatim: `bugs is None` for every status but `ran`; `reason` belongs to `unavailable` alone; `diff_measured=False` when the line count could not be taken. Keep all four `__post_init__` refusals.

**Depth guard:** forge must invoke the council at top level only — `LLM_COUNCIL_DEPTH` is already the engine's recursion guard; a seat that tries to convene its own council must land in `depth_guard`, not hang.

## Tasks

- [ ] **Task 1 — Extend the suites FIRST (TDD).** In `tests/test_forge_verify.py` / `test_forge_handover.py` / `test_forge_gate.py`: the four status invariants against the new reason vocabulary; a degraded panel (1 of 3 seats) still produces `ran` with the findings it got; zero valid seats produces `unavailable/no_valid_seats` and **not** an empty-findings `ran`; the depth guard produces `depth_guard`. Run them; they must FAIL.
- [ ] **Task 2 — Write `deepreview.py`.** Port `measure_diff`, `_numstat_z`, `DiffSize`, `_bugs` severity mapping and the dataclass invariants unchanged; replace `argv`/`_child_env`/`classify`/`session_url` with the council call. Suites go green.
- [ ] **Task 3 — Reprice `gate.py`.** The quote changes from usage credits to provider calls; `--start`'s printed quote must match what a run now actually spends. Update the pricing assertions in `test_forge_gate.py`.
- [ ] **Task 4 — Rename the opt-out flag.** `--no-ultra` → `--skip-deep-review`, preserving `_ultra_line`'s hard-won skip-vs-refused distinction (read that docstring before touching it: the skipped line reads the record's own reason rather than naming the flag, because `skipped` is the status ANY caller writes). Update `cli.py`, `handover.py`, and the CLI suites.
- [ ] **Task 5 — SKILL.md.** Re-price the default-run quote; remove every cloud-review mention; describe the council backend and the new flag.
- [ ] **Task 6 — Zero-trace acceptance (BLOCKING).**
```bash
grep -ril "ultra" shared/lib/forge/ shared/lib/council/ \
     shared/skills/llm-forge/ shared/skills/llm-council/ \
     marketplaces/*/plugins/khenrix-utils/lib/forge/ \
     marketplaces/*/plugins/khenrix-utils/lib/council/ \
     marketplaces/*/plugins/khenrix-utils/skills/llm-forge/ \
     marketplaces/*/plugins/khenrix-utils/skills/llm-council/ tests/test_forge_*.py
```
Expected: **no output.** (Note: the council's `MODES` uses the tier names `ultracode`/`ultra`, which are CLI effort values, not the cloud features — if those match, narrow the pattern to `ultrareview|ultraplan` and say so here.) `docs/superpowers/` is exempt: it documents the removal.
- [ ] **Task 7 — Gates.** `make eval SKILL=llm-forge` (deterministic suites), `python3 scripts/render.py`, stage `shared/lib/forge/` + the three `marketplaces/*/plugins/khenrix-utils/lib/forge/` copies + the SKILL.md + its three rendered copies + `evals/llm-forge/receipt.json`, `make precommit`, one commit.
- [ ] **Task 8 — One real forge run.** The suites are hermetic; a council-backed review has never actually reviewed a fused diff. Run `--start` on a small real task, let it reach the review phase, confirm the handover names the council backend and its seat count, then `--gc`. Budget it: ~19 provider calls.

## Risks

- **The council is not a verification fleet.** The cloud review reproduced every finding before reporting; three independent seats do not. Findings will be *less* filtered. The complementary work is the verified-findings phase in the council study addendum — until that lands, expect more noise per finding and say so in the SKILL.md.
- **`gate.py` prices before spending.** A wrong quote is a promise broken to the operator; the repricing must be measured against a real run (Task 8), not estimated.
- **Another session may be mid-forge.** Precondition 0 is not ceremony — this plan rewrites files that changed 4,300 lines in 48 hours.
