# Multi-repository llm-fanout design

## Intent and scope

One fanout plan and one durable run must coordinate work across several Git
repositories. Tasks may run in parallel where their dependencies permit, share
verified peer evidence within each task, and pass reconciled artifacts between
repositories. The orchestrator owns admission, branch handover, and recovery.
This extends the [llm-fanout design contract](2026-09-22-llm-fanout-design.md);
it does not weaken its provider, skill, memory, or native-isolation gates.

A ticket is a branch association, not a globally unique target. The same
ticket may have a branch in several repositories. A distinct child ticket may
also be used when the work actually belongs to that child. The planner must
record the chosen ticket for each repository explicitly; it must not infer a
mapping from a Jira title or assume every repository uses the parent ticket.
For the fictional EXAMPLE-123 ticket, catalog, inventory, and notification
repositories are separate targets. Billing changes belong to an external
handoff and are outside this example's executor targets.

The initial multi-repository slice permits one repository-writing task and one
local branch handover per target. Read-only tasks may share a target. This
avoids applying two candidate deltas against one original baseline. More than
one target may name the same ticket when their repositories differ. Multiple
writable targets in one Git common directory are rejected initially; supporting
several ticket branches in one repository requires a later, separately tested
ref-serialization and baseline-dependency design.
The first slice also rejects two writable clones of the same normalized
origin, even when their Git directories differ: one origin is one repository
identity in the approved registry. It supports SHA-1 Git repositories only;
SHA-256 object-format support needs a separate baseline/workspace migration.

## Chosen approach

Use one versioned target registry and a global scheduler. The portable plan
records each target's stable logical ID, expected repository identity, ticket
key, and explicit branch ref. Execution binds its local root, pinned base and
branch OIDs, and baseline digest before provider spend.
Every source Task and compiled work task resolves to exactly one target. An
orchestrator-only integration barrier may consume artifacts from several
targets but never gives an executor direct access to several repositories.

Separate runs per repository remain a read-only preparation aid, not the
multi-repository feature: they cannot provide one dependency graph, shared
authority, or exact partial-run recovery. Treating several checkouts as one
directory is rejected because identical relative paths, baseline checks, and
handover would lose repository identity.

The existing `FanoutPlanV1` and saved run formats retain their one-repository
meaning. A pre-upgrade v1 run whose saved runtime/compiler hashes differ from
the installed code has an authenticated read-only historical status path, but
cannot resume or mutate until its exact original runtime is restored. New
records add target identity; old records are never silently reinterpreted:

- `fanout-draft-v2`, `FanoutPlanV2`, `compiled-fanout-plan-v2`, and
  `fanout-plan-admission-v2` bind the complete target registry and each task's
  `target_id` to the source hash and owner review. They do not silently admit
  a v1 source Task with a v2 target supplied only by the draft.
- `fanout-run-inputs-v3` binds each target ID to its canonical local root,
  Git common-directory real path and device/inode, remote identity, exact base
  and existing branch OIDs, branch ref, and baseline digest. Startup, baseline
  manifests, amendment records, and cold recovery use target-keyed versions.
- `fanout-task-packet-v2` and target-bound candidate, verification, and
  handover receipts carry the same target identity. Dependency records also
  carry their source task and target IDs, ref, and digest. V1 readers remain
  available for existing runs.

## Plan and branch contract

The approved Superpowers source plan declares a symbolic `Target` on every
Task. The draft contains the target registry; the compiler checks that the
source target, draft target, and every compiled task agree. A writer cannot
combine source steps from different targets. `Files`, owned paths, check
working directories, and overlap checks are target-qualified, so `src/foo.py`
in two repositories does not collide. A path still cannot escape its target
root. The whole approved plan is included in every seat's control context,
while repository file access remains limited to that seat's target and
explicitly staged evidence.

The branch ref is explicit in the admitted target, validated with Git's ref
rules, and must contain the exact recorded ticket key as a delimited token.
The planner may suggest the repository's existing convention, such as
`feat/EXAMPLE-123-catalog`; it does not create a branch from a guessed
title. Different repositories may use the same ticket and even the same
branch string. Within one repository, duplicate writable targets, target
roots, Git common directories, or branch refs fail admission under the
initial one-writer rule. The portable target records the expected remote;
run inputs bind the canonical Git common directory so two path aliases cannot
masquerade as independent repositories.

