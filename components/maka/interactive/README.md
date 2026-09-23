# Interactive Maka OpenAI relay

This directory implements the fail-closed provider route used when the managed
Maka auth mode is `api-key-relay`. It keeps the real OpenAI Developer Key in
macOS Keychain and gives Maka only a random loopback caller token. This route is
supported on macOS arm64. The alternative `chatgpt-subscription` route uses
Maka's official OpenAI device OAuth and does not use this relay.

The portable component is installed at
`~/.local/share/khenrix-utils/maka`. Merely installing or staging its files does
not start the relay or alter Maka's profile. The explicit
`install_maka_openai_relay.py install` action is the live installation
boundary.

## Request path

1. Maka connects to `http://127.0.0.1:48173/v1` with its random caller token.
2. Before it sends that token, the managed launcher gives `/healthz` a fresh
   challenge and verifies an HMAC made with the owner-only startup attestation.
   The relay never returns the attestation, so a captured proof cannot be
   replayed after a port takeover.
3. The relay verifies the exact loopback Host header, bearer token, HTTP
   method, path, model, and Responses request shape.
4. Only an admitted streaming `POST /v1/responses` triggers a lookup of the
   existing `Codex Auth` Keychain item.
5. The relay replaces the decoy authorization header and connects directly to
   the hard-coded `api.openai.com:443/v1/responses` TLS origin.
6. It buffers only the bounded first SSE event, requires the provider to report
   the requested model and `default` tier, then streams the response. It never
   writes the provider key to disk.

`GET /v1/models` is answered locally with `gpt-6-sol` and `gpt-5.6-sol`. The pinned
adapter's WebSocket probe receives a local 403 and falls back to HTTP.
Non-streaming title and recap calls are rejected, so auxiliary prompts cannot
consume this provider capability.

The reserved `openai-responses-compatible` connection exposes
both models with `xhigh` and `max`, defaulting to `xhigh`. An explicit
`--thinking max` remains `max`; the relay normalizes only the pinned client's
known derived `medium` fallback to `xhigh`. The pinned SDK can also derive a
`detailed` reasoning summary; the relay narrows that value to the reviewed
`auto` summary before forwarding. Other summary values are rejected. It rejects inbound `service_tier`
and adds `service_tier: "default"` to every admitted API request. The managed launcher supplies
`--thinking xhigh` for a headless run that has no explicit value.
The relay rejects a successful response before sending it to Maka if its
initial `response.created` event omits or changes the requested model or
`default` tier. It also stops forwarding if later response metadata changes.
After each response, the relay writes only requested/observed model and
processing tier, HTTP status, and time to the owner-only
`~/.local/state/khenrix-utils/maka/relay-last-tier.json`. It reads those
fields from bounded SSE metadata in memory and never saves the response
stream. A missing observed tier on a rejected response is not evidence of
Standard processing.
Run `mise run maka:relay-tier` to see the last receipt as `standard`,
`unverified`, `unobserved`, or `drift`. The command reads no response text.

The listener is also Maka's authenticated global HTTP proxy. It rejects every
CONNECT and absolute-form request, so an imported connection cannot use the
proxy to reach another provider. Only `127.0.0.1` and `localhost` bypass it.
The reconciler enables only the reserved `keychain-openai` connection, selects
`gpt-6-sol`, keeps `gpt-5.6-sol` selectable, and sets `ask` permissions with
`xhigh` as the normal default.

These checks cover traffic sent through Maka's configured model connection and
Runtime Host proxy. They are not an operating-system firewall for another
binary that discards proxy settings.

## Local state

- macOS Keychain holds the existing real Developer Key under service
  `Codex Auth`.
- `~/.config/khenrix-utils/maka/maka-auth-mode` contains `api-key-relay`.
- `~/.config/khenrix-utils/maka/maka-openai-keychain-account` contains the
  exact non-secret Keychain Account field, mode 0600. The installer validates
  that service/account pair without enumerating Keychain.
- `~/.config/khenrix-utils/maka/relay/caller-token` is a random local decoy,
  mode 0600.
- `~/.config/khenrix-utils/maka/relay/relay-attestation` is a random mode-0600
  value replaced on every service start and removed on a clean stop. It is only
  the local HMAC key for fresh readiness challenges.
- Maka's native credential vault stores the local bearer and proxy decoys.
- `~/.config/khenrix-utils/maka/maka-api-key-relay-ready` is written only after
  final relay and profile verification. Its v2 payload binds readiness to the
  SHA-256 of the exact account-selector bytes.
