# Selective skill delivery

Khenrix Utils owns 17 public direct-copy skills: the composed
`khenrix-quality` and `khenrix-writing` skills plus the 15-skill Superpowers
bundle. The selective installer copies them directly to the native skill roots
and never installs or enables the `khenrix-utils` marketplace. Optional generated
plugin bundles explicitly exclude these skills, leaving one native copy and update
path. The broader reconcile flow remains available for other plugin content:

| Consumer | Destination |
|---|---|
| Claude Code | `~/.claude/skills/<name>` |
| Codex and Maka | `~/.agents/skills/<name>` |
| agy | `~/.gemini/config/skills/<name>` |

Only the names declared in `[skill_delivery].skills` are managed. Sibling skills
are never scanned, rewritten, or removed. The same controller reconciles the bounded
`khenrix-managed` house-style block in Claude, Codex, agy, and Maka instruction
files while preserving all text outside the markers. Per-CLI overlays declared
under `[instructions.overlays]` are rendered inside that same block, exactly as
the broader reconcile controller renders them, so the paths do not create
drift for each other.

## Install and inspect

```bash
mise run skills:plan
mise run skills:apply
mise run skills:doctor
mise run skills:status
```

`skills:plan` is read-only and prints the content hash for every source and
destination. To guard a delayed apply against source or destination changes,
copy its `sha256:…` plan ID and run:

```bash
mise run skills:apply -- --expect sha256:PLAN_ID
```

An apply backs up each changed managed path, verifies the resulting hashes, and
writes `~/.local/state/khenrix-utils/skills/install-receipt.json`. Existing
parents keep their modes. Public skill directories are normalized to `0755`,
with files at `0644` or `0755` according to their executable bit. Apply and
restore hold the owner-only `operation.lock` in the same state directory so an
Agentic Setup reconciliation cannot overlap a receipt refresh. Symlinked
managed paths are refused.

Restore the latest apply, or an explicit backup ID:

```bash
mise run skills:restore
mise run skills:restore -- --backup BACKUP_ID
```

A restore refuses when a managed skill changed after apply. Instruction restore
is block-scoped, so edits outside the Khenrix markers survive.

## Review upstream changes

The two composed Khenrix skills live under `shared/skills/` and each has an
`upstreams.toml`. The 15 vendored Superpowers skills live under
`shared/superpowers/`, with one central manifest under
`shared/superpowers/using-superpowers/`. Each source records
its repository, tracking ref, immutable reviewed commit, relevant paths, license name,
exact upstream license path, local vendored-license path, and a hash of the selected
Git tree entries.

```bash
mise run skills:upstream-status
mise run skills:upstream-diff -- no-ai-slop
mise run skills:upstream-record -- no-ai-slop FULL_40_CHARACTER_COMMIT
mise run skills:upstream-diff -- superpowers
mise run skills:upstream-sync -- superpowers FULL_40_CHARACTER_COMMIT
```

Status distinguishes `REPO_AHEAD` (only unrelated repository paths changed)
from `UPDATE` (one of the reviewed paths changed), and reports
`LICENSE_COPY_MISMATCH` when the vendored license is no longer byte-for-byte equal to
the pinned upstream file. Every source's current pin and local-license path must appear
exactly once in that skill's `THIRD_PARTY_NOTICES.md`; status, diff, and record fail when
the notice or current license copy has drifted. After the diff has been reviewed,
`record` updates the pin, tree hash, matching notice revision, and exact upstream license
bytes as one rollback-safe operation. It never overwrites composed skill instructions.
When upstream license bytes changed, record refuses until the reviewer has checked the
license diff and the manifest/notice wording, then explicitly supplies
`--accept-license-change`.
Review and accept the diff, record the reviewed commit, then adapt the relevant
Khenrix reference and run behavior and routing evals. Recording after eval would
stale the receipt because `upstreams.toml` is in the certified source closure.

The Superpowers snapshot retains the bytes and modes from its 15 selected upstream skill
directories except for one manifest-declared launcher overlay that disables the visual
companion's remote telemetry asset. Review the central diff and license before advancing
its pin, update the whole bundle atomically, then run the delivery tests and routing smoke.
Do not edit an individual Superpowers directory as a local fork. `skills:upstream-sync`
performs that atomic bundle update and reapplies the privacy overlay; use
`--accept-license-change` only after reviewing the license diff.

The `khenrix-upgrade` inventory combines skills from the optional plugin with
native skills declared in `[skill_delivery].skills` and confirmed for that CLI
by the selective install receipt. It ignores every undeclared sibling in the
native skill roots.

The path-tree hash is `sha256` over sorted `git ls-tree -r <commit> -- <paths>`
rows, each encoded exactly as `<mode> <type> <object>\t<path>\n`.

## Maka smoke

After apply, this opt-in command covers the declared direct-copy skills with explicit
and natural routing cases. It also runs a three-turn
ADHD-mode activation, action-first continuation, and bounded normal-mode
acknowledgement. Every new session starts in a fresh temporary directory.

```bash
mise run skills:maka-smoke
```

It never passes `--yolo`, caps steps and time, and writes a hash-bound receipt
beside the install receipt. The runner reads Maka's SQLite event log in read-only
mode: explicit cases require a successful canonical root-Turn admission receipt
from the `agents` source (with queued message admissions accepted for compatible
clients), while natural cases require a matching `skill_loaded` event with
`invocation=model_tool`. Both paths reject truncated instructions. When Maka
records SkillSearch shadow candidates, the loaded skill must be the first-ranked
candidate; malformed, missing, or lower-ranked shadow evidence fails closed. Raw
prompts and outputs stay out of the receipt; only their hashes, bounded sizes,
session/event IDs, routing evidence, and evidence hashes are retained.
