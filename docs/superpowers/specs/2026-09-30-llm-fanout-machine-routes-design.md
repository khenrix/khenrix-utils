# Machine-specific model routes for llm-fanout

Status: approved for implementation planning, 2026-09-30. This amends the approved
[isolated-container design](2026-09-29-llm-fanout-container-boundary-design.md),
not its filesystem or credential boundary. The existing
[implementation plan](../plans/2026-09-29-llm-fanout-container-boundary.md)
must be amended before Task 6 live certification or later release work resumes.
This document does not authorize model spend, installation, or repository writes.

## Outcome and scope

The same fanout code must work on machines whose approved Claude, Codex, and agy
routes differ. One machine may use organization-approved Vertex AI in an EU
location for Claude and Gemini and an OpenAI project API key for Codex. Another
may use vendor subscriptions for non-sensitive work. A route is a per-machine
policy and transport choice, not a synonym for a model or CLI. A successful
certificate for one machine or route must never imply that a different route is
approved or working.

The immediate implementation target is the organization-route machine, because
that is the available certification host. Subscription routes are first-class
profile types in admission and doctor, not a hidden fallback. Existing
subscription-backed native-CLI entry points retain their existing declared
capabilities, limitations, and local data policy; this amendment grants them
no new write authority. A subscription route gains the isolated
repository-writing class only after a vendor-supported, host-only
transport and a separate live certificate prove the same boundary. This is a
capability requirement, not a claim that all three subscription transports are
already feasible. In particular, no route is called repository-write capable
merely because a CLI login succeeds.

## Route authority and selection

An owner-held, versioned machine-route manifest declares one or more typed
profiles per provider. It contains no credential values or OAuth cache paths.
Each profile has a stable route ID; provider and requested, wire, and effective
upstream model IDs; authentication family and host-only credential reference;
fixed upstream origin and API family; project and location where relevant;
maximum data classification; and the execution classes it may attempt.
`fanout doctor` derives `configured`, `authenticated`, and `certified` state
from separate checks and evidence; a manifest cannot self-declare a pass.
The manifest is 0600 outside the repository and is checked against the
machine's Agentic Setup policy. Public fixtures may use invented projects and
inert credential references only.

Admission explicitly selects one profile for each seat. Selection considers
the task's data classification and requested execution class before any model
call. It does not infer approval from ambient environment variables, a CLI
login, a present Keychain item, or a working alternative route. There is no
automatic failover from Vertex to a direct vendor API or from an organization
project to a personal subscription. A user-requested change of route starts a
new admission and cannot reuse the old permit, session, or certificate.
The three providers may use different authentication families in one fanout
run; there is no requirement for three direct API keys.

`fanout doctor` reports configured policy, credential-source reachability, and
model-call proof separately for every profile. A metadata listing, login
status, or successful ADC token mint does not count as a usable model call.
Doctor redacts account identifiers, project IDs, tokens, and response bodies
from ordinary output. A paid canary runs only with its existing explicit spend
authorization and through the selected approved route.

## Host-only transport boundary

The container remains networkless. Its CLI sees only a disposable placeholder
and a seat-local loopback/Unix-socket endpoint. Neither the seat nor the VM
relay receives an ADC file, API key, subscription token, 1Password session,
Keychain access, or host home mount. The authenticated host broker validates
the guest's original provider wire request and hands a bounded, structured
request to a route-specific host sender. The sender may resolve a short-lived
credential from an approved host-only source, but it never returns that
credential to the broker protocol or guest. Credential values are not logged,
hashed into public evidence, or used as route identity. Credential rotation
may preserve a route only when its approved source, principal/project, and
policy remain the same; drift fails admission.

For Vertex Claude, the host sender maps the validated Messages request to a
pinned regional Anthropic publisher model endpoint, removes any guest-selected
upstream model field, inserts the required Vertex protocol version, and uses
host ADC authorization. For Vertex Gemini, it maps the validated Gemini
request to the pinned regional Google publisher model endpoint and uses host
ADC authorization. For OpenAI API-key Codex, it keeps the pinned Responses API
path and obtains the approved project credential from a host-only source such
as a Keychain relay or an owner-approved 1Password reference. The sender pins
the project as well as the origin. Exact request/response and streaming
compatibility must be proved against the installed CLI versions; the mapping
is not assumed from documentation alone. The current hard-coded direct
Anthropic, OpenAI, and Gemini sender is not an approved substitute for these
machine routes.

