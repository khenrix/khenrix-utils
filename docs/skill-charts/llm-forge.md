# llm-forge — flow

```mermaid
flowchart TD
    accTitle: llm-forge flow
    START([user asks for a hard change built several ways]) --> G_WORTH{worth ~19 provider calls and ~63 GB?}
    G_WORTH -- no --> COUNCIL([use llm-council instead])
    G_WORTH -- yes --> GATE[--start prints the quote]
    GATE --> G_AGREE{operator agrees at §5 step 2?}
    G_AGREE -- no --> STOP([nothing spent])
    G_AGREE -- yes --> FLEET[B1, three seats, one verifier clone each]
    FLEET --> FUSE[you fuse in the synthesis worktree]
    FUSE --> LEDGER[--ledger stores your claim ledger]
    LEDGER --> G_REVIEW{convene §13's panel?}
    G_REVIEW -- yes --> REVIEW[--review, then --verify-fix]
    G_REVIEW -- no --> COLLECT
    REVIEW --> COLLECT[--collect describes mergeability]
    COLLECT --> G_GC{reclaimed?}
    G_GC -- no --> LEAK([~63 GB stays on disk])
    G_GC -- yes --> DONE([handed over])
```

## Gate evidence

| Gate | Kind | Evidence |
|---|---|---|
| G_WORTH | agent | SKILL.md's cost box states the quote before any spend; the operator decides |
| G_AGREE | code | `tests/test_forge_gate.py::def test_a_quote_cannot_carry_a_number_nobody_could_agree_to` |
| G_REVIEW | code | `tests/test_forge_review.py::def test_a_round_nobody_answered_is_blocked_rather_than_degraded` |
| G_GC | code | `tests/test_forge_gc.py::def test` |
