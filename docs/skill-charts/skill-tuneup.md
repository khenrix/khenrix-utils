# skill-tuneup — flow

One deep target per run: baseline → research → council review 1 → audit → CHECKPOINT →
apply → eval → council review 2 → converge → ship. A read-only triage mode ranks and
stops instead. Source: `shared/skills/skill-tuneup/SKILL.md`.

```mermaid
flowchart TD
    accTitle: skill-tuneup deep-run flow
    accDescr: One skill per run - baseline, upstream research probed on all three CLIs, two council reviews, a user checkpoint, a capped eval-fix loop, severity-gated convergence, then ship. Triage ranks and stops.

    START([user names a target]) --> G_MODE{triage or<br/>deep run?}
    G_MODE -- "sweep / ranking ask" --> TRIAGE[rank khenrix skills<br/>read-only, no tokens, no edits] --> STOP_T([stop: present the worklist])
    G_MODE -- "one target" --> LOCATE[Step 2: locate repo + engines<br/>resolve tier via target-info]
    LOCATE --> G_CLEAN{working tree<br/>entirely clean?}
    G_CLEAN -- no --> HALT_D([stop: ask the user])
    G_CLEAN -- yes --> G_LOCK{lock acquired<br/>with an owner token?}
    G_LOCK -- refused --> HALT_L([stop: another run holds it])
    G_LOCK -- yes --> BASE[Step 4: baseline commit + stale-models<br/>+ prior run-log decisions]
    BASE --> RESEARCH[Step 5: upstream research<br/>every provider finding probed on ALL THREE CLIs]
    RESEARCH --> COUNCIL1[Step 6: council review 1 - the findings]
    COUNCIL1 --> AUDIT[Step 7: audit vs checklist<br/>incl. chart-vs-body drift]
    AUDIT --> G_CHECK{CHECKPOINT:<br/>user approves scope?}
    G_CHECK -- "trims / defers" --> AUDIT

    subgraph CYCLE [improvement cycle - repeats to a fixed point]
        APPLY[Step 8: edit source of truth + render<br/>update the target's chart if the flow changed]
        APPLY --> G_EVAL{eval green?<br/>cap 5 fix-iterations, RUN-GLOBAL}
        G_EVAL -- "cap reached" --> HAND([stop: hand unresolved to the user])
        G_EVAL -- green --> G_MAT{review-material<br/>exit 0?}
        G_MAT -- "exit 2 - fails closed" --> HAND
        G_MAT -- yes --> COUNCIL2[Step 9: council review 2 - the diff]
        COUNCIL2 --> RECORD[record every finding + a cycle-end marker]
        RECORD --> G_CONV{convergence-status<br/>verdict?}
        G_CONV -- keep-iterating --> APPLY
        G_CONV -- stalled --> HAND
    end

    G_CHECK -- approved --> APPLY
    G_CONV -- converged --> G_RECEIPT{verify-final-receipt:<br/>earned, full-panel, current?}
    G_RECEIPT -- no --> PANEL[run the full panel ONCE<br/>on the unchanged candidate] --> G_RECEIPT
    G_RECEIPT -- yes --> G_PRE{make precommit clean?}
    G_PRE -- no --> FIXPRE[fix in-scope, hand off unrelated] --> G_PRE
    G_PRE -- yes --> SHIP[one commit + khenrix-refresh<br/>+ release the lock] --> DONE([done])
```

## Gate evidence

| Gate | Kind | Evidence |
|---|---|---|
| G_MODE | agent | `evals/skill-tuneup/evals.json::Refuses to deep-tune every skill in one run` |
| G_CLEAN | agent | no eval covers this; SKILL.md Step 2 clean-tree rule — shipping stages with `git add -A`, so any unrelated edit would be swept into the tune-up commit |
| G_LOCK | code | `shared/skills/skill-tuneup/scripts/tuneup.py::def lock_acquire` |
| G_CHECK | agent | `evals/skill-tuneup/evals.json::Proposes the change as a checkpoint finding` |
| G_EVAL | code | `scripts/eval_harness.py::gate_ok` — the delta gate itself; the cap-5 rule beside it is an agent rule (SKILL.md non-negotiable) |
| G_MAT | code | `shared/skills/skill-tuneup/scripts/tuneup.py::def review_material` — exits 2 on a git error rather than returning "", because an empty result is what tells Step 9 there is nothing to review |
| G_CONV | code | `shared/skills/skill-tuneup/scripts/tuneup.py::a clean final cycle converges` |
| G_RECEIPT | code | `shared/skills/skill-tuneup/scripts/tuneup.py::def verify_final_receipt` |
| G_PRE | code | `Makefile::precommit` |
