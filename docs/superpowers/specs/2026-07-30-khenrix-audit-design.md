# khenrix-audit — cross-CLI setup conflict & redundancy audit

**Status:** design approved pending user spec review · 2026-07-30
**Process:** deep-research (104 agents, 16 verified claims) → brainstorming (7 user
decisions) → llm-council deep review (3 seats, 2 repo-verified) → convergence.

## 1. Problem

The three agentic CLIs on this machine (Claude Code, Codex, agy) carry 12 plugins,
~90 loaded skills, and 5–6 MCP servers across user and project scopes. Nothing
detects:

- **Trigger collisions** — skills with overlapping descriptions where the wrong one
  fires (measured: `claude-obsidian:save` ↔ `khenrix-wiki-add` at 0.452 trigger-surface
  cosine, rank 1 of all pairs).
- **Silent-failure configs** — hooks/permission rules/allowed-tools written against
  bare MCP server keys that plugin-namespacing (`mcp__plugin_<p>_<s>__<tool>`) means
  never match; plugin MCP servers endpoint-deduped out of existence.
- **Declared↔live drift, both directions** — e.g. google-drive MCP deliberately
  removed from `capabilities.toml` 2026-07-20 yet still live in `~/.claude.json` and
  `~/.codex/config.toml`, because reconcile is additive-only and nothing checks the
  reverse direction.
- **Cross-CLI drift** — context7 declared and live on Claude/Codex, absent on agy.
- **Context-budget overflow** — skill descriptions all load upfront into a listing
  budgeted at 1% of the context window; overflow drops descriptions
  least-invoked-first. This machine: ~7,760 always-on tokens vs ~2,000 budget (3.9×),
  so descriptions are being dropped right now.
- **Redundancy & ecosystem staleness** — duplicate/worse plugins (three
  skill-authoring paths, three CLAUDE.md paths, two browser MCP stacks), and no
  process for "a better replacement exists" / "the setup would benefit from X".

## 2. Verified constraints (deep-research; all Claude-side, cited in the run report)

1. Skill descriptions are the expensive surface (upfront, budgeted, truncatable);
   MCP tool schemas are deferred by default (tool search) — **MCP count is cheap**.
   The audit must never justify MCP removal by context cost.
2. Skill precedence: enterprise > personal > project, all overriding bundled; plugin
   skills namespaced `plugin:skill`, bare name also reachable unless a command owns it.
3. MCP precedence: local > project `.mcp.json` > user > plugin > connectors;
   whole-entry replacement, no merging; **plugin servers dedupe by endpoint** — a
   same-endpoint plugin server is silently dropped and its namespaced tool name then
   does not exist.
4. `skillOverrides` cannot disable plugin skills — per-skill surgical silencing is
   unavailable for essentially all installed skills; the unit of disablement is the
   plugin.
5. No first-party or community tool compares skills to each other; `claude plugin
   validate` passes byte-identical descriptions. `claude plugin details` gives
   per-skill always-on/on-invoke token costs; `/context`, `/doctor` price but don't
   compare.
6. **Codex/agy mechanics: zero verified claims.** Their semantics must be established
   from source (`~/.cache/khenrix-utils/cli-sources/codex`) and probes, never assumed.
7. Codex `~/.codex/.tmp/plugins` is the curated *catalog* (`startup_sync.rs`), not
   installed surface — 605 SKILL.md files there must never count as loaded.
8. Empirically validated this session: `claude -p "<probe>" --output-format
   stream-json --max-turns 1 --permission-mode plan` emits a
   `tool_use: Skill {"skill": "<plugin>:<name>"}` event and halts before executing —
   skill selection is directly observable, one cheap turn per probe.

## 3. Decisions

