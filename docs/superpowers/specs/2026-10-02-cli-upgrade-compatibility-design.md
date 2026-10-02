# Installed skills across CLI updates

Status: written spec and implementation plan approved on 2026-10-02; execution resumed.
Plan: [CLI upgrade compatibility](../plans/2026-10-02-cli-upgrade-compatibility.md).
This amends CLI admission and development validation in the existing
fanout and Agentic Setup flows. It does not grant new execution, route, spend,
publication, or deployment authority.

## Intent

An installed skill should remain useful when its host CLI is updated. A newer
version string or a mismatch with the versions recorded during development must
not disable the skill. When we change a skill, its helper, or the shared runtime,
we must validate the affected behavior against the CLI versions actually used
for that release. Apply this policy to Claude, Codex, agy, and Maka where they
provide the relevant capability, not only to fanout.

The user's clarification supersedes a blanket requirement to revalidate or
recertify every installed skill after every CLI update. Development validation
is evidence about a skill revision; it is not an expiring runtime license.

## Lifecycle contract

| Event | Required behavior |
| --- | --- |
| Host CLI updates; installed skill is unchanged | Keep discovery and ordinary use available. A recorded-version mismatch alone is not an error or admission refusal. |
| Skill instructions, metadata, helper, or shared runtime changes | Validate the affected revision with the current target CLI versions before publishing or claiming it verified. |
| Required CLI behavior actually breaks | Stop the affected operation with a concrete capability/protocol error; preserve evidence and repair its adapter or instructions. |
| A certified container image, isolated executable, route, or security boundary changes | Keep existing exact-boundary certificate checks and revalidate the affected certified path. |

No automatic paid evaluation, background recertification, CLI downgrade, or
installation rewrite follows solely from a host CLI update. A user-requested
doctor can report that a version is newer than the last development measurement,
but that is informational, not a claim of incompatibility.

## Existing runtime and adapter changes

Extend the existing provider adapters, registry, process validation, and setup
probes. Do not introduce a second invocation engine or a generic compatibility
service. Runtime eligibility depends on the behavior required for the selected
operation, not membership in a hardcoded CLI-version allow-list.

Use stable supported invocation forms and existing semantic parsers. Where a
local preflight is needed to protect a required capability, check that capability
without a model call. Help wording and whole-help hashes are diagnostic evidence,
not contracts. Help alone cannot prove isolation or successful model execution.
Ordinary skill invocation must not acquire a full characterization/evaluation
requirement merely because the CLI is newer than the release measurement.

Retain strict checks for session identity, explicit resume, stdin transport,
required terminal fields, provider errors, output limits, tool restrictions, and
execution boundaries. Permit harmless additional fields or progress data only
where their semantics cannot hide an error or security-relevant action. Missing
success evidence is not success. Do not adapt by silently widening permissions,
changing model or route, or substituting human approval for an actual capability.

For a new fanout run, retain the currently observed invocation identity rather
than compare it with a development-time version constant. Use the existing
profile and run-input machinery. A running or recovered session remains bound
to its recorded inputs; replacing its executable must not silently alter that
record or repeat uncertain paid work. Reuse the existing controlled profile
transition path when a transition is supported; otherwise require a new run or
the recorded executable. This is a particular session's consistency constraint,
not a global ban on using the installed skill after an upgrade.

## Discovery and setup

Agentic Setup's native discovery checks must execute the relevant loader and
compare the managed skills' names, paths, metadata, enablement, and loader errors.
Do not reject the probe before it runs just because the CLI version is absent
from a reviewed-version list. Do not require a fixed vendor system-skill count
or catalog; vendor additions must not fail managed-skill discovery.

Report discovery, configuration parity, authentication, completed model calls,
and certified execution separately. A discovery pass proves neither credentials
nor model behavior. Missing metadata or a real loader error remains actionable.
Keep Khenrix-owned skill delivery separate from Agentic-owned configuration and
preserve unmanaged configuration and user changes.

Instruction-only skills use this delivery and development-validation policy;
they do not each need a CLI-version checker. Script-backed skills and consumers,
including the evaluation harness, should reuse the owning invocation contract
rather than add their own version allow-lists.

## Development and release evidence

On a skill change, identify the affected closure and targets. Run the relevant
deterministic tests, native discovery/metadata checks, and bounded behavior or
pressure evaluations using the current CLIs. Retain the skill/runtime revision,
observed CLI versions and invocation identities, actual commands, outcomes, and
raw private evidence. An unavailable target must be reported as unverified, not
quietly counted as passing. Shared-runtime changes require tests of their affected
consumers, not only the edited skill's prose.

Older receipts remain truthful historical measurements. Never rewrite their
versions or claim they tested a newer binary. A CLI-only host update does not
make those measurements false or automatically invalidate unchanged skill
source. New skill releases still require fresh evidence for their actual revision.

Keep exact image, guest CLI digest, source, route, permit, trial, and release
certificate identity for isolated repository writes. A host CLI update must not
invalidate an unchanged certified guest image it does not execute; updating the
guest binary or image does affect that certificate. Compatibility metadata is
not permission to issue or reuse a certificate for a different boundary.

The final fanout release still needs its approved live/canary/Maka evidence.
Codex's missing positive served-model observation is a separate unresolved
release-proof requirement; a version-policy change does not manufacture it.

## Acceptance checks and scope limits

- An unchanged installed skill remains discoverable and usable after a compatible
  CLI version change, without editing its instructions or version constants and
  without a full paid evaluation.
- Harmless help/schema additions pass; a missing required flag, broken resume,
  malformed terminal, weaker boundary, or actual loader error fails the affected
  operation with a specific reason.
- A changed skill is validated against the current target versions, with honest
  revision-bound evidence. Native discovery validates managed metadata without
  asserting the vendor's system inventory is fixed.
- Run-input drift, uncertain sends, and changed certified guest/image/route
  identity still refuse unsafe recovery or certificate reuse.

This amendment does not promise immunity to genuine upstream breaking changes,
repair unrelated connector authentication, upgrade project-declared runtimes,
or reopen completed delivery-ticket work. Implement the smallest change to the
existing flows and retain the approved final release sequence.

## References

- [OpenAI skill discovery and metadata](https://learn.chatgpt.com/docs/build-skills).
- [Existing machine-route design](2026-09-30-llm-fanout-machine-routes-design.md).
- [Existing isolated-container design](2026-09-29-llm-fanout-container-boundary-design.md).
- `shared/lib/fanout/providers.py`, `shared/lib/fanout/execute.py`,
  `scripts/fanout_baseline.py`, and the Agentic Setup native discovery probes.
