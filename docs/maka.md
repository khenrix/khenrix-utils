# Maka

Maka is a local-first agent workspace with a terminal UI, a one-shot CLI,
Graph Mode, session import, and an evaluation runner. Khenrix Utils owns the
portable Maka runtime and policy used on managed machines: the exact package
pin, launcher, authentication helpers, model and permission defaults,
provenance, tests, and audit lab live in `components/maka`.

The current pin is `maka-agent@0.2.0-dev.44.20260920`. Agentic Setup remains the
broader inventory and sync owner for the other shared skills and plugins.
Khenrix Utils owns and direct-delivers `khenrix-quality` and `khenrix-writing`
plus the 15-skill Superpowers bundle to Claude, Codex, agy, and Maka. Installing
the Maka runtime itself does not copy skills; Maka discovers all 17 from the shared
native skill root after `mise run skills:apply`.

## Supported setups

| Platform | ChatGPT subscription | Keychain API relay | Run full audit lab |
|---|---:|---:|---:|
| macOS arm64 | Yes | Yes | Yes, with Colima |
| Linux x86-64, including WSL | Yes | No | No |

Both authentication routes use `gpt-5.6-sol`, `ask` permissions, and `xhigh`
for normal interactive and headless work. The API route also supports an
explicit `max` setting for planning. The ChatGPT subscription route in this
pinned release does not expose `max` or `ultra`, so `xhigh` is its highest
honest planning setting.

Maka keeps its native workspace data under
`~/Library/Application Support/Maka` on macOS and `~/.config/Maka` on Linux.
The Khenrix component is installed separately at
`~/.local/share/khenrix-utils/maka`, and its launcher is
`~/.local/bin/maka`.

## Install on a new machine

Install `mise`, clone Khenrix Utils, and review the dry-run plan:

```sh
cd ~/khenrix-utils
mise -C components/maka run maka:install-plan
```

The plan shows the platform, exact package version, destination, wrapper, and
portable-file count. It does not change live state. Apply it after review:

```sh
mise -C components/maka run maka:install-apply
```

The installer uses `install-files.txt`, installs the locked dependencies for
the current platform, copies the portable component to its stable path, and
renders the launcher atomically. It does not copy credentials or change Maka's
native profile.

Choose exactly one authentication route. For a ChatGPT subscription:

```sh
mise -C "$HOME/.local/share/khenrix-utils/maka" run maka:auth-mode -- chatgpt-subscription
maka
```

Inside Maka, enter `/setup`, choose **OpenAI OAuth (ChatGPT / Codex)**, and
finish the device login. Maka stores the OAuth session in its owner-only native
credential vault. Khenrix Utils never copies it. Authenticate separately on
each computer; do not copy Codex's `auth.json` into Maka.

For an API key on macOS arm64, select the route and install the Keychain relay:

```sh
mise -C "$HOME/.local/share/khenrix-utils/maka" run maka:auth-mode -- api-key-relay
mise -C "$HOME/.local/share/khenrix-utils/maka" run maka:harden-python
mise -C "$HOME/.local/share/khenrix-utils/maka" run maka:relay-install -- install \
  --keychain-account '<Account field from the Codex Auth item>'
```

Use the non-secret **Account** field from the existing Keychain item whose
service is exactly `Codex Auth`. The relay keeps the real key in Keychain and
stores only local decoys in Maka. Its user LaunchAgent is
`dev.khenrix.maka-openai-relay`.

## Migrate an existing managed Maka setup

An existing Agentic Setup installation has legacy selector and relay paths. The
new installer refuses to activate its launcher until that state has been
reviewed and migrated. Stage the Khenrix component without changing the
launcher, then inspect and apply the safe migration:

```sh
cd ~/khenrix-utils
mise -C components/maka run maka:stage
mise -C "$HOME/.local/share/khenrix-utils/maka" run maka:migrate-plan
mise -C "$HOME/.local/share/khenrix-utils/maka" run maka:migrate-apply
```

The migration always copies the auth-mode selector. For the API route it also
copies the non-secret Keychain account selector, readiness marker, local caller
token, and readiness attestation. It never reads or copies an API key, OAuth
credential, native Maka profile, session, history, or database. It leaves the
old files in place.

For an API installation, replace the former LaunchAgent after reviewing the
staged files:

```sh
mise -C "$HOME/.local/share/khenrix-utils/maka" run maka:harden-python
mise -C "$HOME/.local/share/khenrix-utils/maka" run maka:relay-install -- install \
  --replace-legacy-service \
  --legacy-service-label '<former local LaunchAgent label>' \
  --preserve-profile-credentials
```

The old service is restored automatically if this cutover fails. Once the
staged route passes its checks, activate the new launcher:

```sh
mise -C components/maka run maka:install-apply
```

If the copied state has not changed, undo the state migration with:

```sh
mise -C "$HOME/.local/share/khenrix-utils/maka" run maka:migrate-rollback
```

For an API route, restore the old service before rolling back state:

```sh
mise -C "$HOME/.local/share/khenrix-utils/maka" run maka:relay-install -- restore-legacy \
  --legacy-service-label '<former local LaunchAgent label>'
```

Installed component backups are kept owner-only beneath
`~/.local/state/khenrix-utils/maka/backups`. See the component README for the
backup-ID rollback command.

