# Findings and diff review through fanout

Use the existing planner and executor CLI entries from `REVIEW_KU`, selected by
`self-target-rules.md`. Both reviews are mandatory. Each review is a fresh direct
question with two provider rounds; this does not reopen the tuneup implementation plan.

Put the complete question and bounded source evidence in a private question file.
Evidence outside the captured target repository must be included as excerpts or copied
into that baseline before start. Name exact relative evidence files, source commit and
paths; restricted seats cannot follow arbitrary host paths or run shell commands.

Admit the exact skill roots that supply any required reviewer skills, repeating
`--skill-root` in precedence order. Pass `--skill <exact-name>` for required skills.
Use `--none-reason` only when the supplied question/evidence is sufficient and no executor
skill is needed; never substitute a different skill or drop an unresolved requirement.

For a self-contained review, run:

```bash
python3 "$FANOUT_PLAN" from-question --question-file <private-question-file> --approval invocation-authorized --tier normal --skill-root <exact-review-skill-root> --none-reason "Review the supplied complete evidence; no executor skill is required"
```

Retain the complete admission packet in a private `0600` file. Use `--tier deep` when
the review requires it. The ingress uses Claude, Codex and agy, two rounds, zero retries,
and minimum-success two. If selecting other admitted executors, repeat `--executor` and
disclose the panel before launch. A replacement panel requires the user's explicit
selection, separate admission and disclosure; it is not automatic failure recovery.
Keep the existing minimum-success and synthesis rules.

Create fresh owner-only private, run, and authority roots outside the target repository.
Use the same exact admitted skill roots for start:

```bash
python3 "$FANOUT_EXECUTE" start --admission-file <private-packet> --repo-root "$REPO" --run-root <fresh-run-root> --authority-root <fresh-authority-root> --private-root <fresh-0700-private-root> --skill-root <exact-review-skill-root> --max-turns 6
python3 "$FANOUT_EXECUTE" status --private-root <private-root>
python3 "$FANOUT_EXECUTE" resume --private-root <private-root>
```

Six turns reserve three seats across two rounds with no transport replay. The existing
executor rechecks exact skills, installed profiles, caller baseline, pinned memory
receipt and authenticated worker health before spend. A failed preflight stops the
review; do not bypass memory or use an injected Python runner. V1 read-only is this
review path; v2 and uncertified repository-write gates do not grant review authority.

A round advances only after every expected seat has terminal evidence and enough valid
seats satisfy its admitted minimum-success. Invalid seats do not enter peer context.
After interruption, use status on the same durable run. Inspect exact uncertain turn
identities and use the documented `recover-uncertain` operation to exclude them before
resume; elapsed time or a missing terminal is never permission to replay possible spend.
Refresh the tuneup lock between bounded polls and around an existing user checkpoint.

At the final answer barrier:

```bash
python3 "$FANOUT_EXECUTE" inspect-answers --private-root <private-root> --task-id answer
python3 "$FANOUT_EXECUTE" synthesize-answer --private-root <private-root> --task-id answer --source-seat <final-seat-id-1> --source-seat <final-seat-id-2> --synthesizer-id <invoking-cli-id> --answer-file <private-owner-synthesis>
```

Read the private inspected answers and available blind-round evidence. Report every
failed/uncertain seat's terminal reason and any safe diagnostic, and disclose the source
commit and selected panel. Count only independent answers grounded in the supplied
evidence as corroboration; a second-round echo or inability to read required evidence
is a review quality failure even when transport-valid. The invoking CLI writes a new
synthesis; do not select or copy a seat's answer as owner judgment. Keep all raw evidence.
A partial panel can proceed only when the admitted quorum and final review acceptance
hold. Below that barrier, report an incomplete mandatory review instead of claiming a
one-seat or self-review fallback completed it.
