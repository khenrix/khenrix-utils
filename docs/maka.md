# Maka

Maka is a local-first agent workspace with a terminal UI, a one-shot CLI, Graph
Mode, and an evaluation runner. On this machine it is a companion to Claude,
Codex, and agy. `khenrix-utils` documents it, while
`~/git/agentic-setup` owns the package pin, wrapper, OpenAI relay, shared skills,
and live configuration. Each machine chooses its own OpenAI authentication
route. Agentic Setup keeps that choice as local, non-secret state.

There is no `make setup-maka` command. Maka discovers the selected shared skills
from `~/.agents/skills`, which Agentic Setup keeps aligned with the other CLIs.

## Install on a managed machine

The managed interactive setup supports the locked macOS Apple-silicon and Linux
x86-64 builds, including WSL. It requires `mise` and a checkout of Agentic Setup
at `~/git/agentic-setup`; that repository currently has no published remote, so
obtain the checkout through the team-approved source or transfer. Node.js 24 is
the validated runtime. ChatGPT subscription mode works on both supported
platforms. The company API-key relay and the full Colima eval lab remain
macOS-only.

Agentic Setup pins an exact published nightly because npm's `latest` tag points
to an early alpha with a much smaller command surface. Review the source change,
package integrity, and local compatibility overlays before moving the pin. Then
install the component:

```sh
cd ~/git/agentic-setup
unset XDG_CONFIG_HOME XDG_DATA_HOME
mise trust
mise install
mise trust components/maka/mise.toml
mise -C components/maka install
```

The managed wrapper uses the account's standard home-backed XDG locations. Keep
those variables unset during installation so mise puts the pinned package in the
same store the wrapper will use.

### Choose the authentication route

The route is a per-machine choice. Agentic Setup stores its name, without a
credential, in the owner-only
`~/.config/agentic-setup/maka-auth-mode` file.

| Mode | Use it when | Credential location |
|---|---|---|
| `api-key-relay` | This macOS machine has the company OpenAI Developer Key in Keychain. | The real key stays in Keychain. Maka stores only local relay credentials. |
| `chatgpt-subscription` | This supported macOS or Linux/WSL machine should use a ChatGPT account with Codex access for general, non-sensitive work. | Maka stores the OAuth session in its owner-only local plaintext credential vault. |

The subscription route is currently for general, non-sensitive work. The
shared skills that handle internal or personal data still enforce the approved
company OpenAI or Vertex routes and may refuse to run from a Maka subscription
session. A successful device login does not change those skill-level checks.

For the API-key route, select the mode and install the loopback relay:

```sh
cd ~/git/agentic-setup/components/maka
mise run maka:auth-mode -- api-key-relay
mise run maka:harden-python
mise run maka:relay-install -- install \
  --keychain-account '<Account field from the Codex Auth item>'
```

Use Keychain Access to locate the generic-password item whose service is exactly
`Codex Auth`, then copy only its non-secret **Account** field. The installer
validates that exact service/account pair without enumerating Keychain or
printing the credential. It stores the selected account in the owner-only
`~/.config/agentic-setup/maka-openai-keychain-account` file. The LaunchAgent,
interactive relay, and eval key extractor all read that same file.

The preparation task first force-reinstalls Python 3.12.14 from the locked mise
artifact. It removes macOS extended ACLs, clears group/world write permission
across the complete runtime without following symlinks, and verifies ownership
and filesystem containment. Run it again after any Python reinstall or repin.
The installer task uses that exact `python3.12 -I -S -B` executable with a
minimal environment and verifies it again before the first Keychain lookup.

The relay installer reconciles the Maka profile to its reserved connection and
removes other provider credentials from that profile, including OAuth tokens.
It writes a private v2 readiness marker only after final verification and binds
it to the SHA-256 of the exact account-selector bytes. Ordinary wrapper launches
preserve provider credentials and refuse API mode when that selector or marker
is missing, invalid, or mismatched. The relay checks the binding again before
every Keychain lookup. To change an existing account selector intentionally,
rerun the installer with the new `--keychain-account` value and
`--replace-keychain-account`.
The selector is write-once. Choose one route per machine; changing an
established route requires a separately reviewed migration.

For a ChatGPT subscription, select the subscription route:

```sh
cd ~/git/agentic-setup/components/maka
mise run maka:auth-mode -- chatgpt-subscription
```

Complete the managed setup from the repository root:

```sh
cd ~/git/agentic-setup
mise run setup:audit
mise run setup:plan
```

Review the generated plan before applying it. Use the plan ID printed by the
previous command:

```sh
mise run setup:apply -- --plan-id <plan-id>
```

For a ChatGPT subscription, start `maka` after the wrapper is installed, run
`/setup`, and choose `OpenAI OAuth (ChatGPT / Codex)`. Maka shows a device page
and one-time code. Open the page, sign in with the ChatGPT account, enter the
code, then select an available model. `/model` changes the selection later.
Model access, quota, and reasoning levels come from that ChatGPT account. Do
not copy Codex's `auth.json`; complete the device login separately on each
machine.

Check the selected route after any subscription sign-in. This lightweight
doctor executes the managed wrapper and does not start Colima:

```sh
cd ~/git/agentic-setup/components/maka
mise run maka:auth-doctor
```

Then record and verify the final state:

```sh
cd ~/git/agentic-setup
mise run setup:audit
mise run setup:record
mise run setup:verify
mise run check
```

