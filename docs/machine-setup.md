# Machine setup & two-machine sync (Claude Code)

How to bring a **new machine** up to the same Claude Code setup, and how the two
machines stay **in sync**. The guiding idea: everything that *can* live in a git
repo does (and syncs via git); everything machine-local (settings, secrets,
tooling) is re-created per machine — `khenrix-setup` does most of that for you.

---

## What lives where

| Thing | Home | Syncs how |
|---|---|---|
| MCP servers, skills, base instructions, baseline settings | `khenrix-utils` (`capabilities.toml`, `house-style.md`, `shared/skills/`) | **git** (this repo) + `/khenrix-setup` applies it into the live CLI |
| Obsidian wiki / knowledge base | `~/git/obsidian-vault` | **git** (`git@github.com:khenrix/obsidian-vault.git`, **private**) via the obsidian-git plugin |
| Project repos (e.g. `hunter`) + their `.claude/skills/` | each project repo | **git** (each repo's own remote) |
| Claude settings (`~/.claude/settings.json`), hooks, statusline binary | machine-local | **not git** — re-created per machine (values below) |
| MCP secrets / OAuth tokens | machine-local (`~/.config/...`, env) | **not git** — re-auth per machine |
| Tooling (asdf/node, uv, jq, WSL bridges) | machine-local | **not git** — install per machine |

Rule of thumb: **git-synced = shared knowledge + config source-of-truth;
per-machine = anything with a secret, a path, or an OS-specific shim.**

---

## Prerequisites (install per machine)

- **asdf** → Node (currently `v26.2.0`) — npx/node resolve through it
- **uv / uvx** (`~/.local/bin`) — for `uvx`-launched MCPs + Python
- **jq**, and the **`claude`** CLI (`~/.local/bin`)
- **WSL only** — the Windows bridges in `~/.local/bin`: `powershell.exe` shim +
  `windows-chrome` (used by the `chrome-devtools` MCP and vercel's `BROWSER`).
  On native Linux/macOS: drop `chrome-devtools` and point `vercel`'s `BROWSER`
  at your real browser.

## 1. Clone the git-synced repos

```bash
git clone git@github.com:khenrix/khenrix-utils.git   ~/git/khenrix-utils
git clone git@github.com:khenrix/obsidian-vault.git  ~/git/obsidian-vault   # private
# + your project repos, e.g.:
git clone <hunter remote> ~/git/hunter               # brings its .claude/skills along
```

## 2. Install the Claude plugins

```bash
cd ~/git/khenrix-utils && make setup-claude          # khenrix marketplace + plugin

claude plugin marketplace add anthropics/claude-plugins-official
claude plugin marketplace add ~/git/obsidian-vault   # claude-obsidian lives in the vault repo
# then install the 12 enabled plugins:
#   khenrix-utils, claude-obsidian, and from claude-plugins-official:
#   skill-creator, superpowers, frontend-design, code-review, code-simplifier,
#   typescript-lsp, pyright-lsp, security-guidance, playwright, claude-md-management
```

## 3. Reconcile config (the big one)

Inside Claude Code, run **`/khenrix-setup`**. It diffs the live config against
`capabilities.toml` and additively applies the MCP servers, skills, base
instructions, and baseline settings. Review its table, approve.

## 4. Copy the machine-local bits khenrix-setup doesn't own

- `~/.claude/settings.json`: `model=opus[1m]`, `effortLevel=xhigh`, `tui=fullscreen`,
  `theme=dark-ansi`, `voice` (hold), `skipDangerousModePermissionPrompt`,
  `skipWorkflowUsageWarning`, the `Stop` hook, and `statusLine`.
- `~/.claude/hooks/wiki-autosave-gate.sh` (the once-per-session wiki-save nudge).
- The statusline binary → `~/.local/share/khenrix-utils/statusline/khenrix-statusline`.

## 5. Re-auth the MCP secrets (these never copy — obtain on the new machine)

- **google-drive** — drop `gcp-oauth.keys.json` in `~/.config/google-drive-mcp/`,
  run its OAuth flow → `tokens.json`.
- **vercel** — first run does a browser OAuth handshake.
- **slack** — set `SLACK_MCP_XOXC_TOKEN` + `SLACK_MCP_XOXD_TOKEN` (from your Slack session).
- **linkedin** — logs in via the tool (`uvx mcp-server-linkedin`).
- **claude.ai account MCPs** (Gmail / Calendar / Drive) — just sign into the same
  Claude account; they follow the account, not the machine.

---

## Keeping the two machines in sync (ongoing)

### Obsidian vault — automatic (obsidian-git)

The vault syncs through the **obsidian-git** plugin. Its config lives in
`.obsidian/plugins/obsidian-git/data.json`, which is **gitignored (per-machine)** —
so set it on *each* machine:

```jsonc
{
  "autoSaveInterval": 15,     // auto-commit every 15 min
  "autoPushInterval": 15,     // auto-push
  "autoPullInterval": 15,     // auto-pull the other machine's changes
  "autoPullOnBoot": true,     // pull when Obsidian opens
  "pullBeforePush": true,     // fewer conflicts
  "disablePush": false,       // ⚠ this was the blocker that kept it local-only
  "syncMethod": "merge"
}
```

Or set the equivalents in Obsidian → *Source Control* settings. Result: machine A
commits + pushes; machine B pulls on boot / interval. Since it's one person across
two machines (rarely simultaneous), conflicts are rare; obsidian-git merges, and
`pullBeforePush` keeps it clean. If Obsidian isn't open, `cd ~/git/obsidian-vault
&& git pull --no-edit && git push` does it by hand.

### khenrix-utils + project repos — plain git

```bash
cd ~/git/khenrix-utils && git pull        # then, after edits: git push
cd ~/git/<project>      && git pull        # each project on its own remote
```

After pulling khenrix-utils changes that touch skills/MCP/settings, re-run
`/khenrix-setup` (or `make khenrix-refresh`) so the live CLI picks them up.

### What does NOT sync (re-apply per machine)

Claude `settings.json`, the hook + statusline binary, all MCP secrets/tokens, and
the tooling/WSL bridges. `khenrix-setup` + section 4–5 above rebuild these; they
never travel through git (secrets and machine-specific paths must not).
