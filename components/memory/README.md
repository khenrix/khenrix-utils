# Local cross-CLI memory

This component installs a pinned `claude-mem` worker for Claude Code, Codex, and
Antigravity (`agy`). It keeps each computer's history local and gives all three
CLIs native capture hooks. Maka can search the same memory but intentionally has
no capture hook.

The controller is public and provider-neutral. Credentials, provider selectors,
session history, logs, OAuth state, and SQLite files remain outside this repo.

## Data and privacy

The stable paths are:

- controller: `~/.local/share/agentic-memory/controller`
- SQLite and worker state: `~/.local/share/agentic-memory/data`
- route selector: `~/.config/agentic-memory/route.json`

The worker binds to `127.0.0.1`. Search and the viewer are exposed through a
second loopback endpoint with an owner-only bearer token. The upstream worker is
still a local single-user process, so other processes running as your operating
system user can reach it. Nothing syncs history to Khenrix or claude-mem cloud
services. Chroma, cloud sync, telemetry, hosted memory fallback, Telegram,
transcript watching, and semantic prompt injection are disabled.

Raw prompts and tool events are stored in local SQLite. Text used to compress a
session is sent only to the provider selected in `route.json`. Route selection
is explicit and there is no provider fallback.

## Install

Preview the complete install without changing the machine, then apply it through
mise. Choose the route during the apply:

```sh
mise exec python@3.12 bun@1.4.2 -- \
  python components/memory/memoryctl.py setup --route codex-subscription
mise exec python@3.12 bun@1.4.2 -- \
  python components/memory/memoryctl.py setup --apply --route codex-subscription --start
```

`setup` is a dry run unless `--apply` is present. Apply preserves the existing
database and unrelated hooks. The hook installer backs up a changed JSON file
once as `*.khenrix-backup`.

A successful apply publishes the owner-only final receipt at
`~/.local/state/khenrix-utils/memory/install-receipt.json` after the runtime,
route, all three CLI hooks, and any requested start finish. It records only the
reviewed package, version, artifact integrity, and source commit. A failed apply
leaves an older receipt byte-for-byte unchanged or creates no receipt on a first
install.

The installed controller carries the reviewed `mise.toml` and `mise.lock`, resolves
Bun through that pin, and records the absolute mise-pinned Python interpreter in
each hook command. Desktop-launched hooks therefore work without a shell-activated
mise environment or global `python3`/`bun` commands.

Choose exactly one local route:

```sh
# Uses Claude Code's existing subscription login.
python ~/.local/share/agentic-memory/controller/memoryctl.py route claude-subscription

# Uses Codex's existing ChatGPT login. It refuses API-key login to prevent billing
# the wrong account.
python ~/.local/share/agentic-memory/controller/memoryctl.py route codex-subscription

# macOS only: reads this exact `Codex Auth` Keychain item at request time.
python ~/.local/share/agentic-memory/controller/memoryctl.py route openai-keychain \
  --keychain-account ACCOUNT
```

The ChatGPT route never reads or copies OAuth tokens. Its authenticated relay
checks `account/read` through Codex app-server, then invokes an ephemeral,
read-only `codex exec` with rules, hooks, plugins, apps, agents, browsing, and
shell tools disabled. Adapter sessions are not persisted or captured. Both
OpenAI routes request `gpt-6-sol` with `xhigh` effort and `store: false`. The
Keychain API route explicitly requests Standard processing and rejects a
response that reports another model or processing tier. The ChatGPT subscription
route has no API project-tier guarantee; its processing tier is unverified.
Neither route falls back to an older model. The local Claude route stays selected
on machines already using it until the user changes the route explicitly.

An organization-specific Anthropic or Vertex route lives in an owner-only local
descriptor rather than this public repository:

```json
{
  "schema_version": 1,
  "model": "YOUR_MODEL",
  "auth_method": "api-key",
  "environment": {
    "CLAUDE_CODE_USE_VERTEX": "1",
    "ANTHROPIC_VERTEX_PROJECT_ID": "YOUR_PROJECT",
    "CLOUD_ML_REGION": "YOUR_REGION"
  }
}
```

Save it with mode `0600`, then select it:

```sh
python ~/.local/share/agentic-memory/controller/memoryctl.py route local-claude \
  --provider-file ~/.config/agentic-memory/provider.json
```

Application Default Credentials or a platform credential store are preferred.
The descriptor accepts a small allowlist of Anthropic and Vertex environment
keys when a provider cannot use ambient credentials.

## Daily use

```sh
python ~/.local/share/agentic-memory/controller/memoryctl.py start
python ~/.local/share/agentic-memory/controller/memoryctl.py status
python ~/.local/share/agentic-memory/controller/memoryctl.py doctor
python ~/.local/share/agentic-memory/controller/memoryctl.py viewer
python ~/.local/share/agentic-memory/controller/memoryctl.py backup
python ~/.local/share/agentic-memory/controller/memoryctl.py storage
python ~/.local/share/agentic-memory/controller/memoryctl.py exclude add /path/to/private-project
python ~/.local/share/agentic-memory/controller/memoryctl.py exclude list
```

`doctor` requires the worker, authenticated gateway, and any selected relay to
be running. `status` reports the same state without treating stopped services as
an error. Both validate the final install receipt against the reviewed pin.
Codex requires one interactive trust decision after its hook file is
installed or changes; the report distinguishes missing or untrusted hooks.

Search without opening the viewer:

```sh
python ~/.local/share/agentic-memory/controller/memory_search.py search "decision text"
python ~/.local/share/agentic-memory/controller/memory_search.py timeline --anchor 42
python ~/.local/share/agentic-memory/controller/memory_search.py observations 42 43
python ~/.local/share/agentic-memory/controller/memory_search.py tool-uses 17
```

Backups use SQLite's backup API so WAL-backed data is consistent. Retention is
report-only: the component never deletes history or backups automatically.
Project exclusions are exact local values stored in
`~/.config/agentic-memory/excluded-projects.json`; the public repository ships
an empty denylist.

## Upgrade contract

The package is pinned to version 13.25.3, source commit
`4520de9e0f8d6cdc20597520e383d8b51d93137f`, and the SHA-512 integrity in
`provenance.json`. An upgrade must update all three together, stage into a new
version directory, back up SQLite, verify package identity and search, then
change the declared pin. Never replace the active runtime with an unverified
download and never commit runtime archives or local state.

After reviewing a new pin, `memoryctl.py upgrade` backs up SQLite, stages the
pinned package, refreshes the controller, merges hooks, completes any required
worker restart, and only then replaces the final receipt. Restore a reviewed
backup with `memoryctl.py rollback --backup /absolute/path/to/backup.db`.