Subscription adapters are distinct. They must use each vendor's documented
account or OAuth mechanism; copying a native CLI's login cache into a seat or
relabeling a subscription token as an API key is forbidden. A host-only adapter
must prove that the CLI can complete initial and exact cold-resume turns through
the fixed endpoint while the guest remains credential-free and spend remains
finite. Some subscription API routes have different request fields and token
limits from API-key routes, so adapters may not silently reuse the direct-key
transform. If a route cannot enforce the existing permit's spend and disclosure
bounds, it stays uncertified for repository writes pending a separately reviewed
budget design. Subscription use with internal data additionally requires
machine policy to approve that data class; this design does not grant it.

## Permit, recovery, and certificate identity

The existing `route` field means `initial` or `resume`; a new, separately named
upstream-route identity is required. The broker permit, HMAC spend ledger,
outbound request record, container-seat descriptor, initial-to-resume session
binding, private six-cell receipt, and trial/release certificates bind the
selected route manifest digest, auth family, upstream origin/API family,
project/location identity where relevant, transform version, and effective
model. They bind no credential value. At cold recovery, the controller reopens
the owner-held manifest and rejects a changed route or policy before any send.
An uncertain send remains uncertain; recovery never retries it just because a
credential was refreshed. A certificate is scoped to its machine and route
set and cannot be moved to a subscription machine or used after route drift.

Both request and response attestation refer to the effective upstream route,
not merely the CLI's local `/v1/messages`, `/v1/responses`, or Gemini path.
Route-specific response validation retains the broker's JSON/SSE, deadline,
byte-budget, secret-reflection, and redirect refusals. An unrecognized upstream
model, extra endpoint, incompatible stream, authentication error, quota error,
or transformed request outside its pinned schema fails closed with private raw
evidence and a red certification cell. No adapter may downgrade such a result
to a provider answer.

## Verification and rollout

1. Amend Task 6 before further paid work. Add deterministic tests for route
   manifest validation, data-class policy, explicit selection, no fallback,
   credential-source isolation, fixed origins and transforms, model/project/
   region binding, response compatibility, credential rotation, route drift,
   cold recovery, and certificate cross-route rejection. Fake credentials and
   endpoints prove logic; they are not live route evidence.
2. On the available organization-route machine, use host-only credential
   sources and run the same bounded six-cell Claude/Codex/agy initial/resume
   private certification, including the actual approved Vertex and OpenAI
   routes. A successful ADC mint or 1Password metadata read is preliminary;
   require a completed model turn and effective-route evidence. Keep all raw
   transcripts and machine identifiers outside the public repository.
3. Keep the existing synthetic two-repository trial, same-source-hash strict
   gate, Maka quality evaluation, and separate trial/release certificates.
   Any sender, transform, manifest, policy, or certificate-code change after a
   live proof invalidates its certificate and requires the final-source checks
   again. The completed no-provider VM proof remains useful evidence but does
   not itself certify a changed provider route.
4. On a subscription machine, configure only routes allowed by that machine's
   policy, prove a harmless model turn for each claimed capability, and run a
   separate six-cell certificate before enabling isolated repository writes.
   If a provider's subscription mechanism cannot satisfy the host-only and
   finite-spend contract, report that exact profile as unsupported for this
   execution class rather than weakening the boundary or claiming parity.

The public feature gate, caller-repository handover, Jira/PR/deploy barriers,
and prohibition on provider credentials inside the VM remain unchanged.
Retirement or repository-writing availability on any machine requires its own
certified route set; a certificate for the organization-route machine does not
prove subscription-machine parity.

## Source references

- [Claude on Vertex request format](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/partner-models/claude/use-claude)
- [Gemini content-generation API](https://docs.cloud.google.com/gemini-enterprise-agent-platform/reference/models/inference)
- [Claude Code authentication](https://code.claude.com/docs/en/authentication)
- [Antigravity CLI authentication](https://www.antigravity.google/docs/cli/install/) and [enterprise ADC](https://www.antigravity.google/docs/enterprise)
- [OpenAI ChatGPT plan usage through Codex app-server](https://developers.openai.com/siwc/token-sharing-open-source/codex-app-server) and [preview limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)
