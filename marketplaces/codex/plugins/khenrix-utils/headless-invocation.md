# Headless / full-permissions invocation

How to run any sibling agentic CLI on this machine **non-interactively, with all
permission prompts bypassed**, so one agent can shell out to another and capture a text
response — e.g. Claude drafts a plan, then asks Codex (or agy) to review it before acting.

> **Safety:** the full-perms flags below bypass all permission/sandbox checks — use them
> only in trusted workspaces or externally sandboxed environments (same caveat as the
> `clauded` / `aggy` / `codexo` launch aliases). For review-only invocations,
> Codex `--sandbox read-only` has a native filesystem barrier. Fanout's Claude
> read-only seat uses `--restricted` and a closed read-tool list; its agy seat
> and the legacy Council/Forge read-only agy path use private native deny rules
> and a path-confined pre-tool hook. Bare Claude or agy plan mode alone is not
> a verified no-write boundary. Use a guarded seat or omit agy.

## Per-CLI quick reference

Select effort by role without changing the CLI's managed configuration: execution
uses medium; planning, review, and merging use xhigh for Codex, Claude, and Maka.
Gemini stays high for every role. Codex review/merger launches explicitly pass
`-c 'model_reasoning_effort="xhigh"'`; Claude 2.1.284 uses `--effort xhigh` with
its native model/provider capability gate. Maka selects medium or xhigh through
the native session effort control. Existing explicit chat effort is preserved;
the relay forwards admitted medium and xhigh unchanged. The separate Maka judge
requires xhigh. No CLI automatically switches an existing session by inferred role.
Preserve explicit user choices and native managed constraints; report unsupported
selections instead of adding capability overrides.

| CLI               | Headless command            | Full-perms flag                            |
|-------------------|-----------------------------|--------------------------------------------|
| Claude Code       | `claude -p "<prompt>"`      | `--dangerously-skip-permissions`           |
| Codex             | `codex exec "<prompt>"`     | `--dangerously-bypass-approvals-and-sandbox` |
| Antigravity (agy) | `agy -p "<prompt>"`         | `--dangerously-skip-permissions`           |

### Claude Code

```bash
claude -p "Review this plan and flag risks/gaps:\n\n$(cat plan.md)" \
  --effort xhigh --dangerously-skip-permissions
```

- `-p` / `--print` runs a single prompt and prints the response, then exits.
- `--output-format text|json` — `json` adds the structured result plus cost/usage.
- `--model <id>`, `--append-system-prompt "<text>"`, `--add-dir <path>` as needed.

### Codex

```bash
codex exec "Review this plan and flag risks/gaps:\n\n$(cat plan.md)" \
  -c 'model_reasoning_effort="xhigh"' --dangerously-bypass-approvals-and-sandbox

# or feed the prompt/context via stdin:
cat plan.md | codex exec -c 'model_reasoning_effort="xhigh"' --dangerously-bypass-approvals-and-sandbox \
  "Review this plan for risks, missing steps, and gaps"
```

- `codex exec` (alias `codex e`) is the non-interactive subcommand; with no prompt arg
  (or `-`), instructions are read from stdin.
- `codex exec review` / `codex review` run a code review against the current repo.
- `-m/--model <id>`, `-s/--sandbox <read-only|workspace-write|danger-full-access>`.

### Antigravity (agy) — full permissions only

```bash
agy --dangerously-skip-permissions -p "Review this plan and flag risks/gaps:\n\n$(cat plan.md)"
# flags MUST precede -p (Go flag parsing stops at the first positional) — a flag placed
# after the prompt is silently dropped. This example permits writes.
```

- `-p` / `--print` runs a single prompt non-interactively and prints the response.
  (agy 1.1.1 fixed `-p` hanging when run from scripts/subprocesses — substantive
  prompts complete headless now; earlier versions reliably rode the timeout.)
- `--print-timeout <dur>` bounds the wait (default `5m`).
- `--model "<name>"` pins the model per-run (since 1.1.1; `agy models` prints slugs like
  `gemini-3.8-flash-high`, paired with the display label `"Gemini 3.8 Flash (High)"` —
  the thinking tier is encoded in the name). Since 1.1.2
  an unresolvable name hard-fails non-zero and lists the valid ones, rather than silently
  falling back to the default model.
- `--effort <low|medium|high>` sets reasoning effort separately on earlier Flash models
  (since 1.1.5), capped at `high`. **On Gemini 3.7 Flash it is refused outright, for all
  five values** (re-probed 2026-08-14 on agy 1.1.13): `low`/`medium`/`high` fail with
  `--effort is not supported for model "Gemini 3.7 Flash (High)"`; `ultra`/`max` fail with
  `invalid --effort ... (valid: low, medium, high)` — all non-zero. The council never
  passes this flag, carrying the tier in the model label instead (one source of truth),
  which is also now the only route that works.
