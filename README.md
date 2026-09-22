# khenrix-utils

One source of truth for the agentic tools on this machine — **Claude Code**,
**Codex**, **Antigravity (`agy`)**, and **Maka**. It keeps their shared skills and
base instructions aligned, and also manages the portable settings, MCP servers,
status line, and shell aliases supported by the three CLIs.

> **Bringing up a new machine or syncing across two?** See
> [`docs/machine-setup.md`](docs/machine-setup.md) — full replication steps + what
> syncs via git (this repo, the Obsidian vault, project repos) vs what's re-created
> per machine (Claude settings, MCP secrets, tooling).

## How it works

```text
capabilities.toml + house-style.md
                  ├─ shared/skills/       → 2 Khenrix direct-copy + plugin skills
                  ├─ shared/superpowers/  → 15 vendored Superpowers skills
                  ├─ components/skills/skillctl.py
                  │     ├─ 17 declared direct-copy skills → native skill roots
                  │     └─ bounded house-style block → Claude, Codex, agy, Maka
                  └─ scripts/render.py → optional per-CLI plugin bundles
```

The normal skill path is a selective direct copy. It owns the 17 names under
`[skill_delivery].skills`: `khenrix-quality`, `khenrix-writing`, and the 15-skill
Superpowers bundle. It leaves every sibling skill and all instruction text outside
the Khenrix markers untouched. It does not require the `khenrix-utils` marketplace,
and the selective installer never installs or enables it. Generated plugin bundles
exclude these native-only skills, so they have one copy and update path. The bundles
carry other optional skills for the broader `khenrix-setup` reconcile flow for MCP
servers and CLI-specific settings.

## Usage

Install the pinned tools, review the content-addressed plan, then apply it:

```bash
mise trust && mise install
mise run skills:plan
mise run skills:apply -- --expect sha256:PLAN_ID
mise run skills:doctor
```

The apply copies all 17 skills to Claude, Codex/Maka, and agy, then reconciles the
bounded house-style block in all four instruction files. It writes an install
receipt under `~/.local/state/khenrix-utils/skills/`. See
[`docs/skills.md`](docs/skills.md) for the skill overview, routing rules, restore
command, and upstream update process.

The 15 Superpowers directories are a pinned snapshot of
[`obra/superpowers`](https://github.com/obra/superpowers). Their immutable commit,
selected paths, tree hash, license, and notice are recorded with the canonical
source under `shared/superpowers/using-superpowers/`, so an update starts with a reviewed
upstream diff and ends with `mise run skills:upstream-sync -- superpowers
FULL_40_CHARACTER_COMMIT` and a new reproducible pin. A single declared launcher
overlay disables the optional visual companion's remote logo and telemetry path.

The optional full plugin bundle is still available when a CLI needs the broader
`khenrix-setup` flow:

```bash
mise exec -- make setup-claude
mise exec -- make setup-codex
mise exec -- make setup-agy
```

These targets install the plugin; they do not write live CLI config. Run
`khenrix-setup` inside that CLI to review and add the declared MCP servers and
settings.

### Portable model and effort defaults

The exact cross-machine defaults live under `[settings.defaults]` in
`capabilities.toml`. Install the pinned repo tools once, then inspect or align
only those declared leaves:

```bash
mise trust && mise install
mise run defaults:status   # read-only; does not inspect MCPs, skills or plugins
mise run defaults:apply    # backs up config, then aligns only declared leaves
```

| CLI | Portable defaults |
|---|---|
| Claude | provider-neutral `best`, `effortLevel=xhigh`, `ultracode=true` |
| Codex | `gpt-5.6-sol`, execution/subagents `xhigh`, planning `ultra` |
| agy | `Gemini 3.8 Flash (High)` |

Existing auth, MCP, plugin, skill and machine-specific settings remain intact.
The doctor check `cli-model-defaults` verifies the same fields without printing
observed values.

### Portable memory and Maka

This repository also owns two public runtime components. The runtime installers
do not install or move shared skills or plugins:

- `components/memory` installs the integrity-pinned local `claude-mem` worker,
  preserves its SQLite database, and additively merges capture hooks for Claude,
  Codex, and agy. Maka can search the same loopback-only data without capturing
  a second copy. Choose `claude-subscription`, `codex-subscription`,
  `openai-keychain`, or an owner-only `local-claude` descriptor explicitly.
- `components/maka` installs the pinned Maka v44 runtime, launcher, auth policy,
  provenance, and audit lab. It supports ChatGPT subscription login on macOS and
  Linux/WSL, plus a Keychain-backed API route on macOS.

Review the dry runs before applying either component:

```bash
mise run memory:plan -- --route codex-subscription
mise run memory:install -- --route codex-subscription --start
mise run maka:plan
mise run maka:stage
mise run maka:auth-mode -- chatgpt-subscription
mise run maka:install
```

`scripts/bootstrap-machine.sh` installs the 17 native skills and verifies both runtimes,
but does not enable or install a `khenrix-utils` marketplace/plugin bundle. The explicit
`make setup-claude`, `setup-codex`, and `setup-agy` targets remain the opt-in path. A first run
must set `KHENRIX_MEMORY_ROUTE` and `KHENRIX_MAKA_AUTH_MODE`; it fails instead of
guessing. The Keychain routes additionally require the matching non-secret
account selector. See [`components/memory/README.md`](components/memory/README.md)
and [`docs/maka.md`](docs/maka.md) for route-specific setup, migration, rollback,
privacy boundaries, and verification.

Invoking the machine bootstrap authorizes its direct `skills:apply`; routine updates
remain plan-bound and should pass the reviewed `sha256:…` ID with `--expect`.

### Keeping a CLI current — `khenrix-upgrade`

Each plugin also ships a **`khenrix-upgrade`** skill. Run it inside a CLI to:

1. snapshot the current setup (`scripts/inventory.py`),
2. **deep-research** the latest version changes, models, experimental features and
   best practices for that CLI,
3. review the khenrix skills with the CLI's native tooling (Claude `skill-creator`
   / `skill-reviewer`; Codex `quick_validate.py`; agy `plugin validate`),
