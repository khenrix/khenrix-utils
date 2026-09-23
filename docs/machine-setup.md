# Agentic tool setup and two-machine sync

How to bring a **new machine** up to the same Claude Code, Codex, agy, and Maka
setup, and how the two machines stay **in sync**. Everything that can live in a
git repository does. Machine-local settings, credentials, and tooling are
re-created from reviewed commands on each machine.

---

## What lives where

| Thing | Home | Syncs how |
|---|---|---|
| Canonical skills and base instructions | `khenrix-utils` (`capabilities.toml`, `house-style.md`, `shared/skills/`, `shared/superpowers/`) | **git** + `mise run skills:apply` copies the 17 managed skills and bounded instructions |
| MCP servers and baseline settings | `khenrix-utils` (`capabilities.toml`) | **git** + the broader reconcile flow when deliberately enabled |
| Obsidian wiki / knowledge base | `~/git/obsidian-vault` | **git** (`git@github.com:khenrix/obsidian-vault.git`, **private**) via the obsidian-git plugin |
| Project repos (e.g. `hunter`) + their `.claude/skills/` | each project repo | **git** (each repo's own remote) |
| CLI model/effort defaults + Claude baseline settings, Stop hook and statusline | declared in `khenrix-utils` (`capabilities.toml`, `hooks/`, `statusline/`) | **git** + reconcile; portable default leaves can be aligned exactly while unrelated tuning is preserved |
| MCP secrets / OAuth tokens | machine-local (`~/.config/...`, env) | **not git** — re-auth per machine |
| WSL Windows bridges (`powershell.exe`, `windows-chrome` shims) | machine-local `~/.local/bin` | **not git, but not manual either** — `scripts/bootstrap-tier0.sh` provisions them from this repo |
| Tooling (asdf/node, uv, the `claude` CLI) | machine-local | **not git** — install per machine |

Rule of thumb: **git-synced = shared knowledge + config source-of-truth;
per-machine = anything with a secret, a path, or an OS-specific shim.**

---

## 0. Get `git` and this repo

Tier 0 lives *in* this repo and is what installs `git` — so on a genuinely bare
host one manual step comes first. On Linux/WSL:

```bash
sudo apt-get update && sudo apt-get install -y git
git clone git@github.com-khenrix:khenrix/khenrix-utils.git ~/khenrix-utils
cd ~/khenrix-utils
```

On macOS, install Git through the Command Line Tools or Homebrew, then run the
same `git clone` and `cd` commands.

## 1. Prerequisites — run Tier 0

It needs no credentials and is safe on a bare Linux/WSL or macOS host:

```bash
./scripts/bootstrap-tier0.sh --dry-run   # see the plan; mutates nothing
./scripts/bootstrap-tier0.sh             # provision
```

It installs the base commands (`git curl jq unzip`) through apt on Linux/WSL or
Homebrew on macOS. Linux also gets `ca-certificates`; macOS uses its native
Keychain trust store. On WSL, Tier 0 **creates the Windows bridges** in
`~/.local/bin` (`powershell.exe` shim + `windows-chrome`) —
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

  **Install the `OpenJS.NodeJS.LTS` id specifically — the version-pinned ids are
  a trap.** This machine carried `OpenJS.NodeJS.18` (18.16.0), whose `winget
  upgrade` tops out at 18.20.8; the MCP refuses every one of them at runtime with
  `ERROR: chrome-devtools-mcp does not support Node v18.16.0`, its own guard
  rather than an npm warning. Installing the LTS id performs an in-place major
  upgrade of the same `C:\Program Files\nodejs` install and the pinned entry
  disappears from `winget list` — so there is nothing left to uninstall
  afterwards, and running one would target the directory LTS just populated.
  Tier 0 now checks the VERSION against `chrome-devtools-mcp`'s declared
  `engines.node` (`^20.19.0 || ^22.12.0 || >=23`), not merely that node exists;
  it previously passed 18.16.0 as `ok` while the bridge was dead.
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

Then, per machine (Tier 0 already covers `git curl jq unzip` plus Linux
`ca-certificates`):

- **mise** (`~/.local/bin/mise`) → Node (currently `v26.2.0`), uv and jq — runtimes
  resolve through it (replaced asdf in the 2026-07-08 migration). Python comes from `uv`.
- **uv / uvx** — for `uvx`-launched MCPs + Python
- the **`claude`** CLI (`~/.local/bin`)
- **WSL/Linux with Windows Chrome** — the `chrome-devtools` MCP runs through the
  PowerShell interop bridge and Windows-side Node with `--autoConnect`. It connects to
  the already-running normal Windows Chrome profile, so the agent sees the same tabs,
  cookies and logins instead of launching Chrome for Testing. In Chrome 144+, open
  `chrome://inspect/#remote-debugging` once and enable remote debugging for the profile.
  Tier 0 provisions the PowerShell bridge and verifies Windows-side Node; no Linux Chrome
  installation is required.

  **Migrating a machine that still has the old WSL-native entry: delete it by hand first.**
  Reconcile is additive-only, so it reports the existing server as
  `✏️ UPDATE chrome-devtools — command/args differ` and changes nothing — and
  `--update-drift` does not rescue it either, printing `drift update for MCP not
  auto-applied; edit manually`. A refresh therefore leaves the machine on Chrome for
  Testing while claiming success. Remove the `chrome-devtools` entry from `~/.claude.json`,
  `~/.codex/config.toml` and `~/.gemini/config/mcp_config.json`, then re-run
  `/khenrix-setup` — it re-ADDs the current definition. Same hazard as the vercel and
  google-drive removals noted in `capabilities.toml`.
- On native Linux/macOS, Tier 0 skips the Windows bridges and the platform gate withholds
  `chrome-devtools`; use the host's normal browser tooling when needed.

### Clipboard — do NOT install `wl-clipboard`

Image paste is one of the capabilities that silently died on the second machine,
and the fix is a thing *not* to install. Claude Code dispatches image paste down
a fallback chain:

```
wl-paste  ||  xclip  ||  powershell
```

`||` short-circuits on the **first success**, so anything named `wl-paste` or
`xclip` — a package *or* a hand-written shim — preempts the maintained
PowerShell path before it is ever reached. Two consequences:

- **Do not install `wl-clipboard`.** Under WSLg `wl-paste` succeeds, so the chain
  stops there — and WSLg hands back only a **BI_BITFIELDS** BMP, which the
  bundled decoder frequently cannot read. It fails *silently*: the paste appears
  to work and no image arrives.
- **Do not shim these names.** A `#!` script called `xclip` satisfies
  `command -v` whether or not the real package exists, which is exactly how this
  machine once certified a clipboard that was dead. `python3 scripts/doctor.py
  --only clipboard-no-shim-intercept` fails on any such shim, and
  `--only clipboard-image-roundtrip` proves the real path end to end.

The **real `xclip` package is fine** — and is the right tool — for the *text*
clipboard. The rule is about the image chain: leave `wl-clipboard` off WSL, and
never fake either name.

After Tier 0 has installed `mise`, trust the checked-in tool manifest and install
its lock-pinned Python and uv versions:

```bash
cd ~/khenrix-utils
mise trust
mise install
```

## 2. Clone the remaining git-synced repos

`khenrix-utils` is already cloned (step 0). The rest:

```bash
git clone git@github.com:khenrix/obsidian-vault.git  ~/git/obsidian-vault   # private
# + your project repos, e.g.:
git clone <hunter remote> ~/git/hunter               # brings its .claude/skills along
```

The portable memory and Maka runtimes are part of this repository. Their
credentials, OAuth sessions, local provider descriptor, session history, and
SQLite data remain local to each machine. Individual shared skills and plugins
remain outside these runtime components.

Choose both routes explicitly before the authenticated bootstrap. For a machine
using the existing CLI subscriptions:

```bash
KHENRIX_MEMORY_ROUTE=codex-subscription \
KHENRIX_MAKA_AUTH_MODE=chatgpt-subscription \
  ./scripts/bootstrap-machine.sh --dry-run
KHENRIX_MEMORY_ROUTE=codex-subscription \
KHENRIX_MAKA_AUTH_MODE=chatgpt-subscription \
  ./scripts/bootstrap-machine.sh
```

After the first install, start `maka`, enter `/setup`, choose **OpenAI OAuth
(ChatGPT / Codex)**, and complete the official device login. For memory on a
local Anthropic or Vertex route, create the owner-only descriptor documented
in `components/memory/README.md`, then set
`KHENRIX_MEMORY_ROUTE=local-claude` and `KHENRIX_MEMORY_PROVIDER_FILE` to its
absolute path. The macOS API routes use `openai-keychain` or `api-key-relay` and
also require `KHENRIX_MEMORY_KEYCHAIN_ACCOUNT` or
`KHENRIX_MAKA_KEYCHAIN_ACCOUNT`. The bootstrap refuses an absent or mismatched
route rather than selecting a billable provider automatically. Existing legacy
Maka state uses the reviewed migration in [the Maka guide](maka.md).

## 3. Install the shared skills

The canonical skills use a selective direct copy. No Khenrix marketplace or
plugin install is required. The two Khenrix-authored direct-copy skills live in
`shared/skills/`; the 15 vendored Superpowers skills live in `shared/superpowers/`.

```bash
cd ~/khenrix-utils
mise run skills:plan
mise run skills:apply -- --expect sha256:PLAN_ID
mise run skills:doctor
```

This installs `khenrix-quality`, `khenrix-writing`, and the 15-skill Superpowers
bundle into the native skill roots for Claude, Codex/Maka, and agy. It also
updates the bounded house-style block in all four instruction files. Existing
sibling skills and text outside the markers stay unchanged.

Other Claude plugins keep their normal install path. They are independent of the
17 Khenrix-managed direct-copy skills:

```bash
claude plugin marketplace add anthropics/claude-plugins-official
claude plugin marketplace add ~/git/obsidian-vault   # claude-obsidian lives in the vault repo
# then install the enabled plugins you use:
#   claude-obsidian, and from claude-plugins-official:
#   skill-creator, frontend-design, code-review, code-simplifier,
#   typescript-lsp, pyright-lsp, security-guidance, playwright, claude-md-management
```

Do not install the upstream Superpowers plugin in parallel. Khenrix Utils already
delivers its 15 skills verbatim to every CLI and records one reviewed upstream pin;
installing the plugin would create duplicate names and a second update path.
The central manifest, selected tree hash, license, and notice live under
`shared/superpowers/using-superpowers/`. Update that bundle atomically from its reviewed
`obra/superpowers` revision with `mise run skills:upstream-sync -- superpowers
FULL_40_CHARACTER_COMMIT`, then run the delivery tests and Maka routing smoke.

`scripts/bootstrap-machine.sh` is the broader authenticated bootstrap. It still
handles marketplace plugins, MCP reconciliation, runtime installation, defaults,
and a closing doctor pass. Use its dry run first when that full setup is wanted:

```bash
./scripts/bootstrap-machine.sh --dry-run
./scripts/bootstrap-machine.sh
```

Running the non-dry bootstrap explicitly authorizes its unbound `skills:apply` step;
routine maintenance remains bound to the reviewed plan ID with `--expect`. The bootstrap
does not add or install a `khenrix-utils` marketplace/plugin bundle. The direct-copy skill setup above
does not depend on the separate third-party marketplace flow.

## 4. Align portable CLI config

The narrow defaults controller aligns only the declared model and effort leaves:

```bash
mise run defaults:status
mise run defaults:status-json
mise run defaults:apply
mise exec -- python3 scripts/doctor.py --only cli-model-defaults
```

The portable defaults are Claude provider-neutral `best`, Opus 5.5 `xhigh`
under `modelSettings`, and `ultracode=false` (use `claude --effort ultracode`
for a planning session); Codex `gpt-6-sol`, execution/subagents `xhigh`,
planning `ultra`, and preferred `service_tier=default`; and agy
`Gemini 3.8 Flash (High)`. Apply creates numbered
`*.khenrix-backup` files and preserves every unrelated field.

The broader `khenrix-setup` flow remains available for declared MCP servers,
Claude baseline settings, the Stop hook, status line, and aliases. It is separate
from selective skill delivery. Install the optional Khenrix plugin and run the
skill only when those broader capabilities are wanted; review its table before
applying.

## 5. Re-auth MCP secrets — the only truly-manual step left (never copy)


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
cd ~/khenrix-utils && git pull        # then, after edits: git push
cd ~/git/<project>      && git pull        # each project on its own remote
```

After pulling changes to any canonical direct-copy skill or `house-style.md`, review and
apply the new content-addressed plan:

```bash
mise run skills:plan
mise run skills:apply -- --expect sha256:PLAN_ID
mise run skills:doctor
```

Run `mise run skills:maka-smoke` after a skill or Maka runtime update. For
changes to optional plugin content, run `mise exec -- make khenrix-refresh` and
use the broader reconcile flow in the affected CLI. Refresh updates only plugins
that are already installed; it never installs an absent plugin. Opt in with the
explicit `make setup-claude`, `setup-codex`, or `setup-agy` target.

### What does NOT sync (re-apply per machine)

Only the things that *can't* safely travel through git: **MCP secrets/tokens** (re-auth,
section 5) and the **machine-local toolchain** (asdf/node, uv, the `claude` CLI — install,
Prerequisites). The **WSL bridges are no longer in this list**: they are provisioned from
this repo by `scripts/bootstrap-tier0.sh`, so re-running Tier 0 is how a second machine
gets them, not hand-copying. The canonical skills and house style are declared in
this repository and applied by `mise run skills:apply`. Optional CLI settings, the
Stop hook, and the status line use the broader reconcile flow.
