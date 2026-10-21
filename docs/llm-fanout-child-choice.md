# Optional child route choice

`fanout.child_choice` is an offline-testable selection boundary, disabled by
default. It is not connected to provider dispatch or the public execute command.
It does not launch native children, change an outer executor's model, choose an
owner/evaluator/planner/merger identity, or relax the nested-fanout guard.

An owner can construct `ChildRoute` values from existing exact `ExecutorProfile`
descriptors. Each route ID contains the profile binding key and descriptor digest;
model, effort, role, native roots or invocation changes therefore change the ID.
The owner supplies exact admitted IDs and separately observed usable IDs. A
backend never supplies either authority set.

`choose_child_route` filters those IDs before invoking a backend. Selection is
child-only, read-only and limited to the parent's provider by default. An explicit
`allow_cross_family=True` widens the family filter while retaining every admission,
usability, profile/session capability, native-root and child-timeout check. Profiles
must have executor role and local characterization evidence. That profile evidence
describes the existing native executor contract; it does not prove descendant
session or tool behavior.

One eligible route is returned as a fixed choice without a backend call. Otherwise
the caller must provide a backend, its matching backend-specific confidence policy,
and a local `ChoiceBudget`. Before a backend attempt, the budget bounds serialized
task/choice evidence and consumes one call. Failed attempts consume their call;
there are no automatic retries. The timeout cap filters child profiles but reserves
no child runtime, since this seam cannot launch anything. These limits are local
selection controls, not a durable run-wide financial ledger or token estimate.
The shared budget object is intended for a single owner; cross-process/concurrent
reservation and network timeouts belong to the eventual owner integration.

Results distinguish disabled, blocked, fixed, chosen, abstained and fallback
outcomes. Refusal, malformed answers, low confidence, backend mismatch/unavailability
and transport failure retain explicit reasons. A configured fallback must already
be in the filtered eligible set; it cannot restore a rejected provider or profile.
Every result has `native_launch_supported=False`. An ID alone grants no provider,
credential, account, project, region, tool, write, role or launch permission.

## Decisions adapter

`DecisionsBackend` accepts an injected `DecisionTransport(method, path, body)`;
there is no default HTTP client, SDK dependency, environment-key lookup or secret
discovery. The caller owns authentication, network access, timeout/cancellation and
any real API allocation. Calling the adapter directly does not perform local route
admission; owners use `choose_child_route` for that boundary.

The adapter sends `POST /v1/decisions` with `model="gpt-6-luna"`, text `input` and one
named `choice` question. Its 2–255 options use `value`/`description`. The response
is an `answers` array containing the matching question name and either `type:
"refusal"` or a choice with `choice`, `confidence` and `probabilities`. The adapter
checks those fields, supplied IDs and the probability distribution before accepting
an answer. This follows the [official Decisions API reference](https://developers.openai.com/api/reference/resources/decisions/methods/create).

Reported `usage` is retained as immutable JSON text without summing counters or
relabeling their semantics. Missing usage stays `None`, not zero. Confidence is
separate from the distribution's selected-option probability. Thresholds belong
to the configured backend and need labeled application examples before any
calibrated-accuracy claim, as described in the [official Decisions guide](https://developers.openai.com/api/docs/guides/decisions).

Another adapter, including a future Jev adapter, can implement `ChoiceBackend`:
give it a distinct `backend_id` and return `BackendChoice`. The owner must configure
that ID and its confidence threshold explicitly. No Jev integration or capability
claim is included here.

## Native descendants remain unsupported

This change supplies neither a child process adapter nor an enablement flag in
live configuration. Root-owned synthetic characterization may gather evidence,
but native descendant launch remains disabled until its complete contract is
measured and admitted:

| Boundary | Current claim |
| --- | --- |
| Exact child model/effort/profile and initial/resume session | Unsupported as a descendant launch contract |
| Parent-to-child tool and write restrictions | Unsupported as a descendant launch contract |
| Combined parent/child accounting and enforced runtime allocation | Unsupported as a descendant launch contract |
| Cancellation and complete descendant cleanup | Unsupported as a descendant launch contract |
| Parent/child trace correlation and complete native read proof | Unsupported as a descendant launch contract |

Existing parent-only audits remain parent-only. The selector's offline tests prove
filtering and parsing, not live native delegation, API availability, confidence
calibration or cost savings.
