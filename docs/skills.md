# Shared skills

Khenrix Utils manages 17 skills for Claude Code, Codex, agy, and Maka. The two
Khenrix-authored skills live under `shared/skills/`; the 15 vendored Superpowers
skills live under `shared/superpowers/`. They are copied directly from this
repository. This selective install never installs or
enables the `khenrix-utils` marketplace. Optional generated plugin bundles may contain
other Khenrix skills for the separate broader reconcile flow, but never these
native-only skills.

| Skill | Use it for |
|---|---|
| `khenrix-quality` | Easier-to-follow replies, minimal AI-prose cleanup, and code-quality work. Modes: `adhd`, `prose-edit`, `prose-detect`, `code`, and `code-workflow`. |
| `khenrix-writing` | Humanizing prose, matching a voice sample, or a deeper rewrite. Mode: `humanize`. |
| `using-superpowers` | Load the relevant development-process skill before acting in a root session. |
| `brainstorming` | Turn an open-ended idea into an agreed design before implementation. |
| `writing-plans` | Turn an agreed design into a concrete implementation plan. |
| `executing-plans` | Execute a written plan in reviewable batches. |
| `subagent-driven-development` | Execute plan tasks through bounded sub-agents and staged review. |
| `dispatching-parallel-agents` | Investigate independent problems concurrently. |
| `test-driven-development` | Drive material behavior with a red-green-refactor loop. |
| `systematic-debugging` | Reproduce a failure and find its root cause before changing code. |
| `verification-before-completion` | Gather fresh evidence before claiming work is complete. |
| `requesting-code-review` | Prepare and request a focused code review. |
| `receiving-code-review` | Check review feedback against the code before applying it. |
| `finishing-a-development-branch` | Verify a branch, then merge, open a PR, keep it, or clean it up. |
| `using-git-worktrees` | Create an isolated worktree when the task benefits from one. |
| `writing-skills` | Create or revise a skill when this upstream workflow is explicitly requested. |
| `diagnosing-superpowers` | Diagnose Superpowers loading, routing, or installation failures. |

The shared house style loads `using-superpowers` at the start of a root session,
then selects a process skill when the work needs one. You can describe the task
normally or name a skill. For example:

- “Use `khenrix-quality` in ADHD mode and give me the first action.”
- “Audit this draft with `khenrix-quality`; do not rewrite it.”
- “Use the full code mode while implementing this change.”
- “Use `khenrix-writing` to humanize this text and preserve every fact.”
- “Use `systematic-debugging` to find the cause before changing anything.”
- “Use `writing-plans` to turn this design into implementation steps.”

`khenrix-quality` makes the smallest effective prose edit and preserves required
code behavior, tests, safety, accessibility, observability, and repository
conventions. Its review and audit modes stay read-only until fixes are requested.
ADHD mode can shape the surrounding response while another workflow skill owns
the task.

`khenrix-writing` owns deeper prose transformations. Its `humanize` mode
preserves facts, formatting, code, links, citations, and metadata. Do not run its
rewrite over the same text before or after `khenrix-quality`'s prose mode.

Natural-language requests that name the former `i-have-adhd`, `no-ai-slop`, `ponytail`,
`ponytail-review`, `ponytail-audit`, `ponytail-debt`, `ponytail-gain`,
`ponytail-help`, or `humanizer` route to the matching mode. The corresponding legacy
command forms `/<former-name>`, `$<former-name>`, and `/skill:<former-name>` are retired.
Invoke `khenrix-quality` or `khenrix-writing` with the matching mode instead. The
selective installer owns those two composed skills and the 15 verbatim Superpowers
directories; Agentic Setup removes the retired legacy directories in its bounded migration.

Routing keeps one workflow in charge. An explicitly named skill wins first, followed
by a domain or end-to-end owner such as `fix-jira-ticket`, `agentic-setup`, Khenrix
setup/upgrade, or `llm-forge`. Use `mikado-graph` for dependency decomposition and
Superpowers for a generic development process. `khenrix-quality` shapes the result
inside that owner. `brainstorming` is for open-ended design and does not reopen an
accepted plan. Upstream names such as `superpowers:systematic-debugging` mean the
plain native skill `systematic-debugging` in this installation.