Repository identity is the credential-free normalized `host/namespace/name`
from `origin`; equivalent SSH and HTTPS forms compare equal, but a missing,
ambiguous, or different remote fails before spend.

Before any provider spend, execution resolves every local root, confirms its
repository identity and exact base/branch OID, captures its baseline, and
checks the native boundary and required skill closure for every target. A
repo-write target must start from a clean checkout of either the admitted
existing ticket branch or the pinned base when that branch is absent. An
uncommitted user change is never silently included in a ticket commit. Dirty
repositories remain admissible for read-only tasks under the existing baseline
capture rules. In the first slice, an existing ticket branch must be the
checkout's current symbolic `HEAD` and match its run-input OID; an absent
branch is created only at approved handover from the pinned current `HEAD`.

## Scheduling and context flow

The scheduler retains one DAG, journal, authority, artifact store, and global
task/process caps. Independent targets may run concurrently. A task's
dependency edge declares whether it needs a verified reconciled artifact or
the predecessor's completed branch handover. The latter stays locked until
the handover terminal record exists; reconciliation alone is insufficient.
Failed targets block their dependants but do not stop independent targets.

Round one remains blind. After its all-terminal barrier, claude-mem publishes
and exact-fetches valid peer checkpoints only for seats on that task. Cross-
target context arrives through bounded, immutable dependency records carrying
source task ID, source target ID, artifact ref, and digest. The receiving
packet verifies and labels these bytes as untrusted evidence. It never searches
claude-mem for ambient cross-task context, silently truncates a dependency,
or exposes another repository's workspace. A dependency exceeding the
existing packet bounds must be summarized in an explicit, reviewed artifact
or block.
Claude-mem publication and exact fetch are controller operations; a shell-
capable seat receives no memory credentials. Likewise, its native provider
route must be tokenless or brokered so the seat cannot read API credentials,
cookies, ADC, or SSH agents. An adapter without live proof stays disabled.

Each seat gets a target-specific baseline and workspace. Candidate manifests,
fresh verification, declared checks, skill evidence, and task packets bind the
target ID, repository identity, branch ref, base OID, and baseline digest.
Native file isolation must deny reads and writes to all other target roots;
the only cross-target bytes admitted to a seat are its verified dependency
artifacts. A missing native boundary or credential-blind route still blocks
repository-writing provider spend.

## Handover and recovery

Reconciliation produces a candidate, not a Git branch. For each approved
repo-write target, the orchestrator verifies the candidate in a fresh
target-specific workspace, checks the destination and branch ref against the
captured OIDs, and journals an exact handover intent before changing anything.
It applies only that target's candidate to its local ticket branch, runs its
declared checks, creates a local ticket-keyed commit, and records the new ref
OID and evidence digest. Pushes, PRs, Jira changes, deployments, and other
external actions require separate owner authorization; a plan or branch
handover does not authorize them.

Recovery resolves each target's intent and ref with compare-and-swap semantics.
An interrupted handover cannot be replayed merely because the process ended;
the owner must inspect the exact durable state. The run reports per-target
`pending`, `candidate`, `delivered`, or `blocked` state and an overall partial
result when some branches deliver and others fail. It does not promise an
all-or-nothing transaction across repositories. An amendment cannot retarget
a settled task, change a captured base, or reuse a completed handover.

## Verification and rollout gates

Add RED/GREEN tests for source/draft target spoofing, duplicate roots, origins and refs,
same-ticket/different-repository branches, identical-content repositories,
target-qualified path overlap, wrong-baseline packets, blind first rounds,
cross-target artifact provenance and bounds, reconciliation-versus-handover
dependencies, branch OID drift, interrupted handovers, partial results, and
cold reopen. Run a native two-repository, same-ticket test with both cross-
target dependency modes and verify both local branches and commits. A distinct
child ticket remains an optional mapping when it genuinely owns work. Frozen
pre-upgrade v1 status must remain readable; changed-runtime resume must fail
closed.

Only after deterministic, native-isolation, live CLI, claude-mem, Maka, and
quality gates pass may this count toward Forge/Council retirement. EXAMPLE-123
illustrates read-only planning first; catalog, inventory, and notification writes wait
for technical inputs, a certified native write boundary, and exact owner
approval. No result from several independent one-repository runs is labeled
multi-repository execution parity.
