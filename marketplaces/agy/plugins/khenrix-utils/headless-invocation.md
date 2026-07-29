# Headless / full-permissions invocation

How to run any sibling agentic CLI on this machine **non-interactively, with all
permission prompts bypassed**, so one agent can shell out to another and capture a text
response — e.g. Claude drafts a plan, then asks Codex (or agy) to review it before acting.

> **Safety:** the full-perms flags below bypass all permission/sandbox checks — use them
> only in trusted workspaces or externally sandboxed environments (same caveat as the
> `clauded` / `aggy` / `codexo` launch aliases). For review-only invocations prefer each
> CLI's read-only variant instead: claude `--permission-mode plan`, codex
> `--sandbox read-only`, agy `--mode plan`.

## Per-CLI quick reference

| CLI               | Headless command            | Full-perms flag                            |
|-------------------|-----------------------------|--------------------------------------------|
| Claude Code       | `claude -p "<prompt>"`      | `--dangerously-skip-permissions`           |
| Codex             | `codex exec "<prompt>"`     | `--dangerously-bypass-approvals-and-sandbox` |
| Antigravity (agy) | `agy -p "<prompt>"`         | `--dangerously-skip-permissions`           |

### Claude Code

```bash
claude -p "Review this plan and flag risks/gaps:\n\n$(cat plan.md)" \
  --dangerously-skip-permissions
```

- `-p` / `--print` runs a single prompt and prints the response, then exits.
- `--output-format text|json` — `json` adds the structured result plus cost/usage.
- `--model <id>`, `--append-system-prompt "<text>"`, `--add-dir <path>` as needed.

### Codex

```bash
codex exec "Review this plan and flag risks/gaps:\n\n$(cat plan.md)" \
  --dangerously-bypass-approvals-and-sandbox

# or feed the prompt/context via stdin:
cat plan.md | codex exec --dangerously-bypass-approvals-and-sandbox \
  "Review this plan for risks, missing steps, and gaps"
```

- `codex exec` (alias `codex e`) is the non-interactive subcommand; with no prompt arg
  (or `-`), instructions are read from stdin.
- `codex exec review` / `codex review` run a code review against the current repo.
- `-m/--model <id>`, `-s/--sandbox <read-only|workspace-write|danger-full-access>`.

### Antigravity (agy)

```bash
agy --mode plan --dangerously-skip-permissions -p "Review this plan and flag risks/gaps:\n\n$(cat plan.md)"
# flags MUST precede -p (Go flag parsing stops at the first positional) — a flag placed
# after the prompt is silently dropped; use --mode plan for review-only invocations
```

- `-p` / `--print` runs a single prompt non-interactively and prints the response.
  (agy 1.1.1 fixed `-p` hanging when run from scripts/subprocesses — substantive
  prompts complete headless now; earlier versions reliably rode the timeout.)
- `--print-timeout <dur>` bounds the wait (default `5m`).
- `--model "<name>"` pins the model per-run (since 1.1.1; `agy models` prints slugs like
  `gemini-3.6-flash-high`, and the display label `"Gemini 3.6 Flash (High)"` resolves too —
  the thinking tier is encoded in the name, e.g. "Gemini 3.6 Flash (High)"). Since 1.1.2
  an unresolvable name hard-fails non-zero and lists the valid ones, rather than silently
  falling back to the default model.
- `--effort <low|medium|high>` sets reasoning effort separately (since 1.1.5). Note it
  caps at `high` — there is no Max tier for Flash by either route. The council leaves
  this unset and carries the tier in the model label instead, keeping one source of truth.
- `--mode plan` is a mechanical read-only mode that works headless (unlike `--sandbox`,
  which hung headless as of 2026-06-26, pre-1.1.1 — not re-probed since); use it for
  review-only invocations.
- **Pair `--mode plan` with `--dangerously-skip-permissions`, don't substitute it.** Per
  `agy --help` the flags are orthogonal: `--dangerously-skip-permissions` is
  "auto-approve all tool permission requests without prompting" (a *prompting* policy),
  while `--mode` sets the execution mode (`accept-edits`, `plan`). Plan mode alone leaves
  agy with tool prompts nobody can answer headlessly — it then soft-denies its **own**
  `ReadFile` at `tool_confirmation_manager.go:183` and answers from an empty context,
  producing a fluent one-sentence non-answer that looks like success. Plan mode is still
  the write barrier; auto-approve only removes an unanswerable prompt.
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

Swap the CLI and full-perms flag from the table above to route the review to whichever
model you want — but for agy put every flag BEFORE `-p` (Go flag parsing stops at the
first positional), and for review-only asks pair `--mode plan` WITH
`--dangerously-skip-permissions` rather than substituting one for the other
(e.g. `agy --mode plan --dangerously-skip-permissions -p "..."`). Plan mode is the write
barrier; auto-approve only removes a prompt nobody can answer headlessly. Dropping it is
what made agy soft-deny its own `ReadFile` and answer from an empty context.

## Where the answers actually live (verify, don't infer)

Run `make cli-sources` to sync these, then read them. Pulling is part of the ritual: a
stale checkout gives a confident wrong answer with a plausible citation.

| Question | Authoritative source | Status |
|---|---|---|
| What JSONL events does `codex exec --json` emit, and which are terminal? | `codex-rs/exec/src/exec_events.rs` (full Rust source, Apache-2.0) | **read** — validates `extract_codex_json`, including that `item.completed` is terminal for the ITEM ("either success or failure") and so must NOT decide the turn |
| What changed in claude between versions? | `anthropics/claude-code` CHANGELOG | **read** — the repo has no CLI source; the binary is compiled |
| What does claude's JSON result contain? | the licensed install's `sdk-tools.d.ts` + a live probe | **verified 2026-07-29 on 2.1.220** |
| What does agy emit headlessly? | no public source — live probe, plus `yuting0624/antigravity-for-claude-code` as a dated written record | **verified 2026-07-29 on 1.1.8** |

Measured on 2026-07-29, not inferred:

- **agy `--output-format json` writes ZERO bytes to stderr** (two probes). The diagnostic
  moves into the envelope, so for that seat there is nothing on stderr to sentinel-scan —
  `--log-file` is the only other channel. This is the good outcome: stderr was the phantom
  generator, because it echoed whatever files the seat read.
- **All three CLIs report real token usage**, so council cost is measured, never estimated:
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