## Use Maka

Start the TUI in the repository Maka should work on:

```sh
cd path/to/project
maka
```

The launcher returns to the caller's working directory after selecting the
pinned runtime. It strips provider API keys and process-injection variables,
uses the standard native profile path, checks the selected connection policy,
and blocks self-update. Upgrade through Khenrix Utils instead of running
`maka update`.

Useful TUI commands include:

- `/setup` to add or authenticate a connection;
- `/model` to choose the active model;
- `/session` to open, import, or switch sessions;
- `/graph on`, `/graph off`, or `/graph <task>` to control Graph Mode;
- `/permissions auto` to restore approval prompts for privileged operations.

Each root session loads `using-superpowers` before substantive work so Maka can
select the relevant development workflow. Upstream names such as
`superpowers:systematic-debugging` map to the plain native skill
`systematic-debugging`; the direct-copy install does not use a plugin namespace.
Khenrix domain workflows and an explicitly named user skill keep precedence.

Run one headless turn with a prompt or stdin:

```sh
maka run "Review this repository and identify its highest-risk area"
cat plan.md | maka run -
maka run --continue "Continue the previous task"
maka run --resume <session-id> "Finish the review"
maka run --graph "Implement two independent slices, integrate them, then review"
```

If no thinking value is supplied, the launcher adds `--thinking xhigh`. On the
API route, request the available planning maximum explicitly:

```sh
maka run --thinking max "Create a detailed implementation plan"
```

Do not use that example with subscription OAuth; this pin supports only
`xhigh` there.

Maka stores session history locally. Prompt text, model input, and tool output
included in a model request still go to the selected OpenAI service for
inference.

## Policy and local data

Managed Khenrix state is split by purpose:

- `~/.config/khenrix-utils/maka/` stores the auth route, non-secret API account
  selector, readiness marker, migration receipt, and relay-local state;
- `~/.local/share/khenrix-utils/maka/` stores the portable component;
- `~/.local/state/khenrix-utils/maka/` stores install receipts and backups;
- Maka's native profile stores its local vault, workspace, and sessions.

The subscription launcher enforces one enabled canonical `openai-codex`
connection, `gpt-5.6-sol` as the default, and no enabled proxy, custom endpoint,
custom request body, arbitrary model overlay, or second provider. It does not
read, export, clear, or replace OAuth data. This is a launch-time Runtime Host
check, not an operating-system firewall; a user can change connections later
in the same session.

The API relay admits only narrowly checked Responses requests to the hard-coded
OpenAI origin. It checks a fresh local readiness proof before each launch and
revalidates the account-selector binding before every Keychain lookup. See the
component's `interactive/README.md` for its exact request and storage boundary.

## Verify the installation

Run the credential-free component tests from the checkout:

```sh
mise -C components/maka run maka:test
```

Run the read-only component doctor to verify the pin, npm integrity,
provenance, third-party notices, supported-platform locks, wrapper, and
installed-component drift:

```sh
mise -C components/maka run maka:component-doctor
```

Run the lightweight route and profile check from the stable component:

```sh
mise -C "$HOME/.local/share/khenrix-utils/maka" run maka:auth-doctor
```

This check does not start Colima. The full audit lab ships with every install,
but its full doctor is a macOS/Colima workflow and starts the dedicated
`maka-amd64` VM:

```sh
mise -C "$HOME/.local/share/khenrix-utils/maka" run maka:doctor
```

Other audit tasks include:

```sh
mise -C "$HOME/.local/share/khenrix-utils/maka" run maka:audit-smoke
mise -C "$HOME/.local/share/khenrix-utils/maka" run maka:import-smoke
mise -C "$HOME/.local/share/khenrix-utils/maka" run maka:broker-boundary-smoke
mise -C "$HOME/.local/share/khenrix-utils/maka" run maka:harbor-smoke
mise -C "$HOME/.local/share/khenrix-utils/maka" run maka:pier-preflight
```

Maka does not expose a standalone `maka audit` command in this version.
`maka:audit-smoke` exercises the released session adapters, persistence,
export, and re-import with synthetic data. The supported interactive import is
`maka`, `/session`, then **Import external session…**.

The eval lab uses pinned dependencies, hash-verified compatibility overlays, a
disposable Linux x64 controller, and an egress broker. The evaluated task gets
a random decoy rather than the real API key. Evidence is hash-indexed and
sealed to the local owner after each run; the owner can deliberately unseal it,
so it is not immutable forensic storage.

## Update Maka

Treat the package, lock, wrapper policy, provenance, overlays, tests, and docs
as one reviewed unit:

1. Identify the newest published Apache Maka tag and matching
   `maka-agent@nightly` version. A newer commit on `main` is not necessarily a
   published release.
2. Verify the npm integrity, tag, source commit, and license files.
3. Update the exact version and both supported platform locks.
4. Rebase each named compatibility overlay and verify its base and patched
   hashes rather than assuming it still applies.
5. Run `maka:test` and `maka:component-doctor`.
6. Run the full macOS doctor when the change touches the audit lab, then review
   `maka:install-plan` before applying the upgrade.
7. Verify the selected auth route, `ask` permission default,
   `gpt-5.6-sol`, and reasoning policy on the installed runtime.