- `~/Library/LaunchAgents/dev.khenrix.maka-openai-relay.plist` contains only
  executable paths, local selector paths, and the port. It contains no
  credential.

The user LaunchAgent is intentional. Maka's Runtime Host can continue briefly
after the TUI disconnects, so a foreground-child relay could disappear during
a queued turn. The idle service holds no provider credential; it reads
Keychain separately for each admitted provider request.

The LaunchAgent starts the pinned Python 3.12.14 with `-I -S -B` under
`/usr/bin/env -i`, disables TLS key logging, and loads `/etc/ssl/cert.pem`
explicitly. Ambient Python, SSL, Node, mise, and provider-key variables cannot
redirect it. The hardening task force-reinstalls the locked Python artifact,
removes extended ACLs, clears group/world write bits through no-follow
descriptor operations, and verifies ownership and single-filesystem
containment before the installer can read Keychain.

## Review and test without changing live state

Render the non-secret LaunchAgent for review:

```sh
mise -C components/maka exec -- \
  python ./interactive/install_maka_openai_relay.py render
```

Run the credential-free regression suite:

```sh
mise -C components/maka run maka:test
```

The relay tests use a dummy provider key, an in-process fake upstream, an
ephemeral loopback port, and a fake Runtime Host. They verify origin and proxy
authentication, method/path/model/body gates, upstream header replacement,
CONNECT denial, protocol mutation order, fresh challenge-response readiness,
reasoning normalization, and idempotence. They do not access Keychain or the
network.

## Fresh API installation

First install the portable component and choose `api-key-relay`. Then run from
the stable component path:

```sh
mise -C "$HOME/.local/share/khenrix-utils/maka" run maka:harden-python
mise -C "$HOME/.local/share/khenrix-utils/maka" run maka:relay-install -- install \
  --keychain-account '<Account field from the Codex Auth item>'
```

The auth selector is write-once. The installer refuses to change the
LaunchAgent, relay state, or Maka profile when the selector is absent or says
`chatgpt-subscription`. It validates only the supplied `Codex Auth` account and
never prints Keychain output. Replacing an established account selector
requires both a new value and `--replace-keychain-account`.

The installer removes an old readiness marker, creates the local decoys,
writes and loads `dev.khenrix.maka-openai-relay`, verifies the local endpoint,
and reconciles the native Maka profile through supported Runtime Host APIs. A
fresh install deletes credentials for non-relay connections plus any stored
Tavily key and leaves only the two local decoys. It does not edit Maka's SQLite
or JSON files directly. Restoring a removed provider later requires
reauthentication.

A new readiness marker is published only after a second relay check, private
file verification, and successful profile reconciliation. The relay rechecks
the current selector and bound marker before every Keychain lookup. Ordinary
launcher starts repair policy and local decoys without running the installer's
broad credential purge.

Any failure produces a fixed local error. Provider responses, request bodies,
tokens, and Keychain output are suppressed. A proxy or configuration failure
blocks provider traffic instead of falling back to another connection.

## Migrate the former Agentic Setup relay

Stage the Khenrix component and use its reviewed legacy-state migrator before
replacing the service. The migrator copies only the route selector, non-secret
account selector, readiness marker, caller token, and attestation into
`~/.config/khenrix-utils/maka`; it leaves all former state in place.

After reviewing `maka:migrate-plan` and applying `maka:migrate-apply`, run:

```sh
mise -C "$HOME/.local/share/khenrix-utils/maka" run maka:harden-python
mise -C "$HOME/.local/share/khenrix-utils/maka" run maka:relay-install -- install \
  --replace-legacy-service \
  --legacy-service-label '<former local LaunchAgent label>' \
  --preserve-profile-credentials
```

The replacement stops the explicitly named former local relay service, starts
the Khenrix service, and restores the former service automatically if the new
installation fails. The legacy plist and state are not deleted.
`--preserve-profile-credentials` avoids the fresh installer's broad credential
purge while the existing reviewed profile is being moved.

To undo the service cutover, stop the Khenrix service and restart the untouched
former LaunchAgent:

```sh
mise -C "$HOME/.local/share/khenrix-utils/maka" run maka:relay-install -- restore-legacy \
  --legacy-service-label '<former local LaunchAgent label>'
```

Then run `maka:migrate-rollback` if the copied Khenrix state is unchanged. The
rollback verifies the receipt hashes and removes only its own copies; it does
not alter the former state or native Maka profile.