The brainstorming visual companion is optional. When the user accepts it, launch
it only through its managed script, which always sets
`SUPERPOWERS_DISABLE_TELEMETRY=1`; otherwise leave it closed. Every vendored file
matches upstream except for that one manifest-declared privacy overlay.

## Install or update

Review the plan before applying it:

```bash
mise run skills:plan
mise run skills:apply -- --expect sha256:PLAN_ID
mise run skills:doctor
mise run skills:status
```

The controller owns only the 17 declared skill directories and the bounded
`khenrix-managed` instruction block. All sibling skills and text outside that
block are preserved.

| Consumer | Skill destination | Instruction file |
|---|---|---|
| Claude Code | `~/.claude/skills/<name>` | `~/.claude/CLAUDE.md` |
| Codex | `~/.agents/skills/<name>` | `~/.codex/AGENTS.md` |
| agy | `~/.gemini/config/skills/<name>` | `~/.gemini/GEMINI.md` |
| Maka | `~/.agents/skills/<name>` | `~/.maka/AGENTS.md` |

Apply writes a hash-bound receipt to
`~/.local/state/khenrix-utils/skills/install-receipt.json` and backs up every
managed path it changes. Restore the latest backup, or a named backup, with:

```bash
mise run skills:restore
mise run skills:restore -- --backup BACKUP_ID
```

Restore refuses to overwrite a managed skill that changed after apply. Changes
outside the instruction markers survive restore.

## Review upstream updates

The two composed Khenrix skills each record their sources, reviewed commit,
relevant paths, license, exact upstream and local license-copy paths, local
adaptation, and path-tree hash in `upstreams.toml`. The 15 Superpowers skill
trees are a verbatim snapshot of `obra/superpowers`; one central manifest under
`shared/superpowers/using-superpowers/` records their immutable commit, selected paths,
tree hash, license, and notice.

```bash
mise run skills:upstream-status
mise run skills:upstream-diff -- no-ai-slop
mise run skills:upstream-record -- no-ai-slop FULL_40_CHARACTER_COMMIT
mise run skills:upstream-diff -- superpowers
mise run skills:upstream-sync -- superpowers FULL_40_CHARACTER_COMMIT
```

`REPO_AHEAD` means only unrelated paths changed. `UPDATE` means a reviewed path
changed and needs inspection. `LICENSE_COPY_MISMATCH` means the local license is no
longer the exact file from the pinned revision. Review the diff and license first. Once
the commit is accepted, run `skills:upstream-record` to update the reviewed pin, path
hash, matching revision in `THIRD_PARTY_NOTICES.md`, and exact vendored license bytes as
one rollback-safe operation; it never overwrites the composed skill instructions.
Status, diff, and record fail if a notice no longer names its current pin and license
copy exactly once, or if the current license copy is stale. Then adapt the relevant
reference and run the behavior and routing evals. The pin and license copy are part of
the eval receipt's source closure, so recording after eval would immediately stale that
receipt.

If the upstream license bytes changed, record stops before writing. Review the license
diff, update the manifest license identifier and notice wording if needed, then acknowledge
that review explicitly:

```bash
mise run skills:upstream-record -- SOURCE FULL_40_CHARACTER_COMMIT --accept-license-change
```

For a Superpowers update, review the central diff and any license change, update all
15 directories and the pin as one operation, then run the delivery tests and routing
smoke. `skills:upstream-sync` performs the atomic copy and pin update. Pass
`--accept-license-change` only after reviewing a changed license. Do not patch one of
the copied directories as a private fork.

After a successful apply, verify Maka's native routing separately:

```bash
mise run skills:maka-smoke
```

This covers the declared direct-copy skills with explicit and natural cases, plus a
three-turn ADHD activation, action-first continuation,
and opt-out acknowledgement.
It checks Maka's recorded skill-loading events and writes a receipt beside the install receipt. The
cross-provider eval harness remains the behavior-quality gate.

## Add a writing mode

Keep `khenrix-writing` as a small router. Add a focused file under
`shared/skills/khenrix-writing/references/`, add one row to its mode table, and
give the mode distinct trigger language in the skill description. Record any
new upstream source and license, add behavior and routing eval cases, update the
flowchart, and complete the normal eval and delivery checks before applying it.

See [the skill-eval process](skill-eval-process.md) for the full gate.