4. apply repo improvements (SKILL.md / `capabilities.toml` / house-style) with
   diffs + confirmation, then deliver by changed surface: use the reviewed
   selective plan/apply, doctor, and Maka smoke for the direct-copy skills,
   their provenance, selective settings, or house style; use
   `make khenrix-refresh` for optional rendered plugin content; run both paths
   when both surfaces changed, and
5. write a dated report to `docs/upgrades/<cli>-<date>.md` with recommended
   model, reasoning-effort, and experimental-flag changes; portable default
   changes belong in `capabilities.toml` and are applied with the command above.

It only improves **how** we use the CLI and models — it never changes what a skill
is meant to do. Live model/flag changes are recommended, not auto-applied.

### Optional plugin delivery differs per CLI

The direct-copy skill setup above is the same for every consumer and needs no
marketplace. If the broader plugin bundle is wanted, the three CLIs install it
differently (the reconcile engine itself is identical):

| CLI | Manifest | Install command (run by `make`) |
|-----|----------|---------------------------------|
| Claude | `.claude-plugin/marketplace.json` + `.claude-plugin/plugin.json` | `claude plugin marketplace add` → `claude plugin install …@khenrix-claude-marketplace` |
| Codex | `.agents/plugins/marketplace.json` + `.codex-plugin/plugin.json` | `codex plugin marketplace add` → `codex plugin add khenrix-utils@khenrix-codex-marketplace` |
| agy | flat `plugin.json` at plugin root | `agy plugin install <plugin-dir>` (direct local install — agy has no add-marketplace step) |

Read-only inspection without installing:

```bash
mise run skills:plan      # selective skill and house-style diff
mise run skills:status    # exits non-zero when managed content drifted
mise run defaults:status # model/effort drift only
mise run verify           # validate manifests + skills
mise exec -- make status  # full config diff for every CLI
```

## Editing the source of truth

- **MCP servers / settings / shell aliases / instruction targets:** `capabilities.toml`
- **Shared house style:** `house-style.md` (rendered into each CLI's memory file
  inside an idempotent `khenrix-managed` block)
- **Khenrix-authored direct-copy skills:** `shared/skills/khenrix-quality/` and
  `shared/skills/khenrix-writing/`
- **Vendored direct-copy skills:** the 15 Superpowers directories under
  `shared/superpowers/`
