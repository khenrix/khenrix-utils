# Maka runtime and audit lab

Khenrix Utils owns the portable Maka runtime, its model and permission policy,
the managed launcher, authentication helpers, and the complete audit and
evaluation lab in this directory. It pins `maka-agent` to
`0.2.0-dev.70.20261005`; the npm integrity, canonical Apache source commit, dependency
versions, and compatibility-overlay hashes are recorded in `provenance.json`.
The Apache license and notices are under `third_party/apache-maka/`.

The Harbor controller, compatibility overlays, and historical certification
records remain frozen at `0.2.0-dev.62.20260928`. They do not certify this
interactive runtime upgrade; the full legacy lab gate has not been rerun.
The current package's source association comes from canonical npm provenance
metadata, not a Git release tag or verified attestation signature.

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

The installer requires `mise`. It stages only paths listed in
`install-files.txt` plus an isolated copy of the exact lock-pinned npm package,
applies the source-hash-guarded GPT-6.1 patch, then copies that verified candidate
to `~/.local/share/khenrix-utils/maka` and renders `~/.local/bin/maka`
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

This builds a complete candidate under
`~/.local/share/khenrix-utils/maka-candidates/` and verifies the pinned
package overlay. It leaves the active component and `~/.local/bin/maka`
unchanged.

## Install a reviewed source SDK companion

The source-runtime commands deliver an independently validated source-built SDK
under the installed component's `runtime/source-candidate/`. The npm CLI pin,
wrapper, and npm install receipt continue to describe the npm installation.
Companion delivery does not start a Runtime Host or certify a native model call.

Prepare an owner-only JSON file containing the full reference of the candidate's
`runtime-manifest.json`: `path`, `sha256`, `bytes`, `owner`, `mode`, `device`, and
`inode`. The manifest must enumerate the complete regular-file closure with
those same fields, plus `artifact_root`, `schema`, `files`, `entrypoints`, `worker`,
and `statistics` (`files`, `bytes`, `symlinks: 0`). Any packaging repair's `staged`
reference must identify a closure file; its `original` reference stays intact.
Source manifests are consumed as frozen inputs; their schema name is recorded
without imposing a particular build workflow.

Generate the plan without writing installed state, then review its complete JSON
and SHA-256 before applying it. Plan output uses sorted, indented JSON with a
trailing newline, so the saved output's SHA-256 is the approval digest:

```sh
mise -C components/maka run maka:source-runtime-plan -- \
  --input-ref /absolute/path/manifest-reference.json > /absolute/path/reviewed-plan.json
shasum -a 256 /absolute/path/reviewed-plan.json
mise -C components/maka run maka:source-runtime-apply -- \
  --plan /absolute/path/reviewed-plan.json --plan-sha256 '<reviewed SHA-256>'
mise -C components/maka run maka:source-runtime-doctor
```

The helper verifies every file's hash, size, mode, owner, path and inode, rejects
extra files, directories, symlinks and unsafe ancestors, then verifies independent
staged copies. It rebinds installed references to their final paths and inodes,
including worker, entrypoints and staged packaging repairs. Original build
references and the full source manifest reference remain in the installed record.
Upgrades use native atomic directory exchange on macOS or Linux, serialize under
an owner-only lock, and compare the reviewed prior receipt and closure before and
after cutover. Receipt publication uses an atomic exchange or exclusive rename,
preserving any concurrent replacement. Staged files, directories and a transaction
journal are fsynced before cutover. Recovery classifies bound tree and receipt
identities; it never deletes a previous tree based on a process-local phase flag.

The independent receipt is
`~/.local/state/khenrix-utils/maka/source-runtime/receipt.json` (mode `0600`).
Backups remain under that directory's `backups/`; rollback preserves the removed
candidate there. Review the current receipt and its SHA-256 before rollback:

```sh
mise -C components/maka run maka:source-runtime-rollback -- \
  --receipt-sha256 '<reviewed current receipt SHA-256>'
```

An interrupted process leaves `source-runtime/transaction.json`. Plan, doctor and
ordinary mutations refuse pending transactions. Review that owner-only journal
and its SHA-256 before recovering it:

```sh
mise -C components/maka run maka:source-runtime-recover -- \
  --transaction-sha256 '<reviewed transaction SHA-256>'
```

Recovery completes a fully published transaction or restores its exact prior
state. An unknown tree or concurrent receipt is preserved and reported for
reconciliation. Retained staging and backup paths remain available for review.

Normal component activation and component rollback share the companion lock and
refuse an active companion or pending transaction. Explicitly retire the SDK
before replacing the npm component; retirement moves the verified companion and
receipt into owner-only backups without changing npm provenance:

```sh
mise -C components/maka run maka:source-runtime-retire -- \
  --receipt-sha256 '<reviewed current receipt SHA-256>'
```

For isolated delivery, all commands accept absolute `--component-root` and
`--state-root` paths. Apply uses the paths recorded in its reviewed plan. Run the
credential-free companion tests with `mise -C components/maka run maka:source-runtime-test`.

