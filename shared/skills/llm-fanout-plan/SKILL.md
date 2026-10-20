---
name: llm-fanout-plan
description: Use when an approved implementation plan, a direct cross-model question, or an owned task bundle needs a skill-aware fanout execution plan before Claude, Codex, and agy work begins.
---

# llm-fanout-plan

Produce one validated DAG. Planning never starts providers or approves amendments.

For a v2 plan, map each source Task's `**Target:**` to one exact repository target. Distinct repositories may share a ticket key, branch name and relative paths. Keep each source Step on its declared target; compare owned paths by `(target_id, relative_path)` and declare `artifact` or `handover` for cross-target dependencies.

Executors produce read-only proposals using their normal installed skills, plugins, tools and authentication. Source changes belong to the invoking owner as `orchestrator-action` tasks, with exact paths and checks and at least one same-target read proposal dependency. Do not allocate repository-writing model seats. Group closely related code and tests into one useful proposal instead of splitting every check into a session.

For targets that need Git handover, verify author and committer identity under
hardened Git before recording baselines. Repository-local `user.name` and
`user.email` are explicit options; global Git identity is ignored. Missing
identity is an owner prerequisite, not a task for a model seat.

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

Execution sends the full admitted plan to every seat. Name exact evidence files and provenance, not merely directories or guessed manifests. Native seats can use installed read skills for repository, documentation and service lookups; a link is not proof of successful access. Capture the source baseline before execution and leave it unchanged. Authentication and read access do not authorize messages, configuration changes or other writes. These are trusted same-user CLI sessions, not OS-confined sandboxes.

Keep independent first turns separate from peer exchange. The full plan supplies
context, not permission to browse other tasks' answers or run/session stores.
Declare needed dependencies explicitly; later peer reads follow only the
coordinator's delivered evidence and exact handles. Native argument audits can
reject observed boundary breaches but cannot prove all same-user reads absent.

Claude, Codex, and agy are default seats; repeated `--executor` replaces them. Panels of one, two or more are valid; reconciliation requires every configured identity, not a fixed count of two. Equal answers still count as separate attributed results. Maka evaluates through its normal native read capabilities, not as a required fourth executor.

Set `rounds` to the default per-seat cap (default 2), and use `iteration_limits` to override individual executor IDs. Caps are integers 1–10; reject unknown identities. Tasks must be small enough for medium executor effort; planning, evaluation and merging use xhigh, while Gemini uses high for every role. Reserve the sum of seat caps for each work task, with zero automatic retries. A 2/3/4 panel therefore reserves nine turns, not twelve. Do not increase caps after observing an answer without an admitted amendment.

For a direct question, pass resolved `--skill` names or a reviewed `--none-reason`. Bundles declare `quality_tier`. After inspecting a write bundle, the invoking owner supplies a separate `--owner-review-file` with `reviewer` and `binding_sha256`. For a v1 bundle, use `fanout-owner-review-v1` and hash the bundle's canonical JSON. For a v2 bundle, use `fanout-owner-review-v2` and hash the canonical JSON of `{"schema_version":"fanout-owner-review-binding-v2","bundle":<finalized bundle>}`. The producer cannot include its own approval. This attestation is not cryptographic identity proof.

Admit a reviewed bundle with `from-bundle --bundle ... --owner-review-file ...`. `compile` and `validate` accept source and draft, not `--owner-review-file`; they never admit a write bundle. Reject an embedded owner-review field rather than stripping it. Finalize the bundle's source, draft, owned paths, checks, skills, and tier before review: changing any of those declared values changes its canonical digest and requires a new separate owner review and `from-bundle` admission.

## Map the work

1. Load the entire accepted plan. Keep each `Task N/Step M` whole. Combine dependent code and immediate test Steps in one bounded session; split only for ownership, class, or a useful dependency boundary. Source `Create`/`Modify` paths need one invoking-owner action; do not assign those writes to executor seats.
2. Declare every work item's Steps, dependencies, acceptance and executor skills or reviewed `none_reason`. Read-only proposals have no owned write paths. Each writing `orchestrator-action` needs source-listed paths, argv checks, and a same-target read proposal dependency; reject repository-writing worker dependencies. Groups express hierarchy and inherited skills, not work. Pushes, deployments, messages and deletions remain separate owner authority barriers.
3. Preserve source dependencies. Parallel tasks need independent results and nonoverlapping owned paths within each target. If independent Steps overlap on the same target, combine them when safe or seek a reviewed source amendment; never invent an ordering edge. Other independent, nonoverlapping tasks may still run in parallel after the whole plan validates.
4. Resolve executor skills across ordered repeated `--skill-root` roots; divergent shadows fail. For an unresolved required skill, require that exact named skill to be installed in an admitted root or obtain a reviewed source amendment that changes the requirement. A different or supposedly equivalent skill is a source change, never an unreviewed substitute; do not drop the requirement to meet a deadline. Planning, authorization, reconciliation, and Superpowers control stay with the invoking CLI. Seats later receive the whole plan, their task, and verified peer context.
5. Compile and validate before execution. Refuse missing write checks, skills, source edges, or review bindings; correct the input and recompile rather than deleting evidence.

Example: diagnosis precedes parser code-and-test and docs; the latter two run concurrently, then reconcile.

Common mistakes: treating source-plan acceptance as later bundle approval; splitting every Step; silently downgrading `deep`; claiming compilation proves installed deep-profile admission; naming an unresolved skill.
