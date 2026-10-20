# Self-target rules

Special handling when the tune-up target IS part of this skill's own machinery. The
danger: a tool reviewing its own under-test diff, or unbounded recursion.

## Target touches the reviewer runtime

Both reviews use the fanout planner and durable read-only executor. Before either launch,
inspect changes to `shared/lib/fanout/**`, both `shared/skills/llm-fanout-*/**`, and
`components/memory/**`. Include any other source in the selected runtime's actual import
or operator closure. A dirty reviewer must not review itself.

Extract the complete committed source tree so relative imports, both CLI entries,
capabilities, and memory modules all come from one commit:

```bash
REVIEW_KU=$(mktemp -d) || exit 1
REVIEW_COMMIT=$(git -C "$KU" rev-parse HEAD) || exit 1
REVIEW_ARCHIVE=$(mktemp) || exit 1
git -C "$KU" archive "$REVIEW_COMMIT" --output="$REVIEW_ARCHIVE" || exit 1
tar -x -f "$REVIEW_ARCHIVE" -C "$REVIEW_KU" || exit 1
FANOUT_PLAN="$REVIEW_KU/shared/skills/llm-fanout-plan/scripts/plan.py"
FANOUT_EXECUTE="$REVIEW_KU/shared/skills/llm-fanout-execute/scripts/execute.py"
```

Both archive and extraction must succeed before reviewer assignments or launch. Do not extract just one engine
file, blend dirty modules into its imports, or copy a current runtime into the archive.
Admit review skills from exact clean roots. Keep `REPO` pointing to the under-test target;
the question/evidence and captured baseline describe its candidate, while `REVIEW_KU`
supplies the independent committed reviewer. Do not execute helpers from the dirty target
as part of reviewer startup. Disclose the selected commit, runtime, panel and reason.

Committed profiles may name the previous models; report that fact instead of changing
the review panel silently. Installed-profile and memory preflight still apply. If that
committed runtime cannot meet the review contract, retain the failure and stop. An
alternative requires an explicitly user-selected independent reviewer, separately
admitted and disclosed; never quietly fall back or lower quorum.

Current native smoke and read probes need their existing spend authorization. A smoke
proves the observed transport/read behavior, not independent review quality or the final
skill receipt. Retired tools have no active tuneup or receipt path.

## Target = skill-tuneup (this skill)

- Audit, edits, and `tuneup.py --self-test` run normally — but follow the instructions
  as committed at HEAD, not the under-test working-tree copy you are editing.
- The tuneup lock (`skill-tuneup.lock.d`, Step 1 of SKILL.md) makes a literal nested
  self-run refuse to start; don't work around it.
- For the final review: fanout-review the diff under the runtime rule above,
  but the ultimate reviewer is the **user reading the diff** — say so explicitly.

## Target = khenrix-setup / khenrix-upgrade

Normal rules, plus: edits go to `shared/skill-templates/<t>/SKILL.md.tmpl` and the
`[skill_facts.<t>.<cli>]` tables — and `capabilities.toml` is in BOTH templated skills'
receipt closures, so a facts edit for one stales the other's receipt too. Budget a real
eval for **both**. Re-seeding is no longer an option even with sign-off:
`verify-final-receipt` rejects any receipt whose `provenance` isn't `"eval"`, so a seeded
receipt now fails the convergence gate it was meant to satisfy.
