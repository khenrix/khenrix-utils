# khenrix-utils

One source of truth for the agentic CLIs on this machine — **Claude Code**,
**Codex**, and **Antigravity (`agy`)** — so they all share the same MCP servers,
skills, base instructions, baseline settings, and managed shell aliases.

## How it works

```
capabilities.toml ──┐
house-style.md  ────┤  (LLM-agnostic source of truth)
shared/skills/  ────┘
        │  scripts/render.py
        ▼
plugins/{claude,codex,agy}/khenrix-utils/   ← self-contained, bundles a copy
        │  make setup-<cli>  (thin: marketplace add + plugin install)
        ▼
the CLI now has the `khenrix-setup` skill
        │  you run the skill inside the CLI
        ▼
reconcile: review live config → diff vs source of truth → additively apply
```

The `make` targets **only install** the plugin. They never write CLI config.
All configuration happens through the **`khenrix-setup` reconcile skill** that
runs *inside* each CLI. The skill is **non-destructive**: it reports a diff and
adds/updates only the entries khenrix owns. Anything you added outside the setup
(machine-specific MCP servers, hand-tuned settings) is left untouched.

## Usage

```bash
make setup-claude   # registers marketplace + installs plugin into Claude Code
make setup-codex    # ... into Codex
make setup-agy      # ... into Antigravity
```

Then, inside the CLI, invoke the skill (e.g. `/khenrix-setup` in Claude Code).
It prints a review table and asks before writing anything.

### Keeping a CLI current — `khenrix-upgrade`

Each plugin also ships a **`khenrix-upgrade`** skill. Run it inside a CLI to:

1. snapshot the current setup (`scripts/inventory.py`),
2. **deep-research** the latest version changes, models, experimental features and
   best practices for that CLI,
3. review the khenrix skills with the CLI's native tooling (Claude `skill-creator`
   / `skill-reviewer`; Codex `quick_validate.py`; agy `plugin validate`),
4. apply repo improvements (SKILL.md / `capabilities.toml` / house-style) with
   diffs + confirmation, then `make khenrix-refresh`, and
5. write a dated report to `docs/upgrades/<cli>-<date>.md` with recommended
   live-config tuning (model, reasoning effort, experimental flags) to apply yourself.

It only improves **how** we use the CLI and models — it never changes what a skill
is meant to do. Live model/flag changes are recommended, not auto-applied.

### Install mechanism differs per CLI

The three CLIs ship the plugin slightly differently (the reconcile engine is
identical — it's bundled into each plugin):

| CLI | Manifest | Install command (run by `make`) |
|-----|----------|---------------------------------|
| Claude | `.claude-plugin/marketplace.json` + `.claude-plugin/plugin.json` | `claude plugin marketplace add` → `claude plugin install …@khenrix-claude-marketplace` |
| Codex | `.agents/plugins/marketplace.json` + `.codex-plugin/plugin.json` | `codex plugin marketplace add` → `codex plugin add khenrix-utils@khenrix-codex-marketplace` |
| agy | flat `plugin.json` at plugin root | `agy plugin install <plugin-dir>` (direct local install — agy has no add-marketplace step) |

Read-only inspection without installing:

```bash
make status         # diff every CLI's live config against capabilities.toml
make verify         # validate manifests + skills
```

## Editing the source of truth

- **MCP servers / settings / shell aliases / instruction targets:** `capabilities.toml`
- **Shared house style:** `house-style.md` (rendered into each CLI's memory file
  inside an idempotent `khenrix-managed` block)
- **Shared skills:** `shared/skills/<name>/SKILL.md` (rendered into every plugin)
- **The reconcile skill itself:** per-CLI under
  `plugins/<cli>/khenrix-utils/skills/khenrix-setup/`

After editing, run **`make khenrix-refresh`** — it re-renders and pushes the
updated plugin/skill/engine into every installed CLI in one step (Claude and
Codex cache plugins by version, so a plain edit isn't picked up until you
refresh). Then re-run `/khenrix-setup` in a CLI to apply any new capabilities.

```bash
make khenrix-refresh   # sync repo → all installed CLIs (no config is changed)
```

## Layout

| Path | Purpose |
|------|---------|
| `capabilities.toml` | LLM-agnostic capability manifest (zero-dependency TOML) |
| `house-style.md` | Shared base instructions → CLAUDE.md / AGENTS.md / GEMINI.md |
| `shared/skills/` | Canonical skill bodies copied into every plugin |
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

## Why TOML, not YAML

The reconcile engine reads the source of truth with Python's stdlib `tomllib`,
so it works on any machine with no `pip install`. TOML is declarative, supports
comments, and mirrors Codex's own `config.toml`.

## Non-destructive guarantee

Every managed entry is tracked by name. On apply the engine will only:
- **add** a declared entry that is missing, or
- **update** an entry it previously wrote (tagged `khenrix-managed`) that drifted.

It will **never remove** an MCP server, setting, or instruction it did not write.
Files are backed up (`*.khenrix-backup`) before any change.
