# llm-fanout Design Contract

## Goal

Replace `llm-council` and `llm-forge` with two provider-neutral skills:

- `llm-fanout-plan` converts an approved Superpowers plan into a validated,
  skill-aware execution DAG. Bounded adapters also admit direct read-only
  questions and Forge-style task bundles; write bundles require owner review.
- `llm-fanout-execute` runs that DAG through Claude, Codex, and agy, exchanges
  verified peer context through claude-mem, and leaves reconciliation to the
  invoking CLI.

The replacement must be behaviorally non-inferior to the legacy engines and
must improve collaboration, routing, recovery, or verification. Exact legacy
output and manifest parity are explicitly out of scope.

## Global Constraints

- Runtime code supports Python 3.11+ with the standard library only. The
  repository's pinned Python 3.12 is the development runtime.
- After integrating the current `origin/main`, snapshot its Council/Forge source,
  skills, fixtures, installed CLI help, and earned receipts as the comparison
  baseline. Their implementation and skill bytes stay unchanged for the rest
  of the core plan; baseline locks may be regenerated only from measured
  current inputs and freshly earned receipts.
- Default executors are exactly `claude`, `codex`, and `agy`. Executor IDs are
  registry keys, not an enum, so later adapters require no schema change.
- Maka is a mandatory routing and evaluation host. It is not an executor and
  must be rejected at schema, registry, and execution-preflight boundaries.
- Defaults are two rounds, hard maximum three, at least two successful seats,
  at most two concurrent tasks, and at most six executor processes.
- Versioned executor profiles pin model, effort, CLI version, invocation flags,
  and effective timeout by provider, execution class, and quality tier.
  Standard defaults are `claude-opus-5-5` at `max` for read-only and `xhigh`
  for repo-write; `gpt-6-sol` at `xhigh` for both; and
  `Gemini 3.8 Flash (High)` for both. A requested read-only `deep` tier uses
  Claude `ultracode`, Codex `ultra`, and agy High at an 1800-second default,
  subject to live CLI characterization. Standard
  read-only default/ceiling is 900 seconds; repo-write is 3600 seconds. A
  task-level timeout above its class/tier ceiling requires a reviewed override
  reason. Initial and resumed turns use the same measured profile or block.
- Provider prompts and context travel over stdin. Prompt, result, token, and
  checkpoint text never appears in argv.
- Every task or subtask explicitly declares executor skills or a reviewed
  `none_reason`. Parent group skills flow to descendants; orchestrator-control
  skills never do.
- Executor processes receive neither the claude-mem gateway token nor a memory
  search helper. The orchestrator alone publishes and exact-fetches memory.
- Multi-round production execution requires healthy claude-mem. Failure enters
  a durable `blocked-memory` phase; there is no silent empty-context fallback.
  Start/resume checks the pinned installation receipt, authenticated worker
  health, and exact transport compatibility before a first-round provider turn.
- Every seat, including read-only seats, uses an isolated run-owned workspace
  with the caller's relevant dirty baseline. Provider-native read-only guards
  and the separate workspace jointly protect the caller and blind peers.
  A repo-write seat may invoke a shell: same-UID directory modes and a separate
  Git workspace do not prevent that ordinary tool from reaching owner secrets,
  peer workspaces, held-out tests, or the caller through absolute paths.
  Certifying repo-write therefore requires a measured native per-seat
  read/write boundary and credential-blind model access that work on the
  initial and exact-resume turns. This contract does not defend against a
  compromised operating system or an installed CLI deliberately escaping the
  measured native boundary. The caller's files, index, refs, configuration,
  and worktree registrations remain unchanged until handover.
- External or destructive actions are `orchestrator-action` barriers and are
  never sent to provider seats.
- Deterministic tests never call live models, networks, claude-mem, credential
  stores, or machine skill roots. Live smokes and pressure campaigns are
  explicit opt-in commands.
- Historical plans, logs, and archived adoption material retain legacy names.

## Execution Model