The installed `~/.local/bin/maka` is a managed wrapper. It removes provider keys
from Maka's environment, fixes the rendered account home, selects the pinned
package, and supplies the configured reasoning effort for headless runs. In
`api-key-relay` mode it also verifies the Keychain-backed loopback relay. In
`chatgpt-subscription` mode it leaves Maka's native OAuth credential alone while
revalidating the enabled proxy and provider state on every launch. It deliberately
blocks `maka update`; upgrades go through the reviewed Agentic Setup workflow so
the package, wrapper, authentication policy, provenance, and tests move together.

The wrapper deliberately ignores ambient `XDG_CONFIG_HOME` and `XDG_DATA_HOME`.
It uses `~/Library/Application Support/Maka` on macOS and `~/.config/Maka` on
Linux/WSL so the subscription preflight, auth doctor, audit, and final CLI all
inspect the same profile and mise store.
The Keychain account selector and readiness marker apply only to API mode;
subscription mode ignores both.

Subscription startup checks that the active model connection is `openai-codex`,
that it has no custom endpoint or payload/model overlays, and that no network
proxy is enabled. This is a launch check, not an operating-system firewall.
Adding or enabling another provider inside the same Maka session changes that
boundary.

Agentic Setup manages the package pin, wrapper policy, shared skills and rules,
and the expected authentication mode. Its audit observer only reports Maka's
runtime policy and connection catalog. The wrapper separately reconciles the
approved chat defaults and route-specific catalog/proxy policy at launch.
Neither path creates, copies, or commits an API key, a ChatGPT OAuth session, or
the Maka profile. Complete authentication once on each machine and do not copy
the credential vault between computers.

The current nightly changes fresh sessions to unrestricted `bypass` mode.
Agentic Setup preserves and verifies `ask` as the global permission default.
Check the status line before running tools, and use `--yolo` only when full file
and network access is intentional.

## Install outside the managed setup

For a separate machine without Agentic Setup, resolve and install the complete
nightly CLI. Do not install the unrelated npm package named `maka`, and do not
use the `latest` tag.

```sh
version="$(npm view maka-agent@nightly version --registry=https://registry.npmjs.org)"
npm install --global "maka-agent@$version"
maka --version
maka --help
```

Start `maka` and run `/setup`. Choose `OpenAI` to enter an API key directly. To
use a ChatGPT subscription, enable upstream's current experimental gate for the
device-code flow, then choose `OpenAI OAuth (ChatGPT / Codex)`; the account must
have Codex access:

```sh
MAKA_CODEX_SUBSCRIPTION_EXPERIMENTAL=1 maka
```

A generic installation stores API keys and OAuth sessions in Maka's owner-only
local plaintext vault. It does not reproduce the managed Keychain relay. Follow
the [official Apache Maka CLI guide](https://github.com/apache/maka/blob/main/packages/cli/README.md)
for the upstream security boundary and platform details.

## Use Maka

Start the terminal UI in the project Maka should work on:

```sh
cd path/to/project
maka
```

Maka keeps session history and workspace state on this computer. Prompts, model
inputs, and any tool output included in a model request are still sent to the
selected remote OpenAI or ChatGPT service for inference.

Useful TUI commands:

- `/setup` adds or updates a model connection.
- `/model` changes the active model.
- `/session` opens, imports, or switches sessions.
- `/graph on`, `/graph off`, and `/graph <task>` control Graph Mode.
- `/permissions auto` restores approval prompts for privileged operations;
  `/permissions bypass` grants unrestricted access for the current session.

Run one non-interactive turn with a prompt or stdin:

```sh
maka run "Review this repository and identify its highest-risk area"
cat plan.md | maka run -
maka run --continue "Continue the previous task"
maka run --resume <session-id> "Finish the review"
maka run --graph "Implement two independent slices, integrate them, then review"
```

The managed wrapper supplies `xhigh` when `maka run` does not include an
explicit `--thinking` value. Run `maka run --help` for model, connection,
timeout, step-limit, resume, and full-access options.

## Audit and evaluation

Maka records sessions locally and can import synthetic or external Claude and
Codex sessions through `/session`. It does not expose a standalone `maka audit`
command. The managed component provides safe smoke tests around import, export,
the relay, and evaluation:

```sh
mise -C ~/git/agentic-setup/components/maka run maka:audit-smoke
mise -C ~/git/agentic-setup/components/maka run maka:import-smoke
mise -C ~/git/agentic-setup/components/maka run maka:broker-boundary-smoke
```

Run a declarative experiment directly with:

```sh
maka eval run experiment.json --out .maka-eval/run-001
```

Harbor and Pier runs have additional pinned container and Python requirements.
Use the managed component tasks and its README instead of installing those
dependencies globally.

## Update Maka

1. Fetch `https://github.com/apache/maka` and identify the newest published
   `v<nightly-version>` tag. A newer commit on `main` is not an installable
   release.
2. Resolve `npm view maka-agent@nightly version` and verify that its Git tag,
   npm integrity, and provenance agree.
3. Update the exact Agentic Setup pin, lock, wrapper path, provenance, container
   identities, and version checks.
4. Rebase every named compatibility overlay against the new package. Hash
   equality still needs to be demonstrated; do not assume it.
5. Run the component tests, doctor, Agentic Setup audit, reviewed plan/apply,
   record, verify, and check commands.
6. Confirm the live runtime policy still says `permissionMode: ask` and that the
   selected `api-key-relay` or `chatgpt-subscription` route still works.