## Migrate an existing Agentic Setup installation

The component installer refuses to activate its launcher when it finds an old
Agentic Setup auth selector without the corresponding Khenrix state. Stage the
component, inspect the migration plan, and copy the reviewed state:

```sh
mise -C components/maka run maka:stage
mise -C components/maka run maka:migrate-plan
mise -C components/maka run maka:migrate-apply
mise -C components/maka run maka:install-apply
```

The migration copies only the auth-mode selector. In API mode it also copies
the non-secret Keychain account selector, readiness marker, local caller token,
and local readiness attestation. It never reads or copies the Developer Key,
OAuth data, Maka's native profile, sessions, history, or databases. The old
files remain in place.

For a migrated API route, replace the former Agentic Setup LaunchAgent only
after activating the verified component:

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
identify the backup ID. Before restoring a component that predates the
`parallelToolCalls` relay profile, downgrade that profile while the newer
component is still active. This uses Maka's supported Runtime Host catalog
update to restore the 6.0/5.6 model overrides and, when selected, the 6.1 default to 6.0:

```sh
cd "$HOME/.local/share/khenrix-utils/maka"
mise exec -- node --input-type=module <<'JS'
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const load = (path) => import(pathToFileURL(join(process.cwd(), path)).href);
const { restoreLegacyMakaRelayProfile } = await load('interactive/configure_maka_openai_relay.mjs');
const packageRoot = 'runtime/package';
const { connectRuntimeHostCliConnection } = await load(
  `${packageRoot}/dist/runtime-host-cli-context.js`,
);
const { readRuntimeHostConnectionCatalog } = await load(
  `${packageRoot}/node_modules/@maka/runtime-host/dist/client/catalog-reader.js`,
);
const { resolveMakaWorkspaceRoot } = await load(
  `${packageRoot}/node_modules/@maka/storage/dist/workspace-root.js`,
);
const context = await connectRuntimeHostCliConnection({ rootPath: resolveMakaWorkspaceRoot() });
try {
  await restoreLegacyMakaRelayProfile(context.connection, readRuntimeHostConnectionCatalog);
} finally {
  await context.close();
}
JS
```

The downgrade rejects an unfamiliar reserved connection and preserves credentials
and the proxy. Restore the backed-up component
directly from the checkout after the downgrade:

```sh
mise -C components/maka exec -- \
  python ./scripts/install_component.py rollback --backup-id <backup-id>
```

The final receipt is `~/.local/state/khenrix-utils/maka/install-receipt.json`.
It is published atomically only after component cutover succeeds and records the
reviewed package, version, npm integrity, Apache source commit, source/patched
hashes of the GPT-6.1 compatibility overlay, and a digest of every installed
runtime package file. The component
doctor rejects a missing, stale, non-private, or malformed receipt, component
drift, and managed-wrapper drift. `maka:stage` builds a separate candidate
with a private candidate receipt and does not touch the active component,
wrapper, or final receipt. A backup rollback restores the receipt that belonged to the restored
runtime, or removes it when that runtime had no final receipt.

## Managed policy

Both routes use `gpt-6.1-sol` and `ask` permissions. Execution defaults to medium;
planning, review, and merging explicitly select xhigh. Existing explicit native
chat effort is preserved. The launcher adds `--thinking medium` to
`maka run` when no explicit value is present. `gpt-6-sol` and `gpt-5.6-sol` remain selectable
so existing sessions can resume.

The API relay declares `medium`, `xhigh`, and `max` and preserves admitted effort.
An explicit `maka run --thinking max ...` remains supported. The
ChatGPT subscription route exposes only up to `xhigh` for GPT-6.1 Sol until a
live `max` probe proves that entitlement. Therefore
`max` and `ultra` are unavailable there. Its closest honest planning setting is
therefore `xhigh`. The separate no-tools judge still requires xhigh.

The launcher clears provider API-key variables, Node injection, mise injection,
and ambient XDG overrides before it starts Maka. It fixes the account home,
selects the pinned package, keeps the caller's working directory, and blocks
`maka update`; upgrades must change the reviewed Khenrix component and lock.

Subscription startup enforces one enabled canonical `openai-codex` connection,
`gpt-6.1-sol` as its default model, while retaining `gpt-6-sol` and `gpt-5.6-sol` for old sessions,
and no enabled proxy, custom base URL,
request body, arbitrary model override, or other provider. It never reads,
exports, clears, or replaces OAuth data. These Runtime Host checks run at
launch; they are not an operating-system egress boundary, and a user can change
connections later in the same TUI session.