| # | Decision | Choice |
|---|----------|--------|
| D1 | Authority | Report + **guided apply**: per-finding confirmation; risky calls advisory |
| D2 | Modes | One run covers everything (local + ecosystem); ecosystem served from a 30-day TTL cache, `--refresh-ecosystem` to force |
| D3 | Surface | All scopes (user + `~/git/*` projects), all component types (skills, plugins, MCP, hooks, subagents, commands, instruction files), all three CLIs |
| D4 | Ledger | `docs/setup-audit/ledger.json` (git-synced, portable decisions) + gitignored machine-local ledger for machine-specific waivers; **engine is sole writer** (`ledger-*` subcommands, atomic). Revised from capabilities.toml by council evidence: stdlib has no TOML writer, and `capabilities.toml` is hashed into the setup/upgrade eval-receipt closure (`checks.py:183`) so ledger writes would stale unrelated receipts |
| D5 | Architecture | Deterministic stdlib-only engine + model judgment layer + two-tier trigger evidence (arena judge, then live probes) |
| D6 | Remediation | Escalating ladder, cheapest first, per-finding rung availability stated |
| D7 | skill-creator | Optional accelerator only (query generation, description drafts) — never a hard dependency, never bypasses the repo eval gate |
| D8 | Name | `khenrix-audit` (council 2-1 over `khenrix-setup-audit`); incumbent fix included: drop "audit" from khenrix-setup's description; new description built from uncontested phrases; primary invocation path is `/khenrix-audit` (names survive listing truncation) |

## 4. Architecture

### 4.1 Engine — `shared/skills/khenrix-audit/scripts/setup_audit.py`

Stdlib-only. Read-only **except** the `ledger-*` subcommands (atomic
`tmp`+`os.replace`, lock file, sole writer across all three CLIs). Modular internals,
thin CLI. Reuses the bundled `reconcile.py` for CLI config parsing (import, as
`inventory.py` does) — no third parser. Lives in the skill's own `scripts/` dir, NOT
`scripts/lib/`, to stay out of every other skill's receipt closure.

**Phase A — inventory.** Pure function of `--home-root` (fixtures exercise the
walkers). Walks:

- Claude: `installed_plugins.json`, plugin cache (skills/agents/commands/hooks/MCP/LSP
  per plugin), `~/.claude/{skills,agents,commands}`, `settings.json`(+`.local`)
  hooks/permissions/skillOverrides, `~/.claude.json` mcpServers (global + per-project).
- Codex: `config.toml` (mcp_servers, plugins, marketplaces), `plugins/cache`, skills
  incl. `.system`; `.tmp/plugins` recorded as catalog.
- agy: `~/.gemini/config/{plugins,mcp_config.json,config.json}`.
- Projects: `~/git/*` bounded (only known config paths per repo: `.mcp.json`,
  `.claude/*`, instruction files; no source-tree recursion, no escaping symlinks,
  explicit "N skipped" on any cap).
- Repo: canonical checkout only — resolved via `--repo-root` or checkout markers
  (`.git` + `capabilities.toml` + `shared/skills/`), **never inferred from the
  installed plugin root** (`render.py` copies everything into `marketplaces/`; editing
  a rendered copy loses the change on refresh).

Every item: `{id, cli, scope, type, name, provenance, effective_state, meta}` with
- `provenance ∈ {loaded, catalog, source, rendered-artifact}` — only `loaded` enters
  collision/overlap/budget checks. `rendered-artifact` detected structurally (path
  under a `marketplaces/` tree), preventing the audit's debut report from drowning in
  the repo's own rendered copies.
- `effective_state ∈ {enabled, disabled, shadowed, deduped, load_failed, unknown}` —
  cache presence ≠ installed ≠ enabled ≠ effective.
- **Redaction at inventory time**: env/header *names* + truncated hash for equality;
  values never stored (denylist key regex + high-entropy/prefix value heuristic);
  URL userinfo/query stripped. Endpoint-dedupe comparisons work on hashes. Model and
  web phases only ever see the sanitized inventory. Self-test feeds a fixture home
  containing a fake token and asserts it appears in no artifact; artifact writes
  secret-scan and fail closed.

**Phase B — mechanical checks.** Pure functions `inventory → findings`; audit clock
injected. Gated per-CLI by a versioned `SEMANTICS` table
(`{cli: {precedence_verified, namespacing, dedupe_rule, source_ref}}`): checks whose
semantics are unverified for a CLI emit `informational (semantics unverified)` and
can never reach the remediation ladder; unsupported surfaces report `NOT EVALUATED`,
never a clean pass.