- `--mode plan` is a planning instruction, **not** a mechanical read-only mode.
  A 2026-08-14 probe on agy 1.1.13 blocked one requested write, but a live
  2026-09-24 probe on agy 1.2.10 with `--mode plan
  --dangerously-skip-permissions` created a file in a disposable repository.
  The [execution-modes documentation](https://www.agy.dev/docs/cli/modes/)
  describes plan mode as prepending `/plan`; the
  [headless documentation](https://www.agy.dev/docs/cli/headless/) says
  `--dangerously-skip-permissions` approves tool calls. Removing the bypass alone
  soft-denied a harmless read in our headless probe, so it is not a usable
  read-only replacement. A guarded agy seat needs an isolated configuration,
  native deny rules, a fail-closed PreToolUse hook, and a live write-denial proof.
- The legacy `llm-council` and `llm-forge` read-only agy paths now remove the
  bypass flag and install a private per-seat native policy and path-confined
  pre-tool hook. A live agy 1.2.10 Council canary on 2026-09-24 read its
  assigned fixture, attempted a write, received the hook's denial, and left
  no file behind. Raw `agy --mode plan` remains unsafe to treat as read-only.
  The evaluation harness's private no-tools agent is a separate boundary.
- `--add-dir <path>` widens the workspace.
- **Auth EOL (as of 2026-06):** consumer-OAuth Gemini/agy access is slated to wind down
  around mid-2026 — migrate to an API key / Antigravity sign-in. If agy fails with an
  auth/quota error (it prints nothing to stdout on a 429 and logs `RESOURCE_EXHAUSTED` /
  `Individual quota reached`), this is the likely cause; the council classifies it
  `auth_or_quota`. Whether it retries depends on PROVENANCE: a reason scanned out of a
  merged stderr stream always retries (a seat that merely echoed a file naming those
  strings must not lose its seat), while a reason taken from the provider's own structured
  error field may be terminal if we recognise it. A missing binary is terminal either way.

## Cross-review example

From inside one CLI, get a second opinion from another and capture it to a file:

```bash
# Claude drafted plan.md; ask Codex to critique it.
codex exec "$(cat plan.md)\n\nReview this implementation plan: call out risks, \
missing steps, and anything that won't work." \
  --dangerously-bypass-approvals-and-sandbox > codex-review.md
```

Swap the CLI and full-perms flag from the table above only when the target may
write within an external sandbox. For a read-only agy review, use a proven
guarded seat or omit agy; adding `--mode plan` to the full-permissions example
does not make it safe. Put every agy flag before `-p` (Go flag parsing stops at
the first positional).

## Where the answers actually live (verify, don't infer)

Run `make cli-sources` to sync these, then read them. Pulling is part of the ritual: a
stale checkout gives a confident wrong answer with a plausible citation.

| Question | Authoritative source | Status |
|---|---|---|
| What JSONL events does `codex exec --json` emit, and which are terminal? | `codex-rs/exec/src/exec_events.rs` (full Rust source, Apache-2.0) | **read** — validates `extract_codex_json`, including that `item.completed` is terminal for the ITEM ("either success or failure") and so must NOT decide the turn |
| What changed in claude between versions? | `anthropics/claude-code` CHANGELOG | **read** — the repo has no CLI source; the binary is compiled |
| What does claude's JSON result contain? | the licensed install's `sdk-tools.d.ts` + a live probe | **verified 2026-07-29 on 2.1.220; re-probed 2026-08-16 on 2.1.233, shape unchanged** |
| What does agy emit headlessly? | no public source — live probe, plus `yuting0624/antigravity-for-claude-code` as a dated written record | **verified 2026-07-29 on 1.1.8** |

Measured on 2026-07-29, not inferred:

- **agy `--output-format json` writes ZERO bytes to stderr** (two probes). The diagnostic
  moves into the envelope, so for that seat there is nothing on stderr to sentinel-scan —
  `--log-file` is the only other channel. This is the good outcome: stderr was the phantom
  generator, because it echoed whatever files the seat read.
- The observed CLI envelopes included real token usage:
  agy `usage{input,output,thinking,cache_read,total}_tokens`; codex
  `TurnCompletedEvent{usage}` with `cached_input_tokens`/`cache_write_input_tokens`; claude
  `usage{input,output,cache_creation_input,cache_read_input}_tokens` **plus its own
  `total_cost_usd`** — for claude, prefer the CLI's cost over anything derived locally.
- **agy can emit a raw newline inside its JSON `"response"` string** (reported by the
  wrapper above; not reproduced on our 1.1.8 in two probes). Python's strict parser rejects
  a raw control character, turning one stray byte into a lost seat — so `extract_agy_json`
  parses with `strict=False`. A well-formed envelope parses identically either way.

Leaked or mirrored copies of a proprietary CLI are out of bounds for any vendor. When
something is closed, the licensed install on this machine plus a live probe IS the source.

The active skill-eval path is `scripts/eval_harness.py` through fanout's bounded
`eval_provider.py` bridge and shared process supervisor. It sends prompts on stdin,
disables memory/collaboration, retains raw attempts, and checks the entire no-tools
command before launch. Codex and agy use private empty homes; Claude's existing
safe-mode auth path keeps its real HOME with a private execution cwd. Missing usage
or served-model observations remain unknown. Only a verified unstarted spawn retries;
a malformed successful judge verdict has its separately bounded once-fresh retry.
Current review/smoke instructions use fanout; legacy council commands and deterministic
security suites remain available for historical compatibility.

Council/Forge source and evidence are historical-only under `capabilities.toml`.
They are excluded from optional plugin output, native delivery and current receipts.
The unchanged historical baseline lock verifies original source blobs, not an
installed consumer or a current live campaign. Prepare a fresh private snapshot
explicitly, outside the checkout, from the locally available original revision:

```bash
mise exec -- python scripts/fanout_baseline.py prepare-historical --root "$PWD" --historical-root /absolute/private/owned/snapshot
KHENRIX_HISTORICAL_BASELINE_ROOT=/absolute/private/owned/snapshot mise exec -- make verify
```

The owned parent must be mode `0700`; preparation writes only the lock's 59 original
blobs and unchanged lock to the snapshot, with provenance separately in its parent.
The deterministic historical gate requires that explicit root, refuses unsafe or
changed subjects, and performs no automatic fetch or regeneration. Existing current
live canaries and receipts still require their own fresh certification.
The release aggregate's fixed `make precommit` child receives only this explicitly
validated historical selector, not the ambient environment. Its retained attempt
and process proof include the original source provenance; reopening validates that
same stored subject, independently of the caller's later environment.
