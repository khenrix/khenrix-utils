# Maka runtime and audit lab

Khenrix Utils owns the portable Maka runtime, its model and permission policy,
the managed launcher, authentication helpers, and the complete audit and
evaluation lab in this directory. It pins `maka-agent` to
`0.2.0-dev.44.20260920`; the npm integrity, Apache tag and commit, dependency
versions, and compatibility-overlay hashes are recorded in `provenance.json`.
The Apache license and notices are under `third_party/apache-maka/`.

Agentic Setup remains the broader inventory and sync owner for the other shared
skills and plugins used by Claude, Codex, agy, and Maka. Khenrix Utils owns and
direct-delivers `khenrix-quality`, `khenrix-writing`, and the 15-skill
Superpowers bundle. Installing this Maka component does not copy those skills;
Maka discovers them from the shared native skill root on the machine.

The managed runtime supports these routes:

| Platform | ChatGPT subscription | Keychain API relay | Run full eval lab |
|---|---:|---:|---:|
| macOS arm64 | Yes | Yes | Yes, with Colima |
| Linux x86-64, including WSL | Yes | No | No |

The installer requires `mise`. It copies only paths listed in
`install-files.txt` to `~/.local/share/khenrix-utils/maka`, installs the exact
lock for the current supported platform, and renders `~/.local/bin/maka`
atomically. It does not copy credentials or alter Maka's native profile. The
native profile remains `~/Library/Application Support/Maka` on macOS and
`~/.config/Maka` on Linux.

## Install

Run the dry-run plan from a Khenrix Utils checkout first:

```sh
cd ~/khenrix-utils
mise -C components/maka run maka:install-plan
```

On a new machine, apply the reviewed component and choose one authentication
route:

```sh
mise -C components/maka run maka:install-apply
mise -C "$HOME/.local/share/khenrix-utils/maka" run maka:auth-mode -- chatgpt-subscription
```

For a ChatGPT subscription, start `maka`, enter `/setup`, select **OpenAI OAuth
(ChatGPT / Codex)**, and complete the device login. Maka stores that OAuth
credential in its owner-only native profile. Khenrix Utils never copies it, and
you must not copy Codex's `auth.json` into Maka.

For the API-key route on macOS arm64, select the route and install the relay
from the stable component:

```sh
mise -C "$HOME/.local/share/khenrix-utils/maka" run maka:auth-mode -- api-key-relay
mise -C "$HOME/.local/share/khenrix-utils/maka" run maka:harden-python
mise -C "$HOME/.local/share/khenrix-utils/maka" run maka:relay-install -- install \
  --keychain-account '<Account field from the Codex Auth item>'
```

The supplied value is the non-secret **Account** field for the existing
Keychain item whose service is exactly `Codex Auth`. The real Developer Key
remains in Keychain. The installer does not enumerate Keychain or print the
credential.

Use stage-only installation when preparing an existing machine for migration:

```sh
mise -C components/maka run maka:stage
```

This updates `~/.local/share/khenrix-utils/maka` without replacing the current
`~/.local/bin/maka` launcher.

## Migrate an existing Agentic Setup installation

The component installer refuses to activate its launcher when it finds an old
Agentic Setup auth selector without the corresponding Khenrix state. Stage the
component, inspect the migration plan, and copy the reviewed state:

```sh
mise -C components/maka run maka:stage
mise -C "$HOME/.local/share/khenrix-utils/maka" run maka:migrate-plan
mise -C "$HOME/.local/share/khenrix-utils/maka" run maka:migrate-apply
```

The migration copies only the auth-mode selector. In API mode it also copies
the non-secret Keychain account selector, readiness marker, local caller token,
and local readiness attestation. It never reads or copies the Developer Key,
OAuth data, Maka's native profile, sessions, history, or databases. The old
files remain in place.

For a migrated API route, replace the former Agentic Setup LaunchAgent only
after reviewing the staged component:

```sh
mise -C "$HOME/.local/share/khenrix-utils/maka" run maka:harden-python
mise -C "$HOME/.local/share/khenrix-utils/maka" run maka:relay-install -- install \
  --replace-legacy-service \
  --legacy-service-label '<former local LaunchAgent label>' \
  --preserve-profile-credentials
```

`--replace-legacy-service` stops the former service and starts
`dev.khenrix.maka-openai-relay` atomically. A failed installation restores the
previous service if it was loaded. `--preserve-profile-credentials` is only for
this reviewed migration; a fresh API installation deliberately removes other
provider credentials while establishing its single-provider profile.

After the staged route passes its checks, activate the Khenrix launcher:

```sh
mise -C components/maka run maka:install-apply
```

The migration can be rolled back while the copied Khenrix state remains
unchanged:

```sh
mise -C "$HOME/.local/share/khenrix-utils/maka" run maka:migrate-rollback
```

For an API-service rollback, restore the untouched former LaunchAgent first:

