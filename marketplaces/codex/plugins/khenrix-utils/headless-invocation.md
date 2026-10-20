# Native headless invocation

Sibling CLIs run non-interactively using their normal installed configuration,
skills, authentication and authorized MCP tools. Fanout owns durable coordination,
exact session resumes and result verification; it does not replace the CLIs with a
tool-free model API.

## Trust and authority

Native seats are trusted processes running as the same user. Separate workspaces,
read-only instructions and known-writer denials are not OS confinement or
credential blindness. Managed organization policy still applies. If that policy
refuses a required read, report the actual refusal instead of changing policy or
claiming the tool was available.

Executors and evaluators may use installed skills and authorized read tools.
Repository and service writes belong to the invoking orchestrator after it
reconciles every configured final identity. Messages require separate approval
of exact content and destination. A full-permission command is not approval for
an external action.

## Supported invocation surfaces

| CLI | Non-interactive surface | Exact-session continuation |
|---|---|---|
| Claude | `claude --print --output-format json` | Recorded `--session-id` initially, then `--resume` that exact ID |
| Codex | `codex exec --json -` | `codex exec resume` with the recorded thread ID |
| agy | `agy --input-format stream-json --output-format stream-json` | `--conversation` with the recorded conversation ID |
| Maka | `maka run -` | `--resume` with the allocated native session ID |

Fanout's provider profiles construct the complete current argv and stdin envelope;
use those profiles rather than copying a partial command from this table.
Claude and agy support `--dangerously-skip-permissions`; Codex supports
`--dangerously-bypass-approvals-and-sandbox`. These bypass interactive prompts,
not managed policy. Their use does not make a process mechanically read-only.
Known service mutators are denied where the CLI supports exact per-tool denials,
without disabling the corresponding read tools.

Maka's native ask-mode `run` is not a general approval UI. A read outside its
managed workspace can require a sandbox-boundary expansion, and a configured MCP
tool can require a Session/provider/tool-specific grant. The non-interactive CLI
denies the former and stops on the latter; successful discovery is not a completed
read. A grant from a separate TUI client does not transfer to a new headless
provider identity. Do not write grant storage directly or claim a pregrant flag
the installed CLI does not expose.

The admitted trusted-native evaluator uses an explicitly preallocated exact
bypass Session and the ordinary installed launcher with `--yolo`. It binds the
Session identity, journal and actual tool evidence to the attempt and records
cleanup. This path has the same user's full filesystem and network access;
workspace separation and read-role instructions do not provide confinement.
An ask-mode refusal alone does not authorize that path or a new external action.

Planning, reviewing and merging use xhigh for Claude, Codex and Maka. Execution
uses medium. Gemini uses high in every role. Explicit supported model choices
remain recorded; a requested model is not proof of the served model.

## Version compatibility and evidence

Inspect the licensed installed CLI's `--help`, shipped declarations and actual
output. For public source, fetch the canonical upstream before reading it.
`make cli-sources` maintains public upstreams; it does not provide proprietary
source. Leaked or mirrored proprietary source is out of bounds.

New runs probe installed CLI contracts and versions. Updating a compatible CLI
does not disable an unchanged installed skill. An existing run, however, binds
its recorded versions and command profiles: drift refuses dispatch before a
provider intent or again at the process boundary. Use an explicit reviewed
transition or a fresh admission; never rewrite the old run's recorded identity.

A terminal successful CLI result is distinct from successful tool usage. Retain
actual tool starts/results and exact native session identities. Unknown token
usage, tool counts and served-model observations remain unknown, not zero or a
value inferred from the request.

## Evaluation and smoke checks

`scripts/eval_harness.py` uses the native `eval_provider.py` bridge and shared
bounded process supervisor. Executor runs use medium and evaluator runs xhigh
(Gemini high). Both keep normal installed read capabilities. Each native model
attempt has zero automatic transport retries. A malformed successful judge
verdict permits one fresh-workspace retry, retains both raw attempts, and refuses
a receipt if the second verdict is invalid.

Fresh evaluation receipts bind source, criteria, selected rendered inputs and
measurement instrument before spend and recheck them before issuance. Historical
receipt adoption is explicitly unverified; it cannot satisfy a final live gate.

`make smoke-llm-fanout` and `make benchmark-llm-fanout` use hermetic providers and
memory with real durable recovery. They never start paid models and do not
certify installed CLI read tools. Live work uses the public planner and executor
commands documented in `llm-fanout-plan` and `llm-fanout-execute`.

The retired Council/Forge engines and container certificate machinery are no
longer execution paths. Old private raw evidence remains historical; it is not
restamped as evidence for the native implementation.
