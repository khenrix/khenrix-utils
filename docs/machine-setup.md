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
| Claude baseline settings + Stop hook + statusline | declared in `khenrix-utils` (`capabilities.toml`, `hooks/`, `statusline/`) | **git** + `/khenrix-setup` installs/registers them (add-when-absent, never overrides your tuning) |
| MCP secrets / OAuth tokens | machine-local (`~/.config/...`, env) | **not git** — re-auth per machine |
| Tooling (asdf/node, uv, jq, WSL bridges) | machine-local | **not git** — install per machine |

Rule of thumb: **git-synced = shared knowledge + config source-of-truth;
per-machine = anything with a secret, a path, or an OS-specific shim.**

---

## 0. Get `git` and this repo

Tier 0 lives *in* this repo and is what installs `git` — so on a genuinely bare
distro one manual step comes first, and only one:

```bash
sudo apt-get update && sudo apt-get install -y git
git clone git@github.com:khenrix/khenrix-utils.git ~/git/khenrix-utils
cd ~/git/khenrix-utils
```

## 1. Prerequisites — run Tier 0

It needs no credentials and is safe on a bare distro:

```bash
./scripts/bootstrap-tier0.sh --dry-run   # see the plan; mutates nothing
./scripts/bootstrap-tier0.sh             # provision
```

It installs the apt base (`git curl jq unzip ca-certificates`), **creates the WSL
Windows bridges** in `~/.local/bin` (`powershell.exe` shim + `windows-chrome`) —
these used to be a hand-rolled manual step, which is exactly how a second machine
ended up with a `chrome-devtools` MCP that was configured and dead — and reports
anything it cannot install itself. It is idempotent; re-running is safe.

Still manual, because WSL cannot install them:

- **Windows-side Node.js** — `winget install OpenJS.NodeJS.LTS`. **Not optional
  on WSL, and not the same thing as WSL's node.** The `chrome-devtools` MCP runs
  on the *Windows* side through the PowerShell shim and spawns `npx.cmd` there,
  so a WSL-only Node leaves it dead at spawn. Tier 0 reports it as MISSING and
  `python3 scripts/doctor.py --only windows-node` verifies it by making
  `node.exe` evaluate an expression.
- **Google Chrome on Windows** — any install location works; `windows-chrome`
  resolves it (PATH → Program Files → Program Files (x86) → LOCALAPPDATA).
  Override with `WINDOWS_CHROME_PATH` if it lives somewhere exotic.

Two separate doctor checks cover this bridge, and the distinction matters:
`--only windows-chrome` proves the *browser* exists (it reads the version
resource out of chrome.exe), while `--only windows-chrome-shim` proves the
*shim can actually launch it* by pointing `WINDOWS_CHROME_PATH` at a throwaway
recorder and asserting the URL arrives intact. The shim once spent its entire
life unable to launch anything — an AV refuses `FromBase64String` next to
`Start-Process` as a fileless-PowerShell signature — while the browser check
reported PASS throughout, because Chrome did exist. Values now cross the
boundary through `WSLENV`, never on the command line.

Then, per machine:

- **asdf** → Node (currently `v26.2.0`) — npx/node resolve through it
- **uv / uvx** (`~/.local/bin`) — for `uvx`-launched MCPs + Python
- **jq**, and the **`claude`** CLI (`~/.local/bin`)
- On native Linux/macOS: Tier 0 skips the Windows bridges; drop `chrome-devtools`
  and point `vercel`'s `BROWSER` at your real browser.

## 2. Clone the remaining git-synced repos

`khenrix-utils` is already cloned (step 0). The rest:

```bash
git clone git@github.com:khenrix/obsidian-vault.git  ~/git/obsidian-vault   # private
# + your project repos, e.g.:
git clone <hunter remote> ~/git/hunter               # brings its .claude/skills along
```

## 3. Install the Claude plugins

```bash
cd ~/git/khenrix-utils && make setup-claude          # khenrix marketplace + plugin

claude plugin marketplace add anthropics/claude-plugins-official
claude plugin marketplace add ~/git/obsidian-vault   # claude-obsidian lives in the vault repo
# then install the 12 enabled plugins:
#   khenrix-utils, claude-obsidian, and from claude-plugins-official:
#   skill-creator, superpowers, frontend-design, code-review, code-simplifier,
#   typescript-lsp, pyright-lsp, security-guidance, playwright, claude-md-management
```

## 4. Reconcile config — the big step (does almost everything now)

Inside Claude Code, run **`/khenrix-setup`**. It diffs the live config against
`capabilities.toml` and additively applies — **only when a value is absent, never
overriding your tuning** — all of:

- **MCP servers** + **skills** + **base instructions** (`~/.claude/CLAUDE.md`)
- **Baseline settings** → `~/.claude/settings.json`: `model=opus[1m]`, `effortLevel=xhigh`,
  `tui=fullscreen`, `theme=dark-ansi`, `voice` (hold), `skipDangerousModePermissionPrompt`,
  `skipWorkflowUsageWarning`
- **The Stop hook** — installs `wiki-autosave-gate.sh` → `~/.claude/hooks/` **and** registers
  the stanza (skipped if you already have a Stop hook)
- **The statusline** — installs the renderer + points `statusLine` at it

Review its table, approve. That's it — there's no longer a machine-local settings/hook to
hand-copy (all of the above ships in this repo now).

## 5. Re-auth MCP secrets — the only truly-manual step left (never copy)

- **google-drive** — drop `gcp-oauth.keys.json` in `~/.config/google-drive-mcp/`,
  run its OAuth flow → `tokens.json`.
- **vercel** — first run does a browser OAuth handshake.
- **slack** — set `SLACK_MCP_XOXC_TOKEN` + `SLACK_MCP_XOXD_TOKEN` (from your Slack session).
- **linkedin** — logs in via the tool (`uvx mcp-server-linkedin`).
- **claude.ai account MCPs** (Gmail / Calendar / Drive) — just sign into the same
  Claude account; they follow the account, not the machine.
- **1Password** — two *independent* paths that do not substitute for each other:
  - The **MCP** (`1password-mcp.exe`, launched through the PowerShell shim) works
    once the Windows desktop app is installed; nothing to re-auth. MCP tools only
    load at **CLI session start**, so enabling it mid-session needs a restart before
    the tools are callable.
  - The **`op` CLI inside WSL needs its own auth.** The desktop app's *"Integrate
    with 1Password CLI"* exposes its auth socket to **Windows** processes only, and
    the `op` installed in WSL is a **Linux** binary, so it reports `No accounts
    configured for use with 1Password CLI` *with desktop integration fully enabled*.
    That is the Windows/WSL boundary, not a broken setup — re-toggling the desktop
    setting will never fix it. Authenticate WSL's `op` directly, either with
    `op account add` (prompts for the master password; works in a Linux shell) or by
    exporting `OP_SERVICE_ACCOUNT_TOKEN`.
  - **`op run --` and `op read` are CLI features**, so a consumer running inside WSL
    cannot resolve `op://` references through the MCP. `python3 scripts/doctor.py
    --only onepassword-usable` reports which path (if any) actually works here.

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

Only the things that *can't* safely travel through git: **MCP secrets/tokens** (re-auth,
section 4) and the **tooling/WSL bridges** (install, Prerequisites). Everything else —
including Claude settings, the Stop hook, and the statusline — is now declared in this repo
and applied by `/khenrix-setup`, so it no longer needs hand-copying.
