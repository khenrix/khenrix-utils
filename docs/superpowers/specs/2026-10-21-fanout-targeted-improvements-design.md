# Targeted native fanout improvements

## Accepted intent

Improve the existing native fanout and portable setup using selected mechanisms from
Orca, Headroom, Hindsight, ai-memory and context-mode. Keep the current native CLIs,
skills, authorized read tools, memory stores, durable journal and owner-only writes.
Do not install another proxy, memory service, global hook or ranking system.

This design records the accepted research conclusions and subsequent role-policy
and child-model-picker amendments. It does not reopen the approved architecture.

## Baseline and roles

Start from the committed quality refresh `8be1a7495b3040f912ab28379e10ab2f071d687d`,
which includes khenrix-quality 1.1.1 and the current evaluation/read-proof helpers.
Preserve unrelated work and immutable historical receipts.

Implementation uses GPT-6.1 Sol at medium; Codex/Claude/Maka planning, review and
merging use xhigh. Gemini execution uses `gemini-3.8-flash-high` at high; planning,
review, evaluation and merging use `gemini-4-argon-high` at high. Initial and resume
commands, requested model, effort and profile identity must agree. Native managed
capability gates remain authoritative; unsupported selections must not silently fall
back to another model, account, project or region.

## Changes

1. Resolve effective HOME, CODEX_HOME and CLAUDE_CONFIG_DIR from the actual child
   environment once. Discovery, configuration readers, write-policy construction,
   trace capture and launch diagnostics use that same interpretation. Record paths,
   not credentials or complete environment/configuration documents.
2. Publish portable JSON/TOML changes atomically, preserving mode, backups, unrelated
   data and existing malformed-input refusal. Reject changed preimages instead of
   applying stale decisions. Preserve legitimate sequential baseline/defaults updates
   by rebuilding their reports. A comparison before replacement is not a claim of
   atomic compare-and-swap against uncooperative external editors.
3. Extend existing usage evidence rather than create another accounting system.
   Preserve raw numeric provider counters and their semantics, distinguish missing
   counters from zero and estimates from observations, and avoid double counting
   resumed cumulative counters or cache reads. Include all participating turns,
   reference reads, owner synthesis and evaluation in experiment totals.
4. Add an optional, versioned skill-presentation projection. Default delivery remains
   complete. Only an explicitly reviewed exact admitted tree may omit noninstruction
   helper/assets/packaging contents from the prompt. SKILL.md, all text instruction
   documents and any reviewed required resources in other formats remain inline.
   The full verified tree and exact authorized native paths remain available.
   Available, inline-delivered and observed-complete-read are different facts. Verify
   every staged file, including omitted files, before every turn. Bind the projection
   policy/version to saved evidence; cold recovery cannot silently change it. Unknown
   trees fall back to complete delivery. Do not infer required resources solely from
   Markdown links: backtick references and conditional instructions exist.
5. Extend the existing knowledge/context corpus for chronology, source independence,
   failed required reads and last-good-artifact preservation. Retain exact citations,
   source authority and published timestamps. Do not claim deterministic semantic
   conflict resolution or savings from upstream benchmark figures.
6. Add a disabled-by-default, child-only fixed-choice model-selection seam. An
   executor's own identity and outer planner/merger roles remain fixed. A separate
   child route ID identifies an exact native model/effort/profile; local code filters
   approved usable routes before asking a backend. Provider-local selection is the
   default; explicit cross-family admission may widen it. Returned choices cannot
   authorize routes, tools, writes, credentials or new roles. Decisions is the first
   backend; a Jev adapter can implement the same choice contract. Refusal, unavailable
   usage, invalid output or low confidence is explicit; fallback may use only an
   already admitted eligible fixed route. Thresholds are backend-specific and must
   not be described as calibrated accuracy without a labeled evaluation.

Planned groups may nest without a new arbitrary depth limit; executable work remains
at the leaves. Each admitted work item can use all three model seats and its own peer
iteration policy. Keep the separate nested-fanout guard. Child delegation is not a
recursive invocation of the complete fanout engine. Native descendant launch is
unsupported until model, session, tool, budget, cancellation and parent/child trace
contracts are measured. Do not relabel the existing parent-only audit as child proof.

## Evaluation and delivery

Retain every configured outer final and independent first-turn blindness. The owner
reconciles all configured identities, even equal answers. Preserve iteration caps and
zero automatic provider retries. Evaluation answer-only controls are opt-in for
fully-inline fixtures and must not disable normal native fanout reads.

Use the existing executable patch checker and complete native read aggregation where
applicable. Keep historical results unchanged. Safety fixes can ship independently
of an unsuccessful optimization. The conservative projection stays off globally until
representative paired native evidence shows at least 20% lower aggregate measured
input tokens, no correctness/capability/citation regression, and no more than 10%
worse median latency. Missing counters retain the baseline; prompt bytes are not
provider tokens. A small pilot is preliminary, not global certification.

Public source and tests stay generic. Machine-specific route approval, credentials
and source-pin adoption belong in the private setup owner. No external message or
comment is authorized by execution of this design. New paid campaigns need an exact
fresh allocation; previously spent approvals and receipts are not reused.
