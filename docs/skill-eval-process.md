# Skill-eval process (provider-agnostic)

**Every change to a skill in this repo must be eval-tested and blind-reviewed before it
is committed.** This is a hard gate, not a suggestion. The point is the same rigor the
`llm-council` work proved out — with-skill vs baseline, judged against assertions, then a
blind A/B — applied to *every* skill and *every* provider, not just Claude.

This repo ships a portable, stdlib-only harness (`scripts/eval_harness.py`) that delivers
that loop for all three CLIs. Claude's `skill-creator` and Codex's native skill tooling
are optional accelerators on top (see below) — the harness is the baseline that also
covers agy, which has no native skill tooling at all.

## The loop

1. **Edit the skill.** For shared skills, edit `shared/skills/<name>/SKILL.md`. For the
   templated per-CLI skills, edit `shared/skill-templates/<skill>/SKILL.md.tmpl` (shared
   prose) and/or the `[skill_facts.<skill>.<cli>]` facts in `capabilities.toml`. Then
   `python3 scripts/render.py` so the rendered bodies the harness runs are current.
2. **Write/refresh evals.** `evals/<skill>/evals.json` — 2-5 cases, each with `prompt`,
   optional `files`, and `assertions`. Make assertions **discriminating**: things a
   no-skill baseline would structurally fail (that gap is the skill's measured value).
   Objective and answer-only — "runs the engine read-only first", not "is well written".
3. **Run with-skill vs baseline, per provider.** `make eval SKILL=<name>` (defaults to
   the `claude` executor; add `PROVIDERS=claude,codex,agy` for the full panel). The
   harness injects the rendered SKILL.md for the with_skill run and uses the bare prompt
   for the baseline.

   **Baseline caveat:** `without_skill` is the executor's *ambient* environment — truly
   skill-free only if the skill isn't already installed on that CLI. If it is installed
   (a prior `make khenrix-refresh`), it can auto-trigger and the baseline becomes the
   *old installed version*, so the comparison is new-body-vs-old, not with-vs-without.
   For the cleanest signal, iterate with the harness BEFORE refreshing/installing the
   change. The blind A/B and delta stay meaningful either way.
4. **Grade.** An LLM judge scores each output against the assertions → `grading.json`
   (`text`/`passed`/`evidence`).
5. **Blind A/B.** The two outputs are shuffled into A/B (with a hidden key) and the judge
   picks the better one blind → `comparison.json`, then de-anonymized.
6. **Iterate** until with_skill consistently beats baseline on the discriminating
   assertions (`run_summary.delta.pass_rate >= 0`, and the blind winner is `with_skill`).
7. **Only then** `make verify && make eval-test && make eval SKILL=<name>` → commit.

## Layout

```
evals/<skill>/evals.json          # committed: id / name / prompt / files / assertions
evals/<skill>/workspace/          # gitignored (evals/*/workspace/)
  iteration-N/
    eval-<id>-<name>/
      <provider>__with_skill/     {prompt.txt, answer.md, grading.json, <fanout artifacts>}
      <provider>__without_skill/  {prompt.txt, answer.md, grading.json, …}
      comparison.json             # blind A/B verdict, de-anonymized
    benchmark.json                # metadata + runs[] + run_summary{with_skill,without_skill,delta}
```

The artifact schema matches `skill-creator`'s (`grading.json` / `benchmark.json` /
`comparison.json`), so the two interoperate — you can grade with one and aggregate with
the other.

## Commands

```bash
make eval-test                              # hermetic harness logic tests (no tokens)
make eval SKILL=khenrix-setup               # claude executor, normal mode
make eval SKILL=khenrix-setup PROVIDERS=claude,codex,agy MODE=deep
```

Notes: executors run **read-only / plan-only** by default (`make_readonly` swaps each
provider's bypass flag — claude `--permission-mode plan` plus plan-file suppression
(`--disallowedTools ExitPlanMode` + an appended system prompt), codex `--sandbox read-only`;
agy gets two SOFT layers — a READONLY_POSTURE line prepended to every executor's prompt
and a throwaway git-worktree cwd, since its sandbox hangs headless (see `make_readonly`'s
docstring); cwd-relative writes are discarded but absolute-path writes remain possible —
so a skill that mutates config (`khenrix-setup`/`khenrix-upgrade`) is
mechanically constrained on claude/codex during an eval, while the real HOME is kept so auth still resolves
(sandboxing HOME instead hid credentials and every run failed `auth_or_quota`). Full
three-provider runs are token-expensive (~3-4×); use the single-provider `claude` loop for
iteration and the full panel for the final gate. `--no-readonly` opts out when a skill
genuinely must write. agy's containment is posture + worktree, not a sandbox — lower-risk, not sealed.

## Per-provider tooling (accelerators, not the gate)

- **Claude** — `skill-creator` (installed via `claude-plugins-official`) has the richest
  loop: grader/comparator/analyzer subagents, an eval viewer, benchmark variance, and
  trigger-description optimization. Prefer it when authoring on Claude. We **reference** it;
  we do not vendor it. Its artifact schema is the one this harness emits.
- **Codex** — `~/.codex/skills/.system/skill-creator/` scaffolds + validates
  (`quick_validate.py`), but has **no** structured evals/blind-review. Use it for
  scaffolding/validation; use this harness for the eval loop.
- **agy** — no native skill tooling. The harness is the only eval path; `agy plugin
  validate` covers manifest validation.

## Orchestrator skills (llm-council)

`llm-council` is special: harness executors run under `LLM_COUNCIL_DEPTH=1`, so an
injected body cannot convene a real nested council — the with-skill/baseline benchmark
still runs but its delta measures solo answers and is **advisory only**, never the
receipt gate. What earns the receipt is the model/mode wiring verified
**deterministically** by `python3 shared/skills/llm-council/scripts/fanout.py
--self-test` and a live `--smoke` (inspect the manifest's `model`/`thinking` and
`[mode: …]`). Its synthesis quality has a bespoke blind-review workspace under
`evals/llm-council/` (authored with skill-creator).

## Maintenance runs (skill-tuneup)

The `skill-tuneup` skill automates this loop for periodic maintenance of an existing
skill: it researches upstream drift since the target's last substantive commit, audits,
applies user-approved fixes, scaffolds a missing eval set per this doc, and iterates
`make eval` to a fresh receipt before committing. Its per-target decisions live in
`docs/tuneups/log/`.