| ID | Check |
|----|-------|
| B1 | Name collisions + shadowing per documented precedence, incl. bare-name plugin-skill reachability |
| B2 | Bare-key MCP references in hooks matchers, permission rules, allowed-tools, subagent tools where the server is plugin-namespaced (silently-never-matches) |
| B3 | Endpoint-dedupe hazards: same normalized command/URL reachable at two scopes (provider-specific canonicalization; `definite` vs `possible` tiers) |
| B4 | Declared↔live drift, both directions × 3 CLIs, **with ownership**: `drift` (declared∧¬live, or live∧managed-provenance∧¬declared) vs `unmanaged` (live∧¬managed∧¬declared → INFO once, not waivable) vs `in-sync`. Provenance consumed from `reconcile.py --status` (EXTRA/missing vocabulary) + a khenrix-setup sidecar stamp of managed installs (new: `managed.json`). Historical removals (google-drive) seeded as `desired_state: managed-absent` policy — the finding re-raises until the entry is actually gone on every machine; it can never be self-suppressing |
| B5 | Cross-CLI capability drift, driven by per-CLI applicability (platform gates, provider-specific docs MCP) — not naive set equality |
| B6 | Trigger-surface overlap nomination: TF-IDF cosine over the extracted trigger surface (quoted phrases, `Triggers:` lists, "use when" clauses) — validated this session: whole-description scoring buried the known-bad pair; trigger-surface ranked it #1. Nomination by top-K rank (K≈15), not absolute threshold; exact shared-quoted-phrase second signal; domain stopwords; intra-plugin pairs reported (not excluded — they're the fixable ones); labeled corpus (≥10 hand-labeled pairs) gives precision/recall before thresholds are trusted; nomination coverage reported |
| B7 | Context-budget analysis (Claude-only until equivalents verified): always-on totals from a token-counts input file the model produces via `claude plugin details` (keeps engine hermetic); one estimator per comparison, method recorded; `--context-window` parameterized; counts effective descriptions post-precedence; includes instruction files (CLAUDE.md/AGENTS.md/GEMINI.md) and MCP server-instruction blocks; reports "over budget by X%, N descriptions at risk" (drop *order* is unobservable — never predicts victims) |
| B8 | Hook-event collision: exact duplicate hook bodies (e.g. `wiki-autosave-gate.sh` at user level AND in the khenrix plugin) or demonstrably conflicting semantics → finding; ordinary shared-event composition → informational |
| B9 | Hygiene as inventory facts (version `unknown`, stale backups, disabled-but-cached, empty dirs) — findings only when a defect is established; cache/backup deletion is never an automatic remediation |
| B10 | Config parse/schema validity for every file touched, incl. duplicate keys in one JSON/TOML (last-wins-silently) — a malformed `settings.local.json` silently disables all settings |
| B11 | Dangling references: hook command paths absent on disk; MCP env vars unset (checked via `printenv` exit code per house style); allowed-tools naming nonexistent tools; marketplace sources unreachable |
| B12 | SKILL.md frontmatter validity (name↔dir mismatch, description limits, 500-line body) — repo's own stated constraints, currently unenforced on installed surface |
| B13 | Managed-block divergence: diff the khenrix-managed block *content* across CLAUDE.md/AGENTS.md/GEMINI.md targets, not just presence |
| B14 | `~/.claude.json` health: size growth, stale project entries for deleted dirs |
| B15 | Same skill reachable via two paths (symlink or personal+plugin) — phantom collisions, double budget counting |
| B16 | Vendored skill whose source plugin is still enabled (the ladder's own byproduct; see rung 3) |

**Findings.** Stable IDs: opaque hash of canonical JSON
`{rule, rule_version, cli, scope, kind, sorted subject identities}` + human display
slug + `evidence_fingerprint` (hash of normalized evidence). Severity =
`consequence × confidence × reach`, with
`consequence ∈ {silent-capability-loss, wrong-tool-fires, state-divergence, cost,
hygiene}` — budget overflow ranks as silent-capability-loss (it causes description
drops), not "cost". Every finding carries a justification class
(`correctness | drift | cost`); `cost`-justified MCP findings are forbidden by
construction (constraint 1). Output: `findings.json` (schema-versioned) with a
`capabilities` block (`can_probe`, `can_token_count`, `semantics_verified_for`,
`writable_ledger`) that the SKILL.md branches on — capability detection, not CLI
identity.

### 4.2 Ledger

- `docs/setup-audit/ledger.json` — portable, git-synced. Machine-specific waivers →
  `~/.local/state/khenrix/ledger.local.json` (gitignored); the report always prints
  "N local waivers active".
- Entry: `{id, evidence_fingerprint, desired_state: managed-present|managed-absent|
  unmanaged, disposition: open|waived|wontfix|resolved, apply_status:
  pending|applied|failed, reason, until?, created, by_cli, machine}`.
- Waivers are exact-evidence: `id` match + `fingerprint` mismatch → re-raise as
  "waiver stale — situation changed". `until` expiry (RFC 3339 UTC) re-raises at
  original severity, annotated. `wontfix` needs no expiry (stops waiver-renewal
  churn). Expired/resolved entries are never auto-deleted — they are the audit trail.
  Waived findings appear in a collapsed report section, never vanish.

### 4.3 SKILL.md judgment layer

- **Phase C — adjudication.** Model classifies B6 nominations DISTINCT / AMBIGUOUS /
  DUPLICATE from the engine-extracted trigger surfaces + frontmatter only; full-body
  reads capped at 3 pairs/run. Runs in a no-tools judgment context over sanitized
  input (prompt-injection boundary: third-party skill text never meets a
  write-capable context).
- **Phase D — trigger evidence, two tiers.**
  - Tier 1 (default, all CLIs): extend the repo's `scripts/eval_trigger.py` with
    `--arena skillA,skillB[,…]` — judge sees N name+description pairs, picks a
    winner per probe prompt; confusion matrix out. Cheap, provider-portable via the
    existing fanout engine, and doubles as the eval gate for description edits.
  - Tier 2 (Claude-only, top-N AMBIGUOUS pairs): live headless observation
    (validated mechanism, constraint 8) in an **isolated temp config dir** with only
    the pair + fixed decoys registered (selection depends on invocation history —
    probes must not perturb the live config, and the live config must not perturb
    probes); k ≥ 3 samples per prompt; fire-*rates*, `INCONCLUSIVE` when unstable;
    harness validated against known-positive/negative/collision fixtures before its
    verdicts can settle findings; advisory until then.
- **Phase E — ecosystem discovery.** Engine emits a bounded subject manifest; the
  model executes research (engine has no network). Registry-first evidence (GitHub
  releases API, npm, marketplace manifests, deprecation notices) before web search;
  per-subject 30-day TTL cache in `docs/setup-audit/ecosystem-cache.json`;
  `--refresh-ecosystem` forces. Findings require citations for: official-vs-community
  status, last release, deprecation/supersession, tool-surface overlap. No citation →
  no finding. All ADVISORY, never auto-applied. Web failure → `DISCOVERY INCOMPLETE`,
  never "no replacements". Candidate *additions* come only from declared/user-stated
  gaps — no session-history mining.
- **Phase F — report.** Deterministically rendered skeleton from `findings.json`
  (model prose only as annotations that cannot remove or mutate mechanical findings):
  committed `docs/setup-audit/latest.md` (redacted; git diff of it IS the trend) +
  gitignored `docs/setup-audit/runs/<machine>/<utc-timestamp>-<inventory-hash>.{md,json}`,
  pruned to last 10 (plus any referenced by an unresolved decision) with
  confirmation, never silently. Every skipped phase gets an explicit line
  ("probes: skipped — no claude on PATH"; "ecosystem: cache, 12d old"). Overall
  status ∈ {complete, complete-with-findings, incomplete, fatal}; `--check` exits
  non-zero above a severity threshold for `make verify`/CI use.
- **Phase G — guided apply.** Per finding: `apply / not now / waive / accept current
  state / reject recommendation / detail` (skip ≠ durable waiver; reasons required
  only for durable decisions). Safety: every action binds to the inventory hash it
  was computed from (stale → refuse); typed action plan (target, affected components,
  prerequisites, rollback command) from a deterministic allowlist — model/web text
  never becomes a shell command; destructive ops need TTY + explicit confirmation +
  restore bundle first + ledger record after; whole-plugin disable requires the
  dependency listing of everything else that plugin provides; after any live
  mutation, re-inventory and mark superseded queue items `stale — re-run`.

### 4.4 Remediation ladder (per collision finding, cheapest available rung recommended)

1. **Narrow OUR description** (khenrix-utils, description-only): audit prepares the
   diff, arena-scoped receipt required — the edited skill **plus every skill it was
   nominated against**, as one set (a per-skill receipt cannot detect handing
   prompts to a third skill). Body-level changes hand off to `skill-tuneup`.
2. **Disable the offending plugin** (`claude plugin disable`) — with the full list of
   what else is lost.
3. **Vendor the one wanted skill** into khenrix-utils — records
   `[vendored] source/upstream_ref/vendored_at`, B16 then watches for
   source-still-enabled, staleness tracking explicitly owned by `skill-tuneup`;
   license/provenance review required; never automatic.
4. Beyond that: advisory only.

### 4.5 Boundaries

- **khenrix-setup** applies declared, additive state. **khenrix-audit is the repo's
  only remover**, per-item confirmed, restore-bundled, ledger-recorded. Accepted
  *additions* flow: audit → capabilities.toml edit (model, hand-authored style) →
  setup applies. Setup gains one small feature: stamping what it installs
  (`managed.json` sidecar) — the provenance B4 needs.
- **khenrix-upgrade** improves *how* CLIs/models are used; audit examines *what* is
  installed.
- **skill-tuneup** owns deep single-skill work; audit hands off anything beyond a
  description tweak.
- **doctor.py** verifies behavioral capability (round-trips); audit analyzes
  configuration surface. No overlap; audit may recommend running doctor.

### 4.6 Rendering & degradation

`per_cli = true` with `[skill_facts.khenrix-audit.{claude,codex,agy}]` (description,
paths, available phases) — a byte-identical shared body would ship Claude-only probe
instructions to Codex/agy. At runtime, phases branch on the engine's `capabilities`
block, not CLI name (this machine runs Codex *and* has claude on PATH). SKILL.md is a
thin dispatcher (<200 lines) with progressive disclosure into
`references/{checks,remediation-ladder,probe-protocol,ecosystem-evidence}.md`;
always-on cost ≈ the description alone.

### 4.7 Description strategy (self-application)

The listing is 3.9× over budget — a new, rarely-invoked skill is a prime truncation
victim. Therefore: short description built from uncontested phrases ("which skill
fires", "duplicate or overlapping skills", "declared-vs-live drift", "shadowed MCP
server", "conflicting hooks across Claude/Codex/agy"), two negative clauses
(not one skill's content → skill-tuneup; not the wiki → wiki-lint), primary path
`/khenrix-audit`. Companion fix in the same change: khenrix-setup's description drops
"audit" (its `[skill_facts]` entries at capabilities.toml). B6 self-check includes
khenrix-audit itself.

## 5. Testing & gating

- Engine `--self-test`: hermetic fixture homes (`--home-root`), golden-file inventory
  JSON, `schema_version` on inventory + findings. Known-truth regression fixtures:
  google-drive removal case, duplicated wiki-autosave-gate hook, save↔wiki-add pair,
  and a clean fixture asserting zero findings (false-positive baseline). Hostile
  fixtures: malformed/duplicate-key TOML/JSON, unreadable files, symlink escapes,
  multiple cached versions, fake secrets (assert absent from all artifacts),
  prompt-like skill text, waiver expiry + fingerprint invalidation, partial provider
  failure, catalog exclusion, khenrix-utils-shaped repo tree yielding zero
  rendered-copy duplicates. Wired into `make verify` (like doctor-test).
- `eval_trigger.py --arena`: unit tests + a labeled-pair corpus with reported
  precision/recall.
- Skill eval: `make eval SKILL=khenrix-audit` receipt across all three providers
  (hard gate), plus the khenrix-setup description change re-receipts khenrix-setup.
- Receipt-closure regression test: a ledger/report write leaves all receipts fresh.

## 6. Explicitly out of scope

- Changing reconcile's additive-only contract.
- Auto-applying anything without per-item confirmation.
- Auditing remote machines (synced policy ≠ proof another machine complied; findings
  re-raise per machine until observed absent).
- Wiki content, code review, security audit (separate skills own those).
