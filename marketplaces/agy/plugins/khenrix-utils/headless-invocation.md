# Headless / full-permissions invocation

How to run any sibling agentic CLI on this machine **non-interactively, with all
permission prompts bypassed**, so one agent can shell out to another and capture a text
response — e.g. Claude drafts a plan, then asks Codex (or agy) to review it before acting.

> **Safety:** every command below bypasses all permission/sandbox checks. Use only in
> trusted workspaces or externally sandboxed environments — same caveat as the
> `clauded` / `aggy` / `codexo` launch aliases.

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
agy -p "Review this plan and flag risks/gaps:\n\n$(cat plan.md)" \
  --dangerously-skip-permissions
```

- `-p` / `--print` runs a single prompt non-interactively and prints the response.
- `--print-timeout <dur>` bounds the wait (default `5m`).
- `--add-dir <path>` widens the workspace.
- **Auth EOL (as of 2026-06):** consumer-OAuth Gemini/agy access is slated to wind down
  around mid-2026 — migrate to an API key / Antigravity sign-in. If agy fails with an
  auth/quota error (it prints nothing to stdout on a 429 and logs `RESOURCE_EXHAUSTED` /
  `Individual quota reached`), this is the likely cause; the council classifies it
  `auth_or_quota` and does not retry.

## Cross-review example

From inside one CLI, get a second opinion from another and capture it to a file:

```bash
# Claude drafted plan.md; ask Codex to critique it.
codex exec "$(cat plan.md)\n\nReview this implementation plan: call out risks, \
missing steps, and anything that won't work." \
  --dangerously-bypass-approvals-and-sandbox > codex-review.md
```

Swap the CLI and full-perms flag from the table above to route the review to whichever
model you want (e.g. `agy -p ... --dangerously-skip-permissions`).