1. The invoking CLI authors one draft mapping from an approved Superpowers
   plan. A direct-question adapter may create a single read-only work item from
   the invoking question; a Forge-bundle adapter preserves checks, owned paths,
   and approval barriers. The deterministic compiler validates every path;
   planning is not a council.
2. The scheduler starts dependency-ready leaf tasks within task and seat caps.
3. Claude, Codex, and agy receive the same full plan packet, task, reconciled
   dependencies, and skill bundle. Round one is blind.
4. The orchestrator waits until every expected seat is terminal. If the
   minimum success count is met, it publishes each valid checkpoint to a
   distinct run/task/seat claude-mem project, exact-fetches every returned ID,
   and verifies identity and digest.
5. Each surviving seat resumes its exact session with a peer packet containing
   every other valid checkpoint once, never its own. Peer text is delimited as
   untrusted evidence.
6. The invoking CLI selects or synthesizes a result. Read-only synthesis binds
   at least two final-barrier answer digests, the synthesizer identity, the new
   answer artifact, and any declared checks; it is not disguised as a seat
   answer. Repository synthesis binds source candidate contributions and runs
   fresh verification of the delivered result. Forge-style work requires that
   synthesis rather than selecting a single seat. Dependants unlock only after
   the reconciled artifact passes its applicable verification.
7. Executors may propose plan amendments. Only the orchestrator may accept an
   amendment, and only for unscheduled work. The affected subtree is revalidated
   and re-preflighted under a new immutable plan revision.
8. An accepted amendment changes scheduler, execution registry, and provider
   profile authority as one recoverable transition. After a crash at any
   intermediate write, startup resolves the old/new plan and profile digests
   from durable evidence before any provider turn can spend.

## Public Data Contracts

### FanoutPlanV1

`FanoutPlanV1` contains `schema_version`, the source path/hash/parser version,
defaults, and `tasks[]`.

Each task has:

- `id`, `kind` (`group` or `work`), optional `parent_id`, title, and objective.
- `source_step_ids`. Atomic source steps are stable Task/Step identifiers plus
  content hashes. Executable tasks partition all atomic source steps exactly
  once; the compiler never splits inside one step.
- Explicit `depends_on`; only `work` tasks are scheduled or dependency targets.
- `execution_class`: `read-only`, `repo-write`, or `orchestrator-action`.
- Executor `required_skills` or a non-empty reviewed `none_reason`.
- Normalized `owned_paths`, acceptance statements, and `checks[]`.
- Provider policy: executor IDs, rounds, quality tier, class-derived default
  timeout or explicit reviewed override, retries, and minimum success.

Each check contains argv, cwd, an environment allowlist, timeout, accepted exit
codes, and expected artifacts. Shell-form commands are invalid. Unordered tasks
with overlapping owned paths are invalid.

### Provider contracts

Define `ProviderCapabilities`, `ExecutorProfile`, `ProviderRequest`,
`ProviderResult`, `ProviderAdapter`, `ProviderRegistry`, and a separate
`RoutingTargetRegistry`.

`ProviderResult` includes provider/executor identity, validity, reason/hint,
attempt count, duration, usage, exact `session_id`, and digest-bound answer,
stdout, and stderr artifacts.

Profiles are addressed by `(executor_id, execution_class, quality_tier)`;
`RunInputs` commits every admitted profile digest, including both classes for
one executor. A dispatch intent binds the selected digest and effective
timeout. Profiles record requested model/effort, normalized CLI argv, installed
CLI version, observed model where structured output exposes one, and a durable
digest. Missing observed model is explicitly unverified, not inferred from the
request. A model-specific availability failure may select only a predeclared
alternate before any seat in that round spends; otherwise the run blocks for
owner action. The registry remains provider-neutral.

Session rules:

- Claude: assign a UUID using `--session-id`; resume that UUID.
- Codex: parse `thread.started.thread_id`; resume the explicit ID. Resume uses
  `-c sandbox_mode="read-only"` or `"workspace-write"` because the resume
  command has no `--sandbox` flag.
- agy: parse `conversation_id`; resume using `--conversation`, preserving plan
  or isolated write mode.
- Never use `--continue`, `--last`, or cwd-based continuation.