The API route uses the authenticated loopback design documented in
`interactive/README.md`. It keeps the real key in Keychain, gives Maka only
local decoys, and admits narrowly validated Responses requests to OpenAI. Its
LaunchAgent is `dev.khenrix.maka-openai-relay`.
The relay rejects inbound processing-tier settings and adds
`service_tier: "default"` after validating each request. This explicitly
requests OpenAI Standard processing on the API route. It checks the provider's
reported model and tier before releasing a successful response stream, and
rejects missing or differing initial metadata. The subscription route
has no API project-tier guarantee.
The API relay keeps an owner-only, metadata-only last-response receipt so its
observed tier can be checked with `mise run maka:relay-tier` without logging
response content.

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

Run the current portable runtime, journal/protocol, transport and safety regressions
from the checkout. This route needs no historical package, profile access or model call:

```sh
mise -C components/maka run maka:test-native
```

The broader route also preserves the frozen Harbor overlay and runtime-boundary
tests. Supply a separately obtained, unpatched `.62` package explicitly:

```sh
mise -C components/maka run maka:test -- \
  --baseline-package-root /private/path/to/maka-agent-0.2.0-dev.62.20260928
```

It refuses a missing, differently versioned, patched or source-drifted baseline
before test launch; it never downloads or installs one. Current-runtime tests
still use the current package, while frozen checks use only that baseline.
The broad route retains the original local-profile preservation and import
smokes. The native route covers synthetic import/export and CLI help/version
in private scratch; real-profile preservation is a separate installed check.
Neither regression route constitutes a historical paid Harbor certification.

Run the read-only portable doctor to check the package pin, provenance,
third-party attribution, both supported platform locks, wrapper template, final
receipt, and installed-component drift:

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

The separate blind A/B judge requests and attests exactly `gpt-6.1-sol`, `xhigh`,
default service tier, and zero tools. It refuses historical 5.6 judge evidence.
The coding-tool smoke does not certify this route.
After installing this component, building the updated controller/egress images,
and staging the immutable Linux runtime, `maka judge-harbor-boundary` is a
read-only, non-paid probe that requires the dedicated Colima profile to be
running. It verifies the installed source against its private install receipt,
the complete staged runtime tree, and both resolved controller/egress image
identities,
then prints the digest to pin as `expected_boundary_sha256`. A paid ballot
requires both an explicit selector and one JSON request on stdin:

```sh
MAKA_JUDGE_PAID_RUN=1 maka judge-harbor - < private-ballot.json
```

The request has exactly `panel_id: "harbor"`, `criterion`, `answer_a`,
`answer_b`, and the pinned `expected_boundary_sha256`. Each ballot uses a fresh
Host session and nonce. The route keeps request JSON in bounded process memory
and a one-use private FIFO; it never creates a regular request file. The judge
container reaches the provider only through
the relay, which removes all outbound tools; the private relay receipt binds
the nonce and prompt hashes, model, effort, service tier, and zero tools. The
Host transcript must contain exactly one user prompt and one structured answer
without tool messages. The ballot and transcript are not included in the
returned receipt. The route remains uncharacterized until a live provider run
confirms the transmitted request, provider response, and Host archive together.

The coding runtime applies two hash-verified compatibility overlays to the published npm
payload. `relay-root-virtiofs-v2` handles Harbor's exact-root artifact writes on
macOS virtiofs without weakening other task paths. `eval-openai-onboarding-v2`
supplies the single pinned model row locally for the lab's exact OpenAI route,
so model discovery does not consume a broker capability, and names the hosted
Eval session to prevent an auxiliary title request from racing the first
subject request. Every near miss retains upstream behavior. Expected base and
patched hashes are in `provenance.json`; the staged runtime is covered by
`RUNTIME_SHA256SUMS`.

The current judge adds `harbor-judge-sol61-v1` after those frozen overlays. It
adds the 6.1 discovery row alongside 5.6 under the same exact lab-route guard,
and API metadata with `low`, `medium`, `high`, `xhigh`, and `max` efforts. It
keeps its original immutable runtime identity and hashes.

The active judge selects `harbor-judge-sol61-h2-v1`, adding `undici-h2-close-v2`
after the unchanged 6.1 overlay. Its separate immutable runtime ends in
`relay-root-virtiofs-v2-eval-openai-onboarding-v2-harbor-judge-sol61-v1-undici-h2-close-v2`.
The H2 overlay registers completion listeners before destroying the socket and
completes teardown once when either the socket or HTTP/2 session closes. It also
handles sessions that are already destroyed and removes both listeners after
completion. The previous `undici-h2-close-v1` runtime remains immutable. Exact input
and output hashes are pinned in provenance, completion markers, image labels,
runtime-path checks and the active boundary. The build stages and checks all
three runtime identities; the default coding runtime and its 5.6 benchmark
inputs stay pinned. The historical coding runtime retains its independently
measured H2 shutdown failure. Synthetic coding-body and current 6.1/xhigh/default/
zero-tools H2 regressions run against the corrected active runtime; they do not
certify a historical-runtime pass. Changed component source invalidates old install and judge-boundary
receipts. Source tests do not replace a reviewed owner install, dedicated image
rebuild, fresh boundary digest, and paid characterization.

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
