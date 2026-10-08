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
Near-duplicate/title-based merging is explicitly disabled. Native exact-write
idempotency remains unchanged; exact fetches retain every requested row's ID,
author, and complete checkpoint text.

Managed Claude/Codex/agy hooks sanitize prompts, nested tool input/response
strings, agy tool arguments/errors, and transcript-backed turns before dispatch
to the worker. They remove `private`, `claude-mem-context`, `system_instruction`,
`system-instruction`, `persisted-output`, and `system-reminder` blocks, including
case variations, nested blocks and an unclosed block's remaining text. Ordinary
text and event/session metadata are retained; unsafe identity metadata is rejected
instead of reassociating the capture. JSON-encoded tool strings are decoded before
redaction and malformed JSON-shaped strings are rejected. Wholly private captures
are skipped before route resolution, worker startup, logging, storage or provider use; empty
sanitized turns cannot fall back to media text or an earlier transcript turn.
The latest matching role owns capture even if its content is empty or media-only.

Hook JSON is limited to 1 MiB, with 32 levels of nesting and 100,000 nodes across
the capture. Transcript input and its sanitized snapshot are limited to 16 MiB,
10,000 lines, and 1 MiB per JSONL record. Duplicate fields, malformed JSON,
non-finite values, unsafe transcript paths, changing source files and oversized
input fail closed with a content-free diagnostic. No input is truncated to meet
these limits. Codex skips rejected memory captures with a content-free warning
and a successful hook exit so prompts, tools and Stop can continue; rejected
data never reaches the worker. Codex uses inline prompt, tool and summary fields
without opening or forwarding its full transcript. Only a summary lacking
nonempty inline assistant text uses the bounded transcript fallback.
Native Claude Code context and prompt-initialization hooks likewise use inline
metadata/prompt without opening or forwarding an unused transcript, which may
not exist at startup. Transcript alias types are still checked before removal.
Qwen-shaped events sharing the Claude adapter retain their existing transcript
boundary: a nonempty `QWEN_PROJECT_DIR` or a whole `/.qwen/` path segment in
either alias (after separator normalization) prevents this native-only removal.
Claude summary and other transcript-consuming events retain strict validation.
Transcript sources are opened through no-follow descriptors and
never modified. The child receives only a sanitized temporary snapshot in a
0700 directory with a 0600 file, removed when the hook finishes. Text blocks use
one joined-message tag state for inline and transcript content, while
retaining their original structure and safe fields. Tags spanning blocks cannot
expose the enclosed text; retained tool-block input is also sanitized.
Nested message, record and tool-use identities are preserved or rejected;
ordinary tool argument keys named `id`, `model` or `name` remain sanitizable.

Sanitized prompts and tool events are stored in local SQLite. Text used to
compress a session is sent only to the provider selected in `route.json`. Route
selection is explicit and there is no provider fallback. These guarantees apply
to the owned hook entry point; the upstream worker's direct, unmanaged HTTP
capture endpoints and existing history are outside this ingress contract.
Maka remains search-only. Existing project exclusion settings and nested/adapter
hook suppression still apply.

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
route, all three CLI hooks, and any requested start finish. It records the
reviewed package, version, artifact integrity, and source commit. Receipt v2 adds
`capture_sha256`, a named and framed binding of `memoryctl.py` and
`memory_ingress.py`, checked against the installed controller inventory.
`controller_sha256` continues to mean the `memory_exchange.py` digest. Historical
v1 receipts remain unchanged and are stale for the new capture implementation.
A matching receipt also requires the complete safe installed controller; checkout
source is not an installed fallback. Controller and token files are opened
nonblocking before regular-file validation so FIFO substitution cannot hang.
A failed apply leaves an older receipt byte-for-byte unchanged or creates no
receipt on a first install.

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
OpenAI routes request `gpt-6.1-sol` with `xhigh` effort and `store: false`. The
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

The package is pinned to version 13.34.2, source commit
`a80767e975dde7131cefaee46a90c130acec30d2`, and the SHA-512 integrity in
`provenance.json`. An upgrade must update all three together, stage into a new
version directory, back up SQLite, verify package identity and search, then
change the declared pin. Never replace the active runtime with an unverified
download and never commit runtime archives or local state.

After reviewing a new pin, `memoryctl.py upgrade` backs up SQLite, stages the
pinned package, refreshes the controller, merges hooks, completes any required
worker restart, and only then replaces the final receipt. Restore a reviewed
backup with `memoryctl.py rollback --backup /absolute/path/to/backup.db`.