All three adapters use stdin prompt transport. Opt-in capability
characterization records installed CLI version and help hashes alongside
fixture streams; production preflight blocks when an exact session,
structured output, sandbox, or cleanup contract is unavailable. Live help
hashes do not make the deterministic source/receipt baseline gate stale on
every CLI auto-update.

### Skill contracts

Define `SkillRoot`, `ResolvedSkill`, `SkillAdmission`, `SkillLoadEvidence`, and
`SkillResolver`.

The engine proves delivery and byte hashes. Native load events prove native
admission where a target exposes them. Model acknowledgements are telemetry,
not proof. Identical shadow copies use deterministic precedence and are
recorded; divergent copies, escaping symlinks, missing references, oversized
trees, or changed hashes fail before a provider call.

Each seat receives complete `SKILL.md` content, a hashed one-level reference
index, and a read-only staged skill tree. Skill-specific behavioral acceptance
tests prove use.

### Artifact and run-state contracts

Artifact writes are atomic, exclusive, content-addressed, contained beneath a
run root, quota checked, and verified before consumption. Canonical JSON uses
UTF-8, sorted keys, compact separators, no NaN/infinity, and a trailing newline.

Run events distinguish dispatch intent, process start, provider terminal,
artifacts durable, publication intent, checkpoint published, checkpoint
verified, reconciliation pending/verifying, completion, action block, memory
block, and uncertain attempt.

A crash after a provider might have run but before terminal evidence creates an
`uncertain-attempt`. It is never automatically retried; the orchestrator must
explicitly accept possible duplicate spend. Durable turns are never rerun to
repair publication, verification, reconciliation, or resume state.

A run lock and an owner-only orchestrator capability prevent seat processes or
concurrent controllers from submitting reconciliation.

The default production authority is an owner-only local monotonic CAS store
outside each run root, paired with fsynced file-backed scheduler and execution
CAS stores. It protects against accidental run-root rollback, torn writes,
concurrent controllers, and crash/restart; it does not claim resistance to an
account-level adversary able to restore both the run and authority roots.
The existing pinned HTTPS authority remains an explicit high-assurance option.
Both modes have an owner bootstrap, health check, and no-spend failure state.

Amendment recovery binds intent, scheduler acceptance, execution-registry CAS,
and profile transition to the same old/new plan and profile digests. Before
intent, the replacement plan, run inputs, and profile descriptors are stored as
content-addressed artifacts. A cold-start resolver reopens every store and
authenticates those bytes; the caller need not reconstruct them. A crash before
or after each durable boundary, a CAS failure, and removed task IDs have
defined replay outcomes. An intent may be abandoned only while scheduler and
execution authority both retain the old binding. A pending intent blocks all
provider spend and scheduler/execution mutations, including reconciliation,
action completion, handover, and GC; read-only status remains available.
Ambiguous state never silently chooses either revision.

## Claude-mem Protocol

`components/memory/memory_exchange.py` is an owner-local controller installed
by `memoryctl.py`. It accepts strict JSON on stdin and returns strict JSON on
stdout. It uses authenticated `/api/memory/save` and exact
`/api/observations/batch`; semantic search and `/context/inject` are not
correctness paths.

Executor child environments set `KHENRIX_NESTED_AGENT=1` so automatic memory
hooks cannot leak prior peer context into blind round one. The orchestrator's
explicit exchange is unaffected.

Control plan chunks use a run-scoped control project. Checkpoints use a distinct
project per run/task/seat. Checkpoint key and digest are part of the title/text
surface that claude-mem hashes. Exact replay of the same key/digest is accepted;
same key with another digest, one ID mapped to another checkpoint, missing IDs,
foreign metadata, or tampered narratives fail closed.

Round-one publication begins only after every expected seat is terminal.
Oversized packets fail rather than truncate. Memory observations follow normal
claude-mem retention; fanout GC reports exact observation IDs but never performs
broad deletion without a verified exact-ID deletion API.

## Repository Lifecycle

