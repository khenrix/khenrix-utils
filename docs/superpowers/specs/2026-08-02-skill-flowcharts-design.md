# Skill flowcharts with provable gates + cross-CLI feedback loop — design

**Date:** 2026-08-02 (updated 2026-08-04)  **Status:** design (pre-plan)  **Repo:** khenrix-utils
**Extends:** `2026-07-30-per-provider-eval-gating-design.md` (independent; shares its
closure constraints)

## Goal

Every skill gets one mermaid flowchart that explains how it works — simple, clear,
complete — and the chart is a **maintained artifact with an honesty contract**, not
decoration: every decision diamond ("gate") in a chart must name the evidence that
proves it behaves as drawn. skill-tuneup creates, updates, and audits these charts.
Separately, skill-tuneup gains a cross-CLI feedback-loop rule: a provider-specific
finding is not closed until it has been checked against the other two CLIs.

## Why the honesty contract is the centerpiece

This repo's single most persistent defect class — named by three consecutive
convergence runs — is **documentation asserting behaviour the code does not have**.
A flowchart is another prose surface that can lie, and it lies more convincingly than
text. So a chart here is only allowed to draw a gate if it can point at the thing that
proves the gate: a deterministic test for code-enforced gates, an eval assertion or
audit item for agent-enforced ones. A gate with no evidence is a lint failure, not a
style choice.

## Placement — `docs/skill-charts/<skill>.md`

Charts live **outside every hash closure**:

- Adding a file under `shared/skills/<name>/` puts it in that skill's source closure
  (`checks._skill_source_files` rglobs the skill dir) — authoring the charts there would
  stale **all** receipts at once, the 252-run trap the 2026-07-30 council review caught.
- Embedding charts in SKILL.md bodies would change the eval harness's `with_skill`
  inputs, count against the 500-line body budget, and push a human-facing diagram into
  the executor's context.
- `docs/` is in no closure; mermaid renders natively on GitHub and in Obsidian.

Audience is the human maintainer. The plugin does not ship charts.

## Style contract

Derived from current mermaid guidance (sources at bottom); enforced by review, with the
mechanical parts linted:

1. **One `flowchart` per file**, `TD` for decision-heavy skills, `LR` for linear
   pipelines. Exactly one ```mermaid block per chart file.
2. **≤ ~25 nodes, one chart per file.** A skill that exceeds the budget references its
   failure-handling table instead of drawing every branch — completeness is achieved by
   the Gate evidence table, not by node count.
3. **Subgraphs for phases**, max 2 levels of nesting.
4. **Semantic node IDs** (`BASELINE`, `G_CONVERGED`), never single letters.
5. **Shape semantics:** stadium `([...])` = start/stop/halt, rectangle = action,
   diamond `{...}` = gate. Every diamond's ID starts `G_` — that prefix is what the
   lint keys on.
6. **Edge labels carry the condition** (`-- "cap reached" -->`), node text stays short
   (`<br/>` for line breaks, no paragraphs in nodes).
7. **Accessibility:** `accTitle` + `accDescr` at the top of every chart.
8. **`%%` comments** for anything non-obvious in the source.

## The gate-evidence taxonomy

Every chart file contains a `## Gate evidence` table with one row per `G_*` diamond:

| Gate | Kind | Evidence |
|---|---|---|
| G_CONV | code | `shared/skills/skill-tuneup/scripts/tuneup.py::a clean final cycle converges` |
| G_CHECK | agent | SKILL.md Step 7 hard stop; `evals/skill-tuneup/evals.json::Proposes the change as a checkpoint finding` |

(Gate IDs in the table may be bare or backticked — the lint's row regex tolerates
both, since authors will write `` `G_X` `` and a cosmetic backtick must not read as a
missing row. Both evidence strings above are verified present in their files; every
fragment cited anywhere must be grepped before it is written, including in this spec.)

Two kinds, honestly distinguished:

