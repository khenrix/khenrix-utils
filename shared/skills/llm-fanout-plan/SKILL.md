---
name: llm-fanout-plan
description: Use when an approved implementation plan, a direct cross-model question, or an owned task bundle needs a skill-aware fanout execution plan before Claude, Codex, and agy work begins.
---

# llm-fanout-plan

Produce one validated DAG. Planning never starts providers or approves amendments.

For a v2 plan, map each source Task's `**Target:**` to one exact repository target; distinct repositories may share a ticket key and relative path names. Keep each work task and source Step on its declared target. A repository writer owns only one target, with at most one repo-write work task per target. Compare owned paths by `(target_id, relative_path)`, and declare `artifact` or `handover` mode on each cross-target dependency.

Current public v2 execution is no-spend: admission and a dormant `start`/read-only `status` are supported, but public `resume` refuses provider turns for both read-only and repo-write work. Green compilation, profile preflight, or a test-injected transport does not lift that temporary gate. Do not promise v2 model work as the next step.

## Choose the ingress

| Input | Plan shape | Admission |
|---|---|---|
| Direct question only | One `read-only` task; CLIs are seats, not tasks | Invocation-authorized; explicit tier |
| Approved Superpowers plan | Partition whole Steps; preserve `Depends on` edges | Review source and draft |
| Owned task bundle | Preserve paths, checks, skills, and dependencies | Separate digest-bound owner review for writes |

If a question belongs to an accepted implementation plan, keep it inside its source Step. Do not invent another task. Direct-question ingress is for a question that is the entire request.

If implementation has no accepted plan, **REQUIRED SUB-SKILLS:** use `brainstorming`, then `writing-plans` before fanout compilation.
Keep executable Steps and checklists inside source Tasks. Global Constraints, pre-task Review Focus, and pre-task File Structure may contain declarative bullets; other document sections may contain prose, not lists or hidden work. Every source Step needs an immediate `Depends on` line naming earlier Step IDs or `none`. For an older accepted plan without these edges, request a reviewed source amendment rather than guessing concurrency.

Use `python3 scripts/plan.py --help` beside this file: `from-question`, `from-bundle`, `compile`, `validate`. Commands emit JSON. Keep questions in files, never argv; store source/draft/compiled evidence in private run state. Choose `normal` or `deep` explicitly for a direct question or bundle. Source and bundle tiers bind plan defaults: `normal` maps to `standard`, while `deep` requires read-only deep work throughout. A standard plan may give a read-only task its own deep policy. Refuse hidden or malformed quality-tier hints in source Global Constraints. Compilation does not characterize an installed CLI. Execution preflight must admit each selected profile and installed version before provider spend.

Execution sends the admitted source Markdown to every seat, but a link or absolute local path inside it does not grant file access. For source-backed questions, put bounded evidence in the source or commit exact, provenance-labeled files to the caller repository before execution captures its baseline. Include an exact relative evidence index such as `evidence/README.md` and name the required files in the admitted question; a directory alone is not a readable file. Seats should use their permitted directory-list and file-read tools, without assuming shell access. Cite paths relative to that baseline; restricted seats may not read other host directories. Do not stage evidence after a run starts or use a symlink to evade the workspace boundary.

Claude, Codex, and agy are default seats; repeated `--executor` replaces them. Maka routes or evaluates, never executes.

For a direct question, pass resolved `--skill` names or a reviewed `--none-reason`. Bundles declare `quality_tier`. After inspecting a write bundle, the invoking owner supplies a separate `--owner-review-file` with `schema_version: fanout-owner-review-v1`, `reviewer`, and `binding_sha256` of its canonical JSON. The producer cannot include its own approval. This attestation is not cryptographic identity proof.

Admit a reviewed bundle with `from-bundle --bundle ... --owner-review-file ...`. `compile` and `validate` accept source and draft, not `--owner-review-file`; they never admit a write bundle. Reject an embedded owner-review field rather than stripping it. Finalize the bundle's source, draft, owned paths, checks, skills, and tier before review: changing any of those declared values changes its canonical digest and requires a new separate owner review and `from-bundle` admission.

## Map the work

1. Load the entire accepted plan. Keep each `Task N/Step M` whole. Combine dependent code and immediate test Steps in one bounded session; split only for ownership, class, or a useful dependency boundary. A source Task that declares `Create`/`Modify` paths must have one repo-write owner; if those paths genuinely need separate owners, amend the accepted source plan into separate Tasks before compiling. Read-only source Steps may join distinct writers when their write paths come from other source Tasks.
2. Declare every work item's class (`read-only`, `repo-write`, `orchestrator-action`), Steps, dependencies, paths, acceptance, and executor skills or reviewed `none_reason`. A repo-write task must own at least one source-listed path; all source `Create`/`Modify` paths need write ownership, while `Test` paths are optional. Each write task needs argv checks. Read-only answers are artifacts: do not invent a workspace file check. Groups express hierarchy and inherited skills, not work. Approvals, pushes, deploys, and deletions are invoking-CLI action barriers.
3. Preserve source dependencies. Parallel tasks need independent results and nonoverlapping owned paths within each target. If independent Steps overlap on the same target, combine them when safe or seek a reviewed source amendment; never invent an ordering edge. Other independent, nonoverlapping tasks may still run in parallel after the whole plan validates.
4. Resolve executor skills across ordered repeated `--skill-root` roots; divergent shadows fail. For an unresolved required skill, require that exact named skill to be installed in an admitted root or obtain a reviewed source amendment that changes the requirement. A different or supposedly equivalent skill is a source change, never an unreviewed substitute; do not drop the requirement to meet a deadline. Planning, authorization, reconciliation, and Superpowers control stay with the invoking CLI. Seats later receive the whole plan, their task, and verified peer context.
5. Compile and validate before execution. Refuse missing write checks, skills, source edges, or review bindings; correct the input and recompile rather than deleting evidence.

Example: diagnosis precedes parser code-and-test and docs; the latter two run concurrently, then reconcile.

Common mistakes: treating source-plan acceptance as later bundle approval; splitting every Step; silently downgrading `deep`; claiming compilation proves installed deep-profile admission; naming an unresolved skill.
