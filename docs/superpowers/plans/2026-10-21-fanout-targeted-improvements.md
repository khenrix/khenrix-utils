# Targeted Fanout Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development.
> Implementation tasks use GPT-6.1 Sol medium; independent reviews use xhigh.

**Goal:** Improve native launch fidelity, portable write safety, evidence accounting
and optional skill presentation without replacing the existing runtime.

**Architecture:** Reuse existing profiles, process supervision, staging, journal,
evaluation and delivery. Separate immutable source/admission evidence from optional
prompt presentation. Keep the child chooser an opt-in extension rather than an
alternative outer orchestrator.

**Tech Stack:** Existing Python standard-library engines, pytest, native CLI/Node
fixtures, mise-pinned tools and the current source-owner delivery commands.

**Spec:** `docs/superpowers/specs/2026-10-21-fanout-targeted-improvements-design.md`.

## Global Constraints

- Baseline is quality refresh `8be1a7495b3040f912ab28379e10ab2f071d687d`; preserve quality 1.1.1.
- Full native authorized read skills, MCPs and authentication remain available.
- Keep every configured outer final, peer attribution, first-turn blindness and owner-only writes.
- Sol execution is medium; Codex/Claude/Maka review and merging are xhigh.
- Gemini execution is 3.8 Flash High; planning/review/evaluation/merge are 4 Argon High.
- No provider auto retries, route/account fallback, new memory service or global proxy.
- Preserve public caller validation, useful tests, packaging, exports and historical evidence.
- Public changes are generic; private machine adoption stays with its setup owner.
- No external messages, comments, publication or production deployment without separate authority.

## Review Focus

- An overridden native config root must not disagree with trace, policy or diagnostic roots.
- Stale config preimages and failed publication must preserve existing data and permissions.
- Omitted prompt files still participate in full-tree integrity and cold recovery.
- Missing/cumulative/cache usage must not manufacture savings or duplicate consumed turns.
- Descendant model choice must not widen authority or masquerade as verified child execution.

## Execution arrangement

One integrator owns Git index/commits. The user's explicit parallel-work preference
permits disjoint implementers in this existing workspace; each receives exact path
ownership and does not stage or commit another task's changes. Reviews are isolated
from producer reasoning. Canonical task status is in claude-mem; this plan's ignored
SDD ledger stores recovery evidence and rulings, not a competing authoritative todo.

### Task 1: Effective native context and role-model coherence

**Target:** Existing source workspace.
**Files:** Shared native context helper, fanout process/providers/native_trace/diagnostic/eval_provider,
portable inventory/doctor consumers and focused native/profile tests. Coordinate
standalone reconciliation imports/rendering with Task 2; do not edit its owned files concurrently.
**Interfaces:** A single stdlib root resolver accepts an explicit environment mapping.
Launch evidence stores only nonsecret paths. Initial/resume/model/effort/profile digest
remain coherent; evaluator and owner Gemini profiles select Argon, executors Flash.

- [ ] **Step 1:** Add failing real-behavior tests for overridden roots and role-specific initial/resume selection.
  Depends on: none
- [ ] **Step 2:** Implement the resolver and captured launch/trace binding; complete affected consumers.
  Depends on: Step 1
- [ ] **Step 3:** Run native/profile/process/evaluator tests; record RED/GREEN and exact commands.
  Depends on: Step 2
- [ ] **Step 4:** Integrator commits the owned diff; independent reviewer checks spec and quality.
  Depends on: Step 3

### Task 2: Atomic, stale-aware portable settings publication

**Target:** Existing source workspace.
**Files:** `scripts/lib/reconcile.py`, a small bundled config-I/O helper if needed,
renderer/check input closure, reconcile/defaults/status/MCP/packaging tests.
**Interfaces:** Existing callers preserve their reports and backups. Every apply binds
to its expected preimage; changed bytes/existence refuse with a fresh-plan instruction.
Same-directory temporary publication preserves mode and fsyncs before replacement.

- [ ] **Step 1:** Add failing tests for stale JSON/TOML reports, failure ordering, mode preservation and MCP bypass callers.
  Depends on: none
- [ ] **Step 2:** Reuse existing atomic-write mechanics; migrate every relevant mutation and complete bundled imports.
  Depends on: Step 1
- [ ] **Step 3:** Verify stale/unrelated edits and malformed inputs are retained; keep the existing sequential baseline/defaults regression green.
  Depends on: Step 2
- [ ] **Step 4:** Integrator commits the owned diff; independent reviewer checks spec and quality.
  Depends on: Step 3

### Task 3: Versioned conservative skill presentation

