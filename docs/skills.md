# Shared skills

Khenrix Utils manages two skills for Claude Code, Codex, agy, and Maka. They are
copied directly from this repository. This selective install never installs or
enables the `khenrix-utils` marketplace. Optional generated plugin bundles may contain
other Khenrix skills for the separate broader reconcile flow, but never these two
native-only skills.

| Skill | Use it for | Main modes |
|---|---|---|
| `khenrix-quality` | Easier-to-follow replies, minimal AI-prose cleanup, and code-quality work | `adhd`, `prose-edit`, `prose-detect`, `code`, `code-workflow` |
| `khenrix-writing` | Humanizing prose, matching a voice sample, or a deeper rewrite | `humanize` |

The shared house style handles ordinary work automatically. A skill loads when
the request needs one of the modes above. You can describe the task normally or
name the skill and mode. For example:

- “Use `khenrix-quality` in ADHD mode and give me the first action.”
- “Audit this draft with `khenrix-quality`; do not rewrite it.”
- “Use the full code mode while implementing this change.”
- “Use `khenrix-writing` to humanize this text and preserve every fact.”

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
selective installer owns only the two composed skill directories; Agentic Setup removes
the retired legacy directories in its bounded migration.

## Install or update

Review the plan before applying it:

```bash
mise run skills:plan
mise run skills:apply -- --expect sha256:PLAN_ID
mise run skills:doctor
mise run skills:status
```

The controller owns only the two named skill directories and the bounded
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

Each composed skill records its sources, reviewed commit, relevant paths,
license, exact upstream and local license-copy paths, local adaptation, and path-tree
hash in `upstreams.toml`.

```bash
mise run skills:upstream-status
mise run skills:upstream-diff -- no-ai-slop
mise run skills:upstream-record -- no-ai-slop FULL_40_CHARACTER_COMMIT
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

After a successful apply, verify Maka's native routing separately:

```bash
mise run skills:maka-smoke
```

This runs one explicit case per composed skill, three grouped natural cases containing
every former skill name, plus a three-turn ADHD activation, action-first continuation,
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