```sh
mise -C "$HOME/.local/share/khenrix-utils/maka" run maka:relay-install -- restore-legacy \
  --legacy-service-label '<former local LaunchAgent label>'
```

Component upgrades keep owner-only backups under
`~/.local/state/khenrix-utils/maka/backups`. The apply output and install receipt
identify the backup ID. Restore one directly from the checkout with:

```sh
mise -C components/maka exec -- \
  python ./scripts/install_component.py rollback --backup-id <backup-id>
```

## Managed policy

Both routes use `gpt-5.6-sol`, `ask` permissions, and `xhigh` by default for
interactive and headless work. The launcher adds `--thinking xhigh` to
`maka run` when no explicit value is present.

The API relay declares exactly `xhigh` and `max`; an explicit
`maka run --thinking max ...` is the highest supported planning setting. The
ChatGPT subscription route exposes only `xhigh` in this pinned Maka build, so
`max` and `ultra` are unavailable there. Its closest honest planning setting is
therefore `xhigh`. Ordinary execution uses `xhigh` on both routes.

The launcher clears provider API-key variables, Node injection, mise injection,
and ambient XDG overrides before it starts Maka. It fixes the account home,
selects the pinned package, keeps the caller's working directory, and blocks
`maka update`; upgrades must change the reviewed Khenrix component and lock.

Subscription startup enforces one enabled canonical `openai-codex` connection,
`gpt-5.6-sol` as its default model, and no enabled proxy, custom base URL,
request body, arbitrary model override, or other provider. It never reads,
exports, clears, or replaces OAuth data. These Runtime Host checks run at
launch; they are not an operating-system egress boundary, and a user can change
connections later in the same TUI session.

The API route uses the authenticated loopback design documented in
`interactive/README.md`. It keeps the real key in Keychain, gives Maka only
local decoys, and admits narrowly validated Responses requests to OpenAI. Its
LaunchAgent is `dev.khenrix.maka-openai-relay`.

Managed component state lives beneath:

- `~/.config/khenrix-utils/maka/` for the route, account selector, readiness
  marker, and migration receipt;
- `~/.config/khenrix-utils/maka/relay/` for relay-only local decoys and locks;
- `~/.local/share/khenrix-utils/maka/` for the installed portable component;
- `~/.local/state/khenrix-utils/maka/` for install receipts and backups.

These directories do not contain the Developer Key. The native Maka profile
contains the selected route's local vault and session data and is deliberately
outside the portable component.

## Verify and test

Run the credential-free regression suite from the checkout:

```sh
mise -C components/maka run maka:test
```

Run the read-only portable doctor to check the package pin, provenance,
third-party attribution, both supported platform locks, wrapper template, and
installed-component drift:

```sh
mise -C components/maka run maka:component-doctor
```

The lightweight auth doctor inspects the selected route and native profile
without starting Colima:

```sh
mise -C "$HOME/.local/share/khenrix-utils/maka" run maka:auth-doctor
```

The full audit lab ships in every component install. Its full doctor is for
macOS with Colima and starts the dedicated `maka-amd64` VM:

```sh
mise -C "$HOME/.local/share/khenrix-utils/maka" run maka:doctor
```

`maka:harbor-smoke` builds the pinned Linux x64 controller and runs one
Terminal-Bench `fix-git` cell with `gpt-5.6-sol`, `xhigh`, one repetition, and
one task group. `maka:pier-preflight` checks Pier without starting a benchmark
trial. Eval credentials enter only the disposable controller through stdin.

The lab applies two hash-verified compatibility overlays to the published npm
payload. `relay-root-virtiofs-v2` handles Harbor's exact-root artifact writes on
macOS virtiofs without weakening other task paths. `eval-openai-onboarding-v2`
supplies the single pinned model row locally for the lab's exact OpenAI route,
so model discovery does not consume a broker capability, and names the hosted
Eval session to prevent an auxiliary title request from racing the first
subject request. Every near miss retains upstream behavior. Expected base and
patched hashes are in `provenance.json`; the staged runtime is covered by
`RUNTIME_SHA256SUMS`.

The evaluated task receives a random decoy and cannot mount the broker socket.
Only the egress proxy can replace authorization for a strictly validated
`POST https://api.openai.com/v1/responses` request using `gpt-5.6-sol` and
`xhigh`. The capability is bounded to 32 requests. The per-trial audit records
the authorized-request sequence without headers or body content.

Each run writes `evidence/runs/<run>/SHA256SUMS`, then makes the evidence files
owner-readable and directories owner-searchable. This is locally sealed,
hash-indexed evidence, not immutable forensic storage. Synthetic import tests
do not read real Claude or Codex histories.

`maka:audit-smoke` exercises released external-session adapters, persistence,
export, and re-import. Maka has no standalone `maka audit` command in this
version; the supported interactive import flow is `maka`, `/session`, then
**Import external session…**.