**Target:** Existing source workspace.
**Files:** Fanout skills/admission, collaboration prompt and recovery, compiler/bundle
evidence only where required; skill, collaboration and cold-recovery tests.
**Interfaces:** Complete delivery stays default. Opt-in policy contains version and
reviewed exact admitted tree/file sets, independently identified from full admission.
Required instruction bytes remain exact and the complete staged tree remains verified.

- [ ] **Step 1:** Add failing tests for opt-in presentation, changed omitted files, backtick references and cold recovery policy drift.
  Depends on: none
- [ ] **Step 2:** Implement the smallest optional projection over verified full admission; unknown tree falls back to full delivery.
  Depends on: Step 1
- [ ] **Step 3:** Verify full default behavior, native authorized paths and source/projection/read evidence distinctions.
  Depends on: Step 2
- [ ] **Step 4:** Integrator commits the owned diff; independent reviewer checks spec and quality.
  Depends on: Step 3

### Task 4: Honest usage and existing knowledge regression closure

**Target:** Existing source workspace.
**Files:** Existing provider usage parsing, terminal/evaluation totals and context-efficiency
corpus/pilot tests; reuse current memory failure and source-reference fixtures.
**Interfaces:** Preserve compatibility of existing scalar usage fields. Add explicit
counter semantics/status without inventing zero for missing data or double-counting
resumed totals/cache buckets. Reuse current baseline-retention behavior.

- [ ] **Step 1:** Add missing behavioral tests for usage normalization and chronology/required-read failures; retain tests already proving the behavior.
  Depends on: Task 1/Step 4
- [ ] **Step 2:** Implement only demonstrated gaps in the shared owner, completing parser/terminal/report callers.
  Depends on: Step 1
- [ ] **Step 3:** Run existing context corpus/pilot, provider and memory suites; preserve citations, last-good state and unknown counters.
  Depends on: Step 2
- [ ] **Step 4:** Integrator commits the owned diff; independent reviewer checks spec and quality.
  Depends on: Step 3

### Task 5: Disabled-by-default child model-choice extension

**Target:** Existing source workspace.
**Files:** A focused fanout child-choice module, tests and generic documentation;
reuse profile descriptors rather than modify outer executor identities.
**Interfaces:** Backend-independent fixed-choice request/result over eligible route IDs.
OpenAI Decisions uses its documented choice endpoint; transport/credentials are injected,
never discovered by public code. Jev can use the same interface. Local family/capability
and budget policy precedes backend selection. Fallback cannot widen the eligible set.

- [ ] **Step 1:** Add failing tests for Claude-only/all-admitted filtering, malformed/refused/low-confidence choices, fixed fallback and unchanged outer roles.
  Depends on: Task 1/Step 4
- [ ] **Step 2:** Implement the minimal chooser and adapter contract with typed evidence; do not silently enable native descendant launches.
  Depends on: Step 1
- [ ] **Step 3:** Record actual native delegation/tracing/budget/cancellation capability results using an explicitly allocated synthetic characterization, or mark unsupported boundaries accurately.
  Depends on: Step 2
- [ ] **Step 4:** Commit and review the extension; keep the nested-fanout guard and parent-only audit claims unchanged unless a separately proved descendant contract replaces them.
  Depends on: Step 3

### Task 6: Integrated verification, paired evaluation and native adoption

**Target:** Source workspace, followed by the existing private setup owner for machine-specific adoption.
**Files:** Active documentation/skill metadata, source-owner manifests, generated plugin
copies and exact private adoption pins; preserve unrelated private edits.
**Interfaces:** Fresh same-source tests, review and receipt evidence; no historical
receipt restamping. Current native read tools stay enabled in real capability tests.

- [ ] **Step 1:** Run renderer checks, full deterministic suites and executable/native-read regressions; fix all demonstrated affected failures and obtain broad independent review.
  Depends on: Task 1/Step 4, Task 2/Step 4, Task 3/Step 4, Task 4/Step 4, Task 5/Step 4
- [ ] **Step 2:** Under a fresh finite allocation, run a paired baseline/candidate panel with at most sixteen outer launches including owner and counterbalanced Maka review, zero automatic retries; retain every attempt and account for all usage.
  Depends on: Step 1
- [ ] **Step 3:** Retain full delivery unless representative measured input tokens improve by at least 20%, correctness/capabilities/citations do not regress and median latency is no more than 10% worse. A preliminary pilot alone cannot globally enable the projection.
  Depends on: Step 2
- [ ] **Step 4:** Apply exact reviewed source-owner skill/plugin and private setup plans, then run doctor/audit/record/verify checks. Report source, installed and live states separately and retain private evidence. Do not push, post or deploy without the required authority.
  Depends on: Step 3
