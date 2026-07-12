# Cross-CLI plugin/skill/MCP parity + reproducible-environment docs — design

**Date:** 2026-07-12  **Status:** design (pre-plan)  **Repo:** khenrix-utils

## Goal

Make the machine's agentic-CLI environment (Claude Code, Codex, agy) **inventoried,
reproducible-as-a-capability-set, and — where mechanically possible — consistent across
all three CLIs.** Two outcomes:

1. **Reproducible capability set.** A new computer can be brought to the same *set of
   capabilities* via one documented, idempotent path. This reproduces the capability
   set, **not** an exact byte-for-byte state: third-party plugins install from GitHub
   marketplaces at HEAD, so exact-version reproduction is not guaranteed. D1 records the
   source + resolved version it observed as a best-effort reference; it does not enforce
   pins (see Non-goals).
2. **Feasibility-gated parity.** Every capability that *can* run on codex/agy does, via
   each CLI's own mechanism; Claude-only capabilities are documented as such, not forced.
   Where a capability exists both as a codex-native plugin and as a shared MCP, exactly
   **one** is active per CLI — never both. Precedence: if reconcile already **owns** the
   MCP (i.e. it's one of the 5 shared MCPs, e.g. `google-drive`), the reconcile-owned MCP
   wins on codex and bootstrap does NOT install the competing native plugin; native is
   preferred only for capabilities reconcile does **not** own (slack, github, superpowers).

## Scope

**In:** all independently-installed Claude Code plugins (+ their skills/MCP/hooks), the
11 Claude MCP servers, and the codex/agy equivalents. **Out (this effort):** the wiki
skills (`claude-obsidian`'s `wiki`/`save`/wiki-lint/wiki-ingest set) + `shared/lib/wikisync`
(owned by a concurrent workstream — excluded by exact path/skill identifier, see below);
re-authoring Claude-only mechanisms in codex/agy (deferred); pulling third-party plugins
into `capabilities.toml` reconcile (deliberately NOT done — see Non-goals).

## Current state (inventory snapshot, 2026-07-12)

Claude plugins (15) by component / portability:

| Plugin | Source | Components | Portability |
|---|---|---|---|
| khenrix-utils | local dir (this repo) | skills(12), hooks | already cross-CLI (reconcile) |
| superpowers | gh anthropics/claude-plugins-official | skills(14), hooks | skills need per-CLI port; hooks Claude-only |
| claude-obsidian | local dir ~/git/obsidian-vault | skills(15), commands, hooks, agents | non-wiki skills portable (port); rest Claude-only; wiki set OUT of scope |
| skill-creator | claude-plugins-official | skills(1) | skill portable (port) |
| frontend-design | claude-plugins-official | skills(1) | skill portable (port) |
| claude-md-management | claude-plugins-official | skills(1), commands | skill portable (port) |
| last30days | gh mvanhorn/last30days-skill | skills(1), hooks | skill portable (port) |
| watch | gh bradautomates/claude-video | skills(1), hooks | skill portable (port) |
| code-review | claude-plugins-official | commands | Claude-only (slash-command) |
| code-simplifier | claude-plugins-official | agents | Claude-only (subagent) |
| security-guidance | claude-plugins-official | hooks | Claude-only (PostToolUse hooks) |
| playwright | claude-plugins-official | mcp | MCP portable (not yet on codex/agy) |
| pyright-lsp / typescript-lsp | claude-plugins-official | lsp | Claude-only (each CLI has own LSP) |
| codex | gh openai/codex-plugin-cc | skills(3), commands, hooks, agents | Claude-only (it IS the Claude→Codex bridge) |

Claude MCP servers (11): claude.ai Gmail/Calendar/Drive (**OAuth connectors — Claude-only**);
context7, vercel, google-drive, chrome-devtools, linkedin (**already mirrored to codex +
agy** via khenrix reconcile); slack, codebase-memory-mcp (**Claude-only so far**);
plugin:playwright (**Claude-only so far**).

Codex: khenrix skills + 5 shared MCPs + `openai-curated` marketplace (all uninstalled;
includes slack, google-drive, github, gmail, notion, superpowers, …) + `openaiDeveloperDocs`
(HTTP MCP, **currently active** — must be reproduced by D2).
agy: khenrix skills + the same 5 shared MCPs (`~/.gemini/config/mcp_config.json`) + GEMINI.md.

## Ownership partition (who writes what)

Bootstrap and the existing `capabilities.toml` reconcile write to overlapping targets
(codex config, agy `mcp_config.json`), so the boundary is stated explicitly:

- **reconcile owns** the 5 shared MCPs (context7, vercel, google-drive, chrome-devtools,
  linkedin) on all three CLIs, plus everything else already tagged `khenrix-managed`.
  Bootstrap must **not** re-emit these — it invokes `khenrix-setup` and lets reconcile
  place them.
- **bootstrap owns** only the *parity additions* this effort introduces (playwright-MCP,
  slack, codebase-memory-mcp on codex/agy; the codex-native overlap installs; the ported
  skill bodies) and the one-time prereq/marketplace setup.
- **The XOR rule resolves in reconcile's favor for owned capabilities.** `google-drive`
  is reconcile-owned on codex, so bootstrap installs **no** codex-native google-drive
  plugin — codex uses the reconcile-placed MCP. The three native-preferred members differ
  in what they XOR against: **slack** has a real shared-MCP counterpart (native plugin XOR
  the slack MCP on codex); **superpowers** overlaps at the *skill* layer (native plugin XOR
  ported skill bodies — so its bodies go to **agy only**, never codex where they'd shadow
  the plugin); **github** is a **codex-native enhancement with no MCP or plugin counterpart
  on Claude** (Claude/agy use the `gh` CLI + the codex bridge), so it is **XOR-exempt** —
  it still gets a manifest row (per-CLI status: Claude/agy = `gh` CLI, codex = native) so
  `--check` tracks it as an intentional codex-only extra, not an untracked install.
- Bootstrap steps are idempotent against entries reconcile already placed (check-before-act
  keyed on the same server names reconcile uses). The `--check` **XOR assertion** uses a
  declared native-plugin↔shared-MCP name-equivalence map (e.g. codex `slack` plugin ≡
  `slack` MCP) and confirms both (a) no capability is active as a native plugin AND its
  paired shared MCP on the same CLI, and (b) no ported skill body duplicates a
  codex-native plugin's skills (the superpowers shadow case). XOR-exempt entries (github)
  are skipped.

## Deliverables

### D1 — `docs/environment/inventory.{toml,md}` + `scripts/env_inventory.py`
Canonical record with ONE machine-readable desired-state source, so the doc and the
checker cannot drift apart:

- **Desired state = a single committed manifest** (`docs/environment/inventory.toml`,
  stdlib `tomllib`-parseable, no hashes/version-pins — that stays descoped). Per plugin:
  name, source (marketplace + GitHub URL / local dir), resolved version observed at
  authoring, component types, per-CLI portability verdict, and a `host-os` field where a
  command shape is OS-conditional (e.g. `chrome-devtools` is a Windows PowerShell
  command — recorded as Windows-shape, not a uniform "mirrored" claim). Per MCP:
  transport (stdio/HTTP/OAuth-connector), command *shape*, secret requirement, per-CLI
  intended status. This manifest is the reproduction target; `env_inventory.py` **renders
  `inventory.md` from it** (the doc is generated, never hand-desynced from the manifest).
  Codex-native-only enhancements that have no Claude counterpart (e.g. github) are
  included with an `xor-exempt` flag, so `--check` tracks them as intentional codex-only
  extras rather than untracked installs.
- **Observed state (generated report → gitignored path):** `env_inventory.py` probes live
  state and emits a report to a **`.gitignore`d path** (never committed — it holds local
  host state). It **prefers structured/JSON output** (`--json` where the subcommand
  supports it) and falls back to parsing the CLI's on-disk config files (`~/.claude/`,
  `~/.codex/`, `~/.gemini/config/`) rather than scraping human-facing list output; when it
  must run a CLI it sets `TERM=dumb`/non-interactive env and time-bounds the call. The plan
  MUST first confirm each `<cli> plugin/mcp list [--json]` subcommand exists and is
  parseable before committing to it; any probe that can't get structured data reads the
  config file instead.
- **`--check`:** compares *observed* against the *manifest* and classifies each entry with
  a precise pass/fail split so an uncheckable required capability can't pass silently:
  - `satisfied` → pass; `not-applicable` (not intended on this CLI by design) → pass;
    `awaiting-auth` (registered but needs interactive creds) → pass.
  - `missing` (required capability absent) → **fail**; `mismatch` → **fail**;
    `probe-error` (a required CLI/binary is absent or its state couldn't be observed) →
    **fail** (a distinct non-zero code so it reads as "inconclusive", not "matched").

  It is **on-demand, not wired into `make verify`** (ad-hoc plugin experimentation must
  not turn the repo gate red).
- **No-secrets protection:** the primary guard is **default-on recursive sanitization** —
  before any report or rendered-doc serialization, every value-bearing channel is stripped,
  not just command-args: env maps, HTTP headers, cookies, URL userinfo/query components,
  and provider auth fields. Only explicitly-safe symbolic references (env-var *names*,
  declared config keys) are emitted. The repo secret scan (`checks.py`) is a
  **shape-limited backstop only** (high-confidence token shapes, not path-style refs like
  `GOOGLE_DRIVE_MCP_TOKEN_PATH`): it covers the committed `inventory.md` via `scan_secrets`,
  and a small `scan_secrets`-over-a-path shim (guarded `if path.exists()`, so a
  not-yet-generated report never crashes the gate) covers the gitignored report when
  present. Sanitization, not the backstop, is the guarantee.

`env_inventory.py` is read-only, stdlib-only, and carries a small `--self-test` over
captured, sanitized fixtures (missing binary, logged-out, malformed/JSON-vs-text output)
plus a **negative-leak test** — a sentinel secret planted in each value-bearing carrier
(env/header/cookie/URL/auth-field), asserting it appears in neither the report nor the
rendered doc — wired into `make eval-test`; kept minimal, not gold-plated.

### D2 — `scripts/bootstrap-machine.sh` (idempotent, per-CLI)
Reproduces the capability set on a fresh machine. Sections, each check-before-act, each
using the **documented non-interactive flag** for its CLI (the plan pins the exact
`--yes`/non-interactive flag per command; where a prompt cannot be bypassed, the step is
marked MANUAL with instructions rather than left to hang):
- **Prereqs:** the three CLIs themselves (install method + pinned/known-good version for
  each — this is the hardest repro step, not a footnote), asdf + the **specific** node
  version the reconcile-owned MCP commands hardcode (`capabilities.toml` pins an absolute
  asdf node path, e.g. for google-drive — install that exact version or verify reconcile's
  PATH fallback resolves) with shims loaded before any `npx`/global-install step, uv, gh,
  Playwright browser binaries + their OS packages. Records supported OS/shell/arch.
- **Claude:** `plugin marketplace add` each GitHub marketplace; `plugin install` each
  plugin; `claude mcp add` the stdio/HTTP servers referencing secrets by env/path; the
  claude.ai OAuth connectors are flagged as an interactive `/mcp` login step (not scripted).
  The two **local-dir plugins** (`claude-obsidian` ← `~/git/obsidian-vault`, `khenrix-utils`
  ← this repo) are not GitHub-marketplace installs: the plan specifies their exact local
  marketplace-add / registration commands (or marks them a MANUAL step) with a checkable
  postcondition (`plugin list` shows them enabled), since recording a path in D3 does not
  install them.
- **Codex:** install the overlapping codex-native plugins **as the codex-preferred
  implementation for capabilities reconcile does NOT own** — slack (XOR the slack MCP),
  github (a codex-native enhancement with no Claude/MCP counterpart — XOR-exempt, tracked
  in D1), superpowers@openai-curated (**not** google-drive: that's reconcile-owned, so codex
  keeps the shared MCP and no native drive plugin is installed). Reproduce the active
  `openaiDeveloperDocs` HTTP MCP. **Precedence rule:** for any capability covered by an
  installed codex-native plugin, bootstrap does **not** also add the corresponding shared
  MCP on codex — native XOR MCP, never both.
- **agy:** merge `mcp_config.json` entries via a **stdlib-Python helper** (not sed/awk/jq),
  with defined conflict semantics — atomic write, preserve unknown keys, reject malformed
  input, and on a name collision with a *different* command/transport: stop and report
  (don't silently keep the old value or clobber). The helper's merge logic is unit-tested
  in `--self-test`. Skills reconcile via khenrix-setup.
- **khenrix-utils:** clone → `make khenrix-refresh` → **`khenrix-setup` per CLI**. The plan
  states explicitly whether each `khenrix-setup` invocation is a deterministic script step
  or an agent-skill step, and gives the exact headless command + success check for each.

### D3 — `docs/environment/auth-and-secrets.md` (checklist, no values)
Every provisioning prerequisite, split into **secret** vs **non-secret configuration**,
never a value:
- **Secrets:** name, the env var / on-disk path it's referenced by, how to provision it on
  a new machine — the genuine stored secrets are the two google-drive paths
  (`GOOGLE_DRIVE_MCP_TOKEN_PATH`, `GOOGLE_DRIVE_OAUTH_CREDENTIALS`). `UV_HTTP_TIMEOUT` and
  similar are **configuration/tuning**, NOT secrets.
- **Auth state beyond token files (interactive per-machine logins, no stored value):** CLI
  logins (claude/codex/agy), `gh` auth, marketplace / private-repo access, codex connector
  authorization, the claude.ai OAuth connector logins, and the **slack** and **linkedin**
  MCP logins — both authenticate interactively per machine (`capabilities.toml` stores no
  credential for either; slack's XOXC/XOXD are session-bound and rotate, which is why
  reconcile deliberately leaves slack unmanaged). Each is an explicit interactive step and
  reports `awaiting-auth` in the parity smoke until performed.
- **Local content provenance:** `claude-obsidian`'s `~/git/obsidian-vault` and
  `codebase-memory-mcp`'s on-disk store path — recorded as required paths (clone URL or
  "user-supplied local content"), with behavior-when-absent noted.

## Portable-parity installs (the "if possible" set, performed + documented)
- MCPs to codex/agy where missing and sensible: playwright-MCP, slack, codebase-memory-mcp
  (subject to the codex native-XOR-MCP precedence rule above). Slack's smoke is **expected
  to report `awaiting-auth`** on a fresh machine — its token is session-scoped/rotating
  (see D3), not a provision-once value.
- **Skill bodies are ported, not copied.** Each source skill (superpowers non-hook skills,
  claude-obsidian non-wiki skills, skill-creator, frontend-design, claude-md-management,
  last30days, watch) is examined for: (a) frontmatter/trigger format the target CLI
  understands, (b) plugin-relative script/asset references that would break outside the
  Claude plugin root. A skill whose references resolve after a mechanical
  frontmatter-translation is ported + validated; one that depends on Claude-only assets or
  hook wiring is marked **Claude-only** instead. The plan produces a **per-skill/per-target
  matrix** — source provenance, translation applied, target-parser load acceptance, every
  relative reference resolves, discoverable — with a representative behavioral check per
  source plugin (not per skill). **superpowers is the exception:** codex gets the native
  plugin, so superpowers bodies are ported to **agy only** (porting to codex would shadow
  the native plugin).
- Codex-native overlaps installed + documented as the codex-preferred path — slack, github,
  superpowers@openai-curated (google-drive stays the reconcile MCP), honoring precedence.

## Explicitly Claude-only (documented, NOT ported)
LSP plugins (pyright/typescript), security-guidance hooks, code-review command,
code-simplifier subagent, the codex bridge plugin, claude.ai OAuth connectors, and any
skill whose plugin-relative references don't resolve on the target CLI.

## Non-goals
- Not pulling third-party plugins under `capabilities.toml` reconcile (keeps the
  non-destructive reconcile invariant clean; third-party drift stays out of our ownership).
- Not re-authoring Claude-only mechanisms in codex/agy idioms (separate future effort).
- Not touching wikisync / wiki skills (excluded by exact identifier — see Scope).
- Not building a checksum/commit-pinned lock manifest — over-engineered for a personal
  machine; source + resolved-version recording is the chosen fidelity level.

## Checks (how each deliverable is verified)
- **D1 probe accuracy:** `env_inventory.py --self-test` passes over the fixture set
  (missing-binary / logged-out / malformed output — the failure modes a two-run stability
  check can't reach); a one-time hand spot-check confirms the parser reads real output
  correctly; `--check` correctly classifies a deliberately removed-then-restored entry as
  `missing` (fail) then `satisfied` (pass), and reports a stubbed missing-binary case as
  `probe-error` (fail), not a silent pass.
- **D2 install-path + idempotency (the load-bearing check):** the skip-path is not
  sufficient — the plan verifies the **install path actually runs**. Primary method is a
  **disposable config-root**: point each CLI's config env at a temp dir and run bootstrap
  there so install commands execute without touching real config/auth (the plan first
  confirms each CLI honors a config-root override — `CLAUDE_CONFIG_DIR` / `CODEX_HOME` /
  the agy equivalent — and falls back to the host method for any CLI that doesn't). For
  components that
  can't be isolated that way, an uninstall-one → re-run → confirm-reinstall → re-run →
  confirm-zero-change cycle on the host is the fallback, with restoration details captured
  first. (A full clean-container run is explicitly out — the CLIs need interactive OAuth
  and aren't headless-provisionable.) A dry-run mode prints actions without performing them
  and never expands secret-bearing variables. Merge safety: never clobbers existing
  MCP/config; the name-collision-with-different-command case is fixture-tested.
- **D3 completeness + no-secrets:** `auth-and-secrets.md` + bootstrap contain no secret
  *values* (redaction is the guarantee; `checks.py` shape-scan is a backstop, run over the
  rendered D1 doc too). Completeness is a **both-directions** set check: every env-var/path
  reference in bootstrap + MCP definitions — including the reconcile-owned entries in
  `capabilities.toml`, where the google-drive OAuth paths (`GOOGLE_DRIVE_MCP_TOKEN_PATH`,
  `GOOGLE_DRIVE_OAUTH_CREDENTIALS`) actually live (linkedin has no stored credential —
  interactive auth only) — appears in the docs (catches undocumented refs), AND every docs
  entry maps to a real reference (catches stale entries) — with an explicit allowlist for
  intentionally manual-only items.
- **Parity smoke (function, not registration):** after installs, each newly-added MCP is
  driven through a real init + non-empty `tools/list` (and one safe, deterministic call
  where creds allow) **through its target CLI**; a credential-gated server (e.g. slack)
  reports a distinct `awaiting-auth` rather than a pass. Skills are verified by the
  per-skill/per-target matrix (load acceptance + every relative reference resolves +
  discoverable in a fresh session on the target CLI), plus one representative behavioral
  invocation per source plugin.
- **Repo gates:** `make verify` stays green (unchanged — no new hard gate added); no skill
  receipts touched (this effort adds docs + scripts, not skills). `env_inventory.py` and
  the agy-merge helper carry `--self-test` wired into `make eval-test`.

## Risks / tradeoffs
- **Third-party version drift:** unmanaged plugins can change upstream; D1's recorded
  source+version + a periodic re-probe mitigate (a `khenrix-upgrade`-adjacent concern).
  Accepted: repro is of the capability set, not exact versions.
- **User-facing CLIs as scripting APIs:** the riskiest assumption is that `plugin list` /
  `mcp list` output is a stable machine interface. Mitigated by preferring `--json`/config
  files and fixture-testing the parser; residual risk is CLI output churn between runs.
- **agy MCP config is file-based + headless CLIs may prompt:** bootstrap edits
  `mcp_config.json` via the Python merge helper; interactive/prompting steps are pinned to
  non-interactive flags or marked MANUAL, never left to hang.
- **Secret + auth provisioning stays manual:** by design (checklist), so "green on the
  source machine" ≠ "green on a fresh machine" for anything credential-dependent — the
  smoke check reports those as `awaiting-auth`, documented as an explicit human step.
- **Skill-port fidelity:** copying markdown loses plugin-relative asset/script paths and
  per-CLI trigger semantics; the port step's static reference-resolution + representative
  discovery check is what prevents a silently-broken skill (else it's marked Claude-only).
