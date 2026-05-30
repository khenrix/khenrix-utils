# khenrix-utils

One source of truth for the agentic CLIs on this machine — **Claude Code**,
**Codex**, and **Antigravity (`agy`)** — so they all share the same MCP servers,
skills, base instructions, and baseline settings.

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

- **MCP servers / settings / instruction targets:** `capabilities.toml`
- **Shared house style:** `house-style.md` (rendered into each CLI's memory file
  inside an idempotent `khenrix-managed` block)
- **Shared skills:** `shared/skills/<name>/SKILL.md` (rendered into every plugin)
- **The reconcile skill itself:** per-CLI under
  `plugins/<cli>/khenrix-utils/skills/khenrix-setup/`

After editing, run `make render` (or any `setup-*`, which renders first).

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