Baseline capture includes tracked, staged, deleted, binary, mode, symlink, and
non-ignored untracked state without writing caller Git metadata. Secret,
escaping-link, per-file-size, and aggregate-size violations fail before spend.

Candidate bundles contain a manifest of full content-addressed bytes, modes,
symlink targets, and deletions relative to the immutable baseline. Each seat
keeps its own workspace across rounds. Peer candidate files are evidence only
and are never applied automatically to another seat.

Individual candidates and reconciled synthesis run immutable argv checks in
fresh verifier workspaces. Collection is non-mutating. Handover verifies the
destination baseline, stages changes transactionally, journals every step, and
restores the pre-handover backup if final verification fails.

## Skill Delivery and Maka

Both fanout skills are canonical direct-copy skills. A generic `skill_bundles`
mapping materializes `shared/lib/fanout` as `lib/fanout` inside each installed
skill without duplicating the canonical source. Composite plan IDs, backups,
receipts, rollback, collision checks, and traversal checks cover the whole tree.

Maka smoke schema v2 records ordered `expected_skills[]` and proves primary
admission, nested natural loads, source, rank, truncation, session identity, and
unexpected-skill rejection. Validate-only cases remove provider credentials and
use canary binaries so a prompt saying “dry run” cannot accidentally spend.
The private Host's profile exposes only `Skill` and `SkillSearch`. A guarded
overlay suppresses Maka's archive-only `Read` injection for that profile while
retaining compaction archives; the managed relay independently permits only
those two tools on the provider wire and attests the outbound set.

Maka also judges quality comparisons through a separate evaluation adapter that
cannot be registered as an executor. Routing smoke uses the interactive GPT-6
Sol route. Quality comparison runs both that route and the pinned Harbor-lab
GPT-5.6 Sol profile through no-tools, arbitrary-case, structured-ballot
invocations, not the existing fixed Harbor benchmark. Each ballot records
auth route, requested and observed model, and effort. The routes are separate
mandatory panels: failure of either fails certification. Repeated GPT-6 Sol
ballots never increase the number of independent cases or model families.

## Quality and Retirement Gates

Core certification requires:

- At least 60 distinct pre-registered sealed answer cases, including Council
  normal and deep strata and the final reconciled answer delivered through its
  installed consumer path. Failed or missing deliveries count as candidate
  losses. Deterministic assertion performance is no lower than Council; the
  one-sided exact binomial 95% upper bound on the rate of paired
  baseline-correct/candidate-wrong losses is below 5 percentage points. This
  conservative loss-only bound is a lower bound on net paired correctness.
  Win-or-tie is at least 80%; candidate wins are at least baseline wins; and
  there are no new factual, authorization, security, or safety regressions.
  The same cases and pass rules apply independently to both Maka panels;
  abstentions remain in the denominator.
- Claude, Codex, and Maka judge counterbalanced A/B and B/A presentations.
  Position-inconsistent ballots abstain. Each case contributes at most one
  judgment per model family: concordant Codex/Maka-family ballots collapse to
  one, discordant ones abstain, and a case is decisive only when the distinct
  family judgments agree. Abstentions remain in the denominator but not the
  win-or-tie numerator. “Superior” requires at least 20 decisive cases and a
  95% Wilson lower bound above 0.5.
- At least 12 distinct Forge comparison tasks, including review/deep-review
  defect cases and final installed Forge-style ingress. Fanout delivers a
  freshly synthesized candidate, has no fewer fresh verifier successes or
  held-out defect detections, never weakens checks, and never mutates the caller.
- A randomized sharing-disabled ablation measures task quality. Random 128-bit
  fact transfer is only a transport/resume test: 6/6 peer transfers, no blind
  leakage, and no unjustified poisoned-peer adoption.
- At least 30 operational samples support p95 claims. A standard three-seat,
  two-round run is six no-retry turns. Resume never repeats a settled turn.

Only a green non-inferiority receipt unlocks consumer migration.

Legacy retirement additionally requires zero active run directories, legacy
refs, registered worktrees, and related Git admin entries across every repository
named by legacy manifests. Historical artifacts remain untouched. Destructive
per-run cleanup keeps its normal approval boundary.