- **Selective delivery, receipts, restore, and Maka smoke:** `components/skills/`
- **Other shared skills:** `shared/skills/<name>/SKILL.md` (rendered into every plugin)
- **The `khenrix-setup` / `khenrix-upgrade` skills:** one shared body in
  `shared/skill-templates/<skill>/SKILL.md.tmpl`, with the provider-specific bits
  (paths, commands, config terms, per-CLI procedure) in the
  `[skill_facts.<skill>.<cli>]` tables in `capabilities.toml`. `render.py` fills the
  template per CLI — never edit the generated `marketplaces/.../SKILL.md`.

After editing a canonical direct-copy skill, run its evals, then apply the
reviewed content-addressed plan:

```bash
mise run skills:test
mise run skills:upstream-status
mise run skills:plan
mise run skills:apply -- --expect sha256:PLAN_ID
mise run skills:doctor
mise run skills:maka-smoke
```

For optional plugin content, run `mise exec -- make khenrix-refresh`. Claude and
Codex cache plugins by version, so plain edits are not picked up until refresh.
Then run `khenrix-setup` in the CLI if the change affects live capabilities.

## Layout

| Path | Purpose |
|------|---------|
| `capabilities.toml` | LLM-agnostic capability manifest (zero-dependency TOML) |
| `house-style.md` | Bounded shared instructions → Claude, Codex, agy, and Maka instruction files |
| `shared/skills/` | Shared Khenrix/plugin skill bodies; contains the two Khenrix direct-copy skills |
| `shared/superpowers/` | The 15 vendored Superpowers skill bodies; copied directly and excluded from plugins |
| `components/skills/` | Selective delivery, restore, provenance checks, and Maka routing smoke |
| `shared/skill-templates/` | Shared body templates for the per-CLI skills (filled from `[skill_facts.*]`) |
| `statusline/khenrix-statusline` | Shared status line renderer (Claude + agy), installed by the reconcile engine |
| `marketplaces/<cli>/` | Per-CLI marketplace + plugin (Claude/Codex have a marketplace manifest; agy installs the plugin dir directly) |
| `marketplaces/<cli>/plugins/khenrix-utils/` | Per-CLI plugin (bundles skills + a copy of the source of truth) |
| `scripts/render.py` | Renders shared assets into plugins; validates |
| `scripts/lib/reconcile.py` | The diff/merge engine the skills call |

## Managed aliases

`khenrix-setup --apply` adds an idempotent block to `~/.bash_aliases` with full-auto
launch aliases:

```bash
clauded='claude --dangerously-skip-permissions'
aggy='agy --dangerously-skip-permissions'
codexo='codex --dangerously-bypass-approvals-and-sandbox'
```

These bypass normal permission prompts, so they are intended only for trusted
workspaces or externally sandboxed environments.

## Managed status line

A single zero-dependency Python renderer (`statusline/khenrix-statusline`,
stdlib-only) drives the status line for both **Claude Code** and **agy**. It reads
the CLI's JSON status payload on stdin and prints one compact line:

```
Opus 4.8 | khenrix-utils | git main* | ctx 42.7% | $1.23 | 5h 12% | 7d 63% | acceptEdits
```

Segments are emitted only when the data is present, so the same script adapts to
each CLI's payload (cost and rate-limit segments are Claude-only). Colour follows
context/limit thresholds and respects `NO_COLOR`; any error degrades to a single
non-fatal line so the CLI never breaks.

`khenrix-setup --apply` installs the renderer to
`~/.local/share/khenrix-utils/statusline/khenrix-statusline` (a stable path, not the
version-pinned plugin cache) and points each CLI's `statusLine` setting at it. Codex
instead uses its native TUI status line, configured via `[settings.codex.tui]`. The
install obeys the same non-destructive rules — an existing renderer is only
overwritten on `--update-drift`, and a hand-set `statusLine` is left untouched.

## Why TOML, not YAML

The reconcile engine reads the source of truth with Python's stdlib `tomllib`,
so it works on any machine with no `pip install`. TOML is declarative, supports
comments, and mirrors Codex's own `config.toml`.

## Non-destructive guarantee

Every managed entry is tracked by name or exact field path. On apply the engine will only:
- **add** a declared entry that is missing, or
- **update** a declared drifted entry when `--update-drift` is explicitly used.

It will **never remove** an MCP server, setting, or instruction it did not write.
Portable defaults update only their named leaves; all sibling fields remain in
place. Files are backed up (`*.khenrix-backup`) before any change.