- **`code`** — the gate is enforced by a deterministic test or check. Evidence is a
  `` `path::label` `` reference; the lint verifies the file exists **and contains the
  label string** (a self-test check name, a make target's assertion, a function name).
  Deleting the test breaks the chart's lint — the chart cannot outlive its proof.
- **`agent`** — the gate is a process rule the orchestrating model enforces (a
  checkpoint, a scope rule). No test can prove an LLM will stop; claiming one would be
  the exact defect class this design exists to kill. Evidence cites the
  `` `evals/<skill>/evals.json::<assertion fragment>` `` that covers it — assertion
  text is a literal string in a file, so the lint resolves it with the same grep the
  `code` path uses. Prose-only evidence is accepted only when no eval covers the gate,
  and says so ("no eval covers this; SKILL.md Step N rule"). Every reference present
  in a row is resolved, whatever the kind.

This is the user requirement "tests for each of the gates, to prove they work as we
assume" implemented without over-claiming: code gates are proven, agent gates are
covered and labeled as such.

## The lint — `scripts/lib/charts.py`

Stdlib-only, `--self-test`, called from `checks.run_all()` (same hook point as the
portability module in the companion plan; **never** `render.py`, which is in
`GLOBAL_INPUTS`). Rules:

1. A chart file exists for every dir under `shared/skills/` and
   `shared/skill-templates/` that contains a `SKILL.md` or `SKILL.md.tmpl`.
2. Exactly one ```mermaid block, starting with `flowchart`.
3. Every `G_*` diamond in the chart has exactly one Gate-evidence row; every row
   matches a diamond; a duplicate row for the same gate is an error (a dict-keyed
   parse would silently keep one of them). Row IDs may be bare or backticked — both
   parse; a cosmetic backtick must never read as a missing row.
4. Every `code` row carries at least one `` `path::label` `` reference. **All**
   references present in any row are resolved: file exists, label string found in it.
5. A `code` row citing a `.py` file additionally requires that path to appear in the
   `Makefile` — proof the suite is actually run by a target, not merely present on
   disk. The Makefile's own comment states the failure mode: suites no target runs
   rot. (Non-`.py` evidence like `Makefile::precommit` is exempt.)
6. Kind must be `code` or `agent`.
7. **Fail loud, never open:** if the skill scan finds zero skills in a tree that has a
   `capabilities.toml`, that is an error, not a clean pass — the same vacuous-green
   defect the bats runner and `forge_packaging` already guard against.

The lint is syntactic. It cannot prove the drawn flow matches the SKILL.md — that
semantic check is skill-tuneup's audit job (below). Stated as a limit, not hidden.

## skill-tuneup integration

- **Create/update:** Step 8 gains: if the run changed the target's flow (steps, gates,
  failure paths), update `docs/skill-charts/<target>.md` in the same pass. A missing
  chart for the target is scaffolded per this spec (full-gate targets only;
  council-only targets get a chart offered in their own repo's docs, not forced).
- **Audit:** `references/audit-checklist.md` gains a Chart section: does the chart
  match the body's current steps, gates, and failure exits? Chart-vs-body drift is a
  **Stale-reference** finding like any other doc drift.
- **Convergence:** no new gate. Chart drift found in a cycle is a finding in that
  cycle; the existing severity rules apply.

## Cross-CLI feedback loop

New rule in skill-tuneup (Step 5 research + audit checklist + run log): **a
provider-specific finding is not closed until probed on all three CLIs.** A CLI bug,
flag change, timeout behaviour, parse quirk or capability gap found on one provider is
checked against the other two before it is recorded. The record is **one required
sentence** in the run-log entry naming what was checked and what was found — not a
mandatory structured blob, which would tax the common case where two of three answers
are "n/a, that CLI has no such flag". A structured per-provider record is reserved for
findings that touch a **shared code path** (the sentinel lists, timeout mapping, output
parsing) — exactly where the ad-hoc version of this loop has already paid off.
Precedent already in the repo: codex's version-gate wall is recognised only in codex's
phrasing — "the same wall on another CLI lands in `nonzero_exit` until its string joins
`PERSISTENT_SENTINELS`" (llm-council SKILL.md). That is this loop, done ad-hoc; the
rule makes it standing.

## Validation — the self-run

After the charts, lint, and SKILL.md changes land, run a full skill-tuneup deep run
**on skill-tuneup itself**. It exercises every changed surface at once: the new chart
maintenance step, the feedback-loop rule, its own updated chart, and (if the
2026-07-30 plan has been executed) the per-provider measurement. Its eval loop re-earns
the receipt the SKILL.md edits staled.

## Testing

`charts.py --self-test`: temp-tree cases — missing chart, two mermaid blocks, gate
without row, row without gate, unresolvable code evidence, resolvable evidence, agent
row with prose-only evidence, non-flowchart block. Wired into `make eval-test`.

## Risks

- **Charts can still lie about flow order** — the lint checks gates and evidence, not
  topology. Mitigated by the audit item; accepted as a limit.
- **Label-grep evidence is weak coupling** — a renamed self-test label breaks the lint
  (good) but a *weakened* test with the same label passes it (undetected). The eval
  and mutation-testing practice covers that layer.
- **12 charts is a real authoring cost** and each is a new surface to keep current.
  Proportionality: charts change only when flow changes, and the maintainer is
  skill-tuneup, whose runs already read the body closely. The projected yield is
  uneven — prose-only skills (markitdown) produce all-`agent` tables, engine-backed
  skills (mikado-graph, skill-tuneup, llm-council) carry the `code` rows — which is
  accepted: the user requirement is a chart per skill, and under the resolvable-
  assertion rule even all-`agent` tables are lint-verified, not prose.
- **A new skill trips the presence rule the day it lands**: `make verify` goes red
  until its chart exists. This is deliberate — a new skill owes a chart in the same
  commit, exactly as it owes an eval set — and is documented in CLAUDE.md so the skill
  author learns it before the gate teaches it. (This fired during planning:
  `shared/skills/llm-forge` shipped 2026-08-04, making it the 12th chart in the plan.)
- **SKILL.md edits stale skill-tuneup's receipt** — re-earned by the self-run's eval
  loop (cost note in the plan).

## Sources

- [Mastering Diagramming as Code — kallemarjokorpi.fi](https://www.kallemarjokorpi.fi/blog/mastering-diagramming-as-code-essential-mermaid-flowchart-tips-and-tricks-2/)
- [Mermaid flowchart sizing and layout best practices — mermaidcreator.com](https://www.mermaidcreator.com/blog/mermaid-flowchart-sizing-layout-best-practices)
- [A Complete Guide to Flowcharts with Mermaid — diagramly.ai](https://docs.diagramly.ai/posts/2024/flowchart-guide-mermaid/)
- [Flowcharts Beyond the Basics with Mermaid — reliablepenguin.com](https://blogs.reliablepenguin.com/2025/12/26/flowcharts-beyond-the-basics-with-mermaid)
- [Mermaid Subgraphs guide — mermaideditor.lol](https://mermaideditor.lol/blog/mermaid-subgraph-guide)
